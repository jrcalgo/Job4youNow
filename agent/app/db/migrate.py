"""Apply every `db/migrations/*.sql` file, in filename order, over the Data
API. Mirrors telegram/src/cli.mjs's `migrate` command — SQL files use
`CREATE TABLE IF NOT EXISTS` throughout, so re-running this is always safe.

Each file is split into individual statements before being sent: the RDS
Data API's `ExecuteStatement` action rejects a request containing more than
one statement ("Multistatements aren't supported"), confirmed against a
real cluster — unlike a normal psql/libpq connection, which happily runs a
whole multi-statement file in one call. `BatchExecuteStatement` doesn't
help either; it repeats ONE statement over multiple parameter sets, not
multiple distinct statements. Splitting on `;` after stripping full-line
`--` comments is safe here specifically because none of these migration
files embed a semicolon inside a string/dollar-quoted literal (verified by
inspection, not a general-purpose SQL splitter) — the exact same approach
telegram/src/cli.mjs's `cmdMigrate` already uses for its own schema.sql.

Usage:
    uv run python -m agent.app.db.migrate
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.app.config import get_settings
from agent.app.db.aurora_client import build_aurora_client
from agent.app.logging import configure_logging, get_logger

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
log = get_logger("db.migrate")


def _split_statements(sql_text: str) -> list[str]:
    without_comments = "\n".join(line for line in sql_text.splitlines() if not line.strip().startswith("--"))
    return [statement.strip() for statement in without_comments.split(";") if statement.strip()]


async def run_migrations() -> None:
    settings = get_settings()
    client = build_aurora_client(settings)
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for path in files:
        statements = _split_statements(path.read_text())
        log.info("applying migration", extra={"context": {"file": path.name, "statements": len(statements)}})
        for statement in statements:
            await client.exec(statement)
    log.info("migrations complete", extra={"context": {"count": len(files)}})


def main() -> None:
    configure_logging(get_settings().log_level)
    asyncio.run(run_migrations())


if __name__ == "__main__":
    main()
