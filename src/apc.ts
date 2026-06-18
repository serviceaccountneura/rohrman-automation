/**
 * apc.ts — Official Tekion Automotive Partner Cloud (APC) API client.
 *
 * Every Dealer-Level API call uses the same four headers, per the APC docs:
 *
 *   POST https://api-sandbox.tekioncloud.com/openapi/v4.0.0/<resource>
 *   Content-Type:  application/json
 *   app_id:        <APC_APP_ID>
 *   Authorization: Bearer <token from the Tekion token API>
 *   dealer_id:     <APC_DEALER_ID>
 *
 * This is the supported, stable, ToS-clean path. Configure via env vars and use
 * it like:
 *
 *   const apc = apcFromEnv();
 *   const r = await apc.post('/sales/appointments/abc-123/assignees',
 *                            { userId: '...', primary: true });
 *
 * For a quick CLI smoke test:
 *   npm run apc -- GET  /sales/appointments
 *   npm run apc -- POST /sales/appointments/abc-123/assignees \
 *                  '{"userId":"...","primary":true}'
 */
import 'dotenv/config';
import { readFileSync, writeFileSync } from 'node:fs';

/** Where the 24h token is cached on disk so all CLI commands reuse it. */
export const TOKEN_CACHE_FILE = '.tekion-token.json';

export type ApcConfig = {
  /** Base URL: https://api-sandbox.tekioncloud.com/openapi/v4.0.0 (sandbox) */
  baseUrl: string;
  appId: string;
  dealerId: string;
  /** A static Bearer token, OR a function that fetches/refreshes one. */
  token: string | (() => Promise<string>);
};

export type ApcResponse<T = unknown> = {
  status: number;
  ok: boolean;
  body: T;
};

export class ApcClient {
  constructor(private cfg: ApcConfig) {}

  private async authHeaders(): Promise<Record<string, string>> {
    const tok = typeof this.cfg.token === 'function' ? await this.cfg.token() : this.cfg.token;
    return {
      'Content-Type': 'application/json',
      app_id: this.cfg.appId,
      Authorization: `Bearer ${tok}`,
      dealer_id: this.cfg.dealerId,
    };
  }

  async request<T = unknown>(
    method: string,
    path: string,
    body?: unknown,
    extraHeaders?: Record<string, string>,
  ): Promise<ApcResponse<T>> {
    const url = path.startsWith('http') ? path : `${this.cfg.baseUrl}${path}`;
    const res = await fetch(url, {
      method,
      headers: { ...(await this.authHeaders()), ...(extraHeaders || {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let parsed: unknown = text;
    try { parsed = JSON.parse(text); } catch { /* keep as text */ }
    return { status: res.status, ok: res.ok, body: parsed as T };
  }

  get   <T = unknown>(p: string) { return this.request<T>('GET', p); }
  post  <T = unknown>(p: string, body: unknown) { return this.request<T>('POST', p, body); }
  put   <T = unknown>(p: string, body: unknown) { return this.request<T>('PUT', p, body); }
  patch <T = unknown>(p: string, body: unknown) { return this.request<T>('PATCH', p, body); }
  delete<T = unknown>(p: string) { return this.request<T>('DELETE', p); }

  /**
   * Upload a file (e.g. an invoice image) as multipart/form-data.
   *
   * IMPORTANT: we deliberately do NOT set Content-Type — fetch sets the correct
   * `multipart/form-data; boundary=…` header automatically when given a FormData
   * body. Setting it manually breaks the upload.
   */
  async upload<T = unknown>(
    path: string,
    file: { filename: string; buffer: Buffer; contentType?: string },
    options?: { fileField?: string; fields?: Record<string, string> },
  ): Promise<ApcResponse<T>> {
    const tok = typeof this.cfg.token === 'function' ? await this.cfg.token() : this.cfg.token;
    const form = new FormData();
    // Buffer is a Uint8Array, but its backing type is ArrayBufferLike which TS
    // refuses to widen to BlobPart — cast through a plain Uint8Array view.
    const blob = new Blob([new Uint8Array(file.buffer)],
      { type: file.contentType || 'application/octet-stream' });
    form.append(options?.fileField || 'file', blob, file.filename);
    if (options?.fields) {
      for (const [k, v] of Object.entries(options.fields)) form.append(k, v);
    }
    const url = path.startsWith('http') ? path : `${this.cfg.baseUrl}${path}`;
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        app_id: this.cfg.appId,
        Authorization: `Bearer ${tok}`,
        dealer_id: this.cfg.dealerId,
      },
      body: form,
    });
    const text = await res.text();
    let parsed: unknown = text;
    try { parsed = JSON.parse(text); } catch { /* keep as text */ }
    return { status: res.status, ok: res.ok, body: parsed as T };
  }
}

/**
 * TokenManager — mints + caches a Bearer token from APC's public token endpoint.
 *
 * Per the APC docs + the real sandbox response (verified 2026-06-15):
 *   POST {APC_BASE_URL}/public/tokens
 *   Content-Type: application/x-www-form-urlencoded
 *   body: app_id=<app_id>&secret_key=<secret_key>
 *   response: {
 *     "status": "success",
 *     "data": {
 *       "token_type": "Bearer",
 *       "access_token": "eyJ...",
 *       "expire_in": 86399,            // seconds until expiry
 *       "expire_on": 1781613009,       // epoch SECONDS (not ms)
 *       "issued_at": 1781526609
 *     }
 *   }
 *
 * Tokens last ~24h; rate limit is 20 tokens per 15 minutes — so we cache
 * aggressively and only refresh on expiry.
 */
export class TokenManager {
  private cached: string | null = null;
  private expiresAtMs = 0;

  constructor(
    private baseUrl: string,
    private appId: string,
    private secretKey: string,
    /** Optional file to persist the token across separate CLI runs (24h reuse). */
    private cacheFile?: string,
  ) {}

  /** Read a still-valid token from the cache file, if present. */
  private loadFromDisk(): boolean {
    if (!this.cacheFile) return false;
    try {
      const raw = readFileSync(this.cacheFile, 'utf8');
      const { access_token, expiresAtMs } = JSON.parse(raw) as { access_token: string; expiresAtMs: number };
      if (access_token && Date.now() < expiresAtMs - 60_000) {
        this.cached = access_token;
        this.expiresAtMs = expiresAtMs;
        return true;
      }
    } catch { /* no file / unreadable — mint fresh */ }
    return false;
  }

  private saveToDisk(): void {
    if (!this.cacheFile || !this.cached) return;
    try {
      writeFileSync(this.cacheFile, JSON.stringify({ access_token: this.cached, expiresAtMs: this.expiresAtMs }));
    } catch { /* best-effort */ }
  }

  /** Milliseconds until the cached token expires (0 if none). */
  expiresInMs(): number {
    return Math.max(0, this.expiresAtMs - Date.now());
  }

  async getToken(): Promise<string> {
    // 1) live in-memory token  2) valid on-disk token  3) mint fresh
    if (this.cached && Date.now() < this.expiresAtMs - 60_000) return this.cached;
    if (this.loadFromDisk()) return this.cached!;
    return this.refresh();
  }

  async refresh(): Promise<string> {
    const url = `${this.baseUrl}/public/tokens`;
    const body = new URLSearchParams({ app_id: this.appId, secret_key: this.secretKey });
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
    });
    const text = await res.text();
    if (!res.ok) throw new Error(`Token request failed (${res.status}): ${text}`);
    let json: unknown;
    try { json = JSON.parse(text); } catch { throw new Error(`Token response not JSON: ${text}`); }
    const data = (json as { data?: Record<string, unknown> })?.data ?? {};
    // Real Tekion field is `access_token`; keep fallbacks for resilience.
    const tok = (data.access_token || data.accessToken || data.token || data.bearerToken) as string | undefined;
    if (!tok) throw new Error(`Token field missing in response: ${text}`);

    // Real Tekion fields: `expire_on` (epoch seconds) and `expire_in` (seconds).
    const expOn = (data.expire_on ?? data.expires_at ?? data.expiresAt ?? data.expiry) as number | string | undefined;
    const expIn = (data.expire_in ?? data.expires_in) as number | string | undefined;
    let expiryMs: number;
    if (expOn !== undefined) {
      const n = Number(expOn);
      // < 1e12 ⇒ epoch seconds, convert to ms.
      expiryMs = n < 1e12 ? n * 1000 : n;
    } else if (expIn !== undefined) {
      expiryMs = Date.now() + Number(expIn) * 1000;
    } else {
      expiryMs = Date.now() + 23 * 60 * 60 * 1000; // fallback: just under 24h
    }
    this.cached = tok;
    this.expiresAtMs = expiryMs;
    this.saveToDisk();
    return tok;
  }
}

/**
 * Build an ApcClient from environment variables (.env).
 *
 * Required: APC_BASE_URL, APC_APP_ID, APC_DEALER_ID
 * Plus one of:
 *   - APC_SECRET_KEY → token is minted + auto-refreshed (recommended)
 *   - APC_TOKEN      → static token pasted manually (debug only)
 */
export function apcFromEnv(): ApcClient {
  const required = ['APC_BASE_URL', 'APC_APP_ID', 'APC_DEALER_ID'] as const;
  for (const k of required) {
    if (!process.env[k]) throw new Error(`Missing env var: ${k} (see .env.example)`);
  }
  const baseUrl = process.env.APC_BASE_URL!.replace(/\/$/, '');
  const appId = process.env.APC_APP_ID!;
  const dealerId = process.env.APC_DEALER_ID!;

  let token: string | (() => Promise<string>);
  if (process.env.APC_SECRET_KEY) {
    const tm = new TokenManager(baseUrl, appId, process.env.APC_SECRET_KEY, TOKEN_CACHE_FILE);
    token = () => tm.getToken();
  } else if (process.env.APC_TOKEN) {
    token = process.env.APC_TOKEN;
  } else {
    throw new Error('Need APC_SECRET_KEY (preferred) or APC_TOKEN in .env');
  }

  return new ApcClient({ baseUrl, appId, dealerId, token });
}

/**
 * Build a TokenManager from env, backed by the shared on-disk cache.
 * Used by the `npm run token` command to mint + persist a 24h token that
 * every other CLI command then reuses without re-minting.
 */
export function tokenManagerFromEnv(): TokenManager {
  for (const k of ['APC_BASE_URL', 'APC_APP_ID', 'APC_SECRET_KEY'] as const) {
    if (!process.env[k]) throw new Error(`Missing env var: ${k} (see .env.example)`);
  }
  return new TokenManager(
    process.env.APC_BASE_URL!.replace(/\/$/, ''),
    process.env.APC_APP_ID!,
    process.env.APC_SECRET_KEY!,
    TOKEN_CACHE_FILE,
  );
}

// CLI smoke test:  tsx src/apc.ts <METHOD> <path> [bodyJson]
if (process.argv[1]?.endsWith('apc.ts')) {
  const [method, path, bodyJson] = process.argv.slice(2);
  if (!method || !path) {
    console.error('Usage: tsx src/apc.ts <METHOD> <path> [bodyJson]');
    process.exit(1);
  }
  const client = apcFromEnv();
  const body = bodyJson ? JSON.parse(bodyJson) : undefined;
  const res = await client.request(method.toUpperCase(), path, body);
  console.log(JSON.stringify(res, null, 2));
  process.exit(res.ok ? 0 : 1);
}
