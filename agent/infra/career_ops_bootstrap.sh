#!/usr/bin/env sh
# agent/infra/career_ops_bootstrap.sh — one-time, idempotent setup for the
# career-ops-modified submodule: installs its Node dependencies + Playwright
# browser, and seeds the three User Layer files (cv.md, config/profile.yml,
# portals.yml) it needs to run at all — none of which career-ops ships
# itself (see its DATA_CONTRACT.md: these are gitignored, per-user files).
#
# Safe to re-run: every step is guarded by an existence check, so running
# this again after the user has replaced the seeded files with their real
# CV/profile/company list is a no-op.
#
# Called from the agent container's startup (see agent/Dockerfile) because
# career-ops-modified is bind-mounted, not baked into the image — its real
# content (and therefore whether `npm install` has already run) is only
# knowable at container start, not at image build time.
set -eu

CAREER_OPS_DIR="${CAREER_OPS_DIR:-/repo/agent/modules/career-ops-modified}"

if [ ! -d "$CAREER_OPS_DIR" ] || [ -z "$(ls -A "$CAREER_OPS_DIR" 2>/dev/null)" ]; then
  echo "career_ops_bootstrap: $CAREER_OPS_DIR is missing or empty — run 'git submodule update --init' first. Skipping." >&2
  exit 0
fi

cd "$CAREER_OPS_DIR"

if [ ! -d node_modules ]; then
  echo "career_ops_bootstrap: installing career-ops npm dependencies..."
  npm install --no-audit --no-fund
fi

if [ ! -d node_modules/playwright ] || [ ! -d "${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}" ]; then
  echo "career_ops_bootstrap: installing Playwright's Chromium browser..."
  npx --yes playwright install --with-deps chromium
fi

mkdir -p config

if [ ! -f portals.yml ]; then
  echo "career_ops_bootstrap: seeding portals.yml from templates/portals.example.yml (edit before real use)"
  cp templates/portals.example.yml portals.yml
fi

if [ ! -f config/profile.yml ]; then
  echo "career_ops_bootstrap: seeding config/profile.yml from the dual-track example profile (edit before real use)"
  cp examples/dual-track-engineer-instructor/profile.yml config/profile.yml
fi

if [ ! -f cv.md ]; then
  echo "career_ops_bootstrap: seeding cv.md from the dual-track example CV (edit before real use)"
  cp examples/dual-track-engineer-instructor/cv.md cv.md
fi

echo "career_ops_bootstrap: done."
