/**
 * capture.ts — Record every network call Tekion makes while you click around.
 *
 * Run:  npm run capture
 *
 * Reuses auth.json (from `npm run auth`) so you start already logged in. It opens
 * a real browser AND records a full HAR file. You then manually perform ONE of
 * each action you want to automate:
 *     - add a vehicle to inventory
 *     - create a deal / customer
 *     - open/create a repair order
 *     - add a part
 * Press ENTER when done. The HAR is written to captured/session-<timestamp>.har.
 *
 * The HAR holds the exact request URLs, methods, headers (incl. auth) and JSON
 * bodies — i.e. the "endpoints we are hitting". Then run `npm run analyze`.
 */
import 'dotenv/config';
import { chromium } from 'playwright';
import { existsSync, mkdirSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { proxyFromEnv } from './proxy.js';

const BASE_URL = process.env.TEKION_BASE_URL || 'https://app.tekioncloud.com';

function waitForEnter(message: string): Promise<void> {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => rl.question(message, () => { rl.close(); resolve(); }));
}

async function main() {
  if (!existsSync('auth.json')) {
    console.error('❌ auth.json not found. Run `npm run auth` first.');
    process.exit(1);
  }
  if (!existsSync('captured')) mkdirSync('captured');

  const harPath = `captured/session-${Date.now()}.har`;

  const browser = await chromium.launch({
    headless: false,
    proxy: proxyFromEnv(),
  });
  const context = await browser.newContext({
    storageState: 'auth.json',
    recordHar: { path: harPath, content: 'embed' },
  });
  const page = await context.newPage();

  // Also stream API calls live to the terminal so you see endpoints as they fire.
  page.on('requestfinished', async (req) => {
    const url = req.url();
    if (!/tekion(cloud)?\.com/.test(url)) return;
    const type = req.resourceType();
    if (type !== 'xhr' && type !== 'fetch') return;
    const method = req.method();
    console.log(`  [${method}] ${url.replace(/\?.*$/, '')}`);
  });

  await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
  console.log('\n🎬 Recording. Perform ONE of each action you want to automate:');
  console.log('   inventory add • create deal/customer • repair order • add part');
  console.log('   (Watch the [METHOD] url lines below — those are your endpoints.)\n');
  await waitForEnter('\nPress ENTER when you have done all the actions… ');

  await context.close(); // flushes the HAR
  await browser.close();
  console.log(`\n✅ Saved traffic to ${harPath}`);
  console.log('Next: run `npm run analyze` to extract a clean endpoint list.');
}

main().catch((e) => { console.error(e); process.exit(1); });
