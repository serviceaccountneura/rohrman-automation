/**
 * getDealAccessory.ts — Fetch one accessory within a deal.
 * GET /v4.0.0/deals/{deal-id}/deal-payment/accessories/{accessory-id}
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run deal:accessory -- <dealId> <accessoryId>
 *   e.g. npm run deal:accessory -- 1157502503886266296 ad66678b-6932-4e4a-ba6a-cae8cab34276
 *
 * Both IDs are path parameters supplied on the command line — nothing to edit
 * in the file. This is a read-only GET, so it's safe to run anytime.
 */
import { apcFromEnv } from '../src/apc.js';
import { getDealAccessory } from '../src/openapi/deals.js';
import { printResult, printSummary } from './_print.js';

async function main() {
  const [dealId, accessoryId] = process.argv.slice(2);
  if (!dealId || !accessoryId) {
    console.error('Usage: npm run deal:accessory -- <dealId> <accessoryId>');
    process.exit(1);
  }
  const apc = apcFromEnv();
  const res = await getDealAccessory(apc, dealId, accessoryId);
  printResult('Get Deal Accessory', res);
  if (res.ok) {
    const a = res.body.data;
    console.log('Key fields:');
    printSummary({
      'Accessory ID': a.id,
      'Code': a.code,
      'Name': a.name,
      'Cost': a.cost,
      'Price': a.price,
      'Pay type': a.payType,
      'Taxable': a.taxable,
      'Part number': a.partNumber,
    });
  }
  process.exit(res.ok ? 0 : 1);
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
