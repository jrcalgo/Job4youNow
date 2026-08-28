"""Entrypoint: one FastAPI app, running the HTTP API and the background
worker/scheduler loops in the same asyncio process (see the plan's "Occam's
Razor deployment rule" — split into separate containers later only if
there's evidence the combined process is actually the bottleneck).

Run with:
    uv run uvicorn agent.app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from agent.app.api.routes import router
from agent.app.application import AgentApplication
from agent.app.cache.invalidation import CacheInvalidator
from agent.app.cache.ttl_cache import TtlCache
from agent.app.config import Settings, get_settings
from agent.app.db.aurora_client import AuroraClient, build_aurora_client
from agent.app.db.db_gate import DbGate
from agent.app.formatting.presenters import Presenters
from agent.app.graph.nodes import GraphDeps
from agent.app.graph.supervisor import build_supervisor_graph
from agent.app.logging import configure_logging, get_logger, set_redacted_secrets
from agent.app.models.artifacts import ArtifactBucket
from agent.app.models.tasks import TaskSource
from agent.app.tools.artifact_store import ArtifactStore, LocalPrivateBackupStore, PrivateStores
from agent.app.tools.career_ops_tool import CareerOpsTool
from agent.app.tools.cursor_sdk_tool import CursorSdkTool
from agent.app.tools.db_read_tool import DbReadTool
from agent.app.tools.model_catalog import ModelCatalogService
from agent.app.tools.resume_tool import ResumeTool
from agent.app.tools.scan_tool import ScanTool
from agent.app.workers.concurrency import Concurrency
from agent.app.workers.scheduler import Scheduler
from agent.app.workers.task_worker import TaskWorkerPool

log = get_logger("main")


class AppServices:
    """Everything built once at startup and torn down at shutdown. Held on
    `app.state.services` so tests can reach in and replace pieces (e.g. swap
    the AuroraClient for a fake) without restarting the process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: AuroraClient = build_aurora_client(settings)
        self.cache = TtlCache(max_entries=settings.cache_max_entries)
        self.presenters = Presenters()

        cache_invalidator = CacheInvalidator(self.cache)
        self.db_gate = DbGate(self.client, cache_invalidator)

        # Two S3 buckets by privacy boundary — see models/artifacts.py's
        # ArtifactBucket and the plan's "Target Storage Policy". The
        # private bucket is always paired with a local backup; the job
        # bucket never is (public data has no privacy reason to need one).
        job_artifacts = ArtifactStore(
            bucket_kind=ArtifactBucket.JOB_ARTIFACTS, bucket_name=settings.job_artifacts_bucket, prefix=settings.job_artifacts_prefix
        )
        private_bucket = ArtifactStore(
            bucket_kind=ArtifactBucket.PRIVATE_USER_ARTIFACTS,
            bucket_name=settings.private_user_bucket,
            prefix=settings.private_user_prefix,
        )
        self.private_stores = PrivateStores(
            bucket=private_bucket, local_backup=LocalPrivateBackupStore(settings.private_local_backup_dir)
        )
        self.job_artifacts = job_artifacts  # kept for future public-artifact tools; unused today

        self.concurrency = Concurrency(settings)
        cursor_sdk_tool = CursorSdkTool(settings, self.concurrency.cursor_runs, self.client)
        self.model_catalog = ModelCatalogService(settings)
        graph_deps = GraphDeps(
            scan_tool=ScanTool(cursor_sdk_tool, settings),
            resume_tool=ResumeTool(cursor_sdk_tool, settings, self.private_stores),
            career_ops_tool=CareerOpsTool(cursor_sdk_tool),
            db_read_tool=DbReadTool(self.client),
            db_gate=self.db_gate,
            presenters=self.presenters,
        )
        self.compiled_graph = build_supervisor_graph(graph_deps)

        self.agent_application = AgentApplication(self.client, self.cache, settings, self.private_stores, self.model_catalog)
        self.user_task_pool = TaskWorkerPool(
            client=self.client,
            source=TaskSource.USER,
            limiter=self.concurrency.user_tasks,
            settings=settings,
            compiled_graph=self.compiled_graph,
            presenters=self.presenters,
            private_stores=self.private_stores,
        )
        self.scan_task_pool = TaskWorkerPool(
            client=self.client,
            source=TaskSource.SCHEDULER,
            limiter=self.concurrency.scheduled_scans,
            settings=settings,
            compiled_graph=self.compiled_graph,
            presenters=self.presenters,
            private_stores=self.private_stores,
        )
        self.scheduler = Scheduler(self.client, settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    set_redacted_secrets([settings.cursor_api_key, settings.aurora_secret_arn])

    services = AppServices(settings)
    app.state.agent_application = services.agent_application

    stop_event = asyncio.Event()
    background_tasks = [
        asyncio.create_task(services.user_task_pool.run_forever(stop_event)),
        asyncio.create_task(services.scan_task_pool.run_forever(stop_event)),
        asyncio.create_task(services.scheduler.run_forever(stop_event)),
    ]
    log.info("agent app started", extra={"context": {"maxUserTasks": settings.max_user_tasks, "maxScanTasks": settings.max_scan_tasks}})

    try:
        yield
    finally:
        stop_event.set()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        log.info("agent app stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="job4younow-agent", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
