# rohrman-automation

Invoice automation for Rohrman Automotive Group. An AP clerk uploads an invoice
into a folder; the system reads it with OCR and creates the matching record in
Tekion — a purchase order and pre-invoice, or a journal entry.

Nineteen dealerships, one Tekion login.

| | |
|---|---|
| **[docs/RUNNING.md](docs/RUNNING.md)** | set up the database, run both servers, test a document, reset between runs |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | how it works, the API surface, and the known gaps |

The frontend lives in a separate repo: `rohrman-automotive-DE1-frontend`.

---

## Quick start

```bash
# database
psql -U postgres -c "CREATE ROLE rohrman LOGIN PASSWORD 'rohrman';"
psql -U postgres -c "CREATE DATABASE rohrman OWNER rohrman;"
uv run alembic upgrade head

# a login (there is no open registration)
uv run python scripts/seed_user.py        # test@ccript.com / testpass1234

# the API
uv run uvicorn api.main:app --reload --port 8000
```

`.env` needs `TEKION_USERNAME`, `TEKION_PASSWORD`, `TEKION_TOTP_SECRET`,
`DATABASE_URL` and `JWT_SECRET`. See [docs/RUNNING.md](docs/RUNNING.md) for the
full list and what is optional.

---

## What runs where

```
POST /api/pipeline/process   upload an invoice into a folder
GET  /api/pipeline/jobs/{id} poll until it finishes
```

The upload folder decides the flow:

| Folder | Result in Tekion |
|---|---|
| `SUBLET` | purchase order + pre-invoice |
| `MISCELLANEOUS` | purchase order + pre-invoice |
| `STOCK` | vendor stock order, ready to receive |
| `OEM` | journal entry, saved as a **draft** |

Work is queued in the `documents` table and drained by background workers, so
several uploads at once are safe — they wait their turn rather than being
rejected. OCR runs in parallel; Tekion is talked to one conversation at a time.

---

## Layout

```
api/
  routes/          FastAPI endpoints (pipeline, tekion, ocr, dashboard, auth, users)
  services/
    tekion_client.py     the Tekion API client — login, POs, pre-invoice, GL
    pipeline_service.py  OCR then dispatch to the right flow
    je_creation.py       Parts Manufacture Ticket -> journal entry
    ap_approval.py       the AP approval SOP (unfinished — see ARCHITECTURE)
    job_queue.py         SELECT ... FOR UPDATE SKIP LOCKED
    worker.py            the worker pool
  models/          SQLModel tables + Pydantic schemas
main_pipeline/     the OCR extraction (Gemini)
scripts/           seed a user, reset test data, dry-run the AP and JE flows
src/               TypeScript capture harness — how the Tekion endpoints were
                   discovered. Does not run in production.
alembic/           migrations
```

---

## Scripts

```bash
uv run python scripts/seed_user.py                   # create/update a login
uv run python scripts/reset_test_data.py --dry-run   # what a reset would clear
uv run python scripts/reset_test_data.py             # clear documents to test again
uv run python scripts/je_creation_dryrun.py          # journal entry, read-only
uv run python scripts/ap_approval_dryrun.py          # AP approval, read-only
```

`reset_test_data.py` clears **our** records only. Anything already sent to
Tekion stays there and has to be voided in Tekion.

---

## Notes

- Tekion has no official API for these flows. The endpoints were captured from
  the live web app with Playwright and ported by hand; `npm run pw:capture:je`
  and `npm run pw:capture:ap` are how that is done.
- Every upload is real. OEM produces a reversible draft; sublet and misc create
  a purchase order **and** a pre-invoice.
- The app is US-only — use a US connection or set `US_PROXY` for the capture
  scripts.
- Secrets (`.env`, `tekion-auth.json`, `captured/`) are gitignored.
