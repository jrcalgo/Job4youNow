// test/helpers/fake-agent-server.mjs — a real local HTTP server standing in
// for the agent app's API, wired in via AGENT_API_BASE. Mirrors
// fake-telegram-server.mjs's shape exactly, for the same reason: exercise
// agent/client.mjs's actual request construction end to end rather than
// mocking global fetch.
import { createServer } from 'node:http';

export async function startFakeAgentServer() {
  const requests = [];
  const handlers = [];

  const server = createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const [path, query] = req.url.split('?');
      const record = { method: req.method, path, query: query || '', bodyRaw: Buffer.concat(chunks) };
      requests.push(record);
      const handler = handlers.find((h) => h.match(record));
      const { status = 200, body = [] } = handler ? handler.respond(record) : {};
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(body));
    });
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();

  return {
    requests,
    baseUrl: `http://127.0.0.1:${port}`,
    when(matchPathSuffix, respond) {
      handlers.unshift({ match: (req) => req.path.endsWith(matchPathSuffix), respond });
    },
    async close() {
      await new Promise((resolve) => server.close(resolve));
    },
  };
}
