/**
 * proxy.ts — Build a Playwright proxy config from the US_PROXY env var.
 *
 * Playwright wants credentials as separate fields, NOT inline in the URL:
 *     { server: 'http://host:port', username: 'u', password: 'p' }
 * so we parse "http://user:pass@host:port" into that shape here. Used by every
 * script that opens a browser, so the US egress is configured in exactly one place.
 *
 * Returns undefined when US_PROXY is empty (i.e. you're relying on a system VPN).
 */
export function proxyFromEnv() {
  const raw = process.env.US_PROXY?.trim();
  if (!raw) return undefined;
  try {
    const u = new URL(raw);
    const server = `${u.protocol}//${u.host}`; // host already includes the port
    const username = u.username ? decodeURIComponent(u.username) : undefined;
    const password = u.password ? decodeURIComponent(u.password) : undefined;
    return username ? { server, username, password } : { server };
  } catch {
    // Not a full URL (e.g. "host:port") — pass through as-is.
    return { server: raw };
  }
}
