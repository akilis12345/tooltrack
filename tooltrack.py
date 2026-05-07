from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_mysqldb import MySQL 
from datetime import date, datetime, timedelta
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from apscheduler.schedulers.background import BackgroundScheduler
import MySQLdb.cursors 
import random
import os
import smtplib
import threading
app = Flask(__name__)
app.secret_key = "mysecretkey123"
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# =========================
# DATABASE CONFIG
# =========================
app.config['MYSQL_HOST'] = os.environ.get('MYSQL_HOST')
app.config['MYSQL_USER'] = os.environ.get('MYSQL_USER')
app.config['MYSQL_PASSWORD'] = os.environ.get('MYSQL_PASSWORD')
app.config['MYSQL_DB'] = os.environ.get('MYSQL_DB')

if not all([
    app.config['MYSQL_HOST'],
    app.config['MYSQL_USER'],
    app.config['MYSQL_PASSWORD'],
    app.config['MYSQL_DB']
]):
    raise Exception("Missing MYSQL environment variables")

# =========================
# MAIL CONFIG (FIXED)
# =========================


# INIT EXTENSIONS (ORDER MATTERS)
scheduler = BackgroundScheduler()
mysql = MySQL(app)  


def is_available(mysql, equipment_name, required_qty, total_qty, start_date, end_date):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cur.execute("""
        SELECT datefrom, dateto, quantity
        FROM borrow
        WHERE equipment_name = %s
        AND status = 1
    """, (equipment_name,))

    borrows = cur.fetchall()

    current_day = start_date
    end_day = end_date

    while current_day <= end_day:
        used = 0

        for b in borrows:
            b_from = b["datefrom"]
            b_to = b["dateto"]

            if b_from <= current_day <= b_to:
                used += b["quantity"]

        if total_qty - used < required_qty:
            cur.close()
            return False

        current_day += timedelta(days=1)

    cur.close()
    return True


def find_next_available_date(mysql, equipment_name, required_qty, total_qty, start_date, end_date):
    test_date = start_date
    duration = end_date - start_date

    for _ in range(365):
        new_start = test_date
        new_end = test_date + duration

        if is_available(mysql, equipment_name, required_qty, total_qty, new_start, new_end):
            return new_start

        test_date += timedelta(days=1)

    return None

# =========================
# AUTH ROUTES
# =========================

@app.route("/")
def home():
    return render_template("Login.html")

# =========================
# Log in
# =========================

@app.route("/login", methods=["POST"])
def login():
    username = request.form['username']
    password = request.form['password']

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM users WHERE username=%s AND password=%s", (username, password))
    user = cur.fetchone()
    cur.close()

    if user:
        if user["is_verified"] == 0:
            # ✅ FIX: store email for verification page
            session["pending_email"] = user["email"]
            session.modified = True

            flash("Please verify your email first.")
            return redirect(url_for("verify_page"))

        # ✅ Normal login
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['fullname'] = user['fullname']
        session['Purok'] = user['purok']

        if user['Admin'] == 1:
            return redirect(url_for("admin_dashboard"))
        else:
            return redirect(url_for("dashboard"))

    flash("Invalid username or password")
    return redirect(url_for("home"))

# =========================
# sign up
# =========================
 
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'GET':
        return render_template("signup.html")

    surname = request.form['surname']
    firstname = request.form['firstname']
    middleinitial = request.form['middleinitial']
    username = request.form['username']
    Gmail = request.form['Gmail']
    phone = request.form['phone']
    password = request.form['password']
    Purok = request.form['Purok']

    fullname = f"{surname}, {firstname} {middleinitial}"

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # Check if email exists
    cur.execute("SELECT * FROM users WHERE email=%s", (Gmail,))
    existing = cur.fetchone()

    if existing:
        if existing["is_verified"] == 0:
            code = str(random.randint(100000, 999999))

            cur.execute("""
                UPDATE users
                SET verification_code=%s
                WHERE email=%s
            """, (code, Gmail))

            mysql.connection.commit()

            # ✅ FIX
            session["pending_email"] = Gmail
            session.modified = True

            threading.Thread(
                target=send_verification_email,
                args=(Gmail, code)
            ).start()
            flash("Verification code resent.")
            return redirect(url_for("verify_page"))
        else:
            flash("Email already registered.")
            return redirect(url_for("home"))

    # New user
    code = str(random.randint(100000, 999999))

    cur.execute("""
        INSERT INTO users
        (fullname, username, password, email, phonenumber, Purok, verification_code, is_verified)
        VALUES (%s,%s,%s,%s,%s,%s,%s,0)
    """, (fullname, username, password, Gmail, phone, Purok, code))

    mysql.connection.commit()
    cur.close()

    # ✅ FIX
    session["pending_email"] = Gmail

    threading.Thread(
        target=send_verification_email,
        args=(Gmail, code)
    ).start()
    flash("Verification code sent.")

    return redirect(url_for("verify_page"))

# =========================
# Email Verification
# =========================

@app.route("/verify")
def verify_page():
    email = session.get("pending_email")

    if not email:
        flash("Session expired. Please login or register again.")
        return redirect(url_for("home"))

    return render_template("verify.html", email=email)

@app.route("/verify_code", methods=["POST"])
def verify_code():
    email = session.get("pending_email")  # ALWAYS use session
    code = request.form.get("code", "").strip()

    if not email:
        flash("Session expired. Please register again.")
        return redirect(url_for("home"))

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cur.execute("""
        SELECT * FROM users
        WHERE email=%s AND verification_code=%s
    """, (email, code))

    user = cur.fetchone()

    if not user:
        flash("Invalid verification code")
        return redirect(url_for("verify_page"))

    cur.execute("""
        UPDATE users
        SET is_verified=1
        WHERE email=%s
    """, (email,))

    mysql.connection.commit()
    cur.close()

    flash("Email verified! You can now log in.")
    session.pop("pending_email", None)

    return redirect(url_for("home"))

@app.route("/resend-code", methods=["POST"])
def resend_code():

    # Get email from session
    email = session.get("pending_email")

    print("EMAIL IN SESSION:", email)

    # If session expired or missing
    if not email:
        flash("Session expired. Please register or log in again.")
        return redirect(url_for("home"))

    try:
        # Generate new verification code
        code = str(random.randint(100000, 999999))

        # Update code in database
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        cur.execute("""
            UPDATE users
            SET verification_code = %s
            WHERE email = %s AND is_verified = 0
        """, (code, email))

        mysql.connection.commit()
        cur.close()

        # Send email again
        threading.Thread(
            target=send_verification_email,
            args=(email, code)
        ).start()

        flash("Verification code resent successfully.")

    except Exception as e:
        print("RESEND ERROR:", e)
        flash("Failed to resend code. Please try again.")

    return redirect(url_for("verify_page"))

def send_verification_email(email, code):
    try:
        message = Mail(
            from_email='tooltrack2026@gmail.com',  # must be verified in SendGrid
            to_emails=email,
            subject='Email Verification Code',
            plain_text_content=f'Your verification code is: {code}'
        )

        sg = SendGridAPIClient(os.environ.get("SENDGRID_API_KEY"))

        response = sg.send(message)

        print("EMAIL SENT:", response.status_code)

        return True

    except Exception as e:
        print("EMAIL ERROR:", str(e))
        return False


def send_return_email(email, fullname, equipment_name, due_date):
    try:
        message = Mail(
            from_email='tooltrack2026@gmail.com',  # must be verified in SendGrid
            to_emails=email,
            subject='Equipment Return Reminder',
            plain_text_content=f'''
Hello {fullname},

Reminder: your borrowed equipment is due today.

Equipment: {equipment_name}
Due Date: {due_date}

Please return it on time.

Thank you.
'''
        )

        sg = SendGridAPIClient(os.environ.get("SENDGRID_API_KEY"))

        response = sg.send(message)

        print("RETURN EMAIL SENT:", response.status_code)

    except Exception as e:
        print("EMAIL ERROR:", str(e))

@app.route("/Logout")
def Login():
    session.clear()
    return render_template("Login.html")

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/admin-dashboard")
def admin_dashboard():

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cur.execute("""
        SELECT COUNT(*) AS total
        FROM borrow
        WHERE status = 1
    """)
    request_count = cur.fetchone()["total"]

    cur.close()

    return render_template("admin_dashboard.html",
        request_count=request_count
    )

@app.route("/dashboard")
def dashboard():
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cur.execute("SELECT COUNT(*) AS total FROM equipments")
    equipment_count = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) AS total FROM borrow WHERE user_id=%s AND status=1", (session["user_id"],))
    active = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) AS total FROM borrow WHERE user_id=%s AND status=0", (session["user_id"],))
    pending = cur.fetchone()["total"]

    cur.close()

    return render_template(
        "welcome.html",
        equipment_count=equipment_count,
        active=active,
        pending=pending
    )

# =========================
# USER REQUESTS (PENDING)
# =========================

@app.route("/Pending")
def Pending():
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cur.execute("""
        SELECT id, equipment_name, quantity, datefrom, dateto, status
        FROM borrow
        WHERE user_id = %s
        ORDER BY id DESC
    """, (session['user_id'],))

    requests = cur.fetchall()
    cur.close()

    session["request_notif_seen"] = True

    return render_template("Pending.html", requests=requests)


@app.route("/borrow")
def borrow():
    if "user_id" not in session:
        return redirect(url_for("home"))

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM equipments")
    equipment = cur.fetchall()
    cur.close()

    # Ensure equipment is always a list, even if empty
    if equipment is None:
        equipment = []

    return render_template("borrow.html", equipment=equipment)

# =========================
# History (History)
# =========================

@app.route("/History")
def History():

    if "user_id" not in session:
        return redirect(url_for("home"))

    search_name = request.args.get("search_name", "")
    search_user = request.args.get("search_user", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    query = """
        SELECT id, fullname, purok, equipment_name, quantity, datefrom, dateto, status, description
        FROM borrow
        WHERE status IN (2, 3)
    """

    params = []

    # EQUIPMENT SEARCH
    if search_name:
        query += " AND equipment_name LIKE %s"
        params.append("%" + search_name + "%")

    # 👤 FULLNAME SEARCH (this is what you meant)
    if search_user:
        query += " AND fullname LIKE %s"
        params.append("%" + search_user + "%")

    # DATE FILTER
    if start_date and end_date:
        query += " AND datefrom >= %s AND dateto <= %s"
        params.append(start_date)
        params.append(end_date)

    query += " ORDER BY id DESC"

    cur.execute(query, params)
    requests = cur.fetchall()

    cur.execute("""
        SELECT COUNT(*) AS total
        FROM borrow
        WHERE status = 1
    """)
    borrow_request_count = cur.fetchone()["total"]

    cur.close()

    return render_template(
        "History.html",
        requests=requests,
        borrow_request_count=borrow_request_count
    )

@app.route("/users")
def users():

    if "user_id" not in session:
        return redirect(url_for("home"))

    search = request.args.get("search", "")

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    query = """
        SELECT id, fullname, username, purok, email, phonenumber
        FROM users
        WHERE 1=1
    """
    params = []

    if search:
        query += " AND (fullname LIKE %s OR username LIKE %s)"
        params.append("%" + search + "%")
        params.append("%" + search + "%")

    query += " ORDER BY fullname ASC"

    cur.execute(query, params)
    users = cur.fetchall()

    cur.execute("SELECT COUNT(*) AS total FROM borrow WHERE status = 1")
    borrow_request_count = cur.fetchone()["total"]

    cur.close()

    return render_template(
        "users.html",
        users=users,
        search=search,
        borrow_request_count=borrow_request_count
    )
#=============
# ADMIN INVENTORY
# =========================

@app.route("/inventoryA")
def inventoryA():
    search = request.args.get('search')
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    if search:
        cur.execute("""
            SELECT * FROM equipments
            WHERE equipmentname LIKE %s
        """, ('%' + search + '%',))
    else:
        cur.execute("SELECT * FROM equipments")

    equipment_list = cur.fetchall()

    # ✅ Calculate available for each item
    for item in equipment_list:
        cur.execute("""
            SELECT SUM(quantity) as borrowed
            FROM borrow
            WHERE equipment_name = %s
            AND status = 1
        """, (item["equipmentname"],))
        result = cur.fetchone()
        borrowed = result["borrowed"] if result["borrowed"] else 0
        item["available"] = item["quantity"] - borrowed

    # Count pending requests
    cur.execute("SELECT COUNT(*) AS total FROM borrow WHERE status = 1")
    borrow_request_count = cur.fetchone()["total"]

    cur.close()

    return render_template(
        "Admininv.html",
        equipment=equipment_list,
        borrow_request_count=borrow_request_count
    )
# =========================
# MANAGE SHI
# =========================
@app.route("/manage")
def manage():

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # equipment list
    cur.execute("SELECT * FROM equipments")
    equipment_list = cur.fetchall()

    # FIX: define the missing variable
    cur.execute("SELECT COUNT(*) AS total FROM borrow WHERE status = 1")
    borrow_request_count = cur.fetchone()["total"]

    cur.close()

    return render_template(
        "manage.html",
        equipment=equipment_list,
        borrow_request_count=borrow_request_count
    )

@app.route("/edit-quantity", methods=["POST"])
def update_quantity():
    equipment_id = request.form.get('id')
    action = request.form['action']
    value = int(request.form.get('value') or 0)

    cur = mysql.connection.cursor()

    if action == "delete":
        cur.execute("DELETE FROM equipments WHERE id = %s", (equipment_id,))

    elif action == "add":
        cur.execute("""
            UPDATE equipments
            SET quantity = quantity + %s
            WHERE id = %s
        """, (value, equipment_id))
        flash("Stock added successfully")

    elif action == "remove":
        cur.execute("""
            UPDATE equipments
            SET quantity = quantity - %s
            WHERE id = %s
        """, (value, equipment_id))

    mysql.connection.commit()
    cur.close()

    return redirect(url_for("manage"))

@app.route('/add-equipment', methods=['POST'])
def add_equipment():
    equipmentname = request.form['equipmentname']
    quantity = int(request.form['quantity'] or 0)
    description = request.form.get('description', '')
    paid = 1 if request.form.get('paid') else 0

    cur = mysql.connection.cursor()

    cur.execute("""
        INSERT INTO equipments
        (equipmentname, quantity, description, type)
        VALUES (%s, %s, %s, %s)
    """, (equipmentname, quantity, description, paid))

    mysql.connection.commit()
    cur.close()

    flash("Equipment successfully added")

    return redirect(url_for("manage"))

@app.route("/update-status", methods=["POST"])
def update_status():

    equipment_id = request.form["id"]
    status = request.form["status"]

    cur = mysql.connection.cursor()

    cur.execute("""
        UPDATE equipments
        SET status = %s
        WHERE id = %s
    """, (status, equipment_id))

    mysql.connection.commit()
    cur.close()

    flash("Equipment successfully modified")

    return redirect(url_for("manage"))


# ===============================================================================================
# CUSTOMER INVENTORY
# ===============================================================================================

@app.route("/Costumerinv")
def costumer_inv():
    search = request.args.get('search')

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    if search:
        cur.execute("""
            SELECT * FROM equipments
            WHERE equipmentname LIKE %s
        """, ('%' + search + '%',))
    else:
        cur.execute("SELECT * FROM equipments")

    equipment_list = cur.fetchall()

    # FIX: calculate real availability
    for item in equipment_list:
        cur.execute("""
            SELECT SUM(quantity) as borrowed
            FROM borrow
            WHERE equipment_name = %s
            AND status = 1
        """, (item["equipmentname"],))

        result = cur.fetchone()
        borrowed = result["borrowed"] if result["borrowed"] else 0

        item["available"] = item["quantity"] - borrowed

    # request count
    cur.execute("""
        SELECT COUNT(*) as total 
        FROM borrow 
        WHERE user_id = %s 
        AND status = 1
    """, (session["user_id"],))

    request_count = cur.fetchone()["total"]

    cur.close()

    return render_template(
        "Costumerinv.html",
        equipment=equipment_list,
        request_count=request_count
    )
# =========================
# BORROW SYSTEM (CART STYLE)
# =========================
@app.route("/add-to-borrow", methods=["POST"])
def add_to_borrow():

    if "user_id" not in session:
        return redirect(url_for("home"))

    equipment_id = request.form["id"]
    quantity = int(request.form["quantity"])
    datefrom = request.form["datefrom"]
    dateto = request.form["dateto"]

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cur.execute("""
        SELECT equipmentname, quantity
        FROM equipments 
        WHERE id = %s
    """, (equipment_id,))

    equipment = cur.fetchone()

    if not equipment:
        flash("Equipment not found")
        return redirect(url_for("borrow"))

    start_dt = datetime.strptime(datefrom, "%Y-%m-%d").date()
    end_dt = datetime.strptime(dateto, "%Y-%m-%d").date()
    today = datetime.now().date()

    if start_dt < today:
        flash("You cannot select a past date.")
        return redirect(url_for("borrow"))

    if start_dt > end_dt:
        flash("Invalid date range")
        return redirect(url_for("borrow"))

    if quantity <= 0:
        flash("Invalid quantity")
        return redirect(url_for("borrow"))

    if quantity > equipment["quantity"]:
        flash("Requested quantity exceeds stock")
        return redirect(url_for("borrow"))

    available = is_available(
        mysql,
        equipment["equipmentname"],
        quantity,
        equipment["quantity"],
        start_dt,
        end_dt
    )

    if not available:
        next_date = find_next_available_date(
            mysql,
            equipment["equipmentname"],
            quantity,
            equipment["quantity"],
            start_dt,
            end_dt
        )

        if next_date:
            flash(f"Not available. Try {next_date}")
        else:
            flash("No available dates found")

        return redirect(url_for("borrow"))

    if "borrow_list" not in session:
        session["borrow_list"] = []

    session["borrow_list"].append({
        "id": equipment_id,
        "name": equipment["equipmentname"],
        "quantity": quantity,
        "datefrom": datefrom,
        "dateto": dateto
    })

    session.modified = True

    flash("Added successfully!")
    return redirect(url_for("borrow"))

@app.route("/submit-borrow", methods=["POST"])
def submit_borrow():

    if "user_id" not in session:
        return redirect(url_for("home"))

    if "borrow_list" not in session or len(session["borrow_list"]) == 0:
        flash("No items to submit")
        return redirect(url_for("borrow"))

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    for item in session["borrow_list"]:
        cur.execute("""
            INSERT INTO borrow
            (user_id, fullname, purok, equipment_name, quantity, datefrom, dateto, status, email_sent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 0)
        """, (
            session["user_id"],
            session["fullname"],
            session["Purok"],
            item["name"],
            item["quantity"],
            item["datefrom"],
            item["dateto"]
        ))

    mysql.connection.commit()
    cur.close()

    session["borrow_list"] = []
    session.modified = True

    flash("Borrow request submitted!")
    return redirect(url_for("borrow"))

@app.route("/remove-item", methods=["POST"])
def remove_item():

    if "user_id" not in session:
        return redirect(url_for("home"))

    index = int(request.form["index"])

    if "borrow_list" in session:
        if 0 <= index < len(session["borrow_list"]):
            session["borrow_list"].pop(index)
            session.modified = True

    return redirect(url_for("borrow"))


# =========================
# REQUEST MANAGEMENT (ADMIN)
# =========================

@app.route("/Request")
def Request():
    if "user_id" not in session:
        return redirect(url_for("home"))

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT *
        FROM borrow
        WHERE status = 1
        ORDER BY id DESC
    """)
    requests = cur.fetchall()
    cur.close()

    # Ensure requests is always a list
    if requests is None:
        requests = []

    borrow_request_count = len(requests)  # safer than querying count separately

    return render_template(
        "Request.html",
        requests=requests,
        borrow_request_count=borrow_request_count
    )
@app.route("/update-request", methods=["POST"])
def update_request():

    if "user_id" not in session:
        return redirect(url_for("home"))

    request_id = request.form["id"]
    action = request.form["action"]
    description = request.form.get("description", "")

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # get borrow record
    cur.execute("SELECT * FROM borrow WHERE id = %s", (request_id,))
    item = cur.fetchone()

    if not item:
        flash("Request not found")
        return redirect(url_for("Request"))

    # always update description
    cur.execute("""
        UPDATE borrow
        SET description = %s
        WHERE id = %s
    """, (description, request_id))

    # get dates
    today = date.today()
    due_date = item["dateto"]

    # convert if stored as string
    if isinstance(due_date, str):
        due_date = datetime.strptime(due_date, "%Y-%m-%d").date()

    # RETURN PROCESS
    if action == "return_good":

        # ✅ CHECK IF LATE
        if today > due_date:
            status = 4   # LATE RETURN
        else:
            status = 2   # NORMAL RETURN

        cur.execute("""
            UPDATE borrow
            SET status = %s
            WHERE id = %s
        """, (status, request_id))

    elif action == "return_broken":

        cur.execute("""
            UPDATE borrow
            SET status = 3
            WHERE id = %s
        """, (request_id,))

    mysql.connection.commit()
    cur.close()

    return redirect(url_for("Request"))

def check_due_returns():
    with app.app_context():
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        today = date.today()

        cur.execute("""
            SELECT b.id, b.user_id, b.fullname, b.equipment_name, b.dateto, b.email_sent, u.email
            FROM borrow b
            JOIN users u ON b.user_id = u.id
            WHERE b.status = 1
        """)

        borrows = cur.fetchall()

        for b in borrows:
            due_date = b["dateto"]

            if isinstance(due_date, str):
                due_date = datetime.strptime(due_date, "%Y-%m-%d").date()

            # ✅ ONLY SEND ONCE ON DUE DATE
            if due_date == today and b["email_sent"] == 0:

                send_return_email(
                    b["email"],
                    b["fullname"],
                    b["equipment_name"],
                    b["dateto"]
                )

                # mark as sent (prevents spam)
                cur.execute("""
                    UPDATE borrow
                    SET email_sent = 1
                    WHERE id = %s
                """, (b["id"],))

                mysql.connection.commit()

        cur.close()

@app.before_request
def run_due_check_once():
    if "due_check_done" not in session:
        check_due_returns()
        session["due_check_done"] = True
# =========================
# RUN APP
# =========================
if __name__ == "__main__":
    app.run()
