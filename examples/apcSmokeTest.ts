/**
 * apcSmokeTest.ts — verify the APC client end-to-end against the sandbox.
 *   1. Mint a Bearer token from app_id + secret_key (TokenManager)
 *   2. Confirm expiry parses to ~24h out
 *   3. Make a real Open-API data call and print what comes back
 *
 * Run: npx tsx examples/apcSmokeTest.ts
 */
import 'dotenv/config';
import { TokenManager, apcFromEnv } from '../src/apc.js';
import { listVehicles } from '../src/openapi/vehicleInventory.js';
import { buildIndividualCustomer, createCustomer } from '../src/openapi/customers.js';

async function main() {
  const baseUrl = process.env.APC_BASE_URL!.replace(/\/$/, '');
  const appId = process.env.APC_APP_ID!;
  const secret = process.env.APC_SECRET_KEY!;

  // 1 + 2: token mint and expiry check.
  const tm = new TokenManager(baseUrl, appId, secret);
  const token = await tm.getToken();
  const expiresAt = (tm as unknown as { expiresAtMs: number }).expiresAtMs;
  console.log('✅ Token minted.');
  console.log('   token (first 32):', token.slice(0, 32) + '…');
  console.log('   expires at      :', new Date(expiresAt).toISOString());
  console.log('   hours from now  :', ((expiresAt - Date.now()) / 3_600_000).toFixed(2));

  const apc = apcFromEnv();

  // 3a: read call — vehicle inventory (Open API).
  console.log('\n📡 GET vehicle-inventories (Open API)…');
  const veh = await listVehicles(apc, { make: 'Chevrolet', year: 2022 });
  console.log('   HTTP status:', veh.status, veh.ok ? '(ok)' : '(not ok)');
  console.log('   ' + JSON.stringify(veh.body).slice(0, 400));

  // 3b: write call — create a test customer (Open API, real v4.0.0 schema).
  console.log('\n📡 POST customers — Create Customer (Open API)…');
  // NOTE: dob and taxId are sensitive PII gated behind a separate APC data
  // permission. Including them without that permission returns HTTP 400
  // ("check permission for the following fields: customerDetails.dob").
  // Omit them for the basic smoke test.
  const payload = buildIndividualCustomer({
    firstName: 'Smoke',
    lastName: 'Test',
    email: 'smoketest@example.com',
    mobile: '9876543210',
    countryCode: 1,
    address: { line1: '123 Main Street', city: 'Birmingham', state: 'Alabama', postalCode: '12345', country: 'US' },
  });
  const cust = await createCustomer(apc, payload);
  console.log('   HTTP status:', cust.status, cust.ok ? '(ok)' : '(not ok)');
  console.log('   ' + JSON.stringify(cust.body).slice(0, 600));
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
