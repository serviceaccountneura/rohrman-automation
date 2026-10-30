/**
 * captureOemJe.ts — OEM Parts Manufacture Ticket JE + PDF attach capture.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:capture:oem-je
 *       npm run pw:capture:oem-je -- /accounting/journalEntry/list
 *
 * Same harness as captureJE.ts, but the goal is the **document upload /
 * attachment** calls that go with Save as Draft. Backend currently posts an
 * empty `assetAttachmentDto`; this run is how we learn the real upload + link
 * sequence from Tekion's UI.
 *
 * WHAT TO CLICK, IN ORDER:
 *   1. Land on the Journal Entry list.
 *   2. Create New Journal Entry → "General" → "Manual".
 *   3. Journal Number/Name = 76 (MANUFACTURERS PARTS STMT).
 *   4. Accounting Date = Parts Ticket invoice date.
 *   5. Description / Reference = Parts Ticket invoice number.
 *   6. Line 1: GL 3000 TRADE CREDITORS  CREDIT  control = MMYY of acct date.
 *   7. Line 2: GL 2410 PARTS INV        DEBIT   (amount from ticket).
 *   8. Confirm Balance reads $0.00.
 *   9. **Attach / upload the invoice PDF** on this JE screen (pause after
 *      upload so the document + link calls show up in the log).
 *  10. Click **Save as Draft** — do NOT Submit.
 *  11. Click the red "STOP" button (top-right) to flush the capture.
 *
 * ★➜ marks JE draft writes; ✎➜ marks document / attachment traffic.
 *
 * Outputs (captured/):
 *   oem-je-live-<ts>.jsonl
 *   oem-je-session-<ts>.har
 *   oem-je-trace-<ts>.zip
 */
import 'dotenv/config';
import { chromium } from 'playwright';
import { appendFileSync, existsSync, mkdirSync, unlinkSync } from 'node:fs';
import { BASE_URL, SESSION_FILE, proxyFromEnv, openLoggedIn } from './login.js';

const START_PATH =
  process.argv[2] || process.env.OEM_JE_START_PATH || '/accounting/journalEntry/list';
const MAX_BODY = 20_000;
const STOP_FILE = 'captured/STOP-OEM-JE';

function ts(): string {
  return String(Date.now());
}

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

function isInteresting(url: string): boolean {
  if (!/tekion(cloud)?\.com/.test(url)) return false;
  if (/\.js($|\?)|\.css($|\?)|\.svg|\.png|\.woff/i.test(url)) return false;
  if (NOISE.test(url)) return false;
  return true;
}

function isLikelyJeWrite(method: string, url: string): boolean {
  if (!['POST', 'PUT', 'PATCH'].includes(method)) return false;
  return /journal|accountingChain|\/je\b|voucher|manualEntry|draft/i.test(url);
}

/** Document upload, link-to-JE, or attachment DTO traffic. */
function isLikelyAttachment(method: string, url: string): boolean {
  if (!['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) return false;
  return /document|attachment|assetAttachment|upload|media|file/i.test(url);
}

async function main() {
  if (!existsSync('captured')) mkdirSync('captured');
  const stamp = ts();
  const harPath = `captured/oem-je-session-${stamp}.har`;
  const tracePath = `captured/oem-je-trace-${stamp}.zip`;
  const livePath = `captured/oem-je-live-${stamp}.jsonl`;

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
    viewport: null,
    recordHar: { path: harPath, content: 'embed' },
  });
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true });

  let stop!: () => void;
  const stopped = new Promise<void>((resolve) => {
    stop = () => resolve();
  });
  await context.exposeBinding('__oemJeCaptureStop', () => {
    console.log('\n⏹  STOP pressed — saving…');
    stop();
  });
  await context.addInitScript(() => {
    if (window.top !== window) return;
    const fire = () =>
      (window as unknown as { __oemJeCaptureStop: () => void }).__oemJeCaptureStop();

    document.addEventListener(
      'keydown',
      (e) => {
        if (e.ctrlKey && e.shiftKey && (e.key === 'X' || e.key === 'x')) {
          e.preventDefault();
          fire();
        }
      },
      true,
    );

    const ensure = () => {
      if (document.getElementById('__oem_je_stop_btn') || !document.body) return;
      const b = document.createElement('button');
      b.id = '__oem_je_stop_btn';
      b.textContent = '⏹ STOP';
      b.title = 'Stop & save capture (Ctrl+Shift+X)';
      b.style.cssText = [
        'position:fixed',
        'top:8px',
        'right:8px',
        'z-index:2147483647',
        'background:#d92d20',
        'color:#fff',
        'border:0',
        'border-radius:6px',
        'padding:6px 12px',
        'font:600 12px system-ui,sans-serif',
        'cursor:pointer',
        'opacity:.9',
        'box-shadow:0 2px 8px rgba(0,0,0,.35)',
      ].join(';');
      b.onclick = () => {
        b.textContent = 'saving…';
        b.style.background = '#666';
        fire();
      };
      document.body.appendChild(b);
    };
    setInterval(ensure, 1000);
    document.addEventListener('DOMContentLoaded', ensure);
    ensure();
  });

  const page = await context.newPage();

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
    } catch {
      /* body not retrievable */
    }

    const record = {
      i: ++n,
      at: new Date().toISOString(),
      method,
      url,
      status: res.status(),
      reqBody: req.postData()?.slice(0, MAX_BODY),
      respBody,
    };
    try {
      appendFileSync(livePath, JSON.stringify(record) + '\n');
    } catch {
      /* best-effort */
    }

    if (res.status() === 401 || res.status() === 403) {
      if (++authFailures === 3) {
        console.log('\n⚠  Several 401/403 responses — the saved session looks expired.');
        console.log('   Stop, delete tekion-auth.json, run `npm run pw:login`, and capture again.\n');
      }
    }

    const isWrite = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);
    let mark = ' ·';
    if (isLikelyAttachment(method, url)) mark = '✎➜';
    else if (isLikelyJeWrite(method, url)) mark = '★➜';
    else if (isWrite) mark = ' ➜';
    console.log(`  ${mark} [${method}] ${res.status()} ${url.replace(/\?.*$/, '')}`);
  });

  await page.goto(`${BASE_URL}${START_PATH}`, { waitUntil: 'domcontentloaded' }).catch(() => {});

  console.log(`\n🎬 Recording → ${livePath}`);
  console.log('   OEM JE + PDF attach — walk slowly:');
  console.log('     1. Create New Journal Entry → General → Manual');
  console.log('     2. Journal 76, date + invoice # on header');
  console.log('     3. GL 3000 credit (control MMYY) + GL 2410 debit → Balance $0.00');
  console.log('     4. **Attach the invoice PDF** (pause after upload)');
  console.log('     5. **Save as Draft** (NOT Submit)');
  console.log('   ★➜ = JE write · ✎➜ = document/attachment call');
  console.log('   When finished: click "⏹ STOP", Ctrl+Shift+X,');
  console.log(`   or create ${STOP_FILE}\n`);

  const disconnected = new Promise<void>((resolve) =>
    browser.on('disconnected', () => resolve()),
  );
  const sentinel = new Promise<void>((resolve) => {
    const timer = setInterval(() => {
      if (existsSync(STOP_FILE)) {
        clearInterval(timer);
        try {
          unlinkSync(STOP_FILE);
        } catch {
          /* best-effort */
        }
        console.log('\n⏹  STOP file detected — saving…');
        resolve();
      }
    }, 1000);
    timer.unref?.();
  });
  await Promise.race([stopped, sentinel, disconnected]);

  try {
    await context.tracing.stop({ path: tracePath });
    await context.close();
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
  console.log(`\nNext: npm run pw:analyze:oem-je`);
}

main().catch((e) => {
  console.error('❌', e);
  process.exit(1);
});
