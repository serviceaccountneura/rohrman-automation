/**
 * client.ts — Replay a captured Tekion API call with YOUR data.
 *
 * Uses the saved session (auth.json). It opens the authenticated origin in a
 * headless browser and runs `fetch` *inside* that page, so:
 *   - cookies (incl. httpOnly) are sent automatically, and
 *   - if Tekion stores a bearer token in localStorage, we attach it as
 *     Authorization: Bearer <token>.
 *
 * This mirrors exactly what the SPA does, which is the most resilient way to
 * replay an internal API you don't officially own.
 */
import 'dotenv/config';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { existsSync } from 'node:fs';
import { proxyFromEnv } from './proxy.js';

const BASE_URL = process.env.TEKION_BASE_URL || 'https://app.tekioncloud.com';

export type CallSpec = {
  method: string;
  url: string;
  body?: unknown;
  /** extra headers to send (e.g. content-type, x-dealer-id) */
  headers?: Record<string, string>;
  /** localStorage key holding the JWT, if auth is bearer-based */
  tokenLocalStorageKey?: string;
};

export type CallResult = { status: number; ok: boolean; body: unknown };

export class TekionClient {
  private browser!: Browser;
  private context!: BrowserContext;
  private page!: Page;

  async open() {
    if (!existsSync('auth.json')) throw new Error('auth.json missing — run `npm run auth`.');
    this.browser = await chromium.launch({
      headless: true,
      proxy: proxyFromEnv(),
    });
    this.context = await this.browser.newContext({ storageState: 'auth.json' });
    this.page = await this.context.newPage();
    // Land on the origin so cookies + localStorage are active for in-page fetch.
    await this.page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
  }

  async call(spec: CallSpec): Promise<CallResult> {
    return this.page.evaluate(async (s) => {
      const headers: Record<string, string> = {
        'content-type': 'application/json',
        ...(s.headers || {}),
      };
      if (s.tokenLocalStorageKey) {
        const raw = localStorage.getItem(s.tokenLocalStorageKey) || '';
        const m = raw.match(/eyJ[\w-]+\.[\w-]+\.[\w-]+/); // find JWT inside value/JSON
        if (m) headers['authorization'] = `Bearer ${m[0]}`;
      }
      const res = await fetch(s.url, {
        method: s.method,
        credentials: 'include',
        headers,
        body: s.body !== undefined ? JSON.stringify(s.body) : undefined,
      });
      let parsed: unknown;
      const text = await res.text();
      try { parsed = JSON.parse(text); } catch { parsed = text; }
      return { status: res.status, ok: res.ok, body: parsed };
    }, spec);
  }

  async close() {
    await this.context?.close();
    await this.browser?.close();
  }
}

// Allow a quick CLI smoke test:  tsx src/client.ts '<json CallSpec>'
if (process.argv[1]?.endsWith('client.ts') && process.argv[2]) {
  const spec = JSON.parse(process.argv[2]) as CallSpec;
  const c = new TekionClient();
  await c.open();
  const r = await c.call(spec);
  console.log(JSON.stringify(r, null, 2));
  await c.close();
}
