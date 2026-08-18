/**
 * captureJE.ts — Record the Journal Entry creation flow's real API calls.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:capture:je            (starts at the Journal Entry list)
 *       npm run pw:capture:je -- /accounting/journalEntry/list
 *
 * Same harness as captureApproval.ts, pointed at the Parts Manufacture Ticket
 * SOP instead of AP approval:
 *
 *   Create New Journal Entry -> General -> Manual
 *     -> Journal Number/Name = 76 (MANUFACTURERS PARTS STMT)
 *     -> Accounting Date     = the Parts Ticket invoice date
 *     -> Description / Reference = the Parts Ticket invoice number
 *     -> line 1: GL 3000 TRADE CREDITORS   credit, control = MMYY
 *     -> line 2: GL 2410 PARTS INV         debit
 *     -> SAVE AS DRAFT      <-- click this, it is the call we need
 *
 * WHAT TO CLICK, IN ORDER (this is the point of the run):
 *   1. Land on the Journal Entry list.
 *   2. Create New Journal Entry -> the "General" box -> "Manual".
 *   3. Fill the header and BOTH GL lines until Balance reads $0.00.
 *   4. Click "Save as Draft"  — do NOT click Submit.
 *   5. Click the "STOP" button (top-right) to flush the capture.
 *
 * Go slowly and pause a beat between steps; every call prints as it happens, so
 * the console order matches the SOP order and the write calls are easy to spot.
 *
 * Outputs (captured/):
 *   je-live-<ts>.jsonl   every Tekion xhr/fetch: method, url, req body, status, resp body
 *   je-session-<ts>.har  full HAR (flushed on graceful stop)
 *   je-trace-<ts>.zip    Playwright trace — npx playwright show-trace <file>
 */
import 'dotenv/config';
import { chromium } from 'playwright';
import { appendFileSync, existsSync, mkdirSync, unlinkSync } from 'node:fs';
import { BASE_URL, SESSION_FILE, proxyFromEnv, openLoggedIn } from './login.js';

const START_PATH = process.argv[2] || process.env.JE_START_PATH || '/accounting/journalEntry/list';
const MAX_BODY = 20_000; // chars kept per body in the JSONL
/** Create this file to stop the capture from outside the browser. */
const STOP_FILE = 'captured/STOP-JE';

function ts(): string {
  return String(Date.now());
}

/**
 * Noise the Tekion SPA fires constantly and that has nothing to do with the
 * journal entry flow: session replay, telemetry, notification/clock polling, and
 * the app-shell bootstrap chatter.
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

/**
 * Calls worth shouting about in the console. The JE write we are hunting for is
 * a POST/PUT somewhere under /accounting, so highlight those loudly — it makes
 * the "Save as Draft" click obvious in a busy log.
 */
function isLikelyJeWrite(method: string, url: string): boolean {
  if (!['POST', 'PUT', 'PATCH'].includes(method)) return false;
  return /journal|accountingChain|\/je\b|voucher|manualEntry/i.test(url);
}

async function main() {
  if (!existsSync('captured')) mkdirSync('captured');
  const stamp = ts();
  const harPath = `captured/je-session-${stamp}.har`;
  const tracePath = `captured/je-trace-${stamp}.zip`;
  const livePath = `captured/je-live-${stamp}.jsonl`;

  // Ensure we have a session first (performs the TOTP login if needed).
  // A stale storage state is the usual reason a capture comes back full of
  // 401s, so re-login rather than trusting whatever is on disk.
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
  await context.exposeBinding('__jeCaptureStop', () => { console.log('\n⏹  STOP pressed — saving…'); stop(); });
  await context.addInitScript(() => {
    if (window.top !== window) return; // top frame only
    const fire = () => (window as unknown as { __jeCaptureStop: () => void }).__jeCaptureStop();

    // Keyboard fallback — works even if the button is covered by Tekion's UI.
    document.addEventListener('keydown', (e) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'X' || e.key === 'x')) {
        e.preventDefault();
        fire();
      }
    }, true);

    const ensure = () => {
      if (document.getElementById('__je_stop_btn') || !document.body) return;
      const b = document.createElement('button');
      b.id = '__je_stop_btn';
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
  let authFailures = 0;
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

    // A dead session produces a capture full of 401s and no usable payloads —
    // the last je-live capture was lost that way. Say so immediately.
    if (res.status() === 401 || res.status() === 403) {
      if (++authFailures === 3) {
        console.log('\n⚠  Several 401/403 responses — the saved session looks expired.');
        console.log('   Stop, delete tekion-auth.json, run `npm run pw:login`, and capture again.\n');
      }
    }

    const isWrite = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);
    const mark = isLikelyJeWrite(method, url) ? '★➜' : isWrite ? ' ➜' : ' ·';
    console.log(`  ${mark} [${method}] ${res.status()} ${url.replace(/\?.*$/, '')}`);
  });

  await page.goto(`${BASE_URL}${START_PATH}`, { waitUntil: 'domcontentloaded' }).catch(() => {});

  console.log(`\n🎬 Recording → ${livePath}`);
  console.log('   Walk the Parts Manufacture Ticket SOP, slowly, one step at a time:');
  console.log('     1. Create New Journal Entry → "General" → "Manual"');
  console.log('     2. Journal Number/Name = 76, Accounting Date = invoice date');
  console.log('     3. Description + Reference = invoice number');
  console.log('     4. Line 1 = GL 3000 (credit, control MMYY), Line 2 = GL 2410 (debit)');
  console.log('     5. Confirm Balance $0.00, then click **Save as Draft** (NOT Submit)');
  console.log('   ★➜ marks a likely journal-entry write call — that is the one we need.');
  console.log('   When finished: click "⏹ STOP" (top-right), press Ctrl+Shift+X,');
  console.log(`   or create the file ${STOP_FILE} from a terminal.\n`);

  // Stop on: the in-page button, a `captured/STOP-JE` sentinel file (so the run
  // can be halted from a terminal/agent and still flush the HAR), or the browser
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
  if (authFailures > 0) {
    console.log(`\n⚠  ${authFailures} call(s) returned 401/403 — check the capture is usable.`);
  }
  console.log(`\nNext: npm run pw:analyze:je`);
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
