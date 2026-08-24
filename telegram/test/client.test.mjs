// test/client.test.mjs — db/client.mjs against a fake RDSDataClient (see
// test/helpers/fake-rds-client.mjs). Proves the parameter shaping,
// formattedRecords parsing, and transaction commit/rollback wiring without
// touching real AWS.
import test from 'node:test';
import assert from 'node:assert/strict';
import { ExecuteStatementCommand } from '@aws-sdk/client-rds-data';
import { exec, execBatch, tx, toParam, __setTestClient } from '../src/db/client.mjs';
import { createFakeRdsClient, paramValue } from './helpers/fake-rds-client.mjs';

// db/client.mjs's requireEnv() checks these on first use.
process.env.AWS_REGION ||= 'us-east-1';
process.env.AURORA_RESOURCE_ARN ||= 'arn:aws:rds:us-east-1:000000000000:cluster:fake';
process.env.AURORA_SECRET_ARN ||= 'arn:aws:secretsmanager:us-east-1:000000000000:secret:fake';
process.env.AURORA_DATABASE ||= 'job4menow_test';

test.afterEach(() => __setTestClient(null));

test('toParam maps JS types to Data API SqlParameter shapes', () => {
  assert.deepEqual(toParam('a', null), { name: 'a', value: { isNull: true } });
  assert.deepEqual(toParam('a', true), { name: 'a', value: { booleanValue: true } });
  assert.deepEqual(toParam('a', 7), { name: 'a', value: { longValue: 7 } });
  assert.deepEqual(toParam('a', 7.5), { name: 'a', value: { doubleValue: 7.5 } });
  assert.deepEqual(toParam('a', 'x'), { name: 'a', value: { stringValue: 'x' } });
  assert.deepEqual(toParam('a', { b: 1 }), { name: 'a', value: { stringValue: '{"b":1}' }, typeHint: 'JSON' });
  assert.deepEqual(toParam('a', [1, 2]), { name: 'a', value: { stringValue: '[1,2]' }, typeHint: 'JSON' });
});

test('exec() parses formattedRecords JSON into rows', async () => {
  const fake = createFakeRdsClient();
  fake.when(ExecuteStatementCommand, () => ({ formattedRecords: JSON.stringify([{ id: 1 }, { id: 2 }]) }));
  __setTestClient(fake);

  const { rows } = await exec('SELECT id FROM t');
  assert.deepEqual(rows, [{ id: 1 }, { id: 2 }]);
});

test('exec() surfaces numberOfRecordsUpdated for DML with no result set', async () => {
  const fake = createFakeRdsClient();
  fake.when(ExecuteStatementCommand, () => ({ numberOfRecordsUpdated: 3 }));
  __setTestClient(fake);

  const { rows, numberOfRecordsUpdated } = await exec('UPDATE t SET x = :x', { x: 1 });
  assert.deepEqual(rows, []);
  assert.equal(numberOfRecordsUpdated, 3);
});

test('exec() sends :name placeholders as named SqlParameters', async () => {
  const fake = createFakeRdsClient();
  __setTestClient(fake);
  await exec('SELECT * FROM t WHERE id = :id AND label = :label', { id: 5, label: 'x' });
  const call = fake.calls.find((c) => c.name === 'ExecuteStatementCommand');
  assert.equal(paramValue(call.input.parameters, 'id').longValue, 5);
  assert.equal(paramValue(call.input.parameters, 'label').stringValue, 'x');
});

test('execBatch() is a no-op for an empty paramSets array (never calls the SDK)', async () => {
  const fake = createFakeRdsClient();
  __setTestClient(fake);
  const result = await execBatch('INSERT INTO t VALUES (:x)', []);
  assert.deepEqual(result, { updateResults: [] });
  assert.equal(fake.calls.length, 0);
});

test('tx() commits on success and threads transactionId through to fn', async () => {
  const fake = createFakeRdsClient();
  __setTestClient(fake);
  const seenTxId = await tx(async (transactionId) => {
    await exec('INSERT INTO t VALUES (1)', {}, { transactionId });
    return transactionId;
  });
  assert.equal(seenTxId, 'fake-tx-1');
  assert.deepEqual(fake.calls.map((c) => c.name), [
    'BeginTransactionCommand', 'ExecuteStatementCommand', 'CommitTransactionCommand',
  ]);
  assert.equal(fake.calls[1].input.transactionId, 'fake-tx-1');
});

test('tx() rolls back and rethrows when fn throws', async () => {
  const fake = createFakeRdsClient();
  __setTestClient(fake);
  await assert.rejects(
    () => tx(async () => { throw new Error('boom'); }),
    /boom/,
  );
  assert.deepEqual(fake.calls.map((c) => c.name), ['BeginTransactionCommand', 'RollbackTransactionCommand']);
});
