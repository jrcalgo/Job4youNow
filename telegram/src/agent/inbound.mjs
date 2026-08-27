// agent/inbound.mjs — the ONLY path a Telegram update takes to reach the
// agent app's reasoning. bot.mjs's dispatch decides WHICH updates get here
// (free text with no active queue, or unrecognized free text mid-review —
// see bot.mjs's header comment); this module has zero knowledge of what
// any given message means. It forwards the update to `POST
// /telegram/update` and relays whatever comes back immediately:
//
//   - a deterministic route replies right away via `immediate_messages`
//     (delivered here, synchronously, via outbox.mjs's deliverMessage — the
//     exact function the async outbox-poll loop also uses);
//   - a predictive route instead returns a bare `task_id` with no
//     immediate messages — its eventual result arrives later through that
//     same outbox-poll loop (agent/outbox.mjs), not through this module.
//
// A forward that fails outright (agent app unreachable/erroring) gets a
// user-visible apology instead of the update silently vanishing — bot.mjs
// has already advanced the Telegram offset by the time this runs, so this
// is the last chance to tell the user anything went wrong.
import { sendMessage } from '../telegram/api.mjs';
import { forwardTelegramUpdate } from './client.mjs';
import { deliverMessage } from './outbox.mjs';
import { withMainMenuRow } from '../protocol/core.mjs';
import { log } from '../lib/log.mjs';

const UNREACHABLE_REPLY = 'Sorry, the agent app is unreachable right now. Please try again shortly.';
const QUEUED_REPLY = 'Working on it — I will send the result here once it is ready.';

/**
 * @param {string} token
 * @param {{ chatId: string, updateId: number, messageId?: number|null, hubMessageId?: number|null, text?: string|null, callback?: {action: string, value?: string|null}|null }} update
 * @param {{ dryRun?: boolean }} [opts]
 */
export async function forwardToAgent(token, {
  chatId, updateId, messageId = null, hubMessageId = null, text = null, callback = null,
}, { dryRun = false } = {}) {
  let result;
  try {
    result = await forwardTelegramUpdate({
      chat_id: chatId,
      update_id: updateId,
      message_id: messageId,
      hub_message_id: hubMessageId,
      text,
      callback,
    });
  } catch (err) {
    log.error('agent forward failed', { chatId, error: err?.message });
    await sendMessage(token, chatId, UNREACHABLE_REPLY, { dryRun });
    return;
  }

  const effectiveHub = hubMessageId ?? result?.hub_message_id ?? null;
  const messages = result?.immediate_messages || [];
  for (const message of messages) {
    // eslint-disable-next-line no-await-in-loop
    await deliverMessage(token, message.chat_id || chatId, message, {
      dryRun,
      hubMessageId: effectiveHub,
      chatIdForHub: chatId,
    });
  }

  if (!messages.length && result?.task_id) {
    await sendMessage(token, chatId, QUEUED_REPLY, { replyMarkup: withMainMenuRow(), dryRun });
  }
}
