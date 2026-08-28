// artifacts/store.mjs — S3 as the durable artifact origin, with a local LRU
// cache per bucket so the daemon doesn't re-download an artifact on every
// send. Two buckets exist, split by privacy boundary (see the Python
// agent app's agent/app/models/artifacts.py's ArtifactBucket — the same
// vocabulary, mirrored here):
//
//   - job_artifacts: this bot's own CV/JD delivery for job-queue review.
//     Unchanged pipeline from before the agent app existed — legacy
//     S3_BUCKET/S3_PREFIX env vars still work as the fallback, so an
//     existing deployment needs no env changes for this half.
//   - private_user_artifacts: agent-app-produced private content
//     (augmented resumes, private responses) — a SEPARATE bucket with its
//     own IAM policy, delivered via agent/outbox.mjs.
//
// Split intentionally: S3 objects (uploaded once, at ingest time by
// cli.mjs, or by the agent app) never get deleted by this module — only
// LOCAL cache copies are evicted, with one manifest/byte-budget PER bucket
// so a burst of private-artifact downloads can never evict a job
// artifact's cache entry, or vice versa.
//
// The cache manifest (last-access time + size per key) lives in a JSON file
// inside each bucket's own cache subdirectory — NOT in Aurora. It describes
// local disk state, which is only meaningful to whichever container
// instance wrote it, and touching a database on every artifact send would
// also defeat Aurora's auto-pause (see db/schema.sql's header comment and
// lib/retry.mjs) — moot here anyway, since this daemon touches no database
// at all (see the "Telegram writes no DB" boundary).
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

// job_artifacts falls back to the original S3_BUCKET/S3_PREFIX names, so
// this pipeline's env vars don't have to change for existing deployments.
const BUCKET_CONFIGS = {
  job_artifacts: {
    bucket: () => process.env.JOB_ARTIFACTS_BUCKET || requireEnv('S3_BUCKET'),
    prefix: () => process.env.JOB_ARTIFACTS_PREFIX ?? process.env.S3_PREFIX ?? '',
    cacheSubdir: 'job-artifacts',
  },
  private_user_artifacts: {
    bucket: () => requireEnv('PRIVATE_USER_ARTIFACTS_BUCKET'),
    prefix: () => process.env.PRIVATE_USER_ARTIFACTS_PREFIX ?? 'private/',
    cacheSubdir: 'private-user-artifacts',
  },
};

function configFor(bucketKind) {
  const config = BUCKET_CONFIGS[bucketKind];
  if (!config) throw new Error(`artifacts/store.mjs: unknown bucket kind "${bucketKind}"`);
  return config;
}

function cacheRootDir() { return process.env.J4N_CACHE_DIR || '/app/.cache'; }
function cacheDirFor(bucketKind) { return join(cacheRootDir(), configFor(bucketKind).cacheSubdir); }
function manifestPathFor(bucketKind) { return join(cacheDirFor(bucketKind), '.manifest.json'); }
function maxCacheBytes() { return Number(process.env.J4N_CACHE_MAX_BYTES) || 512 * 1024 * 1024; }

function readManifest(bucketKind) {
  try {
    return JSON.parse(readFileSync(manifestPathFor(bucketKind), 'utf8'));
  } catch {
    return {};
  }
}

function writeManifest(bucketKind, manifest) {
  mkdirSync(cacheDirFor(bucketKind), { recursive: true });
  const tmp = `${manifestPathFor(bucketKind)}.tmp`;
  writeFileSync(tmp, JSON.stringify(manifest, null, 2));
  renameSync(tmp, manifestPathFor(bucketKind));
}

async function sha256File(absPath) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(absPath)) hash.update(chunk);
  return hash.digest('hex');
}

/**
 * Deterministic S3 key for one item's job artifact. Exported so cli.mjs
 * (upload) and this module's own download path always agree on where a
 * given (queueId, n, kind) artifact lives, without threading the key
 * around as extra state. private_user_artifacts keys are never generated
 * here — they come from the agent app itself, via an outbox row's
 * `artifact_ref.key` (see agent/outbox.mjs).
 */
export function artifactKey(queueId, n, kind, ext = '') {
  return `queues/${queueId}/${n}-${kind}${ext}`;
}

/**
 * Upload a local file to the named bucket kind. Idempotent by design — the
 * same key overwrites the same object rather than accumulating orphans.
 * @returns {Promise<{ byteSize: number, checksum: string }>}
 */
export async function uploadToBucket(bucketKind, localAbsPath, key) {
  const config = configFor(bucketKind);
  const checksum = await sha256File(localAbsPath);
  const byteSize = statSync(localAbsPath).size;
  await s3().send(new PutObjectCommand({
    Bucket: config.bucket(),
    Key: `${config.prefix()}${key}`,
    Body: createReadStream(localAbsPath),
    ContentLength: byteSize,
    Metadata: { sha256: checksum },
  }));
  return { byteSize, checksum };
}

/**
 * job_artifacts-only convenience wrapper — cli.mjs's `ingest` command
 * (already validated by protocol/core.mjs's assertSafeArtifactPath) is the
 * only caller. Kept as its own export, unchanged from before the
 * private_user_artifacts bucket existed, so nothing calling this needs to
 * change.
 * @returns {Promise<{ byteSize: number, checksum: string }>}
 */
export async function uploadArtifact(localAbsPath, key) {
  return uploadToBucket('job_artifacts', localAbsPath, key);
}

/**
 * Resolve a local, readable path for `key` in the given bucket kind,
 * downloading from S3 into that bucket's own cache subdirectory on a miss.
 * Every call refreshes the manifest's lastAccess for `key` (that's what
 * makes eviction LRU) and may trigger eviction of OTHER, colder entries IN
 * THE SAME BUCKET'S cache if it's now over budget — never the entry just
 * touched, and never another bucket's cache.
 * @returns {Promise<string>} absolute local path
 */
export async function getArtifactPathFromBucket(bucketKind, key) {
  const config = configFor(bucketKind);
  const manifest = readManifest(bucketKind);
  const localAbs = join(cacheDirFor(bucketKind), key);

  if (existsSync(localAbs)) {
    manifest[key] = { size: statSync(localAbs).size, lastAccess: Date.now() };
    writeManifest(bucketKind, manifest);
    return localAbs;
  }

  mkdirSync(dirname(localAbs), { recursive: true });
  const res = await s3().send(new GetObjectCommand({ Bucket: config.bucket(), Key: `${config.prefix()}${key}` }));
  await pipeline(res.Body, createWriteStream(localAbs));

  manifest[key] = { size: statSync(localAbs).size, lastAccess: Date.now() };
  writeManifest(bucketKind, manifest);
  evictIfNeeded(bucketKind, manifest, key);
  return localAbs;
}

/**
 * job_artifacts-only convenience wrapper — telegram/effects.mjs's CV/JD
 * delivery is the only caller, unchanged from before the
 * private_user_artifacts bucket existed.
 * @returns {Promise<string>} absolute local path
 */
export async function getArtifactPath(key) {
  return getArtifactPathFromBucket('job_artifacts', key);
}

/**
 * Resolve a local path for an agent-app ArtifactLocation-shaped ref — see
 * agent/app/models/artifacts.py, the Python side of this exact shape
 * (`{ bucket, key, checksumSha256, byteSize, localBackupPath }`, produced
 * as JSON on an outbox row's `artifact_ref`). The one entry point
 * agent/outbox.mjs uses, regardless of which of the two buckets it names.
 * @returns {Promise<string>} absolute local path
 */
export async function getArtifactPathFromRef(ref) {
  return getArtifactPathFromBucket(ref.bucket, ref.key);
}

/**
 * Evict least-recently-used cache entries for ONE bucket (local files only
 * — S3 is untouched) until that bucket's cache size is back under
 * J4N_CACHE_MAX_BYTES. `protectedKey` (the entry just written/touched) is
 * never evicted, even if it alone exceeds the budget — a single oversized
 * file shouldn't be able to delete itself the moment it's downloaded.
 */
function evictIfNeeded(bucketKind, manifest, protectedKey = null) {
  let total = Object.values(manifest).reduce((sum, e) => sum + (e.size || 0), 0);
  const limit = maxCacheBytes();
  if (total <= limit) return;

  const oldestFirst = Object.entries(manifest)
    .filter(([key]) => key !== protectedKey)
    .sort((a, b) => a[1].lastAccess - b[1].lastAccess);

  for (const [key, meta] of oldestFirst) {
    if (total <= limit) break;
    const abs = join(cacheDirFor(bucketKind), key);
    try { if (existsSync(abs)) unlinkSync(abs); } catch (err) { log.warn('cache evict: unlink failed', { bucketKind, key, error: err.message }); }
    delete manifest[key];
    total -= meta.size || 0;
    log.info('artifact cache: evicted', { bucketKind, key, freedBytes: meta.size, totalBytesAfter: total });
  }
  writeManifest(bucketKind, manifest);
}

/**
 * Diagnostic snapshot — used by cli.mjs state / bot.mjs health logging.
 * Defaults to job_artifacts (the only bucket that existed before this
 * function took a `bucketKind` argument), so existing callers need no change.
 */
export function cacheStats(bucketKind = 'job_artifacts') {
  const manifest = readManifest(bucketKind);
  const entries = Object.values(manifest);
  return {
    fileCount: entries.length,
    totalBytes: entries.reduce((sum, e) => sum + (e.size || 0), 0),
    maxBytes: maxCacheBytes(),
  };
}
