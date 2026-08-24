// test/repo.test.mjs — db/repo.mjs against the fake RDS client. Verifies the
// SQL/parameter shapes repo functions send AND that responses are correctly
// reassembled into the shapes bot.mjs/cli.mjs expect (in particular, the
// protocol/core.mjs-compatible state object loadFullState() returns).
import test from 'node:test';
import assert from 'node:assert/strict';
import { ExecuteStatementCommand, BatchExecuteStatementCommand } from '@aws-sdk/client-rds-data';
import { __setTestClient } from '../src/db/client.mjs';
import * as repo from '../src/db/repo.mjs';
import { shortHash } from '../src/protocol/core.mjs';
import { createFakeRdsClient, paramValue } from './helpers/fake-rds-client.mjs';

process.env.AWS_REGION ||= 'us-east-1';
process.env.AURORA_RESOURCE_ARN ||= 'arn:aws:rds:us-east-1:000000000000:cluster:fake';
process.env.AURORA_SECRET_ARN ||= 'arn:aws:secretsmanager:us-east-1:000000000000:secret:fake';
process.env.AURORA_DATABASE ||= 'job4menow_test';

test.afterEach(() => __setTestClient(null));

test('insertQueue: one transaction, batches items and artifacts, commits', async () => {
  const fake = createFakeRdsClient();
  __setTestClient(fake);

  await repo.insertQueue({
    id: 'tg-1', title: 'Batch A', source: 'career-ops', items: [
      { n: 1, id: 'report:001', report_num: '001', company: 'Acme', role: 'Engineer', url: '', score: '', location: '', salary: '', legitimacy: '', summary: {}, contacts: [], can_send_cv: true, can_send_contacts: true },
    ],
    artifacts: [{ n: 1, kind: 'cv_pdf', s3Key: 'queues/tg-1/1-cv_pdf.pdf', byteSize: 123, checksum: 'abc' }],
  });

  const names = fake.calls.map((c) => c.name);
  assert.deepEqual(names, [
    'BeginTransactionCommand',
    'ExecuteStatementCommand', // insert into queues
    'BatchExecuteStatementCommand', // queue_items
    'BatchExecuteStatementCommand', // item_artifacts
    'CommitTransactionCommand',
  ]);

  const queuesInsert = fake.calls[1].input;
  assert.match(queuesInsert.sql, /INSERT INTO queues/);
  assert.equal(paramValue(queuesInsert.parameters, 'item_count').longValue, 1);

  const itemsBatch = fake.calls[2].input;
  assert.match(itemsBatch.sql, /INSERT INTO queue_items/);
  assert.equal(itemsBatch.parameterSets.length, 1);
  assert.equal(paramValue(itemsBatch.parameterSets[0], 'company').stringValue, 'Acme');

  const artifactsBatch = fake.calls[3].input;
  assert.match(artifactsBatch.sql, /INSERT INTO item_artifacts/);
  assert.equal(paramValue(artifactsBatch.parameterSets[0], 's3_key').stringValue, 'queues/tg-1/1-cv_pdf.pdf');
});

test('insertQueue: skips the item_artifacts batch entirely when there are no artifacts', async () => {
  const fake = createFakeRdsClient();
  __setTestClient(fake);
  await repo.insertQueue({
    id: 'tg-2', title: 'Batch B', items: [
      { n: 1, id: 'report:002', report_num: '002', company: 'Beta', role: 'Analyst', summary: {}, contacts: [] },
    ],
    artifacts: [],
  });
  assert.deepEqual(fake.calls.map((c) => c.name), [
    'BeginTransactionCommand', 'ExecuteStatementCommand', 'BatchExecuteStatementCommand', 'CommitTransactionCommand',
  ]);
});

test('listQueues marks the active queue by id', async () => {
  const fake = createFakeRdsClient();
  fake.when(ExecuteStatementCommand, () => ({
    formattedRecords: JSON.stringify([
      { id: 'tg-2', title: 'B', item_count: 1, ingested_at: '2026-01-02T00:00:00Z' },
      { id: 'tg-1', title: 'A', item_count: 1, ingested_at: '2026-01-01T00:00:00Z' },
    ]),
  }));
  __setTestClient(fake);
  const queues = await repo.listQueues('tg-1');
  assert.equal(queues.find((q) => q.id === 'tg-1').active, true);
  assert.equal(queues.find((q) => q.id === 'tg-2').active, false);
});

test('resolveQueueByPosition maps a 1-based list position to a queue id', async () => {
  const fake = createFakeRdsClient();
  fake.when(ExecuteStatementCommand, () => ({
    formattedRecords: JSON.stringify([
      { id: 'tg-2', title: 'B', item_count: 1, ingested_at: '2026-01-02T00:00:00Z' },
      { id: 'tg-1', title: 'A', item_count: 1, ingested_at: '2026-01-01T00:00:00Z' },
    ]),
  }));
  __setTestClient(fake);
  assert.equal(await repo.resolveQueueByPosition(1), 'tg-2');
  assert.equal(await repo.resolveQueueByPosition(2), 'tg-1');
  assert.equal(await repo.resolveQueueByPosition(99), null);
});

test('loadFullState reconstructs a protocol/core.mjs-compatible state object', async () => {
  const fake = createFakeRdsClient();
  const session = {
    chat_id: '42', queue_id: 'tg-1', status: 'active', cursor: 1,
    telegram_offset: 5, last_update_id: 100, last_message_id: 200,
    stats: { cvs_sent: 0, notes: 0, reviewed: 0, skipped: 0 }, updated_at: '2026-01-01T00:00:00Z',
  };
  fake.when(ExecuteStatementCommand, (cmd) => {
    const { sql } = cmd.input;
    if (/INSERT INTO sessions/.test(sql)) return { numberOfRecordsUpdated: 0 }; // ON CONFLICT DO NOTHING path
    if (/SELECT \* FROM sessions/.test(sql)) return { formattedRecords: JSON.stringify([session]) };
    if (/SELECT id, title, item_count, ingested_at\s+FROM queues WHERE/.test(sql)) {
      return { formattedRecords: JSON.stringify([{ id: 'tg-1', title: 'Batch A', item_count: 1, ingested_at: '2026-01-01T00:00:00Z' }]) };
    }
    if (/FROM queue_items/.test(sql)) {
      return { formattedRecords: JSON.stringify([{ n: 1, id: 'report:001', report_num: '001', company: 'Acme', role: 'Engineer', url: '', score: '', location: '', salary: '', legitimacy: '', summary: {}, contacts: [], can_send_cv: true, can_send_contacts: true, status: 'pending' }]) };
    }
    if (/FROM item_artifacts/.test(sql)) {
      return { formattedRecords: JSON.stringify([{ n: 1, kind: 'cv_pdf', s3_key: 'queues/tg-1/1-cv_pdf.pdf' }]) };
    }
    throw new Error(`unexpected sql in test: ${sql}`);
  });
  __setTestClient(fake);

  const state = await repo.loadFullState('42');
  assert.equal(state.queue_id, 'tg-1');
  assert.equal(state.queue_short, shortHash('tg-1', 6));
  assert.equal(state.total, 1);
  assert.equal(state.telegram.offset, 5);
  assert.equal(state.items[0].company, 'Acme');
  assert.deepEqual(state.items[0].artifacts, { cv_pdf: 'queues/tg-1/1-cv_pdf.pdf' });
  assert.deepEqual(state.items[0].history, []);
  assert.deepEqual(state.notes, []);
});

test('loadFullState returns null when the session has no active queue', async () => {
  const fake = createFakeRdsClient();
  fake.when(ExecuteStatementCommand, (cmd) => {
    if (/SELECT \* FROM sessions/.test(cmd.input.sql)) {
      return { formattedRecords: JSON.stringify([{ chat_id: '42', queue_id: null, status: 'idle' }]) };
    }
    return { numberOfRecordsUpdated: 0 };
  });
  __setTestClient(fake);
  assert.equal(await repo.loadFullState('42'), null);
});

test('acquireLease returns true only when a row was actually claimed', async () => {
  const fake = createFakeRdsClient();
  fake.when(ExecuteStatementCommand, () => ({ numberOfRecordsUpdated: 1 }));
  __setTestClient(fake);
  assert.equal(await repo.acquireLease('42', 'owner-a', 60_000), true);
  const call = fake.calls.find((c) => c.name === 'ExecuteStatementCommand');
  assert.match(call.input.sql, /lease_owner IS NULL OR lease_expires_at < now\(\) OR lease_owner = :owner/);
});

test('acquireLease returns false when zero rows matched (someone else holds a live lease)', async () => {
  const fake = createFakeRdsClient();
  fake.when(ExecuteStatementCommand, () => ({ numberOfRecordsUpdated: 0 }));
  __setTestClient(fake);
  assert.equal(await repo.acquireLease('42', 'owner-a', 60_000), false);
});
