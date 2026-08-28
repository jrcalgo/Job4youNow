"""Concurrency limits, in one place. `user_tasks` and `scheduled_scans` are
separate semaphores on purpose — a scheduled scan holding every available
Cursor SDK run slot must never be able to make a live Telegram request wait,
which is exactly what would happen if both pools shared one limiter.
"""
from __future__ import annotations

import asyncio

from agent.app.config import Settings


class Concurrency:
    def __init__(self, settings: Settings) -> None:
        self.user_tasks = asyncio.Semaphore(settings.max_user_tasks)
        self.scheduled_scans = asyncio.Semaphore(settings.max_scan_tasks)
        self.cursor_runs = asyncio.Semaphore(settings.max_cursor_runs)
