/**
 * employees.ts
 * ────────────────────────────────────────────────────────────────────
 * Wrappers for every **Employee** endpoint at the **Open API** tier.
 *
 * APC catalog: "Get User", "Get Users", "Employee API".
 * Useful for resolving user IDs (e.g. for Lead Assignees, PO Ordered By,
 * Sales Appointment Assignees).
 */
import type { ApcClient, ApcResponse } from '../apc.js';

const VERSION = 'v4.0.0';
const BASE = `/${VERSION}/employees`;

// ─── Types ─────────────────────────────────────────────────────────────

export interface Employee {
  userId: string;
  firstName: string;
  lastName: string;
  email?: string;
  phone?: string;
  role?: string;
  department?: string;
  active?: boolean;
  [extra: string]: unknown;
}

export interface EmployeeQuery {
  search?: string;
  role?: string;
  department?: string;
  active?: boolean;
  /** Cursor returned by a previous response. */
  nextPageKey?: string;
}

// ─── Endpoints ─────────────────────────────────────────────────────────

/** APC: "Get Users" — list of employees. */
export function listEmployees(
  apc: ApcClient,
  query: EmployeeQuery = {},
): Promise<ApcResponse<{ data: Employee[]; meta: { nextPageKey?: string } }>> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v != null) qs.set(k, String(v));
  const tail = qs.toString() ? `?${qs}` : '';
  return apc.get(`${BASE}${tail}`);
}

/** APC: "Get User" — single employee by ID. */
export function getEmployee(apc: ApcClient, userId: string): Promise<ApcResponse<Employee>> {
  return apc.get(`${BASE}/${encodeURIComponent(userId)}`);
}

/**
 * Convenience: page through every employee that matches a query and
 * return the full list. Open API endpoint, so no per-call charge.
 */
export async function listAllEmployees(apc: ApcClient, query: EmployeeQuery = {}): Promise<Employee[]> {
  const all: Employee[] = [];
  let cursor = query.nextPageKey;
  for (let page = 0; page < 1000; page++) {
    const r = await listEmployees(apc, { ...query, nextPageKey: cursor });
    if (!r.ok) throw new Error(`listEmployees failed: ${r.status}`);
    all.push(...r.body.data);
    cursor = r.body.meta?.nextPageKey;
    if (!cursor) return all;
  }
  return all;
}

/**
 * Convenience: find an employee by email, falling back to a name match.
 * Useful when an external system gives you a person but not their userId.
 */
export async function findEmployeeByEmail(apc: ApcClient, email: string): Promise<Employee | undefined> {
  const r = await listEmployees(apc, { search: email });
  if (!r.ok) return undefined;
  const lower = email.toLowerCase();
  return r.body.data.find((e) => e.email?.toLowerCase() === lower);
}
