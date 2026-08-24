// lib/retry.mjs — backoff helpers for transient AWS Data API / Telegram errors.
import { log } from './log.mjs';

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Generic exponential backoff with jitter. `isRetryable(err)` decides whether
 * to retry at all; `delayFor(err, attempt)` overrides the wait when a
 * specific error carries its own timing hint (e.g. Telegram's retry_after).
 * Rethrows the last error once `maxAttempts` is exhausted.
 */
export async function withRetry(fn, {
  maxAttempts = 5,
  baseDelayMs = 500,
  maxDelayMs = 15_000,
  isRetryable = () => true,
  delayFor,
  label = 'operation',
} = {}) {
  let attempt = 0;
  for (;;) {
    attempt += 1;
    try {
      return await fn(attempt);
    } catch (err) {
      if (attempt >= maxAttempts || !isRetryable(err)) throw err;
      const delay = delayFor
        ? delayFor(err, attempt)
        : Math.min(maxDelayMs, baseDelayMs * 2 ** (attempt - 1)) * (0.75 + Math.random() * 0.5);
      log.warn(`${label}: retrying after transient error`, {
        attempt, delayMs: Math.round(delay), errorName: err?.name, errorMessage: err?.message,
      });
      await sleep(delay);
    }
  }
}

// Aurora Serverless v2 with min ACU 0 auto-pauses after inactivity. The FIRST
// Data API call after a pause throws DatabaseResumingException (HTTP 400,
// message: "... is resuming after being auto-paused. Please wait a few
// seconds and try again."). AWS SDK v3 does not retry 4xx errors on its own
// (see github.com/aws/aws-sdk-js-v3/issues/6701) — every Data API call in
// this codebase goes through db/client.mjs, which wraps it in this retry so
// no call site can forget it. Resume typically completes in ~12-15s.
// DatabaseUnavailableException (504 — writer instance momentarily
// unavailable mid-scale) is the other transient case worth absorbing here.
export async function withAuroraRetry(fn, opts = {}) {
  return withRetry(fn, {
    maxAttempts: 8,
    label: 'aurora data api',
    isRetryable: (err) => err?.name === 'DatabaseResumingException' || err?.name === 'DatabaseUnavailableException',
    delayFor: (err, attempt) => (
      err?.name === 'DatabaseResumingException' ? 3_000 : Math.min(10_000, 1_000 * 2 ** (attempt - 1))
    ),
    ...opts,
  });
}

// Telegram 429 carries an explicit retry_after (seconds) in
// error.parameters.retry_after — honor it exactly rather than guessing a
// backoff. A 409 (another getUpdates consumer holding the token) is
// deliberately NOT retried here: it almost always means a configuration
// problem (two bots sharing a token, or two instances of this daemon), and
// bot.mjs's caller decides how loudly to complain — see
// docs/producing-queues.md's "separate bot" note.
export async function withTelegramRetry(fn, opts = {}) {
  return withRetry(fn, {
    maxAttempts: 5,
    label: 'telegram api',
    isRetryable: (err) => typeof err?.retryAfter === 'number' && err.retryAfter >= 0,
    delayFor: (err) => (Number(err.retryAfter) || 1) * 1000 + 250,
    ...opts,
  });
}
