from __future__ import annotations

import os
import random
import sqlite3
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from io import BytesIO
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("SPENDSMART_DB_PATH", str(DATA_DIR / "spendsmart.db")))

EXPENSE_CATEGORIES = [
    "Food",
    "Travel",
    "Shopping",
    "Education",
    "Rent",
    "Bills",
    "Health",
    "Entertainment",
    "Other",
]
PAYMENT_METHODS = ["Cash", "UPI", "Card", "Net Banking"]
ALERT_LEVELS = {
    "alert_50": {
        "label": "50% warning",
        "message": "You have used 50% of your monthly budget.",
        "percentage": 50,
        "severity": "normal",
    },
    "alert_80": {
        "label": "80% high alert",
        "message": "High Alert! You are close to your monthly spending limit.",
        "percentage": 80,
        "severity": "warning",
    },
    "alert_100": {
        "label": "100% limit exceeded",
        "message": "Your monthly limit is over. Please stop spending or extend your budget.",
        "percentage": 100,
        "severity": "critical",
    },
}
MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


app = Flask(__name__)
app.config["SECRET_KEY"] = "spendsmart-secret-key"
OTP_EXPIRY_MINUTES = 10
DEMO_OTP_CODE = os.getenv("SPENDSMART_DEMO_OTP", "").strip()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def future_str(minutes: int) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def today_parts() -> tuple[int, int]:
    today = date.today()
    return today.month, today.year


def month_label(month: int, year: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {year}"


def rupee(value: float | int | None) -> str:
    amount = float(value or 0)
    return f"₹{amount:,.0f}" if amount == int(amount) else f"₹{amount:,.2f}"


@app.template_filter("rupee")
def rupee_filter(value: float | int | None) -> str:
    return rupee(value)


@app.template_filter("datetimeformat")
def datetime_filter(value: str | None, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    if not value:
        return "-"
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime(fmt)
    except ValueError:
        return value


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            mobile TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            user_type TEXT NOT NULL,
            notifications_enabled INTEGER NOT NULL DEFAULT 1,
            parent_summary_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            budget_amount REAL NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            description TEXT,
            expense_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            alert_type TEXT NOT NULL,
            message TEXT NOT NULL,
            percentage REAL NOT NULL,
            created_at TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS parents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_user_id INTEGER NOT NULL UNIQUE,
            parent_name TEXT NOT NULL,
            parent_email TEXT NOT NULL,
            parent_mobile TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (student_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS budget_extensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            old_budget REAL NOT NULL,
            new_budget REAL NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS password_reset_otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            otp_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_used INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """
    )
    db.commit()


def query_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return get_db().execute(query, params).fetchone()


def query_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return get_db().execute(query, params).fetchall()


def cleanup_expired_otps() -> None:
    get_db().execute("DELETE FROM password_reset_otps WHERE expires_at < ?", (now_str(),))
    get_db().commit()


def generate_otp() -> str:
    if DEMO_OTP_CODE:
        return DEMO_OTP_CODE
    return f"{random.randint(0, 999999):06d}"


def send_email_message(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    smtp_host = os.getenv("SPENDSMART_SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SPENDSMART_SMTP_PORT", "587"))
    smtp_user = os.getenv("SPENDSMART_SMTP_USER", "").strip()
    smtp_password = os.getenv("SPENDSMART_SMTP_PASSWORD", "").strip()
    from_email = os.getenv("SPENDSMART_FROM_EMAIL", smtp_user).strip()

    if not smtp_host or not from_email:
        return False, "SMTP email is not configured."

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = to_email
    message.set_content(body)

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as server:
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.send_message(message)
    except Exception as exc:
        return False, str(exc)
    return True, "OTP sent successfully."


def create_password_reset_otp(user: sqlite3.Row) -> tuple[bool, str]:
    cleanup_expired_otps()
    otp_code = generate_otp()
    db = get_db()
    db.execute("UPDATE password_reset_otps SET is_used = 1 WHERE user_id = ? AND is_used = 0", (user["id"],))
    db.execute(
        """
        INSERT INTO password_reset_otps
        (user_id, email, otp_hash, expires_at, created_at, is_used)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (
            user["id"],
            user["email"],
            generate_password_hash(otp_code),
            future_str(OTP_EXPIRY_MINUTES),
            now_str(),
        ),
    )
    db.commit()

    email_body = (
        f"Hello {user['full_name']},\n\n"
        f"Your SpendSmart password reset OTP is: {otp_code}\n"
        f"This OTP will expire in {OTP_EXPIRY_MINUTES} minutes.\n\n"
        "If you did not request this reset, please ignore this email."
    )
    success, message = send_email_message(user["email"], "SpendSmart Password Reset OTP", email_body)
    if not success:
        app.logger.warning("OTP email delivery failed for %s: %s | OTP=%s", user["email"], message, otp_code)
    return success, otp_code


def get_latest_valid_otp(user_id: int, email: str) -> sqlite3.Row | None:
    cleanup_expired_otps()
    return query_one(
        """
        SELECT * FROM password_reset_otps
        WHERE user_id = ? AND email = ? AND is_used = 0 AND expires_at >= ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id, email, now_str()),
    )


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def get_current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def month_year_from_request() -> tuple[int, int]:
    current_month, current_year = today_parts()
    month = int(request.args.get("month", current_month))
    year = int(request.args.get("year", current_year))
    return month, year


def get_budget(user_id: int, month: int, year: int) -> sqlite3.Row | None:
    return query_one(
        """
        SELECT * FROM budgets
        WHERE user_id = ? AND month = ? AND year = ?
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, month, year),
    )


def get_month_spent(user_id: int, month: int, year: int) -> float:
    row = query_one(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE user_id = ?
          AND CAST(strftime('%m', expense_date) AS INTEGER) = ?
          AND CAST(strftime('%Y', expense_date) AS INTEGER) = ?
        """,
        (user_id, month, year),
    )
    return float(row["total"] if row else 0)


def get_budget_snapshot(user_id: int, month: int, year: int) -> dict[str, Any]:
    budget = get_budget(user_id, month, year)
    amount = float(budget["budget_amount"]) if budget else 0.0
    spent = get_month_spent(user_id, month, year)
    remaining = amount - spent
    usage = (spent / amount * 100) if amount > 0 else 0
    status = "Not set"
    if amount <= 0:
        status = "No budget set"
    elif usage >= 100:
        status = "Limit exceeded"
    elif usage >= 80:
        status = "High alert"
    elif usage >= 50:
        status = "On track with warning"
    else:
        status = "Healthy"
    return {
        "budget": budget,
        "budget_amount": amount,
        "spent": spent,
        "remaining": remaining,
        "usage": round(usage, 1),
        "status": status,
        "month": month,
        "year": year,
        "label": month_label(month, year),
    }


def get_alerts_for_month(user_id: int, month: int, year: int) -> list[sqlite3.Row]:
    return query_all(
        """
        SELECT * FROM alerts
        WHERE user_id = ?
          AND CAST(strftime('%m', created_at) AS INTEGER) = ?
          AND CAST(strftime('%Y', created_at) AS INTEGER) = ?
        ORDER BY created_at DESC
        """,
        (user_id, month, year),
    )


def get_latest_alert_status(user_id: int, month: int, year: int) -> dict[str, Any]:
    alerts = get_alerts_for_month(user_id, month, year)
    if not alerts:
        return {
            "headline": "No alerts yet",
            "message": "You are within your budget range for now.",
            "severity": "success",
        }
    latest = alerts[0]
    severity = "info"
    if latest["alert_type"] == "alert_80":
        severity = "warning"
    elif latest["alert_type"] == "alert_100":
        severity = "danger"
    return {
        "headline": ALERT_LEVELS.get(latest["alert_type"], {}).get("label", "Alert"),
        "message": latest["message"],
        "severity": severity,
    }


def insert_alert(user_id: int, alert_type: str, percentage: float) -> None:
    details = ALERT_LEVELS[alert_type]
    get_db().execute(
        """
        INSERT INTO alerts (user_id, alert_type, message, percentage, created_at, is_read)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (user_id, alert_type, details["message"], percentage, now_str()),
    )
    get_db().commit()


def alert_exists_for_month(user_id: int, alert_type: str, month: int, year: int) -> bool:
    existing = query_one(
        """
        SELECT id FROM alerts
        WHERE user_id = ? AND alert_type = ?
          AND CAST(strftime('%m', created_at) AS INTEGER) = ?
          AND CAST(strftime('%Y', created_at) AS INTEGER) = ?
        LIMIT 1
        """,
        (user_id, alert_type, month, year),
    )
    return existing is not None


def evaluate_budget_alerts(user_id: int, month: int, year: int) -> list[str]:
    snapshot = get_budget_snapshot(user_id, month, year)
    budget_amount = snapshot["budget_amount"]
    usage = snapshot["usage"]
    created: list[str] = []
    if budget_amount <= 0:
        return created
    thresholds = []
    if usage >= 100:
        thresholds.append("alert_100")
    elif usage >= 80:
        thresholds.append("alert_80")
    elif usage >= 50:
        thresholds.append("alert_50")
    for alert_type in thresholds:
        if not alert_exists_for_month(user_id, alert_type, month, year):
            insert_alert(user_id, alert_type, usage)
            created.append(alert_type)
    return created


def parent_details(user_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM parents WHERE student_user_id = ?", (user_id,))


def highest_spending_category(user_id: int, month: int, year: int) -> tuple[str, float]:
    row = query_one(
        """
        SELECT category, COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE user_id = ?
          AND CAST(strftime('%m', expense_date) AS INTEGER) = ?
          AND CAST(strftime('%Y', expense_date) AS INTEGER) = ?
        GROUP BY category
        ORDER BY total DESC
        LIMIT 1
        """,
        (user_id, month, year),
    )
    if row:
        return str(row["category"]), float(row["total"])
    return "No spending yet", 0.0


def parent_summary_payload(user_id: int, month: int, year: int) -> dict[str, Any]:
    user = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    parent = parent_details(user_id)
    snapshot = get_budget_snapshot(user_id, month, year)
    top_category, top_amount = highest_spending_category(user_id, month, year)
    exceeded = snapshot["usage"] >= 100
    return {
        "student_name": user["full_name"] if user else "Student",
        "budget_amount": snapshot["budget_amount"],
        "spent": snapshot["spent"],
        "remaining": snapshot["remaining"],
        "usage": snapshot["usage"],
        "highest_category": top_category,
        "highest_category_amount": top_amount,
        "limit_exceeded": exceeded,
        "month_label": snapshot["label"],
        "parent": parent,
        "linked": parent is not None,
    }


def get_recent_expenses(user_id: int, limit: int = 5) -> list[sqlite3.Row]:
    return query_all(
        """
        SELECT * FROM expenses
        WHERE user_id = ?
        ORDER BY expense_date DESC, id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )


def category_totals(user_id: int, month: int, year: int) -> dict[str, float]:
    rows = query_all(
        """
        SELECT category, COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE user_id = ?
          AND CAST(strftime('%m', expense_date) AS INTEGER) = ?
          AND CAST(strftime('%Y', expense_date) AS INTEGER) = ?
        GROUP BY category
        ORDER BY total DESC
        """,
        (user_id, month, year),
    )
    return {str(row["category"]): float(row["total"]) for row in rows}


def daily_totals(user_id: int, month: int, year: int) -> dict[str, float]:
    rows = query_all(
        """
        SELECT expense_date, COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE user_id = ?
          AND CAST(strftime('%m', expense_date) AS INTEGER) = ?
          AND CAST(strftime('%Y', expense_date) AS INTEGER) = ?
        GROUP BY expense_date
        ORDER BY expense_date ASC
        """,
        (user_id, month, year),
    )
    return {str(row["expense_date"]): float(row["total"]) for row in rows}


def monthly_trend(user_id: int) -> list[dict[str, Any]]:
    rows = query_all(
        """
        SELECT CAST(strftime('%m', expense_date) AS INTEGER) AS month,
               CAST(strftime('%Y', expense_date) AS INTEGER) AS year,
               COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE user_id = ?
        GROUP BY year, month
        ORDER BY year DESC, month DESC
        LIMIT 6
        """,
        (user_id,),
    )
    trend = []
    for row in reversed(rows):
        trend.append(
            {
                "label": month_label(int(row["month"]), int(row["year"])),
                "total": float(row["total"]),
            }
        )
    return trend


def build_insights(user_id: int, month: int, year: int) -> list[str]:
    snapshot = get_budget_snapshot(user_id, month, year)
    categories = category_totals(user_id, month, year)
    top_category, top_amount = highest_spending_category(user_id, month, year)
    insights: list[str] = []
    if top_amount > 0 and top_category != "No spending yet":
        insights.append(f"You are spending more on {top_category} this month.")
    if categories.get("Shopping", 0) > max(categories.get("Food", 0), 0) and categories.get("Shopping", 0) > 0:
        insights.append("Try reducing Shopping expenses to protect your savings goal.")
    if snapshot["usage"] < 50 and snapshot["budget_amount"] > 0:
        insights.append("Your spending is under control this month.")
    elif 50 <= snapshot["usage"] < 80:
        insights.append("You are in the caution zone. Track daily spending more closely.")
    elif snapshot["usage"] >= 80:
        insights.append("You are close to your limit. Consider extending only if essential.")
    if not insights:
        insights.append("Start adding expenses to unlock insights and trends.")
    return insights


def filtered_expenses(user_id: int) -> tuple[list[sqlite3.Row], dict[str, str], float]:
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    payment = request.args.get("payment_method", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    query = "SELECT * FROM expenses WHERE user_id = ?"
    params: list[Any] = [user_id]
    if search:
        query += " AND (description LIKE ? OR category LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])
    if category:
        query += " AND category = ?"
        params.append(category)
    if payment:
        query += " AND payment_method = ?"
        params.append(payment)
    if start_date:
        query += " AND expense_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND expense_date <= ?"
        params.append(end_date)
    query += " ORDER BY expense_date DESC, id DESC"
    expenses = query_all(query, tuple(params))
    total = sum(float(item["amount"]) for item in expenses)
    filters = {
        "search": search,
        "category": category,
        "payment_method": payment,
        "start_date": start_date,
        "end_date": end_date,
    }
    return expenses, filters, total


def greeting_for_now() -> str:
    hour = datetime.now().hour
    if hour < 12:
        return "Good Morning"
    if hour < 17:
        return "Good Afternoon"
    return "Good Evening"


def build_pdf(lines: list[str]) -> bytes:
    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    lines_per_page = 38
    pages = [lines[i : i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [["SpendSmart Report"]]
    objects: list[bytes] = []
    page_ids: list[int] = []
    font_object_id = 3 + len(pages) * 2

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())

    for index, page_lines in enumerate(pages):
        page_id = 3 + index * 2
        content_id = page_id + 1
        page_ids.append(page_id)
        page_object = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_object_id} 0 R >> >> /Contents {content_id} 0 R >>"
        objects.append(page_object.encode())
        y_start = 790
        content_parts = ["BT", "/F1 11 Tf", f"50 {y_start} Td", "14 TL"]
        for i, line in enumerate(page_lines):
            if i == 0:
                content_parts.append(f"({escape(line)}) Tj")
            else:
                content_parts.append(f"T* ({escape(line)}) Tj")
        content_parts.append("ET")
        stream = "\n".join(content_parts).encode("latin-1", errors="replace")
        content_object = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        objects.append(content_object)

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    buffer = BytesIO()
    buffer.write(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(buffer.tell())
        buffer.write(f"{index} 0 obj\n".encode())
        buffer.write(obj)
        buffer.write(b"\nendobj\n")
    xref_start = buffer.tell()
    buffer.write(f"xref\n0 {len(objects) + 1}\n".encode())
    buffer.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        buffer.write(f"{offset:010d} 00000 n \n".encode())
    buffer.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF".encode())
    return buffer.getvalue()


def render_page(template: str, **context: Any):
    user = get_current_user()
    month, year = month_year_from_request()
    common = {
        "current_user": user,
        "current_month": month,
        "current_year": year,
        "month_label": month_label(month, year),
        "expense_categories": EXPENSE_CATEGORIES,
        "payment_methods": PAYMENT_METHODS,
        "month_names": MONTH_NAMES,
        "now_year": date.today().year,
    }
    return render_template(template, **common, **context)


@app.before_request
def bootstrap() -> None:
    init_db()


@app.route("/")
def home():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        user_type = request.form.get("user_type", "Normal User").strip()
        parent_name = request.form.get("parent_name", "").strip()
        parent_email = request.form.get("parent_email", "").strip().lower()
        parent_mobile = request.form.get("parent_mobile", "").strip()

        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if not email or "@" not in email:
            errors.append("A valid email is required.")
        if not mobile or len(mobile) < 10:
            errors.append("A valid mobile number is required.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters long.")
        if user_type not in {"Normal User", "Student"}:
            errors.append("Please choose a valid user type.")
        if user_type == "Student" and (not parent_name or not parent_email or not parent_mobile):
            errors.append("Parent name, email, and mobile are required for student accounts.")
        if query_one("SELECT id FROM users WHERE email = ?", (email,)):
            errors.append("This email is already registered.")

        if errors:
            for item in errors:
                flash(item, "error")
            return render_page("register.html", form=request.form)

        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO users
            (full_name, email, mobile, password_hash, user_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (full_name, email, mobile, generate_password_hash(password), user_type, now_str()),
        )
        user_id = cursor.lastrowid
        if user_type == "Student":
            db.execute(
                """
                INSERT INTO parents
                (student_user_id, parent_name, parent_email, parent_mobile, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, parent_name, parent_email, parent_mobile, now_str()),
            )
        db.commit()
        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))

    return render_page("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = query_one("SELECT * FROM users WHERE email = ?", (email,))
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_page("login.html", form=request.form)
        session.clear()
        session["user_id"] = user["id"]
        flash(f"Welcome back, {user['full_name']}!", "success")
        return redirect(url_for("dashboard"))
    return render_page("login.html", form={})


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = query_one("SELECT * FROM users WHERE email = ?", (email,))
        if not user:
            flash("No account was found with that email address.", "error")
            return render_page("forgot_password_request.html", form=request.form)

        sent, otp_code = create_password_reset_otp(user)
        session["password_reset_email"] = email
        if sent:
            flash("Verification OTP sent to your registered email.", "success")
        else:
            flash(
                "OTP generated, but email delivery is not configured or failed. For local demo use, check the Flask server log.",
                "error",
            )
            app.logger.info("Local demo OTP for %s: %s", email, otp_code)
        return redirect(url_for("verify_reset_otp"))

    return render_page("forgot_password_request.html", form={})


@app.route("/forgot-password/verify", methods=["GET", "POST"])
def verify_reset_otp():
    email = request.form.get("email", "").strip().lower() if request.method == "POST" else session.get("password_reset_email", "")
    if not email:
        flash("Start the forgot password process first.", "error")
        return redirect(url_for("forgot_password"))

    user = query_one("SELECT * FROM users WHERE email = ?", (email,))
    if not user:
        session.pop("password_reset_email", None)
        flash("Account not found for password reset.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        otp = request.form.get("otp", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        otp_record = get_latest_valid_otp(user["id"], email)

        if not otp_record:
            flash("OTP expired or not found. Please request a new verification code.", "error")
            return redirect(url_for("forgot_password"))
        if not otp.isdigit() or len(otp) != 6:
            flash("Please enter a valid 6-digit OTP.", "error")
            return render_page("forgot_password_verify.html", email=email, otp_expiry_minutes=OTP_EXPIRY_MINUTES)
        if len(new_password) < 6:
            flash("New password must be at least 6 characters long.", "error")
            return render_page("forgot_password_verify.html", email=email, otp_expiry_minutes=OTP_EXPIRY_MINUTES)
        if new_password != confirm_password:
            flash("New password and confirm password do not match.", "error")
            return render_page("forgot_password_verify.html", email=email, otp_expiry_minutes=OTP_EXPIRY_MINUTES)
        if not check_password_hash(otp_record["otp_hash"], otp):
            flash("Invalid OTP. Please try again.", "error")
            return render_page("forgot_password_verify.html", email=email, otp_expiry_minutes=OTP_EXPIRY_MINUTES)

        db = get_db()
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user["id"]))
        db.execute("UPDATE password_reset_otps SET is_used = 1 WHERE id = ?", (otp_record["id"],))
        db.commit()
        session.pop("password_reset_email", None)
        flash("Password updated successfully. Please log in with your new password.", "success")
        return redirect(url_for("login"))

    return render_page("forgot_password_verify.html", email=email, otp_expiry_minutes=OTP_EXPIRY_MINUTES)


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    month, year = today_parts()
    snapshot = get_budget_snapshot(user["id"], month, year)
    recent_expenses = get_recent_expenses(user["id"], 6)
    category_map = category_totals(user["id"], month, year)
    alert_center = get_alerts_for_month(user["id"], month, year)[:5]
    chart_data = {
        "usage": snapshot["usage"],
        "categoryLabels": list(category_map.keys()) or ["No data"],
        "categoryValues": list(category_map.values()) or [1],
    }
    return render_page(
        "dashboard.html",
        page_title="Dashboard",
        active_page="dashboard",
        greeting=f"{greeting_for_now()}, {user['full_name'].split()[0]}!",
        snapshot=snapshot,
        recent_expenses=recent_expenses,
        alert_status=get_latest_alert_status(user["id"], month, year),
        alert_center=alert_center,
        chart_data=chart_data,
    )


@app.route("/budget")
@login_required
def budget_page():
    user = get_current_user()
    month, year = month_year_from_request()
    snapshot = get_budget_snapshot(user["id"], month, year)
    extensions = query_all(
        "SELECT * FROM budget_extensions WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    )
    return render_page(
        "budget.html",
        page_title="Budget",
        active_page="budget",
        snapshot=snapshot,
        extensions=extensions,
    )


@app.route("/budget/set", methods=["POST"])
@login_required
def set_budget():
    user = get_current_user()
    month = int(request.form.get("month", 0))
    year = int(request.form.get("year", 0))
    amount = float(request.form.get("budget_amount", 0) or 0)
    reason = request.form.get("reason", "").strip()

    if month < 1 or month > 12 or year < 2000 or amount <= 0:
        flash("Please enter a valid budget, month, and year.", "error")
        return redirect(url_for("budget_page", month=month or None, year=year or None))

    db = get_db()
    existing = get_budget(user["id"], month, year)
    timestamp = now_str()
    if existing:
        db.execute(
            """
            UPDATE budgets
            SET budget_amount = ?, reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (amount, reason, timestamp, existing["id"]),
        )
        flash("Monthly budget updated successfully.", "success")
    else:
        db.execute(
            """
            INSERT INTO budgets
            (user_id, month, year, budget_amount, reason, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], month, year, amount, reason, timestamp, timestamp),
        )
        flash("Monthly budget created successfully.", "success")
    db.commit()
    return redirect(url_for("budget_page", month=month, year=year))


@app.route("/budget/update", methods=["POST"])
@login_required
def update_budget():
    return set_budget()


@app.route("/budget/extend", methods=["POST"])
@login_required
def extend_budget():
    user = get_current_user()
    month = int(request.form.get("month", 0))
    year = int(request.form.get("year", 0))
    new_budget = float(request.form.get("new_budget_amount", 0) or 0)
    reason = request.form.get("reason", "").strip()
    budget = get_budget(user["id"], month, year)
    if not budget:
        flash("Set a monthly budget before extending it.", "error")
        return redirect(url_for("budget_page", month=month, year=year))
    old_budget = float(budget["budget_amount"])
    if new_budget <= old_budget:
        flash("Extended budget must be higher than the current budget.", "error")
        return redirect(url_for("budget_page", month=month, year=year))
    db = get_db()
    db.execute(
        "UPDATE budgets SET budget_amount = ?, updated_at = ? WHERE id = ?",
        (new_budget, now_str(), budget["id"]),
    )
    db.execute(
        """
        INSERT INTO budget_extensions (user_id, old_budget, new_budget, reason, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user["id"], old_budget, new_budget, reason, now_str()),
    )
    db.commit()
    evaluate_budget_alerts(user["id"], month, year)
    flash("Budget extended successfully.", "success")
    return redirect(url_for("budget_page", month=month, year=year))


@app.route("/expenses/add", methods=["GET", "POST"])
@login_required
def add_expense():
    user = get_current_user()
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0) or 0)
        except ValueError:
            amount = 0
        category = request.form.get("category", "").strip()
        payment_method = request.form.get("payment_method", "").strip()
        description = request.form.get("description", "").strip()
        expense_date = request.form.get("expense_date", "").strip()

        errors = []
        if amount <= 0:
            errors.append("Amount must be greater than zero.")
        if category not in EXPENSE_CATEGORIES:
            errors.append("Please select a valid expense category.")
        if payment_method not in PAYMENT_METHODS:
            errors.append("Please select a valid payment method.")
        try:
            expense_dt = datetime.strptime(expense_date, "%Y-%m-%d")
        except ValueError:
            expense_dt = None
            errors.append("Please choose a valid expense date.")

        if errors:
            for item in errors:
                flash(item, "error")
            return render_page("expense_form.html", page_title="Add Expense", active_page="add-expense", form=request.form, edit_mode=False)

        db = get_db()
        db.execute(
            """
            INSERT INTO expenses
            (user_id, amount, category, payment_method, description, expense_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], amount, category, payment_method, description, expense_date, now_str()),
        )
        db.commit()
        evaluate_budget_alerts(user["id"], expense_dt.month, expense_dt.year)
        flash("Expense added successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_page(
        "expense_form.html",
        page_title="Add Expense",
        active_page="add-expense",
        form={"expense_date": date.today().isoformat()},
        edit_mode=False,
    )


@app.route("/expenses/history")
@login_required
def expense_history():
    user = get_current_user()
    expenses, filters, filtered_total = filtered_expenses(user["id"])
    return render_page(
        "expense_history.html",
        page_title="Expense History",
        active_page="expense-history",
        expenses=expenses,
        filters=filters,
        filtered_total=filtered_total,
    )


@app.route("/expenses/edit/<int:expense_id>", methods=["GET", "POST"])
@login_required
def edit_expense(expense_id: int):
    user = get_current_user()
    expense = query_one("SELECT * FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user["id"]))
    if not expense:
        flash("Expense not found.", "error")
        return redirect(url_for("expense_history"))

    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0) or 0)
        except ValueError:
            amount = 0
        category = request.form.get("category", "").strip()
        payment_method = request.form.get("payment_method", "").strip()
        description = request.form.get("description", "").strip()
        expense_date = request.form.get("expense_date", "").strip()

        if amount <= 0 or category not in EXPENSE_CATEGORIES or payment_method not in PAYMENT_METHODS:
            flash("Please complete all expense fields correctly.", "error")
            return render_page("expense_form.html", page_title="Edit Expense", active_page="expense-history", form=request.form, edit_mode=True, expense_id=expense_id)

        db = get_db()
        db.execute(
            """
            UPDATE expenses
            SET amount = ?, category = ?, payment_method = ?, description = ?, expense_date = ?
            WHERE id = ? AND user_id = ?
            """,
            (amount, category, payment_method, description, expense_date, expense_id, user["id"]),
        )
        db.commit()
        try:
            expense_dt = datetime.strptime(expense_date, "%Y-%m-%d")
            evaluate_budget_alerts(user["id"], expense_dt.month, expense_dt.year)
        except ValueError:
            pass
        flash("Expense updated successfully.", "success")
        return redirect(url_for("expense_history"))

    return render_page(
        "expense_form.html",
        page_title="Edit Expense",
        active_page="expense-history",
        form=expense,
        edit_mode=True,
        expense_id=expense_id,
    )


@app.route("/expenses/delete/<int:expense_id>", methods=["POST"])
@login_required
def delete_expense(expense_id: int):
    user = get_current_user()
    get_db().execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user["id"]))
    get_db().commit()
    flash("Expense deleted successfully.", "success")
    return redirect(url_for("expense_history"))


@app.route("/analytics")
@login_required
def analytics():
    user = get_current_user()
    month, year = month_year_from_request()
    categories = category_totals(user["id"], month, year)
    daily = daily_totals(user["id"], month, year)
    trend = monthly_trend(user["id"])
    snapshot = get_budget_snapshot(user["id"], month, year)
    top_category, top_amount = highest_spending_category(user["id"], month, year)
    expense_days = max(len(daily), 1)
    average_daily = snapshot["spent"] / expense_days if snapshot["spent"] else 0
    chart_data = {
        "categoryLabels": list(categories.keys()) or ["No data"],
        "categoryValues": list(categories.values()) or [1],
        "dailyLabels": list(daily.keys()) or ["No data"],
        "dailyValues": list(daily.values()) or [0],
        "monthlyLabels": [item["label"] for item in trend] or [snapshot["label"]],
        "monthlyValues": [item["total"] for item in trend] or [snapshot["spent"]],
    }
    return render_page(
        "analytics.html",
        page_title="Analytics",
        active_page="analytics",
        snapshot=snapshot,
        highest_category=top_category,
        highest_category_amount=top_amount,
        average_daily=average_daily,
        insights=build_insights(user["id"], month, year),
        chart_data=chart_data,
    )


@app.route("/alerts")
@login_required
def alerts():
    user = get_current_user()
    month, year = month_year_from_request()
    alerts_list = get_alerts_for_month(user["id"], month, year)
    return render_page(
        "alerts.html",
        page_title="Alerts",
        active_page="alerts",
        alerts_list=alerts_list,
    )


@app.route("/alerts/mark-read", methods=["POST"])
@login_required
def mark_alerts_read():
    user = get_current_user()
    alert_id = request.form.get("alert_id", "").strip()
    db = get_db()
    if alert_id:
        db.execute("UPDATE alerts SET is_read = 1 WHERE id = ? AND user_id = ?", (alert_id, user["id"]))
    else:
        db.execute("UPDATE alerts SET is_read = 1 WHERE user_id = ?", (user["id"],))
    db.commit()
    flash("Alerts updated.", "success")
    return redirect(url_for("alerts"))


@app.route("/reports")
@login_required
def reports():
    user = get_current_user()
    month, year = month_year_from_request()
    snapshot = get_budget_snapshot(user["id"], month, year)
    categories = category_totals(user["id"], month, year)
    alerts_list = get_alerts_for_month(user["id"], month, year)
    expenses = query_all(
        """
        SELECT * FROM expenses
        WHERE user_id = ?
          AND CAST(strftime('%m', expense_date) AS INTEGER) = ?
          AND CAST(strftime('%Y', expense_date) AS INTEGER) = ?
        ORDER BY expense_date DESC, id DESC
        """,
        (user["id"], month, year),
    )
    parent_summary = parent_summary_payload(user["id"], month, year) if user["user_type"] == "Student" else None
    return render_page(
        "reports.html",
        page_title="Reports",
        active_page="reports",
        snapshot=snapshot,
        categories=categories,
        alerts_list=alerts_list,
        expenses=expenses,
        parent_summary=parent_summary,
    )


@app.route("/reports/download-pdf")
@login_required
def download_report_pdf():
    user = get_current_user()
    month, year = month_year_from_request()
    snapshot = get_budget_snapshot(user["id"], month, year)
    categories = category_totals(user["id"], month, year)
    alerts_list = get_alerts_for_month(user["id"], month, year)
    expenses = query_all(
        """
        SELECT * FROM expenses
        WHERE user_id = ?
          AND CAST(strftime('%m', expense_date) AS INTEGER) = ?
          AND CAST(strftime('%Y', expense_date) AS INTEGER) = ?
        ORDER BY expense_date DESC, id DESC
        """,
        (user["id"], month, year),
    )
    lines = [
        "SpendSmart Monthly Report",
        f"User: {user['full_name']}",
        f"Period: {snapshot['label']}",
        f"Monthly Budget: {rupee(snapshot['budget_amount'])}",
        f"Total Spent: {rupee(snapshot['spent'])}",
        f"Remaining Budget: {rupee(snapshot['remaining'])}",
        f"Usage Percentage: {snapshot['usage']}%",
        "",
        "Category Breakdown:",
    ]
    if categories:
        for category, total in categories.items():
            lines.append(f"- {category}: {rupee(total)}")
    else:
        lines.append("- No category spending yet.")
    lines.extend(["", "Alert History:"])
    if alerts_list:
        for alert in alerts_list:
            lines.append(f"- {alert['created_at']}: {alert['message']}")
    else:
        lines.append("- No alerts this month.")
    lines.extend(["", "Expenses:"])
    if expenses:
        for expense in expenses:
            lines.append(
                f"- {expense['expense_date']} | {expense['category']} | {rupee(expense['amount'])} | {expense['payment_method']} | {expense['description'] or 'No description'}"
            )
    else:
        lines.append("- No expenses recorded this month.")
    if user["user_type"] == "Student":
        parent_summary = parent_summary_payload(user["id"], month, year)
        lines.extend(
            [
                "",
                "Parent Summary:",
                f"- Parent linked: {'Yes' if parent_summary['linked'] else 'No'}",
                f"- Highest spending category: {parent_summary['highest_category']}",
                f"- Limit exceeded: {'Yes' if parent_summary['limit_exceeded'] else 'No'}",
            ]
        )
    pdf_bytes = build_pdf(lines)
    filename = f"SpendSmart-Report-{year}-{month:02d}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = get_current_user()
    if request.method == "POST":
        action = request.form.get("action")
        db = get_db()
        if action == "profile":
            full_name = request.form.get("full_name", "").strip()
            mobile = request.form.get("mobile", "").strip()
            notifications_enabled = 1 if request.form.get("notifications_enabled") == "on" else 0
            parent_summary_enabled = 1 if request.form.get("parent_summary_enabled") == "on" else 0
            if not full_name or not mobile:
                flash("Name and mobile are required.", "error")
            else:
                db.execute(
                    """
                    UPDATE users
                    SET full_name = ?, mobile = ?, notifications_enabled = ?, parent_summary_enabled = ?
                    WHERE id = ?
                    """,
                    (full_name, mobile, notifications_enabled, parent_summary_enabled, user["id"]),
                )
                db.commit()
                flash("Profile settings updated.", "success")
        elif action == "password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not check_password_hash(user["password_hash"], current_password):
                flash("Current password is incorrect.", "error")
            elif len(new_password) < 6:
                flash("New password must be at least 6 characters long.", "error")
            elif new_password != confirm_password:
                flash("New password and confirm password do not match.", "error")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_password), user["id"]),
                )
                db.commit()
                flash("Password updated successfully.", "success")
        return redirect(url_for("settings"))

    parent = parent_details(user["id"]) if user["user_type"] == "Student" else None
    month, year = today_parts()
    snapshot = get_budget_snapshot(user["id"], month, year)
    return render_page(
        "settings.html",
        page_title="Settings",
        active_page="settings",
        parent=parent,
        snapshot=snapshot,
    )


@app.route("/parent-link", methods=["GET", "POST"])
@login_required
def parent_link():
    user = get_current_user()
    if user["user_type"] != "Student":
        flash("Parent monitoring is available only for student accounts.", "error")
        return redirect(url_for("dashboard"))
    existing = parent_details(user["id"])
    if request.method == "POST":
        parent_name = request.form.get("parent_name", "").strip()
        parent_email = request.form.get("parent_email", "").strip().lower()
        parent_mobile = request.form.get("parent_mobile", "").strip()
        if not parent_name or not parent_email or not parent_mobile:
            flash("Please complete all parent details.", "error")
        else:
            db = get_db()
            if existing:
                db.execute(
                    """
                    UPDATE parents
                    SET parent_name = ?, parent_email = ?, parent_mobile = ?
                    WHERE student_user_id = ?
                    """,
                    (parent_name, parent_email, parent_mobile, user["id"]),
                )
                flash("Parent link updated successfully.", "success")
            else:
                db.execute(
                    """
                    INSERT INTO parents
                    (student_user_id, parent_name, parent_email, parent_mobile, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user["id"], parent_name, parent_email, parent_mobile, now_str()),
                )
                flash("Parent link created successfully.", "success")
            db.commit()
        return redirect(url_for("parent_link"))

    month, year = today_parts()
    return render_page(
        "parent_link.html",
        page_title="Parent Monitoring",
        active_page="parent-monitoring",
        parent=existing,
        summary=parent_summary_payload(user["id"], month, year),
    )


@app.route("/parent-summary")
@login_required
def parent_summary():
    user = get_current_user()
    if user["user_type"] != "Student":
        flash("Parent summary is available only for student accounts.", "error")
        return redirect(url_for("dashboard"))
    month, year = month_year_from_request()
    return render_page(
        "parent_summary.html",
        page_title="Parent Summary",
        active_page="parent-monitoring",
        summary=parent_summary_payload(user["id"], month, year),
    )


@app.context_processor
def inject_sidebar_links() -> dict[str, Any]:
    return {
        "sidebar_links": [
            {"endpoint": "dashboard", "key": "dashboard", "label": "Dashboard", "icon": "layout-dashboard"},
            {"endpoint": "add_expense", "key": "add-expense", "label": "Add Expense", "icon": "plus-circle"},
            {"endpoint": "budget_page", "key": "budget", "label": "Budget", "icon": "wallet"},
            {"endpoint": "expense_history", "key": "expense-history", "label": "Expense History", "icon": "receipt-text"},
            {"endpoint": "analytics", "key": "analytics", "label": "Analytics", "icon": "chart-column"},
            {"endpoint": "alerts", "key": "alerts", "label": "Alerts", "icon": "bell"},
            {"endpoint": "reports", "key": "reports", "label": "Reports", "icon": "file-text"},
            {"endpoint": "parent_link", "key": "parent-monitoring", "label": "Parent Monitoring", "icon": "users"},
            {"endpoint": "settings", "key": "settings", "label": "Settings", "icon": "settings"},
        ]
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)
