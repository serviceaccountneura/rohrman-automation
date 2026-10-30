/**
 * serviceAppointments.ts
 * ────────────────────────────────────────────────────────────────────
 * Wrappers for **Service Appointments** endpoints at the **Open API** tier.
 *
 * APC catalog: "Appointment Slots" — read-only slot availability for
 * scheduling. (Appointment Create / Update / Cancel are Premium tier.)
 */
import type { ApcClient, ApcResponse } from '../apc.js';

const VERSION = 'v4.0.0';
const BASE = `/${VERSION}/service/appointment-slots`;

export interface SlotsQuery {
  /** ISO date for the start of the window — required. */
  startDate: string;
  /** ISO date for the end of the window — required. */
  endDate: string;
  /** Filter to a single shop. */
  serviceShopId?: string;
  /** Filter to a specific technician. */
  technicianId?: string;
  /** Service type / opcode the customer wants. */
  opcode?: string;
}

export interface AppointmentSlot {
  slotId: string;
  start: string;
  end: string;
  serviceShopId: string;
  technicianId?: string;
  capacityRemaining?: number;
}

/** APC: "Appointment Slots" — list available time slots. */
export function getAppointmentSlots(
  apc: ApcClient,
  query: SlotsQuery,
): Promise<ApcResponse<{ data: AppointmentSlot[] }>> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v != null) qs.set(k, String(v));
  return apc.get(`${BASE}?${qs}`);
}

/**
 * Convenience: collapse the slot list into "open days" — useful for
 * showing a customer-facing day picker before drilling into times.
 */
export async function listOpenDays(apc: ApcClient, query: SlotsQuery): Promise<string[]> {
  const r = await getAppointmentSlots(apc, query);
  if (!r.ok) throw new Error(`getAppointmentSlots failed: ${r.status}`);
  const days = new Set<string>();
  for (const s of r.body.data) days.add(s.start.slice(0, 10));
  return Array.from(days).sort();
}
