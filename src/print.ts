/**
 * print.ts — shared structured-output helpers used by the runnable blocks
 * at the bottom of each src/openapi/*.ts file.
 */
import type { ApcResponse } from './apc.js';

const line = '─'.repeat(64);

/** Print a clean, structured summary of an APC response. */
export function printResult(title: string, res: ApcResponse<unknown>): void {
  const ok = res.ok ? '✅ OK' : '❌ FAILED';
  console.log(`\n${line}`);
  console.log(title);
  console.log(line);
  console.log(`Status : ${res.status}  ${ok}`);

  const body = res.body as { meta?: { status?: string }; data?: unknown; message?: string };
  if (body?.meta?.status) console.log(`Meta   : ${body.meta.status}`);
  if (!res.ok && body?.message) console.log(`Message: ${body.message}`);

  const data = body?.data ?? body;
  console.log('\nData:');
  console.log(indent(JSON.stringify(data, null, 2)));
  console.log(line + '\n');
}

/** Print a flat key→value summary of the most useful fields. */
export function printSummary(rows: Record<string, unknown>): void {
  const width = Math.max(...Object.keys(rows).map((k) => k.length));
  for (const [k, v] of Object.entries(rows)) {
    console.log(`  ${k.padEnd(width)} : ${v ?? '—'}`);
  }
}

function indent(s: string): string {
  return s.split('\n').map((l) => '  ' + l).join('\n');
}
