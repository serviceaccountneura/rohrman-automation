/**
 * ocrHelpers.ts — Shared OCR JSON field extraction, used by both subletFlow.ts
 * and miscFlow.ts. Reads the structure produced by vision_extract.py.
 */
import { readFileSync } from 'node:fs';

export function loadOcrJson(path: string): any {
  const raw = readFileSync(path, 'utf8');
  return JSON.parse(raw);
}

/** Extract vendor name from OCR data — tries vendor.name, falls back to vendor. */
export function getVendorName(ocr: any): string {
  return ocr?.vendor?.name ?? ocr?.vendor?.displayName ?? '';
}

/** Extract dealership name — user-selected, not OCR-extracted. */
export function getDealershipName(ocr: any): string {
  return ocr?.user_input?.dealership ?? ocr?.dealership?.name ?? ocr?.dealership ?? '';
}

/** Extract RO/control number from OCR data. */
export function getControlNumber(ocr: any): string {
  // Check _po_contract.ro_number first (vision_extract puts it here)
  if (ocr?._po_contract?.ro_number) return String(ocr._po_contract.ro_number).trim();
  // Check identifiers array
  const ids = ocr?.identifiers ?? [];
  for (const id of ids) {
    const label = (id.label ?? '').toLowerCase();
    if (label.includes('ro') || label.includes('repair order') || label.includes('control')) {
      return String(id.value ?? '').trim();
    }
  }
  // Fallback to control_number field
  return ocr?.control_number ?? ocr?.controlNumber ?? ocr?._po_contract?.control_number ?? '';
}

/** Strip trailing non-numeric suffix tokens, e.g. "6076404 RI" -> "6076404". */
function cleanInvoiceNumber(raw: string): string {
  const trimmed = raw.trim();
  const firstToken = trimmed.split(/\s+/)[0];
  return firstToken || trimmed;
}

/** Extract invoice number from OCR data. */
export function getInvoiceNumber(ocr: any): string {
  const ids = ocr?.identifiers ?? [];
  // Match "invoice number" specifically (not "invoice date")
  for (const id of ids) {
    const label = (id.label ?? '').toLowerCase();
    if (label.includes('invoice number') || label.includes('invoice #') || label === 'invoice') {
      return cleanInvoiceNumber(String(id.value ?? ''));
    }
  }
  // Broader fallback
  for (const id of ids) {
    const label = (id.label ?? '').toLowerCase();
    if (label.includes('invoice') && !label.includes('date')) {
      return cleanInvoiceNumber(String(id.value ?? ''));
    }
  }
  const fallback = ocr?.invoice_number ?? ocr?.invoiceNumber ?? ocr?._po_contract?.invoice_number ?? '';
  return fallback ? cleanInvoiceNumber(String(fallback)) : '';
}

/** Extract sales tax amount from OCR totals. */
export function getSalesTax(ocr: any): number {
  const totals = ocr?.totals ?? [];
  for (const t of totals) {
    const label = (t.label ?? '').toLowerCase();
    if (label.includes('sales tax') || label === 'tax') {
      return Math.abs(parseFloat(String(t.value ?? '0').replace(/[$,]/g, '')));
    }
  }
  return 0;
}

/** Extract total amount from OCR data. */
export function getTotalAmount(ocr: any): number {
  const totals = ocr?.totals ?? [];
  for (const t of totals) {
    const label = (t.label ?? '').toLowerCase();
    if (label.includes('grand total') || label.includes('total') || label.includes('balance due')) {
      return parseFloat(String(t.value ?? '0').replace(/[$,]/g, ''));
    }
  }
  const fallback = ocr?.total ?? ocr?._po_contract?.total ?? ocr?.summary?.total ?? 0;
  return Math.abs(fallback);
}

/** Extract line items with description/qty/unit_price/total_price parsed to numbers. */
export function getRawLineItems(ocr: any): Array<{ description: string; qty: number; unitPrice: number; totalPrice: number }> {
  const items = ocr?.line_items ?? [];
  return items.map((item: any) => {
    const qty = parseFloat(String(item.qty ?? '1').replace(/[$,]/g, '')) || 1;
    const unitPrice = Math.abs(parseFloat(String(item.unit_price ?? item.unitPrice ?? '0').replace(/[$,]/g, '')));
    const totalPrice = Math.abs(parseFloat(String(item.total_price ?? item.totalPrice ?? '0').replace(/[$,]/g, '')));
    return {
      description: item.description ?? '',
      qty,
      unitPrice,
      totalPrice,
    };
  });
}
