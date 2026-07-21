/**
 * listVehicles.ts — Get vehicles from inventory.  GET /v4.0.0/vehicle-inventory
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run vehicles:list
 *
 * ▼▼▼  EDIT THE SEARCH FILTERS HERE  ▼▼▼
 * All filters are optional — leave the object empty `{}` to list everything.
 * Available filters: year, make, model, stockId, vin, status (STOCKED_IN / SOLD),
 * createdStartTime / createdEndTime / modifiedStartTime / modifiedEndTime (epoch ms),
 * nextPageToken (for the next page).
 *
 * NOTE: this endpoint must be added to your APC app's Product Suite (Vehicle
 * Inventory bundle) and a new app version published — otherwise it returns 403
 * "The latest app version does not include the API."
 */
import { apcFromEnv } from '../src/apc.js';
import { listVehicles, type VehicleSearchQuery } from '../src/openapi/vehicleInventory.js';
import { printResult, printSummary } from './_print.js';

const FILTERS: VehicleSearchQuery = {
  make: 'Honda',
  model: 'Civic',
  year: 2022,
  status: 'STOCKED_IN',
};
// ▲▲▲  END EDIT  ▲▲▲

async function main() {
  const apc = apcFromEnv();
  const res = await listVehicles(apc, FILTERS);
  printResult('Get Vehicles from Inventory', res);
  if (res.ok) {
    const vehicles = (res.body.data ?? []) as Array<{ vin?: string; year?: string; make?: string; model?: string; stockId?: string }>;
    console.log(`Found ${vehicles.length} vehicle(s):`);
    vehicles.slice(0, 10).forEach((v, i) =>
      console.log(`  ${i + 1}. ${v.year ?? ''} ${v.make ?? ''} ${v.model ?? ''} — VIN ${v.vin ?? '?'} (stock ${v.stockId ?? '?'})`));
    if (res.body.meta?.nextPageToken) printSummary({ nextPageToken: res.body.meta.nextPageToken });
  }
  process.exit(res.ok ? 0 : 1);
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
