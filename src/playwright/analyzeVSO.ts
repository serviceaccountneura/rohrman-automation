/**
 * analyzeVSO.ts — Turn the Vendor Stock Order capture into an endpoint map.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:analyze:vso                 (newest captured/vso-live-*.jsonl)
 *       npm run pw:analyze:vso -- <file.jsonl>
 *
 * Reads the live JSONL (not the HAR — it is complete even on a hard kill) and
 * prints, in call order:
 *   • every WRITE call in full: method, url, status, request body, response body
 *   • every READ call collapsed to method + path + status (the lookups)
 *   • the three groups api/services/vso_po_creation.py actually needs:
 *       PO LOOKUP        — how the PO is found by number
 *       DOCUMENT UPLOAD  — how the invoice file is attached
 *       PRE-INVOICE      — the write that creates the pre-invoice
 * Writes captured/vso-endpoints.json.
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

/** Searches dressed up as POSTs — never the pre-invoice write itself. */
const LOOKUP = /\/lookup\/|\/search$|\/filter$|bootstrap|metadata|permission|preference/i;

const GROUPS: Array<{ name: string; test: RegExp; note: string }> = [
  {
    name: 'PO LOOKUP',
    test: /purchase\/search|partTrade.*search|purchase-order/i,
    note: 'how the PO is found from the number on the invoice',
  },
  {
    name: 'DOCUMENT UPLOAD',
    test: /media-v3|initiate-upload|upload/i,
    note: 'how the invoice PDF is attached',
  },
  {
    name: 'PRE-INVOICE',
    test: /preInvoice|preinvoice|poInvoice/i,
    note: 'the write that creates the pre-invoice — the one we need most',
  },
];

function newestLive(): string {
  const files = readdirSync('captured')
    .filter((f) => f.startsWith('vso-live-') && f.endsWith('.jsonl'))
    .sort();
  if (!files.length) {
    throw new Error('No captured/vso-live-*.jsonl. Run `npm run pw:capture:vso` first.');
  }
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

  if (authFails.length > recs.length / 2) {
    console.log('⚠  Most calls returned 401/403 — this capture is from an expired session');
    console.log('   and contains no usable payloads. Delete tekion-auth.json, run');
    console.log('   `npm run pw:login`, then `npm run pw:capture:vso` again.\n');
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

  console.log('═══ READ CALLS (how the page is populated) ═══\n');
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

  // ── The three things the service needs ────────────────────────────────────
  const grouped: Record<string, Rec[]> = {};
  for (const group of GROUPS) {
    grouped[group.name] = recs.filter(
      (r) => r.status < 400 && group.test.test(r.url) && !(WRITES.has(r.method) && LOOKUP.test(pathOf(r.url)) && group.name === 'PRE-INVOICE'),
    );
  }

  console.log('\n═══ WHAT THE SERVICE NEEDS ═══');
  for (const group of GROUPS) {
    const hits = grouped[group.name];
    console.log(`\n  ${group.name}  — ${group.note}`);
    if (!hits.length) {
      console.log('    none found');
      continue;
    }
    for (const r of hits) {
      console.log(`    #${r.i}  [${r.method}] ${pathOf(r.url)}  (${r.status})`);
    }
  }

  const preInvoice = grouped['PRE-INVOICE'].filter((r) => WRITES.has(r.method));
  console.log('\n  ─────');
  if (!preInvoice.length) {
    console.log('  No pre-invoice write found — did the run click Submit?');
    console.log('  Re-capture and make sure the form is complete first: Tekion blocks');
    console.log('  an incomplete submit client-side and never calls the API.');
  } else {
    const last = preInvoice[preInvoice.length - 1];
    console.log('  Port the last PRE-INVOICE write into api/services/vso_po_creation.py:');
    console.log(`    _PRE_INVOICE_METHOD   = "${last.method}"`);
    console.log(`    _PRE_INVOICE_PATH     = "${pathOf(last.url)}"`);
    console.log('    then align _build_pre_invoice_payload() with its request body.');
  }

  const out = {
    source: file,
    capturedAt: recs[0]?.at,
    groups: Object.fromEntries(
      GROUPS.map((g) => [
        g.name,
        grouped[g.name].map((r) => ({
          order: r.i,
          method: r.method,
          path: pathOf(r.url),
          url: r.url,
          status: r.status,
          requestBody: safeJson(r.reqBody),
          requestShape: shape(safeJson(r.reqBody)),
          responseBody: safeJson(r.respBody),
        })),
      ]),
    ),
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
  writeFileSync('captured/vso-endpoints.json', JSON.stringify(out, null, 2));
  console.log('\n📝 Wrote captured/vso-endpoints.json');
}

main();
