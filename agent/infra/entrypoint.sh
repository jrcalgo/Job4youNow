#!/usr/bin/env sh
# agent/infra/entrypoint.sh — runs the (idempotent, non-fatal) career-ops
# bootstrap before handing off to the real command. Bootstrap failure (e.g.
# no network on first start) must never block the FastAPI app itself from
# coming up — ScanTool/ResumeTool simply report a clear tool-level error at
# call time instead if career-ops-modified isn't actually ready.
set -eu

/repo/agent/infra/career_ops_bootstrap.sh || echo "entrypoint: career_ops_bootstrap.sh failed or was skipped — continuing startup" >&2

exec "$@"
