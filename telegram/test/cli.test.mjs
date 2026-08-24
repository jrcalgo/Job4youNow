// test/cli.test.mjs — cli.mjs's ingest/queues/state/reset command bodies,
// exercised in-process against the fake RDS client + fake S3, with a real
// temp directory standing in for a producer's checkout (e.g. career-ops).
//
// Only success paths are tested in-process, since fail() calls
// process.exit() — that's exactly right for a real CLI invocation, but would
// kill the test runner itself if triggered here. The one failure path this
// file needs to prove (a missing artifact file) is exercised as a real
// subprocess instead, checking the exit code the way a producer script
// actually would.
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { PutObjectCommand } from '@aws-sdk/client-s3';
import { BeginTransactionCommand, BatchExecuteStatementCommand, CommitTransactionCommand } from '@aws-sdk/client-rds-data';

process.env.AWS_REGION ||= 'us-east-1';
process.env.AURORA_RESOURCE_ARN ||= 'arn:aws:rds:us-east-1:000000000000:cluster:fake';
process.env.AURORA_SECRET_ARN ||= 'arn:aws:secretsmanager:us-east-1:000000000000:secret:fake';
process.env.S3_BUCKET ||= 'fake-bucket';
process.env.S3_PREFIX = 'job4menow-telegram/';
process.env.TELEGRAM_CHAT_ID ||= '42';

const { cmdIngest, cmdQueues, cmdState, cmdReset } = await import('../src/cli.mjs');
const { __setTestClient } = await import('../src/db/client.mjs');
const { __setTestS3 } = await import('../src/artifacts/store.mjs');
const { createFakeRdsClient } = await import('./helpers/fake-rds-client.mjs');
const { createRepoBackend } = await import('./helpers/fake-repo-backend.mjs');

// Each cmd* function in cli.mjs returns the same object it prints (see
// cli.mjs's outJson) specifically so tests can assert on a real return value
// instead of capturing process.stdout.write — doing the latter from inside
// node:test's own runner is unreliable, since the runner instruments stdout
// itself for TAP reporting.
function withArgv(extraArgs, fn) {
  const prev = process.argv;
  process.argv = [...prev.slice(0, 2), ...extraArgs];
  return Promise.resolve(fn()).finally(() => { process.argv = prev; });
}

function makeProducerRoot() {
  const dir = mkdtempSync(join(tmpdir(), 'j4n-producer-'));
  mkdirSync(join(dir, 'output'), { recursive: true });
  writeFileSync(join(dir, 'output', '001-acme-cv.pdf'), '%PDF-fake');
  const queuePath = join(dir, 'queue.json');
  writeFileSync(queuePath, JSON.stringify({
    title: 'From producer',
    items: [{
      company: 'Acme', role: 'Engineer', report_num: '1',
      artifacts: { cv_pdf: 'output/001-acme-cv.pdf' },
    }],
  }));
  return { dir, queuePath };
}

test.afterEach(() => { __setTestClient(null); __setTestS3(null); });

// uploadArtifact() passes a live createReadStream() as PutObjectCommand's
// Body — a fake send() that doesn't drain it leaves a dangling read on a
// file this test deletes in its `finally`, which surfaces as an async ENOENT
// well after the test itself has finished. Always drain Body for real.
async function drainingFakeS3(calls) {
  return {
    async send(cmd) {
      calls.push(cmd);
      if (cmd.input.Body && typeof cmd.input.Body[Symbol.asyncIterator] === 'function') {
        for await (const _chunk of cmd.input.Body) { /* drain only — content isn't asserted on here */ }
      }
      return {};
    },
  };
}

test('ingest: uploads the artifact to S3 and writes queue+items+artifacts in one transaction', async () => {
  const { dir, queuePath } = makeProducerRoot();
  try {
    const s3Calls = [];
    __setTestS3(await drainingFakeS3(s3Calls));
    const rds = createFakeRdsClient();
    __setTestClient(rds);

    const parsed = await withArgv(['ingest', '--queue', queuePath, '--root', dir], cmdIngest);
    assert.equal(parsed.status, 'ingested');
    assert.equal(parsed.item_count, 1);
    assert.equal(parsed.artifacts_uploaded, 1);

    assert.equal(s3Calls.length, 1);
    assert.ok(s3Calls[0] instanceof PutObjectCommand);
    assert.match(s3Calls[0].input.Key, new RegExp(`^job4menow-telegram/queues/${parsed.queue_id}/1-cv_pdf\\.pdf$`));

    const names = rds.calls.map((c) => c.name);
    assert.deepEqual(names, ['BeginTransactionCommand', 'ExecuteStatementCommand', 'BatchExecuteStatementCommand', 'BatchExecuteStatementCommand', 'CommitTransactionCommand']);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('ingest --dry-run touches neither S3 nor Aurora', async () => {
  const { dir, queuePath } = makeProducerRoot();
  try {
    const s3Calls = [];
    __setTestS3(await drainingFakeS3(s3Calls));
    const rds = createFakeRdsClient();
    __setTestClient(rds);

    const parsed = await withArgv(['ingest', '--queue', queuePath, '--root', dir, '--dry-run'], cmdIngest);
    assert.equal(parsed.status, 'dry-run');
    assert.equal(parsed.item_count, 1);
    assert.equal(s3Calls.length, 0);
    assert.equal(rds.calls.length, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('queues / state / reset render sensible output against an idle session', async () => {
  const rds = createFakeRdsClient();
  const backend = createRepoBackend({
    session: { chat_id: '42', queue_id: null, status: 'idle', cursor: 1, telegram_offset: 0, stats: {} },
    queues: [{ id: 'tg-1', title: 'Batch A', item_count: 1, ingested_at: '2026-01-01T00:00:00Z', items: [], artifacts: [] }],
  });
  rds.calls = []; // createFakeRdsClient's own .calls array
  const wrapped = { calls: rds.calls, send: (cmd) => { rds.calls.push(cmd); return backend.respond(cmd); } };
  __setTestClient(wrapped);

  const queuesOut = await withArgv(['queues', '--json'], cmdQueues);
  assert.deepEqual(queuesOut, [{ id: 'tg-1', title: 'Batch A', item_count: 1, ingested_at: '2026-01-01T00:00:00Z', active: false }]);

  const stateOut = await withArgv(['state', '--json'], cmdState);
  assert.deepEqual(stateOut, { status: 'empty' });

  const resetOut = await withArgv(['reset', '--force'], cmdReset);
  assert.deepEqual(resetOut, { status: 'reset', chat_id: '42' });
});

// cli.mjs's fail() calls process.exit() directly, which is fatal to whatever
// process calls it — safe and correct for a real CLI invocation, but it
// would kill the test runner itself if triggered in-process. Both scenarios
// below run cli.mjs as a real subprocess instead, checking the exit code and
// stderr the way a producer script actually would.
function runCliSubprocess(args, queueContents, { root } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'j4n-producer-'));
  const queuePath = join(dir, 'queue.json');
  writeFileSync(queuePath, JSON.stringify(queueContents));
  try {
    execFileSync(process.execPath, [resolve('src/cli.mjs'), ...args, '--queue', queuePath, '--root', root || dir], {
      env: { ...process.env, AWS_REGION: 'us-east-1' }, stdio: 'pipe',
    });
    return { exitCode: 0 };
  } catch (err) {
    return { exitCode: err.status, stderr: err.stderr.toString() };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('cli.mjs subprocess: a missing artifact file exits non-zero with a clear stderr message', () => {
  const result = runCliSubprocess(['ingest'], {
    items: [{ company: 'Acme', role: 'Engineer', artifacts: { cv_pdf: 'output/does-not-exist.pdf' } }],
  });
  assert.notEqual(result.exitCode, 0);
  assert.match(result.stderr, /artifact file not found/);
});

test('cli.mjs subprocess: a path-traversal artifact is rejected by validateQueue before any upload is attempted', () => {
  const result = runCliSubprocess(['ingest'], {
    items: [{ company: 'Acme', role: 'Engineer', artifacts: { cv_pdf: '../../etc/passwd' } }],
  });
  assert.notEqual(result.exitCode, 0);
  assert.match(result.stderr, /escapes repository root/);
});
