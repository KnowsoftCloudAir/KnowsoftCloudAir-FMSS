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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import inch

app = Flask(__name__)
# Secrets from environment in production; fallbacks keep local/demo working
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "Knowsoft-eProcurement-Secret-Key-Change-In-Production-2024",
)
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs("static", exist_ok=True)
os.makedirs("instance", exist_ok=True)

# Default admin password: set DEFAULT_ADMIN_PASSWORD in the environment for production.
# Fallback value is only used for first-run bootstrap / local demo.
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Aidah@esemi")

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
        """CREATE TABLE IF NOT EXISTS org_registration_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            logo_filename TEXT,
            address TEXT,
            contact_email TEXT NOT NULL,
            contact_phone TEXT,
            description TEXT,
            status TEXT DEFAULT 'pending',
            admin_notes TEXT,
            reviewed_by INTEGER,
            reviewed_at TEXT,
            created_org_id INTEGER,
            created_admin_id INTEGER,
            created_at TEXT)""",
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
    # Org self-registration requests (public form → general admin review)
    try:
        _exec_sql(
            """CREATE TABLE IF NOT EXISTS org_registration_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                logo_filename TEXT,
                address TEXT,
                contact_email TEXT NOT NULL,
                contact_phone TEXT,
                description TEXT,
                status TEXT DEFAULT 'pending',
                admin_notes TEXT,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                created_org_id INTEGER,
                created_admin_id INTEGER,
                created_at TEXT)"""
        )
    except Exception:
        pass
    # FACE workflow: packages + authorizing officers
    try:
        _exec_sql(
            """CREATE TABLE IF NOT EXISTS face_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT,
                title TEXT,
                signature_file TEXT,
                stamp_file TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT)"""
        )
        _exec_sql(
            """CREATE TABLE IF NOT EXISTS face_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                title TEXT,
                status TEXT DEFAULT 'draft',
                data_json TEXT,
                preparer_name TEXT,
                prepared_at TEXT,
                ready_at TEXT,
                authorized_by INTEGER,
                authorized_at TEXT,
                authorizer_note TEXT,
                created_at TEXT)"""
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


def parse_deadline(deadline_str):
    """Parse service deadline text to datetime, or None if unparseable."""
    if not deadline_str:
        return None
    s = str(deadline_str).strip()
    candidates = [s]
    if len(s) >= 19:
        candidates.append(s[:19])
    if len(s) >= 16:
        candidates.append(s[:16])
    if len(s) >= 10:
        candidates.append(s[:10])
    for candidate in candidates:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%d-%m-%Y",
        ):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace("+00:00", ""))
    except Exception:
        return None


def is_deadline_passed(deadline_str):
    dl = parse_deadline(deadline_str)
    if not dl:
        return False
    # If only a date was given, treat end of that day as cutoff
    if dl.hour == 0 and dl.minute == 0 and dl.second == 0 and "T" not in str(deadline_str) and " " not in str(deadline_str):
        dl = dl.replace(hour=23, minute=59, second=59)
    return datetime.now() > dl


def service_has_award(conn, service_id):
    row = conn.execute(
        "SELECT id FROM quotations WHERE service_id=? AND award_status='awarded' LIMIT 1",
        (service_id,),
    ).fetchone()
    return bool(row)


def is_service_open_for_quotes(service, conn=None):
    """True if vendors may still select / quote on this service."""
    if not service:
        return False
    if not service["is_active"]:
        return False
    stage = service["procurement_stage"] if "procurement_stage" in service.keys() else "open"
    if stage in ("vendor_selected", "in_progress", "completed"):
        return False
    if is_deadline_passed(service["deadline"] if "deadline" in service.keys() else None):
        return False
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True
    awarded = service_has_award(conn, service["id"])
    if close_conn:
        conn.close()
    return not awarded


def close_expired_and_awarded_services(conn=None):
    """Mark services inactive when deadline passed or already awarded (idempotent)."""
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True
    rows = conn.execute(
        "SELECT id, deadline, is_active, procurement_stage FROM services WHERE is_active=1"
    ).fetchall()
    for s in rows:
        should_close = False
        if is_deadline_passed(s["deadline"]):
            should_close = True
        elif service_has_award(conn, s["id"]):
            should_close = True
        elif (s["procurement_stage"] or "") in ("vendor_selected", "in_progress", "completed"):
            should_close = True
        if should_close:
            conn.execute(
                "UPDATE services SET is_active=0 WHERE id=? AND is_active=1",
                (s["id"],),
            )
    conn.commit()
    if close_conn:
        conn.close()


@app.context_processor
def inject_globals():
    """Make app branding and selected organization available in every template."""
    selected_org = None
    oid = session.get("selected_org_id")
    if oid:
        try:
            conn = get_db()
            selected_org = conn.execute(
                "SELECT id, name, logo_filename FROM organizations WHERE id=? AND is_active=1",
                (oid,),
            ).fetchone()
            conn.close()
        except Exception:
            selected_org = None
    return {
        "app_logo": get_setting("app_logo_filename", ""),
        "app_title": get_setting("app_title", "Knowsoft eProcurement"),
        "now": datetime.now,
        "selected_org": selected_org,
        "selected_org_id": oid,
    }


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


@app.route("/register_org", methods=["GET", "POST"])
def register_org():
    """Public form for a new recipient organization to request registration."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        contact_email = request.form.get("contact_email", "").strip().lower()
        if not name or not contact_email:
            flash("Organization name and contact email are required.", "danger")
            return redirect(url_for("register_org"), code=303)

        logo_fn = None
        logo = request.files.get("logo")
        if logo and logo.filename and allowed_file(logo.filename):
            fname = secure_filename(logo.filename)
            logo_fn = f"req_logo_{datetime.now().strftime('%Y%m%d%H%M%S')}_{fname}"
            logo.save(os.path.join("static", logo_fn))

        conn = get_db()
        # Soft check: warn if email already used by an org or admin, but still accept request
        existing_org = conn.execute(
            "SELECT id FROM organizations WHERE lower(contact_email)=?", (contact_email,)
        ).fetchone()
        existing_admin = conn.execute(
            "SELECT id FROM admin_users WHERE lower(username)=?", (contact_email,)
        ).fetchone()
        if existing_org or existing_admin:
            flash(
                "Note: this email is already associated with an existing account. "
                "The general administrator will review your request.",
                "warning",
            )

        conn.execute(
            """INSERT INTO org_registration_requests
               (name, logo_filename, address, contact_email, contact_phone, description, status, created_at)
               VALUES (?,?,?,?,?,?, 'pending', ?)""",
            (
                name,
                logo_fn,
                request.form.get("address", "").strip(),
                contact_email,
                request.form.get("contact_phone", "").strip(),
                request.form.get("description", "").strip(),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        # Simulated confirmation email to the requester
        send_email_simulation(
            contact_email,
            "Knowsoft eProcurement – Registration Request Received",
            f"Dear {name},\n\n"
            "We have received your request to join as a recipient organization on Knowsoft eProcurement.\n"
            "Please check your email within 24 hours for a confirmation email and other details.\n\n"
            "Thank you,\nKnowsoft eProcurement Team",
        )

        flash(
            "Your registration request has been submitted. "
            "Please check your email within 24 hours for a confirmation email and other details.",
            "success",
        )
        return redirect(url_for("index"), code=303)

    return render_template("org_register.html")


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
    close_expired_and_awarded_services(conn)
    vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (session["vendor_id"],)).fetchone()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (vendor["organization_id"],)).fetchone()
    all_services = conn.execute(
        "SELECT * FROM services WHERE organization_id=? AND is_active=1 ORDER BY title",
        (vendor["organization_id"],),
    ).fetchall()
    # Only services still open for quotes (not awarded, deadline not passed)
    services = [s for s in all_services if is_service_open_for_quotes(s, conn)]

    if request.method == "POST":
        service_id = request.form.get("service_id")
        tax_rate = float(request.form.get("tax_rate") or 0)
        quality_spec = request.form.get("quality_spec", "").strip()
        other_details = request.form.get("other_details", "").strip()
        consent = 1 if request.form.get("consent") else 0
        acceptance = request.form.get("acceptance_status", "accepted")

        # Multi-line quotation: Description / Qty / Unit cost / Amount
        descs = request.form.getlist("line_desc[]") or request.form.getlist("line_desc")
        qtys = request.form.getlist("line_qty[]") or request.form.getlist("line_qty")
        units = request.form.getlist("line_unit[]") or request.form.getlist("line_unit")
        line_items = []
        subtotal = 0.0
        for i in range(max(len(descs), len(qtys), len(units))):
            desc = (descs[i] if i < len(descs) else "").strip()
            try:
                qty = float(qtys[i]) if i < len(qtys) else 0
            except (TypeError, ValueError):
                qty = 0
            try:
                unit = float(units[i]) if i < len(units) else 0
            except (TypeError, ValueError):
                unit = 0
            if not desc and qty <= 0 and unit <= 0:
                continue
            amt = qty * unit
            line_items.append({"description": desc or "Item", "qty": qty, "unit_cost": unit, "amount": amt})
            subtotal += amt

        # Backward compatible single-line fields if no multi-line data
        if not line_items:
            unit_price = float(request.form.get("unit_price") or 0)
            quantity = float(request.form.get("quantity") or 0)
            if unit_price > 0 and quantity > 0:
                line_items.append({
                    "description": quality_spec or "Quoted item",
                    "qty": quantity,
                    "unit_cost": unit_price,
                    "amount": unit_price * quantity,
                })
                subtotal = unit_price * quantity

        if not service_id or not line_items or subtotal <= 0 or not consent:
            flash("Please add at least one quotation line (description, qty, unit cost) and accept the consent statement.", "danger")
            conn.close()
            return redirect(url_for("vendor_quotation"))

        service = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        if not service or not is_service_open_for_quotes(service, conn):
            flash(
                "This procurement activity is no longer open for quotations "
                "(awarded or deadline reached).",
                "danger",
            )
            conn.close()
            return redirect(url_for("vendor_quotation"), code=303)

        tax_amount = subtotal * (tax_rate / 100.0)
        total = subtotal + tax_amount
        # Summary fields for existing ranking/report logic
        quantity = sum(li["qty"] for li in line_items) or 1
        unit_price = subtotal / quantity if quantity else subtotal
        service_title = service["title"] if service else ""
        import json as _json
        lines_json = _json.dumps(line_items)
        if other_details:
            other_details = other_details + "\n\n[Line items]\n" + lines_json
        else:
            other_details = "[Line items]\n" + lines_json

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
    org_id = session.get("client_org_id") or session.get("selected_org_id")
    for k in ("admin", "admin_id", "admin_username", "admin_role", "admin_must_change", "client_org_id"):
        session.pop(k, None)
    if org_id:
        session["selected_org_id"] = org_id
        return redirect(url_for("org_portal"))
    return redirect(url_for("index"))


@app.route("/admin/branding", methods=["GET", "POST"])
@login_required("super_admin")
def admin_branding():
    """General admin can upload app title-bar logo and set display title."""
    if request.method == "POST":
        title = request.form.get("app_title", "").strip()
        if title:
            set_setting("app_title", title[:80])
        logo = request.files.get("app_logo")
        if logo and logo.filename and allowed_file(logo.filename):
            fname = secure_filename(logo.filename)
            logo_fn = f"app_logo_{datetime.now().strftime('%Y%m%d%H%M%S')}_{fname}"
            logo.save(os.path.join("static", logo_fn))
            # remove previous logo file if present
            old = get_setting("app_logo_filename", "")
            if old and old != logo_fn:
                try:
                    op = os.path.join("static", old)
                    if os.path.isfile(op):
                        os.remove(op)
                except Exception:
                    pass
            set_setting("app_logo_filename", logo_fn)
            flash("App logo updated.", "success")
        elif request.form.get("remove_logo") == "1":
            old = get_setting("app_logo_filename", "")
            if old:
                try:
                    op = os.path.join("static", old)
                    if os.path.isfile(op):
                        os.remove(op)
                except Exception:
                    pass
            set_setting("app_logo_filename", "")
            flash("App logo removed.", "info")
        else:
            flash("Branding settings saved.", "success")
        return redirect(url_for("admin_branding"), code=303)
    return render_template(
        "admin_branding.html",
        app_logo=get_setting("app_logo_filename", ""),
        app_title=get_setting("app_title", "Knowsoft eProcurement"),
    )


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
    pending_org_requests = 0
    if is_super_admin():
        try:
            pending_org_requests = conn.execute(
                "SELECT COUNT(*) FROM org_registration_requests WHERE status='pending'"
            ).fetchone()[0]
        except Exception:
            pending_org_requests = 0
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
        pending_org_requests=pending_org_requests,
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


@app.route("/admin/org_requests", methods=["GET", "POST"])
@login_required("super_admin")
def admin_org_requests():
    """General admin reviews public requests to register as a recipient organization.
    On approve: create organization + sub-admin account (username = contact email)
    and assign the org to that sub-admin so they can manage their procurement system.
    """
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        req_id = request.form.get("request_id")
        req = conn.execute(
            "SELECT * FROM org_registration_requests WHERE id=?", (req_id,)
        ).fetchone()
        if not req or req["status"] != "pending":
            flash("Request not found or already processed.", "warning")
            conn.close()
            return redirect(url_for("admin_org_requests"), code=303)

        admin_notes = request.form.get("admin_notes", "").strip()
        reviewer_id = session.get("admin_id")

        if action == "reject":
            conn.execute(
                """UPDATE org_registration_requests
                   SET status='rejected', admin_notes=?, reviewed_by=?, reviewed_at=?
                   WHERE id=?""",
                (admin_notes, reviewer_id, datetime.now().isoformat(), req_id),
            )
            conn.commit()
            send_email_simulation(
                req["contact_email"],
                "Knowsoft eProcurement – Registration Request Update",
                f"Dear {req['name']},\n\n"
                "We regret to inform you that your request to join as a recipient organization "
                "has not been approved at this time.\n"
                f"{('Notes: ' + admin_notes) if admin_notes else ''}\n\n"
                "You may contact the administrator for further clarification.\n\n"
                "Knowsoft eProcurement Team",
            )
            flash("Request rejected. Notification email simulated.", "info")

        elif action == "approve":
            password = request.form.get("password", "").strip()
            if not password or len(password) < 6:
                flash("Please set a password of at least 6 characters for the new client account.", "danger")
                conn.close()
                return redirect(url_for("admin_org_requests"), code=303)

            email = (req["contact_email"] or "").strip().lower()
            # Ensure username (email) is free
            existing = conn.execute(
                "SELECT id FROM admin_users WHERE lower(username)=?", (email,)
            ).fetchone()
            if existing:
                flash(
                    f"Cannot approve: an admin account with username '{email}' already exists. "
                    "Reject or ask the requester to use a different email.",
                    "danger",
                )
                conn.close()
                return redirect(url_for("admin_org_requests"), code=303)

            # 1. Create organization
            cur = conn.execute(
                """INSERT INTO organizations
                   (name, logo_filename, address, contact_email, contact_phone, description, is_active, created_at)
                   VALUES (?,?,?,?,?,?,1,?)""",
                (
                    req["name"],
                    req["logo_filename"],
                    req["address"],
                    email,
                    req["contact_phone"],
                    req["description"],
                    datetime.now().isoformat(),
                ),
            )
            org_id = cur.lastrowid

            # 2. Create sub-admin account (login = contact email)
            cur = conn.execute(
                """INSERT INTO admin_users
                   (username, password_hash, role, full_name, is_active, must_change_password, created_at)
                   VALUES (?, ?, 'sub', ?, 1, 1, ?)""",
                (
                    email,
                    generate_password_hash(password),
                    req["name"],
                    datetime.now().isoformat(),
                ),
            )
            new_admin_id = cur.lastrowid

            # 3. Assign org to this sub-admin
            conn.execute(
                """INSERT INTO admin_org_assignments
                   (admin_id, organization_id, is_active, assigned_by, assigned_at)
                   VALUES (?, ?, 1, ?, ?)""",
                (new_admin_id, org_id, reviewer_id, datetime.now().isoformat()),
            )

            # 4. Mark request approved
            conn.execute(
                """UPDATE org_registration_requests
                   SET status='approved', admin_notes=?, reviewed_by=?, reviewed_at=?,
                       created_org_id=?, created_admin_id=?
                   WHERE id=?""",
                (
                    admin_notes,
                    reviewer_id,
                    datetime.now().isoformat(),
                    org_id,
                    new_admin_id,
                    req_id,
                ),
            )
            conn.commit()

            # 5. Simulate sending credentials
            send_email_simulation(
                email,
                "Knowsoft eProcurement – Your Organization Account is Ready",
                f"Dear {req['name']},\n\n"
                "Your request to join as a recipient organization has been approved.\n\n"
                "Login details for the organization admin portal:\n"
                f"  Username (email): {email}\n"
                f"  Temporary password: {password}\n\n"
                "Please log in at the Admin section of the portal and change your password on first login.\n"
                "You will be able to manage your organization's procurement system (services, vendors, "
                "committee, quotations) as a client administrator.\n\n"
                f"{('Notes from administrator: ' + admin_notes) if admin_notes else ''}\n"
                "Welcome aboard!\n\n"
                "Knowsoft eProcurement Team",
            )
            flash(
                f"Approved. Organization '{req['name']}' created and sub-admin account "
                f"({email}) provisioned. Credentials email simulated.",
                "success",
            )

        conn.close()
        return redirect(url_for("admin_org_requests"), code=303)

    pending = conn.execute(
        "SELECT * FROM org_registration_requests WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()
    processed = conn.execute(
        "SELECT * FROM org_registration_requests WHERE status!='pending' ORDER BY reviewed_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return render_template(
        "admin_org_requests.html",
        pending=pending,
        processed=processed,
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

        # Notify all prelisted (approved) vendors for this organization
        vendors = conn.execute(
            """SELECT id, name, email FROM vendors
               WHERE organization_id=? AND (is_approved=1 OR is_approved IS NULL)""",
            (org_id,),
        ).fetchall()
        notified = 0
        failed = 0
        for v in vendors:
            email = (v["email"] or "").strip()
            if not email:
                failed += 1
                continue
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
            try:
                send_email_simulation(
                    email,
                    f"New Service Opportunity – {title}",
                    body,
                )
                notified += 1
            except Exception:
                failed += 1
        conn.close()
        msg = f"Service added. Notification sent to {notified} prelisted vendor(s)."
        if failed:
            msg += f" ({failed} could not be notified — missing email.)"
        if notified == 0 and failed == 0:
            msg = "Service added. No prelisted vendors found to notify yet."
        flash(msg, "success")
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

    # Clear previous awards on same service and close it so it cannot be selected again
    if q["service_id"]:
        conn.execute(
            "UPDATE quotations SET award_status='none', lpo_available=0 WHERE service_id=? AND id!=?",
            (q["service_id"], quotation_id),
        )
        conn.execute(
            """UPDATE services SET procurement_stage=?, progress_percent=?, awarded_quotation_id=?,
               is_active=0 WHERE id=?""",
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


def _org_procurement_records(org_id):
    """All procurement activity for an organization (quotations, awards, LPO, receipts)."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT
            q.id AS quotation_id,
            q.service_id,
            q.service_title,
            q.unit_price,
            q.quantity,
            q.tax_rate,
            q.tax_amount,
            q.subtotal,
            q.total_amount,
            q.system_score,
            q.award_status,
            q.awarded_at,
            q.submitted_at,
            q.lpo_available,
            q.invoice_filename,
            q.delivery_note_filename,
            q.receipt_filename,
            q.vendor_offer_response,
            q.status,
            q.other_details,
            v.id AS vendor_id,
            v.name AS vendor_name,
            v.email AS vendor_email,
            v.phone AS vendor_phone,
            v.cac_no,
            v.tin,
            s.title AS service_name,
            s.deadline,
            s.procurement_stage,
            s.progress_percent,
            pr.lpo_sent,
            pr.lpo_sent_at,
            pr.delivery_status,
            pr.certificate_issued,
            pr.closed,
            pr.notes AS pr_notes
        FROM quotations q
        JOIN vendors v ON q.vendor_id = v.id
        LEFT JOIN services s ON q.service_id = s.id
        LEFT JOIN procurement_records pr ON pr.quotation_id = q.id
        WHERE q.organization_id = ?
        ORDER BY COALESCE(q.awarded_at, q.submitted_at) DESC
        """,
        (org_id,),
    ).fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result


@app.route("/admin/org/<int:org_id>/full_report")
@login_required("admin")
def org_full_procurement_report(org_id):
    """Downloadable full procurement report for one recipient organization."""
    if not require_org_access(org_id):
        return redirect(url_for("admin_dashboard"), code=303)
    conn = get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    conn.close()
    if not org:
        flash("Organization not found.", "danger")
        return redirect(url_for("admin_organizations"), code=303)

    records = _org_procurement_records(org_id)
    awarded = [r for r in records if (r.get("award_status") or "") == "awarded"]
    today = datetime.now().strftime("%d %B %Y")
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=0.6 * inch, rightMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    story = []

    if org["logo_filename"] and os.path.exists(os.path.join("static", org["logo_filename"])):
        try:
            story.append(Image(os.path.join("static", org["logo_filename"]), width=1.6 * inch, height=0.8 * inch))
            story.append(Spacer(1, 8))
        except Exception:
            pass

    story.append(Paragraph(f"<b>{org['name']}</b>", styles["Title"]))
    story.append(Paragraph("<b>FULL PROCUREMENT REPORT</b>", styles["Heading1"]))
    story.append(Paragraph(
        f"Address: {org['address'] or '—'} &nbsp;|&nbsp; Email: {org['contact_email'] or '—'} &nbsp;|&nbsp; "
        f"Phone: {org['contact_phone'] or '—'}<br/>Generated: {today}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 14))

    total_spend = sum(float(r.get("total_amount") or 0) for r in awarded)
    story.append(Paragraph("1. SUMMARY", styles["Heading2"]))
    story.append(Paragraph(
        f"Total quotations received: <b>{len(records)}</b><br/>"
        f"Contracts awarded: <b>{len(awarded)}</b><br/>"
        f"Total awarded value: <b>₦{total_spend:,.2f}</b><br/>"
        f"Purchase orders (LPO) issued: <b>{sum(1 for r in awarded if r.get('lpo_available') or r.get('lpo_sent'))}</b>",
        styles["Normal"],
    ))
    story.append(Spacer(1, 12))

    story.append(Paragraph("2. ALL PROCUREMENT ACTIVITY", styles["Heading2"]))
    if not records:
        story.append(Paragraph("No procurement activity recorded yet.", styles["Normal"]))
    else:
        data = [["#", "Service", "Vendor", "Amount (₦)", "Status", "Date"]]
        for i, r in enumerate(records, 1):
            data.append([
                str(i),
                (r.get("service_name") or r.get("service_title") or "—")[:28],
                (r.get("vendor_name") or "—")[:22],
                f"{float(r.get('total_amount') or 0):,.0f}",
                (r.get("award_status") or r.get("status") or "submitted")[:12],
                (r.get("awarded_at") or r.get("submitted_at") or "")[:10],
            ])
        t = Table(data, colWidths=[28, 120, 100, 70, 70, 70])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a365d")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(t)
    story.append(Spacer(1, 16))

    story.append(Paragraph("3. PURCHASE ORDERS (LPO) ISSUED", styles["Heading2"]))
    lpos = [r for r in awarded if r.get("lpo_available") or r.get("lpo_sent")]
    if not lpos:
        story.append(Paragraph("No purchase orders issued yet.", styles["Normal"]))
    else:
        data = [["LPO #", "Vendor", "Service", "Amount (₦)", "Issued"]]
        for r in lpos:
            data.append([
                f"LPO-{r['quotation_id']}",
                (r.get("vendor_name") or "—")[:24],
                (r.get("service_name") or r.get("service_title") or "—")[:28],
                f"{float(r.get('total_amount') or 0):,.2f}",
                (r.get("lpo_sent_at") or r.get("awarded_at") or "")[:10],
            ])
        t = Table(data, colWidths=[55, 110, 130, 80, 70])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b6cb0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ]))
        story.append(t)
    story.append(Spacer(1, 16))

    story.append(Paragraph("4. PAYMENT RECEIPTS / VOUCHERS", styles["Heading2"]))
    story.append(Paragraph(
        f"Company: <b>{org['name']}</b> — documents uploaded against awarded quotations "
        "(invoice, delivery note, receipt).",
        styles["Normal"],
    ))
    story.append(Spacer(1, 6))
    receipts = [r for r in records if r.get("invoice_filename") or r.get("receipt_filename") or r.get("delivery_note_filename")]
    if not receipts:
        story.append(Paragraph("No payment receipts or supporting documents on file yet.", styles["Normal"]))
    else:
        data = [["Ref", "Vendor", "Invoice", "Delivery Note", "Receipt", "Amount (₦)"]]
        for r in receipts:
            data.append([
                f"Q-{r['quotation_id']}",
                (r.get("vendor_name") or "—")[:20],
                "Yes" if r.get("invoice_filename") else "—",
                "Yes" if r.get("delivery_note_filename") else "—",
                "Yes" if r.get("receipt_filename") else "—",
                f"{float(r.get('total_amount') or 0):,.2f}",
            ])
        t = Table(data, colWidths=[40, 100, 60, 70, 55, 75])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0d9488")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ]))
        story.append(t)
    story.append(Spacer(1, 16))

    story.append(Paragraph("5. AWARDED CONTRACT DETAIL", styles["Heading2"]))
    if not awarded:
        story.append(Paragraph("No contracts awarded yet.", styles["Normal"]))
    else:
        for r in awarded:
            story.append(Paragraph(
                f"<b>{r.get('service_name') or r.get('service_title') or 'Service'}</b> — "
                f"Vendor: {r.get('vendor_name')} ({r.get('vendor_email') or '—'})<br/>"
                f"Amount: ₦{float(r.get('total_amount') or 0):,.2f} &nbsp;|&nbsp; "
                f"Tax: ₦{float(r.get('tax_amount') or 0):,.2f} &nbsp;|&nbsp; "
                f"Awarded: {(r.get('awarded_at') or '')[:10]} &nbsp;|&nbsp; "
                f"Stage: {r.get('procurement_stage') or '—'} &nbsp;|&nbsp; "
                f"Vendor response: {r.get('vendor_offer_response') or 'pending'}",
                styles["Normal"],
            ))
            story.append(Spacer(1, 6))

    story.append(Spacer(1, 24))
    story.append(Paragraph(
        f"This report is issued for <b>{org['name']}</b> by Knowsoft eProcurement. "
        f"Document generated on {today}.",
        styles["Normal"],
    ))
    doc.build(story)
    buffer.seek(0)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (org["name"] or "org"))[:30]
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"Full_Procurement_Report_{safe}_{datetime.now().strftime('%Y%m%d')}.pdf",
        mimetype="application/pdf",
    )


@app.route("/admin/org/<int:org_id>/financial_excel")
@login_required("admin")
def org_financial_excel(org_id):
    """Excel financial report for one recipient organization."""
    if not require_org_access(org_id):
        return redirect(url_for("admin_dashboard"), code=303)
    conn = get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    conn.close()
    if not org:
        flash("Organization not found.", "danger")
        return redirect(url_for("admin_organizations"), code=303)

    records = _org_procurement_records(org_id)
    rows = []
    for r in records:
        rows.append({
            "Company": org["name"],
            "Quotation ID": r["quotation_id"],
            "Service": r.get("service_name") or r.get("service_title") or "",
            "Vendor": r.get("vendor_name") or "",
            "Vendor Email": r.get("vendor_email") or "",
            "Vendor Phone": r.get("vendor_phone") or "",
            "Qty": r.get("quantity") or 0,
            "Unit Price": r.get("unit_price") or 0,
            "Subtotal": r.get("subtotal") or 0,
            "Tax Rate %": r.get("tax_rate") or 0,
            "Tax Amount": r.get("tax_amount") or 0,
            "Total Amount": r.get("total_amount") or 0,
            "Award Status": r.get("award_status") or "",
            "Submitted": (r.get("submitted_at") or "")[:19],
            "Awarded At": (r.get("awarded_at") or "")[:19],
            "LPO Issued": "Yes" if (r.get("lpo_available") or r.get("lpo_sent")) else "No",
            "LPO Date": (r.get("lpo_sent_at") or "")[:19],
            "Invoice on file": "Yes" if r.get("invoice_filename") else "No",
            "Delivery note": "Yes" if r.get("delivery_note_filename") else "No",
            "Receipt / voucher": "Yes" if r.get("receipt_filename") else "No",
            "Stage": r.get("procurement_stage") or "",
            "Progress %": r.get("progress_percent") or 0,
            "Vendor offer response": r.get("vendor_offer_response") or "",
        })

    df = pd.DataFrame(rows)
    # Summary sheet data
    awarded = [r for r in records if (r.get("award_status") or "") == "awarded"]
    summary = pd.DataFrame([
        {"Metric": "Company", "Value": org["name"]},
        {"Metric": "Total quotations", "Value": len(records)},
        {"Metric": "Contracts awarded", "Value": len(awarded)},
        {"Metric": "Total awarded value (₦)", "Value": sum(float(r.get("total_amount") or 0) for r in awarded)},
        {"Metric": "LPOs issued", "Value": sum(1 for r in awarded if r.get("lpo_available") or r.get("lpo_sent"))},
        {"Metric": "Receipts on file", "Value": sum(1 for r in records if r.get("receipt_filename"))},
        {"Metric": "Report date", "Value": datetime.now().strftime("%Y-%m-%d %H:%M")},
    ])

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary.to_excel(writer, index=False, sheet_name="Summary")
        if df.empty:
            pd.DataFrame([{"Note": "No procurement records yet"}]).to_excel(
                writer, index=False, sheet_name="Transactions"
            )
        else:
            df.to_excel(writer, index=False, sheet_name="Transactions")
        # Awarded-only sheet
        if awarded:
            df[df["Award Status"] == "awarded"].to_excel(writer, index=False, sheet_name="Awarded")
    buffer.seek(0)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (org["name"] or "org"))[:30]
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"Financial_Report_{safe}_{datetime.now().strftime('%Y%m%d')}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
    """Global entry; prefers selected organization when present."""
    org_id = session.get("selected_org_id")
    if org_id:
        return redirect(url_for("org_committee_login", org_id=org_id))
    return redirect(url_for("index"))


@app.route("/org/<int:org_id>/committee/login", methods=["GET", "POST"])
def org_committee_login(org_id):
    """Committee login scoped to one recipient organization."""
    conn = get_db()
    org = conn.execute(
        "SELECT * FROM organizations WHERE id=? AND is_active=1", (org_id,)
    ).fetchone()
    if not org:
        conn.close()
        flash("Organization not found or inactive.", "danger")
        return redirect(url_for("index"))
    session["selected_org_id"] = org_id
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        member = conn.execute(
            """SELECT * FROM committee
               WHERE name=? AND is_active=1 AND organization_id=?""",
            (name, org_id),
        ).fetchone()
        if member and check_password_hash(member["password_hash"], password):
            session["committee_id"] = member["id"]
            session["committee_name"] = member["name"]
            session["committee_org_id"] = org_id
            conn.close()
            flash(f"Welcome, {member['name']} — {org['name']} committee.", "success")
            return redirect(url_for("committee_score"), code=303)
        flash("Invalid name or password for this organization's committee.", "danger")
    conn.close()
    return render_template("committee_login.html", org=org, org_scoped=True)


@app.route("/org/<int:org_id>/admin/login", methods=["GET", "POST"])
def org_client_admin_login(org_id):
    """Client (sub-admin) login for a single assigned organization — independent of general admin."""
    conn = get_db()
    org = conn.execute(
        "SELECT * FROM organizations WHERE id=? AND is_active=1", (org_id,)
    ).fetchone()
    if not org:
        conn.close()
        flash("Organization not found or inactive.", "danger")
        return redirect(url_for("index"))
    session["selected_org_id"] = org_id
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        admin = conn.execute(
            "SELECT * FROM admin_users WHERE lower(username)=? AND (is_active=1 OR is_active IS NULL)",
            (username,),
        ).fetchone()
        ok = False
        if admin and check_password_hash(admin["password_hash"], password):
            role = admin["role"] if "role" in admin.keys() and admin["role"] else "super"
            if role == "super":
                ok = True
            else:
                # Sub-admin must be assigned to this organization
                assigned = conn.execute(
                    """SELECT id FROM admin_org_assignments
                       WHERE admin_id=? AND organization_id=? AND is_active=1""",
                    (admin["id"], org_id),
                ).fetchone()
                ok = bool(assigned)
        conn.close()
        if ok and admin:
            role = admin["role"] if "role" in admin.keys() and admin["role"] else "super"
            session["admin"] = True
            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            session["admin_role"] = role
            session["admin_must_change"] = bool(admin["must_change_password"])
            session["client_org_id"] = org_id  # scope hint
            if admin["must_change_password"]:
                flash("Please change your password.", "warning")
                return redirect(url_for("admin_change_password"), code=303)
            flash(f"Logged in to manage {org['name']}.", "success")
            return redirect(url_for("admin_org_detail", org_id=org_id), code=303)
        flash("Invalid credentials or you are not assigned to this organization.", "danger")
        return render_template("org_client_admin_login.html", org=org)
    conn.close()
    return render_template("org_client_admin_login.html", org=org)


@app.route("/committee/logout")
def committee_logout():
    org_id = session.get("committee_org_id") or session.get("selected_org_id")
    for k in ("committee_id", "committee_name", "committee_org_id"):
        session.pop(k, None)
    if org_id:
        session["selected_org_id"] = org_id
        return redirect(url_for("org_portal"))
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
# FACE FORM (UNICEF-style Funding Authorization package)
# Role-based: Preparer → marks Ready → Authorizing Officer signs
# Cover letter (A) + FACE form (B) + Annex 4 itemized (C)
# ---------------------------------------------------------
import json as _json_face

DEFAULT_FACE_ACTIVITIES = [
    {"code": ".A.", "name": "Leadership, Management and Coordination"},
    {"code": ".B.", "name": "Demand Generation"},
    {"code": ".C.", "name": "Human Resource for Health"},
    {"code": ".D.", "name": "Service Delivery"},
    {"code": ".E.", "name": "Supply Chain"},
    {"code": ".F.", "name": "Health Financing"},
    {"code": ".G.", "name": "Health Information Management System"},
]


def _empty_face_data(mode):
    data = {
        "mode": mode,
        "org_name": "",
        "org_address": "",
        "org_email": "",
        "ref_no": "",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "addressee_title": "Chief of Field Office,",
        "addressee_office": "UNICEF Field Office,",
        "addressee_city": "",
        "subject": "",
        "signatory_name": "",
        "signatory_title": "Executive Secretary",
        "un_agency": "UNICEF",
        "country": "NIGERIA",
        "programme_code_title": "",
        "project_code_title": "",
        "responsible_officer": "",
        "implementing_partner": "",
        "currency": "Naira (N)",
        "request_type": "dct",
        "reporting_period": "",
        "new_request_period": "",
        "project_name": "",
        "prog_officer": "",
        "preparer_name": "",
        "org_logo_file": "",
        "activities": [
            {**a, "authorized": 0, "actual": 0, "lines": []}
            for a in DEFAULT_FACE_ACTIVITIES
        ],
        "cover_body_intro": "",
    }
    if mode == "liquidation":
        data["subject"] = "SUBMISSION OF RETIREMENT DOCUMENT, ANNEX 4 AND FACE FORM"
        data["cover_body_intro"] = (
            "I write to notify you of the submission of retirement documents for the following "
            "activities. Attached are relevant documents for your necessary action."
        )
    else:
        data["subject"] = "SUBMISSION OF FUNDING REQUEST, ANNEX 4 (PROPOSAL) AND FACE FORM"
        data["cover_body_intro"] = (
            "I write to submit a funding request for the following activities. "
            "Attached are the FACE form and Annex 4 (itemized cost estimates) for your necessary action."
        )
    return data


def _load_package(pkg_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM face_packages WHERE id=?", (pkg_id,)).fetchone()
    conn.close()
    if not row:
        return None
    pkg = dict(row)
    try:
        pkg["data"] = _json_face.loads(pkg["data_json"] or "{}")
    except Exception:
        pkg["data"] = _empty_face_data(pkg.get("mode") or "requisition")
    return pkg


def _save_package_data(pkg_id, data, **meta):
    conn = get_db()
    fields = ["data_json=?"]
    vals = [_json_face.dumps(data)]
    for k, v in meta.items():
        fields.append(f"{k}=?")
        vals.append(v)
    vals.append(pkg_id)
    conn.execute(f"UPDATE face_packages SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    conn.close()


def _parse_face_activities_from_form(form, mode):
    activities = []
    codes = form.getlist("act_code")
    names = form.getlist("act_name")
    auths = form.getlist("act_authorized")
    actuals = form.getlist("act_actual")
    for i in range(len(names)):
        code = (codes[i] if i < len(codes) else f".{i+1}.").strip() or f".{i+1}."
        name = (names[i] if i < len(names) else "").strip()
        try:
            auth = float(auths[i]) if i < len(auths) and auths[i] not in (None, "") else 0
        except ValueError:
            auth = 0
        try:
            act = float(actuals[i]) if mode == "liquidation" and i < len(actuals) and actuals[i] not in (None, "") else 0
        except ValueError:
            act = 0
        lines = []
        prefixes = form.getlist(f"line_desc_{i}")
        qtys = form.getlist(f"line_qty_{i}")
        days = form.getlist(f"line_days_{i}")
        freqs = form.getlist(f"line_freq_{i}")
        rates = form.getlist(f"line_rate_{i}")
        a_qtys = form.getlist(f"line_aqty_{i}")
        a_days = form.getlist(f"line_adays_{i}")
        a_freqs = form.getlist(f"line_afreq_{i}")
        a_rates = form.getlist(f"line_arate_{i}")
        for j in range(len(prefixes)):
            desc = (prefixes[j] or "").strip()
            if not desc:
                continue
            def fnum(lst, idx):
                try:
                    return float(lst[idx]) if idx < len(lst) and lst[idx] not in (None, "") else 0
                except ValueError:
                    return 0
            q, d, fr, r = fnum(qtys, j), fnum(days, j), fnum(freqs, j), fnum(rates, j)
            budget_total = q * d * fr * r
            line = {
                "description": desc,
                "qty": q, "days": d, "freq": fr, "rate": r,
                "budget_total": budget_total,
            }
            if mode == "liquidation":
                aq, ad, af, ar = fnum(a_qtys, j), fnum(a_days, j), fnum(a_freqs, j), fnum(a_rates, j)
                actual_total = aq * ad * af * ar
                line.update({
                    "aqty": aq, "adays": ad, "afreq": af, "arate": ar,
                    "actual_total": actual_total,
                    "variance_total": budget_total - actual_total,
                })
            lines.append(line)
        if lines and mode == "liquidation":
            auth = sum(L["budget_total"] for L in lines) or auth
            act = sum(L.get("actual_total", 0) for L in lines) or act
        elif lines:
            auth = sum(L["budget_total"] for L in lines) or auth
        if name or auth or act or lines:
            activities.append({
                "code": code, "name": name or f"Activity {i+1}",
                "authorized": auth, "actual": act, "lines": lines,
            })
    return activities



def _sync_face_from_annex(draft, mode):
    """Roll Annex 4 line totals into activity authorized/actual used by FACE + cover letter."""
    for a in draft.get("activities") or []:
        lines = a.get("lines") or []
        if lines:
            a["authorized"] = float(sum(float(L.get("budget_total") or 0) for L in lines))
            if mode == "liquidation":
                a["actual"] = float(sum(float(L.get("actual_total") or 0) for L in lines))
        else:
            a["authorized"] = float(a.get("authorized") or 0)
            if mode == "liquidation":
                a["actual"] = float(a.get("actual") or 0)
            else:
                a["actual"] = 0
    # Header defaults from annex fields
    if draft.get("org_name") and not draft.get("implementing_partner"):
        draft["implementing_partner"] = draft["org_name"]
    return draft


def _face_is_authorizer():
    return bool(session.get("face_authorizer_id"))


def _face_can_edit_package(pkg):
    """Preparer may edit draft only; authorizer may review ready packages."""
    if not pkg:
        return False
    st = pkg.get("status") or "draft"
    if st == "authorized":
        return False
    if st == "draft":
        return not _face_is_authorizer()  # preparers edit drafts
    if st == "ready":
        return _face_is_authorizer()  # authorizer reviews/signs
    return False



def _face_sample_data(mode="liquidation"):
    """Demo package data so users can preview the final report layout."""
    data = _empty_face_data(mode)
    data.update({
        "org_name": "Jigawa State Primary Healthcare Development Agency",
        "org_address": "Block B, New State Secretariat Complex, Dutse, Jigawa State",
        "org_email": "phc@jigawastate.gov.ng",
        "ref_no": "PHCDA/PHC/IMM/VOL1/150",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "addressee_title": "Chief of Field Office,",
        "addressee_office": "UNICEF Kano Field Office,",
        "addressee_city": "Kano.",
        "signatory_name": "Dr. Sample Authorizing Officer",
        "signatory_title": "Executive Secretary",
        "un_agency": "UNICEF",
        "country": "NIGERIA",
        "programme_code_title": "03 Programme Effectiveness — Outcome 1: Maternal Newborn and Child Health",
        "project_code_title": "Outcome 1.1 : HSS",
        "responsible_officer": "Sample Programme Officer",
        "implementing_partner": "Jigawa State Primary Healthcare Development Agency",
        "currency": "Naira (N)",
        "request_type": "dct",
        "reporting_period": "April to June 2024",
        "new_request_period": "July to September 2024",
        "project_name": "Jigawa State Proposal for Quarter Activities (SAMPLE)",
        "prog_officer": "Sample Programme Officer",
        "preparer_name": "Sample Preparer",
    })
    # Sample line under activity A; other activities use summary amounts
    sample_lines = [
        {"description": "Hall hire", "qty": 2, "days": 20, "freq": 1, "rate": 4000,
         "budget_total": 160000, "aqty": 2, "adays": 20, "afreq": 1, "arate": 4000,
         "actual_total": 160000, "variance_total": 0},
        {"description": "2 tea breaks", "qty": 2, "days": 20, "freq": 1, "rate": 4000,
         "budget_total": 160000, "aqty": 2, "adays": 20, "afreq": 1, "arate": 4000,
         "actual_total": 160000, "variance_total": 0},
        {"description": "Lunch", "qty": 2, "days": 20, "freq": 1, "rate": 4000,
         "budget_total": 160000, "aqty": 2, "adays": 20, "afreq": 1, "arate": 4000,
         "actual_total": 160000, "variance_total": 0},
        {"description": "Stationeries", "qty": 2, "days": 20, "freq": 1, "rate": 4000,
         "budget_total": 160000, "aqty": 2, "adays": 20, "afreq": 1, "arate": 4000,
         "actual_total": 160000, "variance_total": 0},
        {"description": "Transport", "qty": 2, "days": 20, "freq": 1, "rate": 4000,
         "budget_total": 160000, "aqty": 2, "adays": 20, "afreq": 1, "arate": 4000,
         "actual_total": 160000, "variance_total": 0},
    ]
    amounts = [
        (2440000, 2280000),
        (23200000, 21280000),
        (2640000, 2640000),
        (3280000, 3280000),
        (2640000, 2640000),
        (3120000, 3120000),
        (2640000, 2640000),
    ]
    for i, a in enumerate(data["activities"]):
        auth, act = amounts[i] if i < len(amounts) else (0, 0)
        a["authorized"] = auth
        a["actual"] = act if mode == "liquidation" else 0
        a["lines"] = sample_lines if i == 0 else []
    if mode != "liquidation":
        for a in data["activities"]:
            a["actual"] = 0
            for L in a.get("lines") or []:
                L["actual_total"] = 0
                L["variance_total"] = L.get("budget_total", 0)
    return data


@app.route("/face/sample/<mode>.pdf")
def face_sample_pdf(mode):
    """Public sample final report so users can see the expected PDF layout."""
    if mode not in ("liquidation", "requisition"):
        mode = "liquidation"
    draft = _face_sample_data(mode)
    buffer = _build_face_package_pdf(draft, mode)
    label = "Liquidation" if mode == "liquidation" else "Requisition"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"FACE_SAMPLE_{label}_{datetime.now().strftime('%Y%m%d')}.pdf",
        mimetype="application/pdf",
    )


@app.route("/face")
def face_hub():
    conn = get_db()
    packages = conn.execute(
        "SELECT id, mode, title, status, preparer_name, prepared_at, ready_at, authorized_at, created_at "
        "FROM face_packages ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return render_template(
        "face_hub.html",
        packages=packages,
        is_authorizer=_face_is_authorizer(),
        authorizer_name=session.get("face_authorizer_name"),
    )


@app.route("/face/authorizer/login", methods=["GET", "POST"])
def face_authorizer_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        conn = get_db()
        user = conn.execute(
            "SELECT * FROM face_users WHERE lower(username)=? AND is_active=1", (username,)
        ).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["face_authorizer_id"] = user["id"]
            session["face_authorizer_name"] = user["full_name"] or user["username"]
            session["face_authorizer_title"] = user["title"] or ""
            flash(f"Welcome, {session['face_authorizer_name']}. Packages ready for authorization are listed below.", "success")
            return redirect(url_for("face_hub"), code=303)
        flash("Invalid authorizing officer credentials.", "danger")
    return render_template("face_authorizer_login.html")


@app.route("/face/authorizer/logout")
def face_authorizer_logout():
    for k in ("face_authorizer_id", "face_authorizer_name", "face_authorizer_title"):
        session.pop(k, None)
    flash("Authorizing officer logged out.", "info")
    return redirect(url_for("face_hub"))


@app.route("/face/authorizers", methods=["GET", "POST"])
@login_required("super_admin")
def face_manage_authorizers():
    """General admin creates FACE authorizing officers."""
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            full_name = request.form.get("full_name", "").strip()
            title = request.form.get("title", "").strip()
            if not username or len(password) < 6:
                flash("Username and password (min 6 characters) required.", "danger")
            else:
                exists = conn.execute("SELECT id FROM face_users WHERE lower(username)=?", (username,)).fetchone()
                if exists:
                    flash("Username already exists.", "warning")
                else:
                    conn.execute(
                        """INSERT INTO face_users (username, password_hash, full_name, title, is_active, created_at)
                           VALUES (?,?,?,?,1,?)""",
                        (username, generate_password_hash(password), full_name, title, datetime.now().isoformat()),
                    )
                    conn.commit()
                    flash(f"Authorizing officer '{username}' created.", "success")
        elif action == "deactivate":
            uid = request.form.get("user_id")
            conn.execute("UPDATE face_users SET is_active=0 WHERE id=?", (uid,))
            conn.commit()
            flash("Authorizing officer deactivated.", "info")
        return redirect(url_for("face_manage_authorizers"), code=303)
    users = conn.execute("SELECT * FROM face_users ORDER BY username").fetchall()
    conn.close()
    return render_template("face_authorizers.html", users=users)


@app.route("/face/new/<mode>", methods=["POST"])
def face_new(mode):
    if mode not in ("requisition", "liquidation"):
        flash("Invalid mode.", "danger")
        return redirect(url_for("face_hub"))
    if _face_is_authorizer():
        flash("Authorizing officers review packages; they do not create new ones.", "warning")
        return redirect(url_for("face_hub"))
    data = _empty_face_data(mode)
    data["preparer_name"] = request.form.get("preparer_name", "").strip() or "Preparer"
    title = request.form.get("title", "").strip() or (
        f"{'Liquidation' if mode == 'liquidation' else 'Requisition'} – {datetime.now().strftime('%Y-%m-%d')}"
    )
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO face_packages (mode, title, status, data_json, preparer_name, prepared_at, created_at)
           VALUES (?,?, 'draft', ?, ?, ?, ?)""",
        (
            mode,
            title,
            _json_face.dumps(data),
            data["preparer_name"],
            datetime.now().isoformat(),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    pkg_id = cur.lastrowid
    conn.close()
    flash("FACE package created. Complete Annex 4, then FACE form and cover letter.", "success")
    return redirect(url_for("face_annex", pkg_id=pkg_id), code=303)


@app.route("/face/package/<int:pkg_id>")
def face_package_view(pkg_id):
    pkg = _load_package(pkg_id)
    if not pkg:
        flash("Package not found.", "danger")
        return redirect(url_for("face_hub"))
    return render_template(
        "face_package_view.html",
        pkg=pkg,
        d=pkg["data"],
        is_authorizer=_face_is_authorizer(),
        can_edit=_face_can_edit_package(pkg),
    )


@app.route("/face/package/<int:pkg_id>/annex", methods=["GET", "POST"])
def face_annex(pkg_id):
    pkg = _load_package(pkg_id)
    if not pkg:
        flash("Package not found.", "danger")
        return redirect(url_for("face_hub"))
    mode = pkg["mode"]
    draft = pkg["data"]
    is_liq = mode == "liquidation"
    editable = (pkg["status"] == "draft") and not _face_is_authorizer()

    if request.method == "POST":
        if not editable:
            flash("This package can no longer be edited by the preparer.", "warning")
            return redirect(url_for("face_package_view", pkg_id=pkg_id))
        draft["org_name"] = request.form.get("org_name", "").strip()
        draft["implementing_partner"] = draft["org_name"] or request.form.get("implementing_partner", "").strip()
        draft["project_name"] = request.form.get("project_name", "").strip()
        draft["prog_officer"] = request.form.get("prog_officer", "").strip()
        draft["reporting_period"] = request.form.get("reporting_period", "").strip()
        draft["new_request_period"] = request.form.get("new_request_period", "").strip()
        draft["activities"] = _parse_face_activities_from_form(request.form, mode)
        _sync_face_from_annex(draft, mode)
        _save_package_data(pkg_id, draft)
        if request.form.get("nav") == "face":
            flash("Annex 4 saved — FACE form amounts updated automatically.", "success")
            return redirect(url_for("face_form", pkg_id=pkg_id), code=303)
        flash("Annex 4 saved — FACE form and cover letter will use these totals.", "success")
        return redirect(url_for("face_annex", pkg_id=pkg_id), code=303)

    return render_template(
        "face_annex.html", mode=mode, d=draft, is_liq=is_liq, pkg=pkg, editable=editable, pkg_id=pkg_id
    )


@app.route("/face/package/<int:pkg_id>/form", methods=["GET", "POST"])
def face_form(pkg_id):
    pkg = _load_package(pkg_id)
    if not pkg:
        flash("Package not found.", "danger")
        return redirect(url_for("face_hub"))
    mode = pkg["mode"]
    draft = pkg["data"]
    is_liq = mode == "liquidation"
    editable = (pkg["status"] == "draft") and not _face_is_authorizer()

    if request.method == "POST":
        if not editable:
            flash("This package can no longer be edited by the preparer.", "warning")
            return redirect(url_for("face_package_view", pkg_id=pkg_id))
        for k in (
            "un_agency", "country", "date", "programme_code_title", "project_code_title",
            "responsible_officer", "implementing_partner", "currency", "request_type",
            "reporting_period", "new_request_period",
        ):
            if k in request.form:
                draft[k] = request.form.get(k, "").strip()
        auths = request.form.getlist("face_authorized")
        actuals = request.form.getlist("face_actual")
        for i, a in enumerate(draft.get("activities") or []):
            if i < len(auths) and auths[i] not in (None, ""):
                try:
                    a["authorized"] = float(auths[i])
                except ValueError:
                    pass
            if mode == "liquidation" and i < len(actuals) and actuals[i] not in (None, ""):
                try:
                    a["actual"] = float(actuals[i])
                except ValueError:
                    pass
        _save_package_data(pkg_id, draft)
        nav = request.form.get("nav")
        if nav == "cover":
            return redirect(url_for("face_cover", pkg_id=pkg_id), code=303)
        if nav == "annex":
            return redirect(url_for("face_annex", pkg_id=pkg_id), code=303)
        flash("FACE form saved.", "success")
        return redirect(url_for("face_form", pkg_id=pkg_id), code=303)

    _sync_face_from_annex(draft, mode)
    total_auth = sum(float(a.get("authorized") or 0) for a in draft.get("activities") or [])
    total_act = sum(float(a.get("actual") or 0) for a in draft.get("activities") or [])
    return render_template(
        "face_form.html", mode=mode, d=draft, is_liq=is_liq, pkg=pkg, editable=editable,
        pkg_id=pkg_id, total_auth=total_auth, total_act=total_act, auto_from_annex=True,
    )


@app.route("/face/package/<int:pkg_id>/cover", methods=["GET", "POST"])
def face_cover(pkg_id):
    pkg = _load_package(pkg_id)
    if not pkg:
        flash("Package not found.", "danger")
        return redirect(url_for("face_hub"))
    mode = pkg["mode"]
    draft = pkg["data"]
    is_liq = mode == "liquidation"
    editable = (pkg["status"] == "draft") and not _face_is_authorizer()

    if request.method == "POST":
        if not editable:
            flash("Cover letter content is locked after submission for authorization.", "warning")
            return redirect(url_for("face_package_view", pkg_id=pkg_id))
        for k in (
            "org_name", "org_address", "org_email", "ref_no", "date",
            "addressee_title", "addressee_office", "addressee_city",
            "subject", "cover_body_intro", "signatory_name", "signatory_title",
        ):
            if k in request.form:
                draft[k] = request.form.get(k, "").strip()
        # Organization logo on cover letter
        logo = request.files.get("org_logo")
        if logo and logo.filename and allowed_file(logo.filename):
            fname = secure_filename(logo.filename)
            out = f"face_org_logo_{pkg_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{fname}"
            logo.save(os.path.join("static", out))
            old = draft.get("org_logo_file") or ""
            if old and old != out:
                try:
                    op = os.path.join("static", old)
                    if os.path.isfile(op):
                        os.remove(op)
                except Exception:
                    pass
            draft["org_logo_file"] = out
        elif request.form.get("remove_org_logo") == "1":
            old = draft.get("org_logo_file") or ""
            if old:
                try:
                    op = os.path.join("static", old)
                    if os.path.isfile(op):
                        os.remove(op)
                except Exception:
                    pass
            draft["org_logo_file"] = ""
        amounts = request.form.getlist("cover_amount")
        for i, a in enumerate(draft.get("activities") or []):
            if i < len(amounts) and amounts[i] not in (None, ""):
                try:
                    if mode == "liquidation":
                        a["actual"] = float(amounts[i])
                    else:
                        a["authorized"] = float(amounts[i])
                except ValueError:
                    pass
        _save_package_data(pkg_id, draft)
        nav = request.form.get("nav")
        if nav == "face":
            return redirect(url_for("face_form", pkg_id=pkg_id), code=303)
        if nav == "ready":
            return redirect(url_for("face_mark_ready", pkg_id=pkg_id), code=303)
        flash("Cover letter saved.", "success")
        return redirect(url_for("face_cover", pkg_id=pkg_id), code=303)

    _sync_face_from_annex(draft, mode)
    return render_template(
        "face_cover.html", mode=mode, d=draft, is_liq=is_liq, pkg=pkg, editable=editable, pkg_id=pkg_id,
        auto_from_annex=True,
    )


@app.route("/face/package/<int:pkg_id>/ready", methods=["POST", "GET"])
def face_mark_ready(pkg_id):
    pkg = _load_package(pkg_id)
    if not pkg:
        flash("Package not found.", "danger")
        return redirect(url_for("face_hub"))
    if _face_is_authorizer():
        flash("Only the preparing office marks a package ready.", "warning")
        return redirect(url_for("face_package_view", pkg_id=pkg_id))
    if pkg["status"] != "draft":
        flash("Package is already submitted or authorized.", "info")
        return redirect(url_for("face_package_view", pkg_id=pkg_id))
    conn = get_db()
    conn.execute(
        "UPDATE face_packages SET status='ready', ready_at=? WHERE id=?",
        (datetime.now().isoformat(), pkg_id),
    )
    conn.commit()
    conn.close()
    flash("Package marked READY. The authorizing officer can now log in, review, and authorize.", "success")
    return redirect(url_for("face_package_view", pkg_id=pkg_id), code=303)


@app.route("/face/package/<int:pkg_id>/authorize", methods=["GET", "POST"])
def face_authorize(pkg_id):
    if not _face_is_authorizer():
        flash("Please log in as the cover-letter authorizing officer.", "warning")
        return redirect(url_for("face_authorizer_login"))
    pkg = _load_package(pkg_id)
    if not pkg:
        flash("Package not found.", "danger")
        return redirect(url_for("face_hub"))
    if pkg["status"] != "ready":
        flash("Only packages marked READY can be authorized.", "warning")
        return redirect(url_for("face_package_view", pkg_id=pkg_id))

    conn = get_db()
    user = conn.execute("SELECT * FROM face_users WHERE id=?", (session["face_authorizer_id"],)).fetchone()
    conn.close()
    if not user:
        flash("Authorizer account not found.", "danger")
        return redirect(url_for("face_authorizer_login"))

    if request.method == "POST":
        # optional upload of signature/stamp at authorize time
        for field, col in (("signature", "signature_file"), ("stamp", "stamp_file")):
            f = request.files.get(field)
            if f and f.filename and allowed_file(f.filename):
                fname = secure_filename(f.filename)
                out = f"face_auth_{field}_{user['id']}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{fname}"
                f.save(os.path.join("static", out))
                conn = get_db()
                conn.execute(f"UPDATE face_users SET {col}=? WHERE id=?", (out, user["id"]))
                conn.commit()
                conn.close()
                user = dict(user)
                user[col] = out

        conn = get_db()
        user = conn.execute("SELECT * FROM face_users WHERE id=?", (session["face_authorizer_id"],)).fetchone()
        if not user["signature_file"] or not user["stamp_file"]:
            conn.close()
            flash("Upload both your signature and official stamp before authorizing.", "danger")
            return redirect(url_for("face_authorize", pkg_id=pkg_id))

        note = request.form.get("authorizer_note", "").strip()
        # stamp signatory on cover from authorizer profile
        data = pkg["data"]
        data["signatory_name"] = user["full_name"] or session.get("face_authorizer_name") or ""
        data["signatory_title"] = user["title"] or session.get("face_authorizer_title") or ""
        data["_auth_signature"] = user["signature_file"]
        data["_auth_stamp"] = user["stamp_file"]
        conn.execute(
            """UPDATE face_packages SET status='authorized', authorized_by=?, authorized_at=?,
               authorizer_note=?, data_json=? WHERE id=?""",
            (
                user["id"],
                datetime.now().isoformat(),
                note,
                _json_face.dumps(data),
                pkg_id,
            ),
        )
        conn.commit()
        conn.close()
        flash("Package AUTHORIZED. Both the preparing office and authorizing officer can download the PDF.", "success")
        return redirect(url_for("face_package_view", pkg_id=pkg_id), code=303)

    return render_template(
        "face_authorize.html",
        pkg=pkg,
        d=pkg["data"],
        user=user,
    )


@app.route("/face/package/<int:pkg_id>/pdf")
def face_pdf(pkg_id):
    pkg = _load_package(pkg_id)
    if not pkg:
        flash("Package not found.", "danger")
        return redirect(url_for("face_hub"))
    if pkg["status"] != "authorized":
        flash("PDF download is available only after the authorizing officer has signed the cover letter.", "warning")
        return redirect(url_for("face_package_view", pkg_id=pkg_id))
    draft = pkg["data"]
    # use authorizer signature/stamp stored on package
    draft["signature_file"] = draft.get("_auth_signature") or ""
    draft["stamp_file"] = draft.get("_auth_stamp") or ""
    if not draft["signature_file"] or not draft["stamp_file"]:
        flash("Authorized package is missing signature/stamp files.", "danger")
        return redirect(url_for("face_package_view", pkg_id=pkg_id))
    buffer = _build_face_package_pdf(draft, pkg["mode"])
    label = "Liquidation" if pkg["mode"] == "liquidation" else "Requisition"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (draft.get("org_name") or "FACE"))[:24]
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"FACE_{label}_{safe}_{pkg_id}_{datetime.now().strftime('%Y%m%d')}.pdf",
        mimetype="application/pdf",
    )


def _build_face_package_pdf(d, mode):
    """Build multi-page PDF: Cover (A), FACE (B), Annex 4 (C)."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=0.55 * inch, rightMargin=0.55 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    story = []
    is_liq = mode == "liquidation"
    activities = d.get("activities") or []

    def money(n):
        try:
            return f"{float(n):,.2f}"
        except Exception:
            return "0.00"

    sig_path = os.path.join("static", d.get("signature_file") or "")
    stamp_path = os.path.join("static", d.get("stamp_file") or "")

    # Organization logo on cover letter letterhead
    logo_file = d.get("org_logo_file") or ""
    logo_path = os.path.join("static", logo_file) if logo_file else ""
    if logo_path and os.path.isfile(logo_path):
        try:
            story.append(Image(logo_path, width=1.5 * inch, height=0.75 * inch))
            story.append(Spacer(1, 6))
        except Exception:
            pass
    if d.get("org_name"):
        story.append(Paragraph(f"<b>{d['org_name']}</b>", styles["Title"]))
    if d.get("org_address"):
        story.append(Paragraph(d["org_address"], styles["Normal"]))
    if d.get("org_email"):
        story.append(Paragraph(f"Email: {d['org_email']}", styles["Normal"]))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"<b>{d.get('ref_no') or ''}</b>" + "&nbsp;" * 40 + f"{d.get('date') or ''}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 12))
    story.append(Paragraph(d.get("addressee_title") or "Chief of Field Office,", styles["Normal"]))
    story.append(Paragraph(d.get("addressee_office") or "UNICEF Field Office,", styles["Normal"]))
    story.append(Paragraph(d.get("addressee_city") or "", styles["Normal"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"<b>{d.get('subject') or ''}</b>", styles["Heading3"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(d.get("cover_body_intro") or "", styles["Normal"]))
    story.append(Spacer(1, 10))
    cover_data = [["SN", "ACTIVITY", "AMOUNT (N)"]]
    grand = 0.0
    for a in activities:
        amt = float(a.get("actual") or 0) if is_liq else float(a.get("authorized") or 0)
        grand += amt
        cover_data.append([a.get("code") or "", a.get("name") or "", money(amt)])
    cover_data.append(["", "GRAND TOTAL", money(grand)])
    ct = Table(cover_data, colWidths=[50, 320, 100])
    ct.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a365d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
    ]))
    story.append(ct)
    story.append(Spacer(1, 20))
    story.append(Paragraph("Best Regards,", styles["Normal"]))
    story.append(Spacer(1, 8))
    if os.path.isfile(sig_path):
        try:
            story.append(Image(sig_path, width=1.4 * inch, height=0.55 * inch))
        except Exception:
            pass
    if os.path.isfile(stamp_path):
        try:
            story.append(Image(stamp_path, width=0.9 * inch, height=0.9 * inch))
        except Exception:
            pass
    story.append(Paragraph(f"<b>{d.get('signatory_name') or ''}</b>", styles["Normal"]))
    story.append(Paragraph(d.get("signatory_title") or "", styles["Normal"]))
    story.append(Paragraph("<i>Authorized signatory (cover letter)</i>", styles["Normal"]))
    story.append(PageBreak())

    story.append(Paragraph("<b>Funding Authorization and Certificate of Expenditure</b>", styles["Heading1"]))
    story.append(Paragraph(
        f"UN Agency: <b>{d.get('un_agency') or 'UNICEF'}</b> &nbsp;&nbsp; Date: <b>{d.get('date') or ''}</b><br/>"
        f"Country: <b>{d.get('country') or ''}</b> &nbsp;&nbsp; "
        f"Type: <b>{'Direct Cash Transfer (DCT)' if d.get('request_type')=='dct' else d.get('request_type','').replace('_',' ').title()}</b><br/>"
        f"Programme: {d.get('programme_code_title') or '—'}<br/>"
        f"Project: {d.get('project_code_title') or '—'}<br/>"
        f"Responsible Officer(s): {d.get('responsible_officer') or '—'}<br/>"
        f"Implementing Partner: <b>{d.get('implementing_partner') or d.get('org_name') or '—'}</b><br/>"
        f"Currency: {d.get('currency') or 'Naira (N)'}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 8))
    if is_liq:
        hdr = ["Activity", "Authorized (A)", "Actual (B)", "Accepted (C)", "Balance (D=A-C)"]
        face_data = [hdr]
        for a in activities:
            auth = float(a.get("authorized") or 0)
            act = float(a.get("actual") or 0)
            face_data.append([
                f"{a.get('code','')} {a.get('name','')}"[:42],
                money(auth), money(act), "", money(auth),
            ])
        face_data.append([
            "TOTAL",
            money(sum(float(a.get("authorized") or 0) for a in activities)),
            money(sum(float(a.get("actual") or 0) for a in activities)),
            "",
            money(sum(float(a.get("authorized") or 0) for a in activities)),
        ])
        widths = [200, 75, 75, 75, 75]
    else:
        hdr = ["Activity", "New Request (E)", "Authorized (F)", "Outstanding (G)"]
        face_data = [hdr]
        for a in activities:
            auth = float(a.get("authorized") or 0)
            face_data.append([f"{a.get('code','')} {a.get('name','')}"[:48], money(auth), "", money(auth)])
        face_data.append([
            "TOTAL",
            money(sum(float(a.get("authorized") or 0) for a in activities)),
            "",
            money(sum(float(a.get("authorized") or 0) for a in activities)),
        ])
        widths = [250, 90, 90, 90]
    ft = Table(face_data, colWidths=widths)
    ft.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a365d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(ft)
    story.append(Spacer(1, 12))
    story.append(Paragraph("<b>CERTIFICATION</b>", styles["Heading3"]))
    if is_liq:
        story.append(Paragraph(
            "The actual expenditures for the period stated herein have been disbursed in accordance with the AWP "
            "and request with itemized cost estimates. Supporting documents can be made available for five years.",
            styles["Normal"],
        ))
    else:
        story.append(Paragraph(
            "The funding request shown above represents estimated expenditures as per AWP and itemized cost "
            "estimates attached.",
            styles["Normal"],
        ))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"Date Submitted: {d.get('date') or ''}", styles["Normal"]))
    if os.path.isfile(sig_path):
        try:
            story.append(Image(sig_path, width=1.3 * inch, height=0.5 * inch))
        except Exception:
            pass
    if os.path.isfile(stamp_path):
        try:
            story.append(Image(stamp_path, width=0.85 * inch, height=0.85 * inch))
        except Exception:
            pass
    story.append(Paragraph(f"Name/Sign: <b>{d.get('signatory_name') or ''}</b>", styles["Normal"]))
    story.append(Paragraph(f"Title / Official Stamp: {d.get('signatory_title') or ''}", styles["Normal"]))
    story.append(PageBreak())

    title = "ANNEX 4 : ITEMIZED COST ESTIMATE / BUDGET"
    title += " (LIQUIDATION)" if is_liq else " (PROPOSAL)"
    story.append(Paragraph(f"<b>{title}</b>", styles["Heading2"]))
    story.append(Paragraph(
        f"Implementing Partner: <b>{d.get('implementing_partner') or d.get('org_name') or '—'}</b><br/>"
        f"Project: {d.get('project_name') or '—'}<br/>"
        f"Responsible Prog. Officer: {d.get('prog_officer') or '—'}<br/>"
        f"Period: {d.get('reporting_period') or d.get('new_request_period') or '—'}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 8))
    for a in activities:
        story.append(Paragraph(f"<b>{a.get('code','')} {a.get('name','')}</b>", styles["Heading4"]))
        lines = a.get("lines") or []
        if not lines:
            if is_liq:
                story.append(Paragraph(
                    f"Authorized: N{money(a.get('authorized'))} | Actual: N{money(a.get('actual'))}",
                    styles["Normal"],
                ))
            else:
                story.append(Paragraph(f"Budget total: N{money(a.get('authorized'))}", styles["Normal"]))
            story.append(Spacer(1, 6))
            continue
        if is_liq:
            ld = [["Description", "Qty", "Days", "Freq", "Rate", "Budget", "Act.Total", "Variance"]]
            for L in lines:
                ld.append([
                    (L.get("description") or "")[:36],
                    str(L.get("qty") or ""), str(L.get("days") or ""), str(L.get("freq") or ""),
                    money(L.get("rate")), money(L.get("budget_total")),
                    money(L.get("actual_total")), money(L.get("variance_total")),
                ])
            ld.append([
                "Sub-total", "", "", "", "",
                money(sum(L.get("budget_total", 0) for L in lines)),
                money(sum(L.get("actual_total", 0) for L in lines)),
                money(sum(L.get("variance_total", 0) for L in lines)),
            ])
            widths = [130, 35, 35, 35, 50, 55, 55, 55]
        else:
            ld = [["Description", "Qty", "Days", "Freq", "Rate", "Total"]]
            for L in lines:
                ld.append([
                    (L.get("description") or "")[:40],
                    str(L.get("qty") or ""), str(L.get("days") or ""), str(L.get("freq") or ""),
                    money(L.get("rate")), money(L.get("budget_total")),
                ])
            ld.append(["Sub-total", "", "", "", "", money(sum(L.get("budget_total", 0) for L in lines))])
            widths = [180, 40, 40, 40, 60, 70]
        lt = Table(ld, colWidths=widths)
        lt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b6cb0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
            ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ]))
        story.append(lt)
        story.append(Spacer(1, 10))

    story.append(Spacer(1, 16))
    story.append(Paragraph("Implementing Partner's Designated Official (authorized):", styles["Normal"]))
    if os.path.isfile(sig_path):
        try:
            story.append(Image(sig_path, width=1.3 * inch, height=0.5 * inch))
        except Exception:
            pass
    if os.path.isfile(stamp_path):
        try:
            story.append(Image(stamp_path, width=0.85 * inch, height=0.85 * inch))
        except Exception:
            pass
    story.append(Paragraph(f"<b>{d.get('signatory_name') or ''}</b> — {d.get('signatory_title') or ''}", styles["Normal"]))
    story.append(Paragraph(f"Date: {d.get('date') or ''}", styles["Normal"]))
    doc.build(story)
    buffer.seek(0)
    return buffer



# ---------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
