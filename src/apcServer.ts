/**
 * apcServer.ts — Web form that talks to the official APC API.
 *
 * Run:  npm run apc-server   →  open http://localhost:3000
 *
 * Loads the endpoint catalog (src/apc-endpoints.ts), exposes it to the browser
 * UI (public/apc.html), and forwards form submissions through the APC client:
 *   - JSON endpoints   →  POST /api/apc/call
 *   - File upload      →  POST /api/apc/upload (multipart, single file)
 *
 * One APC client instance is created lazily on first request, so env-var
 * problems show up as a clean 500 with the missing key, not at startup.
 */
import 'dotenv/config';
import express from 'express';
import multer from 'multer';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { apcFromEnv, type ApcClient } from './apc.js';
import { APC_ENDPOINTS } from './apc-endpoints.js';
import { WebhookRouter } from './webhooks.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 3000);
const WEBHOOK_PATH = process.env.APC_WEBHOOK_PATH || '/webhooks/tekion';

const app = express();

// Webhook route must come BEFORE express.json() so HMAC sees the raw body.
if (process.env.APC_WEBHOOK_SECRET) {
  const wh = new WebhookRouter(process.env.APC_WEBHOOK_SECRET);
  // Default: log every event. Register specific handlers below as you build.
  wh.on('*', (event) => {
    console.log(`📩 [${new Date(event.meta.eventTime).toISOString()}] ` +
      `${event.meta.eventType} (${event.meta.eventId}) ` +
      `dealer=${event.meta.dealerId}`);
    console.log(`   data: ${JSON.stringify(event.data)}`);
  });
  // Example targeted handler — uncomment and adapt as you add real logic:
  // wh.on('document.status.updated', (e) => updateLocalDocStatus(e.data));
  app.post(WEBHOOK_PATH, express.raw({ type: '*/*', limit: '5mb' }), wh.handler());
}

app.use(express.json({ limit: '2mb' }));
// Serve apc.html as the default page (the Playwright form stays at /index.html).
app.use(express.static(join(__dirname, '..', 'public'), { index: 'apc.html' }));

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 25 * 1024 * 1024 }, // 25 MB invoice images / PDFs
});

let client: ApcClient | null = null;
function getClient(): ApcClient {
  return (client ??= apcFromEnv());
}

app.get('/api/apc/endpoints', (_req, res) => {
  res.json(APC_ENDPOINTS);
});

app.post('/api/apc/call', async (req, res) => {
  const { method, path, body } = req.body as { method?: string; path?: string; body?: unknown };
  if (!method || !path) return res.status(400).json({ error: 'method and path are required' });
  try {
    const r = await getClient().request(method, path, body);
    res.status(200).json(r);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.post('/api/apc/upload', upload.single('file'), async (req, res) => {
  const file = req.file;
  const { path, fileField } = req.body as { path?: string; fileField?: string };
  if (!path || !file) return res.status(400).json({ error: 'path and file are required' });
  try {
    const r = await getClient().upload(path, {
      filename: file.originalname,
      buffer: file.buffer,
      contentType: file.mimetype,
    }, { fileField: fileField || 'file' });
    res.status(200).json(r);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.listen(PORT, () => {
  console.log(`\n🚀 APC data-entry form: http://localhost:${PORT}`);
  console.log('   Required env: APC_BASE_URL, APC_APP_ID, APC_DEALER_ID, APC_TOKEN');
  if (process.env.APC_WEBHOOK_SECRET) {
    console.log(`🪝  Webhook receiver mounted at POST ${WEBHOOK_PATH}`);
    console.log('    Expose this URL publicly (cloudflared/ngrok/hosted) and');
    console.log('    register it in APC → My Configurations as the Destination URL.');
  } else {
    console.log('   (Set APC_WEBHOOK_SECRET in .env to enable the webhook receiver.)');
  }
  console.log('   Edit src/apc-endpoints.ts to add/refine endpoints from the APC docs.\n');
});
