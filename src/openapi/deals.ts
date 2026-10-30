/**
 * deals.ts
 * ────────────────────────────────────────────────────────────────────
 * **Deals** bundle (Open API tier). Schema matches the real APC v4.0.0 docs.
 *
 *   GET /openapi/v4.0.0/deals/{deal-id}/deal-payment/accessories/{accessory-id}
 *       — Get Deal Accessory
 */
import type { ApcClient, ApcResponse } from '../apc.js';

const VERSION = 'v4.0.0';
const BASE = `/${VERSION}/deals`;

export type PayType = 'CUSTOMER_PAY' | 'DEALER_PAY' | 'WARRANTY_PAY';
export type ProfitType = 'FRONT' | 'BACK';

/** A single accessory line within a deal's payment. */
export interface DealAccessory {
  id: string;
  code: string;
  name: string;
  disclosureType?: string;     // e.g. "SURFACE_PROTECTION"
  cost?: number;
  price?: number;
  residualValue?: number;
  residualized?: boolean;
  payType?: PayType;
  profitType?: ProfitType;
  taxable?: boolean;
  upfront?: boolean;
  taxUpfront?: boolean;
  partNumber?: string;
  createdTime?: number;
  modifiedTime?: number;
  [extra: string]: unknown;
}

/** Standard APC envelope. */
export interface ApcEnvelope<T> {
  data: T;
  meta: { status: string };
}

/**
 * Get Deal Accessory — fetch one accessory within a deal.
 * GET /v4.0.0/deals/{dealId}/deal-payment/accessories/{accessoryId}
 */
export function getDealAccessory(
  apc: ApcClient,
  dealId: string,
  accessoryId: string,
): Promise<ApcResponse<ApcEnvelope<DealAccessory>>> {
  return apc.get(
    `${BASE}/${encodeURIComponent(dealId)}/deal-payment/accessories/${encodeURIComponent(accessoryId)}`,
  );
}
