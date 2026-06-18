/**
 * financeAndInsurance.ts
 * ────────────────────────────────────────────────────────────────────
 * Wrappers for **Finance & Insurance** endpoints at the **Open API** tier.
 *
 * APC catalog: "Get Matching Finance & Insurance Products" — Open API.
 * (Plan creation, contract generation, ratings etc. are Premium tier.)
 */
import type { ApcClient, ApcResponse } from '../apc.js';

const VERSION = 'v4.0.0';
const BASE = `/${VERSION}/finance-insurance`;

export interface FIMatchQuery {
  /** Vehicle that needs product coverage. */
  vehicleId?: string;
  /** Deal context, if a deal is already in progress. */
  dealId?: string;
  /** Customer who'd own the policy. */
  customerId?: string;
  /** Optional category filter: WARRANTY, GAP, TIRE, etc. */
  category?: string;
}

export interface FIProduct {
  productId: string;
  category: string;
  name: string;
  provider?: string;
  termMonths?: number;
  termMiles?: number;
  retailPrice?: number;
  dealerCost?: number;
}

/** APC: "Get Matching Finance & Insurance Products". */
export function getMatchingProducts(
  apc: ApcClient,
  query: FIMatchQuery = {},
): Promise<ApcResponse<{ data: FIProduct[] }>> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v != null) qs.set(k, String(v));
  const tail = qs.toString() ? `?${qs}` : '';
  return apc.get(`${BASE}/products/matching${tail}`);
}

/**
 * Convenience: take a vehicle + customer and return F&I products grouped
 * by category — gives F&I managers a quick "what can I attach here" view.
 */
export async function matchingByCategory(
  apc: ApcClient,
  query: FIMatchQuery,
): Promise<Record<string, FIProduct[]>> {
  const r = await getMatchingProducts(apc, query);
  if (!r.ok) throw new Error(`getMatchingProducts failed: ${r.status}`);
  const groups: Record<string, FIProduct[]> = {};
  for (const p of r.body.data) {
    (groups[p.category] ??= []).push(p);
  }
  return groups;
}
