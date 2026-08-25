# Knowsoft eProcurement System

Multi-organization electronic procurement portal for vendor registration, quotation submission, committee evaluation, and purchase order generation.

## Features

- **Landing page** – Select recipient organization
- **Vendor portal** – Registration, login, quotation form with tax calculation, document uploads, consent
- **Admin portal** – Manage organizations, services, committee members, dashboards, ranking, LPO & PDF reports
- **Committee portal** – Login, review submissions, score vendors, upload signature
- **Scoring** – System score (price/docs) 40% + Committee average 60%
- **Emails** – Simulated (logged to `instance/email_log.txt` and console)
- **SQLite** database

## Quick Start

```bash
cd artifacts
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

### Default Admin
- Password: `Aidah@esemi`
- You will be forced to change it on first login.

## Typical Flow

1. Admin logs in → adds Organizations → for each org adds Services and Committee members.
2. Vendor selects org on landing → Register → Login → Submit Quotation.
3. Committee members login → score quotations.
4. Admin views rankings per org/service → generates Evaluation Report PDF and LPO for winner.

## Project Structure

```
artifacts/
├── app.py              # Main Flask application
├── requirements.txt
├── instance/
│   ├── procurement.db  # SQLite (created on first run)
│   └── email_log.txt   # Simulated emails
├── static/             # Logos etc.
├── uploads/            # Vendor documents
└── templates/          # Jinja2 templates
```

## Notes

- File uploads limited to 16 MB; allowed types: PDF, PNG, JPG, JPEG, GIF.
- Change `app.secret_key` and use a production WSGI server (gunicorn/waitress) for deployment.
- Real SMTP can be plugged into `send_email_simulation()`.
