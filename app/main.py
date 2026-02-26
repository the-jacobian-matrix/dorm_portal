from __future__ import annotations

from datetime import date
import hashlib
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session, delete, select

from app.auth import configure_oauth, google_callback, google_login, is_auth_configured, require_user_or_redirect
from app.config import settings
from app.db import get_session, init_db
from app.emailer import EmailNotConfiguredError, ensure_email_configured, send_email
from app.models import DailyReport, Student


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"

# StaticFiles requires directories to exist at mount time.
STATIC_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

app = FastAPI(title=settings.app_name)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=not settings.dev_mode,
)

templates = Jinja2Templates(directory=str((Path(__file__).parent / "templates").resolve()))

# Serve static + uploads
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


@app.on_event("startup")
def _startup() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    configure_oauth()


@app.get("/", response_class=HTMLResponse)
def home() -> RedirectResponse:
    return RedirectResponse(url="/students", status_code=303)


def _redirect_with_message(url: str, *, success: str | None = None, error: str | None = None) -> RedirectResponse:
    params: list[str] = []
    if success:
        params.append(f"success={quote_plus(success)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    qs = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(url=f"{url}{qs}", status_code=303)


def _flash_from_query(request: Request) -> dict:
    qp = request.query_params
    flash_success = qp.get("success")
    flash_error = qp.get("error")
    out: dict = {}
    if flash_success:
        out["flash_success"] = flash_success
    if flash_error:
        out["flash_error"] = flash_error
    return out


def _base_template_context(request: Request) -> dict:
    user = request.session.get("user")
    return {
        "current_user": user,
        "google_configured": is_auth_configured(),
        "dev_mode": settings.dev_mode,
        **_flash_from_query(request),
    }


def _require_login(request: Request) -> RedirectResponse | None:
    user_or_redirect = require_user_or_redirect(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    return None


def _normalize_name(name: str) -> str:
    return " ".join(name.strip().split())


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid report date") from exc


def _safe_rating(rating: Optional[int]) -> Optional[int]:
    if rating is None:
        return None
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=422, detail="Rating must be between 1 and 5")
    return rating


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_setup_complete() -> bool:
    return bool(settings.admin_token_hash and settings.session_secret != "dev-change-me")


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    return templates.TemplateResponse(
        "setup.html",
        {
            "request": request,
            "setup_complete": _is_setup_complete(),
            "generated_env": None,
            **_base_template_context(request),
        },
    )


@app.post("/setup", response_class=HTMLResponse)
def setup_submit(request: Request, admin_token: str = Form(...)):
    token = admin_token.strip()
    if not token:
        return templates.TemplateResponse(
            "setup.html",
            {
                "request": request,
                "setup_complete": _is_setup_complete(),
                "generated_env": None,
                "flash_error": "Admin token is required",
                **_base_template_context(request),
            },
            status_code=422,
        )

    baseline_hash = _hash_token("whoisthere")
    token_hash = _hash_token(token)
    session_secret = secrets.token_urlsafe(48)

    generated_env = {
        "ADMIN_TOKEN_HASH": baseline_hash if token == "whoisthere" else token_hash,
        "SESSION_SECRET": session_secret,
        "DEV_MODE": "false",
    }

    return templates.TemplateResponse(
        "setup.html",
        {
            "request": request,
            "setup_complete": _is_setup_complete(),
            "generated_env": generated_env,
            "flash_success": "Environment values generated. Copy them into Vercel project settings.",
            **_base_template_context(request),
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "setup_complete": _is_setup_complete(), **_base_template_context(request)},
    )


@app.post("/login/dev")
def login_dev(request: Request) -> RedirectResponse:
    if not settings.dev_mode:
        return _redirect_with_message("/login", error="Dev mode is disabled")

    request.session["user"] = {
        "id": None,
        "email": settings.dev_user_email,
        "name": settings.dev_user_name,
        "picture_url": None,
        "dev": True,
    }
    return _redirect_with_message("/students", success="Dev login enabled")


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return _redirect_with_message("/login", success="Signed out")


@app.get("/auth/google")
async def auth_google(request: Request):
    return await google_login(request)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, session: Session = Depends(get_session)):
    return await google_callback(request, session)


@app.get("/students", response_class=HTMLResponse)
def students_page(request: Request, session: Session = Depends(get_session)):
    redirect = _require_login(request)
    if redirect:
        return redirect

    q = (request.query_params.get("q") or "").strip()
    stmt = select(Student)
    if q:
        stmt = stmt.where((Student.name.contains(q)) | (Student.email.contains(q)))
    students = session.exec(stmt.order_by(Student.created_at.desc())).all()
    return templates.TemplateResponse(
        "students.html",
        {
            "request": request,
            "students": students,
            "q": q,
            "student_count": len(students),
            **_base_template_context(request),
        },
    )


@app.post("/students")
def create_student(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect = _require_login(request)
    if redirect:
        return redirect

    normalized_name = _normalize_name(name)
    normalized_email = _normalize_email(email)

    if not normalized_name:
        return _redirect_with_message("/students", error="Student name is required")
    if "@" not in normalized_email:
        return _redirect_with_message("/students", error="A valid email is required")

    existing = session.exec(select(Student).where(Student.email == normalized_email)).first()
    if existing:
        return _redirect_with_message(f"/students/{existing.id}", error="A student with this email already exists")

    student = Student(name=normalized_name, email=normalized_email)
    session.add(student)
    session.commit()
    session.refresh(student)
    return _redirect_with_message(f"/students/{student.id}", success="Student created")


@app.get("/students/{student_id}/edit", response_class=HTMLResponse)
def student_edit_page(
    student_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        return _redirect_with_message("/students", error="Student not found")
    return templates.TemplateResponse(
        "student_edit.html",
        {"request": request, "student": student, **_base_template_context(request)},
    )


@app.post("/students/{student_id}/edit")
def student_edit_submit(
    student_id: int,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        return _redirect_with_message("/students", error="Student not found")

    normalized_name = _normalize_name(name)
    normalized_email = _normalize_email(email)

    if not normalized_name:
        return _redirect_with_message(f"/students/{student_id}/edit", error="Student name is required")
    if "@" not in normalized_email:
        return _redirect_with_message(f"/students/{student_id}/edit", error="A valid email is required")

    duplicate = session.exec(select(Student).where(Student.email == normalized_email)).first()
    if duplicate and duplicate.id != student_id:
        return _redirect_with_message(f"/students/{student_id}/edit", error="Another student already uses this email")

    student.name = normalized_name
    student.email = normalized_email
    session.add(student)
    session.commit()
    return _redirect_with_message(f"/students/{student_id}", success="Student updated")


def _try_delete_uploaded_file(image_path: str | None) -> None:
    if not image_path:
        return
    if not image_path.startswith("/uploads/"):
        return
    filename = image_path.removeprefix("/uploads/")
    disk_path = UPLOADS_DIR / filename
    try:
        if disk_path.exists() and disk_path.is_file():
            disk_path.unlink()
    except OSError:
        pass


@app.post("/students/{student_id}/delete")
def student_delete(
    student_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        return _redirect_with_message("/students", error="Student not found")

    reports = session.exec(select(DailyReport).where(DailyReport.student_id == student_id)).all()
    for r in reports:
        _try_delete_uploaded_file(r.image_path)

    session.exec(delete(DailyReport).where(DailyReport.student_id == student_id))
    session.delete(student)
    session.commit()

    return _redirect_with_message("/students", success="Student deleted")


@app.get("/students/{student_id}", response_class=HTMLResponse)
def student_detail(
    student_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    reports = session.exec(
        select(DailyReport)
        .where(DailyReport.student_id == student_id)
        .order_by(DailyReport.report_date.desc(), DailyReport.created_at.desc())
        .limit(10)
    ).all()

    return templates.TemplateResponse(
        "student_detail.html",
        {
            "request": request,
            "student": student,
            "reports": reports,
            "today": date.today().isoformat(),
            **_base_template_context(request),
        },
    )


@app.get("/students/{student_id}/reports", response_class=HTMLResponse)
def report_list(
    student_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    reports = session.exec(
        select(DailyReport)
        .where(DailyReport.student_id == student_id)
        .order_by(DailyReport.report_date.desc(), DailyReport.created_at.desc())
    ).all()

    return templates.TemplateResponse(
        "report_list.html",
        {"request": request, "student": student, "reports": reports, **_base_template_context(request)},
    )


@app.get("/students/{student_id}/reports/new", response_class=HTMLResponse)
def report_new(
    student_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    return templates.TemplateResponse(
        "report_new.html",
        {"request": request, "student": student, "today": date.today().isoformat(), **_base_template_context(request)},
    )


def _save_upload(image_file: UploadFile) -> str:
    original_name = (image_file.filename or "upload").replace("/", "_").replace("\\", "_")
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Unsupported image type")

    payload = image_file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image is too large (max 5MB)")

    out_name = f"{uuid4().hex}_{original_name}"
    out_path = UPLOADS_DIR / out_name
    with out_path.open("wb") as f:
        f.write(payload)

    return f"/uploads/{out_name}"


@app.post("/students/{student_id}/reports")
def create_report(
    student_id: int,
    request: Request,
    report_date: str = Form(...),
    notes: str = Form(...),
    rating: Optional[int] = Form(None),
    image_url: Optional[str] = Form(None),
    image_file: Optional[UploadFile] = File(None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    parsed_date = _parse_iso_date(report_date)
    clean_notes = notes.strip()
    if not clean_notes:
        return _redirect_with_message(f"/students/{student_id}/reports/new", error="Notes are required")

    image_path: str | None = None
    if image_file and image_file.filename:
        image_path = _save_upload(image_file)

    report = DailyReport(
        student_id=student_id,
        report_date=parsed_date,
        notes=clean_notes,
        rating=_safe_rating(rating),
        image_url=(image_url.strip() if image_url and image_url.strip() else None),
        image_path=image_path,
    )
    session.add(report)
    session.commit()

    return _redirect_with_message(f"/students/{student_id}", success="Report saved")


@app.get("/students/{student_id}/reports/{report_id}/edit", response_class=HTMLResponse)
def report_edit_page(
    student_id: int,
    report_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        return _redirect_with_message("/students", error="Student not found")

    report = session.get(DailyReport, report_id)
    if not report or report.student_id != student_id:
        return _redirect_with_message(f"/students/{student_id}/reports", error="Report not found")

    return templates.TemplateResponse(
        "report_edit.html",
        {"request": request, "student": student, "report": report, **_base_template_context(request)},
    )


@app.post("/students/{student_id}/reports/{report_id}/edit")
def report_edit_submit(
    student_id: int,
    report_id: int,
    request: Request,
    report_date: str = Form(...),
    notes: str = Form(...),
    rating: Optional[int] = Form(None),
    image_url: Optional[str] = Form(None),
    clear_image_url: Optional[str] = Form(None),
    clear_image_upload: Optional[str] = Form(None),
    image_file: Optional[UploadFile] = File(None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        return _redirect_with_message("/students", error="Student not found")

    report = session.get(DailyReport, report_id)
    if not report or report.student_id != student_id:
        return _redirect_with_message(f"/students/{student_id}/reports", error="Report not found")

    clean_notes = notes.strip()
    if not clean_notes:
        return _redirect_with_message(f"/students/{student_id}/reports/{report_id}/edit", error="Notes are required")

    report.report_date = _parse_iso_date(report_date)
    report.notes = clean_notes
    report.rating = _safe_rating(rating)

    if clear_image_url:
        report.image_url = None
    elif image_url is not None and image_url.strip() != "":
        report.image_url = image_url.strip()

    if clear_image_upload:
        _try_delete_uploaded_file(report.image_path)
        report.image_path = None

    if image_file and image_file.filename:
        _try_delete_uploaded_file(report.image_path)
        report.image_path = _save_upload(image_file)

    session.add(report)
    session.commit()
    return _redirect_with_message(f"/students/{student_id}/reports", success="Report updated")


@app.post("/students/{student_id}/reports/{report_id}/delete")
def report_delete(
    student_id: int,
    report_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect = _require_login(request)
    if redirect:
        return redirect
    report = session.get(DailyReport, report_id)
    if not report or report.student_id != student_id:
        return _redirect_with_message(f"/students/{student_id}/reports", error="Report not found")

    _try_delete_uploaded_file(report.image_path)
    session.delete(report)
    session.commit()
    return _redirect_with_message(f"/students/{student_id}/reports", success="Report deleted")


@app.post("/students/{student_id}/send", response_class=HTMLResponse)
def send_report_command(
    student_id: int,
    request: Request,
    background: BackgroundTasks,
    report_date: str = Form(...),
    session: Session = Depends(get_session),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    student = session.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    parsed_date = _parse_iso_date(report_date)

    report = session.exec(
        select(DailyReport)
        .where(DailyReport.student_id == student_id)
        .where(DailyReport.report_date == parsed_date)
        .order_by(DailyReport.created_at.desc())
    ).first()

    if not report:
        return templates.TemplateResponse(
            "student_detail.html",
            {
                "request": request,
                "student": student,
                "reports": session.exec(
                    select(DailyReport)
                    .where(DailyReport.student_id == student_id)
                    .order_by(DailyReport.report_date.desc(), DailyReport.created_at.desc())
                    .limit(10)
                ).all(),
                "today": date.today().isoformat(),
                "flash_error": f"No report found for {parsed_date}.",
                **_base_template_context(request),
            },
            status_code=404,
        )

    subject = f"Dorm Report - {student.name} - {report.report_date}"
    public_base_url = str(request.base_url).rstrip("/")

    html_body = templates.get_template("email_report.html").render(
        student=student, report=report, public_base_url=public_base_url
    )
    text_body = (
        f"Dorm Daily Report\n\n"
        f"Student: {student.name}\n"
        f"Date: {report.report_date}\n"
        + (f"Rating: {report.rating}/5\n" if report.rating else "")
        + f"\nNotes:\n{report.notes}\n"
        + (f"\nImage link: {report.image_url}\n" if report.image_url else "")
        + (f"\nUploaded image: {public_base_url}{report.image_path}\n" if report.image_path else "")
    )

    try:
        ensure_email_configured()
        background.add_task(
            send_email,
            to_email=student.email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
        flash_success = f"Queued email to {student.email} for {parsed_date}."
        flash_error = None
    except EmailNotConfiguredError as e:
        flash_success = None
        flash_error = str(e)

    recent = session.exec(
        select(DailyReport)
        .where(DailyReport.student_id == student_id)
        .order_by(DailyReport.report_date.desc(), DailyReport.created_at.desc())
        .limit(10)
    ).all()

    return templates.TemplateResponse(
        "student_detail.html",
        {
            "request": request,
            "student": student,
            "reports": recent,
            "today": date.today().isoformat(),
            "flash_success": flash_success,
            "flash_error": flash_error,
            **_base_template_context(request),
        },
    )
