#!/usr/bin/env python3
"""demo_career_ops_agent.py — proves a Python-invoked Cursor agent can run
career-ops "on call".

The agent's local runtime `cwd` is pointed at the career-ops-modified
submodule, so it has direct filesystem access to
`.cursor/skills/career-ops/SKILL.md` and can follow its routing exactly like
an interactive Cursor session would. This is the minimal proof of that
pattern — not the full agentic system, just the one call that makes it
possible.

Usage:
    uv run python agent/demo_career_ops_agent.py

Requires CURSOR_API_KEY in .env (job4meNow root) or the environment — get one
from https://cursor.com/dashboard/integrations. A locally logged-in
`cursor-agent` CLI session is NOT sufficient on its own: verified empirically
that Agent.prompt(...) with local runtime still raises ConfigurationError
without an explicit api_key, even when `cursor-agent status` reports logged in.
"""
import os
import sys
from pathlib import Path

from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions
from dotenv import load_dotenv

load_dotenv()

CAREER_OPS_DIR = Path(__file__).parent / "modules" / "career-ops-modified"

# Discovery mode only: prints career-ops' command menu, makes no file changes,
# hits no network job boards — a safe, verifiable proof of access without
# triggering a real scan/evaluate/apply action.
PROMPT = (
    "Read .cursor/skills/career-ops/SKILL.md in this working directory and "
    "follow its Discovery Mode instructions exactly, as if no sub-command "
    "were given. Show the command menu it specifies, verbatim. Do not "
    "execute any other mode and do not make any file changes."
)


def main() -> int:
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        print(
            "CURSOR_API_KEY is not set. Get one from "
            "https://cursor.com/dashboard/integrations and put it in .env "
            "(job4meNow root).",
            file=sys.stderr,
        )
        return 1

    if not CAREER_OPS_DIR.exists():
        print(
            f"submodule not found at {CAREER_OPS_DIR} — run "
            "`git submodule update --init` first.",
            file=sys.stderr,
        )
        return 1

    try:
        result = Agent.prompt(
            PROMPT,
            AgentOptions(
                api_key=api_key,
                model="composer-2.5",
                local=LocalAgentOptions(cwd=str(CAREER_OPS_DIR)),
            ),
        )
    except CursorAgentError as err:
        # Covers every specific failure subtype (ConfigurationError,
        # AuthenticationError, RateLimitError, NetworkError, ...) — all
        # inherit from CursorAgentError. The run never executed.
        print(f"startup failed: {err.message} (retryable={err.is_retryable})", file=sys.stderr)
        return 1

    if result.status == "error":
        # The run DID execute but failed mid-flight — different failure mode,
        # different exit code, per the sdk skill's "two kinds of failure" note.
        print(f"run failed: {result.id}", file=sys.stderr)
        return 2

    print(f"run_id: {result.id}  status: {result.status}  model: {result.model}")
    print()
    print(result.result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
