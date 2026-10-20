"""Unit checks for Vendor Invoice verify (no live Tekion).

Run:  uv run python scripts/test_vi_verify_unit.py
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from api.services import vi_verify
from api.services.vi_verify import (
    CODE_AMOUNT_MISMATCH,
    CODE_DUPLICATE,
    CODE_INVOICE_NUMBER_MISMATCH,
    CODE_JE_CONTROL,
    CODE_NO_GL,
    CODE_PO_NOT_FOUND,
    CODE_PRE_INVOICE_MISSING,
    ViVerifyInput,
    verify_vendor_invoice,
)


def _po(
    *,
    order_number: str = "36137",
    total: float = 53.23,
    invoice_status: str = "PRE_INVOICED",
    invoice_numbers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": 10968,
        "orderNumber": order_number,
        "totalAmount": total,
        "invoiceStatus": invoice_status,
        "status": "SUBMITTED",
        "universalId": "MISCELLANEOUS%10968",
        "vendorDetails": {"vendorName": "SCHAUMBURG KIA", "vendorId": "42345"},
        "invoiceNumbers": invoice_numbers
        or [{"invoiceId": "inv-1", "invoiceNumber": "PW-FLOW-001"}],
    }


def _client(
    po: dict[str, Any] | None,
    *,
    hit: dict[str, Any] | None = None,
    extra_hits: list[dict[str, Any]] | None = None,
    postings: list[dict[str, Any]] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.current_dealer_id = "1707"
    client.find_dealer_by_name.return_value = "1707"
    client.find_purchase_order.return_value = po
    client.upload_document.return_value = "media-abc"

    # Patch ApApprovalService methods used inside verify via the real class
    # by stubbing _req_json for search + postings.
    def _req_json(path: str, method: str = "GET", body: Any = None, **_kw: Any) -> dict[str, Any]:
        if "poInvoice/search" in path:
            search = str((body or {}).get("searchText") or "")
            primary = [hit] if hit else []
            # Digit-core searches (near-dup scan) return primary + extras.
            if search.isdigit() and extra_hits:
                hits = primary + list(extra_hits)
            else:
                hits = primary
            return {"data": {"hits": hits, "count": len(hits)}}
        if "invoicePostings" in path:
            return {"data": postings or []}
        if "paymentInfo" in path:
            return {"data": {}}
        if "invoiceDueDate" in path:
            return {"data": {"invoiceDueDate": 1}}
        if "lookup/numbers" in path:
            return {"data": {}}
        if any(x in path for x in ("postTransaction", "/poInvoice/post", "approve")):
            return {"data": {"ok": True}}
        raise AssertionError(f"unexpected call {method} {path}")

    client._req_json.side_effect = _req_json
    return client


def _input(**overrides: Any) -> ViVerifyInput:
    base = dict(
        po_number="36137",
        invoice_number="PW-FLOW-001",
        invoice_amount=53.23,
        dealership_name="Schaumburg Honda",
        po_type="MISCELLANEOUS",
    )
    base.update(overrides)
    return ViVerifyInput(**base)


def test_po_not_found() -> None:
    r = verify_vendor_invoice(client=_client(None), expected=_input(), dry_run=True)
    assert not r.ok and r.code == CODE_PO_NOT_FOUND


def test_pre_invoice_missing() -> None:
    po = _po(invoice_status="NOT_PRE_INVOICED", invoice_numbers=[])
    r = verify_vendor_invoice(client=_client(po), expected=_input(), dry_run=True)
    assert not r.ok and r.code == CODE_PRE_INVOICE_MISSING


def test_amount_mismatch() -> None:
    po = _po(total=53.23)
    hit = {
        "id": "inv-1",
        "invoiceNumber": "PW-FLOW-001",
        "invoiceAmount": {"amount": 9999},  # cents -> $99.99
        "payeeName": "SCHAUMBURG KIA",
        "poDetails": [{"poNum": "36137", "universalId": "MISCELLANEOUS%10968"}],
    }
    postings = [
        {"glAccountId": "1707_2410", "amount": 53.23, "description": "parts", "refText": "x"},
        {"glAccountId": "1707_3002", "amount": -53.23, "metaData": {"apPostingLine": True}},
    ]
    r = verify_vendor_invoice(
        client=_client(po, hit=hit, postings=postings),
        expected=_input(invoice_amount=53.23),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_AMOUNT_MISMATCH


def test_invoice_number_mismatch() -> None:
    po = _po()
    hit = {
        "id": "inv-1",
        "invoiceNumber": "OTHER-999",
        "invoiceAmount": {"amount": 5323},
        "payeeName": "SCHAUMBURG KIA",
        "poDetails": [{"poNum": "36137"}],
    }
    # find_invoice matches by PO number on hit, then we compare invoice numbers
    r = verify_vendor_invoice(
        client=_client(po, hit=hit, postings=[]),
        expected=_input(),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_INVOICE_NUMBER_MISMATCH


def test_missing_gl() -> None:
    po = _po()
    hit = {
        "id": "inv-1",
        "invoiceNumber": "PW-FLOW-001",
        "invoiceAmount": {"amount": 5323},
        "payeeName": "SCHAUMBURG KIA",
        "poDetails": [{"poNum": "36137"}],
    }
    postings = [
        {"glAccountId": "", "amount": 53.23},
        {"glAccountId": "1707_3002", "amount": -53.23, "metaData": {"apPostingLine": True}},
    ]
    r = verify_vendor_invoice(
        client=_client(po, hit=hit, postings=postings),
        expected=_input(),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_NO_GL


def test_je_control_required_for_7xxx() -> None:
    po = _po()
    hit = {
        "id": "inv-1",
        "invoiceNumber": "PW-FLOW-001",
        "invoiceAmount": {"amount": 5323},
        "payeeName": "SCHAUMBURG KIA",
        "poDetails": [{"poNum": "36137"}],
    }
    postings = [
        {
            "glAccountId": "1707_7193",
            "amount": 53.23,
            "description": "",
            "refText": "",
        },
        {"glAccountId": "1707_3002", "amount": -53.23, "metaData": {"apPostingLine": True}},
    ]
    r = verify_vendor_invoice(
        client=_client(po, hit=hit, postings=postings),
        expected=_input(),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_JE_CONTROL


def _happy_hit_postings() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    hit = {
        "id": "inv-1",
        "invoiceNumber": "PW-FLOW-001",
        "invoiceAmount": {"amount": 5323},
        "payeeName": "SCHAUMBURG KIA",
        "poDetails": [{"poNum": "36137", "universalId": "MISCELLANEOUS%10968"}],
    }
    postings = [
        {
            "glAccountId": "1707_2410",
            "amount": 53.23,
            "description": "parts",
            "refText": "ctrl",
        },
        {"glAccountId": "1707_3002", "amount": -53.23, "metaData": {"apPostingLine": True}},
    ]
    return hit, postings


def test_tekion_near_duplicate_flags() -> None:
    po = _po()
    hit = {
        "id": "inv-1",
        "invoiceNumber": "BLK123",
        "invoiceAmount": {"amount": 5323},
        "payeeName": "AUTO GLASS",
        "poDetails": [{"poNum": "36137"}],
    }
    extra = {
        "id": "inv-2",
        "invoiceNumber": "123",
        "invoiceAmount": {"amount": 5323},
        "payeeName": "AUTO GLASS",
        "poDetails": [{"poNum": "99999"}],
    }
    postings = [
        {"glAccountId": "1707_2410", "amount": 53.23, "description": "glass", "refText": "x"},
        {"glAccountId": "1707_3002", "amount": -53.23, "metaData": {"apPostingLine": True}},
    ]
    r = verify_vendor_invoice(
        client=_client(po, hit=hit, extra_hits=[extra], postings=postings),
        expected=_input(invoice_number="BLK123", invoice_amount=53.23),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_DUPLICATE


def test_happy_path_dry_run() -> None:
    hit, postings = _happy_hit_postings()
    client = _client(_po(), hit=hit, postings=postings)
    r = verify_vendor_invoice(client=client, expected=_input(), dry_run=True)
    assert r.ok, r.message
    assert not r.completed
    assert "Dry run" in " ".join(r.notes)
    # Must not create / pre-invoice
    for call in client._req_json.call_args_list:
        path = call.args[0] if call.args else call.kwargs.get("path", "")
        assert "preInvoice/post" not in path
        assert "create" not in path.lower() or "poInvoice" in path


def test_happy_path_completes_when_writes_on() -> None:
    hit, postings = _happy_hit_postings()
    client = _client(_po(), hit=hit, postings=postings)
    expected = _input(
        invoice_file_path=r"C:\tmp\fake-invoice.pdf",
        invoice_file_name="fake-invoice.pdf",
    )
    r = verify_vendor_invoice(client=client, expected=expected, dry_run=False)
    assert r.ok, r.message
    assert r.completed
    assert r.media_id == "media-abc"
    client.upload_document.assert_called_once()
    paths = [
        (c.args[0] if c.args else c.kwargs.get("path", ""))
        for c in client._req_json.call_args_list
    ]
    assert any("postTransaction" in p or p.endswith("/poInvoice/post") or "approve" in p for p in paths)
    assert not any("preInvoice/post" in p for p in paths)


def test_pipeline_po_invoice_path_never_preinvoices() -> None:
    """Static guard: the live PO_INVOICE entrypoints must not call pre-invoice / create-PO."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "api" / "services" / "pipeline_service.py").read_text(
        encoding="utf-8"
    )
    # Slice from _run_po_invoice through _resolve_existing_po (before chart helpers).
    start = src.index("def _run_po_invoice(")
    end = src.index("def _chart_by_number(")
    slice_ = src[start:end]
    # Ban real call sites / imports — not prose like "pre-invoice the PO in Tekion".
    banned = (
        "pre_invoice_stock_order",
        "preInvoice/post",
        ".pre_invoice(",
        "_create_misc_po",
        "_create_sublet_po",
        "_create_stock_po",
        "_run_purchase_order(",
        "_run_stock_pre_invoice(",
    )
    for token in banned:
        assert token not in slice_, f"forbidden call site in PO_INVOICE path: {token}"
    assert "verify_vendor_invoice" in slice_
    # Legacy helpers stay hard-disabled stubs.
    assert "Hard-disabled" in src
    assert src.index("def _run_stock_pre_invoice(") < src.index("def _run_po_invoice(")
    stock_body = src[
        src.index("def _run_stock_pre_invoice(") : src.index("def _run_po_invoice(")
    ]
    assert "pre_invoice_stock_order" not in stock_body
    po_body = src[src.index("def _run_purchase_order(") : src.index("def _sublet_items_from_written_ros(")]
    assert "_create_misc_po" not in po_body
    assert "_create_sublet_po" not in po_body


def test_norm_keeps_prefix() -> None:
    assert vi_verify._norm_invoice("  BLK123  ") == "BLK123"
    assert vi_verify._norm_invoice("123") == "123"


def main() -> None:
    tests = [
        test_norm_keeps_prefix,
        test_po_not_found,
        test_pre_invoice_missing,
        test_amount_mismatch,
        test_invoice_number_mismatch,
        test_missing_gl,
        test_je_control_required_for_7xxx,
        test_tekion_near_duplicate_flags,
        test_happy_path_dry_run,
        test_happy_path_completes_when_writes_on,
        test_pipeline_po_invoice_path_never_preinvoices,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print(f"\nAll {len(tests)} checks passed.")


if __name__ == "__main__":
    main()
