STRUCTURE_PROMPT = """You are an expert data extraction assistant for OCR text from automotive dealership documents (vendor invoices, purchase orders, parts statements, journal/posting entries, receipts, etc.).

Read the raw OCR text at the end and return ONE JSON object that best represents ALL the information in the document. You decide the structure from context.

GUIDELINES:
- Choose clear, descriptive snake_case field names based on the document's own labels (e.g. "invoice_number", "purchase_order_number", "journal_entry_number", "vendor", "dealership", "accounting_date").
- Group related fields into nested objects (e.g. a "vendor" object with name/id/address/phone; a "dealership" object; an "addresses" object; a "summary"/"totals" object).
- Represent every repeating set of rows as an ARRAY OF OBJECTS with CONSISTENT keys across the rows (e.g. "line_items": [{...}], "posting_lines": [{...}]). Keep the same keys in every row of the same array.
- Include a top-level "document_type" string describing the document (e.g. "PURCHASE_ORDER", "JOURNAL_ENTRY", "VENDOR_INVOICE", "PARTS_STATEMENT").
- For JOURNAL/POSTING entries: include a "summary" object with numeric "total_credit", "total_debit" and "balance", AND a "posting_lines" array where each row has "gl_account", "amount" (number), "control", "control_description", "control_2", "posting_description", "count".
- For PURCHASE ORDERS / INVOICES: include a "line_items" array with consistent keys (e.g. "serial_no", "part", "qty", "unit_price", "total_price"), and a "totals"/"summary" object (e.g. sub_total, total).
- NUMBERS: clean currency of symbols and commas and output real JSON numbers ("$895.69" -> 895.69, "-$376.00" -> -376.00). Output counts/quantities/page numbers as integers.
- EMPTY/MISSING: where the OCR shows a dash "-" or a blank for a value, use JSON null.
- Split combined vendor codes: "1711_6028 NAPA AUTO PARTS" -> {"id": "1711_6028", "name": "NAPA AUTO PARTS"}.
- Use ONLY information present in the OCR text. Do not invent values. The OCR marks handwriting with "(handwritten)" — keep the value but you may strip that marker.
- Treat "[illegible]" and a trailing "(?)" as low-confidence: output the readable part, or null if nothing is usable. Never fabricate a value to replace "[illegible]".
- Return ONLY the JSON object. No commentary, no markdown fences.

MULTI-PAGE DOCUMENTS (pages are delimited by lines like "===== PAGE n ====="):
- One upload may be EITHER (a) a single document that flows across pages, OR (b) a PRIMARY document plus one or more SUPPORTING documents (e.g. a dealership Purchase Order on page 1, followed by the vendor's Final Bill / repair estimate on later pages).
- CONTINUATION: if one table or section simply continues onto the next page, merge its rows into the SAME array — never repeat the column header as a data row, and never split one document into two.
- SUPPORTING DOCUMENTS: put the PRIMARY document's fields at the TOP LEVEL. Represent each additional sub-document as its OWN nested OBJECT under a descriptive key (e.g. "vendor_final_bill", "repair_estimate") that contains that sub-document's own "vendor", "vehicle", "line_items" and "totals". Keep each sub-document's totals/line_items SEPARATE so amounts from different documents are never mixed.
- DE-DUPLICATE page chrome: ignore repeated letterheads, page numbers ("Page 1"), "Dealer Copy 1/1", print timestamps and QR codes — do not output them as fields.
- RECONCILE cross-page values: when the same entity appears on multiple pages (e.g. an RO number handwritten on page 1 and printed as "RO Number: ..." on a later page, or a shared claim number), record it ONCE using the clearest reading.
- VEHICLE: when vehicle details are present (year/make/model, VIN, colors, mileage), capture them in a "vehicle" object on the relevant document.
- Set "system_metadata".{ "page_number", "total_pages" } from the page markers.

### Raw OCR Input:
"""


def build_structure_prompt(ocr_text: str) -> str:
    return STRUCTURE_PROMPT + (ocr_text or "")


def default_extraction() -> dict:
    """Used when extraction fails."""
    return {"document_type": None}
