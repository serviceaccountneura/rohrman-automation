/**
 * examples/customerSync.ts
 * ────────────────────────────────────────────────────────────────────
 * One-shot customer sync from an external system (CSV / JSON / another CRM)
 * into Tekion via the real v4.0.0 Customer API.
 *
 * Input file is a JSON array of flat "SimpleIndividual" objects; the
 * buildIndividualCustomer() helper wraps each into Tekion's nested
 * customerDetails tree. If a row already has `customerId`, it's updated.
 *
 *   [ { "firstName": "Jane", "lastName": "Doe", "email": "jane@x.com",
 *       "mobile": "9876543210", "address": { "line1": "...", "postalCode": "12345" } } ]
 *
 * Run:  tsx examples/customerSync.ts ./customers.json
 */
import { readFileSync } from 'node:fs';
import { apcFromEnv } from '../src/apc.js';
import {
  buildIndividualCustomer,
  createCustomer,
  updateCustomer,
  type SimpleIndividual,
} from '../src/openapi/customers.js';

interface InputRow extends SimpleIndividual {
  customerId?: string; // present ⇒ update instead of create
}

async function main(): Promise<void> {
  const path = process.argv[2];
  if (!path) {
    console.error('Usage: tsx examples/customerSync.ts <customers.json>');
    process.exit(1);
  }
  const rows = JSON.parse(readFileSync(path, 'utf8')) as InputRow[];
  if (!Array.isArray(rows)) throw new Error('Input must be a JSON array.');

  const apc = apcFromEnv();
  let created = 0, updated = 0, failed = 0;

  for (const row of rows) {
    try {
      const payload = buildIndividualCustomer(row);
      if (row.customerId) {
        const r = await updateCustomer(apc, row.customerId, payload);
        if (!r.ok) throw new Error(`updateCustomer ${r.status}: ${JSON.stringify(r.body).slice(0, 200)}`);
        updated++;
      } else {
        const r = await createCustomer(apc, payload);
        if (!r.ok) throw new Error(`createCustomer ${r.status}: ${JSON.stringify(r.body).slice(0, 200)}`);
        created++;
      }
    } catch (e) {
      failed++;
      console.error(`Row failed (${row.email ?? row.firstName ?? '?'}):`, e);
    }
  }

  console.log('\n── Sync done ──────────────────────────────────────');
  console.log(`Created: ${created}`);
  console.log(`Updated: ${updated}`);
  console.log(`Failed:  ${failed}`);
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error('customerSync failed:', e);
  process.exit(1);
});
