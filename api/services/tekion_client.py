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

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import pyotp
import requests
from sqlmodel import Session

from api.models.db import TekionSession

BASE = "https://app.tekioncloud.com"
TENANT = "rohrmanautomotivegroup"


class TekionApiClient:
    def __init__(self, db_session: Session | None = None) -> None:
        self.session = requests.Session()
        self.cookies: str = ""
        self.api_token: str = ""
        self.user_id: str = ""
        self.dealer_id: str = ""
        self.site_id: str = ""
        self.dealers: list[dict[str, str]] = []
        self._db: Session | None = db_session
        self._logged_in = False

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
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Chromium";v="138", "Google Chrome";v="138"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
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
        _relogin: bool = True,
    ) -> Any:
        resp = self._req(path, method=method, body=body, extra_headers=extra_headers)
        if not resp.ok:
            # 401/403 → session expired. Re-login once and retry.
            if resp.status_code in (401, 403) and _relogin and not path.startswith("/api/loginservice"):
                print(f"[API] Got {resp.status_code} on {method} {path} — re-logging in…")
                # login() resets dealer_id to the account default (the dealer on
                # loginData). Since _req() rebuilds the dealerid/roleid/tek-siteid
                # headers from that field on every call, letting it stand would
                # silently retarget this retry — and everything after it — at the
                # wrong dealership. Worse, a path with the dealer in the URL
                # (e.g. .../transaction/dealer/1711/draft) would then carry 1711
                # in the URL and 1707 in the headers.
                previous_dealer = self.dealer_id
                self.login()
                if previous_dealer and previous_dealer != self.dealer_id:
                    print(f"[API] Restoring dealer {previous_dealer} after re-login "
                          f"(login defaulted to {self.dealer_id})")
                    self.switch_dealer(previous_dealer)
                return self._req_json(
                    path, method=method, body=body, extra_headers=extra_headers, _relogin=False
                )
            raise Exception(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def login(self) -> list[dict[str, str]]:
        email = os.environ.get("TEKION_USERNAME", "")
        password = os.environ.get("TEKION_PASSWORD", "")
        totp_secret = os.environ.get("TEKION_TOTP_SECRET", "")

        if not email or not password or not totp_secret:
            raise Exception("TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET required in .env")

        # Reset session state for a clean login.
        self.cookies = ""
        self.api_token = ""
        self.user_id = ""
        self.session = requests.Session()

        # Step 1: identity-provider
        print("[API] Login: identity-provider...")
        self._req_json(
            "/api/loginservice/p/identity-provider",
            method="POST",
            body={"email": email},
            _relogin=False,
        )

        # Step 2: password
        print("[API] Login: password...")
        pw_res = self._req_json(
            "/api/loginservice/p/authenticate/password",
            method="POST",
            body={"email": email, "password": password},
            _relogin=False,
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
            _relogin=False,
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
        self._logged_in = True

        print(f"[API] Logged in as {login_data.get('displayName')}, default dealer: {self.dealer_id}")
        print(f"[API] Available dealers: {len(self.dealers)}")

        # Persist the session so we don't re-login next time.
        self._save_session()

        return self.dealers

    # ── Session persistence ────────────────────────────────────────────────────

    def _save_session(self) -> None:
        """Upsert the current session into the DB."""
        if not self._db:
            return
        existing = self._db.get(TekionSession, 1)
        now = datetime.now(timezone.utc)
        if existing:
            existing.cookies = self.cookies
            existing.api_token = self.api_token
            existing.user_id = self.user_id
            existing.dealer_id = self.dealer_id
            existing.site_id = self.site_id
            existing.dealers_json = json.dumps(self.dealers)
            existing.last_used_at = now
            self._db.add(existing)
        else:
            row = TekionSession(
                id=1,
                cookies=self.cookies,
                api_token=self.api_token,
                user_id=self.user_id,
                dealer_id=self.dealer_id,
                site_id=self.site_id,
                dealers_json=json.dumps(self.dealers),
                created_at=now,
                last_used_at=now,
            )
            self._db.add(row)
        self._db.commit()
        print(f"[API] Session saved to DB (dealer={self.dealer_id})")

    def load_session(self) -> bool:
        """Hydrate from the DB session. Returns True if a session was found."""
        if not self._db:
            return False
        row = self._db.get(TekionSession, 1)
        if not row or not row.api_token:
            return False
        self.cookies = row.cookies
        self.api_token = row.api_token
        self.user_id = row.user_id
        self.dealer_id = row.dealer_id
        self.site_id = row.site_id
        try:
            self.dealers = json.loads(row.dealers_json) if row.dealers_json else []
        except (json.JSONDecodeError, TypeError):
            self.dealers = []
        self._logged_in = True
        # Update last_used_at
        row.last_used_at = datetime.now(timezone.utc)
        self._db.add(row)
        self._db.commit()
        print(f"[API] Session loaded from DB (dealer={self.dealer_id}, "
              f"saved {row.created_at.isoformat() if row.created_at else 'unknown'})")
        return True

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

    def get_vendor_by_display_id(self, vendor_display_id: str) -> dict[str, str] | None:
        """Fetch a single vendor by its Tekion vendorDisplayId (e.g. '1707_160').

        Returns the same shape as search_vendor() entries, or None if not found.
        """
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
                        },
                        {
                            "field": "vendorDisplayId",
                            "operator": "IN",
                            "values": [vendor_display_id],
                            "key": "vendorDisplayId",
                        },
                    ],
                    "searchText": "",
                    "pageInfo": {"start": 0, "rows": 5},
                    "sort": [],
                }
            },
        )

        results = self._extract_lookup_results(res, "VENDOR")
        for v in results:
            if str(v.get("vendorDisplayId", "")) == vendor_display_id:
                sites = v.get("sites") or []
                site = sites[0] if sites else {}
                contacts = site.get("pointOfContacts") or []
                contact = contacts[0] if contacts else {}
                return {
                    "id": str(v.get("id", "")),
                    "name": v.get("vendorName", ""),
                    "displayId": v.get("vendorDisplayId", ""),
                    "siteId": str(site.get("id", "")),
                    "phone": contact.get("phone") or contact.get("mobile") or "",
                    "email": contact.get("email") or "",
                }
        return None

    def get_dealer_address(self) -> dict[str, Any]:
        """Fetch the current dealer's default address from the bootstrap endpoint."""
        res = self._req_json("/api/api-platform/u/bootstrap")
        dm = (res.get("data") or {}).get("dealerMaster") or {}
        addresses = dm.get("dealerAddress") or []
        if not addresses:
            raise Exception(f"No dealer address found for dealer {self.dealer_id}")
        return addresses[0]

    def fetch_gl_accounts(self) -> list[dict[str, Any]]:
        """Fetch all GL accounts for the current dealership from Tekion.

        Uses POST /api/lookup/search with GL_ACCOUNT_ASSET, paginated.
        Returns a list of dicts with: account_id, account_number, account_name,
        account_type, department_type, active.
        """
        accounts: list[dict[str, Any]] = []
        start = 0
        rows = 100

        while True:
            res = self._req_json(
                "/api/lookup/search",
                method="POST",
                body={
                    "GL_ACCOUNT_ASSET": {
                        "filters": [],
                        "searchText": "",
                        "pageInfo": {"start": start, "rows": rows},
                        "sort": [],
                    }
                },
            )
            data = (res.get("data") or {}).get("GL_ACCOUNT_ASSET") or {}
            entities = data.get("entities") or []
            count = data.get("count", 0)

            for e in entities:
                d = e.get("data") or {}
                accounts.append({
                    "account_id": d.get("id", ""),
                    "account_number": d.get("accountNumber", ""),
                    "account_name": d.get("accountName", ""),
                    "account_type": d.get("accountTypeId", ""),
                    "department_type": d.get("departmentType", ""),
                    "active": d.get("active", True),
                })

            start += rows
            if start >= count or len(entities) == 0:
                break

        print(f"[API] Fetched {len(accounts)} GL accounts for dealer {self.dealer_id}")
        return accounts

    def create_stock_order(
        self,
        vendor_id: int,
        vendor_name: str,
        vendor_display_id: str,
        vendor_site_id: str,
        vendor_phone: str,
        vendor_email: str,
        parts: list[dict[str, Any]],
        dealer_address: dict[str, Any],
    ) -> dict[str, Any]:
        """Create + submit a vendor stock order (NON_OEM_STOCK_ORDER).

        No pre-invoice — the order is submitted and ready for receiving on the
        Tekion UI.
        """
        # Build the shipping/billing address from the dealer's default address.
        addr = {
            "complexListId": dealer_address.get("complexListId", "Default"),
            "dealerAddressID": dealer_address.get("dealerAddressID", "1"),
            "addressType": dealer_address.get("addressType", 0),
            "addressTypeDisplayName": dealer_address.get("addressTypeDisplayName", "DEFAULT"),
            "streetAddress1": dealer_address.get("streetAddress1", ""),
            "streetAddress2": dealer_address.get("streetAddress2", ""),
            "streetAddress3": dealer_address.get("streetAddress3"),
            "streetAddress4": dealer_address.get("streetAddress4"),
            "city": dealer_address.get("city", ""),
            "state": dealer_address.get("state", ""),
            "zipCode": dealer_address.get("zipCode", ""),
            "country": dealer_address.get("country", "US"),
            "county": dealer_address.get("county", ""),
            "isdCode": dealer_address.get("isdCode", ""),
            "locationUrl": dealer_address.get("locationUrl"),
            "epa": dealer_address.get("epa"),
            "bar": dealer_address.get("bar"),
            "website": dealer_address.get("website"),
            "phoneNumber": dealer_address.get("phoneNumber"),
            "phoneNumberUnformatted": dealer_address.get("phoneNumberUnformatted"),
            "email": dealer_address.get("email"),
            "departmentId": dealer_address.get("departmentId"),
            "departmentName": dealer_address.get("departmentName"),
            "makeId": dealer_address.get("makeId"),
            "makeName": dealer_address.get("makeName"),
            "fromEmail": dealer_address.get("fromEmail"),
            "fromName": dealer_address.get("fromName"),
            "geoLocation": dealer_address.get("geoLocation"),
            "addressOverride": dealer_address.get("addressOverride"),
            "isActive": dealer_address.get("isActive", True),
            "line1": dealer_address.get("streetAddress1", ""),
            "line2": dealer_address.get("streetAddress2", ""),
            "pincode": dealer_address.get("zipCode", ""),
        }

        # Build parts list — parts come in with alias keys (partNumber, unitPrice, etc.)
        parts_payload = []
        for p in parts:
            part_number = p.get("partNumber") or p.get("part_number", "")
            part_name = p.get("partName") or p.get("part_name", "")
            unit_price = p.get("unitPrice") or p.get("unit_price", 0)
            brand_code = p.get("brandCode") or p.get("brand_code", "")
            qty = p.get("qty", 1)
            part_id = f"M_{brand_code}_{part_number}".replace(" ", "_")
            parts_payload.append({
                "partId": part_id,
                "qty": qty,
                "unit": "ea",
                "price": unit_price,
                "charges": [],
                "partNumber": part_number,
                "partName": part_name,
                "dmsPartNumber": part_number.replace(" ", ""),
                "brandCode": brand_code,
                "groupIds": [],
                "extra": {
                    "originalCostPrice": unit_price,
                    "partCostOverriddenByVendorPricing": False,
                },
            })

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
            "shippingAddress": addr,
            "billingAddress": addr,
            "poAssetType": "parts",
            "companyId": 1,
            "parts": parts_payload,
            "items": False,
            "poChargeToAdd": [],
            "poChargeToUpdate": [],
            "poChargeToDelete": [],
            "status": "READY_TO_SUBMIT",
            "controlNumber": None,
            "orderType": "NON_OEM_STOCK_ORDER",
            "purchaseType": "REGULAR",
            "additional": {},
            "newTaxCodeEnabled": False,
            "poTaxConfiguration": {
                "taxCodeGrid": [],
                "taxDetails": [],
                "flatTaxDetails": [],
            },
            "printConfigDto": {
                "copyTypes": ["VENDOR", "DEALER"],
                "sortDetails": None,
                "copyTypeVsLocale": None,
            },
        }

        res = self._req_json(
            "/api/partTrade/u/nonOem/order",
            method="POST",
            body=body,
        )
        data = res.get("data") or {}
        return {
            "orderId": data.get("id"),
            "orderNumber": data.get("orderNumber"),
            "status": data.get("status"),
            "universalId": data.get("universalId"),
            "totalAmount": data.get("totalAmount"),
            "vendorName": (data.get("vendorDetails") or {}).get("vendorName"),
        }

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
