from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus, urlparse

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models import DormUser


oauth = OAuth()

def _validated_redirect_uri(raw_uri: str | None) -> tuple[str | None, str | None]:
    if not raw_uri:
        return None, "GOOGLE_REDIRECT_URI is missing"

    uri = raw_uri.strip()
    if uri.startswith("http://https://"):
        uri = uri.replace("http://https://", "https://", 1)
    elif uri.startswith("https://http://"):
        uri = uri.replace("https://http://", "http://", 1)

    parsed = urlparse(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "GOOGLE_REDIRECT_URI must be a full URL like https://your-domain/auth/google/callback"

    return uri, None


def configure_oauth() -> None:
    if settings.google_client_id and settings.google_client_secret:
        oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )


def is_auth_configured() -> bool:
    redirect_uri, error = _validated_redirect_uri(settings.google_redirect_uri)
    return bool(settings.google_client_id and settings.google_client_secret and redirect_uri and not error)


def get_current_user(request: Request) -> DormUser | None:
    user_dict = request.session.get("user")
    if not user_dict:
        return None

    try:
        return DormUser(
            id=user_dict.get("id"),
            email=user_dict.get("email"),
            name=user_dict.get("name"),
            picture_url=user_dict.get("picture_url"),
        )
    except Exception:
        return None


def require_user(request: Request) -> DormUser:
    user = get_current_user(request)
    if not user or not user.email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_user_or_redirect(request: Request) -> DormUser | RedirectResponse:
    user = get_current_user(request)
    if not user or not user.email:
        return RedirectResponse(url="/login", status_code=303)
    return user


def upsert_user_from_google(*, session: Session, userinfo: dict[str, Any]) -> DormUser:
    email = (userinfo.get("email") or "").strip().lower()
    name = (userinfo.get("name") or userinfo.get("given_name") or email or "User").strip()
    picture = userinfo.get("picture")

    existing = session.exec(select(DormUser).where(DormUser.email == email)).first()
    if existing:
        existing.name = name
        existing.picture_url = picture
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    user = DormUser(email=email, name=name, picture_url=picture)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


async def google_login(request: Request) -> RedirectResponse:
    if not is_auth_configured():
        return RedirectResponse(url="/login?error=Google%20OAuth%20not%20configured", status_code=303)

    redirect_uri, redirect_error = _validated_redirect_uri(settings.google_redirect_uri)
    if redirect_error or not redirect_uri:
        message = quote_plus(redirect_error or "Invalid Google redirect URI")
        return RedirectResponse(url=f"/login?error={message}", status_code=303)

    return await oauth.google.authorize_redirect(request, redirect_uri)


async def google_callback(request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return RedirectResponse(url="/login?error=Google%20login%20failed", status_code=303)

    userinfo = token.get("userinfo")
    if not userinfo:
        # fallback: fetch userinfo endpoint
        try:
            userinfo = await oauth.google.userinfo(token=token)
        except Exception:
            userinfo = None

    if not userinfo or not userinfo.get("email"):
        return RedirectResponse(url="/login?error=Could%20not%20read%20Google%20profile", status_code=303)

    user = upsert_user_from_google(session=session, userinfo=userinfo)

    request.session["user"] = {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture_url": user.picture_url,
    }

    return RedirectResponse(url="/students?success=Signed%20in", status_code=303)
