from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class DormUser(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str
    picture_url: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class Student(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class DailyReport(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    student_id: int = Field(foreign_key="student.id", index=True)
    report_date: date = Field(index=True)

    notes: str
    rating: Optional[int] = Field(default=None, ge=1, le=5)

    # Either paste a URL or upload an image (stored on this server)
    image_url: str | None = None
    image_path: str | None = None

    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
