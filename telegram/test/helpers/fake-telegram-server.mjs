// test/helpers/fake-telegram-server.mjs — a real local HTTP server standing
// in for api.telegram.org, wired in via J4N_TELEGRAM_API_BASE. Using a real
// server (rather than mocking global fetch) exercises telegram/api.mjs's
// actual request construction end to end, including headers, querystrings,
// and multipart bodies.
import { createServer } from 'node:http';

export async function startFakeTelegramServer() {
  const requests = [];
  /** @type {Array<{ match: (req: {method:string,path:string}) => boolean, respond: (req: any) => { status?: number, body: object } }>} */
  const handlers = [];

  const server = createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const [path, query] = req.url.split('?');
      const record = { method: req.method, path, query: query || '', headers: req.headers, bodyRaw: Buffer.concat(chunks) };
      requests.push(record);
      const handler = handlers.find((h) => h.match(record));
      const { status = 200, body = { ok: true, result: {} } } = handler ? handler.respond(record) : {};
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
