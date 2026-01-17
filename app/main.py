from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional
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

app = FastAPI(title="Dorm Management Portal")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=False,
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


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, **_base_template_context(request)},
    )


@app.post("/login/dev")
def login_dev(request: Request) -> RedirectResponse:
    if not settings.dev_mode:
        return RedirectResponse(url="/login?error=Dev%20mode%20is%20disabled", status_code=303)

    request.session["user"] = {
        "id": None,
        "email": settings.dev_user_email,
        "name": settings.dev_user_name,
        "picture_url": None,
        "dev": True,
    }
    return RedirectResponse(url="/students?success=Dev%20login%20enabled", status_code=303)


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login?success=Signed%20out", status_code=303)


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
        {"request": request, "students": students, "q": q, **_base_template_context(request)},
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
    student = Student(name=name.strip(), email=email.strip())
    session.add(student)
    session.commit()
    session.refresh(student)
    return RedirectResponse(url=f"/students/{student.id}", status_code=303)


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
        return RedirectResponse(url="/students?error=Student%20not%20found", status_code=303)
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
        return RedirectResponse(url="/students?error=Student%20not%20found", status_code=303)
    student.name = name.strip()
    student.email = email.strip()
    session.add(student)
    session.commit()
    return RedirectResponse(url=f"/students/{student_id}?success=Student%20updated", status_code=303)


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
        # Best-effort cleanup only
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
        return RedirectResponse(url="/students?error=Student%20not%20found", status_code=303)

    # Cleanup uploaded images for this student's reports
    reports = session.exec(select(DailyReport).where(DailyReport.student_id == student_id)).all()
    for r in reports:
        _try_delete_uploaded_file(r.image_path)

    # Delete reports then student
    session.exec(delete(DailyReport).where(DailyReport.student_id == student_id))
    session.delete(student)
    session.commit()

    return RedirectResponse(url="/students?success=Student%20deleted", status_code=303)


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
    out_name = f"{uuid4().hex}_{original_name}"
    out_path = UPLOADS_DIR / out_name

    with out_path.open("wb") as f:
        f.write(image_file.file.read())

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

    parsed_date = date.fromisoformat(report_date)

    image_path: str | None = None
    if image_file and image_file.filename:
        image_path = _save_upload(image_file)

    report = DailyReport(
        student_id=student_id,
        report_date=parsed_date,
        notes=notes.strip(),
        rating=rating,
        image_url=(image_url.strip() if image_url else None),
        image_path=image_path,
    )
    session.add(report)
    session.commit()

    return RedirectResponse(url=f"/students/{student_id}?success=Report%20saved", status_code=303)


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
        return RedirectResponse(url="/students?error=Student%20not%20found", status_code=303)

    report = session.get(DailyReport, report_id)
    if not report or report.student_id != student_id:
        return RedirectResponse(url=f"/students/{student_id}/reports?error=Report%20not%20found", status_code=303)

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
        return RedirectResponse(url="/students?error=Student%20not%20found", status_code=303)

    report = session.get(DailyReport, report_id)
    if not report or report.student_id != student_id:
        return RedirectResponse(url=f"/students/{student_id}/reports?error=Report%20not%20found", status_code=303)

    report.report_date = date.fromisoformat(report_date)
    report.notes = notes.strip()
    report.rating = rating

    if clear_image_url:
        report.image_url = None
    else:
        # If left blank, keep whatever is already there.
        if image_url is not None and image_url.strip() != "":
            report.image_url = image_url.strip()

    if clear_image_upload:
        _try_delete_uploaded_file(report.image_path)
        report.image_path = None

    if image_file and image_file.filename:
        # Replace previous upload
        _try_delete_uploaded_file(report.image_path)
        report.image_path = _save_upload(image_file)

    session.add(report)
    session.commit()
    return RedirectResponse(url=f"/students/{student_id}/reports?success=Report%20updated", status_code=303)


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
        return RedirectResponse(url=f"/students/{student_id}/reports?error=Report%20not%20found", status_code=303)

    _try_delete_uploaded_file(report.image_path)
    session.delete(report)
    session.commit()
    return RedirectResponse(url=f"/students/{student_id}/reports?success=Report%20deleted", status_code=303)


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

    parsed_date = date.fromisoformat(report_date)

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

    # Best-effort: build a base URL for clickable uploaded-image links.
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
        + (
            f"\nUploaded image: {public_base_url}{report.image_path}\n"
            if report.image_path
            else ""
        )
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
            "current_user": request.session.get("user"),
            "google_configured": is_auth_configured(),
        },
    )
