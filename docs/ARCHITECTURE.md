# Architecture

Invoice automation for Rohrman Automotive Group. An AP clerk uploads an invoice
into a folder; the system reads it, decides what it is, and creates the matching
record in Tekion.

Nineteen dealerships share one Tekion login. Which dealership a record lands in
is an HTTP header, not a separate session.

---

## The shape of it

```
Frontend (Next.js)                Backend (FastAPI)                 Tekion
──────────────────                ─────────────────                 ──────
upload + folder  ──────────────>  documents row (QUEUED)
                                        │
                                  worker claims it
                                        │
                                  OCR (Gemini) ──> vendor, invoice #,
                                        │          date, amount, RO
                                        │
                                  dispatch on folder
                                        ├── SUBLET ───────┐
                                        ├── MISCELLANEOUS ┼──> PO + pre-invoice
                                        ├── STOCK ────────┘
                                        └── OEM ─────────────> journal entry draft
                                        │
poll job  <──────────────────────  PROCESSED / EXCEPTION
```

---

## Tekion access

There is no official API for these flows. Everything was captured from the real
web app with Playwright (`src/playwright/capture*.ts`), analysed into an endpoint
map, and ported by hand into `api/services/tekion_client.py`. The TypeScript in
`/src` is the discovery harness — it does not run in production.

**Login** is three calls, with the MFA code generated in-process from a TOTP
secret, so no human is involved:

```
POST /api/loginservice/p/identity-provider
POST /api/loginservice/p/authenticate/password   -> mfaToken
POST /api/loginservice/p/authenticate/mfa        -> access token + all 19 dealers
```

The session is persisted to `tekion_sessions` (a single row) and reused. A 401
triggers one silent re-login and retry.

**Dealership switching costs nothing.** `switch_dealer()` sets a field; the
request builder derives `dealerid`, `roleid` and `tek-siteid` from it on every
call. One token, nineteen stores.

That design is also its main hazard, and two fixes exist because of it:

- **`TEKION_LOCK`** (`api/services/tekion_lock.py`) serialises all Tekion work.
  Two threads on different dealerships were otherwise retargeting each other —
  a threaded reproduction showed 3 of 5 jobs hitting the wrong dealership.
- **Re-login restores the dealership.** `login()` resets to the account default,
  so a mid-job 401 used to silently move the rest of the job to another store.

---

## The pipeline

`api/services/pipeline_service.py`. One `documents` row tracks a file the whole
way, and that table doubles as the job queue.

**The upload folder is authoritative.** It decides which Tekion flow runs,
regardless of what OCR thinks the document is. OCR's own guess is stored as
`ocr_document_type` so a disagreement is visible, but it never changes routing.

| Folder | Runs | Produces |
|---|---|---|
| `SUBLET` | RO lookup, sublet PO, pre-invoice | PO number |
| `MISCELLANEOUS` | misc PO, LLM-chosen GL, pre-invoice | PO number |
| `STOCK` | vendor stock order (`NON_OEM_STOCK_ORDER`) | PO number, no pre-invoice |
| `OEM` | Parts Manufacture Ticket, journal entry | transaction number, saved as a **draft** |

### The queue

Workers claim rows with the standard Postgres primitive:

```sql
SELECT ... WHERE status = 'QUEUED'
  AND (next_attempt_at IS NULL OR next_attempt_at <= now())
ORDER BY created_at LIMIT 1
FOR UPDATE SKIP LOCKED
```

```
QUEUED --claim--> PROCESSING --ok--> PROCESSED
                      |--- transient failure, attempts < 3 --> QUEUED (30s, 120s backoff)
                      |--- permanent failure ----------------> EXCEPTION
```

Only `OCR_FAILED` and `TEKION_ERROR` retry — a missing invoice number will not
fix itself. A sweeper reclaims rows left `PROCESSING` by a dead worker.

**Concurrency:** OCR runs in parallel across workers (`PIPELINE_WORKERS`, default
2); Tekion is strictly serialised by `TEKION_LOCK`. OCR is the slow part, so
that is where parallelism is worth having.

**Idempotency:** a re-uploaded file (same SHA-256) or the same invoice number for
the same dealership and folder is parked as `DUPLICATE_DOCUMENT` instead of
being posted twice. Checked after OCR, because the invoice number is not known
before that.

---

## OCR

`gemini-2.5-pro` over page images with a response schema. The model picks its own
field names, so the raw output is nested and label-driven:

```json
{ "vendor": { "name": "..." },
  "identifiers": [{ "label": "INVOICE DATE", "value": "05/12/2026" }],
  "totals":      [{ "label": "GRAND TOTAL",  "value": "147.78" }] }
```

`api/services/ocr_helpers.py` flattens that into predictable fields, and the API
returns it as `fields`. Clients should read `fields`, never dig through `result`.

Date extraction deliberately skips due/payment/ship dates — posting to a due date
would put the entry in the wrong period.

---

## GL accounts

Two resolvers, and only one is live.

- **`gl_service_misc.py`** (live) fetches the dealership's real chart of accounts
  from Tekion, caches it in `tekion_gl_accounts`, gives the LLM the account
  *names*, and maps the answer back to a dealership-scoped id (`1708_7473`).
- **`gl_service.py`** is a rules table (SUBLET to 2460, per-dealership stock
  mappings, OEM bulk-oil). It is imported by the routes but **never called** —
  dead code. Sublet's 2460 is string-concatenated inline instead.

The journal entry flow hardcodes 3000 (credit) and 2410 (debit) per the SOP, but
resolves both against the live chart, because the id is dealership-scoped.

---

## Journal entries (OEM)

The Parts Manufacture Ticket SOP needs three values off the ticket: invoice
number, invoice date, amount. Everything else is fixed or derived.

```
POST /api/accounting/u/v2/transaction/dealer/{dealerId}/draft
```

Quirks worth knowing, all captured rather than guessed:

- `amount` is in **dollars**, not cents — the only flow in the codebase that is.
- The credit/debit direction lives in the **sign**; `amountCredited` is `false`
  on both lines.
- The line's Control column maps to the posting's `refId`/`refText` with
  `refType: "VENDOR"`. A bare `MMYY` control does not resolve to a vendor, which
  is why Tekion shows a warning there — it still saves.
- `journalId` is `{dealer}_76`, `documentTypeId` is `{dealer}_document_type_5`.

Submit is deliberately not implemented. Drafts are reversible; posted entries
are not.

---

## Database

One table carries the whole pipeline.

```
documents          the job queue, the audit trail and the results
  identity         file_name, s3_key, file_hash, source_path
  from OCR         vendor_name, invoice_number, ro_number, vin, ocr_document_type
  routing          po_type (the folder), dealership_name
  results          po_number                                      <- SUBLET/MISC/STOCK
                   transaction_id, transaction_number, journal_id  <- OEM
  status           status, exception_type, severity, processed_at
  queue            attempts, locked_at, locked_by, next_attempt_at, last_error
```

| Table | Purpose |
|---|---|
| `vendor_mappings` | dealer + vendor name to Tekion `vendorDisplayId` (387 seeded) |
| `tekion_sessions` | the shared login session, single row |
| `tekion_gl_accounts` | cached chart of accounts per dealership |
| `gl_vendor_mappings` | master vendor-to-GL for misc POs |
| `users`, `refresh_tokens`, `invite_codes` | auth |
| `notifications` | **nothing writes to this yet** |

---

## API

Auth is `Authorization: Bearer <access_token>` on everything except signup,
login and refresh. Access tokens last 15 minutes; refresh tokens rotate.

### The pipeline — what the frontend uses

| | |
|---|---|
| `POST /api/pipeline/process` | multipart `file` + `folder` + `dealership_name`, returns 202 `{documentId}` |
| `GET /api/pipeline/jobs/{id}` | poll until `PROCESSED` or `EXCEPTION` |
| `GET /api/pipeline/queue` | `{queued, processing}` |
| `GET /api/pipeline/folders` | the four valid folders |

Responses here are **camelCase**.

### Tekion

| | |
|---|---|
| `GET /api/tekion/dealers` | all 19 dealerships, cached 24h; `?refresh=true` to bust |
| `POST /api/tekion/po` | create a PO from JSON (no file) — the manual path |
| `GET /api/tekion/vendors/{dealer_id}` | vendor mappings |
| `POST /api/tekion/vendors` | save a mapping (query params, not a body) |
| `POST /api/tekion/gl-accounts/{dealer_id}/refresh` | re-fetch the chart of accounts |

### OCR — the step-by-step path

| | |
|---|---|
| `POST /api/invoices/upload-url` | presigned S3 PUT (needs AWS keys) |
| `POST /api/ocr/extract` | a `file` **or** an `s3_key`, returns `{job_id, document_id}` |
| `GET /api/ocr/jobs/{job_id}` | poll; read `fields` |

Pass the `document_id` to `POST /api/tekion/po` as `documentId` and the PO result
is recorded against the same row.

### Reads

`GET /api/dashboard`, `/api/dashboard/documents`, `/api/dashboard/exceptions`,
`/api/dashboard/exceptions/analytics`, `/api/users`, `/api/notifications`.

Responses here are **snake_case**. That inconsistency with the pipeline routes is
a real wart — check the shape per endpoint rather than writing one shared mapper.

---

## Frontend

Next.js 16 (App Router), next-auth v5, axios, zustand, shadcn.

| | |
|---|---|
| `config/intake-folders.ts` | UI folder to backend flow. The seven-to-four mapping lives here and nowhere else |
| `lib/api/pipeline-service.ts` | upload, poll, dealers, queue |
| `lib/api/dashboard-service.ts` | documents, exceptions, users, and the API-to-table mappers |
| `hooks/use-document-upload.ts` | upload then poll to completion |
| `hooks/use-dashboard-data.ts` | the list hooks; refresh every 10s |
| `lib/stores/dealership-store.ts` | the one selected dealership, persisted |

Three folders — OEM Special Order, Vendor Special Order, Vendor Credit PO — have
no backend flow and show a disabled uploader rather than accepting a file that
would go nowhere.

**Dealership is chosen once, in the header.** Names come from
`GET /api/tekion/dealers` and are sent verbatim; there is no local fallback list
because four stores contain "Toyota" and an approximate name can post to the
wrong books.

**Auth:** next-auth's `jwt` callback is the single refresh authority. The axios
interceptor never refreshes — on a 401 it asks next-auth for the session. Two
refreshers spending the same rotating token was what killed sessions fifteen
minutes after login.

**Calls the browser makes:** `/api/tekion/dealers` on mount,
`/api/pipeline/process` on upload, `/api/pipeline/jobs/{id}` every 2.5s while a
job runs, `/api/pipeline/queue` and the dashboard reads every 10s. All go to
same-origin paths that `next.config.ts` rewrites to the backend.

---

## Known gaps

- **`notifications` is never written** — the endpoint returns an empty list.
- **`resolve_gl` is dead code** — the whole rules table is unused.
- **STOCK has no `brandCode` source.** Tekion builds `partId` as
  `M_{brand}_{part}` and OCR has no brand field, so stock uploads will likely fail.
- **AP approval is unfinished.** `api/services/ap_approval.py` automates the
  approval SOP but its final Post Transaction call was never captured, so the
  flow stops at pre-invoice and a clerk approves in Tekion.
- **JE accounting dates are sent as midnight UTC.** If Tekion renders them in
  dealership-local time an entry could land a day early. Unverified.
- **`TEKION_LOCK` is per-process.** With multiple uvicorn workers each gets its
  own. Run one web process, or set `PIPELINE_WORKERS=0` there and run one
  dedicated worker process.
- **OCR job results live in memory** and are lost on restart (the standalone
  `/api/ocr/*` path only; the pipeline persists to the database).
