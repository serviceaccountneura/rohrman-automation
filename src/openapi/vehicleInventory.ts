/**
 * vehicleInventory.ts
 * ────────────────────────────────────────────────────────────────────
 * Wrappers for every Tekion APC **Vehicle Inventory** endpoint that is
 * offered at the **Open API** tier (i.e. included in the Standard plan
 * at no per-call cost).
 *
 * API names follow the APC catalog exactly (see "APC Plans → APIs").
 * Paths follow the `/openapi/v3.1.0/vehicle-inventories…` pattern that
 * the APC docs use in the "Making Your First API Call" sample. If APC
 * publishes a different path under v4.x, override `VERSION` below.
 *
 * All functions return the raw `ApcResponse<T>` (status + ok + body) so
 * callers can decide how to react to non-2xx responses.
 */
import type { ApcClient, ApcResponse } from '../apc.js';

// Real path per APC v4.0.0 docs: GET /openapi/v4.0.0/vehicle-inventory (singular).
const VERSION = 'v4.0.0';
const BASE = `/${VERSION}/vehicle-inventory`;

// ─── Types ─────────────────────────────────────────────────────────────
export interface Vehicle {
  vehicleId?: string;
  vin: string;
  year: number;
  make: string;
  model: string;
  trim?: string;
  stockNo?: string;
  mileage?: number;
  retailPrice?: number;
  internetPrice?: number;
  invoicePrice?: number;
  msrp?: number;
  status?: string;
  subStatus?: string;
  marketable?: boolean;
  /** Tekion accepts unknown fields — keep this open. */
  [extra: string]: unknown;
}

/** Query params for "Get Vehicles from Inventory" (real v4.0.0 names). */
export interface VehicleSearchQuery {
  year?: string | number;
  make?: string;
  model?: string;
  stockId?: string;
  vin?: string;
  createdStartTime?: number;   // epoch ms
  createdEndTime?: number;
  modifiedStartTime?: number;
  modifiedEndTime?: number;
  status?: string;             // e.g. STOCKED_IN, SOLD
  nextPageToken?: string;
}

export interface BulkUpsertTask {
  taskId: string;
  status: 'PENDING' | 'IN_PROGRESS' | 'COMPLETED' | 'FAILED';
  totalRows?: number;
  processed?: number;
  failed?: number;
  errors?: Array<{ row: number; reason: string }>;
}

export interface VehicleAccessory {
  accessoryId?: string;
  code: string;
  name: string;
  price?: number;
  cost?: number;
  taxable?: boolean;
}

export interface VehicleFee {
  feeId?: string;
  code: string;
  name: string;
  amount: number;
  taxable?: boolean;
}

export interface VehicleDiscount {
  discountId?: string;
  code: string;
  amount: number;
  type?: 'PERCENTAGE' | 'FLAT';
}

export interface VehicleOffer {
  offerId?: string;
  code: string;
  amount: number;
  expiresOn?: string; // ISO date
}

export interface VehicleDamage {
  damageId?: string;
  area: string;
  severity: 'LOW' | 'MEDIUM' | 'HIGH';
  description?: string;
  repairCost?: number;
}

export interface VehicleCost {
  costId?: string;
  type: string;
  amount: number;
  date?: string;
  description?: string;
}

export interface VehicleMedia {
  mediaId?: string;
  url: string;
  mediaType: 'IMAGE' | 'VIDEO' | 'PDF';
  position?: number;
}

// ─── Vehicle CRUD ──────────────────────────────────────────────────────

/** APC: "Get Vehicles from Inventory" — paginated list. GET /v4.0.0/vehicle-inventory */
export function listVehicles(
  apc: ApcClient,
  query: VehicleSearchQuery = {},
): Promise<ApcResponse<{ data: Vehicle[]; meta: { nextPageToken?: string; status?: string } }>> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v != null) qs.set(k, String(v));
  const tail = qs.toString() ? `?${qs}` : '';
  return apc.get(`${BASE}${tail}`);
}

/** APC: "Get Vehicle from Inventory" — single vehicle by ID. */
export function getVehicle(apc: ApcClient, vehicleId: string): Promise<ApcResponse<Vehicle>> {
  return apc.get(`${BASE}/${encodeURIComponent(vehicleId)}`);
}

/** APC: "Create Vehicles" / "Create Vehicle Inventory". */
export function createVehicle(apc: ApcClient, vehicle: Vehicle): Promise<ApcResponse<Vehicle>> {
  return apc.post(BASE, vehicle);
}

/** APC: "Update a Vehicle" / "Update Vehicle Inventory". */
export function updateVehicle(
  apc: ApcClient,
  vehicleId: string,
  patch: Partial<Vehicle>,
): Promise<ApcResponse<Vehicle>> {
  return apc.put(`${BASE}/${encodeURIComponent(vehicleId)}`, patch);
}

/** APC: "Delete Vehicle". */
export function deleteVehicle(apc: ApcClient, vehicleId: string): Promise<ApcResponse<unknown>> {
  return apc.delete(`${BASE}/${encodeURIComponent(vehicleId)}`);
}

/** APC: "Delete Vehicle With Parameters" — soft-delete with reason + audit fields. */
export function deleteVehicleWithParams(
  apc: ApcClient,
  vehicleId: string,
  params: { reason?: string; deletedBy?: string; force?: boolean },
): Promise<ApcResponse<unknown>> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v != null) qs.set(k, String(v));
  return apc.delete(`${BASE}/${encodeURIComponent(vehicleId)}?${qs}`);
}

// ─── Search & bulk ─────────────────────────────────────────────────────

/** APC: "Search Vehicle Inventory" — POST body with rich filters. */
export function searchVehicleInventory(
  apc: ApcClient,
  filters: Record<string, unknown>,
): Promise<ApcResponse<{ data: Vehicle[]; meta: { nextPageKey?: string; totalCount?: number } }>> {
  return apc.post(`${BASE}/search`, filters);
}

/** APC: "Upsert Vehicles in Bulk" — async job, returns a task ID. */
export function upsertVehiclesInBulk(
  apc: ApcClient,
  vehicles: Vehicle[],
): Promise<ApcResponse<{ taskId: string }>> {
  return apc.post(`${BASE}/bulk`, { vehicles });
}

/** APC: "Get Bulk Upsert Task Details" — poll this until status COMPLETED/FAILED. */
export function getBulkUpsertTask(
  apc: ApcClient,
  taskId: string,
): Promise<ApcResponse<BulkUpsertTask>> {
  return apc.get(`${BASE}/bulk/${encodeURIComponent(taskId)}`);
}

/**
 * Convenience: kick off a bulk upsert and poll until it terminates.
 * Pure Open API — uses Upsert + Get Task only.
 */
export async function bulkUpsertAndWait(
  apc: ApcClient,
  vehicles: Vehicle[],
  opts: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<BulkUpsertTask> {
  const start = await upsertVehiclesInBulk(apc, vehicles);
  if (!start.ok) throw new Error(`Bulk upsert failed: ${start.status} ${JSON.stringify(start.body)}`);
  const taskId = start.body.taskId;
  const interval = opts.intervalMs ?? 3000;
  const deadline = Date.now() + (opts.timeoutMs ?? 10 * 60 * 1000);
  for (;;) {
    const r = await getBulkUpsertTask(apc, taskId);
    if (!r.ok) throw new Error(`Bulk task lookup failed: ${r.status} ${JSON.stringify(r.body)}`);
    if (r.body.status === 'COMPLETED' || r.body.status === 'FAILED') return r.body;
    if (Date.now() > deadline) throw new Error(`Bulk task ${taskId} did not finish before timeout.`);
    await new Promise((res) => setTimeout(res, interval));
  }
}

// ─── Accessories ───────────────────────────────────────────────────────

/** APC: "Get Vehicle Accessories". */
export function getAccessories(apc: ApcClient, vehicleId: string): Promise<ApcResponse<VehicleAccessory[]>> {
  return apc.get(`${BASE}/${encodeURIComponent(vehicleId)}/accessories`);
}

/** APC: "Get Vehicle Accessory by ID". */
export function getAccessory(apc: ApcClient, vehicleId: string, accessoryId: string): Promise<ApcResponse<VehicleAccessory>> {
  return apc.get(`${BASE}/${encodeURIComponent(vehicleId)}/accessories/${encodeURIComponent(accessoryId)}`);
}

/** APC: "Create Accessories in Bulk for Vehicle". */
export function createAccessoriesBulk(
  apc: ApcClient,
  vehicleId: string,
  accessories: VehicleAccessory[],
): Promise<ApcResponse<VehicleAccessory[]>> {
  return apc.post(`${BASE}/${encodeURIComponent(vehicleId)}/accessories/bulk`, { accessories });
}

/** APC: "Update Vehicle Accessory". */
export function updateAccessory(
  apc: ApcClient,
  vehicleId: string,
  accessoryId: string,
  patch: Partial<VehicleAccessory>,
): Promise<ApcResponse<VehicleAccessory>> {
  return apc.put(`${BASE}/${encodeURIComponent(vehicleId)}/accessories/${encodeURIComponent(accessoryId)}`, patch);
}

/** APC: "Delete Vehicle Accessory". */
export function deleteAccessory(apc: ApcClient, vehicleId: string, accessoryId: string): Promise<ApcResponse<unknown>> {
  return apc.delete(`${BASE}/${encodeURIComponent(vehicleId)}/accessories/${encodeURIComponent(accessoryId)}`);
}

// ─── Fees ──────────────────────────────────────────────────────────────

/** APC: "Get Vehicle Fees". */
export function getFees(apc: ApcClient, vehicleId: string): Promise<ApcResponse<VehicleFee[]>> {
  return apc.get(`${BASE}/${encodeURIComponent(vehicleId)}/fees`);
}

/** APC: "Get Vehicle Fee by ID". */
export function getFee(apc: ApcClient, vehicleId: string, feeId: string): Promise<ApcResponse<VehicleFee>> {
  return apc.get(`${BASE}/${encodeURIComponent(vehicleId)}/fees/${encodeURIComponent(feeId)}`);
}

/** APC: "Create Fees in Bulk for Vehicle". */
export function createFeesBulk(apc: ApcClient, vehicleId: string, fees: VehicleFee[]): Promise<ApcResponse<VehicleFee[]>> {
  return apc.post(`${BASE}/${encodeURIComponent(vehicleId)}/fees/bulk`, { fees });
}

/** APC: "Update Vehicle Fee". */
export function updateFee(apc: ApcClient, vehicleId: string, feeId: string, patch: Partial<VehicleFee>): Promise<ApcResponse<VehicleFee>> {
  return apc.put(`${BASE}/${encodeURIComponent(vehicleId)}/fees/${encodeURIComponent(feeId)}`, patch);
}

/** APC: "Delete Vehicle Fee". */
export function deleteFee(apc: ApcClient, vehicleId: string, feeId: string): Promise<ApcResponse<unknown>> {
  return apc.delete(`${BASE}/${encodeURIComponent(vehicleId)}/fees/${encodeURIComponent(feeId)}`);
}

// ─── Discounts ─────────────────────────────────────────────────────────

/** APC: "Create discounts for Vehicle". */
export function createDiscount(apc: ApcClient, vehicleId: string, discount: VehicleDiscount): Promise<ApcResponse<VehicleDiscount>> {
  return apc.post(`${BASE}/${encodeURIComponent(vehicleId)}/discounts`, discount);
}

/** APC: "Update discount for Vehicle". */
export function updateDiscount(apc: ApcClient, vehicleId: string, discountId: string, patch: Partial<VehicleDiscount>): Promise<ApcResponse<VehicleDiscount>> {
  return apc.put(`${BASE}/${encodeURIComponent(vehicleId)}/discounts/${encodeURIComponent(discountId)}`, patch);
}

/** APC: "Delete discount for Vehicle". */
export function deleteDiscount(apc: ApcClient, vehicleId: string, discountId: string): Promise<ApcResponse<unknown>> {
  return apc.delete(`${BASE}/${encodeURIComponent(vehicleId)}/discounts/${encodeURIComponent(discountId)}`);
}

// ─── Offers ────────────────────────────────────────────────────────────

/** APC: "Create offers for Vehicle". */
export function createOffer(apc: ApcClient, vehicleId: string, offer: VehicleOffer): Promise<ApcResponse<VehicleOffer>> {
  return apc.post(`${BASE}/${encodeURIComponent(vehicleId)}/offers`, offer);
}

/** APC: "Update offer for Vehicle". */
export function updateOffer(apc: ApcClient, vehicleId: string, offerId: string, patch: Partial<VehicleOffer>): Promise<ApcResponse<VehicleOffer>> {
  return apc.put(`${BASE}/${encodeURIComponent(vehicleId)}/offers/${encodeURIComponent(offerId)}`, patch);
}

/** APC: "Delete offer for Vehicle". */
export function deleteOffer(apc: ApcClient, vehicleId: string, offerId: string): Promise<ApcResponse<unknown>> {
  return apc.delete(`${BASE}/${encodeURIComponent(vehicleId)}/offers/${encodeURIComponent(offerId)}`);
}

// ─── Damages, costs, media, options ────────────────────────────────────

/** APC: "Create Damage for Vehicle". */
export function createDamage(apc: ApcClient, vehicleId: string, damage: VehicleDamage): Promise<ApcResponse<VehicleDamage>> {
  return apc.post(`${BASE}/${encodeURIComponent(vehicleId)}/damages`, damage);
}

/** APC: "Delete Damage for Vehicle". */
export function deleteDamage(apc: ApcClient, vehicleId: string, damageId: string): Promise<ApcResponse<unknown>> {
  return apc.delete(`${BASE}/${encodeURIComponent(vehicleId)}/damages/${encodeURIComponent(damageId)}`);
}

/** APC: "Create Vehicle Cost". */
export function createCost(apc: ApcClient, vehicleId: string, cost: VehicleCost): Promise<ApcResponse<VehicleCost>> {
  return apc.post(`${BASE}/${encodeURIComponent(vehicleId)}/costs`, cost);
}

/** APC: "Create Media for a Vehicle Inventory". */
export function createMedia(apc: ApcClient, vehicleId: string, media: VehicleMedia): Promise<ApcResponse<VehicleMedia>> {
  return apc.post(`${BASE}/${encodeURIComponent(vehicleId)}/media`, media);
}

/** APC: "Delete media for Vehicle". */
export function deleteMedia(apc: ApcClient, vehicleId: string, mediaId: string): Promise<ApcResponse<unknown>> {
  return apc.delete(`${BASE}/${encodeURIComponent(vehicleId)}/media/${encodeURIComponent(mediaId)}`);
}

/** APC: "Delete Option for Vehicle". */
export function deleteOption(apc: ApcClient, vehicleId: string, optionId: string): Promise<ApcResponse<unknown>> {
  return apc.delete(`${BASE}/${encodeURIComponent(vehicleId)}/options/${encodeURIComponent(optionId)}`);
}
