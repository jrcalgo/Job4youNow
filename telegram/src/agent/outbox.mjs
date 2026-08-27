// agent/outbox.mjs — delivers rows the agent app's telegram_outbox table
// produced. This module never constructs Telegram text or interprets
// content: every row already carries exactly what to send
// (`delivery_kind` plus either `public_payload` or `artifact_ref`), built
// entirely by the agent app's formatting layer. Its only job is fetch,
// deliver, acknowledge — the same "thin adapter" boundary the rest of this
// daemon's queue-review half predates and is unrelated to.
//
// `artifact_ref`'s `artifact` field is an agent/app/models/artifacts.py
// ArtifactLocation — `{ bucket, key, checksumSha256... }` — resolved to a
// local path via artifacts/store.mjs's getArtifactPathFromRef, which picks
// the right one of the two S3 buckets from `artifact.bucket`. This module
// never needs to know which bucket that is.
//
// job_artifact and private_artifact intentionally deliver DIFFERENTLY:
// job_artifact (public job-search PDFs/reports — see
// agent/app/models/artifacts.py's ArtifactBucket) still sends the file
// itself, same as this bot's own CV/JD queue-review path. private_artifact
// (augmented resumes, private research answers) NEVER leaves this bot as a
// downloadable file — it is always read back as text and sent as one or
// more ordinary Telegram messages instead. See deliverPrivateArtifactAsText
// below for why that's safe without a second escaping pass.
//
// deliverMessage() below is also reused, unchanged, by agent/inbound.mjs —
// the flat TelegramOutboundMessage shape a POST /telegram/update response's
// `immediate_messages` entries already are (see models/telegram.py) is
// exactly what an outbox row's `public_payload`/`artifact_ref` column
// unwraps to below. One delivery implementation, two arrival paths.
import { readFile } from 'node:fs/promises';
import { editMessageText, sendDocument, sendMessage } from '../telegram/api.mjs';
import { getArtifactPathFromRef } from '../artifacts/store.mjs';
import { escapeHtml } from '../protocol/core.mjs';
import { sleep } from '../lib/retry.mjs';
import { log } from '../lib/log.mjs';
import { acknowledgeOutboxDelivered, fetchPendingOutbox, setHubMessageId } from './client.mjs';

function toReplyMarkup(buttons) {
  if (!buttons?.length) return undefined;
  return { inline_keyboard: buttons.map((row) => row.map((b) => ({ text: b.text, callback_data: b.callback_data }))) };
}

/**
 * private_artifact rows carry user-private content (an augmented resume's
 * extracted text, a private research answer, ...) that must never be
 * handed to the user as a file — see this module's header comment. Fetches
 * the artifact (still durably stored in S3 + a local backup — see
 * agent/app/tools/artifact_store.py — that storage choice is unrelated to
 * how it's DELIVERED), reads it back as UTF-8 text, and sends it as one or
 * more plain Telegram messages via the normal sendMessage path — which
 * already chunks at 4096 chars and attaches `buttons` only to the final
 * chunk, so a long resume or research answer needs no special handling
 * here.
 *
 * `message.caption` is presenter-produced, already-safe HTML (same
 * convention as every other caption/text this adapter treats with
 * escape: false — see sendMessage's doc comment). The artifact's BYTES are
 * a different story: agent/app/tools/resume_tool.py and
 * agent/app/formatting/presenters.py's research_result both write PLAIN,
 * unescaped text/markdown to private storage on purpose — that storage
 * should stay legible on its own, independent of any one delivery
 * channel's escaping rules. This is therefore the ONE place that content
 * needs to become Telegram-HTML-safe, so — unlike every other send in
 * this module — the fetched body is escaped here, right before being
 * combined with the (already-safe) title.
 */
async function deliverPrivateArtifactAsText(token, chatId, message, { dryRun = false } = {}) {
  const title = message.caption || 'Private result';
  let body = '(dry run — artifact content not fetched)';
  if (!dryRun) {
    const localPath = await getArtifactPathFromRef(message.artifact);
    const text = (await readFile(localPath, 'utf8')).trim();
    body = escapeHtml(text || '(empty result)');
  }
  const combined = `<b>${title}</b>\n\n${body}`;
  await sendMessage(token, chatId, combined, { replyMarkup: toReplyMarkup(message.buttons), dryRun, escape: false });
}

/**
 * Deliver public_text to hub (edit) or as new message(s).
 * @returns {Promise<number|null>} new hub message id when a send created one
 */
async function deliverPublicText(token, chatId, message, { dryRun = false, hubMessageId = null, chatIdForHub = chatId } = {}) {
  const replyMarkup = toReplyMarkup(message.buttons);
  const useHub = message.delivery_target === 'hub' && hubMessageId != null;

  if (useHub) {
    try {
      await editMessageText(token, chatId, hubMessageId, message.text, { replyMarkup, dryRun, escape: false });
      return hubMessageId;
    } catch (err) {
      log.warn('hub edit failed, sending new message', { chatId, hubMessageId, error: err?.message });
    }
  }

  const results = await sendMessage(token, chatId, message.text, { replyMarkup, dryRun, escape: false });
  if (dryRun) return null;
  const last = results[results.length - 1];
  const newId = last?.message_id;
  if (message.delivery_target === 'hub' && newId != null) {
    try {
      await setHubMessageId(chatIdForHub, newId);
    } catch (err) {
      log.warn('failed to persist hub message id', { chatId: chatIdForHub, error: err?.message });
    }
  }
  return newId ?? null;
}

/**
 * Deliver one already-formatted, FLAT TelegramOutboundMessage-shaped object
 * — `{ delivery_kind, text?, buttons? }` for `public_text`, `{
 * delivery_kind, artifact, caption?, buttons? }` for `job_artifact` /
 * `private_artifact`. See this module's header comment for why
 * `private_artifact` delivers as text while `job_artifact` still delivers
 * as a document.
 *
 * `escape: false` on the public_text/job_artifact sends below — that
 * text/caption is agent-app-produced, already-final HTML (see
 * agent/app/formatting/chunking.py's to_outbound_messages(), which wraps a
 * title in a real `<b>` tag and escapes every interpolated value itself via
 * formatting/telegram_markdown.py's escape_html()). Escaping it again here
 * would turn intentional `<b>` tags into visible "&lt;b&gt;" text — see
 * telegram/api.mjs's sendMessage doc comment for the full reasoning.
 * @param {string} token
 * @param {string} chatId
 * @param {object} message
 * @param {{ dryRun?: boolean, hubMessageId?: number|null, chatIdForHub?: string }} [opts]
 */
export async function deliverMessage(token, chatId, message, { dryRun = false, hubMessageId = null, chatIdForHub = chatId } = {}) {
  if (message.delivery_kind === 'public_text') {
    await deliverPublicText(token, chatId, message, { dryRun, hubMessageId, chatIdForHub });
    return;
  }
  if (message.delivery_kind === 'private_artifact') {
    await deliverPrivateArtifactAsText(token, chatId, message, { dryRun });
    return;
  }
  const localPath = dryRun ? null : await getArtifactPathFromRef(message.artifact);
  await sendDocument(token, chatId, localPath, message.caption || '', { dryRun, escape: false });
  if (message.buttons?.length) {
    await sendMessage(token, chatId, message.caption || 'Result', { replyMarkup: toReplyMarkup(message.buttons), dryRun, escape: false });
  }
}

/** Unwraps one telegram_outbox row's kind-specific column into the flat shape deliverMessage() expects. */
function messageFromOutboxRow(row) {
  const payload = row.delivery_kind === 'public_text' ? row.public_payload : row.artifact_ref;
  return { delivery_kind: row.delivery_kind, ...payload };
}

export async function deliverOutboxRow(token, row, opts = {}) {
  await deliverMessage(token, row.chat_id, messageFromOutboxRow(row), opts);
}

/**
 * Poll the agent app's outbox forever, delivering each pending row via
 * Telegram and acknowledging it only AFTER a successful send — a crash
 * between send and ack means, at worst, a duplicate delivery on the next
 * poll, never a silently lost message. One bad row never stalls the rest
 * of the batch or the loop itself.
 * @param {string} token
 * @param {{ pollMs?: number, dryRun?: boolean, shouldStop?: () => boolean }} [opts]
 */
export async function runAgentOutboxLoop(token, { pollMs = 3000, dryRun = false, shouldStop = () => false } = {}) {
  log.info('agent outbox: poll loop starting', { pollMs });
  while (!shouldStop()) {
    try {
      const rows = (await fetchPendingOutbox()) || [];
      for (const row of rows) {
        try {
          // eslint-disable-next-line no-await-in-loop
          await deliverOutboxRow(token, row, { dryRun });
          // eslint-disable-next-line no-await-in-loop
          await acknowledgeOutboxDelivered(row.id);
        } catch (err) {
          log.error('agent outbox: delivery failed, will retry next poll', { outboxId: row.id, error: err?.message });
        }
      }
    } catch (err) {
      log.error('agent outbox: poll failed, backing off', { error: err?.message });
    }
    // eslint-disable-next-line no-await-in-loop
    await sleep(pollMs);
  }
  log.info('agent outbox: poll loop stopped');
}
