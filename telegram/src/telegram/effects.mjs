// telegram/effects.mjs — executes the effect markers protocol/core.mjs's
// applyAction() returns (send_card, send_cv, send_jd, ...), plus two
// daemon-only sends (queue list, digest-on-switch) for the 'queues' /
// 'use_queue' actions that bypass applyAction entirely (see core.mjs's
// header comment for why).
//
// Ported from career-ops' telegram-session.mjs's runEffects(), with one
// structural change: the original mutated `state.stats.cvs_sent` directly as
// a side effect and relied on the caller re-persisting the whole state
// afterward. Here, runEffects() returns `{ cvsSent }` instead and bot.mjs
// applies that delta itself before persisting — effects.mjs has no idea how
// (or whether) state is stored, matching how core.mjs's applyAction() also
// stays persistence-agnostic.
//
// send_cv / send_jd resolve an S3 key (not a local path) through
// artifacts/store.mjs, which downloads into the local LRU cache on a miss.
import {
  currentItem, formatCompanySummary, formatCompletion, formatContactsText,
  formatDigest, formatJobCard, formatMore, formatQueueList, inlineKeyboard, chunkMessage,
} from '../protocol/core.mjs';
import { getArtifactPath } from '../artifacts/store.mjs';
import { sendContact, sendDocument, sendMessage } from './api.mjs';
import { log } from '../lib/log.mjs';

/**
 * @param {string} token
 * @param {string} chatId
 * @param {object} state
 * @param {object[]} effects
 * @param {{ dryRun?: boolean }} [opts]
 * @returns {Promise<{ cvsSent: number }>}
 */
export async function runEffects(token, chatId, state, effects, opts = {}) {
  const { dryRun = false } = opts;
  let cvsSent = 0;

  for (const eff of effects) {
    const item = eff.itemId
      ? state.items.find((it) => it.id === eff.itemId)
      : currentItem(state);

    if (eff.type === 'send_card') {
      const cur = currentItem(state);
      await sendMessage(token, chatId, formatJobCard(state, cur), { replyMarkup: inlineKeyboard(state, cur), dryRun });
    } else if (eff.type === 'send_completion') {
      await sendMessage(token, chatId, formatCompletion(state), { dryRun });
    } else if (eff.type === 'send_digest') {
      for (const chunk of chunkMessage(formatDigest(state), 4096, 'Digest')) {
        await sendMessage(token, chatId, chunk, { dryRun });
      }
    } else if (eff.type === 'send_company' && item) {
      await sendMessage(token, chatId, formatCompanySummary(item), { dryRun });
    } else if (eff.type === 'send_more' && item) {
      await sendMessage(token, chatId, formatMore(item), { dryRun });
    } else if (eff.type === 'send_contacts' && item) {
      await sendMessage(token, chatId, formatContactsText(item), { dryRun });
      for (const c of item.contacts || []) {
        if (c.phone) {
          try {
            await sendContact(token, chatId, c, { dryRun });
          } catch (err) {
            log.warn('send_contacts: sendContact failed (text summary already sent)', { error: err.message });
          }
        }
      }
    } else if (eff.type === 'send_jd' && item) {
      const lines = [`JD — ${item.company} / ${item.role}`, item.url || '(no url)'];
      await sendMessage(token, chatId, lines.join('\n'), { dryRun });
      const key = item.artifacts?.jd || item.artifacts?.jd_md;
      if (key) {
        await sendArtifactOrExplain(token, chatId, key, `${item.n}/${state.total} · JD · review only`, dryRun, item.company);
      }
    } else if (eff.type === 'send_cv' && item) {
      const key = item.artifacts?.cv_pdf || item.artifacts?.cv;
      if (!key) {
        await sendMessage(token, chatId, `No tailored PDF for ${item.company} in this queue.`, { dryRun });
        continue;
      }
      const sent = await sendArtifactOrExplain(
        token, chatId, key,
        `${item.n}/${state.total} · ${item.company} · tailored CV · review only — not submitted`,
        dryRun, item.company,
      );
      if (sent) cvsSent += 1;
    }
  }

  return { cvsSent };
}

async function sendArtifactOrExplain(token, chatId, s3Key, caption, dryRun, company) {
  let localPath;
  try {
    localPath = dryRun ? null : await getArtifactPath(s3Key);
  } catch (err) {
    log.warn('artifact fetch failed', { s3Key, error: err.message });
    await sendMessage(token, chatId, `Could not fetch that file for ${company} (${err.message}).`, { dryRun: false });
    return false;
  }
  await sendDocument(token, chatId, localPath, caption, { dryRun });
  return true;
}

/** Daemon-only: QUEUES command. Needs an async queue list, so it never goes through applyAction. */
export async function sendQueueList(token, chatId, queues, { dryRun = false } = {}) {
  await sendMessage(token, chatId, formatQueueList(queues), { dryRun });
}

/** Daemon-only: after switching queues (USE_QUEUE), re-show the digest + current card. */
export async function sendDigestAndCard(token, chatId, state, { dryRun = false } = {}) {
  for (const chunk of chunkMessage(formatDigest(state), 4096, 'Digest')) {
    await sendMessage(token, chatId, chunk, { dryRun });
  }
  const item = currentItem(state);
  await sendMessage(token, chatId, formatJobCard(state, item), { replyMarkup: inlineKeyboard(state, item), dryRun });
}
