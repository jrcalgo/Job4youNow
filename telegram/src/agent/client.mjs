// agent/client.mjs — thin HTTP client for the agent app's OWN API. Not to
// be confused with telegram/api.mjs, which talks to Telegram's Bot API —
// this talks to the Python agent app (agent/app/main.py), the sole owner
// of Aurora and both S3 buckets. This daemon holds no Aurora/S3 write
// credentials of its own for that data; see the "Telegram writes no DB"
// boundary the agent app's docs describe.
import { withRetry } from '../lib/retry.mjs';
import { redactSecrets } from '../protocol/core.mjs';

function apiBase() {
  return process.env.AGENT_API_BASE || 'http://agent:8000';
}

async function agentCallOnce(method, path, { body, timeoutMs = 20_000 } = {}) {
  const url = `${apiBase()}${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const init = { method, signal: controller.signal };
    if (body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' };
      init.body = JSON.stringify(body);
    }
    const res = await fetch(url, init);
    const text = await res.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        throw new Error(`agent api ${method} ${path}: non-JSON HTTP ${res.status}`);
      }
    }
    if (!res.ok) {
      const err = new Error(`agent api ${method} ${path}: HTTP ${res.status} — ${text.slice(0, 200)}`);
      err.status = res.status;
      throw err;
    }
    return data;
  } catch (err) {
    if (err?.status != null) throw err;
    if (err?.name === 'AbortError') throw new Error(`agent api ${method} ${path}: timed out after ${timeoutMs}ms`);
    throw new Error(redactSecrets(err?.message || String(err)));
  } finally {
    clearTimeout(timer);
  }
}

/** Retries anything that isn't a clean 4xx (a 4xx means "ask again differently", not "try again"). */
export async function agentCall(method, path, opts = {}) {
  return withRetry(() => agentCallOnce(method, path, opts), {
    maxAttempts: 4,
    label: 'agent api',
    isRetryable: (err) => err?.status == null || err.status >= 500,
  });
}

export async function fetchPendingOutbox(limit = 50) {
  return agentCall('GET', `/telegram/outbox?limit=${limit}`);
}

export async function acknowledgeOutboxDelivered(outboxId) {
  return agentCall('POST', `/telegram/outbox/${encodeURIComponent(outboxId)}/delivered`);
}

export async function setHubMessageId(chatId, hubMessageId) {
  return agentCall('POST', '/telegram/transport/hub', {
    body: { chat_id: chatId, hub_message_id: hubMessageId },
  });
}

/**
 * Forward one normalized Telegram update to the agent app's sole ingress
 * point — see agent/app/models/telegram.py's TelegramCommand /
 * TelegramUpdateEnvelope for the exact request shape this mirrors, and
 * TelegramUpdateResult for the `{ accepted, task_id, immediate_messages }`
 * response agent/inbound.mjs consumes. `command` fields use the same
 * snake_case names as the Pydantic model — this is a wire contract, not a
 * JS-side naming choice.
 * @param {{ chat_id: string, update_id: number, message_id?: number|null, hub_message_id?: number|null, text?: string|null, callback?: {action: string, value?: string|null}|null }} command
 */
export async function forwardTelegramUpdate(command) {
  return agentCall('POST', '/telegram/update', { body: { command } });
}
