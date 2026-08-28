"""HTML escaping for Telegram's `parse_mode=HTML` — the only parse mode this
app uses (see models/telegram.py's TelegramOutboundMessage). Centralized
here so every presenter escapes user/company/LLM-derived text the same way,
instead of each one remembering to call `.replace("&", ...)` itself.
"""
from __future__ import annotations


def escape_html(text: str | None) -> str:
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def bold(text: str) -> str:
    return f"<b>{escape_html(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{escape_html(text)}</i>"


def bullet_list(items: list[str]) -> str:
    return "\n".join(f"• {escape_html(item)}" for item in items)
