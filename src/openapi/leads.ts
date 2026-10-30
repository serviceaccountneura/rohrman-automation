/**
 * leads.ts
 * ────────────────────────────────────────────────────────────────────
 * Wrappers for every **CRM Leads** endpoint at the **Open API** tier.
 *
 * APC catalog: "Create Lead (v3.1.0)", "Update Lead (v3.1.0)",
 * "Create Lead Vehicles", "Create Lead Contacts", "Create Lead Notes",
 * "Create Lead Trade-In", "Create Lead Assignees", "Create Deal with Lead",
 * plus the matching Update / Delete operations.
 */
import type { ApcClient, ApcResponse } from '../apc.js';

// Real path per APC docs: /openapi/v4.0.0/leads/{lead-id}
const VERSION = 'v4.0.0';
const BASE = `/${VERSION}/leads`;

// ─── Types ─────────────────────────────────────────────────────────────

export interface Lead {
  leadId?: string;
  status?: string;
  source?: string;
  subSource?: string;
  campaign?: string;
  receivedAt?: string;
  /** Allow vendor-specific fields without losing type safety on knowns. */
  [extra: string]: unknown;
}

export interface LeadContact {
  contactId?: string;
  firstName: string;
  lastName: string;
  email?: string;
  phone?: string;
  isPrimary?: boolean;
}

export interface LeadVehicle {
  leadVehicleId?: string;
  vin?: string;
  year?: number;
  make?: string;
  model?: string;
  trim?: string;
  stockNo?: string;
  interestType?: 'NEW' | 'USED' | 'CPO';
}

export interface LeadTradeIn {
  tradeInId?: string;
  vin?: string;
  year?: number;
  make?: string;
  model?: string;
  mileage?: number;
  appraisedValue?: number;
}

export interface LeadNote {
  noteId?: string;
  body: string;
  author?: string;
  createdAt?: string;
}

export interface LeadAssignee {
  assigneeId?: string;
  userId: string;
  role?: string;
  primary?: boolean;
}

export interface DealFromLead {
  dealId?: string;
  status?: string;
  vehicleId?: string;
  customerId?: string;
}

// ─── Lead CRUD ─────────────────────────────────────────────────────────

/** APC: "Create Lead" (v3.1.0). */
export function createLead(apc: ApcClient, lead: Lead): Promise<ApcResponse<Lead>> {
  return apc.post(BASE, lead);
}

/**
 * APC: "Update Lead" — PUT /openapi/v4.0.0/leads/{lead-id}
 * Real schema (verified from docs): body is `{ source: {...} }`.
 * Updates lead source info and external references; returns the full LeadResponse.
 */
export interface LeadSource {
  sourceType?: string;     // e.g. "INTERNET"
  sourceName?: string;     // e.g. "cardekho.com"
  subSource?: string;
  leadEvent?: string;
  leadPromotion?: string;
  externalLeadId?: string;
}

export interface UpdateLeadRequest {
  source: LeadSource;
}

export interface LeadResponse {
  source?: LeadSource;
  status?: string;            // e.g. "BOOKED"
  department?: string;        // e.g. "SALES"
  oemLeadId?: string;
  externalLeadId?: string;
  oemName?: string;
  createdTime?: number;
  modifiedTime?: number;
  stage?: string;
  customStage?: string;
  notes?: { link: string };
  contacts?: { link: string };
  vehicles?: { link: string };
  tradeIns?: { link: string };
  assignees?: { link: string };
  [extra: string]: unknown;
}

export function updateLead(
  apc: ApcClient,
  leadId: string,
  body: UpdateLeadRequest,
): Promise<ApcResponse<{ data: LeadResponse; meta: { status: string } }>> {
  return apc.put(`${BASE}/${encodeURIComponent(leadId)}`, body);
}

// ─── Lead Vehicles ─────────────────────────────────────────────────────

/** APC: "Create Lead Vehicles". */
export function createLeadVehicle(apc: ApcClient, leadId: string, vehicle: LeadVehicle): Promise<ApcResponse<LeadVehicle>> {
  return apc.post(`${BASE}/${encodeURIComponent(leadId)}/vehicles`, vehicle);
}

/** APC: "Update Lead Vehicle". */
export function updateLeadVehicle(
  apc: ApcClient,
  leadId: string,
  leadVehicleId: string,
  patch: Partial<LeadVehicle>,
): Promise<ApcResponse<LeadVehicle>> {
  return apc.put(
    `${BASE}/${encodeURIComponent(leadId)}/vehicles/${encodeURIComponent(leadVehicleId)}`,
    patch,
  );
}

// ─── Lead Contacts ─────────────────────────────────────────────────────

/** APC: "Create Lead Contacts". */
export function createLeadContact(apc: ApcClient, leadId: string, contact: LeadContact): Promise<ApcResponse<LeadContact>> {
  return apc.post(`${BASE}/${encodeURIComponent(leadId)}/contacts`, contact);
}

/** APC: "Update Lead Contact". */
export function updateLeadContact(
  apc: ApcClient,
  leadId: string,
  contactId: string,
  patch: Partial<LeadContact>,
): Promise<ApcResponse<LeadContact>> {
  return apc.put(
    `${BASE}/${encodeURIComponent(leadId)}/contacts/${encodeURIComponent(contactId)}`,
    patch,
  );
}

// ─── Lead Notes ────────────────────────────────────────────────────────

/** APC: "Create Lead Notes". */
export function createLeadNote(apc: ApcClient, leadId: string, note: LeadNote): Promise<ApcResponse<LeadNote>> {
  return apc.post(`${BASE}/${encodeURIComponent(leadId)}/notes`, note);
}

/** APC: "Update Lead Note". */
export function updateLeadNote(
  apc: ApcClient,
  leadId: string,
  noteId: string,
  patch: Partial<LeadNote>,
): Promise<ApcResponse<LeadNote>> {
  return apc.put(
    `${BASE}/${encodeURIComponent(leadId)}/notes/${encodeURIComponent(noteId)}`,
    patch,
  );
}

// ─── Trade-Ins ────────────────────────────────────────────────────────

/** APC: "Create Lead Trade-In". */
export function createLeadTradeIn(apc: ApcClient, leadId: string, tradeIn: LeadTradeIn): Promise<ApcResponse<LeadTradeIn>> {
  return apc.post(`${BASE}/${encodeURIComponent(leadId)}/trade-ins`, tradeIn);
}

/** APC: "Update Lead Trade-In". */
export function updateLeadTradeIn(
  apc: ApcClient,
  leadId: string,
  tradeInId: string,
  patch: Partial<LeadTradeIn>,
): Promise<ApcResponse<LeadTradeIn>> {
  return apc.put(
    `${BASE}/${encodeURIComponent(leadId)}/trade-ins/${encodeURIComponent(tradeInId)}`,
    patch,
  );
}

// ─── Assignees ────────────────────────────────────────────────────────

/** APC: "Create Lead Assignees". */
export function createLeadAssignees(
  apc: ApcClient,
  leadId: string,
  assignees: LeadAssignee[],
): Promise<ApcResponse<LeadAssignee[]>> {
  return apc.post(`${BASE}/${encodeURIComponent(leadId)}/assignees`, { assignees });
}

// ─── Lead → Deal conversion ────────────────────────────────────────────

/** APC: "Create Deal with Lead" — converts a lead into a deal in one call. */
export function createDealWithLead(
  apc: ApcClient,
  leadId: string,
  payload: Record<string, unknown>,
): Promise<ApcResponse<DealFromLead>> {
  return apc.post(`${BASE}/${encodeURIComponent(leadId)}/deals`, payload);
}

// ─── Composite: full intake in one call ───────────────────────────────

export interface IntakePayload {
  lead: Lead;
  contact?: LeadContact;
  vehicleOfInterest?: LeadVehicle;
  tradeIn?: LeadTradeIn;
  note?: LeadNote;
  assignees?: LeadAssignee[];
}

export interface IntakeResult {
  lead: Lead;
  contact?: LeadContact;
  vehicleOfInterest?: LeadVehicle;
  tradeIn?: LeadTradeIn;
  note?: LeadNote;
  assignees?: LeadAssignee[];
}

/**
 * Higher-level helper: take a single intake payload (e.g. submitted from
 * a website form), spread it across the right Open API endpoints, and
 * return everything created. Stops at the first failure.
 */
export async function intakeLead(apc: ApcClient, payload: IntakePayload): Promise<IntakeResult> {
  const leadRes = await createLead(apc, payload.lead);
  if (!leadRes.ok) throw new Error(`createLead failed: ${leadRes.status}`);
  const leadId = leadRes.body.leadId;
  if (!leadId) throw new Error('createLead returned no leadId');

  const out: IntakeResult = { lead: leadRes.body };

  if (payload.contact) {
    const r = await createLeadContact(apc, leadId, payload.contact);
    if (!r.ok) throw new Error(`createLeadContact failed: ${r.status}`);
    out.contact = r.body;
  }
  if (payload.vehicleOfInterest) {
    const r = await createLeadVehicle(apc, leadId, payload.vehicleOfInterest);
    if (!r.ok) throw new Error(`createLeadVehicle failed: ${r.status}`);
    out.vehicleOfInterest = r.body;
  }
  if (payload.tradeIn) {
    const r = await createLeadTradeIn(apc, leadId, payload.tradeIn);
    if (!r.ok) throw new Error(`createLeadTradeIn failed: ${r.status}`);
    out.tradeIn = r.body;
  }
  if (payload.note) {
    const r = await createLeadNote(apc, leadId, payload.note);
    if (!r.ok) throw new Error(`createLeadNote failed: ${r.status}`);
    out.note = r.body;
  }
  if (payload.assignees?.length) {
    const r = await createLeadAssignees(apc, leadId, payload.assignees);
    if (!r.ok) throw new Error(`createLeadAssignees failed: ${r.status}`);
    out.assignees = r.body;
  }
  return out;
}
