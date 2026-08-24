from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, session, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
from datetime import datetime
import pandas as pd
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import inch

app = Flask(__name__)
app.secret_key = "change-this-to-a-very-strong-secret-key-98765"
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs("static", exist_ok=True)

ADMIN_PASSWORD_HASH = generate_password_hash("admin123")

# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------
def get_db():
    conn = sqlite3.connect("procurement.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    
    # Quotations table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_number TEXT,
            name TEXT,
            address TEXT,
            cac_number TEXT,
            experience TEXT,
            tax_clearance TEXT,
            bank TEXT,
            reg_with_govt TEXT,
            three_yrs_audit TEXT,
            description TEXT,
            amount REAL,
            service_type TEXT,
            comment TEXT,
            invoice_filename TEXT,
            submitted_at TEXT,
            system_score REAL DEFAULT 0
        )
    """)

    # Committee members
    conn.execute("""
        CREATE TABLE IF NOT EXISTS committee (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            designation TEXT,
            password_hash TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)

    # Individual scores from committee members
    conn.execute("""
        CREATE TABLE IF NOT EXISTS committee_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            committee_id INTEGER,
            quotation_id INTEGER,
            score REAL,
            comment TEXT,
            scored_at TEXT,
            UNIQUE(committee_id, quotation_id)
        )
    """)

    # Settings (for logo and organization name)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Insert default settings if empty
    cur = conn.execute("SELECT COUNT(*) FROM settings")
    if cur.fetchone()[0] == 0:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("org_name", "Your Organization Name"))
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("logo_filename", ""))

    conn.commit()
    conn.close()

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

def calculate_system_scores():
    """Original automatic scoring"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM quotations").fetchall()
    if not rows:
        conn.close()
        return []

    df = pd.DataFrame([dict(r) for r in rows])
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)

    def yn(val):
        return 1 if str(val).strip().lower() == "yes" else 0

    max_price = df["amount"].max()
    min_price = df["amount"].min()

    def price_score(x):
        if max_price == min_price:
            return 1.0
        return (max_price - x) / (max_price - min_price)

    df["system_score"] = (
        df["experience"].apply(yn) * 20 +
        df["tax_clearance"].apply(yn) * 15 +
        df["three_yrs_audit"].apply(yn) * 15 +
        df["amount"].apply(price_score) * 50
    )

    for _, row in df.iterrows():
        conn.execute(
            "UPDATE quotations SET system_score = ? WHERE id = ?",
            (round(float(row["system_score"]), 2), int(row["id"]))
        )
    conn.commit()
    conn.close()
    return df

def get_final_rankings():
    """Combine system score + average committee score"""
    conn = get_db()
    quotations = conn.execute("SELECT * FROM quotations").fetchall()
    if not quotations:
        conn.close()
        return []

    result = []
    for q in quotations:
        scores = conn.execute(
            "SELECT score FROM committee_scores WHERE quotation_id = ?", (q["id"],)
        ).fetchall()
        
        committee_avg = 0
        if scores:
            committee_avg = sum(s["score"] for s in scores) / len(scores)

        # Final score = 40% system + 60% committee average
        final_score = (q["system_score"] * 0.4) + (committee_avg * 0.6)

        result.append({
            **dict(q),
            "committee_avg": round(committee_avg, 2),
            "final_score": round(final_score, 2),
            "num_scores": len(scores)
        })

    conn.close()
    result.sort(key=lambda x: x["final_score"], reverse=True)
    return result

# ---------------------------------------------------------
# ROUTES - PUBLIC
# ---------------------------------------------------------
@app.route("/")
def index():
    org_name = get_setting("org_name", "eProcurement System")
    logo = get_setting("logo_filename")
    return render_template("index.html", org_name=org_name, logo=logo)

@app.route("/submit", methods=["GET", "POST"])
def submit_quotation():
    if request.method == "POST":
        data = {
            "vendor_number": request.form.get("vendor_number", "").strip(),
            "name": request.form.get("name", "").strip(),
            "address": request.form.get("address", "").strip(),
            "cac_number": request.form.get("cac_number", "").strip(),
            "experience": request.form.get("experience", ""),
            "tax_clearance": request.form.get("tax_clearance", ""),
            "bank": request.form.get("bank", "").strip(),
            "reg_with_govt": request.form.get("reg_with_govt", ""),
            "three_yrs_audit": request.form.get("three_yrs_audit", ""),
            "description": request.form.get("description", "").strip(),
            "amount": request.form.get("amount", "0"),
            "service_type": request.form.get("service_type", "").strip(),
            "comment": request.form.get("comment", "").strip(),
        }

        if not data["vendor_number"] or not data["name"]:
            flash("Vendor Number and Name are required.", "danger")
            return redirect(url_for("submit_quotation"))

        try:
            amount = float(data["amount"])
            if amount <= 0:
                raise ValueError
        except:
            flash("Amount must be a positive number.", "danger")
            return redirect(url_for("submit_quotation"))

        invoice_filename = None
        file = request.files.get("invoice")
        if file and file.filename:
            if not file.filename.lower().endswith(".pdf"):
                flash("Only PDF files are allowed.", "danger")
                return redirect(url_for("submit_quotation"))
            filename = secure_filename(file.filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            invoice_filename = f"{data['vendor_number']}_{timestamp}_{filename}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], invoice_filename))

        conn = get_db()
        conn.execute("""
            INSERT INTO quotations (
                vendor_number, name, address, cac_number, experience,
                tax_clearance, bank, reg_with_govt, three_yrs_audit,
                description, amount, service_type, comment,
                invoice_filename, submitted_at, system_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            data["vendor_number"], data["name"], data["address"], data["cac_number"],
            data["experience"], data["tax_clearance"], data["bank"], data["reg_with_govt"],
            data["three_yrs_audit"], data["description"], amount, data["service_type"],
            data["comment"], invoice_filename, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        conn.commit()
        conn.close()

        calculate_system_scores()
        flash("Quotation submitted successfully! Thank you.", "success")
        return redirect(url_for("index"))

    return render_template("submit.html")

# ---------------------------------------------------------
# ADMIN ROUTES
# ---------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if check_password_hash(ADMIN_PASSWORD_HASH, password):
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Wrong password", "danger")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/admin")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))

    rankings = get_final_rankings()
    winner = rankings[0] if rankings else None
    org_name = get_setting("org_name")
    logo = get_setting("logo_filename")

    # Chart data
    chart_labels = [r["name"][:20] for r in rankings[:8]]
    chart_scores = [r["final_score"] for r in rankings[:8]]

    return render_template("admin.html",
                           quotations=rankings,
                           winner=winner,
                           org_name=org_name,
                           logo=logo,
                           chart_labels=chart_labels,
                           chart_scores=chart_scores)

@app.route("/admin/settings", methods=["GET", "POST"])
def admin_settings():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        org_name = request.form.get("org_name", "").strip()
        if org_name:
            set_setting("org_name", org_name)

        file = request.files.get("logo")
        if file and file.filename:
            filename = secure_filename(file.filename)
            file.save(os.path.join("static", filename))
            set_setting("logo_filename", filename)

        flash("Settings saved successfully!", "success")
        return redirect(url_for("admin_settings"))

    return render_template("admin_settings.html",
                           org_name=get_setting("org_name"),
                           logo=get_setting("logo_filename"))

@app.route("/admin/committee", methods=["GET", "POST"])
def admin_committee():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))

    conn = get_db()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            designation = request.form.get("designation", "").strip()
            password = request.form.get("password", "").strip()
            if name and password:
                count = conn.execute("SELECT COUNT(*) FROM committee WHERE is_active=1").fetchone()[0]
                if count >= 5:
                    flash("Maximum of 5 committee members allowed.", "warning")
                else:
                    conn.execute(
                        "INSERT INTO committee (name, designation, password_hash) VALUES (?, ?, ?)",
                        (name, designation, generate_password_hash(password))
                    )
                    conn.commit()
                    flash("Committee member added.", "success")
        elif action == "delete":
            member_id = request.form.get("member_id")
            conn.execute("UPDATE committee SET is_active=0 WHERE id=?", (member_id,))
            conn.commit()
            flash("Member removed.", "success")

    members = conn.execute("SELECT * FROM committee WHERE is_active=1").fetchall()
    conn.close()
    return render_template("admin_committee.html", members=members)

@app.route("/download/<filename>")
def download_invoice(filename):
    if not session.get("admin") and not session.get("committee_id"):
        return redirect(url_for("admin_login"))
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=True)

# ---------------------------------------------------------
# COMMITTEE ROUTES
# ---------------------------------------------------------
@app.route("/committee/login", methods=["GET", "POST"])
def committee_login():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        member = conn.execute(
            "SELECT * FROM committee WHERE name=? AND is_active=1", (name,)
        ).fetchone()
        conn.close()

        if member and check_password_hash(member["password_hash"], password):
            session["committee_id"] = member["id"]
            session["committee_name"] = member["name"]
            return redirect(url_for("committee_score"))
        flash("Invalid name or password", "danger")
    return render_template("committee_login.html")

@app.route("/committee/logout")
def committee_logout():
    session.pop("committee_id", None)
    session.pop("committee_name", None)
    return redirect(url_for("index"))

@app.route("/committee/score", methods=["GET", "POST"])
def committee_score():
    if not session.get("committee_id"):
        return redirect(url_for("committee_login"))

    conn = get_db()
    quotations = conn.execute("SELECT * FROM quotations ORDER BY name").fetchall()

    if request.method == "POST":
        for q in quotations:
            score_val = request.form.get(f"score_{q['id']}")
            comment = request.form.get(f"comment_{q['id']}", "")
            if score_val:
                try:
                    score = float(score_val)
                    if 0 <= score <= 100:
                        conn.execute("""
                            INSERT OR REPLACE INTO committee_scores 
                            (committee_id, quotation_id, score, comment, scored_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            session["committee_id"], q["id"], score, comment,
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        ))
                except:
                    pass
        conn.commit()
        flash("Your scores have been saved successfully!", "success")
        return redirect(url_for("committee_score"))

    # Get existing scores of this member
    existing = {}
    rows = conn.execute(
        "SELECT * FROM committee_scores WHERE committee_id=?",
        (session["committee_id"],)
    ).fetchall()
    for r in rows:
        existing[r["quotation_id"]] = r

    conn.close()
    return render_template("committee_score.html",
                           quotations=quotations,
                           existing=existing,
                           member_name=session.get("committee_name"))

# ---------------------------------------------------------
# PDF GENERATION
# ---------------------------------------------------------
@app.route("/admin/generate_report")
def generate_report():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))

    rankings = get_final_rankings()
    if not rankings:
        flash("No quotations available.", "warning")
        return redirect(url_for("admin_dashboard"))

    winner = rankings[0]
    org_name = get_setting("org_name", "Organization")
    logo_file = get_setting("logo_filename")
    today = datetime.now().strftime("%d %B %Y")

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    # Logo
    if logo_file and os.path.exists(os.path.join("static", logo_file)):
        try:
            story.append(Image(os.path.join("static", logo_file), width=1.8*inch, height=0.9*inch))
            story.append(Spacer(1, 10))
        except:
            pass

    story.append(Paragraph(f"<b>{org_name}</b>", styles["Title"]))
    story.append(Paragraph("<b>PROCUREMENT COMMITTEE EVALUATION REPORT</b>", styles["Heading1"]))
    story.append(Paragraph(f"Date: {today}", styles["Normal"]))
    story.append(Spacer(1, 20))

    story.append(Paragraph("1. EXECUTIVE SUMMARY", styles["Heading2"]))
    story.append(Paragraph(
        f"This report presents the evaluation of <b>{len(rankings)}</b> vendors. "
        f"Scores combine system evaluation (40%) and committee average scores (60%).",
        styles["Normal"]
    ))
    story.append(Spacer(1, 12))

    story.append(Paragraph("2. RECOMMENDED VENDOR", styles["Heading2"]))
    story.append(Paragraph(
        f"<b>Vendor:</b> {winner['name']}<br/>"
        f"<b>Vendor Number:</b> {winner['vendor_number']}<br/>"
        f"<b>Amount:</b> ₦{winner['amount']:,.2f}<br/>"
        f"<b>Final Score:</b> {winner['final_score']} / 100<br/>"
        f"<b>Committee Average:</b> {winner['committee_avg']}<br/>"
        f"<b>Description:</b> {winner['description'] or 'N/A'}",
        styles["Normal"]
    ))
    story.append(Spacer(1, 15))

    story.append(Paragraph("3. DETAILED EVALUATION", styles["Heading2"]))
    table_data = [["Rank", "Vendor", "Amount (₦)", "System", "Committee", "Final"]]
    for i, r in enumerate(rankings, 1):
        table_data.append([
            str(i),
            r["name"][:25],
            f"{r['amount']:,.0f}",
            f"{r['system_score']:.1f}",
            f"{r['committee_avg']:.1f}",
            f"{r['final_score']:.1f}"
        ])

    table = Table(table_data, colWidths=[40, 140, 80, 60, 70, 60])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a365d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#fefcbf")),
    ]))
    story.append(table)
    story.append(Spacer(1, 25))

    # Committee members
    conn = get_db()
    members = conn.execute("SELECT name, designation FROM committee WHERE is_active=1").fetchall()
    conn.close()

    story.append(Paragraph("4. PROCUREMENT COMMITTEE", styles["Heading2"]))
    for i, m in enumerate(members, 1):
        story.append(Paragraph(f"{i}. {m['name']} — {m['designation'] or 'Member'}", styles["Normal"]))

    story.append(Spacer(1, 30))
    story.append(Paragraph("Generated by Knowsoft eProcurement System", styles["Normal"]))

    doc.build(story)
    buffer.seek(0)

    return send_file(buffer, as_attachment=True,
                     download_name=f"Committee_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                     mimetype="application/pdf")

@app.route("/admin/generate_lpo")
def generate_lpo():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))

    rankings = get_final_rankings()
    if not rankings:
        flash("No quotations available.", "warning")
        return redirect(url_for("admin_dashboard"))

    winner = rankings[0]
    org_name = get_setting("org_name", "Organization")
    logo_file = get_setting("logo_filename")
    today = datetime.now().strftime("%d %B %Y")

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    if logo_file and os.path.exists(os.path.join("static", logo_file)):
        try:
            story.append(Image(os.path.join("static", logo_file), width=1.8*inch, height=0.9*inch))
            story.append(Spacer(1, 10))
        except:
            pass

    story.append(Paragraph(f"<b>{org_name}</b>", styles["Title"]))
    story.append(Paragraph("<b>LOCAL PURCHASE ORDER (LPO)</b>", styles["Heading1"]))
    story.append(Paragraph(f"Date of Issue: {today}", styles["Normal"]))
    story.append(Spacer(1, 20))

    story.append(Paragraph("<b>1. SUPPLIER INFORMATION</b>", styles["Heading2"]))
    story.append(Paragraph(f"<b>Vendor Name:</b> {winner['name']}", styles["Normal"]))
    story.append(Paragraph(f"<b>Vendor Number:</b> {winner['vendor_number']}", styles["Normal"]))
    story.append(Paragraph(f"<b>Description:</b> {winner['description'] or 'N/A'}", styles["Normal"]))
    story.append(Paragraph(f"<b>Total Contract Value:</b> ₦{winner['amount']:,.2f}", styles["Normal"]))
    story.append(Spacer(1, 15))

    story.append(Paragraph("<b>2. JUSTIFICATION</b>", styles["Heading2"]))
    story.append(Paragraph(
        "The above vendor was selected after evaluation by the Procurement Committee "
        "based on technical compliance, experience, price competitiveness and committee scoring.",
        styles["Normal"]
    ))
    story.append(Spacer(1, 15))

    story.append(Paragraph("<b>3. TERMS AND CONDITIONS</b>", styles["Heading2"]))
    story.append(Paragraph(
        "1. Delivery must be according to agreed specifications and timeline.<br/>"
        "2. Payment shall be processed upon satisfactory delivery and invoice verification.<br/>"
        "3. Any deviation requires prior written approval.<br/>"
        "4. This LPO is subject to the procurement evaluation process.",
        styles["Normal"]
    ))
    story.append(Spacer(1, 25))

    story.append(Paragraph("<b>4. APPROVAL</b>", styles["Heading2"]))
    story.append(Paragraph("Authorized Signature: _______________________________", styles["Normal"]))
    story.append(Paragraph(f"Date: {today}", styles["Normal"]))
    story.append(Spacer(1, 30))
    story.append(Paragraph("Generated by Knowsoft eProcurement System", styles["Normal"]))

    doc.build(story)
    buffer.seek(0)

    return send_file(buffer, as_attachment=True,
                     download_name=f"LPO_{winner['vendor_number']}_{datetime.now().strftime('%Y%m%d')}.pdf",
                     mimetype="application/pdf")

# ---------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
