# Job4youNow agent app container — FastAPI + background workers in one
# process (see agent/app/main.py's docstring for why one container is
# enough for the first prototype).
#
# Build context is the REPO ROOT, not agent/ — unlike telegram/ (which has
# its own self-contained package.json/lockfile), this app shares the root
# pyproject.toml/uv.lock with the rest of the Python side (agent/, database/).
# See agent/docker-compose.yml's `context: ..`.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# career-ops-modified (bind-mounted at runtime — see docker-compose.yml) is
# an npm project: ScanTool/ResumeTool run its real scan/pdf scripts rather
# than reimplementing them (see agent/infra/career-ops-setup.md). Node 22.5+
# per its own doctor.mjs recommendation (older 18+ still works, minus the
# sqlite-backed tracker index). git is needed for a couple of its own
# npm-installed dependencies that resolve from git refs, not just npm's registry.
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /repo

# Dependency layer first so rebuilds after an agent/-only source change
# don't re-resolve or re-download anything.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY agent ./agent
RUN chmod +x agent/infra/entrypoint.sh agent/infra/career_ops_bootstrap.sh

EXPOSE 8000

# The entrypoint seeds/updates career-ops-modified's npm deps + Playwright
# browser + starter config at container start (not build time — the
# submodule's real content only exists once the bind mount is live) before
# handing off to the actual app command.
ENTRYPOINT ["agent/infra/entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "agent.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
