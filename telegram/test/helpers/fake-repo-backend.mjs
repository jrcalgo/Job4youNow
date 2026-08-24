// test/helpers/fake-repo-backend.mjs — routes the fake RDS client's
// ExecuteStatementCommand calls by recognizable SQL shape, backed by a small
// mutable in-memory `sessions` row so multi-step flows (switch queue, then
// read state back) behave consistently within one test. Queues/items are
// static fixtures — ingestion is covered separately in test/repo.test.mjs,
// this file only needs to support what bot.mjs's handleUpdate() reads.
export function createRepoBackend({ queues = [], session = null } = {}) {
  const state = {
    queues, // [{ id, title, item_count, ingested_at, items: [...], artifacts: [{n,kind,s3_key}] }]
    session: session || { chat_id: null, queue_id: null, status: 'idle', cursor: 1, telegram_offset: 0, last_update_id: null, last_message_id: null, stats: { cvs_sent: 0, notes: 0, reviewed: 0, skipped: 0 }, updated_at: new Date().toISOString() },
  };

  function respond(cmd) {
    const sql = cmd.input.sql;

    if (/INSERT INTO sessions/.test(sql)) return { numberOfRecordsUpdated: 0 }; // ON CONFLICT DO NOTHING — session is seeded already

    if (/SELECT \* FROM sessions/.test(sql)) {
      return { formattedRecords: JSON.stringify([state.session]) };
    }

    if (/SELECT id, title, item_count, ingested_at\s+FROM queues\s+ORDER BY/.test(sql)) {
      return { formattedRecords: JSON.stringify(state.queues.map((q) => ({ id: q.id, title: q.title, item_count: q.item_count, ingested_at: q.ingested_at }))) };
    }

    if (/FROM queues WHERE id/.test(sql)) {
      const id = paramString(cmd.input.parameters, 'id');
      const q = state.queues.find((x) => x.id === id);
      return { formattedRecords: JSON.stringify(q ? [{ id: q.id, title: q.title, item_count: q.item_count, ingested_at: q.ingested_at }] : []) };
    }

    if (/FROM queue_items\s+WHERE queue_id/.test(sql)) {
      const queueId = paramString(cmd.input.parameters, 'queue_id');
      const q = state.queues.find((x) => x.id === queueId);
      return { formattedRecords: JSON.stringify(q ? q.items : []) };
    }

    if (/FROM item_artifacts WHERE queue_id/.test(sql)) {
      const queueId = paramString(cmd.input.parameters, 'queue_id');
      const q = state.queues.find((x) => x.id === queueId);
      return { formattedRecords: JSON.stringify(q ? q.artifacts || [] : []) };
    }

    if (/UPDATE sessions\s+SET queue_id = :queue_id, cursor = 1/.test(sql)) {
      state.session.queue_id = paramString(cmd.input.parameters, 'queue_id');
      state.session.cursor = 1;
      state.session.status = 'active';
      state.session.stats = JSON.parse(paramString(cmd.input.parameters, 'stats'));
      return { numberOfRecordsUpdated: 1 };
    }

    if (/UPDATE sessions\s+SET queue_id = NULL/.test(sql)) {
      state.session.queue_id = null;
      state.session.status = 'idle';
      state.session.cursor = 1;
      return { numberOfRecordsUpdated: 1 };
    }

    if (/UPDATE sessions\s+SET cursor = :cursor/.test(sql)) {
      state.session.cursor = paramLong(cmd.input.parameters, 'cursor');
      state.session.status = paramString(cmd.input.parameters, 'status');
      state.session.stats = JSON.parse(paramString(cmd.input.parameters, 'stats'));
      const lastMessageId = cmd.input.parameters.find((p) => p.name === 'last_message_id');
      if (lastMessageId && !lastMessageId.value.isNull) state.session.last_message_id = lastMessageId.value.longValue;
      return { numberOfRecordsUpdated: 1 };
    }

    if (/UPDATE sessions\s+SET telegram_offset/.test(sql)) {
      state.session.telegram_offset = paramLong(cmd.input.parameters, 'offset');
      const lastUpdateId = cmd.input.parameters.find((p) => p.name === 'last_update_id');
      if (lastUpdateId && !lastUpdateId.value.isNull) state.session.last_update_id = lastUpdateId.value.longValue;
      return { numberOfRecordsUpdated: 1 };
    }

    if (/UPDATE queue_items SET status/.test(sql)) {
      const queueId = paramString(cmd.input.parameters, 'queue_id');
      const n = paramLong(cmd.input.parameters, 'n');
      const status = paramString(cmd.input.parameters, 'status');
      const q = state.queues.find((x) => x.id === queueId);
      const item = q?.items.find((it) => it.n === n);
      if (item) item.status = status;
      return { numberOfRecordsUpdated: item ? 1 : 0 };
    }

    if (/INSERT INTO session_notes/.test(sql) || /INSERT INTO session_history/.test(sql)) {
      return { numberOfRecordsUpdated: 1 };
    }

    if (/UPDATE sessions\s+SET lease_owner/.test(sql) || /lease_owner = NULL/.test(sql)) {
      return { numberOfRecordsUpdated: 1 };
    }

    throw new Error(`fake-repo-backend: no route for SQL: ${sql}`);
  }

  return { state, respond };
}

function paramString(parameters, name) {
  const p = parameters.find((x) => x.name === name);
  return p?.value?.stringValue ?? null;
}
function paramLong(parameters, name) {
  const p = parameters.find((x) => x.name === name);
  return p?.value?.longValue ?? null;
}
