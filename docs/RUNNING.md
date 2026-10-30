# Running the system

Two repositories, one database, one Tekion login.

| | Repo | Port |
|---|---|---|
| Backend (FastAPI) | `rohrman-automation` | 8000 |
| Frontend (Next.js) | `rohrman-automotive-DE1-frontend` | 3000 |
| Database | PostgreSQL | 5432 |

---

## 1. Database

Two options. Pick one.

### Option A — a Postgres you already have

```bash
psql -U postgres -c "CREATE ROLE rohrman LOGIN PASSWORD 'rohrman';"
psql -U postgres -c "CREATE DATABASE rohrman OWNER rohrman;"
```

### Option B — Docker

`docker-compose.yml` already defines it:

```bash
docker compose up -d db
```

If port 5432 is already taken by a native Postgres service, either stop that
service or remap the container to `5433:5432` and change `DATABASE_URL` to match.

### Apply the schema

```bash
uv run alembic upgrade head
uv run alembic current        # should print the newest revision
```

This also seeds 387 vendor mappings and the GL vendor table — they arrive as
part of the migrations, not from a separate seed step.

---

## 2. Backend

`.env` in the backend repo:

```ini
# Tekion login (required — the automation cannot run without these)
TEKION_USERNAME=
TEKION_PASSWORD=
TEKION_TOTP_SECRET=          # authenticator seed; the 6-digit code is generated in-process

DATABASE_URL=postgresql+psycopg://rohrman:rohrman@localhost:5432/rohrman
JWT_SECRET=                  # python -c "import secrets; print(secrets.token_urlsafe(48))"

# Optional. Without these, uploaded invoices are not archived — see "S3" below.
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=us-east-2
S3_BUCKET=rohrman-invoices

# Optional. Queue workers per process; 0 disables in-process workers.
PIPELINE_WORKERS=2
```

Run it:

```bash
uv run uvicorn api.main:app --reload --port 8000
```

Check: <http://localhost:8000/health> and <http://localhost:8000/docs>.

On start you should see the worker pool come up:

```
[WORKER] w0-… started
[WORKER] started 2 worker(s), polling every 2.0s
```

---

## 3. Create a login

The API has no open registration — signup expects an invite code — so seed the
first account directly:

```bash
uv run python scripts/seed_user.py
# → test@ccript.com / testpass1234
```

Custom:

```bash
uv run python scripts/seed_user.py \
  --email you@ccript.com --password something-long --name "Your Name" \
  --role ADMIN --superuser
```

Re-running for the same email resets that user's password rather than failing.

---

## 4. Frontend

`.env.local` in the frontend repo:

```ini
AUTH_SECRET=                 # npx auth secret
API_BASE_URL="http://127.0.0.1:8000"
```

`API_BASE_URL` is deliberately **not** `NEXT_PUBLIC_`. The browser never calls
the backend directly — `next.config.ts` rewrites same-origin `/api/…` paths to
it, which keeps the backend host out of the bundle and avoids needing CORS.

```bash
npm install
npm run dev
```

Open <http://localhost:3000/login>.

---

## 5. Test a document end to end

1. Sign in with the seeded account.
2. **Document Intake** → **OEM Stock Order**.
3. Confirm the header dealership picker lists real Tekion stores. It defaults to
   the first; change it if needed — the upload posts to whatever is selected there.
4. Drop an invoice, click **Upload**.
5. Watch: `Waiting in the queue…` → `Reading the invoice…` → a Tekion reference.

Expect roughly 30–60 seconds; most of it is OCR.

**Every upload is real.** OEM creates a journal entry *draft* in Tekion
(reversible in the UI). Sublet and Miscellaneous create a purchase order **and**
a pre-invoice, which are harder to undo. Start with OEM.

### Watching the queue

The bar above the uploader shows waiting/in-progress counts. Upload several
files back to back — clicking "Close and keep processing" each time — and the
waiting count climbs past two while in-progress holds at two. Nothing is
rejected for being concurrent.

---

## 6. Reset between test runs

```bash
uv run python scripts/reset_test_data.py --dry-run   # see what would go
uv run python scripts/reset_test_data.py             # documents + notifications
uv run python scripts/reset_test_data.py --all       # also GL cache + Tekion session
```

Users, vendor mappings and the GL vendor table are never touched.

**Two things to know before you use it:**

- It clears *our* records, not Tekion's. Purchase orders, pre-invoices and
  journal entries already sent stay in Tekion and must be voided there. The
  dry run lists which ones reached Tekion.
- Re-uploading the same invoice after a reset **will post to Tekion again**.
  The duplicate check reads the table this script empties.

---

## Troubleshooting

**Everything returns 401 shortly after signing in.**
Sign out and in again. If it persists, the refresh token was revoked — that is
recoverable only with a fresh login.

**The dealership picker says "Dealerships unavailable".**
The backend could not reach Tekion. Check `TEKION_*` in `.env` and the backend
log for a login failure. There is no fallback list on purpose: an approximate
name can match the wrong store out of nineteen.

**A document sits at PROCESSING forever.**
Check the backend log. A worker that dies mid-job leaves the row locked; the
sweeper reclaims it after 30 minutes. `PIPELINE_WORKERS=0` means no workers are
running in that process at all.

**`alembic upgrade head` fails with "Multiple head revisions".**
Two migrations claim the same parent, usually after a merge. Point the newer
one's `down_revision` at the other and re-run.

**The document detail page shows "Document not available".**
Expected without S3. The backend keeps no retrievable copy of the upload.

---

## S3

Optional, and everything works without it. What you lose:

- No archive of the invoice — the file is deleted once processing finishes.
- A restart while documents are queued fails those documents
  (`SOURCE_FILE_MISSING`) instead of resuming, because the fallback re-download
  has nothing to pull from.
- The detail page cannot show the invoice.
- `POST /api/invoices/upload-url` returns 500.

Add the four AWS keys and restart to enable it.
