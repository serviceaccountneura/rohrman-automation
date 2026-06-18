/**
 * examples/vehicleBulkImport.ts
 * ────────────────────────────────────────────────────────────────────
 * Bulk-import vehicles into a dealer's inventory using only Open API
 * endpoints. Reads a JSON array from the file passed on the CLI:
 *
 *   tsx examples/vehicleBulkImport.ts ./inventory.json
 *
 * The script kicks off an async upsert task and polls until it finishes,
 * then prints how many rows were processed and the IDs of any failures.
 */
import { readFileSync } from 'node:fs';
import { apcFromEnv } from '../src/apc.js';
import * as vehicles from '../src/openapi/vehicleInventory.js';

async function main(): Promise<void> {
  const path = process.argv[2];
  if (!path) {
    console.error('Usage: tsx examples/vehicleBulkImport.ts <vehicles.json>');
    process.exit(1);
  }
  const rows = JSON.parse(readFileSync(path, 'utf8')) as vehicles.Vehicle[];
  if (!Array.isArray(rows)) throw new Error('Input must be a JSON array of vehicles.');
  console.log(`Loaded ${rows.length} vehicles from ${path}.`);

  const apc = apcFromEnv();
  console.log('Submitting bulk upsert…');
  const result = await vehicles.bulkUpsertAndWait(apc, rows, { intervalMs: 3000 });

  console.log('\n── Result ─────────────────────────────────────────');
  console.log(`Task:      ${result.taskId}`);
  console.log(`Status:    ${result.status}`);
  console.log(`Total:     ${result.totalRows ?? rows.length}`);
  console.log(`Processed: ${result.processed ?? '-'}`);
  console.log(`Failed:    ${result.failed ?? 0}`);
  if (result.errors?.length) {
    console.log('\nFailures:');
    for (const e of result.errors.slice(0, 10)) {
      console.log(`  row ${e.row}: ${e.reason}`);
    }
    if (result.errors.length > 10) console.log(`  …and ${result.errors.length - 10} more`);
  }
  process.exit(result.status === 'COMPLETED' ? 0 : 1);
}

main().catch((e) => {
  console.error('vehicleBulkImport failed:', e);
  process.exit(1);
});
