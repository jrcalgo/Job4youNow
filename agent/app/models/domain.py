"""Read-shape models for job listings/companies/contacts — what
db/domain_repo.py's read queries return and what formatting/presenters.py
renders. Kept separate from models/db_writes.py because a read shape and a
write intent are different concerns: a `JobListingRow` is "what exists",
a `JobListingWrite` is "what to create".
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RoleBacklog(BaseModel):
    """One line of the backlog-by-role menu — see presenters.backlog()."""

    role_id: str
    pending_count: int


class ContactRow(BaseModel):
    name: str | None = None
    role_title: str | None = None
    email: str | None = None
    phone: str | None = None


class JobListingRow(BaseModel):
    id: str
    role_id: str
    company_name: str
    title: str
    url: str | None = None
    location: str | None = None
    posted_at: datetime | None = None
    retrieved_at: datetime | None = None
    work_mode: str | None = None
    applicant_count: int | None = None
    summary: list[str] = Field(default_factory=list)
    status: str
    contacts: list[ContactRow] = Field(default_factory=list)
