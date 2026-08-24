// artifacts/store.mjs — S3 as the durable artifact origin, with a local LRU
// cache so the daemon doesn't re-download a CV/JD from S3 on every send.
//
// Split intentionally: S3 objects (uploaded once, at ingest time by cli.mjs)
// never get deleted by this module — only LOCAL cache copies are evicted.
// Losing a cache entry just means the next send re-downloads it; losing an
// S3 object would mean the artifact is gone for good, which is exactly the
// durability property a "local cache only" design (rejected during planning)
// didn't have.
//
// The cache manifest (last-access time + size per key) lives in a JSON file
// inside the cache directory itself — NOT in Aurora. It describes local disk
// state, which is only meaningful to whichever container instance wrote it,
// and touching the database on every artifact send would also defeat Aurora's
// auto-pause (see db/schema.sql's header comment and lib/retry.mjs).
import { createHash } from 'node:crypto';
import {
  createReadStream, createWriteStream, existsSync, mkdirSync, readFileSync,
  renameSync, statSync, unlinkSync, writeFileSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import { pipeline } from 'node:stream/promises';
import { S3Client, PutObjectCommand, GetObjectCommand } from '@aws-sdk/client-s3';
import { log } from '../lib/log.mjs';

function requireEnv(name) {
  const v = process.env[name];
  if (!v) throw new Error(`missing required env var: ${name}`);
  return v;
}

let s3Singleton = null;
let testS3 = null;

/** Test-only seam, mirrors db/client.mjs's __setTestClient. Never used outside test/. */
export function __setTestS3(fake) {
  testS3 = fake;
}

function s3() {
  if (testS3) return testS3;
  if (!s3Singleton) s3Singleton = new S3Client({ region: requireEnv('AWS_REGION') });
  return s3Singleton;
}

function bucket() { return requireEnv('S3_BUCKET'); }
function keyPrefix() { return process.env.S3_PREFIX || ''; }
function cacheDir() { return process.env.J4N_CACHE_DIR || '/app/.cache'; }
function manifestPath() { return join(cacheDir(), '.manifest.json'); }
function maxCacheBytes() { return Number(process.env.J4N_CACHE_MAX_BYTES) || 512 * 1024 * 1024; }

function readManifest() {
  try {
    return JSON.parse(readFileSync(manifestPath(), 'utf8'));
  } catch {
    return {};
  }
}

function writeManifest(manifest) {
  mkdirSync(cacheDir(), { recursive: true });
  const tmp = `${manifestPath()}.tmp`;
  writeFileSync(tmp, JSON.stringify(manifest, null, 2));
  renameSync(tmp, manifestPath());
}

async function sha256File(absPath) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(absPath)) hash.update(chunk);
  return hash.digest('hex');
}

/**
 * Deterministic S3 key for one item's artifact. Exported so cli.mjs (upload)
 * and this module's own download path always agree on where a given
 * (queueId, n, kind) artifact lives, without threading the key around as
 * extra state.
 */
export function artifactKey(queueId, n, kind, ext = '') {
  return `queues/${queueId}/${n}-${kind}${ext}`;
}

/**
 * Upload a local file (already validated by protocol/core.mjs's
 * assertSafeArtifactPath at the ingestion boundary) to S3. Idempotent by
 * design — re-ingesting the same queue id overwrites the same key rather
 * than accumulating orphans.
 * @returns {Promise<{ byteSize: number, checksum: string }>}
 */
export async function uploadArtifact(localAbsPath, key) {
  const checksum = await sha256File(localAbsPath);
  const byteSize = statSync(localAbsPath).size;
  await s3().send(new PutObjectCommand({
    Bucket: bucket(),
    Key: `${keyPrefix()}${key}`,
    Body: createReadStream(localAbsPath),
    ContentLength: byteSize,
    Metadata: { sha256: checksum },
  }));
  return { byteSize, checksum };
}

/**
 * Resolve a local, readable path for `key`, downloading from S3 into the
 * cache on a miss. Every call refreshes the manifest's lastAccess for `key`
 * (that's what makes eviction LRU) and may trigger eviction of OTHER,
 * colder entries if the cache is now over budget — never the entry just
 * touched.
 * @returns {Promise<string>} absolute local path
 */
export async function getArtifactPath(key) {
  const manifest = readManifest();
  const localAbs = join(cacheDir(), key);

  if (existsSync(localAbs)) {
    manifest[key] = { size: statSync(localAbs).size, lastAccess: Date.now() };
    writeManifest(manifest);
    return localAbs;
  }

  mkdirSync(dirname(localAbs), { recursive: true });
  const res = await s3().send(new GetObjectCommand({ Bucket: bucket(), Key: `${keyPrefix()}${key}` }));
  await pipeline(res.Body, createWriteStream(localAbs));

  manifest[key] = { size: statSync(localAbs).size, lastAccess: Date.now() };
  writeManifest(manifest);
  evictIfNeeded(manifest, key);
  return localAbs;
}

/**
 * Evict least-recently-used cache entries (local files only — S3 is
 * untouched) until total cache size is back under J4N_CACHE_MAX_BYTES.
 * `protectedKey` (the entry just written/touched) is never evicted, even if
 * it alone exceeds the budget — a single oversized file shouldn't be able to
 * delete itself the moment it's downloaded.
 */
function evictIfNeeded(manifest, protectedKey = null) {
  let total = Object.values(manifest).reduce((sum, e) => sum + (e.size || 0), 0);
  const limit = maxCacheBytes();
  if (total <= limit) return;

  const oldestFirst = Object.entries(manifest)
    .filter(([key]) => key !== protectedKey)
    .sort((a, b) => a[1].lastAccess - b[1].lastAccess);

  for (const [key, meta] of oldestFirst) {
    if (total <= limit) break;
    const abs = join(cacheDir(), key);
    try { if (existsSync(abs)) unlinkSync(abs); } catch (err) { log.warn('cache evict: unlink failed', { key, error: err.message }); }
    delete manifest[key];
    total -= meta.size || 0;
    log.info('artifact cache: evicted', { key, freedBytes: meta.size, totalBytesAfter: total });
  }
  writeManifest(manifest);
}

/** Diagnostic snapshot — used by cli.mjs state / bot.mjs health logging. */
export function cacheStats() {
  const manifest = readManifest();
  const entries = Object.values(manifest);
  return {
    fileCount: entries.length,
    totalBytes: entries.reduce((sum, e) => sum + (e.size || 0), 0),
    maxBytes: maxCacheBytes(),
  };
}
