/**
 * analyzeOemJe.ts — Endpoint map for OEM JE + PDF attach capture.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run pw:analyze:oem-je
 *       npm run pw:analyze:oem-je -- <file.jsonl>
 *
 * Highlights:
 *   • SAVE AS DRAFT candidates (journal write)
 *   • PDF ATTACH candidates (document / attachment / upload)
 * Writes captured/oem-je-endpoints.json.
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
const LOOKUP = /\/lookup\/|\/search$|\/filter$|bootstrap|metadata|permission|preference/i;
const JE_WRITE = /journal|accountingChain|\/je\b|voucher|manualEntry|draft/i;
const ATTACH = /document|attachment|assetAttachment|upload|media|file/i;

function newestLive(): string {
  const files = readdirSync('captured')
    .filter((f) => f.startsWith('oem-je-live-') && f.endsWith('.jsonl'))
    .sort();
  if (!files.length) {
    throw new Error('No captured/oem-je-live-*.jsonl. Run `npm run pw:capture:oem-je` first.');
  }
  return `captured/${files[files.length - 1]}`;
}

function safeJson(text?: string): unknown {
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function pathOf(url: string): string {
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
}

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
  return s
    .split('\n')
    .map((l) => pad + l)
    .join('\n');
}

function main() {
  const file = process.argv[2] || newestLive();
  const recs: Rec[] = readFileSync(file, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((l) => JSON.parse(l) as Rec);

  const writes = recs.filter((r) => WRITES.has(r.method));
  const authFails = recs.filter((r) => r.status === 401 || r.status === 403);

  console.log(`\nAnalyzed: ${file}`);
  console.log(`${recs.length} call(s) — ${writes.length} write\n`);

  if (authFails.length > recs.length / 2) {
    console.log('⚠  Most calls returned 401/403 — expired session. Re-login and re-capture.\n');
  }

  console.log('═══ WRITE CALLS (in order) ═══\n');
  for (const r of writes) {
    const tag = ATTACH.test(r.url) ? ' [ATTACH]' : JE_WRITE.test(r.url) ? ' [JE]' : '';
    console.log(`#${r.i}  [${r.method}] ${r.url}${tag}`);
    console.log(`    status: ${r.status}`);
    const body = safeJson(r.reqBody);
    if (body !== undefined) {
      console.log('    request body:');
      console.log(indent(JSON.stringify(body, null, 2).slice(0, 2500)));
    }
    console.log('');
  }

  const draftCandidates = writes.filter(
    (r) => r.status < 400 && !LOOKUP.test(pathOf(r.url)) && JE_WRITE.test(r.url),
  );
  const attachCandidates = writes.filter(
    (r) => r.status < 400 && !LOOKUP.test(pathOf(r.url)) && ATTACH.test(r.url),
  );

  console.log('═══ SAVE AS DRAFT CANDIDATES ═══\n');
  if (!draftCandidates.length) {
    console.log('  none — did you click Save as Draft with Balance $0.00?');
  } else {
    for (const r of draftCandidates) {
      console.log(`  #${r.i}  [${r.method}] ${pathOf(r.url)}  (${r.status})`);
    }
  }

  console.log('\n═══ PDF ATTACH CANDIDATES ═══\n');
  if (!attachCandidates.length) {
    console.log('  none — did you upload/attach the PDF on the JE screen before Save?');
  } else {
    for (const r of attachCandidates) {
      console.log(`  #${r.i}  [${r.method}] ${pathOf(r.url)}  (${r.status})`);
    }
    console.log('\n  Port upload + link into api/services/je_creation.py');
    console.log('  (today assetAttachmentDto is empty).');
  }

  const mapRec = (r: Rec) => ({
    order: r.i,
    method: r.method,
    path: pathOf(r.url),
    url: r.url,
    status: r.status,
    requestBody: safeJson(r.reqBody),
    requestShape: shape(safeJson(r.reqBody)),
    responseBody: safeJson(r.respBody),
  });

  writeFileSync(
    'captured/oem-je-endpoints.json',
    JSON.stringify(
      {
        source: file,
        capturedAt: recs[0]?.at,
        saveDraftCandidates: draftCandidates.map(mapRec),
        attachCandidates: attachCandidates.map(mapRec),
        writes: writes.map(mapRec),
      },
      null,
      2,
    ),
  );
  console.log('\n📝 Wrote captured/oem-je-endpoints.json');
}

main();
