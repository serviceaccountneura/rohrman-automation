/**
 * src/openapi/index.ts
 * ────────────────────────────────────────────────────────────────────
 * Barrel re-exports for every Tekion APC Open API module wrapped here.
 *
 * Usage:
 *   import { apcFromEnv } from '../apc.js';
 *   import * as vehicles from './openapi/vehicleInventory.js';
 *   import * as leads    from './openapi/leads.js';
 *
 *   const apc = apcFromEnv();
 *   const r = await vehicles.searchVehicleInventory(apc, { year: 2024 });
 *
 * Every module here is **Open API tier** — no per-call charge on the
 * Standard plan.
 */
export * as vehicles from './vehicleInventory.js';
export * as leads from './leads.js';
export * as customers from './customers.js';
export * as employees from './employees.js';
export * as serviceAppointments from './serviceAppointments.js';
export * as support from './support.js';
export * as financeAndInsurance from './financeAndInsurance.js';

// Re-export the core client + token manager from one place so consumers
// can grab everything via `import { apcFromEnv, vehicles } from './openapi/index.js'`.
export { ApcClient, TokenManager, apcFromEnv } from '../apc.js';
export type { ApcConfig, ApcResponse } from '../apc.js';
