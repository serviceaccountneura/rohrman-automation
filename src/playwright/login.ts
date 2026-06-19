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
import { chromium, type Browser, type BrowserContext, type Page, type Locator } from 'playwright';
import { authenticator } from 'otplib';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
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

/** Return the first locator (from a list of selectors) that becomes visible. */
async function firstVisible(page: Page, selectors: string[], timeoutMs = 15_000): Promise<Locator | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const sel of selectors) {
      const loc = page.locator(sel).first();
      if (await loc.isVisible().catch(() => false)) return loc;
    }
    await page.waitForTimeout(300);
  }
  return null;
}

/** On a selector mismatch, save a screenshot + HTML so we can pin the real one. */
async function dumpDebug(page: Page, tag: string): Promise<void> {
  try {
    if (!existsSync('captured')) mkdirSync('captured');
    const base = `captured/login-debug-${tag}`;
    await page.screenshot({ path: `${base}.png`, fullPage: true });
    writeFileSync(`${base}.html`, await page.content());
    console.log(`   🧭 Saved debug: ${base}.png and ${base}.html`);
  } catch { /* best-effort */ }
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
  await page.waitForLoadState('networkidle').catch(() => {});

  // Tekion's login is a multi-step flow: Username → Next → Password → TOTP.
  // We detect which screen we're on each pass and fill ONLY the matching field,
  // so the verification code can never land in the username box.
  const MAX_STEPS = 14;
  const MAX_OTP_TRIES = 3;
  let otpTries = 0;
  let lastCode = '';
  let userDone = false;
  let loggedIn = false;

  for (let step = 0; step < MAX_STEPS; step++) {
    // Left the login flow? Then we're in.
    if (/\/home/.test(page.url()) || (!/\/login/i.test(page.url()) && !/sign-?in/i.test(page.url()))) {
      loggedIn = true;
      break;
    }
    await page.waitForTimeout(800);

    const hasPassword = await page.locator('input[type="password"]').first().isVisible().catch(() => false);
    const hasOtp =
      (await page.locator(SEL.otpSubmit).first().isVisible().catch(() => false)) ||
      (await page.getByText(/TOTP Authenticator|verification code|one[- ]?time/i).first().isVisible().catch(() => false));
    const hasNext = await page.locator('button:has-text("Next")').first().isVisible().catch(() => false);

    // ── Verification-code screen ── (checked first so a code never hits username)
    if (hasOtp && !hasPassword) {
      if (otpTries >= MAX_OTP_TRIES) {
        console.log(`   ⚠️ used all ${MAX_OTP_TRIES} code attempts.`);
        break;
      }
      otpTries++;
      const code = await freshTotp(page, lastCode);
      lastCode = code;
      console.log(`🔐 OTP attempt ${otpTries}/${MAX_OTP_TRIES}: ${code}`);
      const field = await firstVisible(page, [
        'input[autocomplete="one-time-code"]', 'input[name="otp"]', 'input[name="code"]',
        'input[placeholder="Type Here"]', 'input[type="tel"]', 'input[type="text"]',
      ], 8000);
      if (!field) {
        await dumpDebug(page, 'no-otp-field');
        throw new Error('On the code screen but no input found. See captured/login-debug-no-otp-field.html');
      }
      await field.click({ clickCount: 3 }).catch(() => {});
      await field.fill('');
      await field.fill(code);
      await page.locator(SEL.otpSubmit).first().click().catch(() => {});
      await page.waitForTimeout(2500);
      continue;
    }

    // ── Password screen ──
    if (hasPassword) {
      console.log('   • entering password');
      await page.locator('input[type="password"]').first().fill(pass);
      await page.locator(
        'button:has-text("Next"), button:has-text("Sign In"), button:has-text("Login"), button[type="submit"]',
      ).first().click().catch(() => {});
      await page.waitForTimeout(1500);
      continue;
    }

    // ── Username screen ──
    // The field has no name/autocomplete (just type=text), and the *disabled*
    // username field on the password screen matches the same selector — so we
    // require an ENABLED input to avoid grabbing the wrong one mid-transition.
    const userField = await firstVisible(page, [
      'input[name="username"]:not([disabled])', 'input[autocomplete="username"]:not([disabled])',
      'input[type="email"]:not([disabled])', 'input[type="text"]:not([disabled])',
    ], 6000);
    if (userField) {
      console.log('   • entering username');
      await userField.fill('');
      await userField.fill(user);
      // Confirm the value committed before submitting — some React forms keep
      // "Next" inert until the controlled value updates.
      if ((await userField.inputValue().catch(() => '')) !== user) {
        await userField.pressSequentially(user, { delay: 30 }).catch(() => {});
      }
      await page.locator(
        hasNext ? 'button:has-text("Next")' : 'button[type="submit"], button:has-text("Sign In"), button:has-text("Login")',
      ).first().click().catch(() => {});
      userDone = true;
      // Confirm we advanced to the password screen; if not, the click may not
      // have registered — press Enter as a fallback before the loop re-tries.
      const advanced = await page.locator('input[type="password"]').first()
        .waitFor({ state: 'visible', timeout: 6000 }).then(() => true).catch(() => false);
      if (!advanced) await userField.press('Enter').catch(() => {});
      continue;
    }

    // Could not classify this screen — snapshot it for inspection.
    await dumpDebug(page, `step-${step}`);
    await page.waitForTimeout(700);
  }

  void userDone;
  if (!loggedIn) {
    console.log('⏳ Auto-login did not complete. Finish in the browser if needed…');
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
