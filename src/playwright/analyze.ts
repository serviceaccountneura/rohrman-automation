/**
 * analyze.ts — Turn a recorded HAR into a clean list of write endpoints.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:analyze            (uses the newest captured/*.har)
 *       npm run pw:analyze -- path.har
 *
 * For every POST/PUT/PATCH/DELETE call to a Tekion host it prints the method,
 * URL, response status, and the SHAPE of the request body (field names with
 * value types, so no real PII is dumped). It also writes
 * captured/endpoints.json — the map used to build the production script.
 */
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';

interface HarEntry {
  request: { method: string; url: string; postData?: { text?: string } };
  response: { status: number };
}

function newestHar(): string {
  const files = readdirSync('captured').filter((f) => f.endsWith('.har')).sort();
  if (!files.length) throw new Error('No HAR in captured/. Run `npm run pw:capture` first.');
  return `captured/${files[files.length - 1]}`;
}

function safeJson(text?: string): unknown {
  if (!text) return undefined;
  try { return JSON.parse(text); } catch { return text; }
}

/** Replace leaf values with their type so the shape is visible without PII. */
function shape(v: unknown, depth = 0): unknown {
  if (depth > 5) return '…';
  if (Array.isArray(v)) return v.length ? [shape(v[0], depth + 1)] : [];
  if (v && typeof v === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, val] of Object.entries(v)) out[k] = shape(val, depth + 1);
    return out;
  }
  return typeof v;
}

function main() {
  const harPath = process.argv[2] || newestHar();
  const har = JSON.parse(readFileSync(harPath, 'utf8'));
  const entries: HarEntry[] = har.log.entries;
  const writes = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

  const calls = entries.filter((e) => {
    const u = e.request.url;
    return /tekion(cloud)?\.com/.test(u)
      && writes.has(e.request.method)
      && !/analytics|telemetry|datadog|segment|sentry|\/log/i.test(u);
  });

  console.log(`\nAnalyzed: ${harPath}`);
  console.log(`Found ${calls.length} write call(s):\n`);

  const template = calls.map((e, i) => {
    const body = safeJson(e.request.postData?.text);
    console.log(`#${i + 1}  [${e.request.method}] ${e.request.url}`);
    console.log(`    status: ${e.response.status}`);
    if (body !== undefined) {
      console.log('    body shape:');
      console.log(JSON.stringify(shape(body), null, 2).split('\n').map((l) => '      ' + l).join('\n'));
    }
    console.log('');
    return {
      name: `call_${i + 1}`,
      method: e.request.method,
      url: e.request.url,
      exampleBody: body,
    };
  });

  writeFileSync('captured/endpoints.json', JSON.stringify(template, null, 2));
  console.log('📝 Wrote captured/endpoints.json');
}

main();
