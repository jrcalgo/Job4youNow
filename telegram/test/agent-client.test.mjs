// test/agent-client.test.mjs — agent/client.mjs against a real local HTTP
// server (see test/helpers/fake-agent-server.mjs), covering request shape
// and the retry-on-5xx / no-retry-on-4xx distinction.
import test from 'node:test';
import assert from 'node:assert/strict';
import { startFakeAgentServer } from './helpers/fake-agent-server.mjs';

async function withServer(fn) {
  const server = await startFakeAgentServer();
  process.env.AGENT_API_BASE = server.baseUrl;
  try {
    return await fn(server);
  } finally {
    await server.close();
    delete process.env.AGENT_API_BASE;
  }
}

async function loadClient() {
  return import('../src/agent/client.mjs');
}

test('fetchPendingOutbox: GET with limit in the querystring, returns parsed rows', async () => {
  await withServer(async (server) => {
    server.when('/telegram/outbox', () => ({ body: [{ id: 'outbox-1', delivery_kind: 'public_text' }] }));
    const { fetchPendingOutbox } = await loadClient();

    const rows = await fetchPendingOutbox(10);

    assert.equal(server.requests[0].method, 'GET');
    assert.match(server.requests[0].query, /limit=10/);
    assert.deepEqual(rows, [{ id: 'outbox-1', delivery_kind: 'public_text' }]);
  });
});

test('acknowledgeOutboxDelivered: POSTs to the outbox id-specific path', async () => {
  await withServer(async (server) => {
    server.when('/delivered', () => ({ body: { ok: true } }));
    const { acknowledgeOutboxDelivered } = await loadClient();

    await acknowledgeOutboxDelivered('outbox-1');

    assert.equal(server.requests[0].method, 'POST');
    assert.equal(server.requests[0].path, '/telegram/outbox/outbox-1/delivered');
  });
});

test('a 500 is retried automatically and eventually succeeds', async () => {
  await withServer(async (server) => {
    let calls = 0;
    server.when('/telegram/outbox', () => {
      calls += 1;
      if (calls === 1) return { status: 500, body: {} };
      return { body: [] };
    });
    const { fetchPendingOutbox } = await loadClient();

    const rows = await fetchPendingOutbox();

    assert.equal(calls, 2);
    assert.deepEqual(rows, []);
  });
});

test('forwardTelegramUpdate: POSTs the command wrapped in an envelope, returns the parsed result', async () => {
  await withServer(async (server) => {
    server.when('/telegram/update', () => ({
      body: { accepted: true, task_id: null, immediate_messages: [{ chat_id: '42', delivery_kind: 'public_text', text: 'hi' }] },
    }));
    const { forwardTelegramUpdate } = await loadClient();

    const command = { chat_id: '42', update_id: 7, message_id: 3, text: 'hello', callback: null };
    const result = await forwardTelegramUpdate(command);

    assert.equal(server.requests[0].method, 'POST');
    assert.equal(server.requests[0].path, '/telegram/update');
    assert.deepEqual(JSON.parse(server.requests[0].bodyRaw.toString('utf8')), { command });
    assert.equal(result.accepted, true);
    assert.equal(result.immediate_messages[0].text, 'hi');
  });
});

test('a 404 is NOT retried — surfaces immediately with status set', async () => {
  await withServer(async (server) => {
    let calls = 0;
    server.when('/delivered', () => {
      calls += 1;
      return { status: 404, body: { detail: 'not found' } };
    });
    const { acknowledgeOutboxDelivered } = await loadClient();

    await assert.rejects(
      () => acknowledgeOutboxDelivered('missing'),
      (err) => {
        assert.equal(err.status, 404);
        return true;
      },
    );
    assert.equal(calls, 1, 'a clean 4xx means "ask differently", not "try again"');
  });
});
