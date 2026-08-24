// test/telegram-effects.test.mjs — telegram/effects.mjs against the fake
// Telegram HTTP server AND the fake S3 client together, proving the actual
// integration path (applyAction effect -> effects.mjs -> api.mjs -> HTTP,
// and -> artifacts/store.mjs -> S3/cache) rather than each piece in
// isolation.
//
// api.mjs freezes its API base URL into a module-level const the first time
// it's evaluated, so J4N_TELEGRAM_API_BASE must be set BEFORE the very first
// (even transitive) import of it in this process — done once in
// test.before(), rather than per test.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { startFakeTelegramServer } from './helpers/fake-telegram-server.mjs';
import { buildSessionState, validateQueue } from '../src/protocol/core.mjs';

process.env.AWS_REGION ||= 'us-east-1';
process.env.S3_BUCKET ||= 'fake-bucket';
process.env.S3_PREFIX = 'job4menow-telegram/';

let server;
let effects;
let __setTestS3;
let cacheDir;

test.before(async () => {
  server = await startFakeTelegramServer();
  process.env.J4N_TELEGRAM_API_BASE = server.baseUrl;
  cacheDir = mkdtempSync(join(tmpdir(), 'j4n-effects-cache-'));
  process.env.J4N_CACHE_DIR = cacheDir;

  effects = await import('../src/telegram/effects.mjs');
  ({ __setTestS3 } = await import('../src/artifacts/store.mjs'));
});

test.after(async () => {
  await server.close();
  rmSync(cacheDir, { recursive: true, force: true });
});

function fakeS3WithObject(key, content) {
  const calls = [];
  return {
    calls,
    async send(command) {
      calls.push(command);
      if (command instanceof GetObjectCommand) {
        if (command.input.Key !== `job4menow-telegram/${key}`) {
          throw Object.assign(new Error('NoSuchKey'), { name: 'NoSuchKey' });
        }
        return { Body: Readable.from([Buffer.from(content)]) };
      }
      throw new Error(`unhandled: ${command.constructor.name}`);
    },
  };
}

function stateWithOneItem(overrides = {}) {
  const validated = validateQueue({
    title: 'Test batch',
    items: [{
      company: 'Acme', role: 'Engineer', report_num: '1', url: 'https://example.com/1',
      contacts: [{ name: 'Jane Doe', phone: '+15555550123' }],
      summary: { why_match: ['Fit'], risks: [], job: [], company: [] },
      ...overrides,
    }],
  });
  return buildSessionState(validated.queue, { chatId: '42' });
}

test.afterEach(() => __setTestS3(null));

test('send_card sends the current job card with an inline keyboard', async () => {
  server.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
  const state = stateWithOneItem();
  const { cvsSent } = await effects.runEffects('tok', '42', state, [{ type: 'send_card' }]);
  assert.equal(cvsSent, 0);
  const last = server.requests.at(-1);
  const body = JSON.parse(last.bodyRaw.toString('utf8'));
  assert.match(body.text, /Acme/);
  assert.ok(body.reply_markup.inline_keyboard.length > 0);
});

test('send_cv with no artifact on the item explains rather than crashing', async () => {
  server.when('/sendMessage', () => ({ body: { ok: true, result: {} } }));
  const state = stateWithOneItem();
  const { cvsSent } = await effects.runEffects('tok', '42', state, [{ type: 'send_cv', itemId: state.items[0].id }]);
  assert.equal(cvsSent, 0);
  const body = JSON.parse(server.requests.at(-1).bodyRaw.toString('utf8'));
  assert.match(body.text, /No tailored PDF/);
});

test('send_cv downloads the artifact from S3 through the cache and reports cvsSent: 1', async () => {
  server.when('/sendDocument', () => ({ body: { ok: true, result: { message_id: 3 } } }));
  __setTestS3(fakeS3WithObject('queues/tg-1/1-cv_pdf.pdf', '%PDF fake cv'));

  const state = stateWithOneItem();
  state.items[0].artifacts = { cv_pdf: 'queues/tg-1/1-cv_pdf.pdf' };

  const { cvsSent } = await effects.runEffects('tok', '42', state, [{ type: 'send_cv', itemId: state.items[0].id }]);
  assert.equal(cvsSent, 1);
  const docReq = server.requests.find((r) => r.path.endsWith('/sendDocument'));
  assert.ok(docReq.bodyRaw.includes('review only'));
});

test('send_cv reports a friendly error (not a crash) when the S3 object is missing', async () => {
  server.when('/sendMessage', () => ({ body: { ok: true, result: {} } }));
  __setTestS3(fakeS3WithObject('some-other-key', 'irrelevant'));

  // A key never downloaded in any other test in this file — otherwise a
  // previous test's cache entry for the same key would serve a hit here and
  // this would never even ask the (now differently-configured) fake S3.
  const state = stateWithOneItem();
  state.items[0].artifacts = { cv_pdf: 'queues/tg-missing/1-cv_pdf.pdf' };
  const { cvsSent } = await effects.runEffects('tok', '42', state, [{ type: 'send_cv', itemId: state.items[0].id }]);
  assert.equal(cvsSent, 0);
  const body = JSON.parse(server.requests.at(-1).bodyRaw.toString('utf8'));
  assert.match(body.text, /Could not fetch/);
});

test('send_contacts sends the text summary and a native contact card for phone contacts', async () => {
  server.when('/sendMessage', () => ({ body: { ok: true, result: {} } }));
  server.when('/sendContact', () => ({ body: { ok: true, result: {} } }));
  const state = stateWithOneItem();
  await effects.runEffects('tok', '42', state, [{ type: 'send_contacts', itemId: state.items[0].id }]);
  assert.ok(server.requests.some((r) => r.path.endsWith('/sendMessage')));
  assert.ok(server.requests.some((r) => r.path.endsWith('/sendContact')));
});

test('sendQueueList and sendDigestAndCard render without crashing', async () => {
  server.when('/sendMessage', () => ({ body: { ok: true, result: {} } }));
  await effects.sendQueueList('tok', '42', [{ id: 'q1', title: 'A', item_count: 1, ingested_at: '2026-01-01', active: true }]);
  const state = stateWithOneItem();
  await effects.sendDigestAndCard('tok', '42', state);
  assert.ok(server.requests.length >= 2);
});
