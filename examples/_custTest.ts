import 'dotenv/config';
import { apcFromEnv } from '../src/apc.js';
import { buildIndividualCustomer, createCustomer } from '../src/openapi/customers.js';
const apc = apcFromEnv();
const payload = buildIndividualCustomer({
  firstName: 'Smoke', lastName: 'Test',
  email: 'smoketest@example.com', mobile: '9876543210', countryCode: 1,
  address: { line1: '123 Main Street', city: 'Birmingham', state: 'Alabama', postalCode: '12345', country: 'US' },
});
const r = await createCustomer(apc, payload);
const d:any = r.body?.data ?? {};
console.log('HTTP status :', r.status, r.ok ? '(ok)' : '(not ok)');
console.log('meta        :', JSON.stringify((r.body as any)?.meta));
console.log('customerId  :', d.customerId ?? d.id ?? d.globalCustomerId ?? '(see body)');
console.log('creationTime:', d.creationTime ? new Date(d.creationTime).toISOString() : '(none)');
