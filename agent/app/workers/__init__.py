"""Background execution: task_worker.py claims and runs tasks (two pools —
user and scheduled-scan, see concurrency.py), scheduler.py turns due
`scan_schedules` rows into scheduler-sourced tasks. Nothing here talks to
Telegram directly — outbox rows are drained by the (separate) Telegram
adapter through the API, not pushed from here.
"""
