// test/agent-inbound.test.mjs — agent/inbound.mjs's forwardToAgent()
// against fake Telegram + agent-API servers (and a fake S3 for the
// artifact-relay case). Proves: the exact request shape POST'd to
// /telegram/update, that immediate public_text/artifact messages are
// relayed via the same delivery path agent/outbox.mjs uses, that a bare
// task_id with no immediate messages gets an acknowledgement reply, and
// that an unreachable agent app produces a user-visible apology instead of
// a silently lost update.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
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
  const cacheDir = mkdtempSync(join(tmpdir(), 'j4n-agent-inbound-'));
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

async function loadInbound() {
  return import('../src/agent/inbound.mjs');
}

test('forwardToAgent POSTs the normalized update as a TelegramCommand envelope', async () => {
  await withServers(async ({ telegram, agent }) => {
    agent.when('/telegram/update', () => ({ body: { accepted: true, task_id: null, immediate_messages: [] } }));
    const { forwardToAgent } = await loadInbound();

    await forwardToAgent(TOKEN, { chatId: '42', updateId: 5, messageId: 9, text: 'SCAN backend' });

    assert.equal(agent.requests.length, 1);
    assert.equal(agent.requests[0].path, '/telegram/update');
    const body = JSON.parse(agent.requests[0].bodyRaw.toString('utf8'));
    assert.deepEqual(body, {
      command: {
        chat_id: '42', update_id: 5, message_id: 9, hub_message_id: null, text: 'SCAN backend', callback: null,
      },
    });
    assert.equal(telegram.requests.length, 0, 'no immediate messages means no Telegram send at all');
  });
});

test('an immediate public_text message is relayed via sendMessage, preserving the agent app\'s own HTML markup', async () => {
  await withServers(async ({ telegram, agent }) => {
    agent.when('/telegram/update', () => ({
      body: {
        accepted: true,
        task_id: null,
        immediate_messages: [{ chat_id: '42', delivery_kind: 'public_text', text: '<b>Backend backlog</b>\n\n3 roles.', buttons: [] }],
      },
    }));
    telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
    const { forwardToAgent } = await loadInbound();

    await forwardToAgent(TOKEN, { chatId: '42', updateId: 5, text: 'BACKLOG' });

    assert.equal(telegram.requests.length, 1);
    const body = JSON.parse(telegram.requests[0].bodyRaw.toString('utf8'));
    assert.equal(body.text, '<b>Backend backlog</b>\n\n3 roles.', 'the <b> tag must reach Telegram unescaped, not as literal "&lt;b&gt;" text');
  });
});

test('an immediate private_artifact message is fetched from the private bucket and delivered as chat TEXT, never a document', async () => {
  await withServers(async ({ telegram, agent }) => {
    const key = 'resumes/augmented/backend/run-1.md';
    __setTestS3(fakeS3({ objects: { [`private/${key}`]: Buffer.from('# Tailored resume') } }));
    agent.when('/telegram/update', () => ({
      body: {
        accepted: true,
        task_id: null,
        immediate_messages: [{
          chat_id: '42',
          delivery_kind: 'private_artifact',
          artifact: { bucket: 'private_user_artifacts', key, checksum_sha256: 'x', byte_size: 18 },
          caption: 'Your tailored resume',
        }],
      },
    }));
    telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 2 } } }));
    const { forwardToAgent } = await loadInbound();

    await forwardToAgent(TOKEN, { chatId: '42', updateId: 6, text: 'Tailor my resume for this JD: ...' });

    assert.equal(telegram.requests.length, 1);
    assert.equal(telegram.requests[0].path, `/bot${TOKEN}/sendMessage`);
    const body = JSON.parse(telegram.requests[0].bodyRaw.toString('utf8'));
    assert.match(body.text, /<b>Your tailored resume<\/b>/);
    assert.match(body.text, /# Tailored resume/);
  });
});

test('a bare task_id with no immediate messages gets a "working on it" acknowledgement', async () => {
  await withServers(async ({ telegram, agent }) => {
    agent.when('/telegram/update', () => ({ body: { accepted: true, task_id: 'task-1', immediate_messages: [] } }));
    telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
    const { forwardToAgent } = await loadInbound();

    await forwardToAgent(TOKEN, { chatId: '42', updateId: 5, text: 'Tailor my resume for a senior backend role' });

    assert.equal(telegram.requests.length, 1);
    const body = JSON.parse(telegram.requests[0].bodyRaw.toString('utf8'));
    assert.match(body.text, /Working on it/);
  });
});

test('an unreachable agent app apologizes instead of losing the update silently', async () => {
  const telegram = await startFakeTelegramServer();
  process.env.J4N_TELEGRAM_API_BASE = telegram.baseUrl;
  process.env.AGENT_API_BASE = 'http://127.0.0.1:1'; // nothing listens here
  telegram.when('/sendMessage', () => ({ body: { ok: true, result: { message_id: 1 } } }));
  try {
    const { forwardToAgent } = await loadInbound();
    await forwardToAgent(TOKEN, { chatId: '42', updateId: 5, text: 'hello' });

    assert.equal(telegram.requests.length, 1);
    const body = JSON.parse(telegram.requests[0].bodyRaw.toString('utf8'));
    assert.match(body.text, /unreachable/);
  } finally {
    await telegram.close();
    delete process.env.J4N_TELEGRAM_API_BASE;
    delete process.env.AGENT_API_BASE;
  }
});
