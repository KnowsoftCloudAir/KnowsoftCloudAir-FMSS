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
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT DEFAULT 'super',
            full_name TEXT, is_active INTEGER DEFAULT 1,
            must_change_password INTEGER DEFAULT 1, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS admin_org_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            organization_id INTEGER NOT NULL,
            is_active INTEGER DEFAULT 1,
            assigned_by INTEGER,
            assigned_at TEXT,
            deactivated_at TEXT,
            UNIQUE(admin_id, organization_id))""",
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
                """INSERT INTO admin_users
                   (username, password_hash, role, full_name, is_active, must_change_password, created_at)
                   VALUES (?, ?, 'super', 'General Administrator', 1, 1, ?)""",
                ("admin", generate_password_hash(DEFAULT_ADMIN_PASSWORD), datetime.now().isoformat()),
            )
            conn.commit()
        else:
            # Keep default password usable until the admin changes it.
            # Fixes stale hash from a committed procurement.db across deploys.
            if admin_row["must_change_password"]:
                conn.execute(
                    "UPDATE admin_users SET password_hash=?, role=COALESCE(role, 'super') WHERE username='admin'",
                    (generate_password_hash(DEFAULT_ADMIN_PASSWORD),),
                )
                conn.commit()
            # Ensure role column has a value on older rows
            try:
                conn.execute("UPDATE admin_users SET role='super' WHERE role IS NULL OR role=''")
                conn.commit()
            except Exception:
                pass
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


def ensure_extra_columns():
    """Add award / progress / admin-role columns on existing databases."""
    alters = [
        "ALTER TABLE services ADD COLUMN procurement_stage TEXT DEFAULT 'open'",
        "ALTER TABLE services ADD COLUMN progress_percent REAL DEFAULT 0",
        "ALTER TABLE services ADD COLUMN awarded_quotation_id INTEGER",
        "ALTER TABLE quotations ADD COLUMN award_status TEXT DEFAULT 'none'",
        "ALTER TABLE quotations ADD COLUMN awarded_at TEXT",
        "ALTER TABLE quotations ADD COLUMN vendor_offer_response TEXT DEFAULT 'pending'",
        "ALTER TABLE quotations ADD COLUMN vendor_responded_at TEXT",
        "ALTER TABLE quotations ADD COLUMN lpo_available INTEGER DEFAULT 0",
        "ALTER TABLE procurement_records ADD COLUMN service_id INTEGER",
        "ALTER TABLE procurement_records ADD COLUMN progress_percent REAL DEFAULT 0",
        "ALTER TABLE procurement_records ADD COLUMN stage TEXT DEFAULT 'quotes_received'",
        "ALTER TABLE admin_users ADD COLUMN role TEXT DEFAULT 'super'",
        "ALTER TABLE admin_users ADD COLUMN full_name TEXT",
        "ALTER TABLE admin_users ADD COLUMN is_active INTEGER DEFAULT 1",
    ]
    for sql in alters:
        try:
            _exec_sql(sql)
        except Exception:
            pass
    # Ensure assignments table exists (for upgrades)
    try:
        _exec_sql(
            """CREATE TABLE IF NOT EXISTS admin_org_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                organization_id INTEGER NOT NULL,
                is_active INTEGER DEFAULT 1,
                assigned_by INTEGER,
                assigned_at TEXT,
                deactivated_at TEXT,
                UNIQUE(admin_id, organization_id))"""
        )
    except Exception:
        pass


ensure_extra_columns()


# Procurement stage labels and progress mapping
STAGE_META = {
    "open": {"label": "Open for quotes", "percent": 5},
    "quotes_received": {"label": "Quotes received", "percent": 25},
    "under_review": {"label": "Under review", "percent": 45},
    "vendor_selected": {"label": "Vendor selected", "percent": 60},
    "in_progress": {"label": "Service in progress", "percent": 80},
    "completed": {"label": "Service completed", "percent": 100},
}


def compute_service_stage(service_id):
    """Derive stage from quotations / award / acceptance data."""
    conn = get_db()
    service = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
    if not service:
        conn.close()
        return "open", 0, None

    quotes = conn.execute(
        "SELECT * FROM quotations WHERE service_id=?", (service_id,)
    ).fetchall()
    awarded = conn.execute(
        "SELECT * FROM quotations WHERE service_id=? AND award_status='awarded'",
        (service_id,),
    ).fetchone()
    conn.close()

    # Prefer explicit stage stored on service if completed/in_progress
    stored = (service["procurement_stage"] or "open") if "procurement_stage" in service.keys() else "open"
    if stored in ("completed", "in_progress") and awarded:
        meta = STAGE_META.get(stored, STAGE_META["open"])
        pct = service["progress_percent"] if service["progress_percent"] is not None else meta["percent"]
        return stored, pct, awarded

    if awarded:
        resp = awarded["vendor_offer_response"] if "vendor_offer_response" in awarded.keys() else "pending"
        if resp == "accepted":
            return "in_progress", STAGE_META["in_progress"]["percent"], awarded
        return "vendor_selected", STAGE_META["vendor_selected"]["percent"], awarded

    if not quotes:
        return "open", STAGE_META["open"]["percent"], None

    # Any committee scores?
    conn = get_db()
    scored = conn.execute(
        """SELECT COUNT(*) AS c FROM committee_scores cs
           JOIN quotations q ON cs.quotation_id=q.id WHERE q.service_id=?""",
        (service_id,),
    ).fetchone()["c"]
    conn.close()
    if scored > 0:
        return "under_review", STAGE_META["under_review"]["percent"], None
    return "quotes_received", STAGE_META["quotes_received"]["percent"], None

from datetime import datetime
from io import BytesIO
import pandas as pd
from flask import render_template, request, redirect, url_for, flash, send_file, session
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import inch


def get_quarter_date_range(year: int, quarter: int):
    """Return (start_iso, end_iso) for a calendar quarter."""
    if quarter == 1:
        start = f"{year}-01-01T00:00:00"
        end = f"{year}-03-31T23:59:59"
    elif quarter == 2:
        start = f"{year}-04-01T00:00:00"
        end = f"{year}-06-30T23:59:59"
    elif quarter == 3:
        start = f"{year}-07-01T00:00:00"
        end = f"{year}-09-30T23:59:59"
    else:
        start = f"{year}-10-01T00:00:00"
        end = f"{year}-12-31T23:59:59"
    return start, end



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
            if role == "super_admin" and (not session.get("admin") or session.get("admin_role") != "super"):
                flash("Only the general administrator can access this.", "danger")
                return redirect(url_for("admin_dashboard"))
            if role == "vendor" and not session.get("vendor_id"):
                flash("Please login as Vendor.", "warning")
                return redirect(url_for("vendor_login"))
            if role == "committee" and not session.get("committee_id"):
                flash("Please login as Committee Member.", "warning")
                return redirect(url_for("committee_login"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def is_super_admin():
    return session.get("admin") and session.get("admin_role") == "super"


def get_admin_assigned_org_ids():
    """Return list of organization IDs the current admin may manage.
    Super admin → all active orgs. Sub-admin → only active assignments.
    """
    if not session.get("admin"):
        return []
    conn = get_db()
    if is_super_admin():
        rows = conn.execute("SELECT id FROM organizations WHERE is_active=1").fetchall()
        conn.close()
        return [r["id"] for r in rows]
    admin_id = session.get("admin_id")
    rows = conn.execute(
        """SELECT organization_id FROM admin_org_assignments
           WHERE admin_id=? AND is_active=1""",
        (admin_id,),
    ).fetchall()
    conn.close()
    return [r["organization_id"] for r in rows]


def can_manage_org(org_id):
    """True if current admin may manage the given organization."""
    if not session.get("admin"):
        return False
    if is_super_admin():
        return True
    return int(org_id) in get_admin_assigned_org_ids()


def require_org_access(org_id):
    """Flash + redirect helper when sub-admin tries to touch an unassigned org."""
    if not can_manage_org(org_id):
        flash("You do not have permission to manage this organization.", "danger")
        return False
    return True


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
        """SELECT q.*, s.title as service_name, s.procurement_stage, s.progress_percent
           FROM quotations q
           LEFT JOIN services s ON q.service_id=s.id
           WHERE q.vendor_id=? ORDER BY q.submitted_at DESC""",
        (session["vendor_id"],),
    ).fetchall()
    conn.close()
    return render_template("vendor_dashboard.html", vendor=vendor, org=org, quotations=quotes)


@app.route("/vendor/accept_offer/<int:quotation_id>", methods=["POST"])
@login_required("vendor")
def vendor_accept_offer(quotation_id):
    conn = get_db()
    q = conn.execute(
        "SELECT * FROM quotations WHERE id=? AND vendor_id=?",
        (quotation_id, session["vendor_id"]),
    ).fetchone()
    if not q:
        conn.close()
        flash("Quotation not found.", "danger")
        return redirect(url_for("vendor_dashboard"), code=303)
    if (q["award_status"] if "award_status" in q.keys() else "none") != "awarded":
        conn.close()
        flash("This quotation has not been awarded yet.", "warning")
        return redirect(url_for("vendor_dashboard"), code=303)

    action = request.form.get("action", "accepted")
    if action not in ("accepted", "declined"):
        action = "accepted"
    conn.execute(
        "UPDATE quotations SET vendor_offer_response=?, vendor_responded_at=? WHERE id=?",
        (action, datetime.now().isoformat(), quotation_id),
    )
    if action == "accepted" and q["service_id"]:
        # Mark all other quotations for this service as "not awarded"
        conn.execute(
            """UPDATE quotations
               SET award_status='not_awarded', status='not_awarded'
               WHERE service_id=? AND id!=? AND (award_status IS NULL OR award_status IN ('none', 'pending'))""",
            (q["service_id"], quotation_id),
        )
        conn.execute(
            "UPDATE services SET procurement_stage=?, progress_percent=? WHERE id=?",
            ("in_progress", STAGE_META["in_progress"]["percent"], q["service_id"]),
        )
        # Optional: notify other vendors that they were not selected
        others = conn.execute(
            """SELECT v.email, v.name, q.service_title
               FROM quotations q
               JOIN vendors v ON q.vendor_id = v.id
               WHERE q.service_id=? AND q.id!=?""",
            (q["service_id"], quotation_id),
        ).fetchall()
        for o in others:
            send_email_simulation(
                o["email"],
                f"Quotation Outcome – {o['service_title'] or 'Service'}",
                f"Dear {o['name']},\n\n"
                f"Thank you for submitting a quotation for: {o['service_title'] or 'the service'}.\n"
                f"After evaluation, another vendor has been selected and has accepted the offer.\n"
                f"Your quotation status is now: Not Awarded.\n\n"
                f"We appreciate your interest and look forward to future opportunities.\n\n"
                f"Regards,\nKnowsoft eProcurement",
            )
    elif action == "declined" and q["service_id"]:
        conn.execute(
            "UPDATE services SET procurement_stage=?, progress_percent=? WHERE id=?",
            ("vendor_selected", STAGE_META["vendor_selected"]["percent"], q["service_id"]),
        )
    conn.commit()
    conn.close()
    flash(
        "You have accepted the offer. Service is now in progress. Other vendors have been marked as Not Awarded."
        if action == "accepted"
        else "You have declined the offer. The organization has been notified.",
        "success" if action == "accepted" else "warning",
    )
    return redirect(url_for("vendor_dashboard"), code=303)


@app.route("/vendor/download_lpo/<int:quotation_id>")
@login_required("vendor")
def vendor_download_lpo(quotation_id):
    """Vendor downloads LPO only after award."""
    conn = get_db()
    q = conn.execute(
        """SELECT q.*, v.name as vendor_name, v.email as vendor_email,
                  o.name as org_name, o.logo_filename
           FROM quotations q
           JOIN vendors v ON q.vendor_id=v.id
           JOIN organizations o ON q.organization_id=o.id
           WHERE q.id=? AND q.vendor_id=?""",
        (quotation_id, session["vendor_id"]),
    ).fetchone()
    conn.close()
    if not q:
        flash("Not found.", "danger")
        return redirect(url_for("vendor_dashboard"), code=303)
    if (q["award_status"] if "award_status" in q.keys() else "none") != "awarded":
        flash("LPO is only available after the contract is awarded.", "warning")
        return redirect(url_for("vendor_dashboard"), code=303)
    if not (q["lpo_available"] if "lpo_available" in q.keys() else 0):
        flash("LPO not yet released.", "warning")
        return redirect(url_for("vendor_dashboard"), code=303)
    return _build_lpo_pdf(q)


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
        username = request.form.get("username", "admin").strip() or "admin"
        password = request.form.get("password", "")
        conn = get_db()
        admin = conn.execute(
            "SELECT * FROM admin_users WHERE username=? AND (is_active=1 OR is_active IS NULL)",
            (username,),
        ).fetchone()
        ok = False
        if admin and check_password_hash(admin["password_hash"], password):
            ok = True
        elif username == "admin" and password == DEFAULT_ADMIN_PASSWORD:
            # Recover from mismatched hash in committed DB (super admin only)
            conn.execute(
                """INSERT OR REPLACE INTO admin_users
                   (id, username, password_hash, role, full_name, is_active, must_change_password, created_at)
                   VALUES (1, 'admin', ?, 'super', 'General Administrator', 1, 1, ?)""",
                (generate_password_hash(DEFAULT_ADMIN_PASSWORD), datetime.now().isoformat()),
            )
            conn.commit()
            admin = conn.execute("SELECT * FROM admin_users WHERE username='admin'").fetchone()
            ok = True
        conn.close()
        if ok and admin:
            role = admin["role"] if "role" in admin.keys() and admin["role"] else "super"
            session["admin"] = True
            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            session["admin_role"] = role
            session["admin_must_change"] = bool(admin["must_change_password"])
            if admin["must_change_password"]:
                flash("Please change the default password.", "warning")
                return redirect(url_for("admin_change_password"), code=303)
            return redirect(url_for("admin_dashboard"), code=303)
        flash("Invalid username or password.", "danger")
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
            admin_id = session.get("admin_id")
            if admin_id:
                conn.execute(
                    "UPDATE admin_users SET password_hash=?, must_change_password=0 WHERE id=?",
                    (generate_password_hash(new_pass), admin_id),
                )
            else:
                conn.execute(
                    "UPDATE admin_users SET password_hash=?, must_change_password=0 WHERE username=?",
                    (generate_password_hash(new_pass), session.get("admin_username", "admin")),
                )
            conn.commit()
            conn.close()
            session["admin_must_change"] = False
            flash("Password changed successfully.", "success")
            return redirect(url_for("admin_dashboard"), code=303)
    return render_template("admin_change_password.html")


@app.route("/admin/logout")
def admin_logout():
    for k in ("admin", "admin_id", "admin_username", "admin_role", "admin_must_change"):
        session.pop(k, None)
    return redirect(url_for("index"))


# ---------------------------------------------------------
# SUPER-ADMIN: Sub-admin management & org assignments
# ---------------------------------------------------------
@app.route("/admin/sub_admins", methods=["GET", "POST"])
@login_required("super_admin")
def admin_sub_admins():
    """General admin creates sub-admins and assigns/deactivates client (org) management rights."""
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create_sub":
            username = request.form.get("username", "").strip().lower()
            full_name = request.form.get("full_name", "").strip()
            password = request.form.get("password", "")
            if not username or not password or len(password) < 6:
                flash("Username and password (min 6 chars) are required.", "danger")
            else:
                existing = conn.execute(
                    "SELECT id FROM admin_users WHERE username=?", (username,)
                ).fetchone()
                if existing:
                    flash("Username already exists.", "danger")
                else:
                    conn.execute(
                        """INSERT INTO admin_users
                           (username, password_hash, role, full_name, is_active, must_change_password, created_at)
                           VALUES (?, ?, 'sub', ?, 1, 1, ?)""",
                        (
                            username,
                            generate_password_hash(password),
                            full_name or username,
                            datetime.now().isoformat(),
                        ),
                    )
                    conn.commit()
                    flash(f"Sub-admin '{username}' created. They must change password on first login.", "success")
        elif action == "assign_org":
            admin_id = request.form.get("admin_id")
            org_id = request.form.get("organization_id")
            if admin_id and org_id:
                existing = conn.execute(
                    "SELECT id, is_active FROM admin_org_assignments WHERE admin_id=? AND organization_id=?",
                    (admin_id, org_id),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE admin_org_assignments
                           SET is_active=1, assigned_by=?, assigned_at=?, deactivated_at=NULL
                           WHERE id=?""",
                        (session.get("admin_id"), datetime.now().isoformat(), existing["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO admin_org_assignments
                           (admin_id, organization_id, is_active, assigned_by, assigned_at)
                           VALUES (?, ?, 1, ?, ?)""",
                        (admin_id, org_id, session.get("admin_id"), datetime.now().isoformat()),
                    )
                conn.commit()
                flash("Organization assigned to sub-admin.", "success")
        elif action == "deactivate_assignment":
            assignment_id = request.form.get("assignment_id")
            if assignment_id:
                conn.execute(
                    """UPDATE admin_org_assignments
                       SET is_active=0, deactivated_at=? WHERE id=?""",
                    (datetime.now().isoformat(), assignment_id),
                )
                conn.commit()
                flash("Assignment deactivated. Sub-admin no longer manages that client.", "info")
        elif action == "deactivate_sub":
            admin_id = request.form.get("admin_id")
            if admin_id and int(admin_id) != session.get("admin_id"):
                conn.execute(
                    "UPDATE admin_users SET is_active=0 WHERE id=? AND role='sub'",
                    (admin_id,),
                )
                conn.execute(
                    """UPDATE admin_org_assignments SET is_active=0, deactivated_at=?
                       WHERE admin_id=?""",
                    (datetime.now().isoformat(), admin_id),
                )
                conn.commit()
                flash("Sub-admin deactivated and all assignments revoked.", "info")
        return redirect(url_for("admin_sub_admins"), code=303)

    sub_admins = conn.execute(
        "SELECT * FROM admin_users WHERE role='sub' ORDER BY username"
    ).fetchall()
    orgs = conn.execute(
        "SELECT id, name FROM organizations WHERE is_active=1 ORDER BY name"
    ).fetchall()
    assignments = conn.execute(
        """SELECT a.*, u.username, u.full_name, o.name as org_name
           FROM admin_org_assignments a
           JOIN admin_users u ON a.admin_id = u.id
           JOIN organizations o ON a.organization_id = o.id
           ORDER BY u.username, o.name"""
    ).fetchall()
    conn.close()
    return render_template(
        "admin_sub_admins.html",
        sub_admins=sub_admins,
        orgs=orgs,
        assignments=assignments,
    )


@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    conn = get_db()
    allowed_orgs = get_admin_assigned_org_ids()
    if is_super_admin():
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
        rankings = get_final_rankings()
    else:
        if not allowed_orgs:
            orgs = []
            total_vendors = total_quotes = total_services = 0
            recent = []
            rankings = []
        else:
            placeholders = ",".join("?" * len(allowed_orgs))
            orgs = conn.execute(
                f"SELECT * FROM organizations WHERE is_active=1 AND id IN ({placeholders})",
                allowed_orgs,
            ).fetchall()
            total_vendors = conn.execute(
                f"SELECT COUNT(*) FROM vendors WHERE organization_id IN ({placeholders})",
                allowed_orgs,
            ).fetchone()[0]
            total_quotes = conn.execute(
                f"SELECT COUNT(*) FROM quotations WHERE organization_id IN ({placeholders})",
                allowed_orgs,
            ).fetchone()[0]
            total_services = conn.execute(
                f"SELECT COUNT(*) FROM services WHERE is_active=1 AND organization_id IN ({placeholders})",
                allowed_orgs,
            ).fetchone()[0]
            recent = conn.execute(
                f"""SELECT q.*, v.name as vendor_name, o.name as org_name
                    FROM quotations q
                    JOIN vendors v ON q.vendor_id=v.id
                    JOIN organizations o ON q.organization_id=o.id
                    WHERE q.organization_id IN ({placeholders})
                    ORDER BY q.submitted_at DESC LIMIT 10""",
                allowed_orgs,
            ).fetchall()
            rankings = []
            for oid in allowed_orgs:
                rankings.extend(get_final_rankings(organization_id=oid))
            rankings.sort(key=lambda x: x["final_score"], reverse=True)
    conn.close()
    return render_template(
        "admin_dashboard.html",
        orgs=orgs,
        total_vendors=total_vendors,
        total_quotes=total_quotes,
        total_services=total_services,
        recent=recent,
        rankings=rankings[:5],
        is_super=is_super_admin(),
        admin_role=session.get("admin_role", "super"),
        admin_username=session.get("admin_username", "admin"),
    )

def get_quarterly_completed(organization_id=None, year=None, quarter=None):
    """
    Return list of completed / awarded procurements for the given filters.
    Includes: service title, vendor, scores, amount, award date, status.
    """
    conn = get_db()
    sql = """
        SELECT
            q.id AS quotation_id,
            q.service_id,
            q.service_title,
            q.total_amount,
            q.system_score,
            q.award_status,
            q.awarded_at,
            q.submitted_at,
            q.lpo_available,
            v.id AS vendor_id,
            v.name AS vendor_name,
            v.email AS vendor_email,
            o.id AS organization_id,
            o.name AS org_name,
            s.title AS service_name,
            s.procurement_stage,
            s.progress_percent
        FROM quotations q
        JOIN vendors v ON q.vendor_id = v.id
        JOIN organizations o ON q.organization_id = o.id
        LEFT JOIN services s ON q.service_id = s.id
        WHERE q.award_status = 'awarded'
    """
    params = []

    if organization_id:
        sql += " AND q.organization_id = ?"
        params.append(organization_id)

    if year and quarter:
        start, end = get_quarter_date_range(int(year), int(quarter))
        # Prefer awarded_at; fall back to submitted_at
        sql += """ AND (
            (q.awarded_at IS NOT NULL AND q.awarded_at >= ? AND q.awarded_at <= ?)
            OR (q.awarded_at IS NULL AND q.submitted_at >= ? AND q.submitted_at <= ?)
        )"""
        params.extend([start, end, start, end])

    sql += " ORDER BY COALESCE(q.awarded_at, q.submitted_at) DESC"

    rows = conn.execute(sql, params).fetchall()

    result = []
    for r in rows:
        r = dict(r)
        # Committee average
        scores = conn.execute(
            "SELECT score FROM committee_scores WHERE quotation_id=?",
            (r["quotation_id"],)
        ).fetchall()
        committee_avg = sum(s["score"] for s in scores) / len(scores) if scores else 0.0
        system = float(r.get("system_score") or 0)
        final = (system * 0.4) + (committee_avg * 0.6)

        stage = r.get("procurement_stage") or "vendor_selected"
        progress = r.get("progress_percent")
        if progress is None:
            progress = 100 if stage == "completed" else 60

        result.append({
            **r,
            "system_score": round(system, 2),
            "committee_avg": round(committee_avg, 2),
            "final_score": round(final, 2),
            "num_scores": len(scores),
            "stage_label": STAGE_META.get(stage, {}).get("label", stage),
            "progress_percent": progress,
            "award_date": (r.get("awarded_at") or r.get("submitted_at") or "")[:10],
            "amount": float(r.get("total_amount") or 0),
            "service_display": r.get("service_name") or r.get("service_title") or "—",
        })
    conn.close()
    return result


@app.route("/admin/organizations", methods=["GET", "POST"])
@login_required("admin")
def admin_organizations():
    # Only super admin can create / deactivate organizations
    conn = get_db()
    if request.method == "POST":
        if not is_super_admin():
            flash("Only the general administrator can add or deactivate organizations.", "danger")
            return redirect(url_for("admin_organizations"), code=303)
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
    if is_super_admin():
        orgs = conn.execute("SELECT * FROM organizations ORDER BY name").fetchall()
    else:
        allowed = get_admin_assigned_org_ids()
        if allowed:
            placeholders = ",".join("?" * len(allowed))
            orgs = conn.execute(
                f"SELECT * FROM organizations WHERE id IN ({placeholders}) ORDER BY name",
                allowed,
            ).fetchall()
        else:
            orgs = []
    conn.close()
    return render_template(
        "admin_organizations.html",
        orgs=orgs,
        is_super=is_super_admin(),
    )


@app.route("/admin/org/<int:org_id>")
@login_required("admin")
def admin_org_detail(org_id):
    if not require_org_access(org_id):
        return redirect(url_for("admin_dashboard"), code=303)
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
        is_super=is_super_admin(),
    )

@app.route("/admin/quarterly_report", methods=["GET", "POST"])
@login_required("admin")
def admin_quarterly_report():
    """
    Quarterly Procurement Report page.
    Filters: Organization + Year + Quarter.
    Shows summary table of all awarded/completed procurements.
    """
    conn = get_db()
    allowed = get_admin_assigned_org_ids()
    if is_super_admin():
        orgs = conn.execute(
            "SELECT id, name FROM organizations WHERE is_active=1 ORDER BY name"
        ).fetchall()
    elif allowed:
        placeholders = ",".join("?" * len(allowed))
        orgs = conn.execute(
            f"SELECT id, name FROM organizations WHERE is_active=1 AND id IN ({placeholders}) ORDER BY name",
            allowed,
        ).fetchall()
    else:
        orgs = []
    conn.close()

    # Defaults
    current_year = datetime.now().year
    selected_org = request.args.get("org_id") or request.form.get("org_id") or ""
    selected_year = request.args.get("year") or request.form.get("year") or str(current_year)
    selected_quarter = request.args.get("quarter") or request.form.get("quarter") or str(
        (datetime.now().month - 1) // 3 + 1
    )

    try:
        org_id = int(selected_org) if selected_org else None
    except ValueError:
        org_id = None
    # Sub-admin may only query their assigned orgs
    if org_id and not can_manage_org(org_id):
        org_id = None
    if not is_super_admin() and not org_id and allowed:
        org_id = allowed[0] if len(allowed) == 1 else None

    try:
        year = int(selected_year)
        quarter = int(selected_quarter)
        if quarter not in (1, 2, 3, 4):
            quarter = 1
    except ValueError:
        year = current_year
        quarter = 1

    records = get_quarterly_completed(
        organization_id=org_id,
        year=year,
        quarter=quarter,
    )

    # Summary stats
    total_amount = sum(r["amount"] for r in records)
    total_count = len(records)
    avg_score = round(sum(r["final_score"] for r in records) / total_count, 2) if total_count else 0

    years = list(range(current_year - 3, current_year + 2))

    return render_template(
        "admin_quarterly_report.html",
        orgs=orgs,
        records=records,
        selected_org=str(org_id) if org_id else "",
        selected_year=str(year),
        selected_quarter=str(quarter),
        years=years,
        total_amount=total_amount,
        total_count=total_count,
        avg_score=avg_score,
        quarter_label=f"Q{quarter} {year}",
    )


@app.route("/admin/quarterly_report/pdf")
@login_required("admin")
def admin_quarterly_report_pdf():
    """Generate PDF of the quarterly procurement report."""
    selected_org = request.args.get("org_id") or ""
    selected_year = request.args.get("year") or str(datetime.now().year)
    selected_quarter = request.args.get("quarter") or "1"

    try:
        org_id = int(selected_org) if selected_org else None
    except ValueError:
        org_id = None
    try:
        year = int(selected_year)
        quarter = int(selected_quarter)
    except ValueError:
        year = datetime.now().year
        quarter = 1

    records = get_quarterly_completed(organization_id=org_id, year=year, quarter=quarter)

    conn = get_db()
    org_name = "All Organizations"
    if org_id:
        row = conn.execute("SELECT name FROM organizations WHERE id=?", (org_id,)).fetchone()
        if row:
            org_name = row["name"]
    conn.close()

    total_amount = sum(r["amount"] for r in records)
    total_count = len(records)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"<b>{org_name}</b>", styles["Title"]))
    story.append(Paragraph(
        f"<b>QUARTERLY PROCUREMENT REPORT – Q{quarter} {year}</b>",
        styles["Heading1"]
    ))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%d %B %Y %H:%M')}",
        styles["Normal"]
    ))
    story.append(Spacer(1, 12))

    story.append(Paragraph(
        f"<b>Summary:</b> {total_count} completed/awarded procurement(s) | "
        f"Total Value: ₦{total_amount:,.2f}",
        styles["Normal"]
    ))
    story.append(Spacer(1, 15))

    # Table
    table_data = [[
        "S/N", "Service / Item", "Vendor Awarded", "System", "Committee",
        "Final Score", "Amount (₦)", "Award Date", "Status"
    ]]

    for i, r in enumerate(records, 1):
        table_data.append([
            str(i),
            (r["service_display"] or "—")[:40],
            (r["vendor_name"] or "—")[:28],
            f"{r['system_score']:.1f}",
            f"{r['committee_avg']:.1f}",
            f"{r['final_score']:.1f}",
            f"{r['amount']:,.0f}",
            r["award_date"] or "—",
            r["stage_label"][:18],
        ])

    if not records:
        table_data.append(["—", "No completed procurements for this period", "", "", "", "", "", "", ""])

    col_widths = [30, 150, 110, 50, 55, 55, 75, 65, 80]
    table = Table(table_data, colWidths=col_widths)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a365d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (3, 0), (6, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    story.append(Spacer(1, 20))
    story.append(Paragraph(
        "This report covers all awarded procurements in the selected quarter. "
        "Scores = System (40%) + Committee Average (60%).",
        styles["Normal"]
    ))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Generated by Knowsoft eProcurement System", styles["Normal"]))

    doc.build(story)
    buffer.seek(0)

    filename = f"Quarterly_Procurement_Report_Q{quarter}_{year}_{org_name[:20].replace(' ', '_')}.pdf"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf",
    )


@app.route("/admin/quarterly_report/excel")
@login_required("admin")
def admin_quarterly_report_excel():
    """Export quarterly report as Excel (.xlsx)."""
    selected_org = request.args.get("org_id") or ""
    selected_year = request.args.get("year") or str(datetime.now().year)
    selected_quarter = request.args.get("quarter") or "1"

    try:
        org_id = int(selected_org) if selected_org else None
    except ValueError:
        org_id = None
    try:
        year = int(selected_year)
        quarter = int(selected_quarter)
    except ValueError:
        year = datetime.now().year
        quarter = 1

    records = get_quarterly_completed(organization_id=org_id, year=year, quarter=quarter)

    data = []
    for i, r in enumerate(records, 1):
        data.append({
            "S/N": i,
            "Organization": r.get("org_name"),
            "Service / Item Provided": r["service_display"],
            "Vendor Awarded": r["vendor_name"],
            "System Score": r["system_score"],
            "Committee Score": r["committee_avg"],
            "Final Score": r["final_score"],
            "Final Amount (₦)": r["amount"],
            "Award Date": r["award_date"],
            "Status": r["stage_label"],
            "Progress %": r["progress_percent"],
        })

    df = pd.DataFrame(data)
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=f"Q{quarter}_{year}")
    buffer.seek(0)

    filename = f"Quarterly_Procurement_Q{quarter}_{year}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/admin/org/<int:org_id>/services", methods=["POST"])
@login_required("admin")
def admin_add_service(org_id):
    if not require_org_access(org_id):
        return redirect(url_for("admin_dashboard"), code=303)
    title = request.form.get("title", "").strip()
    if title:
        conn = get_db()
        org = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
        org_name = org["name"] if org else "Organization"
        specification = request.form.get("specification", "").strip()
        deadline = request.form.get("deadline", "").strip()
        conn.execute(
            """INSERT INTO services (organization_id, title, specification, deadline, created_at)
               VALUES (?,?,?,?,?)""",
            (org_id, title, specification, deadline, datetime.now().isoformat()),
        )
        conn.commit()

        # Feature 3: all approved (prelisted) vendors of this org receive a notification
        vendors = conn.execute(
            "SELECT id, name, email FROM vendors WHERE organization_id=? AND is_approved=1",
            (org_id,),
        ).fetchall()
        notified = 0
        for v in vendors:
            if v["email"]:
                body = (
                    f"Dear {v['name']},\n\n"
                    f"A new service opportunity has been published by {org_name}.\n\n"
                    f"Service: {title}\n"
                )
                if specification:
                    body += f"Specification: {specification[:300]}{'…' if len(specification) > 300 else ''}\n"
                if deadline:
                    body += f"Deadline: {deadline}\n"
                body += (
                    f"\nYou are on the prelisted vendor list for this organization.\n"
                    f"Please log in to the eProcurement portal to view details and submit a quotation.\n\n"
                    f"Regards,\n{org_name}\nKnowsoft eProcurement"
                )
                send_email_simulation(
                    v["email"],
                    f"New Service Opportunity – {title}",
                    body,
                )
                notified += 1
        conn.close()
        flash(
            f"Service added. {notified} prelisted vendor(s) notified.",
            "success",
        )
    return redirect(url_for("admin_org_detail", org_id=org_id), code=303)


@app.route("/admin/org/<int:org_id>/committee", methods=["POST"])
@login_required("admin")
def admin_add_committee(org_id):
    if not require_org_access(org_id):
        return redirect(url_for("admin_dashboard"), code=303)
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
    return redirect(url_for("admin_org_detail", org_id=org_id), code=303)


@app.route("/admin/service/<int:service_id>/dashboard")
@login_required("admin")
def admin_service_dashboard(service_id):
    conn = get_db()
    service = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
    if not service:
        flash("Service not found.", "danger")
        return redirect(url_for("admin_dashboard"))
    if not require_org_access(service["organization_id"]):
        conn.close()
        return redirect(url_for("admin_dashboard"), code=303)
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (service["organization_id"],)).fetchone()
    rankings = get_final_rankings(organization_id=service["organization_id"], service_id=service_id)
    stage, percent, awarded = compute_service_stage(service_id)
    # Sync derived stage onto service if still early stages
    if stage in ("open", "quotes_received", "under_review", "vendor_selected"):
        try:
            conn.execute(
                "UPDATE services SET procurement_stage=?, progress_percent=? WHERE id=?",
                (stage, percent, service_id),
            )
            conn.commit()
            service = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        except Exception:
            pass
    conn.close()
    stage_label = STAGE_META.get(stage, {}).get("label", stage)
    return render_template(
        "admin_service_dashboard.html",
        service=service,
        org=org,
        rankings=rankings,
        stage=stage,
        stage_label=stage_label,
        progress_percent=percent,
        awarded=awarded,
        stage_meta=STAGE_META,
    )


@app.route("/admin/award/<int:quotation_id>", methods=["POST"])
@login_required("admin")
def admin_award_contract(quotation_id):
    conn = get_db()
    q = conn.execute(
        """SELECT q.*, v.name as vendor_name, v.email as vendor_email, o.name as org_name
           FROM quotations q
           JOIN vendors v ON q.vendor_id=v.id
           JOIN organizations o ON q.organization_id=o.id
           WHERE q.id=?""",
        (quotation_id,),
    ).fetchone()
    if not q:
        conn.close()
        flash("Quotation not found.", "danger")
        return redirect(url_for("admin_dashboard"), code=303)
    if not can_manage_org(q["organization_id"]):
        conn.close()
        flash("You do not have permission to manage this organization.", "danger")
        return redirect(url_for("admin_dashboard"), code=303)

    # Clear previous awards on same service
    if q["service_id"]:
        conn.execute(
            "UPDATE quotations SET award_status='none', lpo_available=0 WHERE service_id=? AND id!=?",
            (q["service_id"], quotation_id),
        )
        conn.execute(
            "UPDATE services SET procurement_stage=?, progress_percent=?, awarded_quotation_id=? WHERE id=?",
            (
                "vendor_selected",
                STAGE_META["vendor_selected"]["percent"],
                quotation_id,
                q["service_id"],
            ),
        )

    conn.execute(
        """UPDATE quotations SET award_status='awarded', awarded_at=?,
           vendor_offer_response='pending', lpo_available=1, status='awarded'
           WHERE id=?""",
        (datetime.now().isoformat(), quotation_id),
    )
    # Procurement record
    existing = conn.execute(
        "SELECT id FROM procurement_records WHERE quotation_id=?", (quotation_id,)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE procurement_records SET winning_vendor_id=?, lpo_sent=1, lpo_sent_at=?,
               stage='vendor_selected', progress_percent=?, service_id=? WHERE quotation_id=?""",
            (
                q["vendor_id"],
                datetime.now().isoformat(),
                STAGE_META["vendor_selected"]["percent"],
                q["service_id"],
                quotation_id,
            ),
        )
    else:
        conn.execute(
            """INSERT INTO procurement_records
               (quotation_id, organization_id, winning_vendor_id, lpo_sent, lpo_sent_at,
                stage, progress_percent, service_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                quotation_id,
                q["organization_id"],
                q["vendor_id"],
                1,
                datetime.now().isoformat(),
                "vendor_selected",
                STAGE_META["vendor_selected"]["percent"],
                q["service_id"],
            ),
        )
    conn.commit()
    conn.close()

    send_email_simulation(
        q["vendor_email"],
        f"Contract Awarded – {q['org_name']}",
        f"Dear {q['vendor_name']},\n\n"
        f"Congratulations! {q['org_name']} has awarded you the contract for: {q['service_title']}.\n"
        f"Contract value: ₦{(q['total_amount'] or 0):,.2f}\n\n"
        f"Please log in to your vendor dashboard to:\n"
        f"  1. Download the Local Purchase Order (LPO)\n"
        f"  2. Accept the offer to begin service delivery\n\n"
        f"Regards,\n{q['org_name']}",
    )
    flash(f"Contract awarded to {q['vendor_name']}. LPO is now available on the vendor dashboard.", "success")
    if q["service_id"]:
        return redirect(url_for("admin_service_dashboard", service_id=q["service_id"]), code=303)
    return redirect(url_for("admin_dashboard"), code=303)


@app.route("/admin/service/<int:service_id>/set_progress", methods=["POST"])
@login_required("admin")
def admin_set_progress(service_id):
    conn = get_db()
    service = conn.execute("SELECT organization_id FROM services WHERE id=?", (service_id,)).fetchone()
    if not service or not can_manage_org(service["organization_id"]):
        conn.close()
        flash("You do not have permission to manage this service.", "danger")
        return redirect(url_for("admin_dashboard"), code=303)
    conn.close()
    stage = request.form.get("stage", "in_progress")
    try:
        percent = float(request.form.get("progress_percent") or STAGE_META.get(stage, {}).get("percent", 80))
    except ValueError:
        percent = 80
    percent = max(0, min(100, percent))
    if stage not in STAGE_META:
        stage = "in_progress"
    if stage == "completed":
        percent = 100
    conn = get_db()
    conn.execute(
        "UPDATE services SET procurement_stage=?, progress_percent=? WHERE id=?",
        (stage, percent, service_id),
    )
    conn.commit()
    conn.close()
    flash(f"Status updated to: {STAGE_META[stage]['label']} ({percent:.0f}%)", "success")
    return redirect(url_for("admin_service_dashboard", service_id=service_id), code=303)


@app.route("/admin/generate_report/<int:org_id>")
@login_required("admin")
def generate_report(org_id):
    if not require_org_access(org_id):
        return redirect(url_for("admin_dashboard"), code=303)
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


def _build_lpo_pdf(q):
    """Shared LPO PDF builder. q must include vendor_name, org_name, logo_filename, etc."""
    today = datetime.now().strftime("%d %B %Y")
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    logo = q["logo_filename"] if "logo_filename" in q.keys() else None
    if logo and os.path.exists(os.path.join("static", logo)):
        try:
            story.append(Image(os.path.join("static", logo), width=1.8 * inch, height=0.9 * inch))
            story.append(Spacer(1, 10))
        except Exception:
            pass

    org_name = q["org_name"] if "org_name" in q.keys() else "Organization"
    vendor_name = q["vendor_name"] if "vendor_name" in q.keys() else "Vendor"
    story.append(Paragraph(f"<b>{org_name}</b>", styles["Title"]))
    story.append(Paragraph("<b>LOCAL PURCHASE ORDER (LPO)</b>", styles["Heading1"]))
    story.append(Paragraph(f"Date of Issue: {today}", styles["Normal"]))
    story.append(Spacer(1, 20))
    story.append(Paragraph("<b>1. SUPPLIER INFORMATION</b>", styles["Heading2"]))
    story.append(Paragraph(f"<b>Vendor Name:</b> {vendor_name}", styles["Normal"]))
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
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"LPO_{str(vendor_name)[:15]}_{datetime.now().strftime('%Y%m%d')}.pdf",
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
    if not q:
        conn.close()
        flash("Quotation not found.", "danger")
        return redirect(url_for("admin_dashboard"), code=303)
    if not can_manage_org(q["organization_id"]):
        conn.close()
        flash("You do not have permission to manage this organization.", "danger")
        return redirect(url_for("admin_dashboard"), code=303)
    # Ensure LPO flag is set when admin downloads
    conn.execute(
        "UPDATE quotations SET lpo_available=1 WHERE id=?", (quotation_id,)
    )
    conn.commit()
    conn.close()

    send_email_simulation(
        q["vendor_email"],
        f"Local Purchase Order – {q['org_name']}",
        f"Dear {q['vendor_name']},\n\nPlease download your LPO for service: {q['service_title']}.\n"
        f"Contract Value: ₦{q['total_amount'] or 0:,.2f}\n\nRegards,\n{q['org_name']}",
    )
    return _build_lpo_pdf(q)


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
