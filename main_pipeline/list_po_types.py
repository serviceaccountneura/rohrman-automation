#!/usr/bin/env python3
"""List PO types from all extracted JSON results."""
import json
import os

results_dir = os.path.join(os.path.dirname(__file__), "results")
for fname in sorted(os.listdir(results_dir)):
    if not fname.endswith(".json"):
        continue
    d = json.load(open(os.path.join(results_dir, fname)))
    dt = d.get("document_type", "?")
    po = d.get("purchase_order_number", "-")
    ro = d.get("ro_number", "-")
    inv = d.get("vendor_invoice_number", d.get("dealership_invoice_number", "-"))
    vendor = d.get("vendor", {}).get("name", "?") if isinstance(d.get("vendor"), dict) else "?"
    
    # GL account — handle dict or list
    ad = d.get("accounting_details", d.get("accounting_entry", {}))
    if isinstance(ad, dict):
        gl = ad.get("gl_account", "-")
    elif isinstance(ad, list) and ad and isinstance(ad[0], dict):
        gl = ad[0].get("gl_account", "-")
    else:
        gl = "-"
    
    # Line items count
    items = d.get("line_items", [])
    if not isinstance(items, list):
        items = []
    vfb_items = d.get("vendor_final_bill", {}).get("line_items", [])
    if not isinstance(vfb_items, list):
        vfb_items = []
    n_items = max(len(items), len(vfb_items))
    
    print(f"{fname}")
    print(f"  type={dt}  PO={po}  RO={ro}  inv={inv}")
    print(f"  vendor={vendor}  GL={gl}  items={n_items}")
    print()
