/**
 * analyzeJE.ts — Turn the Journal Entry capture into a clean endpoint map.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:analyze:vmi                 (newest captured/vmi-live-*.jsonl)
 *       npm run pw:analyze:vmi -- <file.jsonl>
 *
 * Reads the live JSONL (not the HAR — it is complete even on a hard kill) and
 * prints, in call order:
 *   • every WRITE call in full: method, url, status, request body, response body
 *   • every READ call collapsed to method + path + status (the lookups)
 *   • a SAVE AS DRAFT CANDIDATES section — the writes that look like the journal
 *     entry itself, which is the single call api/services/je_creation.py needs
 * Writes captured/vmi-endpoints.json.
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

/** Paths that are lookups/searches dressed up as POSTs — never the JE write. */
const LOOKUP = /\/lookup\/|\/search$|\/filter$|bootstrap|metadata|permission|preference/i;

/** Path shapes that plausibly persist a journal entry. */
const JE_WRITE = /journal|accountingChain|\/je\b|voucher|manualEntry|draft/i;

function newestLive(): string {
  const files = readdirSync('captured')
    .filter((f) => f.startsWith('vmi-live-') && f.endsWith('.jsonl'))
    .sort();
  if (!files.length) throw new Error('No captured/vmi-live-*.jsonl. Run `npm run pw:capture:vmi` first.');
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
  const authFails = recs.filter((r) => r.status === 401 || r.status === 403);

  console.log(`\nAnalyzed: ${file}`);
  console.log(`${recs.length} call(s) — ${writes.length} write, ${reads.length} read\n`);

  // The previous je-live capture was a dead session: every call 401/500 and no
  // payloads. Fail loudly rather than let someone port an empty map.
  if (authFails.length > recs.length / 2) {
    console.log('⚠  Most calls returned 401/403 — this capture is from an expired session');
    console.log('   and contains no usable payloads. Delete tekion-auth.json, run');
    console.log('   `npm run pw:login`, then `npm run pw:capture:vmi` again.\n');
  }

  console.log('═══ WRITE CALLS (in order) ═══\n');
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

  console.log('═══ READ CALLS (how the form is populated) ═══\n');
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

  // ── The bit that matters: which write was "Save as Draft"? ────────────────
  const candidates = writes.filter(
    (r) => r.status < 400 && !LOOKUP.test(pathOf(r.url)) && JE_WRITE.test(r.url),
  );

  console.log('\n═══ SAVE AS DRAFT CANDIDATES ═══\n');
  if (!candidates.length) {
    console.log('  none found — did the run actually click "Save as Draft"?');
    console.log('  Re-capture and make sure the entry balances to $0.00 first,');
    console.log('  otherwise Tekion blocks the save client-side and never calls the API.');
  } else {
    for (const r of candidates) {
      console.log(`  #${r.i}  [${r.method}] ${pathOf(r.url)}  (${r.status})`);
    }
    console.log('\n  Port the last one into api/services/je_creation.py:');
    console.log('    _SAVE_DRAFT_METHOD   = the method above');
    console.log('    _SAVE_DRAFT_ENDPOINT = the path above');
    console.log('    then align _build_je_payload() with its request body.');
  }

  const out = {
    source: file,
    capturedAt: recs[0]?.at,
    saveDraftCandidates: candidates.map((r) => ({
      order: r.i,
      method: r.method,
      path: pathOf(r.url),
      url: r.url,
      status: r.status,
      requestBody: safeJson(r.reqBody),
      requestShape: shape(safeJson(r.reqBody)),
      responseBody: safeJson(r.respBody),
    })),
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
  writeFileSync('captured/vmi-endpoints.json', JSON.stringify(out, null, 2));
  console.log('\n📝 Wrote captured/vmi-endpoints.json');
}

main();
