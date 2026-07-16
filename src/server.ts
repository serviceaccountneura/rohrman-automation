/**
 * server.ts — The "platform": a local web form to enter data and push it to Tekion.
 *
 * Run:  npm run server   →  open http://localhost:3000
 *
 * Flow:
 *   1. Form loads the captured endpoints (captured/endpoints.json).
 *   2. You pick a module (inventory / deal / RO / part), edit the JSON payload
 *      (prefilled with the example body captured from the real app), hit Submit.
 *   3. Server replays the call via TekionClient using your saved session.
 *
 * One TekionClient (one logged-in browser) is reused across requests for speed.
 */
import 'dotenv/config';
import express from 'express';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { TekionClient, type CallSpec } from './client.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 3000);

type Endpoint = {
  name: string;
  method: string;
  url: string;
  exampleBody?: unknown;
  relevantHeaders?: string[];
};

function loadEndpoints(): Endpoint[] {
  const p = 'captured/endpoints.json';
  if (!existsSync(p)) return [];
  return JSON.parse(readFileSync(p, 'utf8'));
}

const app = express();
app.use(express.json({ limit: '2mb' }));
app.use(express.static(join(__dirname, '..', 'public')));

// Lazily-opened shared client.
let client: TekionClient | null = null;
async function getClient(): Promise<TekionClient> {
  if (!client) { client = new TekionClient(); await client.open(); }
  return client;
}

app.get('/api/endpoints', (_req, res) => {
  res.json(loadEndpoints());
});

app.post('/api/insert', async (req, res) => {
  const { url, method, body, headers, tokenLocalStorageKey } = req.body as CallSpec;
  if (!url || !method) return res.status(400).json({ error: 'url and method are required' });
  try {
    const c = await getClient();
    const result = await c.call({ url, method, body, headers, tokenLocalStorageKey });
    res.status(200).json(result);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.listen(PORT, () => {
  const endpoints = loadEndpoints();
  console.log(`\n🚀 Data-entry form: http://localhost:${PORT}`);
  if (!existsSync('auth.json')) console.log('⚠️  No auth.json yet — run `npm run auth` first.');
  if (!endpoints.length) console.log('⚠️  No captured endpoints yet — run capture + analyze first.');
  else console.log(`📦 Loaded ${endpoints.length} endpoint(s) from captured/endpoints.json`);
});
