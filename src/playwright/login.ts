/**
 * login.ts — Log into Tekion and save the session for reuse.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:login
 *
 * Opens a real browser, fills username + password (from .env), and auto-fills
 * the 6-digit verification code generated from TEKION_TOTP_SECRET. The current
 * code is also printed in the terminal as a fallback. When you reach the home
 * dashboard the session is saved to tekion-auth.json so later runs (codegen,
 * capture, the production script) start already logged in.
 *
 * Env:  TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET, [US_PROXY]
 */
import 'dotenv/config';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { authenticator } from 'otplib';
import { existsSync } from 'node:fs';
import { createInterface } from 'node:readline';

export const BASE_URL = process.env.TEKION_BASE_URL ?? 'https://app.tekioncloud.com';
export const SESSION_FILE = 'tekion-auth.json';

export function proxyFromEnv() {
  const raw = process.env.US_PROXY?.trim();
  if (!raw) return undefined;
  try {
    const u = new URL(raw);
    return u.username
      ? { server: `${u.protocol}//${u.host}`, username: decodeURIComponent(u.username), password: decodeURIComponent(u.password) }
      : { server: `${u.protocol}//${u.host}` };
  } catch {
    return { server: raw };
  }
}

export function currentTotp(): string {
  const secret = (process.env.TEKION_TOTP_SECRET ?? '').replace(/\s+/g, '');
  if (!secret) throw new Error('TEKION_TOTP_SECRET not set in .env');
  return authenticator.generate(secret);
}

/** Seconds remaining in the current 30s TOTP window. */
function totpSecondsLeft(): number {
  try { return authenticator.timeRemaining(); } catch { return 30; }
}

/**
 * Return a TOTP code guaranteed to differ from `previous`. If the current code
 * is still the same one (window hasn't rolled over yet), wait for the next
 * window so Tekion never sees a reused code.
 */
async function freshTotp(page: Page, previous: string): Promise<string> {
  let code = currentTotp();
  if (code && code === previous) {
    const waitMs = (totpSecondsLeft() + 1) * 1000;
    console.log(`   ⏳ waiting ${Math.ceil(waitMs / 1000)}s for a new code…`);
    await page.waitForTimeout(waitMs);
    code = currentTotp();
  }
  return code;
}

function waitForEnter(msg: string): Promise<void> {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((res) => rl.question(msg, () => { rl.close(); res(); }));
}

const SEL = {
  username: 'input[name="username"], input[type="email"], input[autocomplete="username"]',
  password: 'input[name="password"], input[type="password"]',
  loginSubmit: 'button[type="submit"], button:has-text("Sign In"), button:has-text("Login")',
  otpInput: 'input[autocomplete="one-time-code"], input[name="otp"], input[name="code"], input[placeholder="Type Here"]',
  otpSubmit: 'button:has-text("Verify and Proceed")',
};

export interface LoginResult {
  browser: Browser;
  context: BrowserContext;
  page: Page;
}

/**
 * Open a browser and ensure we are logged in. Reuses tekion-auth.json if a valid
 * session exists; otherwise performs the full username/password/TOTP login and
 * saves it. Returns the live browser/context/page (caller closes them).
 */
export async function openLoggedIn(headless = false): Promise<LoginResult> {
  const browser = await chromium.launch({ headless, proxy: proxyFromEnv() });
  const context = await browser.newContext({
    storageState: existsSync(SESSION_FILE) ? SESSION_FILE : undefined,
    viewport: { width: 1680, height: 1050 },
  });
  const page = await context.newPage();

  await page.goto(`${BASE_URL}/home`, { waitUntil: 'domcontentloaded' });
  if (!/\/login/.test(page.url())) {
    console.log('✅ Reused existing session — already logged in.');
    return { browser, context, page };
  }

  const user = process.env.TEKION_USERNAME;
  const pass = process.env.TEKION_PASSWORD;
  if (!user || !pass) throw new Error('TEKION_USERNAME / TEKION_PASSWORD not set in .env');

  console.log('🔑 Logging in…');
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded' });
  await page.fill(SEL.username, user);
  await page.fill(SEL.password, pass);
  await page.click(SEL.loginSubmit);

  // Verification-code screen — try up to 3 times, each with a fresh TOTP code.
  const otp = page.locator(SEL.otpInput).first();
  await otp.waitFor({ state: 'visible', timeout: 30_000 });

  const MAX_TRIES = 3;
  let lastCode = '';
  let loggedIn = false;
  for (let attempt = 1; attempt <= MAX_TRIES; attempt++) {
    const code = await freshTotp(page, lastCode);
    lastCode = code;
    console.log(`🔐 Attempt ${attempt}/${MAX_TRIES}: entering code ${code}`);

    const field = page.locator(SEL.otpInput).first();
    await field.click({ clickCount: 3 }).catch(() => {}); // select any existing text
    await field.fill('');
    await field.fill(code);
    await page.click(SEL.otpSubmit);

    // Success = we leave the login screen for the dashboard.
    try {
      await page.waitForURL('**/home**', { timeout: 12_000 });
      loggedIn = true;
      break;
    } catch {
      // Or we left /login some other way (still counts as success).
      if (!/\/login/.test(page.url())) { loggedIn = true; break; }
      console.log(`   ⚠️ attempt ${attempt} did not pass — retrying with a fresh code.`);
      // Make sure the OTP field is back/visible before the next try.
      await page.locator(SEL.otpInput).first().waitFor({ state: 'visible', timeout: 10_000 }).catch(() => {});
    }
  }

  if (!loggedIn) {
    console.log(`⏳ Auto-login did not pass after ${MAX_TRIES} tries. Finish in the browser…`);
    await waitForEnter('   Press ENTER once you are on the Tekion home page… ');
  }

  await context.storageState({ path: SESSION_FILE });
  console.log(`✅ Logged in. Session saved to ${SESSION_FILE}.`);
  return { browser, context, page };
}

// CLI entry — log in, save session, keep the window open briefly to confirm.
if (process.argv[1]?.endsWith('login.ts')) {
  (async () => {
    const { browser, context } = await openLoggedIn(false);
    await context.storageState({ path: SESSION_FILE });
    console.log('\nSession ready. You can now run:  npm run pw:codegen   or   npm run pw:capture');
    await waitForEnter('Press ENTER to close the browser… ');
    await browser.close();
  })().catch((e) => { console.error('❌', e); process.exit(1); });
}
