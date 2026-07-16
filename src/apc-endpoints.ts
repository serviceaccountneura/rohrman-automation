/**
 * apc-endpoints.ts — Catalog of APC endpoints exposed in the data-entry form.
 *
 * This file is the single place you update as you read each APC docs page.
 * Each entry needs only: name, method, path, and (optionally) an exampleBody.
 * For file uploads, set `multipart: { fileField }` and the form will swap to a
 * file picker for that endpoint.
 *
 * Tip: each APC docs page has a "Download Specification" button — open the
 * downloaded YAML/JSON to copy the exact path and request body schema.
 *
 * Only "Add Sales Appointment Assignee" is confirmed from the visible APC sample.
 * Everything else is a stub to be filled in.
 */
export type EndpointDef = {
  name: string;
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  /** path under APC_BASE_URL — keep {placeholders} you'll substitute in the UI */
  path: string;
  description?: string;
  exampleBody?: unknown;
  /** mark this endpoint as a file upload */
  multipart?: { fileField: string; description?: string };
};

export type Catalog = Record<string, EndpointDef[]>;

export const APC_ENDPOINTS: Catalog = {
  // Versions live in each endpoint's path. Sales Appointments uses v4.0.0 per
  // the APC docs sample; Vehicle Inventory uses v3.1.0. Confirm each in its docs page.
  'CRM Appointments': [
    {
      name: 'Add Sales Appointment Assignee',
      method: 'POST',
      path: '/v4.0.0/sales/appointments/{appointment-id}/assignees',
      description: 'Confirmed from APC docs sample. Replace {appointment-id} before sending.',
      exampleBody: { userId: '4f7428e5-f598-4c7e-acfb-668d21ece536', primary: true },
    },
    { name: 'Create Sales Appointment',              method: 'POST', path: '/v4.0.0/sales/appointments',                                    exampleBody: { /* TODO: copy schema from docs */ } },
    { name: 'Get Sales Appointment',                 method: 'GET',  path: '/v4.0.0/sales/appointments/{appointment-id}' },
    { name: 'Update Sales Appointment',              method: 'PUT',  path: '/v4.0.0/sales/appointments/{appointment-id}',                   exampleBody: {} },
    { name: 'Search Sales Appointments',             method: 'POST', path: '/v4.0.0/sales/appointments/search',                              exampleBody: {} },
    { name: 'Add Vehicle Info to Sales Appointment', method: 'POST', path: '/v4.0.0/sales/appointments/{appointment-id}/vehicle-info',       exampleBody: {} },
  ],

  'Vehicle Inventory': [
    {
      name: 'List Vehicle Inventory',
      method: 'GET',
      path: '/v3.1.0/vehicle-inventories?year=2022&make=Chevrolet',
      description: 'Confirmed from APC docs Vehicle Inventory sample. Adjust query params.',
    },
    { name: 'TODO: Create Vehicle Inventory item', method: 'POST', path: '/v3.1.0/vehicle-inventories', exampleBody: {} },
  ],

  'Repair Order': [
    { name: 'TODO: Create Repair Order', method: 'POST', path: '/vX.Y.Z/repair-orders',         exampleBody: {} },
    { name: 'TODO: Get Repair Order',    method: 'GET',  path: '/vX.Y.Z/repair-orders/{ro-id}' },
    { name: 'TODO: Update Repair Order', method: 'PUT',  path: '/vX.Y.Z/repair-orders/{ro-id}', exampleBody: {} },
  ],

  'Documents': [
    {
      name: 'Upload Document (invoice image)',
      method: 'POST',
      path: '/vX.Y.Z/documents/upload',
      description: 'Confirmed available — TIER: Premium API (Enterprise Tier 2/3). Confirm exact version, path, and multipart field name from the APC Document docs page.',
      multipart: { fileField: 'file' },
    },
    { name: 'Document Status',  method: 'GET',    path: '/vX.Y.Z/documents/{documentId}/status', description: 'Premium API.' },
    { name: 'Preview Document', method: 'GET',    path: '/vX.Y.Z/documents/{documentId}/preview', description: 'Premium API.' },
    { name: 'Document Delete',  method: 'DELETE', path: '/vX.Y.Z/documents/{documentId}',         description: 'Premium API.' },
  ],

  'Journal Entry / GL': [
    {
      name: 'Create a GL Posting',
      method: 'POST',
      path: '/vX.Y.Z/gl/postings',
      description: 'Confirmed available — TIER: Select API (Enterprise Tier 1+). Use Get Chart of Accounts first to look up account IDs.',
      exampleBody: { /* TODO: paste from the Journal Entry Posting docs page */ },
    },
    {
      name: 'Get Chart of Accounts',
      method: 'GET',
      path: '/vX.Y.Z/general-ledger/accounts',
      description: 'TIER: Premium API. Returns the GL account IDs you need for postings.',
    },
  ],
};
