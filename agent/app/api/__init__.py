"""The HTTP boundary the Telegram adapter talks to. No `api/schemas.py`
alongside `routes.py` — models/ already provides every request/response
shape a route needs (TelegramUpdateEnvelope, ScheduleCreateRequest, ...), so
a second re-exporting schema module would just be indirection with nothing
to add (see the plan's "do not create empty abstractions").
"""
