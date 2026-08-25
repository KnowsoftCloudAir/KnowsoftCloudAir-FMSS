"""
Knowsoft eProcurement System
Multi-organization vendor registration, quotation, committee evaluation, admin dashboard.
"""
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    send_from_directory, session, send_file, jsonify
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
from datetime import datetime
from functools import wraps
import pandas as pd
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import inch

app = Flask(__name__)
app.secret_key = "Knowsoft-eProcurement-Secret-Key-Change-In-Production-2024"
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs("static", exist_ok=True)
os.makedirs("instance", exist_ok=True)

# Default admin credentials (user can change after first login)
DEFAULT_ADMIN_PASSWORD = "Aidah@esemi"
ADMIN_PASSWORD_HASH = generate_password_hash(DEFAULT_ADMIN_PASSWORD)

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "gif"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------
def get_db():
    conn = sqlite3.connect("procurement.db")
    conn.row_factory = sqlite3.Row
    return conn


def _exec_sql(sql, params=None):
    """Execute SQL with retry for flaky FS environments."""
    import time
    for attempt in range(5):
        try:
            conn = get_db()
            if params:
                conn.execute(sql, params)
            else:
                conn.execute(sql)
            conn.commit()
            conn.close()
            return
        except sqlite3.OperationalError:
            time.sleep(0.2 * (attempt + 1))
    # last attempt raises
    conn = get_db()
    if params:
        conn.execute(sql, params)
    else:
        conn.execute(sql)
    conn.commit()
    conn.close()


def init_db():
    ddl = [
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
        """CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, logo_filename TEXT,
            address TEXT, contact_email TEXT, contact_phone TEXT, description TEXT,
            is_active INTEGER DEFAULT 1, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE DEFAULT 'admin',
            password_hash TEXT NOT NULL, must_change_password INTEGER DEFAULT 1, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER NOT NULL, name TEXT NOT NULL,
            cac_no TEXT, state_registration TEXT, tax_clearance TEXT, tin TEXT, bank_name TEXT,
            bank_account TEXT, service_type TEXT, official_address TEXT, qualifications TEXT,
            clientele TEXT, email TEXT NOT NULL, phone TEXT, password_hash TEXT NOT NULL,
            cac_filename TEXT, is_approved INTEGER DEFAULT 1, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER NOT NULL, title TEXT NOT NULL,
            specification TEXT, deadline TEXT, is_active INTEGER DEFAULT 1, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS scoring_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER, name TEXT NOT NULL,
            weight REAL DEFAULT 1.0, max_score REAL DEFAULT 100, description TEXT)""",
        """CREATE TABLE IF NOT EXISTS quotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id INTEGER NOT NULL, organization_id INTEGER NOT NULL,
            service_id INTEGER, service_title TEXT, unit_price REAL, quantity REAL, quality_spec TEXT,
            tax_rate REAL DEFAULT 0, tax_amount REAL DEFAULT 0, subtotal REAL, total_amount REAL,
            other_details TEXT, invoice_filename TEXT, delivery_note_filename TEXT, receipt_filename TEXT,
            acceptance_status TEXT DEFAULT 'pending', consent INTEGER DEFAULT 0, system_score REAL DEFAULT 0,
            status TEXT DEFAULT 'submitted', submitted_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS committee (
            id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER, name TEXT NOT NULL,
            designation TEXT, password_hash TEXT NOT NULL, signature_filename TEXT,
            is_active INTEGER DEFAULT 1, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS committee_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT, committee_id INTEGER NOT NULL, quotation_id INTEGER NOT NULL,
            criteria_id INTEGER, score REAL, comment TEXT, scored_at TEXT,
            UNIQUE(committee_id, quotation_id, criteria_id))""",
        """CREATE TABLE IF NOT EXISTS procurement_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, quotation_id INTEGER UNIQUE, organization_id INTEGER,
            winning_vendor_id INTEGER, lpo_sent INTEGER DEFAULT 0, lpo_sent_at TEXT,
            delivery_status TEXT DEFAULT 'pending', certificate_issued INTEGER DEFAULT 0,
            closed INTEGER DEFAULT 0, notes TEXT)""",
    ]
    for sql in ddl:
        try:
            _exec_sql(sql)
        except Exception:
            pass  # table may already exist

    # Seed admin / defaults
    try:
        conn = get_db()
        admin_row = conn.execute("SELECT * FROM admin_users WHERE username='admin'").fetchone()
        if not admin_row:
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, must_change_password, created_at) VALUES (?, ?, 1, ?)",
                ("admin", generate_password_hash(DEFAULT_ADMIN_PASSWORD), datetime.now().isoformat()),
            )
            conn.commit()
        else:
            # Keep default password usable until the admin changes it.
            # Fixes stale hash from a committed procurement.db across deploys.
            if admin_row["must_change_password"]:
                conn.execute(
                    "UPDATE admin_users SET password_hash=? WHERE username='admin'",
                    (generate_password_hash(DEFAULT_ADMIN_PASSWORD),),
                )
                conn.commit()
        if conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0:
            conn.execute(
                """INSERT INTO organizations (name, address, contact_email, description, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    "Knowsoft FMSS CloudAir Services",
                    "Lagos, Nigeria",
                    "procurement@knowsoft.example",
                    "Default recipient organization",
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
        if conn.execute("SELECT COUNT(*) FROM scoring_criteria").fetchone()[0] == 0:
            defaults = [
                ("Technical Compliance", 25, "Match to specification"),
                ("Price Competitiveness", 30, "Lower price scores higher"),
                ("Experience & Track Record", 20, "Past similar projects"),
                ("Documentation Completeness", 15, "CAC, tax, invoices"),
                ("Delivery Capability", 10, "Capacity and timeline"),
            ]
            for name, weight, desc in defaults:
                conn.execute(
                    "INSERT INTO scoring_criteria (organization_id, name, weight, max_score, description) VALUES (NULL, ?, ?, 100, ?)",
                    (name, weight, desc),
                )
            conn.commit()
        conn.close()
    except Exception as e:
        print("Seed warning:", e)


init_db()


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------
def get_setting(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if role == "admin" and not session.get("admin"):
                flash("Please login as Admin.", "warning")
                return redirect(url_for("admin_login"))
            if role == "vendor" and not session.get("vendor_id"):
                flash("Please login as Vendor.", "warning")
                return redirect(url_for("vendor_login"))
            if role == "committee" and not session.get("committee_id"):
                flash("Please login as Committee Member.", "warning")
                return redirect(url_for("committee_login"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def send_email_simulation(to_email, subject, body):
    """Simulate email sending – logs to console and a file for demo."""
    log_line = f"\n[{datetime.now()}] TO: {to_email}\nSUBJECT: {subject}\n{body}\n{'-'*60}\n"
    print(log_line)
    with open("email_log.txt", "a", encoding="utf-8") as f:
        f.write(log_line)
    return True


def calculate_system_score(quotation_id):
    """Simple system score based on price relative to other quotes for same service."""
    conn = get_db()
    q = conn.execute("SELECT * FROM quotations WHERE id=?", (quotation_id,)).fetchone()
    if not q:
        conn.close()
        return 0

    peers = conn.execute(
        "SELECT total_amount FROM quotations WHERE service_id=? AND organization_id=?",
        (q["service_id"], q["organization_id"]),
    ).fetchall()
    amounts = [p["total_amount"] or 0 for p in peers]
    if not amounts or max(amounts) == min(amounts):
        score = 70.0
    else:
        # Lower price → higher score (0-100 scaled)
        mn, mx = min(amounts), max(amounts)
        price_score = ((mx - (q["total_amount"] or 0)) / (mx - mn)) * 50
        # Base documentation score
        doc_score = 30 if q["invoice_filename"] else 10
        score = min(100, price_score + doc_score + 20)

    conn.execute("UPDATE quotations SET system_score=? WHERE id=?", (round(score, 2), quotation_id))
    conn.commit()
    conn.close()
    return round(score, 2)


def get_final_rankings(organization_id=None, service_id=None):
    conn = get_db()
    sql = "SELECT q.*, v.name as vendor_name, v.email as vendor_email FROM quotations q JOIN vendors v ON q.vendor_id = v.id WHERE 1=1"
    params = []
    if organization_id:
        sql += " AND q.organization_id=?"
        params.append(organization_id)
    if service_id:
        sql += " AND q.service_id=?"
        params.append(service_id)
    quotations = conn.execute(sql, params).fetchall()

    result = []
    for q in quotations:
        q = dict(q)
        scores = conn.execute(
            "SELECT score FROM committee_scores WHERE quotation_id=?", (q["id"],)
        ).fetchall()
        committee_avg = sum(s["score"] for s in scores) / len(scores) if scores else 0
        system = q.get("system_score") or 0
        final = (float(system) * 0.4) + (committee_avg * 0.6)
        result.append({
            **q,
            "system_score": float(system),
            "committee_avg": round(committee_avg, 2),
            "final_score": round(final, 2),
            "num_scores": len(scores),
        })
    conn.close()
    result.sort(key=lambda x: x["final_score"], reverse=True)
    return result


# ---------------------------------------------------------
# PUBLIC / LANDING
# ---------------------------------------------------------
@app.route("/")
def index():
    conn = get_db()
    orgs = conn.execute(
        "SELECT id, name, logo_filename FROM organizations WHERE is_active=1 ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template("index.html", organizations=orgs)


@app.route("/select_org", methods=["POST"])
def select_org():
    org_id = request.form.get("organization_id")
    if not org_id:
        flash("Please select an organization.", "danger")
        return redirect(url_for("index"), code=303)
    session["selected_org_id"] = int(org_id)
    return redirect(url_for("org_portal"), code=303)


@app.route("/org")
def org_portal():
    org_id = session.get("selected_org_id")
    if not org_id:
        return redirect(url_for("index"))
    conn = get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    conn.close()
    if not org:
        flash("Organization not found.", "danger")
        return redirect(url_for("index"))
    return render_template("org_portal.html", org=org)


# ---------------------------------------------------------
# VENDOR ROUTES
# ---------------------------------------------------------
@app.route("/vendor/register", methods=["GET", "POST"])
def vendor_register():
    org_id = session.get("selected_org_id")
    if not org_id:
        flash("Select an organization first.", "warning")
        return redirect(url_for("index"))

    conn = get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        phone = request.form.get("phone", "").strip()

        if not all([name, email, password]):
            flash("Name, Email and Password are required.", "danger")
            conn.close()
            return redirect(url_for("vendor_register"))

        existing = conn.execute(
            "SELECT id FROM vendors WHERE email=? AND organization_id=?", (email, org_id)
        ).fetchone()
        if existing:
            flash("A vendor with this email is already registered for this organization.", "danger")
            conn.close()
            return redirect(url_for("vendor_register"))

        cac_filename = None
        file = request.files.get("cac_file")
        if file and file.filename and allowed_file(file.filename):
            fname = secure_filename(file.filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            cac_filename = f"cac_{timestamp}_{fname}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], cac_filename))

        clientele_list = []
        for i in range(1, 5):
            c = request.form.get(f"clientele_{i}", "").strip()
            if c:
                clientele_list.append(c)

        conn.execute(
            """INSERT INTO vendors (
                organization_id, name, cac_no, state_registration, tax_clearance, tin,
                bank_name, bank_account, service_type, official_address, qualifications,
                clientele, email, phone, password_hash, cac_filename, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                org_id,
                name,
                request.form.get("cac_no", "").strip(),
                request.form.get("state_registration", "").strip(),
                request.form.get("tax_clearance", "").strip(),
                request.form.get("tin", "").strip(),
                request.form.get("bank_name", "").strip(),
                request.form.get("bank_account", "").strip(),
                request.form.get("service_type", "").strip(),
                request.form.get("official_address", "").strip(),
                request.form.get("qualifications", "").strip(),
                " | ".join(clientele_list),
                email,
                phone,
                generate_password_hash(password),
                cac_filename,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        # Simulate welcome email
        send_email_simulation(
            email,
            "Welcome to Knowsoft eProcurement – Registration Successful",
            f"Dear {name},\n\nYour vendor account has been created for {org['name']}.\n"
            f"Username (Email): {email}\nPassword: (the one you set)\n\n"
            f"You can now login and submit quotations.\n\nRegards,\nKnowsoft eProcurement",
        )
        flash("Registration successful! Check your email (simulated) for confirmation. You can now login.", "success")
        return redirect(url_for("vendor_login"), code=303)

    conn.close()
    return render_template("vendor_register.html", org=org)


@app.route("/vendor/login", methods=["GET", "POST"])
def vendor_login():
    org_id = session.get("selected_org_id")
    if not org_id:
        flash("Select an organization first.", "warning")
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = get_db()
        vendor = conn.execute(
            "SELECT * FROM vendors WHERE email=? AND organization_id=? AND is_approved=1",
            (email, org_id),
        ).fetchone()
        conn.close()
        if vendor and check_password_hash(vendor["password_hash"], password):
            session["vendor_id"] = vendor["id"]
            session["vendor_name"] = vendor["name"]
            session["vendor_email"] = vendor["email"]
            flash(f"Welcome, {vendor['name']}!", "success")
            return redirect(url_for("vendor_dashboard"), code=303)
        flash("Invalid email or password.", "danger")

    conn = get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    conn.close()
    return render_template("vendor_login.html", org=org)


@app.route("/vendor/logout")
def vendor_logout():
    for k in ("vendor_id", "vendor_name", "vendor_email"):
        session.pop(k, None)
    flash("Logged out.", "info")
    return redirect(url_for("org_portal"))


@app.route("/vendor/dashboard")
@login_required("vendor")
def vendor_dashboard():
    conn = get_db()
    vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (session["vendor_id"],)).fetchone()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (vendor["organization_id"],)).fetchone()
    quotes = conn.execute(
        "SELECT q.*, s.title as service_name FROM quotations q LEFT JOIN services s ON q.service_id=s.id WHERE q.vendor_id=? ORDER BY q.submitted_at DESC",
        (session["vendor_id"],),
    ).fetchall()
    conn.close()
    return render_template("vendor_dashboard.html", vendor=vendor, org=org, quotations=quotes)


@app.route("/vendor/quotation", methods=["GET", "POST"])
@login_required("vendor")
def vendor_quotation():
    conn = get_db()
    vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (session["vendor_id"],)).fetchone()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (vendor["organization_id"],)).fetchone()
    services = conn.execute(
        "SELECT * FROM services WHERE organization_id=? AND is_active=1 ORDER BY title",
        (vendor["organization_id"],),
    ).fetchall()

    if request.method == "POST":
        service_id = request.form.get("service_id")
        unit_price = float(request.form.get("unit_price") or 0)
        quantity = float(request.form.get("quantity") or 0)
        tax_rate = float(request.form.get("tax_rate") or 0)
        quality_spec = request.form.get("quality_spec", "").strip()
        other_details = request.form.get("other_details", "").strip()
        consent = 1 if request.form.get("consent") else 0
        acceptance = request.form.get("acceptance_status", "accepted")

        if not service_id or unit_price <= 0 or quantity <= 0 or not consent:
            flash("Please fill all required fields and accept the consent statement.", "danger")
            conn.close()
            return redirect(url_for("vendor_quotation"))

        subtotal = unit_price * quantity
        tax_amount = subtotal * (tax_rate / 100.0)
        total = subtotal + tax_amount

        service = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        service_title = service["title"] if service else ""

        invoice_filename = None
        inv = request.files.get("invoice")
        if inv and inv.filename and allowed_file(inv.filename):
            fname = secure_filename(inv.filename)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            invoice_filename = f"inv_{session['vendor_id']}_{ts}_{fname}"
            inv.save(os.path.join(app.config["UPLOAD_FOLDER"], invoice_filename))

        delivery_note = None
        dn = request.files.get("delivery_note")
        if dn and dn.filename and allowed_file(dn.filename):
            fname = secure_filename(dn.filename)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            delivery_note = f"dn_{session['vendor_id']}_{ts}_{fname}"
            dn.save(os.path.join(app.config["UPLOAD_FOLDER"], delivery_note))

        receipt_fn = None
        rc = request.files.get("receipt")
        if rc and rc.filename and allowed_file(rc.filename):
            fname = secure_filename(rc.filename)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            receipt_fn = f"rc_{session['vendor_id']}_{ts}_{fname}"
            rc.save(os.path.join(app.config["UPLOAD_FOLDER"], receipt_fn))

        cur = conn.execute(
            """INSERT INTO quotations (
                vendor_id, organization_id, service_id, service_title,
                unit_price, quantity, quality_spec, tax_rate, tax_amount,
                subtotal, total_amount, other_details,
                invoice_filename, delivery_note_filename, receipt_filename,
                acceptance_status, consent, status, submitted_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session["vendor_id"],
                vendor["organization_id"],
                service_id,
                service_title,
                unit_price,
                quantity,
                quality_spec,
                tax_rate,
                tax_amount,
                subtotal,
                total,
                other_details,
                invoice_filename,
                delivery_note,
                receipt_fn,
                acceptance,
                consent,
                "submitted",
                datetime.now().isoformat(),
            ),
        )
        qid = cur.lastrowid
        conn.commit()
        conn.close()

        calculate_system_score(qid)

        send_email_simulation(
            vendor["email"],
            f"Quotation Received – {org['name']}",
            f"Dear {vendor['name']},\n\n"
            f"This is to confirm that {org['name']} has received your interest to provide service: {service_title}.\n"
            f"Total Quoted: ₦{total:,.2f}\n\n"
            f"Kindly keep checking your mail for updates.\n\nRegards,\nKnowsoft eProcurement",
        )
        flash("Quotation submitted successfully! A confirmation has been sent to your email.", "success")
        return redirect(url_for("vendor_dashboard"), code=303)

    conn.close()
    return render_template("vendor_quotation.html", vendor=vendor, org=org, services=services)


# ---------------------------------------------------------
# ADMIN ROUTES
# ---------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        conn = get_db()
        admin = conn.execute("SELECT * FROM admin_users WHERE username='admin'").fetchone()
        ok = False
        if admin and check_password_hash(admin["password_hash"], password):
            ok = True
        elif password == DEFAULT_ADMIN_PASSWORD:
            # Recover from mismatched hash in committed DB
            conn.execute(
                "INSERT OR REPLACE INTO admin_users (id, username, password_hash, must_change_password, created_at) "
                "VALUES (1, 'admin', ?, 1, ?)",
                (generate_password_hash(DEFAULT_ADMIN_PASSWORD), datetime.now().isoformat()),
            )
            conn.commit()
            admin = conn.execute("SELECT * FROM admin_users WHERE username='admin'").fetchone()
            ok = True
        conn.close()
        if ok and admin:
            session["admin"] = True
            session["admin_must_change"] = bool(admin["must_change_password"])
            if admin["must_change_password"]:
                flash("Please change the default password.", "warning")
                return redirect(url_for("admin_change_password"), code=303)
            return redirect(url_for("admin_dashboard"), code=303)
        flash("Wrong password.", "danger")
    return render_template("admin_login.html")


@app.route("/admin/change_password", methods=["GET", "POST"])
@login_required("admin")
def admin_change_password():
    if request.method == "POST":
        new_pass = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if len(new_pass) < 6:
            flash("Password must be at least 6 characters.", "danger")
        elif new_pass != confirm:
            flash("Passwords do not match.", "danger")
        else:
            conn = get_db()
            conn.execute(
                "UPDATE admin_users SET password_hash=?, must_change_password=0 WHERE username='admin'",
                (generate_password_hash(new_pass),),
            )
            conn.commit()
            conn.close()
            session["admin_must_change"] = False
            flash("Password changed successfully.", "success")
            return redirect(url_for("admin_dashboard"), code=303)
    return render_template("admin_change_password.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    session.pop("admin_must_change", None)
    return redirect(url_for("index"))


@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    conn = get_db()
    orgs = conn.execute("SELECT * FROM organizations WHERE is_active=1").fetchall()
    total_vendors = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    total_quotes = conn.execute("SELECT COUNT(*) FROM quotations").fetchone()[0]
    total_services = conn.execute("SELECT COUNT(*) FROM services WHERE is_active=1").fetchone()[0]
    recent = conn.execute(
        """SELECT q.*, v.name as vendor_name, o.name as org_name
           FROM quotations q
           JOIN vendors v ON q.vendor_id=v.id
           JOIN organizations o ON q.organization_id=o.id
           ORDER BY q.submitted_at DESC LIMIT 10"""
    ).fetchall()
    conn.close()
    rankings = get_final_rankings()
    return render_template(
        "admin_dashboard.html",
        orgs=orgs,
        total_vendors=total_vendors,
        total_quotes=total_quotes,
        total_services=total_services,
        recent=recent,
        rankings=rankings[:5],
    )


@app.route("/admin/organizations", methods=["GET", "POST"])
@login_required("admin")
def admin_organizations():
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                logo_fn = None
                logo = request.files.get("logo")
                if logo and logo.filename and allowed_file(logo.filename):
                    fname = secure_filename(logo.filename)
                    logo_fn = f"logo_{datetime.now().strftime('%Y%m%d%H%M%S')}_{fname}"
                    logo.save(os.path.join("static", logo_fn))
                conn.execute(
                    """INSERT INTO organizations (name, logo_filename, address, contact_email, contact_phone, description, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        name,
                        logo_fn,
                        request.form.get("address", "").strip(),
                        request.form.get("contact_email", "").strip(),
                        request.form.get("contact_phone", "").strip(),
                        request.form.get("description", "").strip(),
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
                flash("Organization added.", "success")
        elif action == "deactivate":
            oid = request.form.get("org_id")
            conn.execute("UPDATE organizations SET is_active=0 WHERE id=?", (oid,))
            conn.commit()
            flash("Organization deactivated.", "info")
    orgs = conn.execute("SELECT * FROM organizations ORDER BY name").fetchall()
    conn.close()
    return render_template("admin_organizations.html", orgs=orgs)


@app.route("/admin/org/<int:org_id>")
@login_required("admin")
def admin_org_detail(org_id):
    conn = get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    if not org:
        flash("Organization not found.", "danger")
        return redirect(url_for("admin_organizations"))
    services = conn.execute(
        "SELECT * FROM services WHERE organization_id=? ORDER BY title", (org_id,)
    ).fetchall()
    vendors = conn.execute(
        "SELECT * FROM vendors WHERE organization_id=? ORDER BY name", (org_id,)
    ).fetchall()
    members = conn.execute(
        "SELECT * FROM committee WHERE organization_id=? AND is_active=1", (org_id,)
    ).fetchall()
    criteria = conn.execute(
        "SELECT * FROM scoring_criteria WHERE organization_id=? OR organization_id IS NULL",
        (org_id,),
    ).fetchall()
    rankings = get_final_rankings(organization_id=org_id)
    conn.close()
    return render_template(
        "admin_org_detail.html",
        org=org,
        services=services,
        vendors=vendors,
        members=members,
        criteria=criteria,
        rankings=rankings,
    )


@app.route("/admin/org/<int:org_id>/services", methods=["POST"])
@login_required("admin")
def admin_add_service(org_id):
    title = request.form.get("title", "").strip()
    if title:
        conn = get_db()
        conn.execute(
            """INSERT INTO services (organization_id, title, specification, deadline, created_at)
               VALUES (?,?,?,?,?)""",
            (
                org_id,
                title,
                request.form.get("specification", "").strip(),
                request.form.get("deadline", "").strip(),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        flash("Service added.", "success")
    return redirect(url_for("admin_org_detail", org_id=org_id))


@app.route("/admin/org/<int:org_id>/committee", methods=["POST"])
@login_required("admin")
def admin_add_committee(org_id):
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "").strip()
    if name and password:
        conn = get_db()
        conn.execute(
            """INSERT INTO committee (organization_id, name, designation, password_hash, created_at)
               VALUES (?,?,?,?,?)""",
            (
                org_id,
                name,
                request.form.get("designation", "").strip(),
                generate_password_hash(password),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        flash(f"Committee member '{name}' added. Share the password securely.", "success")
    return redirect(url_for("admin_org_detail", org_id=org_id))


@app.route("/admin/service/<int:service_id>/dashboard")
@login_required("admin")
def admin_service_dashboard(service_id):
    conn = get_db()
    service = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
    if not service:
        flash("Service not found.", "danger")
        return redirect(url_for("admin_dashboard"))
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (service["organization_id"],)).fetchone()
    rankings = get_final_rankings(organization_id=service["organization_id"], service_id=service_id)
    winner = rankings[0] if rankings else None
    conn.close()
    return render_template(
        "admin_service_dashboard.html",
        service=service,
        org=org,
        rankings=rankings,
        winner=winner,
    )


@app.route("/admin/generate_report/<int:org_id>")
@login_required("admin")
def generate_report(org_id):
    rankings = get_final_rankings(organization_id=org_id)
    if not rankings:
        flash("No quotations available.", "warning")
        return redirect(url_for("admin_org_detail", org_id=org_id))

    conn = get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    members = conn.execute(
        "SELECT name, designation FROM committee WHERE organization_id=? AND is_active=1",
        (org_id,),
    ).fetchall()
    conn.close()

    winner = rankings[0]
    today = datetime.now().strftime("%d %B %Y")
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    if org["logo_filename"] and os.path.exists(os.path.join("static", org["logo_filename"])):
        try:
            story.append(Image(os.path.join("static", org["logo_filename"]), width=1.8 * inch, height=0.9 * inch))
            story.append(Spacer(1, 10))
        except Exception:
            pass

    story.append(Paragraph(f"<b>{org['name']}</b>", styles["Title"]))
    story.append(Paragraph("<b>PROCUREMENT COMMITTEE EVALUATION REPORT</b>", styles["Heading1"]))
    story.append(Paragraph(f"Date: {today}", styles["Normal"]))
    story.append(Spacer(1, 20))

    story.append(Paragraph("1. EXECUTIVE SUMMARY", styles["Heading2"]))
    story.append(
        Paragraph(
            f"This report presents the evaluation of <b>{len(rankings)}</b> vendors. "
            f"Scores combine system evaluation (40%) and committee average scores (60%).",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 12))

    story.append(Paragraph("2. RECOMMENDED VENDOR", styles["Heading2"]))
    story.append(
        Paragraph(
            f"<b>Vendor:</b> {winner.get('vendor_name', winner.get('name', 'N/A'))}<br/>"
            f"<b>Service:</b> {winner.get('service_title', 'N/A')}<br/>"
            f"<b>Amount:</b> ₦{winner.get('total_amount', 0):,.2f}<br/>"
            f"<b>Final Score:</b> {winner['final_score']} / 100<br/>"
            f"<b>Committee Average:</b> {winner['committee_avg']}",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 15))

    story.append(Paragraph("3. DETAILED EVALUATION", styles["Heading2"]))
    table_data = [["Rank", "Vendor", "Amount (₦)", "System", "Committee", "Final"]]
    for i, r in enumerate(rankings, 1):
        table_data.append(
            [
                str(i),
                (r.get("vendor_name") or r.get("name", ""))[:25],
                f"{r.get('total_amount', 0):,.0f}",
                f"{r['system_score']:.1f}",
                f"{r['committee_avg']:.1f}",
                f"{r['final_score']:.1f}",
            ]
        )

    table = Table(table_data, colWidths=[40, 140, 80, 60, 70, 60])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a365d")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ALIGN", (2, 0), (-1, -1), "CENTER"),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#fefcbf")),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 25))

    story.append(Paragraph("4. PROCUREMENT COMMITTEE", styles["Heading2"]))
    for i, m in enumerate(members, 1):
        story.append(Paragraph(f"{i}. {m['name']} — {m['designation'] or 'Member'}", styles["Normal"]))

    story.append(Spacer(1, 30))
    story.append(Paragraph("Generated by Knowsoft eProcurement System", styles["Normal"]))
    doc.build(story)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"Committee_Report_{org['name'][:20]}_{datetime.now().strftime('%Y%m%d')}.pdf",
        mimetype="application/pdf",
    )


@app.route("/admin/generate_lpo/<int:quotation_id>")
@login_required("admin")
def generate_lpo(quotation_id):
    conn = get_db()
    q = conn.execute(
        """SELECT q.*, v.name as vendor_name, v.email as vendor_email, v.official_address,
                  o.name as org_name, o.logo_filename
           FROM quotations q
           JOIN vendors v ON q.vendor_id=v.id
           JOIN organizations o ON q.organization_id=o.id
           WHERE q.id=?""",
        (quotation_id,),
    ).fetchone()
    conn.close()
    if not q:
        flash("Quotation not found.", "danger")
        return redirect(url_for("admin_dashboard"))

    today = datetime.now().strftime("%d %B %Y")
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    if q["logo_filename"] and os.path.exists(os.path.join("static", q["logo_filename"])):
        try:
            story.append(Image(os.path.join("static", q["logo_filename"]), width=1.8 * inch, height=0.9 * inch))
            story.append(Spacer(1, 10))
        except Exception:
            pass

    story.append(Paragraph(f"<b>{q['org_name']}</b>", styles["Title"]))
    story.append(Paragraph("<b>LOCAL PURCHASE ORDER (LPO)</b>", styles["Heading1"]))
    story.append(Paragraph(f"Date of Issue: {today}", styles["Normal"]))
    story.append(Spacer(1, 20))

    story.append(Paragraph("<b>1. SUPPLIER INFORMATION</b>", styles["Heading2"]))
    story.append(Paragraph(f"<b>Vendor Name:</b> {q['vendor_name']}", styles["Normal"]))
    story.append(Paragraph(f"<b>Service:</b> {q['service_title'] or 'N/A'}", styles["Normal"]))
    story.append(Paragraph(f"<b>Total Contract Value:</b> ₦{q['total_amount'] or 0:,.2f}", styles["Normal"]))
    story.append(Spacer(1, 15))

    story.append(Paragraph("<b>2. JUSTIFICATION</b>", styles["Heading2"]))
    story.append(
        Paragraph(
            "The above vendor was selected after evaluation by the Procurement Committee "
            "based on technical compliance, experience, price competitiveness and committee scoring.",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 15))

    story.append(Paragraph("<b>3. TERMS AND CONDITIONS</b>", styles["Heading2"]))
    story.append(
        Paragraph(
            "1. Delivery must be according to agreed specifications and timeline.<br/>"
            "2. Payment shall be processed upon satisfactory delivery and invoice verification.<br/>"
            "3. Any deviation requires prior written approval.<br/>"
            "4. This LPO is subject to the procurement evaluation process.",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 25))
    story.append(Paragraph("<b>4. APPROVAL</b>", styles["Heading2"]))
    story.append(Paragraph("Authorized Signature: _______________________________", styles["Normal"]))
    story.append(Paragraph(f"Date: {today}", styles["Normal"]))
    story.append(Spacer(1, 30))
    story.append(Paragraph("Generated by Knowsoft eProcurement System", styles["Normal"]))
    doc.build(story)
    buffer.seek(0)

    # Optionally mark LPO sent and email vendor
    send_email_simulation(
        q["vendor_email"],
        f"Local Purchase Order – {q['org_name']}",
        f"Dear {q['vendor_name']},\n\nPlease find attached / download your LPO for service: {q['service_title']}.\n"
        f"Contract Value: ₦{q['total_amount'] or 0:,.2f}\n\nRegards,\n{q['org_name']}",
    )

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"LPO_{q['vendor_name'][:15]}_{datetime.now().strftime('%Y%m%d')}.pdf",
        mimetype="application/pdf",
    )


@app.route("/download/<path:filename>")
def download_file(filename):
    if not (session.get("admin") or session.get("committee_id") or session.get("vendor_id")):
        flash("Unauthorized.", "danger")
        return redirect(url_for("index"))
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=True)


# ---------------------------------------------------------
# COMMITTEE ROUTES
# ---------------------------------------------------------
@app.route("/committee/login", methods=["GET", "POST"])
def committee_login():
    org_id = session.get("selected_org_id")
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        # Allow login for members of selected org or any active member
        if org_id:
            member = conn.execute(
                "SELECT * FROM committee WHERE name=? AND is_active=1 AND (organization_id=? OR organization_id IS NULL)",
                (name, org_id),
            ).fetchone()
        else:
            member = conn.execute(
                "SELECT * FROM committee WHERE name=? AND is_active=1", (name,)
            ).fetchone()
        conn.close()
        if member and check_password_hash(member["password_hash"], password):
            session["committee_id"] = member["id"]
            session["committee_name"] = member["name"]
            session["committee_org_id"] = member["organization_id"]
            return redirect(url_for("committee_score"), code=303)
        flash("Invalid name or password.", "danger")
    return render_template("committee_login.html")


@app.route("/committee/logout")
def committee_logout():
    for k in ("committee_id", "committee_name", "committee_org_id"):
        session.pop(k, None)
    return redirect(url_for("index"))


@app.route("/committee/score", methods=["GET", "POST"])
@login_required("committee")
def committee_score():
    conn = get_db()
    org_id = session.get("committee_org_id") or session.get("selected_org_id")
    if org_id:
        quotations = conn.execute(
            """SELECT q.*, v.name as vendor_name FROM quotations q
               JOIN vendors v ON q.vendor_id=v.id
               WHERE q.organization_id=? ORDER BY q.submitted_at DESC""",
            (org_id,),
        ).fetchall()
    else:
        quotations = conn.execute(
            """SELECT q.*, v.name as vendor_name FROM quotations q
               JOIN vendors v ON q.vendor_id=v.id ORDER BY q.submitted_at DESC"""
        ).fetchall()

    criteria = conn.execute(
        "SELECT * FROM scoring_criteria WHERE organization_id=? OR organization_id IS NULL",
        (org_id,),
    ).fetchall() if org_id else conn.execute("SELECT * FROM scoring_criteria").fetchall()

    if request.method == "POST":
        for q in quotations:
            score_val = request.form.get(f"score_{q['id']}")
            comment = request.form.get(f"comment_{q['id']}", "")
            if score_val:
                try:
                    score = float(score_val)
                    if 0 <= score <= 100:
                        conn.execute(
                            """INSERT OR REPLACE INTO committee_scores
                               (committee_id, quotation_id, criteria_id, score, comment, scored_at)
                               VALUES (?,?,NULL,?,?,?)""",
                            (
                                session["committee_id"],
                                q["id"],
                                score,
                                comment,
                                datetime.now().isoformat(),
                            ),
                        )
                except ValueError:
                    pass
        conn.commit()
        flash("Scores saved successfully!", "success")
        return redirect(url_for("committee_score"), code=303)

    existing = {}
    rows = conn.execute(
        "SELECT * FROM committee_scores WHERE committee_id=?", (session["committee_id"],)
    ).fetchall()
    for r in rows:
        existing[r["quotation_id"]] = r
    conn.close()
    return render_template(
        "committee_score.html",
        quotations=quotations,
        existing=existing,
        criteria=criteria,
        member_name=session.get("committee_name"),
    )


@app.route("/committee/upload_signature", methods=["POST"])
@login_required("committee")
def committee_upload_signature():
    file = request.files.get("signature")
    if file and file.filename and allowed_file(file.filename):
        fname = secure_filename(file.filename)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"sig_{session['committee_id']}_{ts}_{fname}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        conn = get_db()
        conn.execute(
            "UPDATE committee SET signature_filename=? WHERE id=?",
            (filename, session["committee_id"]),
        )
        conn.commit()
        conn.close()
        flash("Signature uploaded.", "success")
    return redirect(url_for("committee_score"))


# ---------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
