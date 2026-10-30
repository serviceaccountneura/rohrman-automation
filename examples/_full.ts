import 'dotenv/config';
import { apcFromEnv } from '../src/apc.js';
import { buildIndividualCustomer, createCustomer } from '../src/openapi/customers.js';
const apc = apcFromEnv();
const r = await createCustomer(apc, buildIndividualCustomer({
  firstName: 'Smoke', lastName: 'Test', email: 'smoketest@example.com',
  mobile: '9876543210', countryCode: 1,
  address: { line1: '123 Main Street', city: 'Birmingham', state: 'Alabama', postalCode: '12345', country: 'US' },
}));
console.log('HTTP', r.status);
console.log(JSON.stringify(r.body, null, 2));
