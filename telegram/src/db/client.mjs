// db/client.mjs — thin wrapper over @aws-sdk/client-rds-data (Aurora Data API).
//
// Every SQL call in this codebase goes through exec()/execBatch()/tx() here
// rather than touching the SDK directly, for two reasons: (1) formatRecordsAs
// 'JSON' still needs its `formattedRecords` string parsed on every call, and
// (2) every call needs the auto-pause/resume retry in lib/retry.mjs —
// centralizing both means no call site can forget either.
//
// Uses named parameters (`:name` in SQL text, matched by `{ name, value }` in
// the parameters array) — this is a Data API–level substitution, not the
// underlying wire protocol's positional placeholders, so it works the same
// way for the Postgres-compatible engine as the MySQL-compatible one.
import {
  RDSDataClient,
  ExecuteStatementCommand,
  BatchExecuteStatementCommand,
  BeginTransactionCommand,
  CommitTransactionCommand,
  RollbackTransactionCommand,
} from '@aws-sdk/client-rds-data';
import { withAuroraRetry } from '../lib/retry.mjs';
import { log } from '../lib/log.mjs';

function requireEnv(name) {
  const v = process.env[name];
  if (!v) throw new Error(`missing required env var: ${name}`);
  return v;
}

let clientSingleton = null;
let testClient = null;

/**
 * Test-only seam: substitute a fake `{ send(cmd) }` for the real RDSDataClient
 * so test/repo.test.mjs can exercise exec()/execBatch()/tx()'s SQL and
 * parameter shaping without touching AWS. Call with `null` to restore the
 * real client. Never used outside test/.
 */
export function __setTestClient(fake) {
  testClient = fake;
}

function client() {
  if (testClient) return testClient;
  if (!clientSingleton) {
    clientSingleton = new RDSDataClient({ region: requireEnv('AWS_REGION') });
  }
  return clientSingleton;
}

function commonArgs() {
  return {
    resourceArn: requireEnv('AURORA_RESOURCE_ARN'),
    secretArn: requireEnv('AURORA_SECRET_ARN'),
    database: process.env.AURORA_DATABASE || 'job4menow',
  };
}

/**
 * Convert one plain JS value into a Data API SqlParameter. Arrays/plain
 * objects are JSON-stringified with typeHint 'JSON' — pair with an explicit
 * `::jsonb` cast in the SQL text for jsonb columns; typeHint alone isn't a
 * substitute for being explicit in the query.
 */
export function toParam(name, value) {
  if (value === null || value === undefined) return { name, value: { isNull: true } };
  if (typeof value === 'boolean') return { name, value: { booleanValue: value } };
  if (typeof value === 'number') {
    return Number.isInteger(value)
      ? { name, value: { longValue: value } }
      : { name, value: { doubleValue: value } };
  }
  if (value instanceof Date) return { name, value: { stringValue: value.toISOString() }, typeHint: 'TIMESTAMP' };
  if (Array.isArray(value) || typeof value === 'object') {
    return { name, value: { stringValue: JSON.stringify(value) }, typeHint: 'JSON' };
  }
  return { name, value: { stringValue: String(value) } };
}

function paramsFrom(obj) {
  return Object.entries(obj || {}).map(([k, v]) => toParam(k, v));
}

/**
 * Execute one statement.
 * @param {string} sql - use :name placeholders, matching keys in `params`
 * @param {object} [params]
 * @param {{ transactionId?: string }} [opts]
 * @returns {Promise<{ rows: object[], numberOfRecordsUpdated?: number }>}
 */
export async function exec(sql, params = {}, opts = {}) {
  return withAuroraRetry(async () => {
    const cmd = new ExecuteStatementCommand({
      ...commonArgs(),
      sql,
      parameters: paramsFrom(params),
      formatRecordsAs: 'JSON',
      transactionId: opts.transactionId,
    });
    const res = await client().send(cmd);
    if (res.formattedRecords) {
      return { rows: JSON.parse(res.formattedRecords) };
    }
    return { rows: [], numberOfRecordsUpdated: res.numberOfRecordsUpdated };
  });
}

/**
 * Execute the same SQL once per row of `paramSets` in a single round trip.
 * BatchExecuteStatement never returns a result set regardless of
 * formatRecordsAs (see AWS's "Using the Data API" JSON-format docs) — use
 * this for bulk INSERT/UPDATE only, never for a SELECT you need rows back from.
 */
export async function execBatch(sql, paramSets, opts = {}) {
  if (!paramSets.length) return { updateResults: [] };
  return withAuroraRetry(async () => {
    const cmd = new BatchExecuteStatementCommand({
      ...commonArgs(),
      sql,
      parameterSets: paramSets.map(paramsFrom),
      transactionId: opts.transactionId,
    });
    const res = await client().send(cmd);
    return { updateResults: res.updateResults };
  });
}

/**
 * Run `fn(transactionId)` inside a Data API transaction: commits on success,
 * rolls back on any thrown error (including a failed commit attempt itself
 * left un-rolled-back only if the rollback call also fails, which is logged).
 * `fn` must pass the given transactionId to every exec()/execBatch() call it
 * makes, or those statements run outside the transaction.
 */
export async function tx(fn) {
  const { transactionId } = await withAuroraRetry(() => client().send(new BeginTransactionCommand(commonArgs())));
  try {
    const result = await fn(transactionId);
    await withAuroraRetry(() => client().send(new CommitTransactionCommand({ ...commonArgs(), transactionId })));
    return result;
  } catch (err) {
    try {
      await client().send(new RollbackTransactionCommand({ ...commonArgs(), transactionId }));
    } catch (rollbackErr) {
      log.error('transaction rollback also failed', { error: rollbackErr?.message });
    }
    throw err;
  }
}
