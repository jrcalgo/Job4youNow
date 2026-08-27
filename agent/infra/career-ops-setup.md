# career-ops-modified setup

The agent app's `ScanTool` and `ResumeTool` (see [agent/app/tools/scan_tool.py](../app/tools/scan_tool.py) and [agent/app/tools/resume_tool.py](../app/tools/resume_tool.py)) work by running career-ops's own `scan` and `pdf` modes inside the `career-ops-modified` submodule — they do not reimplement career-ops's logic. This doc covers getting that submodule from "empty" to "runnable."

## 1. Initialize the submodule

```bash
git submodule update --init agent/modules/career-ops-modified
```

## 2. Install its dependencies and seed starter config

career-ops-modified is an npm project (Node >= 18, Node >= 22.5 recommended) with a Playwright dependency for PDF rendering. It also needs three **User Layer** files — `cv.md`, `config/profile.yml`, `portals.yml` — that career-ops deliberately never ships (they're gitignored inside the submodule; see its `DATA_CONTRACT.md`). Nothing can invent your real CV or company list, so this step seeds safe, fictional placeholders instead:

```bash
CAREER_OPS_DIR=agent/modules/career-ops-modified ./agent/infra/career_ops_bootstrap.sh
```

This is the same script the agent container runs automatically on startup (see [agent/Dockerfile](../Dockerfile)) — running it manually here is only useful for local development outside Docker, or to re-seed after deleting a file. It is idempotent: re-running it after you've edited the seeded files does nothing (each step is guarded by an existence check).

What it seeds, and from where:

| File | Seeded from | Must be replaced with |
|---|---|---|
| `portals.yml` | `templates/portals.example.yml` (career-ops's own official onboarding copy target — see `doctor.mjs`'s fix message) | Your real tracked companies, search queries, and title filter keywords |
| `config/profile.yml` | `examples/dual-track-engineer-instructor/profile.yml` (career-ops's own fictional example persona) | Your real name, contact info, target roles, and comp range |
| `cv.md` | `examples/dual-track-engineer-instructor/cv.md` (the matching fictional CV for the same example) | Your real resume in markdown |

**Scan and resume requests will run against the placeholder "Sam Rivera" persona and an empty/example company list until you replace these three files.** Edit them directly on disk under `agent/modules/career-ops-modified/` (they're bind-mounted into the container — no rebuild needed) — there is no Telegram UI for this yet.

## 3. Verify the setup

From inside `agent/modules/career-ops-modified`:

```bash
node doctor.mjs
```

This is career-ops's own setup diagnostic — it checks Node version, the presence/shape of `portals.yml`/`cv.md`/`config/profile.yml`, and (with `--strict`) that every tracked company's ATS slug actually resolves. A clean bootstrap reports two expected warnings that are safe to ignore for this integration: no Playwright MCP tools detected (that's for interactive CLI use, not the SDK-driven calls this app makes) and `modes/_profile.md` not found (an optional richer narrative/archetypes file this setup doesn't seed — `cp modes/_profile.template.md modes/_profile.md` if you want it).

## What this integration does NOT need

`career-ops-modified`'s own `.env` (Gemini/OpenRouter/OpenAI keys, plugin tokens) is **not** required — those only matter for career-ops's standalone `*-eval.mjs`/`openrouter-runner.mjs` scripts, which this integration doesn't call. Every LLM call career-ops needs here goes through this app's own Cursor SDK configuration (`CURSOR_API_KEY` in the repo-root `.env`).
