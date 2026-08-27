// test/agent-outbox.test.mjs — agent/outbox.mjs against fake Telegram +
// agent-API servers (and a fake S3 for artifact fetches). Proves: a
// public_text row sends via sendMessage; a private_artifact row fetches
// from the PRIVATE bucket (never the job one) and sends via sendDocument;
// a row is acknowledged only after a successful send; one bad row never
// stops the rest of the batch or the loop itself.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { startFakeTelegramServer } from './helpers/fake-telegram-server.mjs';
import { startFakeAgentServer } from './helpers/fake-agent-server.mjs';
import { __setTestS3 } from '../src/artifacts/store.mjs';

process.env.AWS_REGION ||= 'us-east-1';
process.env.PRIVATE_USER_ARTIFACTS_BUCKET ||= 'fake-private-bucket';
process.env.PRIVATE_USER_ARTIFACTS_PREFIX = 'private/';

const TOKEN = 'fake-token-123456';

function fakeS3({ objects = {} } = {}) {
  return {
    async send(command) {
      if (command instanceof GetObjectCommand) {
        const buf = objects[command.input.Key];
        if (!buf) throw Object.assign(new Error('NoSuchKey'), { name: 'NoSuchKey' });
        return { Body: Readable.from([buf]) };
      }
      throw new Error(`fake-s3: unhandled command ${command.constructor.name}`);
    },
  };
}

async function withServers(fn) {
  const telegram = await startFakeTelegramServer();
  const agent = await startFakeAgentServer();
  const cacheDir = mkdtempSync(join(tmpdir(), 'j4n-agent-outbox-'));
  process.env.J4N_TELEGRAM_API_BASE = telegram.baseUrl;
  process.env.AGENT_API_BASE = agent.baseUrl;
  process.env.J4N_CACHE_DIR = cacheDir;
  try {
    return await fn({ telegram, agent });
  } finally {
    await telegram.close();
    await agent.close();
    delete process.env.J4N_TELEGRAM_API_BASE;
    delete process.env.AGENT_API_BASE;
    delete process.env.J4N_CACHE_DIR;
    __setTestS3(null);
    rmSync(cacheDir, { recursive: true, force: true });
  }
}

async function loadOutbox() {
  return import('../src/agent/outbox.mjs');
}

test('public_text row delivers via sendMessage with its buttons', async () => {
  await withServers(async ({ telegram }) => {
    telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
    const { deliverOutboxRow } = await loadOutbox();

    await deliverOutboxRow(TOKEN, {
      id: 'outbox-1',
      chat_id: '42',
      delivery_kind: 'public_text',
      public_payload: { text: 'hello', buttons: [[{ text: 'Go', callback_data: 'go' }]] },
    });

    assert.equal(telegram.requests.length, 1);
    const body = JSON.parse(telegram.requests[0].bodyRaw.toString('utf8'));
    assert.equal(body.text, 'hello');
    assert.deepEqual(body.reply_markup, { inline_keyboard: [[{ text: 'Go', callback_data: 'go' }]] });
  });
});

test('deliverMessage (the shared primitive agent/inbound.mjs also reuses) sends a flat public_text message directly, without re-escaping the agent app\'s own HTML', async () => {
  await withServers(async ({ telegram }) => {
    telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
    const { deliverMessage } = await loadOutbox();

    // agent/app/formatting/chunking.py's to_outbound_messages() wraps a
    // title in a real <b> tag — deliverMessage must forward that verbatim,
    // not double-escape it into visible "&lt;b&gt;" text.
    await deliverMessage('fake-token', '42', { delivery_kind: 'public_text', text: '<b>Commands</b>\n\nBACKLOG — current job backlog by role', buttons: [] });

    assert.equal(telegram.requests.length, 1);
    assert.equal(JSON.parse(telegram.requests[0].bodyRaw.toString('utf8')).text, '<b>Commands</b>\n\nBACKLOG — current job backlog by role');
  });
});

test('private_artifact row fetches from the PRIVATE bucket and delivers as chat TEXT, never a document', async () => {
  await withServers(async ({ telegram }) => {
    telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 2 } } }));
    const key = 'resumes/augmented/backend/run-1.md';
    __setTestS3(fakeS3({ objects: { [`private/${key}`]: Buffer.from('# Rewritten resume') } }));
    const { deliverOutboxRow } = await loadOutbox();

    await deliverOutboxRow(TOKEN, {
      id: 'outbox-2',
      chat_id: '42',
      delivery_kind: 'private_artifact',
      artifact_ref: {
        artifact: { bucket: 'private_user_artifacts', key, checksum_sha256: 'x', byte_size: 19 },
        caption: 'Your updated resume',
      },
    });

    assert.equal(telegram.requests.length, 1);
    assert.equal(telegram.requests[0].path, `/bot${TOKEN}/sendMessage`);
    const body = JSON.parse(telegram.requests[0].bodyRaw.toString('utf8'));
    assert.match(body.text, /<b>Your updated resume<\/b>/);
    assert.match(body.text, /# Rewritten resume/);
    assert.equal(body.parse_mode, 'HTML');
  });
});

test('private_artifact content is escaped before being sent as text, since (unlike public_text/captions) it never passed through a presenter\'s own escaping', async () => {
  await withServers(async ({ telegram }) => {
    telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 3 } } }));
    const key = 'responses/raw-llm-output.md';
    __setTestS3(fakeS3({ objects: { [`private/${key}`]: Buffer.from('Use <script>alert(1)</script> & "quotes"') } }));
    const { deliverOutboxRow } = await loadOutbox();

    await deliverOutboxRow(TOKEN, {
      id: 'outbox-3',
      chat_id: '42',
      delivery_kind: 'private_artifact',
      artifact_ref: { artifact: { bucket: 'private_user_artifacts', key, checksum_sha256: 'x', byte_size: 40 } },
    });

    const body = JSON.parse(telegram.requests[0].bodyRaw.toString('utf8'));
    assert.match(body.text, /&lt;script&gt;alert\(1\)&lt;\/script&gt; &amp; &quot;quotes&quot;/);
    assert.doesNotMatch(body.text, /<script>/);
  });
});

test('private_artifact delivery in dry-run mode never touches S3', async () => {
  await withServers(async ({ telegram }) => {
    const { deliverOutboxRow } = await loadOutbox();

    await deliverOutboxRow(TOKEN, {
      id: 'outbox-4',
      chat_id: '42',
      delivery_kind: 'private_artifact',
      artifact_ref: { artifact: { bucket: 'private_user_artifacts', key: 'never-fetched.md', checksum_sha256: 'x', byte_size: 1 } },
    }, { dryRun: true });

    assert.equal(telegram.requests.length, 0);
  });
});

test('runAgentOutboxLoop acknowledges a row only after a successful send', async () => {
  await withServers(async ({ telegram, agent }) => {
    let acked = false;
    agent.when('/telegram/outbox', () => (
      { body: acked ? [] : [{ id: 'outbox-1', chat_id: '42', delivery_kind: 'public_text', public_payload: { text: 'hi' } }] }
    ));
    agent.when('/delivered', () => {
      acked = true;
      return { body: { ok: true } };
    });
    telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
    const { runAgentOutboxLoop } = await loadOutbox();

    let stopped = false;
    const loopPromise = runAgentOutboxLoop(TOKEN, { pollMs: 5, shouldStop: () => stopped });
    // eslint-disable-next-line no-promise-executor-return
    await new Promise((resolve) => setTimeout(resolve, 60));
    stopped = true;
    await loopPromise;

    assert.equal(acked, true);
    assert.equal(telegram.requests.filter((r) => r.path.endsWith('/sendMessage')).length, 1, 'delivered exactly once');
  });
});

test('one delivery failure does not stop the rest of the batch', async () => {
  await withServers(async ({ telegram, agent }) => {
    const acknowledged = [];
    let delivered = false;
    agent.when('/telegram/outbox', () => ({
      body: delivered
        ? []
        : [
          // Malformed on purpose (no `artifact` in artifact_ref) -> throws
          // when store.mjs tries to read `.bucket` off it.
          { id: 'bad', chat_id: '42', delivery_kind: 'private_artifact', artifact_ref: {} },
          { id: 'good', chat_id: '42', delivery_kind: 'public_text', public_payload: { text: 'ok' } },
        ],
    }));
    agent.when('/delivered', (req) => {
      delivered = true;
      acknowledged.push(req.path);
      return { body: { ok: true } };
    });
    telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
    const { runAgentOutboxLoop } = await loadOutbox();

    let stopped = false;
    const loopPromise = runAgentOutboxLoop(TOKEN, { pollMs: 5, shouldStop: () => stopped });
    // eslint-disable-next-line no-promise-executor-return
    await new Promise((resolve) => setTimeout(resolve, 30));
    stopped = true;
    await loopPromise;

    assert.ok(acknowledged.some((p) => p.endsWith('/good/delivered')));
    assert.ok(!acknowledged.some((p) => p.endsWith('/bad/delivered')), 'the failed row must never be acknowledged');
  });
});
