"""Backlog browser presenter and work_mode derivation."""
from __future__ import annotations

from datetime import datetime, timezone

from agent.app.db.domain_repo import _derive_work_mode
from agent.app.formatting.presenters import Presenters
from agent.app.models.domain import JobListingRow
from agent.app.models.telegram import TelegramInlineButton


def test_derive_work_mode_from_location() -> None:
    assert _derive_work_mode("Remote - US") == "remote"
    assert _derive_work_mode("Hybrid London") == "hybrid"
    assert _derive_work_mode("On-site NYC") == "onsite"
    assert _derive_work_mode(None) == "unknown"


def test_backlog_card_includes_nav_and_enqueue() -> None:
    presenters = Presenters()
    listing = JobListingRow(
        id="listing-abc123",
        role_id="backend",
        company_name="Acme",
        title="Engineer",
        url="https://example.com/job",
        location="Remote",
        posted_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        work_mode="remote",
        status="pending",
        summary=["Python"],
    )
    enqueue = TelegramInlineButton(text="Save to queue", callback_data="tg:bl:enqueue:listing-abc123")
    response = presenters.backlog_card(listing, offset=0, total=3, enqueue_button=enqueue)
    assert response.buttons[0][0].callback_data == "main"
    assert "Next" in response.buttons[1][0].text
    assert response.buttons[2][0].callback_data == "tg:bl:enqueue:listing-abc123"
