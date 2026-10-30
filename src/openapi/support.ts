/**
 * support.ts
 * ────────────────────────────────────────────────────────────────────
 * Wrappers for **Support** endpoints at the **Open API** tier.
 *
 * APC catalog: "Opcode API", "Employee API" (Employee API also lives in
 * src/openapi/employees.ts; here we focus on operation/opcodes).
 */
import type { ApcClient, ApcResponse } from '../apc.js';

const VERSION = 'v4.0.0';
const BASE = `/${VERSION}/support`;

export interface Opcode {
  opcodeId: string;
  code: string;
  description: string;
  laborHours?: number;
  flatRate?: number;
  /** e.g. "DEALER", "WARRANTY", "INTERNAL" */
  type?: string;
}

export interface OpcodeQuery {
  search?: string;
  type?: string;
  nextPageKey?: string;
}

/** APC: "Opcode API" — list operation codes. */
export function listOpcodes(
  apc: ApcClient,
  query: OpcodeQuery = {},
): Promise<ApcResponse<{ data: Opcode[]; meta: { nextPageKey?: string } }>> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v != null) qs.set(k, String(v));
  const tail = qs.toString() ? `?${qs}` : '';
  return apc.get(`${BASE}/opcodes${tail}`);
}

/** Look up one opcode by ID. */
export function getOpcode(apc: ApcClient, opcodeId: string): Promise<ApcResponse<Opcode>> {
  return apc.get(`${BASE}/opcodes/${encodeURIComponent(opcodeId)}`);
}

/** Resolve an opcode by its short `code` field (e.g. "OIL-CHG"). */
export async function findOpcodeByCode(apc: ApcClient, code: string): Promise<Opcode | undefined> {
  const r = await listOpcodes(apc, { search: code });
  if (!r.ok) return undefined;
  const upper = code.toUpperCase();
  return r.body.data.find((o) => o.code.toUpperCase() === upper);
}
