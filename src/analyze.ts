/**
 * analyze.ts — Turn a recorded HAR into a clean list of Tekion API endpoints.
 *
 * Run:  npm run analyze            (uses the newest captured/*.har)
 *       npm run analyze path.har   (a specific HAR)
 *
 * Prints, for every write-ish call (POST/PUT/PATCH) to a Tekion host:
 *   METHOD  url
 *   a trimmed view of the request JSON body (the fields you'll fill from your form)
 *
 * It also writes captured/endpoints.json — a machine-readable template you can
 * feed into client.ts / server.ts to replay the calls with your own data.
 */
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';

type HarEntry = {
  request: {
    method: string;
    url: string;
    postData?: { text?: string; mimeType?: string };
    headers: { name: string; value: string }[];
  };
  response: { status: number };
};

function newestHar(): string {
  const files = readdirSync('captured').filter((f) => f.endsWith('.har'));
  if (!files.length) throw new Error('No HAR files in captured/. Run `npm run capture` first.');
  files.sort();
  return `captured/${files[files.length - 1]}`;
}

function safeJson(text?: string): unknown {
  if (!text) return undefined;
  try { return JSON.parse(text); } catch { return text; }
}

/** Replace leaf values with their type, so we see the SHAPE without dumping PII. */
function shape(value: unknown, depth = 0): unknown {
  if (depth > 4) return '…';
  if (Array.isArray(value)) return value.length ? [shape(value[0], depth + 1)] : [];
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) out[k] = shape(v, depth + 1);
    return out;
  }
  return typeof value; // 'string' | 'number' | 'boolean' | …
}

function main() {
  const harPath = process.argv[2] || newestHar();
  const har = JSON.parse(readFileSync(harPath, 'utf8'));
  const entries: HarEntry[] = har.log.entries;

  const writeMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
  const apiCalls = entries.filter((e) => {
    const u = e.request.url;
    if (!/tekion(cloud)?\.com/.test(u)) return false;
    if (!writeMethods.has(e.request.method)) return false;
    // skip telemetry/analytics noise
    if (/analytics|telemetry|datadog|segment|sentry|log/i.test(u)) return false;
    return true;
  });

  console.log(`\nAnalyzed: ${harPath}`);
  console.log(`Found ${apiCalls.length} write-type API call(s) to Tekion:\n`);

  const template = apiCalls.map((e, i) => {
    const url = e.request.url;
    const method = e.request.method;
    const body = safeJson(e.request.postData?.text);
    console.log(`#${i + 1}  [${method}] ${url}`);
    console.log(`    status: ${e.response.status}`);
    if (body !== undefined) {
      console.log('    body shape:');
      console.log(JSON.stringify(shape(body), null, 2).split('\n').map((l) => '      ' + l).join('\n'));
    }
    console.log('');
    return {
      name: `call_${i + 1}`,
      method,
      url,
      // keep one real example body so you can see actual field names while building
      exampleBody: body,
      // which request headers (besides cookies) you may need to forward
      relevantHeaders: e.request.headers
        .filter((h) => /authorization|x-|content-type|dealer|tenant|tek/i.test(h.name))
        .map((h) => h.name),
    };
  });

  writeFileSync('captured/endpoints.json', JSON.stringify(template, null, 2));
  console.log('📝 Wrote captured/endpoints.json (URLs, example bodies, header names).');
  console.log('   Use it to wire src/client.ts + the form in src/server.ts.\n');
}

main();
