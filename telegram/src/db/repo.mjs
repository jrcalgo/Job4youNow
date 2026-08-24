// db/repo.mjs — repository layer: composes db/client.mjs's exec/execBatch/tx
// into the specific reads and writes bot.mjs and cli.mjs need. No caller
// outside this file should write raw SQL against these tables.
import { exec, execBatch, tx } from './client.mjs';
import { shortHash } from '../protocol/core.mjs';

const DEFAULT_STATS = { cvs_sent: 0, notes: 0, reviewed: 0, skipped: 0 };

// ── Ingestion (cli.mjs) ──────────────────────────────────────────────────────

/**
 * Persist a validated queue (see protocol/core.mjs's validateQueue) plus its
 * already-uploaded artifact S3 keys, in one transaction. `items` must already
 * carry `artifacts: [{ n, kind, s3Key, byteSize, checksum }]` flattened
 * separately — cli.mjs uploads to S3 BEFORE calling this, so a transaction
 * failure here never leaves a DB row pointing at a key that doesn't exist.
 * @param {{ id: string, title: string, source?: string, items: object[], artifacts: object[] }} queue
 */
export async function insertQueue(queue) {
  return tx(async (transactionId) => {
    await exec(
      `INSERT INTO queues (id, title, source, item_count)
       VALUES (:id, :title, :source, :item_count)`,
      { id: queue.id, title: queue.title, source: queue.source || null, item_count: queue.items.length },
      { transactionId },
    );

    await execBatch(
      `INSERT INTO queue_items
         (queue_id, n, item_id, report_num, company, role, url, score, location,
          salary, legitimacy, summary, contacts, can_send_cv, can_send_contacts, status)
       VALUES
         (:queue_id, :n, :item_id, :report_num, :company, :role, :url, :score, :location,
          :salary, :legitimacy, :summary::jsonb, :contacts::jsonb, :can_send_cv, :can_send_contacts, :status)`,
      queue.items.map((it) => ({
        queue_id: queue.id,
        n: it.n,
        item_id: it.id,
        report_num: it.report_num,
        company: it.company,
        role: it.role,
        url: it.url || '',
        score: it.score || '',
        location: it.location || '',
        salary: it.salary || '',
        legitimacy: it.legitimacy || '',
        summary: it.summary || {},
        contacts: it.contacts || [],
        can_send_cv: it.can_send_cv !== false,
        can_send_contacts: it.can_send_contacts !== false,
        status: 'pending',
      })),
      { transactionId },
    );

    if (queue.artifacts.length) {
      await execBatch(
        `INSERT INTO item_artifacts (queue_id, n, kind, s3_key, byte_size, checksum)
         VALUES (:queue_id, :n, :kind, :s3_key, :byte_size, :checksum)`,
        queue.artifacts.map((a) => ({
          queue_id: queue.id,
          n: a.n,
          kind: a.kind,
          s3_key: a.s3Key,
          byte_size: a.byteSize ?? null,
          checksum: a.checksum ?? null,
        })),
        { transactionId },
      );
    }

    return queue.id;
  });
}

// ── Queue listing / lookup ───────────────────────────────────────────────────

export async function listQueues(activeQueueId = null) {
  const { rows } = await exec(
    `SELECT id, title, item_count, ingested_at
       FROM queues
      ORDER BY ingested_at DESC`,
  );
  return rows.map((r) => ({ ...r, active: r.id === activeQueueId }));
}

/** Resolve a 1-based list position (as shown by formatQueueList) to a queue id. */
export async function resolveQueueByPosition(n) {
  const queues = await listQueues();
  const row = queues[n - 1];
  return row ? row.id : null;
}

async function getQueueMeta(queueId) {
  const { rows } = await exec('SELECT id, title, item_count, ingested_at FROM queues WHERE id = :id', { id: queueId });
  return rows[0] || null;
}

async function getQueueItemsWithArtifacts(queueId) {
  const [{ rows: items }, { rows: artifacts }] = await Promise.all([
    exec(
      `SELECT n, item_id AS id, report_num, company, role, url, score, location, salary,
              legitimacy, summary, contacts, can_send_cv, can_send_contacts, status
         FROM queue_items
        WHERE queue_id = :queue_id
        ORDER BY n`,
      { queue_id: queueId },
    ),
    exec('SELECT n, kind, s3_key FROM item_artifacts WHERE queue_id = :queue_id', { queue_id: queueId }),
  ]);
  const artifactsByN = new Map();
  for (const a of artifacts) {
    if (!artifactsByN.has(a.n)) artifactsByN.set(a.n, {});
    artifactsByN.get(a.n)[a.kind] = a.s3_key;
  }
  return items.map((it) => ({
    ...it,
    can_send_cv: Boolean(it.can_send_cv),
    can_send_contacts: Boolean(it.can_send_contacts),
    artifacts: artifactsByN.get(it.n) || {},
    history: [], // audit-only in session_history; not rehydrated — see protocol/core.mjs header comment
  }));
}

// ── Session lifecycle ────────────────────────────────────────────────────────

export async function getOrCreateSession(chatId) {
  await exec(
    `INSERT INTO sessions (chat_id, stats) VALUES (:chat_id, :stats::jsonb)
     ON CONFLICT (chat_id) DO NOTHING`,
    { chat_id: chatId, stats: DEFAULT_STATS },
  );
  const { rows } = await exec('SELECT * FROM sessions WHERE chat_id = :chat_id', { chat_id: chatId });
  return rows[0];
}

/**
 * Reconstruct the full in-memory state object protocol/core.mjs's
 * applyAction/format* functions operate on. Returns null if the chat has no
 * active queue (idle, or never ingested anything yet).
 */
export async function loadFullState(chatId) {
  const session = await getOrCreateSession(chatId);
  if (!session.queue_id) return null;
  const [queue, items] = await Promise.all([
    getQueueMeta(session.queue_id),
    getQueueItemsWithArtifacts(session.queue_id),
  ]);
  if (!queue) return null;
  return {
    version: 1,
    queue_id: queue.id,
    queue_short: shortHash(queue.id, 6),
    created_at: queue.ingested_at,
    updated_at: session.updated_at,
    chat_id: session.chat_id,
    telegram: {
      offset: Number(session.telegram_offset) || 0,
      last_update_id: session.last_update_id != null ? Number(session.last_update_id) : null,
      last_message_id: session.last_message_id != null ? Number(session.last_message_id) : null,
    },
    title: queue.title,
    status: session.status,
    cursor: session.cursor,
    total: items.length,
    items,
    notes: [], // audit-only in session_notes; not rehydrated — see protocol/core.mjs header comment
    stats: session.stats,
  };
}

/** Point the session at a different ingested queue. Never touches telegram_offset/last_update_id. */
export async function switchQueue(chatId, queueId) {
  await exec(
    `UPDATE sessions
        SET queue_id = :queue_id, cursor = 1, status = 'active', stats = :stats::jsonb, updated_at = now()
      WHERE chat_id = :chat_id`,
    { chat_id: chatId, queue_id: queueId, stats: DEFAULT_STATS },
  );
}

/** Abandon review progress on the active queue without deleting the ingested queue data. */
export async function resetSession(chatId) {
  await exec(
    `UPDATE sessions
        SET queue_id = NULL, cursor = 1, status = 'idle', stats = :stats::jsonb, updated_at = now()
      WHERE chat_id = :chat_id`,
    { chat_id: chatId, stats: DEFAULT_STATS },
  );
}

export async function updateSessionProgress(chatId, { cursor, status, stats, lastMessageId }) {
  await exec(
    `UPDATE sessions
        SET cursor = :cursor, status = :status, stats = :stats::jsonb,
            last_message_id = COALESCE(:last_message_id, last_message_id), updated_at = now()
      WHERE chat_id = :chat_id`,
    { chat_id: chatId, cursor, status, stats, last_message_id: lastMessageId ?? null },
  );
}

export async function updateTelegramOffset(chatId, { offset, lastUpdateId }) {
  await exec(
    `UPDATE sessions
        SET telegram_offset = :offset, last_update_id = :last_update_id, updated_at = now()
      WHERE chat_id = :chat_id`,
    { chat_id: chatId, offset, last_update_id: lastUpdateId ?? null },
  );
}

export async function getTelegramOffset(chatId) {
  const session = await getOrCreateSession(chatId);
  return Number(session.telegram_offset) || 0;
}

export async function setItemStatus(queueId, n, status) {
  await exec(
    'UPDATE queue_items SET status = :status WHERE queue_id = :queue_id AND n = :n',
    { queue_id: queueId, n, status },
  );
}

export async function appendNote(chatId, { queueId, itemId, n, text, source = 'telegram' }) {
  await exec(
    `INSERT INTO session_notes (chat_id, queue_id, item_id, n, text, source)
     VALUES (:chat_id, :queue_id, :item_id, :n, :text, :source)`,
    { chat_id: chatId, queue_id: queueId ?? null, item_id: itemId ?? null, n: n ?? null, text, source },
  );
}

export async function appendHistory(chatId, { queueId, itemId, n, action, text }) {
  await exec(
    `INSERT INTO session_history (chat_id, queue_id, item_id, n, action, text)
     VALUES (:chat_id, :queue_id, :item_id, :n, :action, :text)`,
    { chat_id: chatId, queue_id: queueId ?? null, item_id: itemId ?? null, n: n ?? null, action, text: text ?? null },
  );
}

// ── Session lease ─────────────────────────────────────────────────────────
// See db/schema.sql's header comment for why this exists. Claims the lease if
// it's free, expired, or already held by the same owner (renewal); a zero-row
// result means someone else genuinely holds a live lease.

export async function acquireLease(chatId, ownerId, ttlMs) {
  const { numberOfRecordsUpdated } = await exec(
    `UPDATE sessions
        SET lease_owner = :owner, lease_expires_at = now() + (:ttl_ms || ' milliseconds')::interval
      WHERE chat_id = :chat_id
        AND (lease_owner IS NULL OR lease_expires_at < now() OR lease_owner = :owner)`,
    { chat_id: chatId, owner: ownerId, ttl_ms: ttlMs },
  );
  return (numberOfRecordsUpdated || 0) > 0;
}

export async function releaseLease(chatId, ownerId) {
  await exec(
    `UPDATE sessions
        SET lease_owner = NULL, lease_expires_at = NULL
      WHERE chat_id = :chat_id AND lease_owner = :owner`,
    { chat_id: chatId, owner: ownerId },
  );
}
