# rohrman-automation

Automation for entering data into Tekion. The project works
in two complementary ways:

- **APC API** — talks directly to Tekion's Automotive Partner Cloud REST API for
  everything Tekion exposes officially (customers, leads, vehicle inventory,
  deals, document upload, journal entries).
- **Playwright** — drives the Tekion web app for workflows handled through the
  UI (Parts Purchase Orders, Accounts Payable vendor invoices, invoice document
  attachment).

Both paths authenticate automatically and refresh their credentials within the
24-hour window, so you set up once and run commands without re-authenticating
during normal work.

---

## Branches

| Branch | Contents |
|--------|----------|
| `main` | This README only. |
| `automation` | All APC + Playwright code (this is where development happens). |
| `ocr` | Reserved for the OCR / invoice-reading work (intentionally empty for now). |

---

## Setup

```bash
git clone https://github.com/saadurrehman97/rohrman-automation.git
cd rohrman-automation
git checkout automation

npm install
npx playwright install chromium     # only needed for the Playwright path
cp .env.example .env                # then fill in your values (see below)
```

### Environment variables (`.env`)

| Variable | Used by | What it is |
|----------|---------|------------|
| `APC_BASE_URL` | APC | `https://api-sandbox.tekioncloud.com/openapi` (sandbox) or the production base URL |
| `APC_APP_ID` | APC | Application ID from APC → My Applications |
| `APC_SECRET_KEY` | APC | Secret key from APC → Generate App Credentials |
| `APC_DEALER_ID` | APC | Dealership ID from APC → Dealer Dashboard |
| `TEKION_USERNAME` | Playwright | Tekion login username |
| `TEKION_PASSWORD` | Playwright | Tekion login password |
| `TEKION_TOTP_SECRET` | Playwright | Authenticator seed used to generate the login verification code |
| `US_PROXY` | Playwright | Optional US proxy (the app is US-only) |

---

## Authentication

### APC Bearer token (24h, automatic)

The APC API uses a Bearer token obtained by exchanging `APC_APP_ID` +
`APC_SECRET_KEY`. The token is valid for 24 hours. The project mints it once,
caches it in `.tekion-token.json`, and reuses it across every command until it
nears expiry — then it refreshes automatically.

```bash
npm run token        # mint or reuse the 24h token
```

### Tekion login + verification code (Playwright)

The Playwright path logs in with username, password, and a 6-digit verification
code generated from `TEKION_TOTP_SECRET`. The login session is saved
(`tekion-auth.json`) and reused, so the code is only needed when the session
expires.

```bash
npm run otp          # print the current verification code
```

---

## APC API

### Core client

| File | Task |
|------|------|
| `src/apc.ts` | The API client. Builds every request with the required headers (`app_id`, `Authorization`, `dealer_id`, `Content-Type`) and contains the token manager that mints, caches, and refreshes the 24h Bearer token. |
| `src/apcToken.ts` | The `token` command — mint or reuse the 24h token and report how long it is valid. |
| `src/print.ts` | Formats every result into clean, structured output (status, key fields, full data). |

### API definitions (one file per Tekion area)

| File | Tekion area | Capabilities |
|------|-------------|--------------|
| `src/openapi/customers.ts` | Customer | Create and update customers; insurance policy |
| `src/openapi/leads.ts` | CRM Leads | Create/update leads, contacts, vehicles, trade-ins, notes, assignees; convert lead to deal |
| `src/openapi/vehicleInventory.ts` | Vehicle Inventory | List/search/create/update/delete vehicles; accessories, fees, discounts, offers, damages, costs, media; bulk upsert |
| `src/openapi/deals.ts` | Deals | Read deal information (e.g. a deal accessory) |
| `src/openapi/employees.ts` | Employee | Look up users/employees |
| `src/openapi/serviceAppointments.ts` | Service Appointments | Check availability slots |
| `src/openapi/support.ts` | Support | Reference lookups (op codes, departments) |
| `src/openapi/financeAndInsurance.ts` | F&I | Look up matching F&I products |
| `src/openapi/index.ts` | (all) | Re-exports every area from one place |

### Running an API action — where you enter data

Each action has a small runner under `cli/`. Open the runner, fill in the
details inside the clearly-marked block at the top, and run the command. Records
that already exist are referenced by their ID passed on the command line.

| Command | Action | What you supply |
|---------|--------|-----------------|
| `npm run token` | Mint/reuse the 24h token | Nothing |
| `npm run customer:create` | Create a customer | Customer details in the edit block |
| `npm run customer:update -- <id>` | Update a customer | Customer details + the customer ID |
| `npm run vehicles:list` | List vehicles | Search filters in the edit block |
| `npm run deal:accessory -- <dealId> <accId>` | Get a deal accessory | The two IDs |
| `npm run lead:update -- <leadId>` | Update a lead's source | Lead source details + the lead ID |

| File | Task |
|------|------|
| `cli/createCustomer.ts` | Enter customer info and create |
| `cli/updateCustomer.ts` | Enter changed fields and update by ID |
| `cli/listVehicles.ts` | Enter search filters and list inventory |
| `cli/getDealAccessory.ts` | Fetch one accessory in a deal by IDs |
| `cli/updateLead.ts` | Enter lead source info and update by ID |
| `cli/_print.ts` | Shared result formatter |

### Larger batch jobs

| File | Task |
|------|------|
| `examples/vehicleBulkImport.ts` | Bulk-import vehicles from a list |
| `examples/leadIntake.ts` | Full lead intake (lead + contact + vehicle + note) |
| `examples/customerSync.ts` | Create/update customers from a JSON file |

---

## Playwright

| File | Task |
|------|------|
| `src/playwright/tekionPo.ts` | Full browser workflow: log in with the verification code, open Parts → Purchase Order, create an OEM or Miscellaneous order, submit it to the pre-invoice stage, and attach the invoice document. Order details are entered in a marked block; the saved session avoids repeat logins. |
| `src/totp.ts` | Generates the current verification code from the authenticator seed. |

Run:

```bash
npx tsx src/playwright/tekionPo.ts
```

---

## Project layout

```
src/
  apc.ts                 API client + 24h token manager
  apcToken.ts            token command
  print.ts               structured output
  totp.ts                verification-code generator
  openapi/               one file per Tekion API area
  playwright/
    tekionPo.ts          browser workflow (PO / AP / document upload)
cli/                     per-action runners (where you enter data)
examples/                batch jobs
.env.example             configuration template
```

---

## Notes

- The app is US-only; use a US connection (or `US_PROXY`) for the Playwright path.
- Secret files (`.env`, `.tekion-token.json`, `tekion-auth.json`) are never
  committed — see `.gitignore`.
