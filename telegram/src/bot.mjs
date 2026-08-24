#!/usr/bin/env node
// bot.mjs — the daemon. Long-polls Telegram forever and handles every
// update itself; this is the piece career-ops' original telegram-session.mjs
// could not be, since its `wait` command returned after the first update and
// needed an agent to re-invoke it for the next one. Continuous hosting means
// nothing may ever return "back to the agent" — this file IS the loop.
//
// State lives in Aurora (db/repo.mjs), not a local file, so a redeploy or
// crash resumes exactly where it left off. Artifacts (CV/JD) are pulled from
// S3 through a local LRU cache (artifacts/store.mjs) rather than read off a
// mounted career-ops checkout — see docs/producing-queues.md for why.
import 'dotenv/config';
import { hostname } from 'node:os';
import { randomUUID } from 'node:crypto';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync, writeFileSync } from 'node:fs';

import {
  applyAction, currentItem, HELP_TEXT, normalizeUpdate,
} from './protocol/core.mjs';
import { answerCallback, getUpdates, sendMessage } from './telegram/api.mjs';
import { runEffects, sendDigestAndCard, sendQueueList } from './telegram/effects.mjs';
import * as repo from './db/repo.mjs';
import { setRedactedSecrets, log } from './lib/log.mjs';
import { sleep } from './lib/retry.mjs';

function requireEnv(name) {
  const v = process.env[name];
  if (!v) {
    log.error(`missing required env var: ${name}`);
    process.exit(1);
  }
  return v;
}

function touchHeartbeat() {
  const path = process.env.J4N_HEARTBEAT_FILE || '/app/.cache/.heartbeat';
  try {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, String(Date.now()));
  } catch (err) {
    log.warn('heartbeat write failed', { error: err.message });
  }
}

const NO_QUEUE_HINT = 'No active queue yet. Send QUEUES to see what has been ingested, or QUEUE <n> to start one.';
const CURSOR_JUMPING_ACTIONS = new Set(['cv', 'jd', 'contacts', 'company', 'more']);

/**
 * Handle exactly one Telegram update. Returns the (possibly new/updated)
 * session state for the caller's next iteration — never throws for expected
 * error shapes (unknown command, missing artifact, etc.); those become a
 * Telegram reply instead. Unexpected errors DO propagate, so the daemon's
 * poll loop can log and move on without losing the offset it already
 * persisted (offset advances FIRST, before anything else can fail).
 *
 * Exported (unlike the rest of this file's internals) so test/bot.test.mjs
 * can drive it directly against a fake Telegram server + fake Data API
 * without spinning up the real infinite loop below.
 */
export async function handleUpdate(update, { token, chatId, dryRun, state }) {
  const normalized = normalizeUpdate(update, state, chatId);

  // Advance the offset before doing anything else that could throw — losing
  // an update to a downstream bug must never mean re-processing it forever.
  if (typeof normalized.next_offset === 'number') {
    await repo.updateTelegramOffset(chatId, { offset: normalized.next_offset, lastUpdateId: normalized.update_id });
  }

  if (normalized.action === 'unauthorized') return state;
  if (normalized.callback_query_id) await answerCallback(token, normalized.callback_query_id, 'OK');

  // 'queues' and 'use_queue' need an async DB read/write applyAction can't do
  // — see protocol/core.mjs's header comment. Handled here, always, whether
  // or not a queue is currently active.
  if (normalized.action === 'queues') {
    const queues = await repo.listQueues(state?.queue_id);
    await sendQueueList(token, chatId, queues, { dryRun });
    return state;
  }
  if (normalized.action === 'use_queue') {
    const targetId = await repo.resolveQueueByPosition(normalized.n);
    if (!targetId) {
      await sendMessage(token, chatId, `No queue at position ${normalized.n}. Send QUEUES to see the list.`, { dryRun });
      return state;
    }
    await repo.switchQueue(chatId, targetId);
    const nextState = await repo.loadFullState(chatId);
    await sendDigestAndCard(token, chatId, nextState, { dryRun });
    return nextState;
  }

  if (!state) {
    const reply = normalized.action === 'help' ? HELP_TEXT : NO_QUEUE_HINT;
    await sendMessage(token, chatId, reply, { dryRun });
    return state;
  }

  // A callback button on a non-current card (e.g. the user still has an
  // older item's card open) jumps the cursor there before the action runs —
  // ported as-is from career-ops' cmdWait().
  if (normalized.n && CURSOR_JUMPING_ACTIONS.has(normalized.action)) {
    state.cursor = normalized.n;
  }

  const beforeItem = currentItem(state);
  const applied = applyAction(state, normalized);
  state = applied.state;

  if (normalized.action === 'next' || normalized.action === 'skip') {
    const newStatus = normalized.action === 'next' ? 'reviewed' : 'skipped';
    if (beforeItem) {
      await repo.setItemStatus(state.queue_id, beforeItem.n, newStatus);
      await repo.appendHistory(chatId, { queueId: state.queue_id, itemId: beforeItem.id, n: beforeItem.n, action: newStatus });
    }
  } else if (normalized.action === 'note' && normalized.text && beforeItem) {
    await repo.appendNote(chatId, { queueId: state.queue_id, itemId: beforeItem.id, n: beforeItem.n, text: normalized.text });
    await repo.appendHistory(chatId, { queueId: state.queue_id, itemId: beforeItem.id, n: beforeItem.n, action: 'note', text: normalized.text });
  }

  if (applied.reply) await sendMessage(token, chatId, applied.reply, { dryRun });
  const { cvsSent } = await runEffects(token, chatId, state, applied.effects, { dryRun });
  if (cvsSent) state.stats.cvs_sent += cvsSent;

  await repo.updateSessionProgress(chatId, {
    cursor: state.cursor,
    status: state.status,
    stats: state.stats,
    lastMessageId: state.telegram.last_message_id,
  });

  return state;
}

async function main() {
  const token = requireEnv('TELEGRAM_BOT_TOKEN');
  const chatId = requireEnv('TELEGRAM_CHAT_ID');
  setRedactedSecrets([token]);

  requireEnv('AWS_REGION');
  requireEnv('AURORA_RESOURCE_ARN');
  requireEnv('AURORA_SECRET_ARN');
  requireEnv('S3_BUCKET');

  const ownerId = `${hostname()}:${process.pid}:${randomUUID().slice(0, 8)}`;
  const leaseTtlMs = Number(process.env.J4N_LEASE_TTL_MS) || 180_000;
  const leaseRenewMs = Number(process.env.J4N_LEASE_RENEW_MS) || 60_000;
  const pollTimeoutSec = Math.min(50, Number(process.env.J4N_POLL_TIMEOUT_SEC) || 50);
  const dryRun = process.env.J4N_DRY_RUN === '1';

  await repo.getOrCreateSession(chatId);
  const leased = await repo.acquireLease(chatId, ownerId, leaseTtlMs);
  if (!leased) {
    log.error('another instance already holds the session lease for this chat — refusing to start (avoids a Telegram 409)', { ownerId });
    process.exit(1);
  }
  log.info('session lease acquired', { ownerId, chatId });

  let shuttingDown = false;
  const renewTimer = setInterval(() => {
    repo.acquireLease(chatId, ownerId, leaseTtlMs).catch((err) => log.warn('lease renew failed', { error: err.message }));
  }, leaseRenewMs);
  const heartbeatTimer = setInterval(touchHeartbeat, 20_000);
  touchHeartbeat();

  const shutdown = async (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.info(`received ${signal}, shutting down`);
    clearInterval(renewTimer);
    clearInterval(heartbeatTimer);
    try {
      await repo.releaseLease(chatId, ownerId);
    } catch (err) {
      log.warn('release lease failed during shutdown', { error: err.message });
    }
    process.exit(0);
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));

  let state = await repo.loadFullState(chatId);
  log.info('daemon started, entering poll loop', { chatId, pollTimeoutSec, hasActiveQueue: Boolean(state) });

  while (!shuttingDown) {
    touchHeartbeat();
    // eslint-disable-next-line no-await-in-loop
    const offset = state ? state.telegram.offset : await repo.getTelegramOffset(chatId);
    let updates;
    try {
      // eslint-disable-next-line no-await-in-loop
      updates = await getUpdates(token, { offset, timeoutSec: pollTimeoutSec });
    } catch (err) {
      if (err?.status === 409) {
        log.error(
          '409 Conflict from Telegram — another getUpdates consumer is polling this bot token. '
          + 'This must be a SEPARATE token from career-ops\' own telegram mode (see docs/producing-queues.md); '
          + 'check for a duplicate instance of this daemon or a webhook set on this bot.',
        );
      } else {
        log.warn('getUpdates failed, backing off', { error: err?.message });
      }
      // eslint-disable-next-line no-await-in-loop
      await sleep(err?.status === 409 ? 10_000 : 5_000);
      continue;
    }

    for (const update of updates || []) {
      try {
        // eslint-disable-next-line no-await-in-loop
        state = await handleUpdate(update, { token, chatId, dryRun, state });
      } catch (err) {
        log.error('handleUpdate failed — offset already advanced, continuing to the next update', {
          error: err?.message, stack: err?.stack,
        });
      }
    }
  }
}

// Guarded so test/bot.test.mjs can import handleUpdate() above without
// starting the real daemon (env vars for AWS/Telegram may not even be set in
// a unit-test process).
const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main().catch((err) => {
    log.error('fatal error, exiting', { error: err?.message, stack: err?.stack });
    process.exit(1);
  });
}
