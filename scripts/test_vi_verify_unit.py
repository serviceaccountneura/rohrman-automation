"""Unit checks for Vendor Invoice verify (no live Tekion).

Run:  uv run python scripts/test_vi_verify_unit.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from api.services import vi_verify
from api.services.vi_verify import (
    CODE_AMOUNT_MISMATCH,
    CODE_DATE_MISMATCH,
    CODE_DUPLICATE,
    CODE_INVOICE_NUMBER_MISMATCH,
    CODE_JE_CONTROL,
    CODE_NO_GL,
    CODE_PO_NUMBER_MISMATCH,
    CODE_PRE_INVOICE_MISSING,
    CODE_UNBALANCED,
    CODE_VENDOR_MISMATCH,
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

    def _req_json(path: str, method: str = "GET", body: Any = None, **_kw: Any) -> dict[str, Any]:
        if "poInvoice/search" in path:
            search = str((body or {}).get("searchText") or "")
            primary = [hit] if hit else []
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
        vendor_name="SCHAUMBURG KIA",
        invoice_date="10/09/2026",
    )
    base.update(overrides)
    return ViVerifyInput(**base)


def _hit(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "inv-1",
        "invoiceNumber": "PW-FLOW-001",
        "invoiceAmount": {"amount": 5323},
        "payeeName": "SCHAUMBURG KIA",
        "invoiceDate": int(
            datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc).timestamp() * 1000
        ),
        "poDetails": [{"poNum": "36137", "universalId": "MISCELLANEOUS%10968"}],
    }
    base.update(overrides)
    return base


def _vendor_postings(ref: str = "SCHAUMBURG KIA", acct: str = "2410") -> list[dict[str, Any]]:
    return [
        {
            "glAccountId": f"1707_{acct}",
            "amount": 53.23,
            "description": "parts",
            "refText": ref,
        },
        {"glAccountId": "1707_3002", "amount": -53.23, "metaData": {"apPostingLine": True}},
    ]


def test_pre_invoice_missing_no_vi() -> None:
    """Primary lookup is by invoice number on the Pre Invoice list."""
    r = verify_vendor_invoice(client=_client(None, hit=None), expected=_input(), dry_run=True)
    assert not r.ok and r.code == CODE_PRE_INVOICE_MISSING


def test_ocr_po_mismatch_is_hard_fail() -> None:
    """3-way PO #: OCR must match Tekion PO / Pre Invoice when OCR has a PO #."""
    client = _client(_po(), hit=_hit(), postings=_vendor_postings())
    r = verify_vendor_invoice(
        client=client,
        expected=_input(po_number="071526"),  # OCR garbage (looks like a date)
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_PO_NUMBER_MISMATCH
    assert r.match.get("poNumber", {}).get("invoice") == "071526"
    assert r.match.get("poNumber", {}).get("preInvoice") == "36137"


def test_finds_vi_when_ocr_po_blank() -> None:
    """Blank OCR PO is OK — VI + PO legs still must agree."""
    client = _client(_po(), hit=_hit(), postings=_vendor_postings())
    r = verify_vendor_invoice(
        client=client,
        expected=_input(po_number=""),
        dry_run=True,
    )
    assert r.ok, r.message
    assert r.po_number == "36137"


def test_po_total_mismatch() -> None:
    r = verify_vendor_invoice(
        client=_client(
            _po(total=99.99),
            hit=_hit(),
            postings=_vendor_postings(),
        ),
        expected=_input(invoice_amount=53.23),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_AMOUNT_MISMATCH
    assert r.match.get("amount", {}).get("po") == 99.99
    assert r.match.get("amount", {}).get("preInvoice") == 53.23


def test_po_vendor_vs_vi_mismatch() -> None:
    po = _po()
    po["vendorDetails"] = {"vendorName": "OTHER PO VENDOR", "vendorId": "1"}
    r = verify_vendor_invoice(
        client=_client(po, hit=_hit(), postings=_vendor_postings()),
        expected=_input(),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_VENDOR_MISMATCH
    assert "OTHER PO VENDOR" in r.message


def test_amount_mismatch() -> None:
    r = verify_vendor_invoice(
        client=_client(_po(), hit=_hit(invoiceAmount={"amount": 9999}), postings=_vendor_postings()),
        expected=_input(invoice_amount=53.23),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_AMOUNT_MISMATCH


def test_invoice_number_not_on_pre_invoice_list() -> None:
    """Search returns a different invoice # → treat as missing pre-invoice."""
    r = verify_vendor_invoice(
        client=_client(_po(), hit=_hit(invoiceNumber="OTHER-999"), postings=[]),
        expected=_input(),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_PRE_INVOICE_MISSING


def test_vendor_mismatch() -> None:
    r = verify_vendor_invoice(
        client=_client(
            _po(),
            hit=_hit(payeeName="OTHER VENDOR INC"),
            postings=_vendor_postings("OTHER VENDOR INC"),
        ),
        expected=_input(vendor_name="SCHAUMBURG KIA"),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_VENDOR_MISMATCH
    assert "Vendor" in r.message


def test_invoice_date_mismatch() -> None:
    r = verify_vendor_invoice(
        client=_client(_po(), hit=_hit(), postings=_vendor_postings()),
        expected=_input(invoice_date="01/15/2026"),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_DATE_MISMATCH
    assert "date" in r.message.lower()


def test_missing_gl() -> None:
    postings = [
        {"glAccountId": "", "amount": 53.23},
        {"glAccountId": "1707_3002", "amount": -53.23, "metaData": {"apPostingLine": True}},
    ]
    r = verify_vendor_invoice(
        client=_client(_po(), hit=_hit(), postings=postings),
        expected=_input(),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_NO_GL


def test_je_unbalanced_fails() -> None:
    """PO_INVOICE JE screen: Balance must be $0.00 (same as OEM)."""
    postings = [
        {
            "glAccountId": "1707_2410",
            "amount": 53.23,
            "description": "parts",
            "refText": "SCHAUMBURG KIA",
        },
        # Credit short by $1 → Balance ≠ 0
        {
            "glAccountId": "1707_3002",
            "amount": -52.23,
            "metaData": {"apPostingLine": True},
        },
    ]
    r = verify_vendor_invoice(
        client=_client(_po(), hit=_hit(), postings=postings),
        expected=_input(),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_UNBALANCED
    assert "Balance" in r.message


def test_control_must_be_vendor_name() -> None:
    prev = vi_verify.ENFORCE_JE_CONTROL
    vi_verify.ENFORCE_JE_CONTROL = True
    try:
        r = verify_vendor_invoice(
            client=_client(_po(), hit=_hit(), postings=_vendor_postings("WRONG CTRL", "3142")),
            expected=_input(),
            dry_run=True,
        )
        assert not r.ok and r.code == CODE_JE_CONTROL
        assert "SCHAUMBURG KIA" in r.message
    finally:
        vi_verify.ENFORCE_JE_CONTROL = prev


def test_je_control_7xxx_needs_description() -> None:
    prev = vi_verify.ENFORCE_JE_CONTROL
    vi_verify.ENFORCE_JE_CONTROL = True
    try:
        postings = [
            {
                "glAccountId": "1707_7193",
                "amount": 53.23,
                "description": "",
                "refText": "SCHAUMBURG KIA",
            },
            {"glAccountId": "1707_3002", "amount": -53.23, "metaData": {"apPostingLine": True}},
        ]
        r = verify_vendor_invoice(
            client=_client(_po(), hit=_hit(), postings=postings),
            expected=_input(),
            dry_run=True,
        )
        assert not r.ok and r.code == CODE_JE_CONTROL
        assert "Description" in r.message
    finally:
        vi_verify.ENFORCE_JE_CONTROL = prev


def test_sublet_2460_control_is_ro() -> None:
    prev = vi_verify.ENFORCE_JE_CONTROL
    vi_verify.ENFORCE_JE_CONTROL = True
    try:
        postings = [
            {
                "glAccountId": "1707_2460",
                "amount": 53.23,
                "description": "sublet",
                "refText": "SCHAUMBURG KIA",
            },
            {"glAccountId": "1707_3002", "amount": -53.23, "metaData": {"apPostingLine": True}},
        ]
        r = verify_vendor_invoice(
            client=_client(_po(), hit=_hit(), postings=postings),
            expected=_input(po_type="SUBLET", ro_number="123456"),
            dry_run=True,
        )
        assert not r.ok and r.code == CODE_JE_CONTROL
        assert "123456" in r.message
    finally:
        vi_verify.ENFORCE_JE_CONTROL = prev


def test_tekion_near_duplicate_flags() -> None:
    hit = _hit(invoiceNumber="BLK123", payeeName="AUTO GLASS")
    extra = {
        "id": "inv-2",
        "invoiceNumber": "123",
        "invoiceAmount": {"amount": 5323},
        "payeeName": "AUTO GLASS",
        "poDetails": [{"poNum": "99999"}],
    }
    po = _po(
        invoice_numbers=[{"invoiceId": "inv-1", "invoiceNumber": "BLK123"}],
    )
    po["vendorDetails"] = {"vendorName": "AUTO GLASS", "vendorId": "1"}
    r = verify_vendor_invoice(
        client=_client(
            po,
            hit=hit,
            extra_hits=[extra],
            postings=_vendor_postings("AUTO GLASS"),
        ),
        expected=_input(
            invoice_number="BLK123",
            invoice_amount=53.23,
            vendor_name="AUTO GLASS",
        ),
        dry_run=True,
    )
    assert not r.ok and r.code == CODE_DUPLICATE


def test_happy_path_dry_run() -> None:
    client = _client(_po(), hit=_hit(), postings=_vendor_postings())
    r = verify_vendor_invoice(client=client, expected=_input(), dry_run=True)
    assert r.ok, r.message
    assert not r.completed
    assert r.match.get("amount", {}).get("invoice") == 53.23
    assert "Dry run" in " ".join(r.notes)
    for call in client._req_json.call_args_list:
        path = call.args[0] if call.args else call.kwargs.get("path", "")
        assert "preInvoice/post" not in path


def test_happy_path_completes_when_writes_on() -> None:
    client = _client(_po(), hit=_hit(), postings=_vendor_postings())
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
    assert any(
        "postTransaction" in p or p.endswith("/poInvoice/post") or "approve" in p
        for p in paths
    )
    assert not any("preInvoice/post" in p for p in paths)


def test_pipeline_po_invoice_path_never_preinvoices() -> None:
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "api" / "services" / "pipeline_service.py"
    ).read_text(encoding="utf-8")
    start = src.index("def _run_po_invoice(")
    end = src.index("def _chart_by_number(")
    slice_ = src[start:end]
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


def test_norm_keeps_prefix() -> None:
    assert vi_verify._norm_invoice("  BLK123  ") == "BLK123"
    assert vi_verify._norm_invoice("123") == "123"


def main() -> None:
    tests = [
        test_norm_keeps_prefix,
        test_pre_invoice_missing_no_vi,
        test_ocr_po_mismatch_is_hard_fail,
        test_finds_vi_when_ocr_po_blank,
        test_po_total_mismatch,
        test_po_vendor_vs_vi_mismatch,
        test_amount_mismatch,
        test_invoice_number_not_on_pre_invoice_list,
        test_vendor_mismatch,
        test_invoice_date_mismatch,
        test_missing_gl,
        test_je_unbalanced_fails,
        test_control_must_be_vendor_name,
        test_je_control_7xxx_needs_description,
        test_sublet_2460_control_is_ro,
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
