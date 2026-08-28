"""Backlog browser filter and query models."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from agent.app.models.domain import JobListingRow


class BacklogFilters(BaseModel):
    role_ids: list[str] = Field(default_factory=list)
    company_names: list[str] = Field(default_factory=list)
    work_modes: list[str] = Field(default_factory=list)
    posted_at_min: date | None = None
    posted_at_max: date | None = None
    retrieved_at_min: date | None = None
    retrieved_at_max: date | None = None
    applicant_count_min: int | None = None
    applicant_count_max: int | None = None


class BacklogPrefs(BaseModel):
    filters: BacklogFilters = Field(default_factory=BacklogFilters)
    sort_key: str = "retrieved_at"
    sort_dir: str = "desc"


class BacklogQueryResult(BaseModel):
    items: list[JobListingRow]
    total_count: int
    offset: int


class BacklogBrowserState(BaseModel):
    offset: int = 0
    view_role_id: str | None = None


class BacklogFilterWizardState(BaseModel):
    pending: BacklogFilters = Field(default_factory=BacklogFilters)
    sort_key: str = "retrieved_at"
    sort_dir: str = "desc"
