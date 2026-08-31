# Production runbook

For whoever operates this on the EC2 box — human or agent. `DEPLOY.md` covers
first-time setup; this covers running it once it is up, and the things that have
actually gone wrong.

Read "Hard rules" before touching anything. Everything else is reference.

---

## What this system does

Invoices arrive by upload. Gemini reads each page and returns structured JSON. A
worker thread then posts the result into **Tekion**, the dealer management
system, as one of two flows:

| Flow | What it creates in Tekion |
|---|---|
| **VSO** (vendor stock order) | a pre-invoice against an existing purchase order |
| **OEM** | a journal entry, one debit line per part on the invoice |

Both write to a live financial system for 19 real dealerships. **Nothing this
system posts can be undone from here** — a wrong pre-invoice or journal entry has
to be voided by a person inside Tekion.

That single fact should govern every judgement call below.

---

## Hard rules

**1. Never run more than one API process.**

```yaml
deploy:
  replicas: 1
```

That is load-bearing. `TEKION_LOCK` is a `threading.RLock`, so it serialises
Tekion work **inside one process only**. The Tekion client is a singleton whose
active dealership is mutable state. A second replica — or `uvicorn --workers 2` —
posts invoices to the **wrong dealership**. This is measured, not theoretical:
before the lock existed, three of five concurrent jobs went to the wrong store.

For throughput raise `PIPELINE_WORKERS` (threads inside the one process, where
the lock still applies). Scaling out needs a code change first: `dealer_id` as a
per-call parameter, or a Postgres advisory lock.

**2. Never run `scripts/reset_test_data.py` against production.**

It clears the `documents` table — which is also the duplicate check. Re-uploading
anything afterwards posts to Tekion a **second time**, and Tekion keeps the first
one. The script has no idea which database it is pointed at; only `DATABASE_URL`
decides.

**3. Never send test email to a real address.**

SMTP is a live Gmail account. Test invites have reached real inboxes before. To
exercise the invite flow, read the generated row out of the `invite_codes` table
instead of sending, and delete it afterwards.

**4. Do not upload a real invoice to "check if it works".**

There is no dry-run at the HTTP layer. An upload that succeeds has already
written to Tekion. The `scripts/*_dryrun.py` scripts exist for this.

**5. Restart the API after changing backend code.** `--reload` has silently
failed here — WatchFiles printed "Reloading..." and never started a new process,
because worker threads blocked it. Under Docker, always `up -d --build`.

---

## Layout

```
browser ──443──► nginx/Caddy ──► rohrman-web  (Next.js, :3000)
                                      │
                                      └─ proxies /api/* ──► rohrman-api (:8000)
                                                                │
                                                                ├─► RDS Postgres :5432
                                                                ├─► Tekion, Vertex AI :443
                                                                └─► S3 :443
```

Two repositories, two compose files, **no shared Docker network**. The frontend
reaches the API through `host.docker.internal:8099`, so either side can be
rebuilt without the other.

The API is published on **loopback only** (`127.0.0.1:8099`). It has no reverse
proxy of its own and no rate limiting; the frontend is the only thing that should
ever call it.

| | Container | Host port | Public? |
|---|---|---|---|
| Frontend | `rohrman-web` | `3000` | yes, behind TLS |
| Backend | `rohrman-api` | `127.0.0.1:8099` | **no** |
| Database | RDS | `5432` | no — security group only |

---

## Deploying an update

```bash
# Backend FIRST when a release changes both. The frontend tolerates an API that
# is briefly ahead of it, not one behind.
cd ~/rohrman-automation
git pull && docker compose up -d --build
docker compose logs -f api          # watch for "Application startup complete"

cd ~/rohrman-automotive-DE1-frontend
git pull && docker compose up -d --build
```

Migrations run automatically at container start, before uvicorn binds — there is
no separate `alembic upgrade` step. Current head is `a91d4f68c205`.

**Check the queue before deploying.** A restart kills in-flight jobs, and a
document that was mid-post to Tekion may already have written there:

```bash
docker compose exec api python -c "
from api.database import engine
from sqlmodel import Session, text
with Session(engine) as s:
    for row in s.exec(text('select status, count(*) from documents group by status')):
        print(row)
"
```

Anything in `PROCESSING` is actively running. Wait for it.

---

## Health checks

```bash
docker compose ps                                     # both should say (healthy)
curl -fsS http://127.0.0.1:8099/docs                  # API up
curl -fsS -o /dev/null -w '%{http_code}\n' localhost:3000/login   # expect 200
docker compose logs --tail=50 api
```

---

## When something is broken

**API container restarting in a loop** — almost always the database. Check
`DATABASE_URL` (it needs the `+psycopg` driver; plain `postgresql://` selects
psycopg2, which is not installed and fails with a driver error that points
nowhere useful), and that the RDS security group admits this instance's security
group on 5432.

**Documents stuck in `PROCESSING` forever** — the worker died mid-job. The row is
not automatically requeued, because requeueing something that may already have
posted to Tekion is worse than leaving it stuck. Check Tekion for the invoice
before deciding; if it did not post, set the row back to `PENDING` by hand.

**"not_in_mapping" on a vendor** — the invoice's vendor name has no row in
`VendorMapping` (note: **not** `GlVendorMapping`, which is a different table and
the wrong place to fix this). Add an alias row for the name exactly as it appears
on the invoice.

**A flow refuses with a line-item discrepancy** — that is deliberate. The OEM
journal entry refuses to post when the parts do not sum to the invoice total, and
refuses when no line items were found at all. Do not "fix" it by falling back to
a summary line; the refusal is the feature. Flag it for a clerk.

**Disk full** — this has corrupted a source file before (`dashboard.py` became
10,067 null bytes). Container logs are capped at 10 MB × 3 per service, but
uploads under `/tmp/rohrman` are not. Check `df -h` and the `api-uploads` volume.

**Memory** — PDF rendering is the hog. OCR rasterises each page at 300 DPI,
roughly 24 MB per page, and a ten-page invoice holds every page at once.
`PIPELINE_WORKERS` multiplies that. Two is comfortable on a `t3.medium`.

---

## Credentials

Nothing long-lived should sit on the box.

**S3** — leave `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` **empty** and
attach an IAM role to the instance. boto3 then reads temporary, self-rotating
credentials from the instance metadata service. `_get_s3_client()` builds its
kwargs conditionally for exactly this reason: passing `aws_access_key_id=None`
explicitly *suppresses* the fallback chain, whereas omitting it allows it.

Archiving is switched on by `S3_BUCKET` alone. If it is empty, uploads live only
on the container's disk — and every deploy restarts that container.

The role needs, on that bucket only:

```
s3:PutObject, s3:GetObject, s3:DeleteObject   arn:aws:s3:::BUCKET/*
s3:ListBucket                                 arn:aws:s3:::BUCKET
```

**Vertex AI** — `neura_vertex_ai.json` is mounted read-only from the host, never
copied into the image, where it would persist in the layer history.

**Secrets are not in git.** `.env` and `.env.*` are gitignored. To find out what
a variable is for, read `.env.example`, which is committed and annotated.

---

## First administrator

Only an admin can invite anyone, so a fresh database has nobody who can start.

```bash
docker compose exec api python scripts/seed_user.py \
    --email you@yourdomain.com --password '<strong password>' --role ADMIN
```

On a database that already had users, migration `a91d4f68c205` promoted them.

Roles are `ADMIN` and `AP_CLERK`. A clerk cannot invite, delete, see other users,
or change anyone's status — only edit their own details. Dealership access is
enforced **server-side** in `api/services/access.py`; the frontend only hides UI.

One deliberate asymmetry there: an empty dealership *string* means "all 19", but
an empty dealership *array* is refused rather than treated as all. A form bug
should not silently grant access to every store.

---

## Not yet verified in production

Be honest about this list rather than assuming it works.

- **The images have never been built.** The Docker daemon was down throughout
  development. The first `up -d --build` on the instance is the first real test
  of both Dockerfiles.
- **The itemised OEM journal entry has never posted to live Tekion** with more
  than two postings. The multi-debit-line shape is exercised only by dry-runs.
- **Batch splitting** (one scan containing several invoices) has been tested on
  synthetic files only, never on a real batch scan from a dealership.
- **S3 archiving has never run.** No bucket has existed and no credentials have
  ever been configured.

Treat the first run of each of these as a test, with someone watching Tekion.
