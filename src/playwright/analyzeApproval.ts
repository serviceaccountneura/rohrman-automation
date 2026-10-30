/**
 * analyzeApproval.ts — Turn the AP-approval capture into a clean endpoint map.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:analyze:ap                 (newest captured/ap-live-*.jsonl)
 *       npm run pw:analyze:ap -- <file.jsonl>
 *
 * Reads the live JSONL (not the HAR — it is complete even on a hard kill) and
 * prints, in call order:
 *   • every WRITE call in full: method, url, status, request body, response body
 *   • every READ call collapsed to method + path + status (the queue fetches)
 * Writes captured/ap-endpoints.json — the map used to port the flow into
 * api/services/tekion_client.py.
 */
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';

interface Rec {
  i: number;
  at: string;
  method: string;
  url: string;
  status: number;
  reqBody?: string;
  respBody?: string;
}

const WRITES = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

function newestLive(): string {
  const files = readdirSync('captured')
    .filter((f) => f.startsWith('ap-live-') && f.endsWith('.jsonl'))
    .sort();
  if (!files.length) throw new Error('No captured/ap-live-*.jsonl. Run `npm run pw:capture:ap` first.');
  return `captured/${files[files.length - 1]}`;
}

function safeJson(text?: string): unknown {
  if (!text) return undefined;
  try { return JSON.parse(text); } catch { return text; }
}

function pathOf(url: string): string {
  try { return new URL(url).pathname; } catch { return url; }
}

/** Replace leaf values with their type so shape is visible without dumping PII. */
function shape(v: unknown, depth = 0): unknown {
  if (depth > 6) return '…';
  if (Array.isArray(v)) return v.length ? [shape(v[0], depth + 1)] : [];
  if (v && typeof v === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, val] of Object.entries(v)) out[k] = shape(val, depth + 1);
    return out;
  }
  return typeof v;
}

function indent(s: string, pad = '      '): string {
  return s.split('\n').map((l) => pad + l).join('\n');
}

function main() {
  const file = process.argv[2] || newestLive();
  const recs: Rec[] = readFileSync(file, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((l) => JSON.parse(l) as Rec);

  const writes = recs.filter((r) => WRITES.has(r.method));
  const reads = recs.filter((r) => !WRITES.has(r.method));

  console.log(`\nAnalyzed: ${file}`);
  console.log(`${recs.length} call(s) — ${writes.length} write, ${reads.length} read\n`);

  console.log('═══ WRITE CALLS (the approval actions) ═══\n');
  for (const r of writes) {
    console.log(`#${r.i}  [${r.method}] ${r.url}`);
    console.log(`    status: ${r.status}`);
    const body = safeJson(r.reqBody);
    if (body !== undefined) {
      console.log('    request body:');
      console.log(indent(JSON.stringify(body, null, 2)));
    }
    const resp = safeJson(r.respBody);
    if (resp !== undefined) {
      console.log('    response body:');
      console.log(indent(JSON.stringify(resp, null, 2).slice(0, 2500)));
    }
    console.log('');
  }

  console.log('═══ READ CALLS (how the queue/detail is fetched) ═══\n');
  const seen = new Map<string, { count: number; status: number; example: Rec }>();
  for (const r of reads) {
    const key = `${r.method} ${pathOf(r.url)}`;
    const hit = seen.get(key);
    if (hit) hit.count++;
    else seen.set(key, { count: 1, status: r.status, example: r });
  }
  for (const [key, v] of seen) {
    console.log(`  ${key}  (${v.count}×, ${v.status})`);
  }

  const out = {
    source: file,
    capturedAt: recs[0]?.at,
    writes: writes.map((r) => ({
      order: r.i,
      method: r.method,
      url: r.url,
      path: pathOf(r.url),
      status: r.status,
      requestBody: safeJson(r.reqBody),
      requestShape: shape(safeJson(r.reqBody)),
      responseBody: safeJson(r.respBody),
    })),
    reads: [...seen.entries()].map(([key, v]) => ({
      key,
      count: v.count,
      status: v.status,
      url: v.example.url,
      requestBody: safeJson(v.example.reqBody),
      responseShape: shape(safeJson(v.example.respBody)),
    })),
  };
  writeFileSync('captured/ap-endpoints.json', JSON.stringify(out, null, 2));
  console.log('\n📝 Wrote captured/ap-endpoints.json');
}

main();
