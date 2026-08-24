// test/store.test.mjs — artifacts/store.mjs against a fake S3 client (see
// __setTestS3) plus a real temp directory for the local cache. File I/O
// itself is exercised for real; only the network boundary is faked.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { createHash } from 'node:crypto';
import { PutObjectCommand, GetObjectCommand } from '@aws-sdk/client-s3';
import {
  __setTestS3, artifactKey, cacheStats, getArtifactPath, uploadArtifact,
} from '../src/artifacts/store.mjs';

process.env.AWS_REGION ||= 'us-east-1';
process.env.S3_BUCKET ||= 'fake-bucket';
process.env.S3_PREFIX = 'job4younow-telegram/';

function fakeS3({ objects = {} } = {}) {
  const calls = [];
  return {
    calls,
    async send(command) {
      calls.push(command);
      if (command instanceof PutObjectCommand) {
        // Materialize the body so later GetObjectCommand calls in the same
        // test can read back exactly what was "uploaded".
        const chunks = [];
        for await (const chunk of command.input.Body) chunks.push(chunk);
        objects[command.input.Key] = Buffer.concat(chunks);
        return {};
      }
      if (command instanceof GetObjectCommand) {
        const buf = objects[command.input.Key];
        if (!buf) throw Object.assign(new Error('NoSuchKey'), { name: 'NoSuchKey' });
        return { Body: Readable.from([buf]) };
      }
      throw new Error(`fake-s3: unhandled command ${command.constructor.name}`);
    },
  };
}

function withTempCacheDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), 'j4n-cache-'));
  const prevDir = process.env.J4N_CACHE_DIR;
  const prevMax = process.env.J4N_CACHE_MAX_BYTES;
  process.env.J4N_CACHE_DIR = dir;
  return Promise.resolve(fn(dir)).finally(() => {
    process.env.J4N_CACHE_DIR = prevDir;
    process.env.J4N_CACHE_MAX_BYTES = prevMax;
    rmSync(dir, { recursive: true, force: true });
  });
}

test.afterEach(() => __setTestS3(null));

test('artifactKey is deterministic given the same (queueId, n, kind)', () => {
  assert.equal(artifactKey('tg-1', 3, 'cv_pdf', '.pdf'), 'queues/tg-1/3-cv_pdf.pdf');
});

test('uploadArtifact computes the real sha256 + size and PUTs under S3_PREFIX', async () => {
  await withTempCacheDir(async (dir) => {
    const srcPath = join(dir, 'source.pdf');
    const content = Buffer.from('hello cv pdf bytes');
    writeFileSync(srcPath, content);
    const expectedSha = createHash('sha256').update(content).digest('hex');

    const fake = fakeS3();
    __setTestS3(fake);
    const key = artifactKey('tg-1', 1, 'cv_pdf', '.pdf');
    const result = await uploadArtifact(srcPath, key);

    assert.equal(result.byteSize, content.length);
    assert.equal(result.checksum, expectedSha);
    assert.equal(fake.calls.length, 1);
    assert.equal(fake.calls[0].input.Key, `job4younow-telegram/${key}`);
    assert.equal(fake.calls[0].input.Metadata.sha256, expectedSha);
  });
});

test('getArtifactPath downloads on a cache miss, then serves the SAME file on a hit without calling S3 again', async () => {
  await withTempCacheDir(async () => {
    const key = artifactKey('tg-1', 1, 'jd', '.md');
    const content = Buffer.from('# JD text');
    const fake = fakeS3({ objects: { [`job4younow-telegram/${key}`]: content } });
    __setTestS3(fake);

    const path1 = await getArtifactPath(key);
    assert.equal(readFileSync(path1, 'utf8'), '# JD text');
    assert.equal(fake.calls.filter((c) => c instanceof GetObjectCommand).length, 1);

    const path2 = await getArtifactPath(key);
    assert.equal(path2, path1);
    assert.equal(fake.calls.filter((c) => c instanceof GetObjectCommand).length, 1, 'second call must be a cache hit');

    const stats = cacheStats();
    assert.equal(stats.fileCount, 1);
    assert.equal(stats.totalBytes, content.length);
  });
});

test('LRU eviction removes the coldest entry once the cache exceeds its byte budget, but never the entry just downloaded', async () => {
  await withTempCacheDir(async () => {
    process.env.J4N_CACHE_MAX_BYTES = '15'; // tiny — forces eviction after two ~10-byte files
    const keyA = artifactKey('tg-1', 1, 'cv_pdf', '.pdf');
    const keyB = artifactKey('tg-1', 2, 'cv_pdf', '.pdf');
    const objects = {
      [`job4younow-telegram/${keyA}`]: Buffer.from('AAAAAAAAAA'), // 10 bytes, downloaded first (oldest)
      [`job4younow-telegram/${keyB}`]: Buffer.from('BBBBBBBBBB'), // 10 bytes, downloaded second (newest)
    };
    __setTestS3(fakeS3({ objects }));

    const pathA = await getArtifactPath(keyA);
    // Ensure a strictly later lastAccess timestamp for B than A.
    await new Promise((r) => setTimeout(r, 5));
    await getArtifactPath(keyB);

    assert.equal(existsSync(pathA), false, 'the colder entry (A) should have been evicted');
    const stats = cacheStats();
    assert.equal(stats.fileCount, 1);
    assert.ok(stats.totalBytes <= 15);
  });
});
