#!/usr/bin/env node
// cli.mjs — operator-facing commands. Run these from the host (or `docker
// compose exec bot node src/cli.mjs ...`), not from inside a Telegram chat.
//
// Usage:
//   node src/cli.mjs whoami
//   node src/cli.mjs migrate
//   node src/cli.mjs ingest --queue <path.json> [--root <path>] [--title "..."] [--dry-run]
//   node src/cli.mjs queues [--json]
//   node src/cli.mjs state [--json]
//   node src/cli.mjs reset [--force]
//
// `ingest` is the producer boundary: any pipeline (career-ops or otherwise)
// that wants this bot to review a batch of jobs calls THIS command with a
// queue JSON file — see docs/producing-queues.md for the exact contract and
// the career-ops invocation. It's also the only place
// protocol/core.mjs's assertSafeArtifactPath runs (against --root), and the
// only place local files get uploaded to S3 — the daemon itself never reads
// from --root or any mounted producer checkout.
import 'dotenv/config';
import { existsSync, readFileSync } from 'node:fs';
import { extname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { generateQueueId, validateQueue } from './protocol/core.mjs';
import { exec } from './db/client.mjs';
import * as repo from './db/repo.mjs';
import { artifactKey, uploadArtifact } from './artifacts/store.mjs';
import { getUpdates } from './telegram/api.mjs';
import { setRedactedSecrets } from './lib/log.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));

function fail(msg, code = 1) {
  process.stderr.write(`cli.mjs: ${msg}\n`);
  process.exit(code);
}

function hasFlag(name) {
  return process.argv.includes(`--${name}`);
}

function opt(name, def = '') {
  const i = process.argv.indexOf(`--${name}`);
  if (i < 0) return def;
  const v = process.argv[i + 1];
  return v && !v.startsWith('--') ? v : def;
}

// Returns `obj` (in addition to printing it) so test/cli.test.mjs can assert
// on the return value directly instead of scraping stdout — capturing
// process.stdout.write from inside node:test's own runner is unreliable
// (the runner instruments stdout itself for TAP reporting).
function outJson(obj) {
  process.stdout.write(`${JSON.stringify(obj, null, 2)}\n`);
  return obj;
}

function requireEnv(name) {
  const v = process.env[name];
  if (!v) fail(`missing required env var: ${name}`);
  return v;
}

export async function cmdWhoami() {
  const token = requireEnv('TELEGRAM_BOT_TOKEN');
  setRedactedSecrets([token]);
  const chatId = process.env.TELEGRAM_CHAT_ID || '';
  const updates = await getUpdates(token, { offset: 0, timeoutSec: 0 });
  const seen = new Map();
  for (const u of updates || []) {
    const msg = u.message || u.edited_message || u.callback_query?.message;
    const from = u.message?.from || u.callback_query?.from || msg?.from;
    const chat = msg?.chat;
    if (!chat) continue;
    seen.set(String(chat.id), {
      chat_id: chat.id,
      chat_type: chat.type,
      username: from?.username || chat.username || null,
      first_name: from?.first_name || chat.first_name || null,
      matches_env: chatId ? String(chat.id) === String(chatId) : false,
    });
  }
  const list = [...seen.values()];
  if (!list.length) {
    return outJson({
      status: 'no_updates',
      hint: 'Message your bot once from Telegram (the SEPARATE bot for this daemon — see docs/producing-queues.md), then re-run whoami.',
      TELEGRAM_CHAT_ID_configured: Boolean(chatId),
    });
  }
  return outJson({
    status: 'ok',
    chats: list,
    hint: chatId
      ? (list.some((c) => c.matches_env) ? 'Configured TELEGRAM_CHAT_ID matches an observed chat.' : 'Configured TELEGRAM_CHAT_ID does not match any observed chat.')
      : 'Set TELEGRAM_CHAT_ID in .env to your chat_id.',
  });
}

export async function cmdMigrate() {
  const sqlPath = join(__dirname, 'db', 'schema.sql');
  const raw = readFileSync(sqlPath, 'utf8');
  // Strip full-line `--` comments, then split on statement-terminating
  // semicolons. Safe for this file specifically: no statement here embeds a
  // semicolon inside a string/JSONB literal — verified by inspection, not a
  // general-purpose SQL splitter.
  const statements = raw
    .split('\n')
    .filter((line) => !line.trim().startsWith('--'))
    .join('\n')
    .split(';')
    .map((s) => s.trim())
    .filter(Boolean);

  for (const statement of statements) {
    await exec(statement);
  }
  return outJson({ status: 'migrated', statements: statements.length });
}

export async function cmdIngest() {
  const queuePath = opt('queue');
  if (!queuePath) fail('ingest needs --queue <path.json>');
  const absQueue = resolve(queuePath);
  if (!existsSync(absQueue)) fail(`queue file not found: ${queuePath}`);

  const root = resolve(opt('root', process.cwd()));
  let raw;
  try {
    raw = JSON.parse(readFileSync(absQueue, 'utf8'));
  } catch (err) {
    fail(`invalid queue JSON: ${err.message}`);
  }
  if (opt('title')) raw.title = opt('title');

  const validated = validateQueue(raw, { root });
  if (!validated.ok) fail(validated.error);

  const queueId = generateQueueId();
  const dryRun = hasFlag('dry-run');

  const artifactsToUpload = [];
  for (const item of validated.queue.items) {
    for (const [kind, relPath] of Object.entries(item.artifacts)) {
      artifactsToUpload.push({ n: item.n, kind, absPath: resolve(root, relPath), ext: extname(relPath) });
    }
  }

  if (dryRun) {
    return outJson({
      status: 'dry-run',
      queue_id: queueId,
      title: validated.queue.title,
      item_count: validated.queue.items.length,
      artifacts: artifactsToUpload.map((a) => ({ n: a.n, kind: a.kind, path: a.absPath })),
    });
  }

  const uploaded = [];
  for (const a of artifactsToUpload) {
    if (!existsSync(a.absPath)) fail(`artifact file not found: ${a.absPath} (item ${a.n}, ${a.kind})`);
    const key = artifactKey(queueId, a.n, a.kind, a.ext);
    const { byteSize, checksum } = await uploadArtifact(a.absPath, key);
    uploaded.push({ n: a.n, kind: a.kind, s3Key: key, byteSize, checksum });
  }

  await repo.insertQueue({
    id: queueId,
    title: validated.queue.title,
    source: absQueue,
    items: validated.queue.items,
    artifacts: uploaded,
  });

  return outJson({
    status: 'ingested',
    queue_id: queueId,
    title: validated.queue.title,
    item_count: validated.queue.items.length,
    artifacts_uploaded: uploaded.length,
    hint: 'Send QUEUES in Telegram to review it (or QUEUE <n> if you know its position).',
  });
}

export async function cmdQueues() {
  const chatId = process.env.TELEGRAM_CHAT_ID || null;
  let activeQueueId = null;
  if (chatId) {
    const session = await repo.getOrCreateSession(chatId);
    activeQueueId = session.queue_id;
  }
  const queues = await repo.listQueues(activeQueueId);
  if (hasFlag('json')) return outJson(queues);
  if (!queues.length) {
    process.stdout.write('No queues ingested yet.\n');
    return queues;
  }
  for (const q of queues) {
    process.stdout.write(`${q.active ? '→ ' : '  '}${q.id}  ${q.title}  (${q.item_count} role(s), ingested ${String(q.ingested_at).slice(0, 10)})\n`);
  }
  return queues;
}

export async function cmdState() {
  const chatId = requireEnv('TELEGRAM_CHAT_ID');
  const state = await repo.loadFullState(chatId);
  if (!state) {
    if (hasFlag('json')) return outJson({ status: 'empty' });
    process.stdout.write('No active queue.\n');
    return null;
  }
  if (hasFlag('json')) {
    return outJson({
      status: state.status,
      queue_id: state.queue_id,
      cursor: state.cursor,
      total: state.total,
      title: state.title,
      stats: state.stats,
      offset: state.telegram.offset,
    });
  }
  process.stdout.write(`status=${state.status} cursor=${state.cursor}/${state.total} queue=${state.queue_short} offset=${state.telegram.offset}\n`);
  return state;
}

export async function cmdReset() {
  if (!hasFlag('force')) fail('reset requires --force (abandons review progress on the active queue only — ingested queues are kept; see local README)');
  const chatId = requireEnv('TELEGRAM_CHAT_ID');
  await repo.resetSession(chatId);
  return outJson({ status: 'reset', chat_id: chatId });
}

function usage() {
  process.stdout.write(
    'Usage:\n'
    + '  node src/cli.mjs whoami\n'
    + '  node src/cli.mjs migrate\n'
    + '  node src/cli.mjs ingest --queue <path.json> [--root <path>] [--title "..."] [--dry-run]\n'
    + '  node src/cli.mjs queues [--json]\n'
    + '  node src/cli.mjs state [--json]\n'
    + '  node src/cli.mjs reset --force\n',
  );
}

async function main() {
  const cmd = process.argv[2];
  if (cmd === 'whoami') return cmdWhoami();
  if (cmd === 'migrate') return cmdMigrate();
  if (cmd === 'ingest') return cmdIngest();
  if (cmd === 'queues') return cmdQueues();
  if (cmd === 'state') return cmdState();
  if (cmd === 'reset') return cmdReset();
  usage();
  process.exit(cmd ? 1 : 0);
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main().catch((err) => fail(err?.message || String(err)));
}
