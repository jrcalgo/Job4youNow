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

import { applyAction, currentItem, generateQueueId, withMainMenuRow, normalizeUpdate } from './protocol/core.mjs';
import { answerCallback, getUpdates, sendMessage } from './telegram/api.mjs';
import { runEffects, sendDigestAndCard, sendQueueList } from './telegram/effects.mjs';
import * as repo from './db/repo.mjs';
import { setRedactedSecrets, log } from './lib/log.mjs';
import { sleep } from './lib/retry.mjs';
import { runAgentOutboxLoop } from './agent/outbox.mjs';
import { forwardToAgent } from './agent/inbound.mjs';
import { agentCall } from './agent/client.mjs';

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

const CURSOR_JUMPING_ACTIONS = new Set(['cv', 'jd', 'contacts', 'company', 'more']);
// parseCommand()/parseCallbackData() flag these two reasons when a message
// was deliberately refused locally (a submit/injection attempt, or RESET) —
// never forward either one to the agent app, active queue or not.
const LOCAL_ONLY_REASONS = new Set(['rejected_input', 'reset_refused']);

/** Forwards to the agent app's canonical main menu (hub edit when possible). */
async function forwardMainMenuToAgent(token, normalized, chatId, dryRun) {
  await forwardToAgent(token, {
    chatId,
    updateId: normalized.update_id,
    hubMessageId: normalized.hub_message_id,
    messageId: normalized.message_id,
    callback: { action: 'main', value: null },
  }, { dryRun });
}
/** Forwards one normalized text update to the agent app — see agent/inbound.mjs. */
async function forwardTextToAgent(token, chatId, normalized, dryRun) {
  await forwardToAgent(token, {
    chatId, updateId: normalized.update_id, messageId: normalized.message_id, text: normalized.raw,
  }, { dryRun });
}

/**
 * Forwards one normalized 'app_callback' (a tapped button outside this
 * daemon's own "tg:" callback namespace — see protocol/core.mjs's
 * parseCallbackData) to the agent app as a TelegramCallback, the same way
 * agent/app/routing/intent_router.py's parse_callback would parse it had
 * the user typed the equivalent command instead of tapping a button.
 */
async function forwardCallbackToAgent(token, chatId, normalized, dryRun) {
  await forwardToAgent(token, {
    chatId,
    updateId: normalized.update_id,
    hubMessageId: normalized.hub_message_id,
    callback: { action: normalized.appAction, value: normalized.appValue },
  }, { dryRun });
}

async function handleBlEnqueue(token, chatId, normalized, dryRun) {
  const listingId = normalized.listing_id;
  try {
    const data = await agentCall('GET', `/job-listings/${encodeURIComponent(listingId)}`);
    if (!data?.ok || !data.listing) {
      await sendMessage(token, chatId, 'Could not load that listing.', {
        replyMarkup: withMainMenuRow(),
        dryRun,
      });
      return;
    }
    const listing = data.listing;
    const queueId = generateQueueId();
    const title = `Backlog: ${listing.company_name} — ${listing.title}`.slice(0, 120);
    await repo.insertQueue({
      id: queueId,
      title,
      source: `backlog:${listingId}`,
      items: [{
        n: 1,
        id: listing.id,
        report_num: null,
        company: listing.company_name,
        role: listing.role_id,
        url: listing.url || '',
        score: '',
        location: listing.location || '',
        salary: '',
        legitimacy: '',
        status: 'pending',
        artifacts: {},
        contacts: [],
        summary: { job: listing.summary || [], company: [], risks: [], why_match: [] },
        history: [],
        can_send_cv: false,
        can_send_contacts: false,
      }],
      artifacts: [],
    });
    await agentCall('PATCH', `/job-listings/${encodeURIComponent(listingId)}/status`, {
      body: { status: 'queued' },
    });
    await sendMessage(
      token,
      chatId,
      `Saved to queue ${queueId}. Send QUEUES to review (no CV/JD until full ingest).`,
      { replyMarkup: withMainMenuRow(), dryRun },
    );
  } catch (err) {
    log.error('enqueue from backlog failed', { listingId, error: err?.message });
    await sendMessage(token, chatId, 'Failed to save listing to queue.', {
      replyMarkup: withMainMenuRow(),
      dryRun,
    });
  }
}

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
  if (normalized.action === 'main') {
    await forwardMainMenuToAgent(token, normalized, chatId, dryRun);
    return state;
  }
  if (normalized.action === 'help' && normalized.source === 'callback') {
    await forwardToAgent(token, {
      chatId,
      updateId: normalized.update_id,
      hubMessageId: normalized.hub_message_id,
      callback: { action: 'help', value: null },
    }, { dryRun });
    return state;
  }
  if (normalized.action === 'bl_enqueue') {
    await handleBlEnqueue(token, chatId, normalized, dryRun);
    return state;
  }
  if (normalized.action === 'use_queue') {
    const targetId = await repo.resolveQueueByPosition(normalized.n);
    if (!targetId) {
      await sendMessage(token, chatId, `No queue at position ${normalized.n}. Send QUEUES to see the list.`, {
        replyMarkup: withMainMenuRow(),
        dryRun,
      });
      return state;
    }
    await repo.switchQueue(chatId, targetId);
    const nextState = await repo.loadFullState(chatId);
    await sendDigestAndCard(token, chatId, nextState, { dryRun });
    return nextState;
  }

  // A tapped button the agent app itself rendered (BACKLOG's role list,
  // the MODELS wizard, the main menu's Backlog/Models buttons, ...) —
  // queue-agnostic, exactly like 'queues'/'use_queue' above, since these
  // features have nothing to do with queue review. Forwarding (rather
  // than the old "malformed callback" dead end) is what actually lets a
  // user tap through the agent app's own menus from Telegram.
  if (normalized.action === 'app_callback') {
    await forwardCallbackToAgent(token, chatId, normalized, dryRun);
    return state;
  }

  if (!state) {
    if (normalized.action === 'help' && normalized.source === 'text') {
      await forwardToAgent(token, {
        chatId,
        updateId: normalized.update_id,
        messageId: normalized.message_id,
        hubMessageId: normalized.hub_message_id,
        text: '',
      }, { dryRun });
      return state;
    }
    // No active queue and this isn't HELP/QUEUES/QUEUE <n> (all handled
    // above) — every other typed message is a free-text agent query
    // (SCAN ..., BACKLOG, a research question, ...), never a queue-review
    // instruction, since there is no queue to review yet. Native command
    // WORDS (e.g. "next") parse the same way and are forwarded too — they
    // are meaningless without an active queue regardless.
    if (normalized.source === 'text' && !LOCAL_ONLY_REASONS.has(normalized.reason)) {
      await forwardTextToAgent(token, chatId, normalized, dryRun);
      return state;
    }
    // A stale/malformed callback (e.g. a button left over from a queue that
    // no longer exists) with no active queue — forward to agent main menu.
    await forwardMainMenuToAgent(token, normalized, chatId, dryRun);
    return state;
  }

  // An active queue exists, but this text didn't match any native command
  // in the closed grammar (protocol/core.mjs's parseCommand) — rather than
  // the old blanket "Unknown command" reply, treat it as a free-text agent
  // query. Deliberate submit/injection attempts and RESET stay refused
  // locally (LOCAL_ONLY_REASONS), never reaching the agent app.
  if (normalized.source === 'text' && normalized.action === 'unknown' && !LOCAL_ONLY_REASONS.has(normalized.reason)) {
    await forwardTextToAgent(token, chatId, normalized, dryRun);
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
  // JOB_ARTIFACTS_BUCKET is the current name; S3_BUCKET is the legacy one
  // artifacts/store.mjs still falls back to — accept either here so this
  // check can't reject a deployment store.mjs itself would happily serve.
  if (!process.env.JOB_ARTIFACTS_BUCKET && !process.env.S3_BUCKET) {
    log.error('missing required env var: JOB_ARTIFACTS_BUCKET (or legacy S3_BUCKET)');
    process.exit(1);
  }

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
    await agentOutboxLoop.catch((err) => log.warn('agent outbox loop ended with an error', { error: err?.message }));
    process.exit(0);
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));

  // Independent of the queue-review loop below and unrelated to it — see
  // agent/outbox.mjs's header comment. Defaults ON (see .env.example) now
  // that handleUpdate() forwards free text to the agent app — set
  // J4N_AGENT_OUTBOX_ENABLED=0 only when running with no agent app at all.
  let agentOutboxLoop = Promise.resolve();
  if (process.env.J4N_AGENT_OUTBOX_ENABLED === '1') {
    agentOutboxLoop = runAgentOutboxLoop(token, {
      pollMs: Number(process.env.J4N_AGENT_OUTBOX_POLL_MS) || 3000,
      dryRun,
      shouldStop: () => shuttingDown,
    });
  }

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
