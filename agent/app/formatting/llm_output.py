"""Normalizing raw LLM text before it reaches a presenter. Isolated from
presenters.py because "clean up what the model said" and "lay out the
Telegram message" are different concerns — a presenter should never need to
know what a tool-chatter line looks like.
"""
from __future__ import annotations

import re

# Lines a model sometimes emits that describe its own process rather than
# the answer itself — not useful to an end user reading a Telegram message.
_TOOL_CHATTER_PATTERNS = (
    re.compile(r"^\s*(i'll|i will|let me|now i'll|next,? i'll)\b.*$", re.IGNORECASE),
    re.compile(r"^\s*(running|calling|invoking|executing)\s+\S+.*$", re.IGNORECASE),
)


def _is_tool_chatter(line: str) -> bool:
    return any(pattern.match(line) for pattern in _TOOL_CHATTER_PATTERNS)


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def normalize_llm_text(raw: str) -> str:
    """Trim, drop tool-chatter lines, and collapse excess blank lines. Does
    NOT add headings/bullets or otherwise rewrite content — that would risk
    changing meaning; this only removes noise."""
    lines = [line for line in raw.strip().splitlines() if not _is_tool_chatter(line)]
    return _collapse_blank_lines("\n".join(lines)).strip()
