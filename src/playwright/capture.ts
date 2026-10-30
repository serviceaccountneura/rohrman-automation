/**
 * capture.ts — Record the real selectors, network calls, and a full trace
 * while you click through the workflow once.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:capture
 *
 * Starts already logged in (reuses tekion-auth.json; logs in first if needed),
 * then records everything while YOU manually perform the full chain in the
 * browser:
 *     Parts → Purchase Order → create (OEM or Misc) → submit to pre-invoice
 *     → Accounts Payable → create vendor invoice + attach the invoice PDF
 *     → Accounting → create the journal entry
 *
 * On finish it writes, under captured/:
 *   • session-<ts>.har    — every request/response (the endpoint + payload map)
 *   • trace-<ts>.zip      — full Playwright trace (DOM snapshots per action;
 *                            open with:  npx playwright show-trace <file>)
 * and prints every write-type API call live as you trigger it.
 *
 * Then run `npm run pw:analyze` to turn the HAR into a clean endpoint list.
 */
import 'dotenv/config';
import { chromium } from 'playwright';
import { existsSync, mkdirSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { BASE_URL, SESSION_FILE, proxyFromEnv, openLoggedIn } from './login.js';

function waitForEnter(msg: string): Promise<void> {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((res) => rl.question(msg, () => { rl.close(); res(); }));
}

function ts(): string {
  // Date.now is fine in a user-run script (not a workflow).
  return String(Date.now());
}

async function main() {
  if (!existsSync('captured')) mkdirSync('captured');
  const stamp = ts();
  const harPath = `captured/session-${stamp}.har`;
  const tracePath = `captured/trace-${stamp}.zip`;

  // Make sure we have a session first (logs in with TOTP if needed).
  if (!existsSync(SESSION_FILE)) {
    console.log('No saved session — logging in first…');
    const { browser } = await openLoggedIn(false);
    await browser.close();
  }

  const browser = await chromium.launch({ headless: false, proxy: proxyFromEnv() });
  const context = await browser.newContext({
    storageState: SESSION_FILE,
    recordHar: { path: harPath, content: 'embed' },
  });
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true });

  const page = await context.newPage();

  // Live endpoint log — print every write-type Tekion API call as it fires.
  page.on('requestfinished', (req) => {
    const url = req.url();
    if (!/tekion(cloud)?\.com/.test(url)) return;
    const type = req.resourceType();
    if (type !== 'xhr' && type !== 'fetch') return;
    const method = req.method();
    if (!['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) return;
    console.log(`  ➜ [${method}] ${url.replace(/\?.*$/, '')}`);
  });

  await page.goto(`${BASE_URL}/parts/purchase-order/list`, { waitUntil: 'domcontentloaded' });

  console.log('\n🎬 Recording. Perform the full chain in the browser, slowly:');
  console.log('   1) Parts → Purchase Order → Create → (OEM or Misc) → fill → submit to pre-invoice');
  console.log('   2) Accounts Payable → Create Invoice → attach the invoice PDF → match PO');
  console.log('   3) Accounting → Create Journal Entry → post');
  console.log('\n   (Write calls appear below as you trigger them.)\n');
  await waitForEnter('\nPress ENTER when the whole chain is done… ');

  await context.tracing.stop({ path: tracePath });
  await context.close(); // flushes the HAR
  await browser.close();

  console.log(`\n✅ Saved:`);
  console.log(`   HAR   : ${harPath}`);
  console.log(`   Trace : ${tracePath}   (view: npx playwright show-trace ${tracePath})`);
  console.log(`\nNext: npm run pw:analyze`);
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
