/**
 * auth.ts — Log into Tekion once (in a real browser window) and save the session.
 *
 * Run:  npm run auth
 *
 * Opens a headed Chromium pointed at Tekion. You log in by hand (handles MFA,
 * captcha, SSO — anything). When you're fully inside the app, press ENTER in the
 * terminal. We then persist:
 *   - auth.json        → cookies + localStorage (Playwright "storageState")
 *   - the bearer token we can find in localStorage (printed + saved)
 *
 * Everything downstream (capture, replay) reuses auth.json so you don't log in
 * every time. auth.json is gitignored — it contains live credentials.
 */
import 'dotenv/config';
import { chromium, type Page } from 'playwright';
import { writeFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { proxyFromEnv } from './proxy.js';
import { isSessionValid } from './session.js';
import { currentTotp } from './totp.js';

const BASE_URL = process.env.TEKION_BASE_URL || 'https://app.tekioncloud.com';
const HAS_TOTP = !!process.env.TEKION_TOTP_SECRET;
const HAS_CREDS = !!(process.env.TEKION_USERNAME && process.env.TEKION_PASSWORD);

function waitForEnter(message: string): Promise<void> {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => rl.question(message, () => { rl.close(); resolve(); }));
}

/**
 * Best-effort: fill username + password if env-provided. Tekion's exact field
 * names may differ; we try the common variants. Falls back silently to manual
 * entry if no selector matches.
 */
async function tryAutoLogin(page: Page): Promise<void> {
  if (!HAS_CREDS) return;
  const userSelectors = ['input[name="username"]', 'input[name="email"]', 'input[type="email"]', 'input[autocomplete="username"]'];
  const passSelectors = ['input[name="password"]', 'input[type="password"]', 'input[autocomplete="current-password"]'];
  for (const sel of userSelectors) {
    const loc = page.locator(sel).first();
    if (await loc.count().catch(() => 0)) { await loc.fill(process.env.TEKION_USERNAME!); break; }
  }
  for (const sel of passSelectors) {
    const loc = page.locator(sel).first();
    if (await loc.count().catch(() => 0)) { await loc.fill(process.env.TEKION_PASSWORD!); break; }
  }
}

/**
 * Best-effort: poll for the OTP input and auto-fill it from TEKION_TOTP_SECRET.
 * If no matching input appears within timeoutMs, give up so the user can type
 * the code manually (it's still being printed to the terminal as a fallback).
 */
async function tryAutoOtp(page: Page, timeoutMs = 120_000): Promise<boolean> {
  if (!HAS_TOTP) return false;
  const selectors = [
    'input[autocomplete="one-time-code"]',
    'input[name="otp"]',
    'input[name="code"]',
    'input[name="verificationCode"]',
    'input[name="mfaCode"]', 'input[name="mfa_code"]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]', 'input[placeholder*="OTP" i]',
  ];
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    for (const sel of selectors) {
      const loc = page.locator(sel).first();
      if (await loc.count().catch(() => 0)) {
        const { code } = currentTotp();
        await loc.fill(code);
        console.log(`🔐 Auto-filled OTP (${code}) via selector ${sel}.`);
        return true;
      }
    }
    await page.waitForTimeout(750);
  }
  return false;
}

/**
 * Keep the current TOTP code visible in the terminal as a manual-entry fallback.
 * Returns a function that stops the ticker.
 */
function startTotpTicker(): () => void {
  if (!HAS_TOTP) return () => {};
  let lastCode = '';
  const tick = () => {
    try {
      const { code, secondsRemaining } = currentTotp();
      if (code !== lastCode) {
        console.log(`🔐 OTP now: ${code}   (rotates in ${secondsRemaining}s)`);
        lastCode = code;
      }
    } catch { /* swallow — secret may be misconfigured */ }
  };
  tick();
  const t = setInterval(tick, 1000);
  return () => clearInterval(t);
}

async function main() {
  // If a previous session is still alive, don't make the user do MFA again.
  if (await isSessionValid()) {
    console.log('✅ auth.json is still valid — skipping login. No MFA required.');
    console.log('   (Delete auth.json if you need to force a fresh login.)');
    return;
  }

  const browser = await chromium.launch({
    headless: false,
    proxy: proxyFromEnv(),
  });
  const context = await browser.newContext();
  const page = await context.newPage();

  await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
  console.log('\n👉 Logging into Tekion.');
  if (HAS_CREDS) {
    await tryAutoLogin(page);
    console.log('   ✏️  Auto-filled username + password (verify on screen, click Sign In).');
  } else {
    console.log('   Enter your username + password in the opened window.');
  }
  console.log('   💡 TIP: tick "Trust this device" to extend the session for ~30–90 days.\n');

  // Show / auto-fill the OTP — both at once, so whichever works wins.
  const stopTicker = startTotpTicker();
  const otpTask = tryAutoOtp(page).catch(() => false);

  await waitForEnter('   Press ENTER once you are fully inside the dashboard… ');
  await otpTask; stopTicker();

  // Save cookies + localStorage for reuse.
  await context.storageState({ path: 'auth.json' });
  console.log('✅ Saved session to auth.json');

  // Best-effort: pull a bearer/JWT-looking value out of localStorage so you can
  // see exactly what token the SPA uses for Authorization headers.
  const tokens = await page.evaluate(() => {
    const out: Record<string, string> = {};
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)!;
      const v = localStorage.getItem(k) || '';
      // JWTs are three base64 segments separated by dots, or stored in a JSON blob.
      if (/eyJ[\w-]+\.[\w-]+\.[\w-]+/.test(v) || /token|auth|jwt|bearer/i.test(k)) {
        out[k] = v.length > 400 ? v.slice(0, 400) + '…(truncated)' : v;
      }
    }
    return out;
  });

  if (Object.keys(tokens).length) {
    writeFileSync('captured-localstorage-tokens.json', JSON.stringify(tokens, null, 2));
    console.log('🔑 Token-looking localStorage keys saved to captured-localstorage-tokens.json:');
    console.log('   ' + Object.keys(tokens).join(', '));
  } else {
    console.log('ℹ️  No obvious token in localStorage — Tekion may use httpOnly cookies.');
    console.log('   That is fine: auth.json still carries the cookies for replay.');
  }

  await browser.close();
  console.log('\nNext: run `npm run capture` to record the API calls each action makes.');
}

main().catch((e) => { console.error(e); process.exit(1); });
