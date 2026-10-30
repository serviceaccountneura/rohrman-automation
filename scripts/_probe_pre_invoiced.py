from __future__ import annotations
import json, sys
sys.path.insert(0, ".")
from sqlmodel import Session
from api.db import engine
from api.routes.tekion import get_client, reset_client
from api.services.ap_approval import ApApprovalService
from api.services.tekion_lock import tekion_scope
from api.services.vi_verify import ViVerifyInput, verify_vendor_invoice

def main():
    dealer = sys.argv[1] if len(sys.argv) > 1 else "Schaumburg Honda"
    reset_client()
    with Session(engine) as session:
        with tekion_scope():
            client = get_client(session)
            client.switch_dealer(client.find_dealer_by_name(dealer))
            svc = ApApprovalService(client)
            hits = svc.search_pre_invoiced(rows=10)
            for h in hits[:8]:
                amt = h.get("invoiceAmount")
                dollars = (amt.get("amount") or 0) / 100.0 if isinstance(amt, dict) else amt
                po = (h.get("poDetails") or [{}])[0]
                print(json.dumps({"id": h.get("id"), "invoiceNumber": h.get("invoiceNumber"), "amount": dollars, "poNum": po.get("poNum"), "payee": h.get("payeeName")}, default=str))

if __name__ == "__main__":
    main()
