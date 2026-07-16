/**
 * session.ts — Is auth.json still a logged-in Tekion session?
 *
 * Strategy: load the saved storage state and visit the base URL. If Tekion
 * keeps us inside the app, the session is valid; if it redirects to /login or
 * /sign-in, it has expired and MFA is required again.
 *
 * Used by:
 *   - `npm run auth`   → skip the MFA prompt entirely when still valid
 *   - `npm run check`  → quick CLI to confirm without opening anything visible
 */
import 'dotenv/config';
import { chromium } from 'playwright';
import { existsSync } from 'node:fs';
import { proxyFromEnv } from './proxy.js';

const BASE_URL = process.env.TEKION_BASE_URL || 'https://app.tekioncloud.com';

export async function isSessionValid(): Promise<boolean> {
  if (!existsSync('auth.json')) return false;
  const browser = await chromium.launch({ headless: true, proxy: proxyFromEnv() });
  try {
    const ctx = await browser.newContext({ storageState: 'auth.json' });
    const page = await ctx.newPage();
    await page.goto(BASE_URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
    // After the SPA boots it may client-side route to a login screen — give it a moment.
    await page.waitForTimeout(1500);
    const url = page.url().toLowerCase();
    await ctx.close();
    // If we landed anywhere that looks like an auth screen, the session died.
    return !/login|sign-?in|\/auth(\/|$)|mfa|otp/.test(url);
  } catch {
    return false;
  } finally {
    await browser.close();
  }
}

// CLI mode:  tsx src/session.ts
if (process.argv[1]?.endsWith('session.ts')) {
  const ok = await isSessionValid();
  if (ok) {
    console.log('✅ Session is valid — no MFA needed. Run capture/server normally.');
    process.exit(0);
  }
  console.log('⚠️  No valid session — run `npm run auth` to log in (one-time MFA).');
  process.exit(1);
}
