# SpendSmart

SpendSmart is a complete Flask + SQLite smart expense management system with:

- user registration and login
- forgot password with OTP verification
- session-based authentication
- monthly budget setup and extension history
- expense CRUD with filters
- alert thresholds at 50%, 80%, and 100%
- analytics dashboards using Chart.js
- parent monitoring for student accounts
- monthly PDF reports

## Run Locally

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Start the app:

```bash
python app.py
```

4. Open `http://127.0.0.1:5000`

SQLite data is stored at `data/spendsmart.db`.

If your workspace is on OneDrive or another sync folder that blocks SQLite journal writes, you can run with a custom DB path:

```bash
set SPENDSMART_DB_PATH=C:\path\to\spendsmart.db
python app.py
```

## Email OTP Setup

Forgot password uses an email OTP sent to the registered email address.

Set these environment variables before starting the app:

```bash
set SPENDSMART_SMTP_HOST=smtp.gmail.com
set SPENDSMART_SMTP_PORT=587
set SPENDSMART_SMTP_USER=your-email@example.com
set SPENDSMART_SMTP_PASSWORD=your-app-password
set SPENDSMART_FROM_EMAIL=your-email@example.com
```

If SMTP is not configured or delivery fails, the OTP is still generated and written to the Flask server log for local development/demo use.

## Fixed Demo OTP

For college demos, you can force a fixed OTP like `123456`:

```bash
set SPENDSMART_DEMO_OTP=123456
python app.py
```

When this variable is set, forgot-password will always generate that exact OTP.
