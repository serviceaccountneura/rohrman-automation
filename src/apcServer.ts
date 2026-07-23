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
import { TekionSession } from './playwright/tekionPo.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 3000);
const WEBHOOK_PATH = process.env.APC_WEBHOOK_PATH || '/webhooks/tekion';

const app = express();

// Webhook route must come BEFORE express.json() so HMAC sees the raw body.
if (process.env.APC_WEBHOOK_SECRET) {
  const wh = new WebhookRouter(process.env.APC_WEBHOOK_SECRET);
  // Default: log every event. Register specific handlers below as you build.
  wh.on('*', (event) => {
    console.log(`[WEBHOOK] [${new Date(event.meta.eventTime).toISOString()}] ` +
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

// ─── PO creation endpoint (receives OCR JSON from Python pipeline) ───────────

type OcrPayload = Record<string, any> & {
  po_type: string;
};

const PO_TYPES = new Set([
  'SUBLET',
  'OEM_STOCK_ORDER',
  'OEM_SPECIAL_ORDER',
  'VENDOR_STOCK_ORDER',
  'VENDOR_SPECIAL_ORDER',
  'MISCELLANEOUS',
  'VENDOR_CREDIT_PO',
]);

// Lazy TekionSession — created on first PO request, reused for subsequent ones.
let tekionSession: TekionSession | null = null;

async function getTekionSession(): Promise<TekionSession> {
  if (!tekionSession) {
    console.log('[TEKION] Starting browser session (non-headless for debugging)...');
    tekionSession = new TekionSession();
    await tekionSession.start();
  }
  return tekionSession;
}

app.post('/api/po/create', async (req, res) => {
  const payload = req.body as OcrPayload;

  if (!payload || typeof payload !== 'object') {
    return res.status(400).json({ error: 'JSON body required' });
  }

  const poType = String(payload.po_type || '').toUpperCase();
  if (!poType) {
    return res.status(400).json({ error: 'po_type is required', valid_types: [...PO_TYPES] });
  }
  if (!PO_TYPES.has(poType)) {
    return res.status(400).json({ error: `Invalid po_type: ${poType}`, valid_types: [...PO_TYPES] });
  }

  console.log(`\n[PO] POST /api/po/create -- po_type=${poType}`);
  console.log(`   vendor=${payload.vendor?.name ?? '?'}  total=${payload.total ?? '?'}  items=${payload.line_items?.length ?? 0}`);

  try {
    const session = await getTekionSession();
    const result = await session.createPoFromOcr(payload);
    console.log(`[PO] Automation complete: ${result.poNumber} (${result.state})`);

    res.status(200).json({
      success: true,
      message: 'PO created via Playwright automation',
      po_type: poType,
      po_number: result.poNumber,
      po_state: result.state,
      control_number: result.controlNumber ?? null,
    });
  } catch (err) {
    console.error(`[PO] Automation failed: ${err}`);
    // Reset session on error so next attempt gets a fresh login
    if (tekionSession) {
      await tekionSession.stop().catch(() => {});
      tekionSession = null;
    }
    res.status(500).json({
      success: false,
      error: String(err),
      po_type: poType,
    });
  }
});

app.listen(PORT, () => {
  console.log(`\n APC + PO server: http://localhost:${PORT}`);
  console.log('   POST /api/po/create  — create PO from OCR JSON (Playwright automation)');
  console.log('   POST /api/apc/call   — forward to APC API');
  console.log('   POST /api/apc/upload — file upload to APC API');
  console.log('   Required env: APC_BASE_URL, APC_APP_ID, APC_DEALER_ID, APC_TOKEN');
  console.log('   PO automation env: TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET');
  if (process.env.APC_WEBHOOK_SECRET) {
    console.log(`  Webhook receiver mounted at POST ${WEBHOOK_PATH}`);
    console.log('    Expose this URL publicly (cloudflared/ngrok/hosted) and');
    console.log('    register it in APC → My Configurations as the Destination URL.');
  } else {
    console.log('   (Set APC_WEBHOOK_SECRET in .env to enable the webhook receiver.)');
  }
  console.log('   Edit src/apc-endpoints.ts to add/refine endpoints from the APC docs.\n');
});
