// telegram/api.mjs — thin wrapper over the Telegram Bot API's HTTP surface.
//
// Ported from career-ops' local/telegram/telegram-session.mjs (same
// endpoints, same long-poll shape). Two deliberate changes from the original
// while porting:
//
//   1. BUG FIX: the original's tgCall() catch block unconditionally rewrapped
//      every caught error into `new Error(redactSecrets(err.message...))`
//      before rethrowing — including the error it had JUST thrown two lines
//      above with `.status`/`.retryAfter` set for a non-ok HTTP response.
//      That rewrap drops both properties, so the original's cmdWait() checks
//      for `err.status === 409` and `err.retryAfter` downstream could never
//      actually match a real Telegram error. Fixed here by rethrowing an
//      already-shaped error as-is instead of rewrapping it a second time.
//   2. sendMessage()'s per-chunk ternary in the original computed `chunk`
//      via `chunks.length > 1 && !chunks[i].startsWith('Digest ') ? (i === 0
//      && chunks.length > 1 ? chunks[i] : chunks[i]) : chunks[i]` — every
//      branch returns `chunks[i]`, so it was dead code. Simplified to
//      `chunks[i]` directly; behavior is identical.
//
// 429 (Too Many Requests) is retried transparently inside tgCall() via
// lib/retry.mjs, using Telegram's own retry_after — callers never see a 429
// unless retries are exhausted. 409 (another getUpdates consumer holding the
// token) is NOT retried here; bot.mjs's poll loop decides how to react.
import { basename } from 'node:path';
import { readFile } from 'node:fs/promises';
import { chunkMessage, escapeHtml, redactSecrets } from '../protocol/core.mjs';
import { withTelegramRetry } from '../lib/retry.mjs';

const API_HOST = 'api.telegram.org';
// A function, not a module-level const — read at CALL time so a consumer
// that imports this module once (e.g. agent/outbox.mjs) still sees a
// test's J4N_TELEGRAM_API_BASE override, however late it's set.
function apiBase() { return process.env.J4N_TELEGRAM_API_BASE || `https://${API_HOST}`; }

function apiUrl(token, method) {
  return `${apiBase()}/bot${token}/${method}`;
}

async function tgCallOnce(token, method, opts = {}) {
  const { body = null, form = null, timeoutMs = 30_000, methodHttp = 'POST' } = opts;
  const url = apiUrl(token, method);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const init = { method: methodHttp, signal: controller.signal, redirect: 'error' };
    if (form) {
      init.body = form;
    } else if (body != null) {
      init.headers = { 'Content-Type': 'application/json' };
      init.body = JSON.stringify(body);
    }
    const res = await fetch(url, init);
    const text = await res.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      throw new Error(`Telegram ${method}: non-JSON HTTP ${res.status}`);
    }
    if (!res.ok || data.ok === false) {
      const desc = data.description || text.slice(0, 200);
      const err = new Error(redactSecrets(`Telegram ${method}: HTTP ${res.status} — ${desc}`, [token]));
      err.status = res.status;
      err.retryAfter = data.parameters?.retry_after;
      throw err; // shaped + already redacted — the catch below must not rewrap this.
    }
    return data.result;
  } catch (err) {
    if (err?.status != null) throw err;
    if (err?.name === 'AbortError') {
      throw new Error(redactSecrets(`Telegram ${method}: timed out after ${timeoutMs}ms`, [token]));
    }
    throw new Error(redactSecrets(err?.message || String(err), [token]));
  } finally {
    clearTimeout(timer);
  }
}

export async function tgCall(token, method, opts = {}) {
  return withTelegramRetry(() => tgCallOnce(token, method, opts));
}

/**
 * `escape` defaults to true for the native queue-review path (career-ops
 * formatters in protocol/core.mjs produce raw plain text that has never
 * been through any HTML escaping). Pass `escape: false` ONLY for text that
 * is already final, safe HTML — i.e. agent-app-produced text, which
 * agent/app/formatting/chunking.py's to_outbound_messages() builds as real
 * markup (e.g. `<b>{title}</b>`) using its own centralized escape_html()
 * for every interpolated value. Escaping that a second time here wouldn't
 * just be redundant — it would turn intentional <b> tags into visible
 * "&lt;b&gt;" text and double-escape any already-escaped substring (e.g.
 * "&amp;" -> "&amp;amp;"). See agent/outbox.mjs's deliverMessage, the only
 * caller that passes escape: false.
 */
export async function sendMessage(token, chatId, text, { replyMarkup, dryRun = false, escape = true } = {}) {
  const chunks = chunkMessage(text, 4096);
  const results = [];
  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i];
    const payload = {
      chat_id: chatId,
      text: escape ? escapeHtml(chunk) : chunk,
      parse_mode: 'HTML',
      link_preview_options: { is_disabled: true },
      ...(replyMarkup && i === chunks.length - 1 ? { reply_markup: replyMarkup } : {}),
    };
    if (dryRun) {
      results.push({ dry_run: true, chars: payload.text.length });
      continue;
    }
    results.push(await tgCall(token, 'sendMessage', { body: payload, timeoutMs: 20_000 }));
  }
  return results;
}

/**
 * Edit an existing hub message. `escape` — see sendMessage's doc comment.
 * Returns Telegram message result or null on dry run.
 */
export async function editMessageText(token, chatId, messageId, text, { replyMarkup, dryRun = false, escape = true } = {}) {
  const payload = {
    chat_id: chatId,
    message_id: messageId,
    text: escape ? escapeHtml(text.slice(0, 4096)) : text.slice(0, 4096),
    parse_mode: 'HTML',
    link_preview_options: { is_disabled: true },
    ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
  };
  if (dryRun) return { dry_run: true, chars: payload.text.length };
  return tgCall(token, 'editMessageText', { body: payload, timeoutMs: 20_000 });
}

/** `escape` — see sendMessage's doc comment; same rule applies to `caption`. */
export async function sendDocument(token, chatId, absPath, caption, { dryRun = false, escape = true } = {}) {
  if (dryRun) return { dry_run: true, file: basename(absPath) };
  // Buffers the whole file — same as the original. Fine at CV/JD PDF scale
  // (single-digit MB); a genuinely streamed multipart body would need a
  // fetch-compatible ReadableStream body instead of the Blob-based FormData
  // used here, which isn't worth the complexity at these artifact sizes.
  const bytes = await readFile(absPath);
  const form = new FormData();
  form.append('chat_id', String(chatId));
  form.append('caption', (escape ? escapeHtml(caption) : caption).slice(0, 1024));
  form.append('parse_mode', 'HTML');
  form.append('document', new Blob([bytes]), basename(absPath));
  return tgCall(token, 'sendDocument', { form, timeoutMs: 60_000 });
}

export async function sendContact(token, chatId, contact, { dryRun = false } = {}) {
  const phone = String(contact.phone || '').trim();
  const first = String(contact.first_name || contact.name || 'Contact').trim().split(/\s+/)[0];
  const last = String(contact.last_name || '').trim();
  if (!phone) throw new Error('contact has no phone — use text contacts instead');
  const body = {
    chat_id: chatId,
    phone_number: phone,
    first_name: first,
    ...(last ? { last_name: last } : {}),
  };
  if (dryRun) return { dry_run: true, contact: first };
  return tgCall(token, 'sendContact', { body, timeoutMs: 20_000 });
}

export async function answerCallback(token, callbackQueryId, text) {
  if (!callbackQueryId) return;
  try {
    await tgCall(token, 'answerCallbackQuery', {
      body: { callback_query_id: callbackQueryId, text: text || undefined },
      timeoutMs: 10_000,
    });
  } catch { /* non-fatal — the button still visually resolves client-side */ }
}

export async function getUpdates(token, { offset, timeoutSec = 50, limitMs } = {}) {
  const urlParams = new URLSearchParams({
    timeout: String(timeoutSec),
    allowed_updates: JSON.stringify(['message', 'edited_message', 'callback_query']),
  });
  if (offset) urlParams.set('offset', String(offset));
  // Long poll: HTTP timeout must exceed Telegram's own wait timeout.
  const httpTimeout = limitMs != null
    ? Math.min(limitMs + 5_000, (timeoutSec + 10) * 1000)
    : (timeoutSec + 10) * 1000;
  return tgCall(token, `getUpdates?${urlParams}`, { methodHttp: 'GET', timeoutMs: httpTimeout });
}
