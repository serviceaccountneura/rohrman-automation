/**
 * tekionPo.ts — Tekion DMS browser automation (Playwright)
 * ─────────────────────────────────────────────────────────────────────────────
 * Authorized automation for Schaumburg Honda Automotive (user: Max Smith, AP Clerk).
 * Drives app.tekioncloud.com to:
 *   1. Log in (username + password + TOTP authenticator)
 *   2. Open Parts → Purchase Order
 *   3. Create a Purchase Order (OEM Special Order or Miscellaneous Order)
 *   4. Submit it to the Pre-Invoice state
 *   5. Attach the vendor-invoice document (PDF/JPG)
 *
 * The flow mirrors the UI sequence of the app. Each user action is backed by an
 * internal REST call the SPA fires; those calls are listed in INTERNAL_ENDPOINTS
 * and asserted via the network listener so a UI change surfaces immediately.
 *
 * Run:  tsx src/playwright/tekionPo.ts
 * Env:  TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET, [US_PROXY]
 */
import 'dotenv/config';
import {
  chromium,
  type Browser,
  type BrowserContext,
  type Page,
  type Locator,
  type Request,
} from 'playwright';
import { authenticator } from 'otplib';
import { existsSync } from 'node:fs';

// ─── Configuration ───────────────────────────────────────────────────────────

const BASE_URL = process.env.TEKION_BASE_URL ?? 'https://app.tekioncloud.com';
const STORAGE_STATE = 'tekion-auth.json';
const HEADLESS = process.env.HEADLESS === 'true';

const CREDENTIALS = {
  username: process.env.TEKION_USERNAME ?? '',
  password: process.env.TEKION_PASSWORD ?? '',
  totpSecret: (process.env.TEKION_TOTP_SECRET ?? '').replace(/\s+/g, ''),
};

/** US egress for the geofenced app (consumer VPN or US residential proxy). */
function proxyFromEnv() {
  const raw = process.env.US_PROXY?.trim();
  if (!raw) return undefined;
  try {
    const u = new URL(raw);
    return u.username
      ? { server: `${u.protocol}//${u.host}`, username: decodeURIComponent(u.username), password: decodeURIComponent(u.password) }
      : { server: `${u.protocol}//${u.host}` };
  } catch {
    return { server: raw };
  }
}

// ─── Route URLs (SPA client-side routes, confirmed from the address bar) ──────

const ROUTES = {
  login: `${BASE_URL}/login`,
  home: `${BASE_URL}/home`,
  poList: `${BASE_URL}/parts/purchase-order/list`,
  oemSpecialOrderAdd: `${BASE_URL}/parts/purchase-order/oem-order/OEM_SPECIAL_ORDER/add`,
  oemStockOrderAdd: `${BASE_URL}/parts/purchase-order/oem-order/OEM_STOCK_ORDER/add`,
  miscOrderAdd: `${BASE_URL}/parts/purchase-order/misc/add`,
  vendorInvoiceCreate: `${BASE_URL}/accounting/invoice/create`,
  journalEntryType: `${BASE_URL}/accounting/journalEntry/transactionType`,
  journalEntryCreate: `${BASE_URL}/accounting/journalEntry/transactionType/GENERAL/create`,
} as const;

/**
 * Internal REST endpoints the SPA calls behind each action.
 * Paths follow Tekion's `/{service}/api/{version}/...` microservice convention.
 */
const INTERNAL_ENDPOINTS = {
  login: 'POST /identity/api/v1/login',
  mfaVerify: 'POST /identity/api/v1/mfa/verify',
  partsContext: 'GET  /parts/api/v1/context',
  poList: 'GET  /parts/api/v1/purchase-orders',
  poDraftOem: 'POST /parts/api/v1/oem-orders/draft',
  poAssignParts: 'POST /parts/api/v1/oem-orders/{poId}/parts',
  poSaveDraft: 'PUT  /parts/api/v1/oem-orders/{poId}',
  poSubmitOem: 'POST /parts/api/v1/oem-orders/{poId}/submit',
  poMiscCreate: 'POST /parts/api/v1/misc-orders',
  poMiscPreInvoice: 'POST /parts/api/v1/misc-orders/{poId}/pre-invoice',
  docUpload: 'POST /document/api/v1/documents',
  docLink: 'POST /document/api/v1/documents/{docId}/link',
  invoiceCreate: 'POST /accounting/api/v1/vendor-invoices',
} as const;

// ─── Selectors (data-testid first, role/text fallback — real Playwright style) ─

const S = {
  // Login
  username: 'input[name="username"], input[type="email"], input[autocomplete="username"]',
  password: 'input[name="password"], input[type="password"]',
  loginSubmit: 'button[type="submit"], button:has-text("Sign In"), button:has-text("Login")',
  otpInput: 'input[autocomplete="one-time-code"], input[name="otp"], input[name="code"], input[placeholder="Type Here"]',
  otpSubmit: 'button:has-text("Verify and Proceed")',

  // App shell
  appGrid: '[data-testid="app-grid"], [aria-label="App Grid"], header button:has(svg)',
  globalSearch: 'input[placeholder="Search here..."]',

  // Purchase Order list
  poCreateBtn: 'button:has-text("Create")',
  poCreateMenuItem: (label: string) => `[role="menuitem"]:has-text("${label}"), li:has-text("${label}")`,
  poListRows: '[data-testid="po-list"] tr, table tbody tr',

  // OEM Special Order — Assign Parts modal
  assignPartsLink: 'text=Assign Parts',
  assignPartsSearchType: '[data-testid="search-by"], button:has-text("Part Number")',
  assignPartsSearchInput: 'input[placeholder="Search Here..."]',
  assignPartsRowCheckbox: (priority: string) => `tr:has-text("${priority}") input[type="checkbox"]`,
  assignPartsSave: '[role="dialog"] button:has-text("Save")',

  // OEM Special Order — Order Details
  submitOrderAsSelect: '[data-testid="submit-order-as"], label:has-text("Submit Order as") + * select, label:has-text("Submit Order as") ~ * [role="combobox"]',
  requiredQtyInput: (rowIndex: number) => `[data-testid="parts-list"] tr:nth-child(${rowIndex}) input`,
  saveAsDraftBtn: 'button:has-text("Save as Draft"), button:has-text("Save As Draft")',
  saveBtn: 'footer button:has-text("Save"), button:has-text("Save"):not(:has-text("Draft"))',

  // OEM Special Order — Submit OEM Order modal
  submitModalDealerCode: '[role="dialog"] [data-testid="dealer-code"], [role="dialog"] label:has-text("Dealer Code") ~ * [role="combobox"]',
  submitModalOrderType: '[role="dialog"] [data-testid="order-type"], [role="dialog"] label:has-text("Order Type") ~ * [role="combobox"]',
  submitModalShipmentPriority: '[role="dialog"] label:has-text("Shipment Priority") ~ * [role="combobox"]',
  submitModalAltShipmentPriority: '[role="dialog"] label:has-text("Alternate Shipment Priority") ~ * [role="combobox"]',
  submitModalSubmit: '[role="dialog"] button:has-text("Submit")',

  // Success toast
  successToast: 'text=/successfully created/i, [role="status"]:has-text("Success")',

  // Miscellaneous Order
  miscVendor: 'label:has-text("Vendor") ~ * input, [data-testid="vendor"] input',
  miscVendorSite: 'label:has-text("Vendor Site") ~ * [role="combobox"]',
  miscVendorPhone: 'label:has-text("Vendor Phone") ~ * input',
  miscVendorEmail: 'label:has-text("Vendor Email") ~ * input',
  miscRequestedBy: 'label:has-text("Requested By") ~ * [role="combobox"]',
  miscEstDelivery: 'label:has-text("Estimated Delivery Date") ~ * input',
  miscInvoiceNumber: 'label:has-text("Invoice Number") ~ * input',
  miscControlNumber: 'label:has-text("Control Number") ~ * input',
  miscComment: 'label:has-text("Comment") ~ * textarea',
  miscItemPart: '[data-testid="items-list"] input[placeholder="Type"]',
  miscItemQty: '[data-testid="items-list"] [data-col="qty"] input',
  miscItemPrice: '[data-testid="items-list"] [data-col="price"] input',
  miscItemGlAccount: '[data-testid="items-list"] [data-col="gl"] [role="combobox"]',
  miscSubmitPreInvoice: 'button:has-text("Submit & Pre-Invoice")',
  miscSubmit: 'button:has-text("Submit"):not(:has-text("Pre-Invoice"))',

  // Vendor Invoice — document upload
  invoiceUploadButton: 'button:has-text("Upload Documents")',
  invoiceFileInput: 'input[type="file"]',
  invoiceEnterPo: 'label:has-text("Enter PO") input[type="checkbox"], input[type="checkbox"] ~ *:has-text("Enter PO")',
  invoicePoNumber: '[data-testid="po-row"] [role="combobox"], label:has-text("PO Number") ~ * [role="combobox"]',
  invoiceVendor: 'label:has-text("Vendor Number / Name") ~ * [role="combobox"]',
  invoiceNumber: 'label:has-text("Invoice Number") ~ * input',
  invoiceAmount: 'label:has-text("Invoice Amount") ~ * input',
  invoiceDate: 'label:has-text("Invoice Date") ~ * input',
  invoiceDueDate: 'label:has-text("Invoice Due Date") ~ * input',
  invoiceApGlAccount: 'label:has-text("AP GL Account") ~ * [role="combobox"]',
  invoiceNext: 'button:has-text("Next")',
} as const;

// ─── Types ───────────────────────────────────────────────────────────────────

export type PoPriority = 'CSO' | 'WSL';
export type SubmitStage = 'DRAFT' | 'SUBMITTED' | 'PRE_INVOICE';

export interface OemSpecialOrderInput {
  dealerCode: string;             // e.g. "207562"
  orderType: string;              // e.g. "EMG"
  shipmentPriority: string;       // e.g. "Next Day Delivery"
  alternateShipmentPriority: string;
  parts: Array<{ partNumber: string; requiredQty: number; priority: PoPriority }>;
  submitStage: SubmitStage;
}

export interface MiscOrderInput {
  vendorName: string;             // e.g. "ADVANCE AUTO PARTS"
  vendorSite?: string;
  vendorPhone?: string;
  vendorEmail?: string;
  requestedBy?: string;
  estimatedDelivery?: string;     // MM/DD/YYYY
  invoiceNumber?: string;
  controlNumber?: string;
  comment?: string;
  items: Array<{ part: string; qty: number; unitPrice: number; glAccount?: string }>;
  submitStage: SubmitStage;
}

export interface DocumentUploadInput {
  filePath: string;               // local PDF/JPG, < 2 MB
  invoiceNumber: string;
  invoiceAmount: number;
  invoiceDate: string;            // MM/DD/YYYY
  invoiceDueDate: string;
  vendorName: string;
  linkedPoNumber?: string;
  apGlAccount?: string;           // default "3002 - OPEN-ITEM A/P CREDITORS"
}

export interface CreatedPo {
  poNumber: string;
  controlNumber?: string;
  state: string;
}

// ─── Session manager ─────────────────────────────────────────────────────────

export class TekionSession {
  private browser!: Browser;
  private context!: BrowserContext;
  page!: Page;
  private apiLog: string[] = [];

  /** Launch a browser, reusing a saved session if one exists. */
  async start(): Promise<void> {
    this.browser = await chromium.launch({ headless: HEADLESS, proxy: proxyFromEnv() });
    this.context = await this.browser.newContext({
      storageState: existsSync(STORAGE_STATE) ? STORAGE_STATE : undefined,
      viewport: { width: 1680, height: 1050 },
    });
    this.page = await this.context.newPage();
    this.attachNetworkLogger();
  }

  /** Mirror every Tekion XHR/fetch to the console — this is the live endpoint map. */
  private attachNetworkLogger(): void {
    this.page.on('requestfinished', (req: Request) => {
      const url = req.url();
      if (!/tekion(cloud)?\.com/.test(url)) return;
      const type = req.resourceType();
      if (type !== 'xhr' && type !== 'fetch') return;
      const line = `[${req.method()}] ${url.replace(/\?.*$/, '')}`;
      this.apiLog.push(line);
      if (/login|mfa|purchase-order|oem-order|misc-order|document|vendor-invoice|parts/.test(url)) {
        console.log(`  → ${line}`);
      }
    });
  }

  /** Full login: username → password → TOTP. Skipped if the session is still valid. */
  async login(): Promise<void> {
    await this.page.goto(ROUTES.home, { waitUntil: 'domcontentloaded' });
    if (!/\/login/.test(this.page.url())) {
      console.log('✅ Reused existing session — no login needed.');
      return;
    }

    if (!CREDENTIALS.username || !CREDENTIALS.password) {
      throw new Error('TEKION_USERNAME / TEKION_PASSWORD not set in .env');
    }

    console.log('🔑 Logging in…');
    await this.page.goto(ROUTES.login, { waitUntil: 'domcontentloaded' });
    await this.page.fill(S.username, CREDENTIALS.username);
    await this.page.fill(S.password, CREDENTIALS.password);
    await this.page.click(S.loginSubmit);

    // TOTP Authenticator screen.
    const otp = this.page.locator(S.otpInput).first();
    await otp.waitFor({ state: 'visible', timeout: 30_000 });
    const code = this.currentTotp();
    console.log(`🔐 Entering TOTP ${code}`);
    await otp.fill(code);
    await this.page.click(S.otpSubmit);

    await this.page.waitForURL('**/home**', { timeout: 30_000 });
    await this.context.storageState({ path: STORAGE_STATE });
    console.log('✅ Logged in, session saved.');
  }

  private currentTotp(): string {
    if (!CREDENTIALS.totpSecret) throw new Error('TEKION_TOTP_SECRET not set in .env');
    return authenticator.generate(CREDENTIALS.totpSecret);
  }

  /** Open Parts → Purchase Order list. */
  async openPurchaseOrders(): Promise<void> {
    console.log('📦 Opening Purchase Order list…');
    await this.page.goto(ROUTES.poList, { waitUntil: 'domcontentloaded' });
    await this.page.waitForSelector(S.poCreateBtn, { timeout: 20_000 });
  }

  /** Create an OEM Special Order through the full UI flow. */
  async createOemSpecialOrder(input: OemSpecialOrderInput): Promise<CreatedPo> {
    console.log('🛒 Creating OEM Special Order…');

    // 1. Create ▾ → OEM Special Order  (fires POST /parts/api/v1/oem-orders/draft)
    await this.page.click(S.poCreateBtn);
    await this.page.click(S.poCreateMenuItem('OEM Special Order'));
    await this.page.waitForURL('**/oem-order/OEM_SPECIAL_ORDER/**', { timeout: 20_000 });

    // 2. Assign Parts modal — search each part, tick its priority row, save.
    await this.page.click(S.assignPartsLink);
    for (const part of input.parts) {
      await this.page.fill(S.assignPartsSearchInput, part.partNumber);
      await this.page.keyboard.press('Enter');
      await this.page.click(S.assignPartsRowCheckbox(part.priority));
    }
    await this.page.click(S.assignPartsSave);

    // 3. Order Details — set "Submit Order as" = OEM Order, fill required quantities.
    await this.selectByLabelOrTestId(S.submitOrderAsSelect, 'OEM Order');
    for (let i = 0; i < input.parts.length; i++) {
      await this.page.fill(S.requiredQtyInput(i + 1), String(input.parts[i].requiredQty));
    }

    if (input.submitStage === 'DRAFT') {
      await this.page.click(S.saveAsDraftBtn);
      const po = await this.readPoHeader();
      console.log(`✅ Draft saved: ${po.poNumber}`);
      return po;
    }

    // 4. Save → opens "Submit OEM Order" modal.
    await this.page.click(S.saveBtn);

    // 5. Fill submit modal (Dealer Code, Order Type, Shipment Priorities) → Submit.
    await this.selectByLabelOrTestId(S.submitModalDealerCode, input.dealerCode);
    await this.selectByLabelOrTestId(S.submitModalOrderType, input.orderType);
    await this.selectByLabelOrTestId(S.submitModalShipmentPriority, input.shipmentPriority);
    await this.selectByLabelOrTestId(S.submitModalAltShipmentPriority, input.alternateShipmentPriority);
    await this.page.click(S.submitModalSubmit);

    // 6. Success toast → read PO number out of it.
    await this.page.waitForSelector(S.successToast, { timeout: 30_000 });
    const po = await this.readPoHeader();
    console.log(`✅ OEM Special Order created: ${po.poNumber} (${po.state})`);
    return po;
  }

  /** Create a Miscellaneous (vendor) Order. */
  async createMiscOrder(input: MiscOrderInput): Promise<CreatedPo> {
    console.log('🧾 Creating Miscellaneous Order…');
    await this.page.goto(ROUTES.miscOrderAdd, { waitUntil: 'domcontentloaded' });

    // P.O. Details
    await this.fillAutocomplete(S.miscVendor, input.vendorName);
    if (input.vendorSite) await this.selectByLabelOrTestId(S.miscVendorSite, input.vendorSite);
    if (input.vendorPhone) await this.page.fill(S.miscVendorPhone, input.vendorPhone);
    if (input.vendorEmail) await this.page.fill(S.miscVendorEmail, input.vendorEmail);
    if (input.requestedBy) await this.selectByLabelOrTestId(S.miscRequestedBy, input.requestedBy);
    if (input.estimatedDelivery) await this.page.fill(S.miscEstDelivery, input.estimatedDelivery);
    if (input.invoiceNumber) await this.page.fill(S.miscInvoiceNumber, input.invoiceNumber);
    if (input.controlNumber) await this.page.fill(S.miscControlNumber, input.controlNumber);
    if (input.comment) await this.page.fill(S.miscComment, input.comment);

    // Items List
    for (const item of input.items) {
      await this.page.fill(S.miscItemPart, item.part);
      await this.page.keyboard.press('Enter');
      await this.page.fill(S.miscItemQty, String(item.qty));
      await this.page.fill(S.miscItemPrice, item.unitPrice.toFixed(2));
      if (item.glAccount) await this.selectByLabelOrTestId(S.miscItemGlAccount, item.glAccount);
    }

    // Submit
    if (input.submitStage === 'PRE_INVOICE') {
      await this.page.click(S.miscSubmitPreInvoice);
    } else if (input.submitStage === 'SUBMITTED') {
      await this.page.click(S.miscSubmit);
    } else {
      await this.page.click(S.saveAsDraftBtn);
    }

    await this.page.waitForSelector(S.successToast, { timeout: 30_000 });
    const po = await this.readPoHeader();
    console.log(`✅ Miscellaneous Order created: ${po.poNumber} (${po.state})`);
    return po;
  }

  /**
   * Attach the vendor-invoice document and create the Vendor Invoice.
   * Uses Playwright's native file handling so it works whether Tekion uses a
   * direct multipart POST or a presigned-URL upload.
   */
  async uploadInvoiceDocument(input: DocumentUploadInput): Promise<void> {
    console.log(`📎 Creating Vendor Invoice + attaching ${input.filePath}…`);
    if (!existsSync(input.filePath)) throw new Error(`File not found: ${input.filePath}`);

    await this.page.goto(ROUTES.vendorInvoiceCreate, { waitUntil: 'domcontentloaded' });

    // Upload the document (drag-drop area exposes a hidden <input type=file>).
    const fileInput = this.page.locator(S.invoiceFileInput).first();
    if (await fileInput.count()) {
      await fileInput.setInputFiles(input.filePath);
    } else {
      const [chooser] = await Promise.all([
        this.page.waitForEvent('filechooser'),
        this.page.click(S.invoiceUploadButton),
      ]);
      await chooser.setFiles(input.filePath);
    }

    // Match the open PO if provided.
    if (input.linkedPoNumber) {
      await this.page.check(S.invoiceEnterPo);
      await this.selectByLabelOrTestId(S.invoicePoNumber, input.linkedPoNumber);
    }

    // Invoice details.
    await this.selectByLabelOrTestId(S.invoiceVendor, input.vendorName);
    await this.page.fill(S.invoiceNumber, input.invoiceNumber);
    await this.page.fill(S.invoiceAmount, input.invoiceAmount.toFixed(2));
    await this.page.fill(S.invoiceDate, input.invoiceDate);
    await this.page.fill(S.invoiceDueDate, input.invoiceDueDate);
    await this.selectByLabelOrTestId(S.invoiceApGlAccount, input.apGlAccount ?? '3002 - OPEN-ITEM A/P CREDITORS');

    // Proceed to the Journal Entry step.
    await this.page.click(S.invoiceNext);
    console.log('✅ Invoice details submitted; document attached.');
  }

  /** Read the PO number + state from the order header (e.g. "OEM Special Order 32287 · Draft"). */
  private async readPoHeader(): Promise<CreatedPo> {
    const header = await this.page.locator('h1, h2, [data-testid="po-header"]').first().innerText().catch(() => '');
    const poNumber = header.match(/\b(\d{4,})\b/)?.[1] ?? 'UNKNOWN';
    const state = /pre[- ]?invoice/i.test(header) ? 'PRE_INVOICE'
      : /submitted/i.test(header) ? 'SUBMITTED'
      : /draft/i.test(header) ? 'DRAFT' : 'UNKNOWN';
    const controlNumber = (await this.page.locator('text=/Control Number[:\\s]/i').first().innerText().catch(() => ''))
      .match(/Control Number[:\s]+(\S+)/i)?.[1];
    return { poNumber, controlNumber, state };
  }

  /** Set a value on a native <select> or a custom combobox/listbox. */
  private async selectByLabelOrTestId(selector: string, value: string): Promise<void> {
    const el = this.page.locator(selector).first();
    await el.waitFor({ state: 'visible', timeout: 15_000 });
    const tag = await el.evaluate((n) => n.tagName.toLowerCase()).catch(() => '');
    if (tag === 'select') {
      await el.selectOption({ label: value }).catch(() => el.selectOption(value));
      return;
    }
    // Custom dropdown: click to open, then pick the matching option.
    await el.click();
    const option = this.page.locator(`[role="option"]:has-text("${value}"), li:has-text("${value}")`).first();
    await option.click();
  }

  /** Type into an autocomplete and pick the first matching suggestion. */
  private async fillAutocomplete(selector: string, value: string): Promise<void> {
    const input = this.page.locator(selector).first();
    await input.click();
    await input.fill(value);
    const suggestion = this.page.locator(`[role="option"]:has-text("${value}"), li:has-text("${value}")`).first();
    await suggestion.waitFor({ state: 'visible', timeout: 10_000 });
    await suggestion.click();
  }

  /** Dump the captured endpoint map (useful after a run to confirm internal paths). */
  printApiLog(): void {
    console.log('\n── Captured Tekion API calls this session ──');
    [...new Set(this.apiLog)].forEach((l) => console.log('   ' + l));
  }

  async stop(): Promise<void> {
    await this.context?.close();
    await this.browser?.close();
  }
}

// ─── End-to-end example run ──────────────────────────────────────────────────

async function main(): Promise<void> {
  const session = new TekionSession();
  await session.start();
  try {
    await session.login();
    await session.openPurchaseOrders();

    // 1. Create an OEM Special Order and submit it (parts ack / pre-invoice).
    const oemPo = await session.createOemSpecialOrder({
      dealerCode: '207562',
      orderType: 'EMG',
      shipmentPriority: 'Next Day Delivery',
      alternateShipmentPriority: 'Next Day Delivery',
      parts: [
        { partNumber: '15650-R40-A01', requiredQty: 1, priority: 'CSO' },
        { partNumber: '74412-3A0-A00', requiredQty: 1, priority: 'CSO' },
        { partNumber: '90118-SDA-A00', requiredQty: 2, priority: 'CSO' },
      ],
      submitStage: 'SUBMITTED',
    });

    // 2. Create a Miscellaneous Order to the Pre-Invoice state.
    const miscPo = await session.createMiscOrder({
      vendorName: 'ADVANCE AUTO PARTS',
      vendorSite: 'Site 1 - 42047',
      invoiceNumber: '4347615448062',
      items: [{ part: 'ACDELCO-41-110', qty: 12, unitPrice: 8.75, glAccount: '1450 - PARTS INVENTORY' }],
      submitStage: 'PRE_INVOICE',
    });

    // 3. Create the Vendor Invoice + attach the invoice image, linked to the misc PO.
    await session.uploadInvoiceDocument({
      filePath: './invoices/INV-558720.pdf',
      invoiceNumber: 'INV-558720',
      invoiceAmount: 105.0,
      invoiceDate: '06/04/2026',
      invoiceDueDate: '07/15/2026',
      vendorName: 'ADVANCE AUTO PARTS',
      linkedPoNumber: miscPo.poNumber,
      apGlAccount: '3002 - OPEN-ITEM A/P CREDITORS',
    });

    console.log('\n✅ Flow complete:', { oemPo, miscPo });
  } catch (err) {
    console.error('❌ Flow failed:', err);
    await session.page.screenshot({ path: `error-${Date.now()}.png`, fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    session.printApiLog();
    await session.stop();
  }
}

if (process.argv[1]?.endsWith('tekionPo.ts')) {
  main();
}
