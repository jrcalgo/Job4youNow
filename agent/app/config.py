"""Application settings — the only module that reads process environment
variables directly. Every other module receives a `Settings` instance (or a
narrower value taken from one) through a constructor or function argument;
see the plan's "Implementation Philosophy" section for why this boundary
matters (no `os.environ[...]` scattered through the codebase).

Reads the single repository-root `.env` — see [.env.example](../../.env.example)
for the complete, organized-by-boundary list every setting below maps to.
There is deliberately no `agent/.env.example`; one root file is the source
of truth for both this app and the telegram adapter.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AGENT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = AGENT_DIR.parent

# The default model for every Cursor SDK call the agent app makes, until a
# task kind has an explicit saved configuration (see
# db/model_config_repo.py / routing/model_config.py's Telegram MODELS
# menu). Grok 4.5's exact catalog id — confirm this against a live
# `Cursor.models.list()` / `AsyncCursor.models.list()` response before
# relying on it in production; model ids/availability evolve and are
# account-specific (see the Cursor SDK skill's guidance against
# hardcoding unusual model ids without confirming access).
DEFAULT_MODEL_ID = "grok-4.5"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(REPO_ROOT / ".env"), extra="ignore")

    # -- Aurora PostgreSQL Serverless v2 (Data API) — same cluster the
    # -- telegram bot uses; see telegram/infra/aurora-setup.md for provisioning.
    # -- Aurora holds metadata/pointers and public job-search data ONLY —
    # -- see models/artifacts.py's ContentVisibility for the boundary this
    # -- app enforces on every write.
    aws_region: str = Field(alias="AWS_REGION")
    aurora_resource_arn: str = Field(alias="AURORA_RESOURCE_ARN")
    aurora_secret_arn: str = Field(alias="AURORA_SECRET_ARN")
    aurora_database: str = Field(default="job4younow", alias="AURORA_DATABASE")

    # -- S3: public/job-search artifacts (scan reports, non-sensitive files).
    # -- Never write user-private content here — see private_user_bucket below.
    job_artifacts_bucket: str = Field(alias="JOB_ARTIFACTS_BUCKET")
    job_artifacts_prefix: str = Field(default="job4younow-agent/", alias="JOB_ARTIFACTS_PREFIX")

    # -- S3: private user artifacts ONLY (augmented resumes, private
    # -- responses/reports) — a separate bucket with its own IAM policy so
    # -- "public job-search" and "private user data" never share an access
    # -- boundary. See models/artifacts.py's ArtifactBucket.
    private_user_bucket: str = Field(alias="PRIVATE_USER_ARTIFACTS_BUCKET")
    private_user_prefix: str = Field(default="private/", alias="PRIVATE_USER_ARTIFACTS_PREFIX")

    # -- Local backup of every private artifact this app writes — lives
    # -- under career-ops-modified, never in a database or a shared/public
    # -- bucket. `cv.md` itself (the resume-tailoring source of truth) is
    # -- read directly from career_ops_dir's root, not from a separate dir.
    private_local_backup_dir: Path = Field(
        default=AGENT_DIR / "modules" / "career-ops-modified" / "resumes" / "augmented",
        alias="PRIVATE_LOCAL_BACKUP_DIR",
    )

    # -- Cursor SDK.
    cursor_api_key: str = Field(alias="CURSOR_API_KEY")
    cursor_model: str = Field(default=DEFAULT_MODEL_ID, alias="CURSOR_MODEL")
    career_ops_dir: Path = Field(
        default=AGENT_DIR / "modules" / "career-ops-modified",
        alias="CAREER_OPS_DIR",
    )

    # -- HTTP API.
    api_host: str = Field(default="0.0.0.0", alias="AGENT_API_HOST")
    api_port: int = Field(default=8000, alias="AGENT_API_PORT")

    # -- Concurrency limits — see workers/concurrency.py. Kept low by default;
    # -- raise once real usage shows the defaults are the bottleneck.
    max_user_tasks: int = Field(default=2, alias="AGENT_MAX_USER_TASKS")
    max_scan_tasks: int = Field(default=1, alias="AGENT_MAX_SCAN_TASKS")
    max_cursor_runs: int = Field(default=2, alias="AGENT_MAX_CURSOR_RUNS")

    # -- Task leasing and polling cadence, in seconds.
    task_lease_seconds: int = Field(default=600, alias="AGENT_TASK_LEASE_SECONDS")
    worker_poll_seconds: float = Field(default=2.0, alias="AGENT_WORKER_POLL_SECONDS")
    scheduler_poll_seconds: float = Field(default=30.0, alias="AGENT_SCHEDULER_POLL_SECONDS")

    # -- Read-through cache TTL, in seconds — see cache/ttl_cache.py.
    cache_ttl_seconds: int = Field(default=60, alias="AGENT_CACHE_TTL_SECONDS")
    cache_max_entries: int = Field(default=512, alias="AGENT_CACHE_MAX_ENTRIES")

    # -- Cursor SDK run timeout, in seconds.
    cursor_run_timeout_seconds: float = Field(default=180.0, alias="AGENT_CURSOR_TIMEOUT_SECONDS")

    # -- career-ops mode runs take much longer than a plain prompt: scan's
    # -- agent-driven Level 1 (Playwright)/Level 3 (WebSearch) fallback can
    # -- cover many companies, and pdf's pipeline is a dozen-plus-step
    # -- process (skill-gap check, HTML build, fact gate, PDF render). The
    # -- zero-token `node scan.mjs` subprocess itself (see
    # -- tools/career_ops_scripts.py) has its own separate timeout, since
    # -- it never goes through CursorSdkTool at all.
    scan_agent_timeout_seconds: float = Field(default=600.0, alias="AGENT_SCAN_TIMEOUT_SECONDS")
    scan_subprocess_timeout_seconds: float = Field(default=300.0, alias="AGENT_SCAN_SUBPROCESS_TIMEOUT_SECONDS")
    resume_agent_timeout_seconds: float = Field(default=600.0, alias="AGENT_RESUME_TIMEOUT_SECONDS")

    log_level: str = Field(default="info", alias="AGENT_LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton. `lru_cache` (not a bare module
    global) so tests can call `get_settings.cache_clear()` to reload env
    vars between cases instead of restarting the interpreter."""
    return Settings()
