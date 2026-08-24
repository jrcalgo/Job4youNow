// lib/log.mjs — tiny structured logger. No dependency: this is a small
// personal service, a full logging framework would be pure overhead.
//
// Lines are single-line JSON on stdout (info/debug) or stderr (warn/error) so
// `docker logs` / `docker compose logs` stay greppable and are easy to ship
// to CloudWatch or similar later without changing the call sites.
const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const configuredLevel = LEVELS[String(process.env.J4N_LOG_LEVEL || 'info').toLowerCase()] ?? LEVELS.info;

let redactList = [];

/** Register secrets (bot token, etc.) to strip from every subsequent log line. */
export function setRedactedSecrets(secrets) {
  redactList = (secrets || []).filter((s) => typeof s === 'string' && s.length >= 6);
}

function redact(str) {
  let out = str;
  for (const s of redactList) out = out.split(s).join('«redacted»');
  return out;
}

function emit(level, msg, meta) {
  if (LEVELS[level] < configuredLevel) return;
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    level,
    msg: redact(String(msg)),
    ...(meta && typeof meta === 'object' ? meta : {}),
  });
  (level === 'error' || level === 'warn' ? process.stderr : process.stdout).write(`${line}\n`);
}

export const log = {
  debug: (msg, meta) => emit('debug', msg, meta),
  info: (msg, meta) => emit('info', msg, meta),
  warn: (msg, meta) => emit('warn', msg, meta),
  error: (msg, meta) => emit('error', msg, meta),
};
