from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, session
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
from datetime import datetime
import pandas as pd

app = Flask(__name__)
app.secret_key = "change-this-to-a-strong-secret-key-12345"
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# Simple admin password (change it!)
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
            score REAL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------
# SCORING
# ---------------------------------------------------------
def calculate_scores():
    conn = get_db()
    rows = conn.execute("SELECT * FROM quotations").fetchall()
    conn.close()

    if not rows:
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

    df["score"] = (
        df["experience"].apply(yn) * 20 +
        df["tax_clearance"].apply(yn) * 15 +
        df["three_yrs_audit"].apply(yn) * 15 +
        df["amount"].apply(price_score) * 50
    )

    conn = get_db()
    for _, row in df.iterrows():
        conn.execute(
            "UPDATE quotations SET score = ? WHERE id = ?",
            (round(float(row["score"]), 2), int(row["id"]))
        )
    conn.commit()
    conn.close()

    return df.sort_values("score", ascending=False).to_dict("records")


# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


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
                flash("Only PDF files are allowed for invoice.", "danger")
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
                invoice_filename, submitted_at, score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            data["vendor_number"], data["name"], data["address"], data["cac_number"],
            data["experience"], data["tax_clearance"], data["bank"], data["reg_with_govt"],
            data["three_yrs_audit"], data["description"], amount, data["service_type"],
            data["comment"], invoice_filename, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        conn.commit()
        conn.close()

        calculate_scores()

        flash("Quotation submitted successfully! Thank you.", "success")
        return redirect(url_for("index"))

    return render_template("submit.html")


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
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/admin")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))

    ranked = calculate_scores()
    winner = ranked[0] if ranked else None

    return render_template("admin.html", quotations=ranked, winner=winner)


@app.route("/download/<filename>")
def download_invoice(filename):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=True)


# ---------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5000)