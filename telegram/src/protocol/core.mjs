// @ts-check
/**
 * protocol/core.mjs — pure helpers for the Telegram job-review protocol.
 *
 * No network I/O, no database I/O, no filesystem I/O. Everything here is a
 * deterministic function over plain objects, which is exactly what makes it
 * safe to unit-test without mocking AWS or Telegram (see test/core.test.mjs).
 *
 * Lineage: lifted from career-ops' local/telegram/telegram-core.mjs (a
 * career-ops-local customization — see that repo's local/README.md) and
 * adapted to run as a continuously-hosted daemon instead of an
 * agent-invoked-per-tap CLI:
 *   - State persistence (readState/writeState/defaultStatePath) and the
 *     mkdir-based session lock (acquireSessionLock) are gone from this file —
 *     they're replaced by db/repo.mjs (Aurora-backed session repository) and
 *     a DB-row lease, respectively. Nothing here knows Postgres exists.
 *   - assertSafeArtifactPath is kept, but only used at the ingestion boundary
 *     now (cli.mjs, validating paths under a career-ops-style root before
 *     upload to S3) — not at send time, which resolves an S3 key instead.
 *   - Two commands the original grammar had no use for are added: RESUME
 *     (the original could only be resumed by re-running a local CLI command;
 *     a hosted bot has no "local CLI" for the user to reach) and QUEUES/
 *     QUEUE <n> (the original always had exactly one queue file at a time,
 *     started by an agent; this daemon accumulates ingested queues and the
 *     user picks one from Telegram).
 *   - `applyAction` intentionally has NO case for 'queues' or 'use_queue':
 *     both need an async database read (the list of ingested queues) that a
 *     pure reducer can't do. bot.mjs intercepts those two actions itself,
 *     before calling applyAction, the same way it already intercepts a
 *     callback's item-jump before calling applyAction.
 *
 * Interactive protocol: digest -> n/M cards -> constrained commands.
 * Never auto-submits applications. Telegram replies are always data /
 * selection commands, never agent instructions — see parseCommand.
 */

import { createHash, randomBytes } from 'node:crypto';
import { normalize, relative, resolve, sep } from 'node:path';

export const TELEGRAM_MAX_MESSAGE = 4096;
export const ALLOWED_ARTIFACT_PREFIXES = ['output/', 'reports/', 'jds/', 'batch/'];
export const ACTIONS = Object.freeze([
  'next', 'skip', 'more', 'cv', 'jd', 'contacts', 'company', 'note',
  'pause', 'resume', 'list', 'queues', 'use_queue', 'help', 'jump',
  'app_callback', 'unknown', 'stale', 'unauthorized',
]);

// Buttons the main menu shows for agent-app features that need no typed
// argument — mirrors agent/app/routing/intent_router.py's recognized
// callback actions (`backlog`, and every `_MODEL_CONFIG_ACTIONS` entry via
// `modelmenu`). None of these are in this daemon's own "tg:" callback
// namespace — see parseCallbackData's app_callback fallback below for why
// that's exactly what makes them forwardable.
const APP_MENU_BUTTONS = [
  { text: 'Backlog', callback_data: 'backlog' },
  { text: 'Models', callback_data: 'modelmenu' },
];

const FORBIDDEN_SUBMIT = /\b(submit|apply\s+now|send\s+application|click\s+apply)\b/i;

// Shared by applyAction's 'help' case AND bot.mjs's no-active-queue path
// (HELP has to work even when nothing has been ingested yet), so there is
// exactly one place this text is written.
export const HELP_TEXT = [
  'Commands: NEXT, SKIP, CV, JD, CONTACTS, COMPANY, MORE, NOTE <text>, LIST, QUEUES, QUEUE <n>, HELP, PAUSE, RESUME, or a number to jump.',
  'Buttons do the same. Nothing here submits an application.',
  'Anything else you type (a question, SCAN <role>, BACKLOG, MODELS, resume help, ...) goes to the agent app instead.',
].join('\n');

/**
 * Shown on /start, HELP, or whenever bot.mjs has no active queue and the
 * update isn't free text (e.g. a stale button) — the menu-first surface for
 * native commands. Deliberately separate from HELP_TEXT (shown mid-review,
 * where a full command list is more useful than a 2-button menu) — see
 * bot.mjs's dispatch for exactly when each is used.
 *
 * The agent-app command list below is kept in sync BY HAND with
 * agent/app/formatting/presenters.py's help() and
 * agent/app/routing/intent_router.py's actual prefixes (not every command
 * presenters.help() documents is wired up yet — SCHEDULE, notably, isn't —
 * so this only lists ones that really route correctly today).
 */
export const MAIN_MENU_TEXT = [
  'Welcome to Job4youNow. Tap a button below, or type a command:',
  '',
  'Backlog — current job backlog by role (or type BACKLOG)',
  'Models — choose which Cursor SDK model each task uses (or type MODELS)',
  'Queues — switch between ingested job-review queues',
  'Help — full command list',
  '',
  'Typed only — no button for these yet:',
  'SCAN <role> <query> — start a scan',
  'STATUS <task id> — check a task\'s progress',
  'RESUME <role> :: <job description> — tailor your resume',
  'Or just ask the agent app a question in plain text.',
].join('\n');

/**
 * Main menu for the no-active-queue screen — there is no queue/item to bind
 * callback_data to yet. Mixes two callback namespaces on purpose:
 * "tg:menu:<action>" (this daemon's own, queue-independent actions —
 * QUEUES/HELP) and bare "<action>[:<value>]" (the agent app's own
 * convention — Backlog/Models). parseCallbackData tells the two apart by
 * the "tg:" prefix alone, so bot.mjs can route each to the right place
 * without this keyboard needing to know how that dispatch works.
 */
export function mainMenuKeyboard() {
  return {
    inline_keyboard: [
      [
        { text: 'Backlog', callback_data: 'backlog' },
        { text: 'Settings', callback_data: 'settingsmenu' },
      ],
      [
        { text: 'Models', callback_data: 'modelmenu' },
        { text: 'Queues', callback_data: 'tg:menu:queues' },
      ],
      [{ text: 'Help', callback_data: 'tg:menu:help' }],
    ],
  };
}

/** Prepend a Main menu row to any inline keyboard (telegram-native sends). */
export function withMainMenuRow(keyboard) {
  const rows = keyboard?.inline_keyboard || [];
  return { inline_keyboard: [[{ text: 'Main menu', callback_data: 'tg:menu:main' }], ...rows] };
}

/** Escape HTML for Telegram parse_mode=HTML. */
export function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Redact secrets (bot tokens, DB creds, etc.) from any string before logging/replying. */
export function redactSecrets(value, secrets = []) {
  let out = String(value ?? '');
  for (const s of secrets) {
    if (typeof s === 'string' && s.length >= 6) out = out.split(s).join('«redacted»');
  }
  return out;
}

/**
 * Chunk text for Telegram, preferring paragraph/bullet boundaries.
 * @param {string} text
 * @param {number} [limit]
 * @param {string} [labelPrefix] e.g. "Digest" -> "Digest 1/2"
 */
export function chunkMessage(text, limit = TELEGRAM_MAX_MESSAGE, labelPrefix = '') {
  const raw = String(text ?? '').trim();
  if (!raw) return [''];
  if (raw.length <= limit) return [raw];

  /** @type {string[]} */
  const parts = [];
  let rest = raw;
  while (rest.length > limit) {
    const window = rest.slice(0, limit);
    let cut = Math.max(
      window.lastIndexOf('\n\n'),
      window.lastIndexOf('\n• '),
      window.lastIndexOf('\n- '),
      window.lastIndexOf('\n'),
      window.lastIndexOf(' '),
    );
    if (cut < Math.floor(limit * 0.4)) cut = limit;
    parts.push(rest.slice(0, cut).trimEnd());
    rest = rest.slice(cut).trimStart();
  }
  if (rest) parts.push(rest);

  if (!labelPrefix || parts.length <= 1) return parts;
  return parts.map((p, i) => `${labelPrefix} ${i + 1}/${parts.length}\n\n${p}`);
}

function oneLine(s) {
  return String(s ?? '').replace(/\s*\n\s*/g, ' ').trim();
}

/**
 * Short, stable, non-secret hash used to bind inline-keyboard callback_data to
 * a specific queue/item without leaking full ids into the 64-byte callback
 * payload. Exported (unlike the original private helper) because db/repo.mjs
 * needs to recompute the identical value when reconstructing session state
 * from Aurora after a restart — old inline-keyboard buttons a user still has
 * on screen must keep validating against the same queue_short/item hash.
 */
export function shortHash(input, len = 6) {
  return createHash('sha256').update(String(input)).digest('hex').slice(0, len);
}

/**
 * Validate queue JSON before ingesting it. Artifact paths are checked against
 * `root` at THIS point only — cli.mjs's ingest command is the sole caller,
 * passing the producer's root (e.g. a career-ops checkout) so relative paths
 * like "output/042-acme-cv.pdf" resolve the way the producer intended. After
 * ingest, artifacts live in S3 and are addressed by key, not by this path.
 * @param {any} queue
 * @param {{ root?: string }} [opts]
 * @returns {{ ok: true, queue: object } | { ok: false, error: string }}
 */
export function validateQueue(queue, opts = {}) {
  const root = resolve(opts.root || process.cwd());
  if (!queue || typeof queue !== 'object' || Array.isArray(queue)) {
    return { ok: false, error: 'queue must be a JSON object' };
  }
  const items = queue.items;
  if (!Array.isArray(items) || items.length === 0) {
    return { ok: false, error: 'queue.items must be a non-empty array' };
  }
  const ids = new Set();
  /** @type {object[]} */
  const normalized = [];
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (!it || typeof it !== 'object') {
      return { ok: false, error: `items[${i}] must be an object` };
    }
    const company = oneLine(it.company);
    const role = oneLine(it.role);
    if (!company || !role) {
      return { ok: false, error: `items[${i}] requires company and role` };
    }
    const id = oneLine(it.id) || (it.report_num ? `report:${String(it.report_num).padStart(3, '0')}` : `item:${i + 1}`);
    if (ids.has(id)) return { ok: false, error: `duplicate item id: ${id}` };
    ids.add(id);

    const artifacts = {};
    const artIn = (it.artifacts && typeof it.artifacts === 'object') ? it.artifacts : {};
    for (const [k, v] of Object.entries(artIn)) {
      if (typeof v !== 'string' || !v.trim()) continue;
      const check = assertSafeArtifactPath(v, root);
      if (!check.ok) return { ok: false, error: `items[${i}].artifacts.${k}: ${check.error}` };
      artifacts[k] = check.rel;
    }

    const summary = (it.summary && typeof it.summary === 'object') ? it.summary : {};
    const asList = (x) => Array.isArray(x) ? x.map(oneLine).filter(Boolean) : (x ? [oneLine(x)] : []);

    normalized.push({
      n: i + 1,
      id,
      report_num: it.report_num != null ? String(it.report_num).padStart(3, '0') : null,
      company,
      role,
      url: typeof it.url === 'string' ? it.url.trim() : '',
      score: it.score != null ? oneLine(it.score) : '',
      location: it.location != null ? oneLine(it.location) : '',
      salary: it.salary != null ? oneLine(it.salary) : '',
      legitimacy: it.legitimacy != null ? oneLine(it.legitimacy) : '',
      status: 'pending',
      artifacts,
      contacts: Array.isArray(it.contacts) ? it.contacts : [],
      summary: {
        job: asList(summary.job),
        company: asList(summary.company),
        risks: asList(summary.risks),
        why_match: asList(summary.why_match || summary.whyMatch),
      },
      history: [],
      can_send_cv: it.can_send_cv !== false,
      can_send_contacts: it.can_send_contacts !== false,
    });
  }

  return {
    ok: true,
    queue: {
      title: oneLine(queue.title) || 'Jobs under review',
      items: normalized,
    },
  };
}

/**
 * Ensure a path stays inside `root` and under an allowed prefix. Used only at
 * ingestion (see validateQueue above) — never at send time.
 * @param {string} relOrAbs
 * @param {string} root
 */
export function assertSafeArtifactPath(relOrAbs, root) {
  const absRoot = resolve(root);
  const abs = resolve(absRoot, relOrAbs);
  const rel = relative(absRoot, abs);
  if (!rel || rel.startsWith('..') || rel.includes(`..${sep}`) || normalize(rel).startsWith('..')) {
    return { ok: false, error: 'path escapes repository root' };
  }
  const posix = rel.split(sep).join('/');
  if (!ALLOWED_ARTIFACT_PREFIXES.some((p) => posix === p.slice(0, -1) || posix.startsWith(p))) {
    return { ok: false, error: `path not under allowed dirs (${ALLOWED_ARTIFACT_PREFIXES.join(', ')})` };
  }
  return { ok: true, rel: posix, abs };
}

/**
 * Deterministic-shaped, collision-resistant id for a queue — cli.mjs's
 * ingest command calls this directly (queues now exist independently of any
 * review session); buildSessionState below reuses it too, so the two never
 * drift onto two different id formats.
 */
export function generateQueueId(now = new Date().toISOString()) {
  return `tg-${now.replace(/[:.]/g, '-').replace(/Z$/, '')}-${randomBytes(3).toString('hex')}`;
}

/**
 * Build a fresh in-memory session state from a validated queue. Same shape
 * db/repo.mjs's loadFullState() reconstructs from Aurora after a restart, so
 * every function below can stay agnostic to where the state actually lives.
 * @param {object} validatedQueue
 * @param {{ chatId: string, now?: string }} opts
 */
export function buildSessionState(validatedQueue, opts) {
  const now = opts.now || new Date().toISOString();
  const queueId = generateQueueId(now);
  const items = validatedQueue.items.map((it) => ({ ...it }));
  return {
    version: 1,
    queue_id: queueId,
    queue_short: shortHash(queueId, 6),
    created_at: now,
    updated_at: now,
    chat_id: String(opts.chatId),
    telegram: {
      offset: 0,
      last_update_id: null,
      last_message_id: null,
    },
    title: validatedQueue.title,
    status: 'active',
    cursor: 1,
    total: items.length,
    items,
    notes: [],
    stats: { cvs_sent: 0, notes: 0, reviewed: 0, skipped: 0 },
  };
}

export function currentItem(state) {
  if (!state?.items?.length) return null;
  const n = Number(state.cursor);
  return state.items.find((it) => it.n === n) || null;
}

/** Build opening digest text (plain; escape later). */
export function formatDigest(state) {
  const lines = [
    `career-ops Telegram review — ${state.title}`,
    `${state.total} role(s). Queue ${state.queue_short}.`,
    '',
    'Commands: NEXT · SKIP · CV · JD · CONTACTS · COMPANY · MORE · NOTE <text> · LIST · QUEUES · HELP · PAUSE · or a number to jump.',
    'Nothing here submits an application.',
    '',
  ];
  for (const it of state.items) {
    const bits = [
      `${it.n}/${state.total}`,
      it.report_num ? `#${it.report_num}` : null,
      it.company,
      '—',
      it.role,
    ].filter(Boolean);
    if (it.score) bits.push(`· ${it.score}`);
    lines.push(`• ${bits.join(' ')}`);
    const detail = [];
    if (it.location) detail.push(it.location);
    if (it.salary) detail.push(it.salary);
    if (it.summary.why_match[0]) detail.push(`fit: ${it.summary.why_match[0]}`);
    if (it.summary.risks[0]) detail.push(`watch: ${it.summary.risks[0]}`);
    if (detail.length) lines.push(`  ${detail.join(' · ')}`);
  }
  lines.push('');
  lines.push('Reply NEXT to begin, send a number to jump, or PAUSE to stop.');
  return lines.join('\n');
}

/** Build n/M job card (plain). */
export function formatJobCard(state, item = currentItem(state)) {
  if (!item) return 'No current item.';
  const head = [
    `${item.n}/${state.total}`,
    item.report_num ? `#${item.report_num}` : null,
    item.company,
    '—',
    item.role,
  ].filter(Boolean).join(' ');

  const lines = [head, ''];
  if (item.score || item.location || item.salary) {
    lines.push([item.score, item.location, item.salary].filter(Boolean).join(' · '));
    lines.push('');
  }
  if (item.summary.why_match.length) {
    lines.push('Fit:');
    for (const b of item.summary.why_match.slice(0, 4)) lines.push(`- ${b}`);
    lines.push('');
  }
  if (item.summary.risks.length) {
    lines.push('Watch:');
    for (const b of item.summary.risks.slice(0, 3)) lines.push(`- ${b}`);
    lines.push('');
  }
  if (item.summary.job.length) {
    lines.push('Role:');
    for (const b of item.summary.job.slice(0, 4)) lines.push(`- ${b}`);
    lines.push('');
  }
  lines.push('Available: CV · JD · CONTACTS · COMPANY · MORE · NOTE <text> · SKIP · NEXT · PAUSE');
  lines.push('Review only — not submitted.');
  return lines.join('\n');
}

export function formatCompanySummary(item) {
  const lines = [`Company — ${item.company}`, ''];
  const bullets = item.summary.company.length ? item.summary.company : ['No company summary in queue.'];
  for (const b of bullets) lines.push(`- ${b}`);
  if (item.summary.risks.length) {
    lines.push('', 'Risks / unknowns:');
    for (const b of item.summary.risks) lines.push(`- ${b}`);
  }
  return lines.join('\n');
}

export function formatMore(item) {
  const lines = [`More — ${item.company} / ${item.role}`, ''];
  for (const b of item.summary.job) lines.push(`- ${b}`);
  for (const b of item.summary.why_match) lines.push(`- Fit: ${b}`);
  for (const b of item.summary.risks) lines.push(`- Watch: ${b}`);
  if (item.url) lines.push('', `URL: ${item.url}`);
  if (lines.length <= 3) lines.push('No additional detail in the queue file.');
  return lines.join('\n');
}

export function formatContactsText(item) {
  const contacts = Array.isArray(item.contacts) ? item.contacts : [];
  if (!contacts.length) return `No contacts queued for ${item.company}.`;
  const lines = [`Contacts — ${item.company}`, ''];
  contacts.forEach((c, i) => {
    const name = oneLine(c.name || c.full_name || `Contact ${i + 1}`);
    const title = oneLine(c.title || c.role || '');
    const bits = [name];
    if (title) bits.push(title);
    if (c.email) bits.push(String(c.email));
    if (c.linkedin || c.url) bits.push(String(c.linkedin || c.url));
    if (c.phone) bits.push(String(c.phone));
    lines.push(`${i + 1}. ${bits.join(' · ')}`);
  });
  lines.push('', 'Contacts only — no outreach sent.');
  return lines.join('\n');
}

/**
 * Render the list of ingested queues for the QUEUES command. Selection
 * ("QUEUE <n>" / "USE <n>") addresses rows by their position in THIS list,
 * so bot.mjs must format and interpret against the exact same ordering
 * db/repo.mjs's listQueues() returns (newest first).
 * @param {Array<{id: string, title: string, item_count: number, ingested_at: string, active?: boolean}>} queues
 */
export function formatQueueList(queues) {
  if (!queues.length) return 'No queues ingested yet.';
  const lines = ['Ingested queues:', ''];
  queues.forEach((q, i) => {
    const mark = q.active ? '→ ' : '  ';
    const when = String(q.ingested_at || '').slice(0, 10);
    lines.push(`${mark}${i + 1}. ${q.title} (${q.item_count} role(s), ${when})`);
  });
  lines.push('', 'Reply QUEUE <n> (or USE <n>) to switch.');
  return lines.join('\n');
}

// Caps the QUEUES keyboard so a chat with many ingested queues never sends
// an unwieldy wall of buttons — formatQueueList's own text listing above
// is still complete regardless; only the tappable shortcut is capped.
const MAX_QUEUE_BUTTONS = 10;

/**
 * One button per ingested queue, addressed by list position via the
 * native "tg:queue:<n>" callback_data (see parseCallbackData) — so QUEUES
 * gives the user something to tap instead of requiring them to type
 * "QUEUE <n>" from memory. Mirrors formatQueueList's exact ordering, since
 * both render the same `queues` array from db/repo.mjs's listQueues().
 * @param {Array<{id: string, title: string}>} queues
 */
export function queueListKeyboard(queues) {
  if (!queues.length) return undefined;
  return {
    inline_keyboard: queues.slice(0, MAX_QUEUE_BUTTONS).map((q, i) => [{
      text: `${i + 1}. ${q.title}`.slice(0, 48),
      callback_data: `tg:queue:${i + 1}`,
    }]),
  };
}

export function inlineKeyboard(state, item = currentItem(state)) {
  if (!item) return undefined;
  const q = state.queue_short;
  const k = shortHash(item.id, 6);
  const btn = (label, action) => ({
    text: label,
    callback_data: `tg:v1:${q}:${k}:${action}`.slice(0, 64),
  });
  return {
    inline_keyboard: [
      [{ text: 'Main menu', callback_data: 'tg:menu:main' }],
      [btn('Next', 'next'), btn('Skip', 'skip'), btn('More', 'more')],
      [btn('CV', 'cv'), btn('JD', 'jd'), btn('Company', 'company')],
      [btn('Contacts', 'contacts'), btn('Help', 'help'), btn('Pause', 'pause')],
      [btn('List', 'list'), btn('Queues', 'queues')],
    ],
  };
}

/**
 * Parse free-text Telegram commands into a normalized action.
 * Closed grammar — never treat free text as agent instructions.
 * @param {string} text
 */
export function parseCommand(text) {
  const raw = String(text ?? '').trim();
  if (!raw) return { action: 'unknown', raw };

  // Strip leading slash commands like /next or /start@BotName
  let stripped = raw.replace(/^\/+/, '');
  if (stripped.includes('@')) {
    stripped = stripped.split('@')[0];
  }
  const upper = stripped.toUpperCase();

  if (upper === 'NEXT' || upper === 'N') return { action: 'next', raw };
  if (upper === 'SKIP' || upper === 'S') return { action: 'skip', raw };
  if (upper === 'MORE' || upper === 'M') return { action: 'more', raw };
  if (upper === 'CV' || upper === 'RESUME_CV' || upper === 'PDF') return { action: 'cv', raw };
  if (upper === 'JD' || upper === 'JOB' || upper === 'POSTING') return { action: 'jd', raw };
  if (upper === 'CONTACTS' || upper === 'WHO' || upper === 'CONTACT') return { action: 'contacts', raw };
  if (upper === 'COMPANY' || upper === 'CO') return { action: 'company', raw };
  if (upper === 'LIST' || upper === 'DIGEST') return { action: 'list', raw };
  if (upper === 'QUEUES' || upper === 'QUEUE') return { action: 'queues', raw };
  if (upper === 'HELP' || upper === '?' || upper === 'H' || upper === 'START') return { action: 'help', raw };
  if (upper === 'PAUSE' || upper === 'STOP') return { action: 'pause', raw };
  if (upper === 'RESUME' || upper === 'CONTINUE') return { action: 'resume', raw };
  if (upper === 'RESET') return { action: 'unknown', raw, reason: 'reset_refused' };

  const useQueue = /^(QUEUE|USE)\s+(\d{1,3})$/i.exec(stripped);
  if (useQueue) {
    return { action: 'use_queue', n: Number(useQueue[2]), raw };
  }

  const note = /^(NOTE|N:)\s+(.+)$/is.exec(stripped);
  if (note) {
    return { action: 'note', text: note[2].trim(), raw };
  }

  if (/^\d{1,3}$/.test(stripped)) {
    return { action: 'jump', n: Number(stripped), raw };
  }

  // Injection / submit attempts -> unknown (never instructions)
  if (FORBIDDEN_SUBMIT.test(raw) || /ignore\s+(all\s+)?(previous|prior)/i.test(raw)) {
    return { action: 'unknown', raw, reason: 'rejected_input' };
  }

  return { action: 'unknown', raw };
}

/**
 * Parse callback_data from inline keyboard. `state` may be null/undefined —
 * bot.mjs calls this even when the chat has no active queue (e.g. Telegram
 * still shows a button from a session that was since abandoned), and that
 * must resolve to 'stale', not throw.
 * @param {string} data
 * @param {object|null} state
 */
export function parseCallbackData(data, state) {
  const raw = String(data ?? '');

  // Queue-independent main-menu buttons (mainMenuKeyboard()) — no queue/item
  // to bind to, so these skip the item-binding checks below entirely and
  // work identically whether or not a queue is currently active.
  const menuMatch = /^tg:menu:([a-z_]+)$/.exec(raw);
  if (menuMatch) {
    const [, action] = menuMatch;
    const allowedMenu = new Set(['queues', 'help', 'main']);
    if (!allowedMenu.has(action)) return { action: 'unknown', raw, reason: 'bad_action' };
    return { action, raw };
  }

  const enqueueMatch = /^tg:bl:enqueue:(.+)$/.exec(raw);
  if (enqueueMatch) {
    return { action: 'bl_enqueue', listing_id: enqueueMatch[1], raw };
  }

  // QUEUES list buttons (queueListKeyboard()) — same "queue-independent,
  // no item to bind to" shape as tg:menu:, but carries a list position
  // instead of a fixed action name. Reuses the exact 'use_queue' action
  // bot.mjs already handles for the typed "QUEUE <n>" / "USE <n>" commands.
  const queueMatch = /^tg:queue:(\d{1,3})$/.exec(raw);
  if (queueMatch) {
    return { action: 'use_queue', n: Number(queueMatch[1]), raw };
  }

  const m = /^tg:v1:([a-f0-9]{6}):([a-f0-9]{6}):([a-z]+)$/.exec(raw);
  if (m) {
    if (!state) return { action: 'stale', raw, reason: 'no_active_session' };
    const [, qShort, itemKey, action] = m;
    if (qShort !== state.queue_short) {
      return { action: 'stale', raw, reason: 'queue_mismatch' };
    }
    const item = state.items.find((it) => shortHash(it.id, 6) === itemKey);
    if (!item) return { action: 'stale', raw, reason: 'item_mismatch' };
    const allowed = new Set(['next', 'skip', 'more', 'cv', 'jd', 'company', 'contacts', 'help', 'pause', 'list', 'queues']);
    if (!allowed.has(action)) return { action: 'unknown', raw, reason: 'bad_action' };
    return { action, itemId: item.id, n: item.n, raw };
  }

  // Everything else falls outside this daemon's own "tg:"-namespaced
  // callback grammar entirely — which, BY DESIGN, is exactly the signal
  // that a button belongs to the agent app's own callback_data convention
  // instead (agent/app/routing/intent_router.py's parse_callback:
  // "action" or "action:value" — e.g. "role:backend", "modeltask:scan_role",
  // "backlog"). Previously every one of these was misclassified as
  // 'unknown'/malformed_callback, so tapping a button the agent app itself
  // rendered (BACKLOG's role list, the MODELS wizard, ...) was a dead end —
  // bot.mjs now forwards 'app_callback' to the agent app the same way it
  // already forwards unrecognized free text.
  //
  // Still rejected as 'unknown' rather than forwarded: anything starting
  // with "tg:" (reserved for this daemon, so a typo there should never
  // silently leak to the agent app) and anything whose action portion
  // isn't a plausible identifier (letters/digits/underscores only) — e.g.
  // a stray "not-a-callback" string, which is never a shape either side of
  // this system actually produces.
  if (raw.startsWith('tg:')) return { action: 'unknown', raw, reason: 'malformed_callback' };
  const appMatch = /^([a-z][a-z0-9_]*)(?::(.*))?$/i.exec(raw);
  if (!appMatch) return { action: 'unknown', raw, reason: 'malformed_callback' };
  const [, appAction, appValue] = appMatch;
  return { action: 'app_callback', appAction, appValue: appValue ?? null, raw };
}

/**
 * Check whether an update is from the allowlisted chat.
 * @param {any} update
 * @param {string} allowedChatId
 */
export function isAuthorizedUpdate(update, allowedChatId) {
  const want = String(allowedChatId);
  const msg = update?.message || update?.edited_message;
  if (msg) {
    const chatId = msg.chat?.id;
    const fromId = msg.from?.id;
    if (String(chatId) !== want) return false;
    if (fromId != null && String(fromId) !== want && String(chatId) !== want) return false;
    return true;
  }
  const cb = update?.callback_query;
  if (cb) {
    const chatId = cb.message?.chat?.id;
    const fromId = cb.from?.id;
    if (chatId != null && String(chatId) !== want) return false;
    if (fromId != null && String(fromId) !== want) return false;
    return chatId != null || fromId != null;
  }
  return false;
}

/**
 * Normalize a Telegram update into an action for the daemon.
 * The caller (bot.mjs) is responsible for persisting `next_offset` — this
 * function has no database access.
 * @param {any} update
 * @param {object} state
 * @param {string} allowedChatId
 */
export function normalizeUpdate(update, state, allowedChatId) {
  const updateId = update?.update_id;
  const base = {
    update_id: updateId,
    next_offset: typeof updateId === 'number' ? updateId + 1 : state?.telegram?.offset,
  };

  if (!isAuthorizedUpdate(update, allowedChatId)) {
    return { ...base, action: 'unauthorized', status: 'rejected' };
  }

  if (update.callback_query) {
    const parsed = parseCallbackData(update.callback_query.data, state);
    return {
      ...base,
      status: 'ok',
      source: 'callback',
      callback_query_id: update.callback_query.id,
      hub_message_id: update.callback_query.message?.message_id,
      ...parsed,
    };
  }

  const text = update.message?.text || update.edited_message?.text || '';
  const parsed = parseCommand(text);
  return {
    ...base,
    status: 'ok',
    source: 'text',
    message_id: update.message?.message_id || update.edited_message?.message_id,
    ...parsed,
  };
}

/**
 * Apply a deterministic action to session state (no network, no database).
 * Returns { state, reply, effects }. Effects are markers for the caller to
 * execute (send a message/document, or — for 'queues'/'use_queue', which
 * never reach this function — a database read); see the module header for
 * why 'queues' and 'use_queue' are handled by bot.mjs instead.
 * @param {object} state
 * @param {object} action
 */
export function applyAction(state, action) {
  const now = new Date().toISOString();
  const next = structuredClone(state);
  next.updated_at = now;
  /** @type {object[]} */
  const effects = [];
  /** @type {string|null} */
  let reply = null;

  const item = currentItem(next);

  const advance = (status) => {
    if (!item) return;
    item.status = status;
    item.history.push({ at: now, action: status });
    if (status === 'reviewed') next.stats.reviewed += 1;
    if (status === 'skipped') next.stats.skipped += 1;
    if (next.cursor < next.total) {
      next.cursor += 1;
      effects.push({ type: 'send_card' });
    } else {
      next.status = 'complete';
      effects.push({ type: 'send_completion' });
    }
  };

  switch (action.action) {
    case 'next':
      advance('reviewed');
      reply = null;
      break;
    case 'skip':
      advance('skipped');
      reply = null;
      break;
    case 'jump': {
      const n = Number(action.n);
      if (!Number.isInteger(n) || n < 1 || n > next.total) {
        reply = `Jump out of range. Use 1–${next.total}.`;
      } else {
        next.cursor = n;
        effects.push({ type: 'send_card' });
      }
      break;
    }
    case 'pause':
      next.status = 'paused';
      reply = 'Paused. Send RESUME when you want to continue.';
      break;
    case 'resume':
      if (next.status !== 'paused') {
        reply = next.status === 'complete' ? 'Session already complete. Send QUEUES to start another.' : 'Not paused.';
      } else {
        next.status = 'active';
        effects.push({ type: 'send_card' });
      }
      break;
    case 'note': {
      const text = oneLine(action.text || '');
      if (!text) {
        reply = 'Usage: NOTE <your note>';
        break;
      }
      next.notes.push({ at: now, item_id: item?.id || null, n: item?.n || null, text, source: 'telegram' });
      if (item) item.history.push({ at: now, action: 'note', text });
      next.stats.notes += 1;
      reply = 'Note saved locally (not an instruction).';
      break;
    }
    case 'cv':
      effects.push({ type: 'send_cv', itemId: item?.id });
      break;
    case 'jd':
      effects.push({ type: 'send_jd', itemId: item?.id });
      break;
    case 'contacts':
      effects.push({ type: 'send_contacts', itemId: item?.id });
      break;
    case 'company':
      effects.push({ type: 'send_company', itemId: item?.id });
      break;
    case 'more':
      effects.push({ type: 'send_more', itemId: item?.id });
      break;
    case 'list':
      effects.push({ type: 'send_digest' });
      break;
    case 'help':
      reply = HELP_TEXT;
      break;
    case 'stale':
      reply = 'That button is from an older queue. Use LIST or wait for the current card.';
      break;
    case 'unauthorized':
      reply = null;
      break;
    default:
      reply = 'Unknown command. Send HELP for options. Free text is not treated as instructions — use NOTE <text> to save a note.';
  }

  return { state: next, reply, effects };
}

export function formatCompletion(state) {
  return [
    `Session complete — ${state.title}`,
    `Reviewed: ${state.stats.reviewed} · Skipped: ${state.stats.skipped} · Notes: ${state.stats.notes} · CVs sent: ${state.stats.cvs_sent}`,
    'Send QUEUES to review another ingested batch. Telegram never submits an application.',
  ].join('\n');
}
