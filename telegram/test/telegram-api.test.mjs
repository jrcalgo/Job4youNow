// test/telegram-api.test.mjs — telegram/api.mjs against a real local HTTP
// server (see test/helpers/fake-telegram-server.mjs), covering the bug fix
// (status/retryAfter surviving tgCall's catch block) and the 429-retry /
// 409-no-retry distinction lib/retry.mjs is responsible for.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startFakeTelegramServer } from './helpers/fake-telegram-server.mjs';

const TOKEN = 'fake-token-123456';

async function withServer(fn) {
  const server = await startFakeTelegramServer();
  process.env.J4N_TELEGRAM_API_BASE = server.baseUrl;
  try {
    return await fn(server);
  } finally {
    await server.close();
    delete process.env.J4N_TELEGRAM_API_BASE;
  }
}

// api.mjs reads J4N_TELEGRAM_API_BASE at call time (module-level const would
// capture it before we set it), so import lazily per test after the env var
// is in place.
async function loadApi() {
  return import(`../src/telegram/api.mjs?t=${Date.now()}-${Math.random()}`);
}

test('sendMessage: escapes HTML, attaches reply_markup only to the last chunk', async () => {
  await withServer(async (server) => {
    server.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
    const { sendMessage } = await loadApi();

    await sendMessage(TOKEN, '42', 'a & b <c>', { replyMarkup: { inline_keyboard: [] } });
    assert.equal(server.requests.length, 1);
    const body = JSON.parse(server.requests[0].bodyRaw.toString('utf8'));
    assert.equal(body.text, 'a &amp; b &lt;c&gt;');
    assert.deepEqual(body.reply_markup, { inline_keyboard: [] });
    assert.equal(server.requests[0].path, `/bot${TOKEN}/sendMessage`);
  });
});

test('sendMessage: long text is chunked, only the LAST request carries reply_markup', async () => {
  await withServer(async (server) => {
    server.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
    const { sendMessage } = await loadApi();

    await sendMessage(TOKEN, '42', 'x'.repeat(9000), { replyMarkup: { inline_keyboard: [] } });
    assert.ok(server.requests.length >= 2);
    const bodies = server.requests.map((r) => JSON.parse(r.bodyRaw.toString('utf8')));
    for (const b of bodies.slice(0, -1)) assert.equal(b.reply_markup, undefined);
    assert.deepEqual(bodies.at(-1).reply_markup, { inline_keyboard: [] });
  });
});

test('sendMessage: escape:false sends agent-app-produced HTML verbatim (no double-escaping)', async () => {
  await withServer(async (server) => {
    server.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
    const { sendMessage } = await loadApi();

    await sendMessage(TOKEN, '42', '<b>Commands</b>\n\nBACKLOG &amp; more', { escape: false });
    const body = JSON.parse(server.requests[0].bodyRaw.toString('utf8'));
    assert.equal(body.text, '<b>Commands</b>\n\nBACKLOG &amp; more');
  });
});

test('editMessageText: sends edit payload with HTML', async () => {
  await withServer(async (server) => {
    server.when('/editMessageText', () => ({ body: { ok: true, result: { message_id: 9 } } }));
    const { editMessageText } = await loadApi();

    await editMessageText(TOKEN, '42', 9, '<b>Menu</b>', { replyMarkup: { inline_keyboard: [] }, escape: false });
    assert.equal(server.requests.length, 1);
    const body = JSON.parse(server.requests[0].bodyRaw.toString('utf8'));
    assert.equal(body.message_id, 9);
    assert.equal(body.text, '<b>Menu</b>');
    assert.equal(server.requests[0].path, `/bot${TOKEN}/editMessageText`);
  });
});

test('sendMessage --dry-run never hits the network', async () => {
  await withServer(async (server) => {
    const { sendMessage } = await loadApi();
    const result = await sendMessage(TOKEN, '42', 'hello', { dryRun: true });
    assert.equal(server.requests.length, 0);
    assert.equal(result[0].dry_run, true);
  });
});

test('getUpdates: GET with offset in the querystring', async () => {
  await withServer(async (server) => {
    server.when('/getUpdates', () => ({ body: { ok: true, result: [{ update_id: 5 }] } }));
    const { getUpdates } = await loadApi();
    const result = await getUpdates(TOKEN, { offset: 7, timeoutSec: 1 });
    assert.deepEqual(result, [{ update_id: 5 }]);
    assert.equal(server.requests[0].method, 'GET');
    assert.match(server.requests[0].query, /offset=7/);
  });
});

test('a Telegram-level error (ok:false) throws with status set — the original bug this port fixes', async () => {
  await withServer(async (server) => {
    server.when('/sendMessage', () => ({ status: 403, body: { ok: false, error_code: 403, description: 'Forbidden: bot was blocked by the user' } }));
    const { sendMessage } = await loadApi();
    await assert.rejects(
      () => sendMessage(TOKEN, '42', 'hi'),
      (err) => {
        assert.equal(err.status, 403);
        assert.match(err.message, /Forbidden/);
        return true;
      },
    );
  });
});

test('429 with retry_after is retried automatically and eventually succeeds', async () => {
  await withServer(async (server) => {
    let calls = 0;
    server.when('/sendMessage', () => {
      calls++;
      if (calls === 1) return { status: 429, body: { ok: false, error_code: 429, description: 'Too Many Requests', parameters: { retry_after: 0 } } };
      return { body: { ok: true, result: { message_id: 1 } } };
    });
    const { sendMessage } = await loadApi();
    const result = await sendMessage(TOKEN, '42', 'hi');
    assert.equal(calls, 2);
    assert.equal(result[0].message_id, 1);
  });
});

test('409 (another getUpdates consumer active) is NOT retried — surfaces immediately with status 409', async () => {
  await withServer(async (server) => {
    let calls = 0;
    server.when('/getUpdates', () => { calls++; return { status: 409, body: { ok: false, error_code: 409, description: 'Conflict: terminated by other getUpdates request' } }; });
    const { getUpdates } = await loadApi();
    await assert.rejects(
      () => getUpdates(TOKEN, { timeoutSec: 1 }),
      (err) => { assert.equal(err.status, 409); return true; },
    );
    assert.equal(calls, 1, 'a 409 must not be retried internally — bot.mjs decides how to react');
  });
});

test('sendDocument: multipart body carries chat_id, caption, and the file', async () => {
  await withServer(async (server) => {
    server.when('/sendDocument', () => ({ body: { ok: true, result: { message_id: 2 } } }));
    const { sendDocument } = await loadApi();

    const dir = mkdtempSync(join(tmpdir(), 'j4n-doc-'));
    const filePath = join(dir, 'resume.pdf');
    writeFileSync(filePath, '%PDF-fake-bytes');
    try {
      await sendDocument(TOKEN, '42', filePath, '1/3 · Acme · cv · review only');
      assert.equal(server.requests.length, 1);
      assert.match(server.requests[0].headers['content-type'], /multipart\/form-data/);
      assert.ok(server.requests[0].bodyRaw.includes('resume.pdf'));
      assert.ok(server.requests[0].bodyRaw.includes('review only'));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

test('sendDocument: escape:false sends an already-safe caption verbatim', async () => {
  await withServer(async (server) => {
    server.when('/sendDocument', () => ({ body: { ok: true, result: { message_id: 2 } } }));
    const { sendDocument } = await loadApi();

    const dir = mkdtempSync(join(tmpdir(), 'j4n-doc-'));
    const filePath = join(dir, 'resume.pdf');
    writeFileSync(filePath, '%PDF-fake-bytes');
    try {
      await sendDocument(TOKEN, '42', filePath, 'R&amp;D role — tailored resume', { escape: false });
      assert.ok(server.requests[0].bodyRaw.includes('R&amp;D role'));
      assert.ok(!server.requests[0].bodyRaw.includes('R&amp;amp;D'), 'must not double-escape an already-escaped caption');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

test('sendContact requires a phone number', async () => {
  await withServer(async () => {
    const { sendContact } = await loadApi();
    await assert.rejects(() => sendContact(TOKEN, '42', { name: 'No Phone' }), /has no phone/);
  });
});

test('answerCallback swallows errors (non-fatal by design)', async () => {
  await withServer(async (server) => {
    server.when('/answerCallbackQuery', () => ({ status: 500, body: { ok: false, description: 'boom' } }));
    const { answerCallback } = await loadApi();
    await assert.doesNotReject(() => answerCallback(TOKEN, 'cbq-1', 'OK'));
  });
});
