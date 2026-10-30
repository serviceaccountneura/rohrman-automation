/**
 * createCustomer.ts — Create a customer in Tekion.  POST /v4.0.0/customers
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run customer:create
 *
 * ▼▼▼  EDIT THE CUSTOMER INFO HERE  ▼▼▼
 * Fill in the person's details below. Only firstName + lastName are required;
 * everything else is optional.
 *
 * NOTE: `dob` and `taxId` are sensitive PII gated behind a separate APC Data
 * Permission. If your app doesn't have that permission, including them returns
 * HTTP 400 ("check permission for the following fields: customerDetails.dob").
 * Leave them out unless your app is permissioned for them.
 */
import { apcFromEnv } from '../src/apc.js';
import { buildIndividualCustomer, createCustomer, type SimpleIndividual } from '../src/openapi/customers.js';
import { printResult, printSummary } from './_print.js';

const CUSTOMER: SimpleIndividual = {
  prefix: 'Mr',
  firstName: 'John',
  middleName: '',
  lastName: 'Doe',
  email: 'john.doe@example.com',
  mobile: '9876543210',
  countryCode: 1,                 // 1 = US
  // dob: '1990-05-20',           // ← needs DOB data permission
  address: {
    line1: '1100 E Golf Rd',
    city: 'Schaumburg',
    state: 'Illinois',
    county: 'Cook',
    postalCode: '60173',
    country: 'US',
  },
  // taxId: '123456789',          // ← needs Tax data permission
  marketingOptIn: true,
};
// ▲▲▲  END EDIT  ▲▲▲

async function main() {
  const apc = apcFromEnv();
  const res = await createCustomer(apc, buildIndividualCustomer(CUSTOMER));
  printResult('Create Customer', res);
  if (res.ok) {
    const d = res.body.data as { id?: string; displayId?: string; creationTime?: number };
    console.log('Key fields:');
    printSummary({
      'Customer ID (UUID)': d.id,
      'Display ID': d.displayId,
      'Created': d.creationTime ? new Date(d.creationTime).toISOString() : undefined,
    });
  }
  process.exit(res.ok ? 0 : 1);
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
