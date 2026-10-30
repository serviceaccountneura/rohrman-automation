/**
 * captureApproval.ts — Record the AP approval flow's real API calls.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:capture:ap            (starts at the AP/payables list)
 *       npm run pw:capture:ap -- /accounting/payables/invoice/list
 *
 * Differences from capture.ts (which was built for the PO-creation chain):
 *   • No terminal ENTER — a floating "STOP & SAVE" button is injected into the
 *     page, so the run can be driven entirely from the browser.
 *   • Logs READS as well as writes. The approval queue is fetched with GETs/
 *     search POSTs, and we need those to drive approval headlessly later.
 *   • Streams every call to captured/ap-live-<ts>.jsonl as it happens, so the
 *     endpoint data survives even if the browser is killed and the HAR never
 *     flushes.
 *
 * Outputs (captured/):
 *   ap-live-<ts>.jsonl   every Tekion xhr/fetch: method, url, req body, status, resp body
 *   ap-session-<ts>.har  full HAR (flushed on graceful stop)
 *   ap-trace-<ts>.zip    Playwright trace — npx playwright show-trace <file>
 */
import 'dotenv/config';
import { chromium } from 'playwright';
import { appendFileSync, existsSync, mkdirSync, unlinkSync } from 'node:fs';
import { BASE_URL, SESSION_FILE, proxyFromEnv, openLoggedIn } from './login.js';

const START_PATH = process.argv[2] || process.env.AP_START_PATH || '/home';
const MAX_BODY = 20_000; // chars kept per body in the JSONL
/** Create this file to stop the capture from outside the browser. */
const STOP_FILE = 'captured/STOP';

function ts(): string {
  return String(Date.now());
}

/**
 * Noise the Tekion SPA fires constantly and that has nothing to do with the
 * approval flow: session replay, telemetry, notification/clock polling, and the
 * app-shell bootstrap chatter.
 */
const NOISE = new RegExp(
  [
    'openreplay', '/ingest/', 'analytics', 'telemetry', 'datadog', 'segment',
    'sentry', 'newrelic', 'fullstory', 'pusher', 'tnps', 'notificationService',
    'releaseNote', 'user/clock', 'system/alerts', 'application-pins',
    'messaging/metadata', 'accessToken/generate', 'tapapi', 'ui-skeleton',
    '/languages', 'labels/GLOBAL', 'feedback',
  ].join('|'),
  'i',
);

/** Tekion app/API traffic only — skip telemetry and static assets. */
function isInteresting(url: string): boolean {
  if (!/tekion(cloud)?\.com/.test(url)) return false;
  if (/\.js($|\?)|\.css($|\?)|\.svg|\.png|\.woff/i.test(url)) return false;
  if (NOISE.test(url)) return false;
  return true;
}

async function main() {
  if (!existsSync('captured')) mkdirSync('captured');
  const stamp = ts();
  const harPath = `captured/ap-session-${stamp}.har`;
  const tracePath = `captured/ap-trace-${stamp}.zip`;
  const livePath = `captured/ap-live-${stamp}.jsonl`;

  // Ensure we have a session first (performs the TOTP login if needed).
  if (!existsSync(SESSION_FILE)) {
    console.log('No saved session — logging in first…');
    const { browser } = await openLoggedIn(false);
    await browser.close();
  }

  const browser = await chromium.launch({
    headless: false,
    proxy: proxyFromEnv(),
    args: ['--start-maximized'],
  });
  const context = await browser.newContext({
    storageState: SESSION_FILE,
    // viewport:null => the page uses the real window size. A fixed viewport
    // larger than the screen makes Tekion's own buttons unreachable.
    viewport: null,
    recordHar: { path: harPath, content: 'embed' },
  });
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true });

  // ── Stop signal: a button injected into the page (no terminal needed) ──────
  let stop!: () => void;
  const stopped = new Promise<void>((resolve) => { stop = () => resolve(); });
  await context.exposeBinding('__apCaptureStop', () => { console.log('\n⏹  STOP pressed — saving…'); stop(); });
  await context.addInitScript(() => {
    if (window.top !== window) return; // top frame only
    const fire = () => (window as unknown as { __apCaptureStop: () => void }).__apCaptureStop();

    // Keyboard fallback — works even if the button is covered by Tekion's UI.
    document.addEventListener('keydown', (e) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'X' || e.key === 'x')) {
        e.preventDefault();
        fire();
      }
    }, true);

    const ensure = () => {
      if (document.getElementById('__ap_stop_btn') || !document.body) return;
      const b = document.createElement('button');
      b.id = '__ap_stop_btn';
      b.textContent = '⏹ STOP';
      b.title = 'Stop & save capture (Ctrl+Shift+X)';
      b.style.cssText = [
        'position:fixed', 'top:8px', 'right:8px', 'z-index:2147483647',
        'background:#d92d20', 'color:#fff', 'border:0', 'border-radius:6px',
        'padding:6px 12px', 'font:600 12px system-ui,sans-serif', 'cursor:pointer',
        'opacity:.9', 'box-shadow:0 2px 8px rgba(0,0,0,.35)',
      ].join(';');
      b.onclick = () => {
        b.textContent = 'saving…';
        b.style.background = '#666';
        fire();
      };
      document.body.appendChild(b);
    };
    // SPA route changes can wipe the DOM — keep re-adding it.
    setInterval(ensure, 1000);
    document.addEventListener('DOMContentLoaded', ensure);
    ensure();
  });

  const page = await context.newPage();

  // ── Live JSONL log — survives a hard kill ─────────────────────────────────
  let n = 0;
  page.on('response', async (res) => {
    const req = res.request();
    const url = req.url();
    if (!isInteresting(url)) return;
    const type = req.resourceType();
    if (type !== 'xhr' && type !== 'fetch') return;

    const method = req.method();
    let respBody: string | undefined;
    try {
      const ct = (res.headers()['content-type'] || '').toLowerCase();
      if (ct.includes('json') || ct.includes('text')) {
        respBody = (await res.text()).slice(0, MAX_BODY);
      }
    } catch { /* body not retrievable (redirect/aborted) — url+status still useful */ }

    const record = {
      i: ++n,
      at: new Date().toISOString(),
      method,
      url,
      status: res.status(),
      reqBody: req.postData()?.slice(0, MAX_BODY),
      respBody,
    };
    try { appendFileSync(livePath, JSON.stringify(record) + '\n'); } catch { /* best-effort */ }

    const isWrite = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);
    const mark = isWrite ? '➜' : ' ·';
    console.log(`  ${mark} [${method}] ${res.status()} ${url.replace(/\?.*$/, '')}`);
  });

  await page.goto(`${BASE_URL}${START_PATH}`, { waitUntil: 'domcontentloaded' }).catch(() => {});

  console.log(`\n🎬 Recording → ${livePath}`);
  console.log('   Perform the AP approval on a pre-invoiced PO, slowly, one step at a time.');
  console.log('   Every Tekion API call prints below (➜ = write call). Telemetry is filtered out.');
  console.log('   When finished: click "⏹ STOP" (top-right), press Ctrl+Shift+X,');
  console.log(`   or create the file ${STOP_FILE} from a terminal.\n`);

  // Stop on: the in-page button, a `captured/STOP` sentinel file (so the run can
  // be halted from a terminal/agent and still flush the HAR), or the browser
  // being closed manually.
  const disconnected = new Promise<void>((resolve) => browser.on('disconnected', () => resolve()));
  const sentinel = new Promise<void>((resolve) => {
    const timer = setInterval(() => {
      if (existsSync(STOP_FILE)) {
        clearInterval(timer);
        try { unlinkSync(STOP_FILE); } catch { /* best-effort */ }
        console.log('\n⏹  STOP file detected — saving…');
        resolve();
      }
    }, 1000);
    timer.unref?.();
  });
  await Promise.race([stopped, sentinel, disconnected]);

  try {
    await context.tracing.stop({ path: tracePath });
    await context.close(); // flushes the HAR
    await browser.close();
  } catch {
    console.log('   (browser closed early — HAR/trace may be partial; the JSONL is complete)');
  }

  console.log(`\n✅ Captured ${n} API call(s):`);
  console.log(`   Live  : ${livePath}`);
  console.log(`   HAR   : ${harPath}`);
  console.log(`   Trace : ${tracePath}`);
  console.log(`\nNext: npm run pw:analyze:ap`);
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
