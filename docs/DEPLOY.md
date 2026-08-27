# Deploying to EC2

Two repositories, two images, two compose files, and no coupling between them.
The frontend reaches the API through the host, so either can be rebuilt and
restarted without touching the other.

```
browser ──443──► nginx ──► rohrman-web  (Next.js, :3000)
                              │
                              └─ proxies /api/* ──► rohrman-api (FastAPI, :8000)
                                                        │
                                                        ├─► RDS Postgres :5432
                                                        ├─► Tekion, Vertex AI :443
                                                        └─► S3 :443
```

The browser never calls the API directly. Next.js rewrites `/api/*` onward
server-side, which is why the API is published on loopback only.

## One-time setup on the instance

```bash
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user      # log out and back in
```

## Backend

```bash
git clone <backend-repo> && cd rohrman-automation
```

Create `.env`:

```
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@your-rds-endpoint:5432/rohrman_de1
JWT_SECRET=<64+ random characters>

TEKION_USERNAME=...
TEKION_PASSWORD=...
TEKION_TOTP_SECRET=...

AWS_ACCESS_KEY_ID=...          # omit if the instance has an IAM role
AWS_SECRET_ACCESS_KEY=...
S3_BUCKET=rohrman-invoices
AWS_REGION=eu-north-1

SMTP_USER=you@yourdomain.com
SMTP_PASSWORD=<16-char Gmail App Password>

FRONTEND_URL=https://invoices.yourdomain.com
```

Put `neura_vertex_ai.json` beside it — the compose file mounts it read-only
rather than copying it into the image, where it would persist in the layer
history.

```bash
docker compose up -d --build
docker compose logs -f api
```

Migrations run automatically at container start, before uvicorn binds.

## Frontend

```bash
git clone <frontend-repo> && cd rohrman-automotive-DE1-frontend
```

Create `.env.local`:

```
AUTH_SECRET=<32+ random characters, different from JWT_SECRET>
AUTH_URL=https://invoices.yourdomain.com
API_BASE_URL=http://host.docker.internal:8099
```

The two URLs point at different things on purpose.

`AUTH_URL` must be what a **browser** types. next-auth builds callback URLs
from it, so anything internal there redirects people somewhere they cannot
reach.

`API_BASE_URL` is resolved **inside the frontend container**, and a container's
own `localhost` is the container — not the host — so `http://localhost:8099`
finds nothing. `host.docker.internal` is the host as seen from inside a
container, which the compose file makes resolve on Linux via `host-gateway`.

A public URL would work too, but the request would leave the instance and come
back through the internet to reach a process on the same machine.

```bash
docker compose up -d --build
```

## Region

The production database is in **eu-north-1** (Stockholm). Put the EC2 instance
and the S3 bucket in the same region. Cross-region adds latency to every query
and charges for data transfer between them — and this application makes a lot
of small queries per document.

## First administrator

Every account created after the access-control migration defaults to a regular
user, and only an administrator can invite anyone. On a brand new database
nobody can invite the first person.

```bash
docker compose exec api python scripts/seed_user.py \
    --email you@yourdomain.com --password '<strong password>' --role ADMIN
```

On a database that already had users, migration `a91d4f68c205` promoted them,
so this is only needed for a fresh install.

## TLS

Both containers serve plain HTTP. Terminate TLS in front — nginx or Caddy on
the host, or an ALB. Caddy is two lines and renews certificates itself:

```
invoices.yourdomain.com {
    reverse_proxy localhost:3000
}
```

## Do not scale the API

```yaml
deploy:
  replicas: 1
```

That is load-bearing, not boilerplate. `TEKION_LOCK` is a `threading.RLock`,
which serialises Tekion work **within one process**. The Tekion client is a
singleton whose active dealership is mutable state, so a second replica — or
`uvicorn --workers 2` — reintroduces a race that posts invoices to the **wrong
dealership**. Measured before the lock existed: three of five concurrent jobs
went to the wrong store.

For throughput raise `PIPELINE_WORKERS`. Those are threads inside the one
process, where the lock still applies.

It also multiplies memory: OCR renders each page at 300 DPI, roughly 24 MB per
page, and a ten-page invoice holds every page at once. Two workers is
comfortable on a `t3.medium`; raise it only with the memory to match.

Scaling horizontally needs a code change first — `dealer_id` as a per-call
parameter, or a Postgres advisory lock instead of a thread lock.

## Updating

```bash
git pull && docker compose up -d --build
```

The API runs migrations on start, so a schema change needs no separate step.
Deploy the backend first when a release changes both: the frontend tolerates an
API that is briefly ahead of it, not one behind.

## Checks

```bash
docker compose ps                                    # both healthy
docker compose logs --tail=50 api
curl -fsS http://127.0.0.1:8099/docs                 # API up
curl -fsS -o /dev/null -w '%{http_code}\n' localhost:3000/login
```

An API container that keeps restarting is almost always the database: check
`DATABASE_URL`, and that the RDS security group admits the instance's security
group on 5432.
