# Dorm Management Portal (FastAPI)

Mobile-friendly dorm parent portal to:
- Add students (name + email)
- Write daily dorm performance reports
- Link images (URL) or upload images
- Generate and send reports from the interface

## Quickstart

1) Create venv + install deps

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2) (Optional) Configure SMTP (for sending emails)

Create an `.env` file in the project root:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_account@gmail.com
SMTP_PASSWORD=your_app_password
SMTP_FROM=dorm@example.com
SMTP_USE_TLS=true
```

If SMTP is not configured, the app will show a clear error when you try to send.

2b) Configure Google Sign-In (no Firebase)

Create OAuth credentials in Google Cloud Console and set these in `.env`:

```env
SESSION_SECRET=change-me-please
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/auth/google/callback
```

Also add this Authorized redirect URI in Google:
- `http://127.0.0.1:8000/auth/google/callback`

If you run the server on port 8001, use:
- `http://127.0.0.1:8001/auth/google/callback`

## Dev mode (skip Google keys)

If you just want to try the UI locally without setting up Google OAuth, you can enable dev login:

```env
DEV_MODE=true
DEV_USER_EMAIL=dev@example.com
DEV_USER_NAME=Dev User
```

This adds a "Dev login" button on `/login`. Do not enable this in production.

3) Run

```powershell
uvicorn app.main:app --reload --reload-dir app --reload-dir static
```

Open: http://127.0.0.1:8000 (or http://127.0.0.1:8001 if you run with `--port 8001`)

## Notes
- Database: SQLite file `dorm.db` in the project root.
- Uploads: stored in `static/uploads/` and served at `/uploads/...`.
