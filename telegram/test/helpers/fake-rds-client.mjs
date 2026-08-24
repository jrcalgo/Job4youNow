// test/helpers/fake-rds-client.mjs — a fake standing in for RDSDataClient's
// `.send()`, wired in via db/client.mjs's __setTestClient() seam. Records
// every command sent (for assertions on the SQL/params db/repo.mjs
// generates) and answers with programmable canned responses, matched by
// command class name in order — this exercises the real exec/execBatch/tx
// code paths without touching AWS.
import {
  ExecuteStatementCommand,
  BatchExecuteStatementCommand,
  BeginTransactionCommand,
  CommitTransactionCommand,
  RollbackTransactionCommand,
} from '@aws-sdk/client-rds-data';

export function createFakeRdsClient() {
  const calls = [];
  /** @type {Array<{ match: (cmd: any) => boolean, respond: (cmd: any) => any }>} */
  const handlers = [];

  function on(CommandClass, respond) {
    handlers.push({ match: (cmd) => cmd instanceof CommandClass, respond });
  }

  // Sensible defaults so a test only needs to program the response it cares about.
  on(BeginTransactionCommand, () => ({ transactionId: 'fake-tx-1' }));
  on(CommitTransactionCommand, () => ({}));
  on(RollbackTransactionCommand, () => ({}));
  on(ExecuteStatementCommand, () => ({ numberOfRecordsUpdated: 0 }));
  on(BatchExecuteStatementCommand, () => ({ updateResults: [] }));

  return {
    calls,
    /** Insert a handler that takes priority over the defaults above. */
    when(CommandClass, respond) {
      handlers.unshift({ match: (cmd) => cmd instanceof CommandClass, respond });
    },
    async send(command) {
      calls.push({ name: command.constructor.name, input: command.input });
      const handler = handlers.find((h) => h.match(command));
      if (!handler) throw new Error(`fake-rds-client: no handler for ${command.constructor.name}`);
      return handler.respond(command);
    },
  };
}

/** Helper for asserting against `parameters: [{ name, value }]` arrays. */
export function paramValue(parameters, name) {
  const p = parameters.find((x) => x.name === name);
  return p?.value;
}
