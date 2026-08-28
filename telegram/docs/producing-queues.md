# Producing queues for the hosted bot

This bot never talks to career-ops, mounts a career-ops checkout, or runs any career-ops code. It only knows about the [queue JSON contract](#queue-json-contract) below and the [`ingest` CLI command](#ingesting-a-queue) that reads one. Any producer — career-ops today, something else later — talks to this bot exclusively through `node src/cli.mjs ingest`.

career-ops itself is never edited to support this. The queue JSON shape below matches what career-ops' own telegram mode used to produce, so an existing producer built against that shape needs no format change to route its queue through this hosted bot instead.

## Why its own bot token

Telegram allows exactly one `getUpdates` long-poll consumer per bot token. This daemon polls **forever**, so its token must never be shared with any other poller — sharing one token means a race for whichever process polls next gets an HTTP 409 and the other goes silent, a guaranteed collision the moment two consumers use the same token around the same time.

The fix: **create a bot with [@BotFather](https://t.me/BotFather)** dedicated to this daemon. Message it once, then run:

```bash
docker compose exec bot node src/cli.mjs whoami
```

to discover the chat id. A private chat's `chat.id` is your own Telegram user id — the same value works across any number of your own bots; only `TELEGRAM_BOT_TOKEN` needs to be unique to this daemon.

## Queue JSON contract

Identical to the shape career-ops' `modes/telegram.md` already produces — no format change for existing producers:

```json
{
  "title": "Evaluated shortlist 2026-08-22",
  "items": [
    {
      "id": "report:042",
      "report_num": "042",
      "company": "Acme",
      "role": "Staff Platform Engineer",
      "url": "https://example.com/jobs/42",
      "score": "4.3/5",
      "location": "Charlotte hybrid",
      "salary": "",
      "artifacts": {
        "cv_pdf": "output/042-acme-cv.pdf",
        "jd": "jds/042-acme.md"
      },
      "contacts": [],
      "summary": {
        "job": ["Owns platform APIs", "On-call for core services"],
        "company": ["B2B SaaS", "Series C"],
        "risks": ["Hybrid schedule"],
        "why_match": ["Strong systems + backend overlap with CV"]
      }
    }
  ]
}
```

Validated by `src/protocol/core.mjs`'s `validateQueue()`:

- `title` — optional, defaults to "Jobs under review".
- `items[]` — required, non-empty. Each needs `company` and `role`.
- `items[].id` — optional; defaults to `report:{report_num}` (zero-padded to 3 digits) or `item:{n}` if there's no `report_num` either. Must be unique within the queue.
- `items[].artifacts` — a map of `kind -> path, RELATIVE TO --root` (see below). Only `output/`, `reports/`, `jds/`, and `batch/` are allowed prefixes — anything else, or any path that escapes `--root` (e.g. `../../etc/passwd`), is rejected before anything is uploaded. Recognized kinds: `cv_pdf` (or `cv`) and `jd` (or `jd_md`) — that's what `CV` and `JD` in Telegram look for.
- `items[].contacts[]` — optional; `{ name, title, email, linkedin, phone }`. A contact with a `phone` gets sent as a native Telegram contact card in addition to the text summary.
- `items[].summary.{job,company,risks,why_match}` — optional string arrays. Keep bullets short; this is read on a phone.

Same sourcing rule as career-ops' own mode file: summaries must be grounded in what the producer already has (reports, CV, profile, whatever) — never fabricated to fill the shape.

## Ingesting a queue

```bash
docker compose exec bot node src/cli.mjs ingest \
  --queue /path/to/telegram-queue.json \
  --root /path/to/producer/checkout \
  [--title "Override title"] \
  [--dry-run]
```

- `--root` is the directory `artifacts` paths in the queue JSON are relative to. For career-ops, that's the repo root (e.g. `output/042-acme-cv.pdf` resolves under it). It is used **only** to validate and upload artifacts at ingest time — the daemon has no further access to it and never reads from it again.
- `--dry-run` validates and reports what would be uploaded (paths, sizes) without touching S3 or Aurora. Always safe to run first.
- On success: uploads every referenced artifact to S3 (each `(queue, item, kind)` gets one deterministic key — re-ingesting the same queue id overwrites the same objects, never accumulates orphans), then writes the queue plus its items in one Aurora transaction. Prints a JSON summary including `queue_id`.
- career-ops invocation, run from the career-ops checkout after building `output/telegram-queue.json` per its own `modes/telegram.md` step 1:

  ```bash
  docker compose -f /path/to/Job4youNow/telegram/docker-compose.yml exec bot \
    node src/cli.mjs ingest --queue /career-ops/output/telegram-queue.json --root /career-ops
  ```

  (adjust the two paths to wherever each checkout actually lives on the host, and to however you've mounted career-ops into this container if you choose to — nothing here requires that mount to be a bind mount into the SAME container that's running the daemon; a plain host path works too as long as `docker compose exec` can reach the queue file and artifacts, e.g. by mounting career-ops' `output/`/`reports/`/`jds/` read-only into this container at ingest time. The daemon service itself needs no such mount — only whichever shell runs the `ingest` command does.)

## Reviewing in Telegram

Nothing here changes from career-ops' original design — this is still human-in-the-loop, still never submits an application:

`NEXT` · `SKIP` · `CV` · `JD` · `CONTACTS` · `COMPANY` · `MORE` · `NOTE <text>` · `LIST` · `HELP` · `PAUSE` · or a number to jump. Buttons on each card do the same.

Two commands this daemon added that career-ops' original grammar had no use for (it always had exactly one queue file, started by an agent that could also run a local `resume` command — a hosted bot has neither):

- `QUEUES` — lists every ingested queue, marking the one currently active.
- `QUEUE <n>` (or `USE <n>`) — switches to the queue at that list position. Resets review progress (cursor/stats) on the new queue; never resets your Telegram offset (that belongs to the conversation, not the queue).
- `RESUME` — continues a `PAUSE`d session. (The original told you to run a CLI command instead; there's no "local CLI" to reach from a phone.)

## Operating notes

- `node src/cli.mjs queues` / `state` — same information as Telegram's `QUEUES`/`LIST`, for the host/operator side.
- `node src/cli.mjs reset --force` — abandons review progress on the *active* queue only. Ingested queues are never deleted by this — pick a different one with `QUEUES`/`QUEUE <n>`, or re-ingest.
- `node src/cli.mjs migrate` — applies `src/db/schema.sql` (run once against a fresh Aurora database, and again after any future schema change).
- First ingest/send after an idle period can take ~15s while Aurora resumes from auto-pause — see [../infra/aurora-setup.md](../infra/aurora-setup.md). This is expected, not a hang.
- `npm test` runs the full suite this contract (queue validation, ingest, artifact upload/cache, the Telegram command grammar) is checked against.
