/**
 * updateCustomer.ts — Update an existing customer.  PUT /v4.0.0/customers/{customerId}
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run customer:update -- <customerId>
 *   e.g. npm run customer:update -- 1d7cb521-4ef7-444d-94b7-b77843f00f7c
 *
 * ▼▼▼  EDIT THE FIELDS YOU WANT TO CHANGE  ▼▼▼
 * The customerId comes from the command line (the `id` returned by create).
 * Same body shape as create — fill what you want to update.
 */
import { apcFromEnv } from '../src/apc.js';
import { buildIndividualCustomer, updateCustomer, type SimpleIndividual } from '../src/openapi/customers.js';
import { printResult, printSummary } from './_print.js';

const UPDATED: SimpleIndividual = {
  prefix: 'Mr',
  firstName: 'John',
  lastName: 'Doe',
  email: 'john.doe.updated@example.com',   // ← changed
  mobile: '9998887777',                    // ← changed
  countryCode: 1,
  address: {
    line1: '500 Park Blvd',
    city: 'Itasca',
    state: 'Illinois',
    postalCode: '60143',
    country: 'US',
  },
  marketingOptIn: false,
};
// ▲▲▲  END EDIT  ▲▲▲

async function main() {
  const customerId = process.argv[2];
  if (!customerId) {
    console.error('Usage: npm run customer:update -- <customerId>');
    process.exit(1);
  }
  const apc = apcFromEnv();
  const res = await updateCustomer(apc, customerId, buildIndividualCustomer(UPDATED));
  printResult(`Update Customer ${customerId}`, res);
  if (res.ok) {
    const d = res.body.data as { id?: string; displayId?: string; lastUpdateTime?: number };
    console.log('Key fields:');
    printSummary({
      'Customer ID': d.id,
      'Display ID': d.displayId,
      'Last updated': d.lastUpdateTime ? new Date(d.lastUpdateTime).toISOString() : undefined,
    });
  }
  process.exit(res.ok ? 0 : 1);
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
