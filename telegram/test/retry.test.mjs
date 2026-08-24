// test/retry.test.mjs — lib/retry.mjs backoff semantics, using fake errors
// and fake timers-by-injection (small delays) rather than real AWS/Telegram.
import test from 'node:test';
import assert from 'node:assert/strict';
import { withRetry, withAuroraRetry, withTelegramRetry } from '../src/lib/retry.mjs';

function namedError(name, extra = {}) {
  const e = new Error(name);
  e.name = name;
  Object.assign(e, extra);
  return e;
}

test('withRetry gives up immediately when isRetryable says no', async () => {
  let calls = 0;
  await assert.rejects(
    () => withRetry(async () => { calls++; throw new Error('nope'); }, { isRetryable: () => false, maxAttempts: 5 }),
    /nope/,
  );
  assert.equal(calls, 1);
});

test('withRetry retries up to maxAttempts then rethrows the last error', async () => {
  let calls = 0;
  await assert.rejects(
    () => withRetry(async () => { calls++; throw new Error(`fail-${calls}`); }, {
      maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 2,
    }),
    /fail-3/,
  );
  assert.equal(calls, 3);
});

test('withRetry returns the value on eventual success', async () => {
  let calls = 0;
  const result = await withRetry(async () => {
    calls++;
    if (calls < 2) throw new Error('transient');
    return 'ok';
  }, { maxAttempts: 5, baseDelayMs: 1, maxDelayMs: 2 });
  assert.equal(result, 'ok');
  assert.equal(calls, 2);
});

test('withAuroraRetry retries DatabaseResumingException and DatabaseUnavailableException only', async () => {
  let calls = 0;
  const result = await withAuroraRetry(async () => {
    calls++;
    if (calls === 1) throw namedError('DatabaseResumingException');
    return 'resumed';
  });
  assert.equal(result, 'resumed');
  assert.equal(calls, 2);

  await assert.rejects(
    () => withAuroraRetry(async () => { throw namedError('BadRequestException'); }, { maxAttempts: 2 }),
    /BadRequestException/,
  );
});

test('withTelegramRetry honors retryAfter and does not retry errors without it', async () => {
  let calls = 0;
  const start = Date.now();
  const result = await withTelegramRetry(async () => {
    calls++;
    if (calls === 1) throw namedError('TooManyRequests', { retryAfter: 0 }); // 0s + 250ms floor
    return 'ok';
  });
  assert.equal(result, 'ok');
  assert.ok(Date.now() - start >= 200);

  await assert.rejects(
    () => withTelegramRetry(async () => { throw new Error('not retryable'); }),
    /not retryable/,
  );
});
