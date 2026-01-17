from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Dorm Management Portal"

    # Session / Auth
    session_secret: str = "dev-change-me"

    # Development mode (allows local login without Google OAuth configured)
    dev_mode: bool = False
    dev_user_email: str = "dev@example.com"
    dev_user_name: str = "Dev User"

    # Google OAuth (no Firebase)
    google_client_id: str | None = None
    google_client_secret: str | None = None
    # e.g. http://127.0.0.1:8000/auth/google/callback
    google_redirect_uri: str | None = None

    # Email (optional; required only for sending)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool = True


settings = Settings()
