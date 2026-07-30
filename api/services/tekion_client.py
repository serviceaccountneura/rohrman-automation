"""Tekion API client — direct REST calls (no browser automation).

Python port of src/api/tekionApi.ts. Uses requests + pyotp instead of
fetch + otplib.

Flow:
  1. login()          -> identity-provider -> password -> MFA -> tokens
  2. switchDealer()   -> set dealer context headers
  3. searchVendor()   -> lookup/search VENDOR by name
  4. searchRO()       -> lookup/search REPAIR_ORDER_ASSET by RO number
  5. getROJobs()      -> service-module/u/ro/{roId} -> job list
  6. createSubletPO() -> partTrade/u/sublet/order/v2/create
  7. createMiscPO()   -> partTrade/u/misc/order
  8. preInvoice()     -> getInvoiceDate -> dueDate -> postings -> post

Env: TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

import pyotp
import requests

BASE = "https://app.tekioncloud.com"
TENANT = "rohrmanautomotivegroup"


class TekionApiClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.cookies: str = ""
        self.api_token: str = ""
        self.user_id: str = ""
        self.dealer_id: str = ""
        self.site_id: str = ""
        self.dealers: list[dict[str, str]] = []

    @property
    def current_dealer_id(self) -> str:
        return self.dealer_id

    # ── Request helper ────────────────────────────────────────────────────────

    def _req(
        self,
        path: str,
        method: str = "GET",
        body: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{BASE}{path}"

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "clientid": "web",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE,
            "Referer": f"{BASE}/",
        }
        if self.cookies:
            headers["Cookie"] = self.cookies
        if self.api_token:
            headers["tekion-api-token"] = self.api_token
        if self.user_id:
            headers["userid"] = self.user_id
            headers["original-userid"] = self.user_id
        if self.dealer_id:
            headers["dealerid"] = self.dealer_id
            headers["roleid"] = f"{self.dealer_id}_APClerk"
            headers["tek-siteid"] = self.site_id
        headers["applicationid"] = "ARC_NA"
        headers["productids"] = "ARC"
        headers["program"] = "DEFAULT"
        headers["subapplicationid"] = "US"
        headers["locale"] = "en_US"
        headers["original-tenantid"] = TENANT
        headers["tenantname"] = TENANT
        if extra_headers:
            headers.update(extra_headers)

        resp = self.session.request(
            method=method,
            url=url,
            headers=headers,
            json=body if body is not None else None,
        )

        # Capture Set-Cookie headers
        set_cookies = resp.raw.headers.getlist("Set-Cookie") if resp.raw else []
        if set_cookies:
            new_cookies = "; ".join(c.split(";")[0] for c in set_cookies)
            self.cookies = f"{self.cookies}; {new_cookies}" if self.cookies else new_cookies
            names = [c.split("=")[0].split(";")[0] for c in set_cookies]
            print(f"[API] Cookies captured: {', '.join(names)}")

        return resp

    def _req_json(
        self,
        path: str,
        method: str = "GET",
        body: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        resp = self._req(path, method=method, body=body, extra_headers=extra_headers)
        if not resp.ok:
            raise Exception(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def login(self) -> list[dict[str, str]]:
        email = os.environ.get("TEKION_USERNAME", "")
        password = os.environ.get("TEKION_PASSWORD", "")
        totp_secret = os.environ.get("TEKION_TOTP_SECRET", "")

        if not email or not password or not totp_secret:
            raise Exception("TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET required in .env")

        # Step 1: identity-provider
        print("[API] Login: identity-provider...")
        self._req_json(
            "/api/loginservice/p/identity-provider",
            method="POST",
            body={"email": email},
        )

        # Step 2: password
        print("[API] Login: password...")
        pw_res = self._req_json(
            "/api/loginservice/p/authenticate/password",
            method="POST",
            body={"email": email, "password": password},
        )
        pw_data = pw_res.get("data") or {}
        mfa_response = pw_data.get("mfaResponse") or pw_data
        mfa_token = mfa_response.get("mfaToken")
        user_id = mfa_response.get("userId")
        if not mfa_token:
            raise Exception("No mfaToken in password response")
        if not user_id:
            raise Exception("No userId in password response")

        # Step 3: MFA
        totp = pyotp.TOTP(totp_secret.replace(" ", ""))
        otp = totp.now()
        print(f"[API] Login: MFA (otp={otp})...")
        mfa_res = self._req_json(
            "/api/loginservice/p/authenticate/mfa",
            method="POST",
            body={
                "tenantId": TENANT,
                "userId": user_id,
                "mfaToken": mfa_token,
                "authenticatorType": "GOOGLE_AUTHENTICATOR",
                "otp": otp,
            },
        )

        login_data = (
            (mfa_res.get("data") or {}).get("loginData")
            or (mfa_res.get("data") or {}).get("mfaResponse", {}).get("loginData")
        )
        if not login_data:
            raise Exception("No loginData in MFA response")

        self.dealer_id = login_data["dealerId"]
        self.user_id = login_data.get("id") or user_id
        self.api_token = login_data.get("access_token", "")
        self.dealers = [
            {
                "dealerId": d["dealerId"],
                "dealerDisplayName": d["dealerDisplayName"],
                "dealerChars": d.get("dealerChars", ""),
            }
            for d in login_data.get("dealer", [])
        ]
        self.site_id = f"-1_{self.dealer_id}"

        print(f"[API] Logged in as {login_data.get('displayName')}, default dealer: {self.dealer_id}")
        print(f"[API] Available dealers: {len(self.dealers)}")

        return self.dealers

    def switch_dealer(self, dealer_id: str) -> None:
        self.dealer_id = dealer_id
        self.site_id = f"-1_{dealer_id}"
        print(f"[API] Switched to dealer {dealer_id}")

    def find_dealer_by_name(self, name: str) -> str | None:
        lower = name.lower().strip()
        # Exact match
        for d in self.dealers:
            if d["dealerDisplayName"].lower() == lower:
                return d["dealerId"]
        # Contains match
        for d in self.dealers:
            dn = d["dealerDisplayName"].lower()
            if lower in dn or dn in lower:
                return d["dealerId"]
        # Keyword match
        keywords = [w for w in lower.split() if len(w) > 2]
        for d in self.dealers:
            dn = d["dealerDisplayName"].lower()
            if all(k in dn for k in keywords):
                return d["dealerId"]
        return None

    # ── Lookups ───────────────────────────────────────────────────────────────

    def search_vendor(self, search_text: str) -> list[dict[str, str]]:
        res = self._req_json(
            "/api/lookup/search",
            method="POST",
            body={
                "VENDOR": {
                    "filters": [
                        {
                            "field": "vendorStatus",
                            "operator": "NIN",
                            "values": ["DRAFT", "INACTIVE", "ON_HOLD"],
                            "key": "vendorStatus",
                        }
                    ],
                    "searchText": search_text,
                    "pageInfo": {"start": 0, "rows": 20},
                    "sort": [],
                }
            },
        )

        results = self._extract_lookup_results(res, "VENDOR")
        vendors = []
        for v in results:
            sites = v.get("sites") or []
            site = sites[0] if sites else {}
            contacts = site.get("pointOfContacts") or []
            contact = contacts[0] if contacts else {}
            vendors.append(
                {
                    "id": str(v.get("id", "")),
                    "name": v.get("vendorName", ""),
                    "displayId": v.get("vendorDisplayId", ""),
                    "siteId": str(site.get("id", "")),
                    "phone": contact.get("phone") or contact.get("mobile") or "",
                    "email": contact.get("email") or "",
                }
            )
        return vendors

    def search_ro(self, ro_number: str) -> list[dict[str, str]]:
        res = self._req_json(
            "/api/lookup/search",
            method="POST",
            body={
                "REPAIR_ORDER_ASSET": {
                    "filters": [
                        {"field": "status", "operator": "NIN", "values": ["INVOICED", "CLOSED", "VOIDED"]},
                        {"field": "siteId", "operator": "IN", "values": [self.site_id]},
                    ],
                    "searchText": ro_number,
                    "pageInfo": {"start": 0, "rows": 20},
                    "sort": [],
                }
            },
        )

        results = self._extract_lookup_results(res, "REPAIR_ORDER_ASSET", "displayValue")
        ros = []
        for r in results:
            ros.append(
                {
                    "id": str(r.get("id", "")),
                    "roNumber": str(r.get("_displayValue") or r.get("roNumber") or r.get("roNo") or ""),
                    "status": r.get("status", ""),
                }
            )
        return ros

    def get_ro_jobs(self, ro_id: str) -> list[dict[str, str]]:
        res = self._req_json(
            f"/api/service-module/u/ro/{ro_id}",
            method="POST",
            body=["JOB", "RECOMMENDATION"],
        )

        jobs = (res.get("data") or {}).get("jobs") or []
        return [
            {
                "id": j["id"],
                "jobNumber": str(j.get("jobNumber", "")),
                "payType": j.get("payType", ""),
                "subPayType": j.get("subPayType", ""),
                "roId": j.get("roId", ro_id),
                "roNumber": j.get("roNo", ""),
            }
            for j in jobs
        ]

    # ── Sublet PO Creation ────────────────────────────────────────────────────

    def create_sublet_po(
        self,
        vendor_id: int,
        vendor_name: str,
        vendor_display_id: str,
        vendor_site_id: str,
        items: list[dict[str, Any]],
        vendor_phone: str = "",
        vendor_email: str = "",
    ) -> dict[str, Any]:
        items_to_add = []
        for item in items:
            items_to_add.append(
                {
                    "description": item["description"],
                    "jobId": item["jobId"],
                    "invoiceId": None,
                    "invoiceAmount": None,
                    "partTaxable": False,
                    "laborTaxable": False,
                    "glAccountId": item.get("glAccountId", ""),
                    "itemId": item["jobId"],
                    "itemType": "job",
                    "referenceId": item["referenceId"],
                    "referenceNumber": item["referenceNumber"],
                    "subletOpcodeDetail": {
                        "opcode": item.get("opcode", "SUBLET"),
                        "serviceTypeIds": item.get("serviceTypeIds", []),
                        "category": item.get("category", "MISCELLANEOUS"),
                        "subletMarkUpDetails": [],
                    },
                    "poLineItemLaborAmount": item["laborAmount"],
                    "poLineItemPartsAmount": item["partsAmount"],
                }
            )

        body = {
            "action": "SUBMITTED",
            "poTaxConfiguration": {"taxCodeGrid": [], "taxDetails": [], "flatTaxDetails": []},
            "newTaxCodeEnabled": False,
            "taxable": False,
            "taxDetails": [
                {"taxRegimeType": "SALES_TAX", "partTaxPercentage": 10, "laborTaxPercentage": 10}
            ],
            "taxOverridden": False,
            "flatTaxApplied": False,
            "vendorId": vendor_id,
            "vendorName": vendor_name,
            "vendorDisplayId": vendor_display_id,
            "vendorSiteId": vendor_site_id,
            "vendorPhone": vendor_phone,
            "vendorEmail": vendor_email,
            "estimateDeliveryTime": None,
            "expectedPickupDate": "",
            "temporaryVendor": False,
            "vendorAddress": {},
            "billingAddress": {},
            "itemsToAdd": items_to_add,
            "itemsToUpdate": [],
            "itemsToDelete": [],
            "printConfigDto": {
                "copyTypes": ["VENDOR", "DEALER"],
                "sortDetails": None,
                "copyTypeVsLocale": None,
            },
        }

        print("[API] Creating Sublet PO...")
        res = self._req_json("/api/partTrade/u/sublet/order/v2/create", method="POST", body=body)

        data = res.get("data")
        if not data:
            raise Exception("No data in sublet create response")

        po_id = data["id"]
        po_number = data["orderNumber"]
        universal_id = f"SUBLET%{po_id}"

        print(f"[API] Sublet PO created: {po_number} (id={po_id}, status={data['status']})")

        return {
            "poId": po_id,
            "poNumber": po_number,
            "universalId": universal_id,
            "status": data["status"],
            "orderType": data.get("orderType", ""),
        }

    # ── Misc PO Creation ──────────────────────────────────────────────────────

    def create_misc_po(
        self,
        vendor_id: str,
        vendor_name: str,
        vendor_display_id: str,
        vendor_site_id: str,
        items: list[dict[str, Any]],
        vendor_phone: str = "",
        vendor_email: str = "",
    ) -> dict[str, Any]:
        formatted_items = []
        for item in items:
            gross_total = round(item["qty"] * item["price"], 2)
            formatted_items.append(
                {
                    "grossTotalPrice": gross_total,
                    "item": item["description"],
                    "id": str(uuid.uuid4()),
                    "qty": item["qty"],
                    "unit": item.get("unit", "ea"),
                    "price": item["price"],
                    "additional": {},
                    "charges": [],
                }
            )

        body = {
            "vendorSiteId": vendor_site_id,
            "vendorId": vendor_id,
            "vendorPhone": vendor_phone,
            "vendorEmail": vendor_email,
            "vendorName": vendor_name,
            "vendorDisplayId": vendor_display_id,
            "vendorAddress": {},
            "temporaryVendor": False,
            "estimateDeliveryTime": None,
            "shippingAddress": {},
            "billingAddress": {},
            "poAssetType": "misc",
            "companyId": 1,
            "parts": False,
            "items": formatted_items,
            "poChargeToAdd": [],
            "poChargeToUpdate": [],
            "poChargeToDelete": [],
            "status": "READY_TO_SUBMIT",
            "controlNumber": None,
            "orderType": "MISC",
            "purchaseType": "REGULAR",
            "additional": {},
            "newTaxCodeEnabled": False,
            "poTaxConfiguration": {"taxCodeGrid": [], "taxDetails": [], "flatTaxDetails": []},
            "printConfigDto": {
                "copyTypes": ["VENDOR", "DEALER"],
                "sortDetails": None,
                "copyTypeVsLocale": None,
            },
        }

        print("[API] Creating Misc PO...")
        res = self._req_json("/api/partTrade/u/misc/order", method="POST", body=body)

        data = res.get("data")
        if not data:
            raise Exception("No data in misc order create response")

        po_id = data["id"]
        po_number = data["orderNumber"]
        universal_id = f"MISCELLANEOUS%{po_id}"

        print(f"[API] Misc PO created: {po_number} (id={po_id}, status={data['status']})")

        return {
            "poId": po_id,
            "poNumber": po_number,
            "universalId": universal_id,
            "status": data["status"],
            "orderType": data.get("orderType", ""),
        }

    # ── Pre-Invoice Flow ──────────────────────────────────────────────────────

    def pre_invoice(
        self,
        vendor_id: str,
        vendor_site_id: str,
        vendor_name: str,
        vendor_display_id: str,
        dealer_id: str,
        po_id: int,
        po_number: str,
        universal_id: str,
        invoice_number: str,
        invoice_amount: float,
        gl_account_id: str,
        ap_gl_account_id: str,
        ref_text: str,
        po_type: str,
        sales_tax: float = 0.0,
        invoice_date: str | None = None,
    ) -> dict[str, str]:
        amount_cents = round(invoice_amount * 100)
        tax_cents = round(sales_tax * 100)
        subtotal_cents = amount_cents - tax_cents

        # Step 1: Get invoice date
        print("[API] Pre-invoice: getInvoiceDate...")
        date_res = self._req_json(
            "/api/accounting/u/poInvoice/getInvoiceDate",
            method="POST",
            body={"type": "INVOICE", "vendorId": vendor_id},
        )
        _data = date_res.get("data")
        if isinstance(_data, dict):
            invoice_date_ms = _data.get("invoiceDate")
        elif isinstance(_data, (int, float)):
            invoice_date_ms = _data
        else:
            invoice_date_ms = None
        if not invoice_date_ms:
            date_str = invoice_date or datetime.now(tz=timezone.utc).strftime("%m/%d/%Y")
            invoice_date_ms = int(datetime.strptime(date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc).timestamp() * 1000)
        print(f"[API] Invoice date: {datetime.fromtimestamp(invoice_date_ms / 1000, tz=timezone.utc).strftime('%m/%d/%Y')}")

        # Step 2: Get due date
        print("[API] Pre-invoice: invoiceDueDate...")
        due_res = self._req_json(
            "/api/accounting/u/poInvoice/invoiceDueDate",
            method="POST",
            body={
                "invoiceDate": invoice_date_ms,
                "vendorId": vendor_id,
                "vendorSiteId": int(vendor_site_id),
            },
        )
        _due_data = due_res.get("data")
        if isinstance(_due_data, dict):
            due_date_ms = _due_data.get("invoiceDueDate")
        elif isinstance(_due_data, (int, float)):
            due_date_ms = _due_data
        else:
            due_date_ms = None
        if not due_date_ms:
            due_date_ms = invoice_date_ms + 30 * 24 * 60 * 60 * 1000
        print(f"[API] Due date: {datetime.fromtimestamp(due_date_ms / 1000, tz=timezone.utc).strftime('%m/%d/%Y')}")

        # Step 3: Get postings
        print("[API] Pre-invoice: postings...")
        self._req_json(
            "/api/accounting/u/poInvoice/preInvoicing/postings",
            method="POST",
            body={
                "vendorId": vendor_id,
                "poUniversalIds": [universal_id],
                "invoiceAmount": {"amount": subtotal_cents, "currency": "USD"},
                "apGlAccountId": ap_gl_account_id,
                "poNumbers": [po_number],
            },
        )

        # Step 4: Post pre-invoice
        print("[API] Pre-invoice: post...")
        post_res = self._req_json(
            "/api/accounting/u/poInvoice/preInvoice/post",
            method="POST",
            body={
                "dealerId": dealer_id,
                "payeeId": int(vendor_id),
                "payeeType": "VENDOR",
                "vendorSiteId": vendor_site_id,
                "invoiceNumber": invoice_number,
                "invoiceAmount": {"amount": amount_cents, "currency": "USD"},
                "invoiceDate": invoice_date_ms,
                "invoiceDueDate": due_date_ms,
                "salesTax": {"amount": tax_cents, "currency": "USD"},
                "taxes": [{"type": "SALES_TAX", "amount": tax_cents}],
                "fees": {"amount": 0, "currency": "USD"},
                "shippingCharges": {"amount": 0, "currency": "USD"},
                "discount": {"amount": 0, "currency": "USD"},
                "attachments": [],
                "transactionDetails": None,
                "accountingDetails": [
                    {
                        "description": None,
                        "glAccountId": gl_account_id,
                        "postingAmount": {"amount": subtotal_cents, "currency": "USD"},
                        "controlNumberList": None,
                        "refText": ref_text,
                    }
                ],
                "type": "INVOICE",
                "payeeNumber": vendor_display_id,
                "payeeName": vendor_name,
                "apGlAccountId": ap_gl_account_id,
                "poDetails": [
                    {
                        "poId": po_id,
                        "poNum": po_number,
                        "poType": po_type,
                        "universalId": universal_id,
                        "poInvoicedAmount": {"amount": subtotal_cents, "currency": "USD"},
                    }
                ],
            },
        )

        post_data = post_res.get("data") or post_res
        invoice_id = post_data.get("id") or post_data.get("invoiceId") or "unknown"
        print(f"[API] Pre-invoice posted: invoiceId={invoice_id}")

        return {"invoiceId": str(invoice_id), "status": "posted"}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_lookup_results(
        self, res: dict[str, Any], entity_type: str, display_value_field: str | None = None
    ) -> list[dict[str, Any]]:
        data = res.get("data") or {}
        entities = (data.get(entity_type) or {}).get("entities")
        if isinstance(entities, list):
            results = []
            for e in entities:
                d = e.get("data") or e
                if display_value_field:
                    d[f"_{display_value_field}"] = e.get("displayValue")
                results.append(d)
            return results

        # Fallback shapes
        et_data = data.get(entity_type) or {}
        if et_data.get("results"):
            return et_data["results"]
        if data.get("results"):
            return data["results"]
        if isinstance(data, list):
            return data
        top_et = res.get(entity_type) or {}
        if top_et.get("results"):
            return top_et["results"]
        if isinstance(data.get(entity_type), list):
            return data[entity_type]

        print(f"[API] Could not extract results for {entity_type}: {str(res)[:200]}")
        return []
