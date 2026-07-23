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
  subletOrderAdd: `${BASE_URL}/parts/purchase-order/sublet/add`,
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
  miscVendor: '#VENDOR input',
  miscVendorSite: '#SITES input',
  miscVendorPhone: '[data-test-id="@tekion-parts-purchaseOrder-isTemporaryVendor-vendorPhone-phoneInput-phoneNumberInput"]',
  miscVendorEmail: '#VENDOR_EMAIL',
  miscRequestedBy: '#REQUESTED_BY input',
  miscEstDelivery: '[data-test="@tekion-parts-purchaseOrder-estimatedDeliveryDate-requestedBy"]',
  miscInvoiceNumber: '#INVOICE_NUMBER',
  miscControlNumber: '#CONTROL_NUMBER',
  miscComment: '#COMMENT',
  miscItemPart: '[data-test-id="@tekion-parts-purchaseOrder-partsListTableForMiscPo-itemNameColumnInput"]',
  miscItemQty: '[data-test-id="@tekion-parts-purchaseOrder-partsPOFormPage-partsListTable-requiredQuantity-cell-0-1"] input',
  miscItemPrice: '[data-test-id="@tekion-parts-purchaseOrder-partsPOFormPage-partsListTable-partPrice-cell-0-2"] input',
  miscItemGlAccount: '[data-test-id="@tekion-parts-purchaseOrder-partsListTableForMiscPo-glAccountColumnSelect"] input',
  miscItemTax: '[data-test-id*="partsListTable-taxable-cell"] input[type="checkbox"]',
  miscSubmitPreInvoice: 'button:has-text("Submit & Pre-Invoice")',
  miscSubmit: 'button:has-text("Submit"):not(:has-text("Pre-Invoice"))',

  // Miscellaneous Order — Pre-Invoice form
  miscPreInvoiceNumber: '#INVOICE_NUMBER',
  miscPreInvoiceDate: '[data-field-id="INVOICE_DATE"] input[placeholder="MM/DD/YYYY"]',
  miscPreInvoiceDueDate: '[data-field-id="INVOICE_DUE_DATE"] input[placeholder="MM/DD/YYYY"]',
  miscPreInvoiceAmount: '#INVOICE_AMOUNT',
  miscPreInvoiceGlAccount: '#GL_ACCOUNT',
  miscPreInvoiceAcctDetailGl: '[data-test-id="@tekion-parts-purchaseOrder-accountingDetailsTable-returnCellAdvancedSelect"]',
  miscPreInvoiceAcctDetailAmount: '[data-field-id="ACCOUNTING_DETAILS__0__AMOUNT"] input',
  miscPreInvoiceAcctDetailDesc: '[data-test-id="@tekion-parts-poPreInvoice-preInvoiceFormPanel-inputWrapper"]',
  miscPreInvoiceComment: '#COMMENT',
  miscPreInvoiceSubmit: '[data-test-id="@tekion-parts-purchaseOrder-poPreInvoiceSaveComponent-Submit"]',

  // Sublet Order — actual data-test-id values from Tekion UI
  subletVendor: '#vendorName input',
  subletVendorSite: '#siteSelector input',
  subletVendorPhone: '[data-test-id="@tekion-parts-purchaseOrder-poSublet-subletPoFormPage-partsSubletPoViewForm-formWithSubmission-vendorPhone-phoneInput-phoneNumberInput"]',
  subletVendorEmail: '#vendorEmail',
  subletInvoiceNumber: '#invoiceNumber',
  subletTaxable: '[data-test-id="@tekion-parts-purchaseOrder-poSublet-subletPoFormPage-partsSubletPoViewForm-taxableField"]',
  // Sublet list table — row 0 fields (use nth() for additional rows)
  subletRowRoNumber: '[data-test-id*="-roNumber-cell-"] input',
  subletRowJobNumber: '[data-test-id*="-jobNumber-cell-"] input',
  subletRowDescription: '[data-test-id*="-description-cell-"] input',
  subletRowGlAccount: '[data-test-id*="-glAccount-cell-"] input',
  subletRowLaborPrice: '[data-test-id*="-laborAmount-cell-"] input',
  subletRowPartsPrice: '[data-test-id*="-partAmount-cell-"] input',
  subletSubmit: 'button:has-text("Submit"):not(:has-text("Pre-Invoice"))',

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

export interface SubletOrderInput {
  vendorName: string;
  vendorAddress?: string;
  vendorPhone?: string;
  vendorEmail?: string;
  invoiceNumber?: string;
  items: Array<{
    roNumber?: string;
    jobNumber?: string;
    description: string;
    subletType?: string;
    glAccount?: string;
    laborPrice?: number;
    partsPrice?: number;
  }>;
  submitStage: SubmitStage;
}

export interface MiscOrderInput {
  vendorName: string;             // e.g. "ADVANCE AUTO PARTS"
  vendorAddress?: string;
  vendorSite?: string;
  vendorPhone?: string;
  vendorEmail?: string;
  requestedBy?: string;
  estimatedDelivery?: string;     // MM/DD/YYYY
  invoiceNumber?: string;
  controlNumber?: string;
  comment?: string;
  items: Array<{ part: string; qty: number; unitPrice: number; glAccount?: string; taxable?: boolean }>;
  invoiceDate?: string;          // MM/DD/YYYY
  invoiceDueDate?: string;       // MM/DD/YYYY
  invoiceAmount?: number;        // total invoice amount
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
      console.log('[SESSION] Reused existing session -- no login needed.');
      return;
    }

    if (!CREDENTIALS.username || !CREDENTIALS.password) {
      throw new Error('TEKION_USERNAME / TEKION_PASSWORD not set in .env');
    }

    console.log('[SESSION] Logging in...');
    await this.page.goto(ROUTES.login, { waitUntil: 'domcontentloaded' });
    await this.page.fill(S.username, CREDENTIALS.username);
    await this.page.fill(S.password, CREDENTIALS.password);
    await this.page.click(S.loginSubmit);

    // TOTP Authenticator screen.
    const otp = this.page.locator(S.otpInput).first();
    await otp.waitFor({ state: 'visible', timeout: 30_000 });
    const code = this.currentTotp();
    console.log(`[SESSION] Entering TOTP ${code}`);
    await otp.fill(code);
    await this.page.click(S.otpSubmit);

    await this.page.waitForURL('**/home**', { timeout: 30_000 });
    await this.context.storageState({ path: STORAGE_STATE });
    console.log('[SESSION] Logged in, session saved.');
  }

  private currentTotp(): string {
    if (!CREDENTIALS.totpSecret) throw new Error('TEKION_TOTP_SECRET not set in .env');
    return authenticator.generate(CREDENTIALS.totpSecret);
  }

  /** Open Parts → Purchase Order list. */
  async openPurchaseOrders(): Promise<void> {
    console.log('[PO] Opening Purchase Order list...');
    for (let attempt = 0; attempt < 3; attempt++) {
      await this.page.goto(ROUTES.poList, { waitUntil: 'domcontentloaded' });
      // Wait for Create button with network idle
      await this.page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
      const createBtn = this.page.locator(S.poCreateBtn);
      if (await createBtn.isVisible({ timeout: 10_000 }).catch(() => false)) {
        return;
      }
      console.log(`[PO] Create button not visible (attempt ${attempt + 1}), retrying...`);
      await this.page.waitForTimeout(2000);
    }
    // Last attempt — let it throw if still not found
    await this.page.goto(ROUTES.poList, { waitUntil: 'domcontentloaded' });
    await this.page.waitForSelector(S.poCreateBtn, { timeout: 20_000 });
  }

  /** Switch the active dealership in Tekion by name. */
  async switchDealership(dealerName: string): Promise<void> {
    if (!dealerName) {
      console.log('[DEALER] No dealer name provided, skipping dealership switch');
      return;
    }

    console.log(`[DEALER] Switching to: ${dealerName}`);

    // Check if already on the correct dealership by reading the header
    const currentDealer = await this.page.locator('[class*="root_selectedDealer_dealer__"]').first().innerText().catch(() => '');
    if (currentDealer && currentDealer.trim() === dealerName) {
      console.log(`[DEALER] Already on ${dealerName}, no switch needed`);
      return;
    }

    // Open the dealership selector menu — retry up to 3 times
    let menuOpened = false;
    for (let attempt = 0; attempt < 3 && !menuOpened; attempt++) {
      const dealerMenuBtn = this.page.locator('[data-test-id="@tekion-selectedDealer-chevronIcon"]');
      await dealerMenuBtn.waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});
      await dealerMenuBtn.click({ force: true }).catch(async () => {
        console.log(`[DEALER] Chevron click failed (attempt ${attempt + 1}), trying container click`);
        await this.page.locator('[class*="root_dealerSelect_container"]').first().click({ force: true }).catch(() => {});
        await this.page.locator('[class*="root_selectedDealer_container"]').first().click({ force: true }).catch(() => {});
      });
      await this.page.waitForTimeout(1500);

      // Check if the dealership list appeared
      menuOpened = await this.page.locator('[class*="root_dealerInfoList"]').first().isVisible().catch(() => false);
      console.log(`[DEALER] Dealer list visible (attempt ${attempt + 1}): ${menuOpened}`);
      if (!menuOpened) {
        await this.page.waitForTimeout(1000);
      }
    }

    if (!menuOpened) {
      await this.page.screenshot({ path: `dealer-menu-debug-${Date.now()}.png` }).catch(() => {});
      console.log('[DEALER] Menu did not open after 3 attempts, screenshot saved');
      return;
    }

    // Wait for the dealership list to appear
    await this.page.waitForSelector('[data-test-id="@tekion-dealerInfoList-searchInput"]', { timeout: 5000 }).catch(() => {});

    // Search for the dealership
    const searchInput = this.page.locator('[data-test-id="@tekion-dealerInfoList-searchInput"]');
    const searchVisible = await searchInput.isVisible().catch(() => false);
    console.log(`[DEALER] Search input visible: ${searchVisible}`);
    if (searchVisible) {
      await searchInput.fill(dealerName);
      await this.page.waitForTimeout(1000);
    }

    // Click the dealership item — use count() since virtualized lists may hide items
    const dealerItem = this.page.locator(`[data-test-id="@tekion-dealerInfoItem-${dealerName}"]`);
    const itemCount = await dealerItem.count();
    console.log(`[DEALER] Found ${itemCount} matching dealership item(s)`);
    if (itemCount > 0) {
      await dealerItem.first().scrollIntoViewIfNeeded().catch(() => {});
      await this.page.waitForTimeout(300);
      await dealerItem.first().click();
      console.log(`[DEALER] Clicked dealership: ${dealerName}`);
      // Wait for Tekion to reload after dealership switch (adaptive)
      await this.page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
      await this.page.waitForTimeout(1000);
    } else {
      console.log(`[DEALER] Could not find dealership "${dealerName}" in the list`);
      // Close the menu
      await this.page.keyboard.press('Escape');
      await this.page.waitForTimeout(500);
    }
  }

  /** Create an OEM Special Order through the full UI flow. */
  async createOemSpecialOrder(input: OemSpecialOrderInput): Promise<CreatedPo> {
    console.log('[PO] Creating OEM Special Order...');

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
      console.log(`[PO] Draft saved: ${po.poNumber}`);
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
    console.log(`[PO] OEM Special Order created: ${po.poNumber} (${po.state})`);
    return po;
  }

  /** Create a Miscellaneous (vendor) Order. */
  async createMiscOrder(input: MiscOrderInput): Promise<CreatedPo> {
    console.log('[PO] Creating Miscellaneous Order...');
    await this.page.goto(ROUTES.miscOrderAdd, { waitUntil: 'domcontentloaded' });
    await this.page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    await this.page.waitForSelector(S.miscVendor, { timeout: 15_000 }).catch(() => {});

    // Vendor (react-select autocomplete, disambiguated by address/phone)
    if (input.vendorName) {
      console.log(`[PO] Filling vendor: ${input.vendorName}`);
      await this.fillVendorAutocomplete(S.miscVendor, input.vendorName, input.vendorAddress, input.vendorPhone);
      await this.page.waitForTimeout(1000);

      // Vendor Site — may auto-select after vendor is picked, check first
      const siteValue = await this.page.locator('#SITES [class*="singleValue"]').first().innerText().catch(() => '');
      if (siteValue) {
        console.log(`[PO] Vendor site already auto-selected: ${siteValue}`);
      } else {
        const siteInput = this.page.locator(S.miscVendorSite).first();
        if (await siteInput.count()) {
          console.log('[PO] Selecting vendor site (first available)');
          await siteInput.click({ force: true }).catch(() => {});
          const siteOption = this.page.locator('[role="option"]').first();
          if (await siteOption.isVisible().catch(() => false)) {
            await siteOption.click();
            await this.page.waitForTimeout(500);
          }
        }
      }
    }

    if (input.vendorPhone) {
      const phoneInput = this.page.locator(S.miscVendorPhone).first();
      if (await phoneInput.count()) await phoneInput.fill(input.vendorPhone);
    }
    if (input.vendorEmail) await this.page.fill(S.miscVendorEmail, input.vendorEmail);
    if (input.requestedBy) await this.selectByLabelOrTestId(S.miscRequestedBy, input.requestedBy);
    if (input.estimatedDelivery) await this.page.fill(S.miscEstDelivery, input.estimatedDelivery);
    if (input.invoiceNumber) await this.page.fill(S.miscInvoiceNumber, input.invoiceNumber);
    if (input.controlNumber) await this.page.fill(S.miscControlNumber, input.controlNumber);
    if (input.comment) await this.page.fill(S.miscComment, input.comment);

    // Items List — plain text input for part name, number inputs for qty/price
    for (const item of input.items) {
      console.log(`[PO]   Item: ${item.part} qty=${item.qty} price=${item.unitPrice}`);
      await this.page.fill(S.miscItemPart, item.part);
      await this.page.fill(S.miscItemQty, String(item.qty));
      await this.page.fill(S.miscItemPrice, item.unitPrice.toFixed(2));
      if (item.glAccount) {
        const glInput = this.page.locator(S.miscItemGlAccount).first();
        if (await glInput.count()) {
          await glInput.click();
          await glInput.fill(item.glAccount);
          const glOption = this.page.locator(`[role="option"]:has-text("${item.glAccount}")`).first();
          if (await glOption.isVisible({ timeout: 5000 }).catch(() => false)) {
            await glOption.click();
          }
        }
      }
      // Tax checkbox — check if taxable is true
      if (item.taxable) {
        const taxCheckbox = this.page.locator(S.miscItemTax).first();
        if (await taxCheckbox.count() && !(await taxCheckbox.isChecked())) {
          await taxCheckbox.check();
        }
      }
    }

    console.log('[PO] Misc form filled.');
    await this.page.screenshot({ path: `misc-filled-${Date.now()}.png`, fullPage: true }).catch(() => {});

    if (input.submitStage === 'DRAFT') {
      console.log('[PO] submitStage=DRAFT -- stopping for manual review in browser.');
      return { poNumber: 'PENDING', controlNumber: undefined, state: 'DRAFT' };
    }

    // Click "Submit & Pre-Invoice" to proceed to the pre-invoice form
    console.log('[PO] Clicking Submit & Pre-Invoice...');
    const preInvoiceBtn = this.page.locator(S.miscSubmitPreInvoice).first();
    await preInvoiceBtn.waitFor({ state: 'visible', timeout: 10_000 });
    await preInvoiceBtn.click();
    await this.page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    await this.page.waitForTimeout(2000);
    console.log('[PO] Pre-invoice form loaded.');

    // Fill pre-invoice form
    await this.fillMiscPreInvoiceForm(input);

    // Click final Submit
    console.log('[PO] Clicking final Submit on pre-invoice form...');
    const submitBtn = this.page.locator(S.miscPreInvoiceSubmit).first();
    await submitBtn.waitFor({ state: 'visible', timeout: 10_000 });
    await submitBtn.click();
    await this.page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    await this.page.waitForTimeout(3000);

    // Try to read the PO number from the success toast or URL
    const poNumber = await this.extractPoNumber();
    console.log(`[PO] Pre-invoice submitted. PO Number: ${poNumber ?? 'unknown'}`);
    await this.page.screenshot({ path: `misc-submitted-${Date.now()}.png`, fullPage: true }).catch(() => {});
    return { poNumber: poNumber ?? 'PENDING', controlNumber: undefined, state: 'PRE_INVOICE' };
  }

  /** Fill the pre-invoice form that appears after "Submit & Pre-Invoice" on a Misc PO. */
  private async fillMiscPreInvoiceForm(input: MiscOrderInput): Promise<void> {
    // Invoice Number
    if (input.invoiceNumber) {
      const invInput = this.page.locator(S.miscPreInvoiceNumber).first();
      if (await invInput.count()) {
        console.log(`[PO] Pre-invoice: Invoice Number = ${input.invoiceNumber}`);
        await invInput.fill(input.invoiceNumber);
      }
    }

    // Invoice Date
    if (input.invoiceDate) {
      const dateInput = this.page.locator(S.miscPreInvoiceDate).first();
      if (await dateInput.count()) {
        console.log(`[PO] Pre-invoice: Invoice Date = ${input.invoiceDate}`);
        await dateInput.fill(input.invoiceDate);
      }
    }

    // Invoice Due Date
    if (input.invoiceDueDate) {
      const dueInput = this.page.locator(S.miscPreInvoiceDueDate).first();
      if (await dueInput.count()) {
        console.log(`[PO] Pre-invoice: Invoice Due Date = ${input.invoiceDueDate}`);
        await dueInput.fill(input.invoiceDueDate);
      }
    }

    // Invoice Amount — may already be pre-filled from PO total
    if (input.invoiceAmount !== undefined) {
      const amountInput = this.page.locator(S.miscPreInvoiceAmount).first();
      if (await amountInput.count()) {
        const currentVal = await amountInput.inputValue().catch(() => '');
        if (!currentVal) {
          console.log(`[PO] Pre-invoice: Invoice Amount = ${input.invoiceAmount}`);
          await amountInput.fill(input.invoiceAmount.toFixed(2));
        } else {
          console.log(`[PO] Pre-invoice: Invoice Amount already pre-filled = ${currentVal}`);
        }
      }
    }

    // Accounting Details — fill first row with GL Account and Amount
    // The form shows a warning if the full amount is not accounted for
    const acctGlSelect = this.page.locator(S.miscPreInvoiceAcctDetailGl).first();
    if (await acctGlSelect.count()) {
      // Use GL account from first item, or default to 2410
      const targetGl = input.items.find(i => i.glAccount)?.glAccount ?? '2410';
      console.log(`[PO] Pre-invoice: filling Accounting Details row 0 (GL=${targetGl})...`);
      await acctGlSelect.click({ force: true });
      await this.page.waitForTimeout(500);
      // Type the GL account code to filter
      const glInput = acctGlSelect.locator('input').first();
      if (await glInput.count()) {
        await glInput.fill(targetGl);
        await this.page.waitForTimeout(1000);
      }
      // Try to find an option containing the GL code
      const glOption = this.page.locator(`[role="option"]:has-text("${targetGl}")`).first();
      if (await glOption.isVisible({ timeout: 5000 }).catch(() => false)) {
        const glText = await glOption.innerText().catch(() => '');
        console.log(`[PO]   GL Account: selecting "${glText.replace(/\\s+/g, ' ').trim()}"`);
        await glOption.click();
        await this.page.waitForTimeout(500);
      } else {
        // Fallback: select first option
        const firstOpt = this.page.locator('[role="option"]').first();
        if (await firstOpt.isVisible({ timeout: 3000 }).catch(() => false)) {
          const glText = await firstOpt.innerText().catch(() => '');
          console.log(`[PO]   GL Account: no match for ${targetGl}, selecting first: "${glText.replace(/\\s+/g, ' ').trim()}"`);
          await firstOpt.click();
          await this.page.waitForTimeout(500);
        }
      }
    }

    // Accounting Details — Amount (use invoice amount or PO total)
    const acctAmountInput = this.page.locator(S.miscPreInvoiceAcctDetailAmount).first();
    if (await acctAmountInput.count()) {
      // Read the invoice amount from the form if not provided
      let amount = input.invoiceAmount;
      if (amount === undefined) {
        const formAmount = await this.page.locator(S.miscPreInvoiceAmount).first().inputValue().catch(() => '');
        amount = formAmount ? Number(formAmount) : undefined;
      }
      if (amount !== undefined) {
        console.log(`[PO]   Accounting Amount: ${amount.toFixed(2)}`);
        await acctAmountInput.fill(amount.toFixed(2));
      }
    }

    // Accounting Details — Description
    const acctDescInput = this.page.locator(S.miscPreInvoiceAcctDetailDesc).first();
    if (await acctDescInput.count()) {
      const desc = input.items.map(i => i.part).join(', ');
      console.log(`[PO]   Accounting Description: ${desc}`);
      await acctDescInput.fill(desc);
    }

    // Comments
    if (input.comment) {
      const commentInput = this.page.locator(S.miscPreInvoiceComment).first();
      if (await commentInput.count()) {
        console.log(`[PO] Pre-invoice: Comment = ${input.comment}`);
        await commentInput.fill(input.comment);
      }
    }
  }

  /** Try to extract the PO number from success toast or page URL after submission. */
  private async extractPoNumber(): Promise<string | undefined> {
    // Try success toast
    const toast = this.page.locator('[role="status"]:has-text("success"), [role="alert"]:has-text("success")').first();
    if (await toast.isVisible({ timeout: 3000 }).catch(() => false)) {
      const toastText = await toast.innerText().catch(() => '');
      const match = toastText.match(/(\d{4,})/);
      if (match) return match[1];
    }
    // Try URL pattern
    const url = this.page.url();
    const urlMatch = url.match(/\/(\d{4,})(?:\?|$|\/)/);
    if (urlMatch) return urlMatch[1];
    return undefined;
  }

  /**
   * Create a Sublet Order through the UI.
   * Navigates via Create dropdown -> Sublet, then fills vendor, control number,
   * invoice number, and line items from the OCR data.
   */
  async createSubletOrder(input: SubletOrderInput): Promise<CreatedPo> {
    console.log('[PO] Creating Sublet Order...');

    // 1. Create dropdown -> Sublet (retry if menu items not loaded yet)
    let allItems: string[] = [];
    let menu: any;
    for (let attempt = 0; attempt < 5; attempt++) {
      await this.page.click(S.poCreateBtn);
      console.log(`[PO] Create dropdown clicked (attempt ${attempt + 1}), waiting for menu...`);

      // Wait for the dropdown menu to appear
      const menuSelector = '[data-test-id="@tekion-parts-purchaseOrderList-createPurchaseOrderMenu"]';
      menu = this.page.locator(menuSelector);
      await menu.waitFor({ state: 'visible', timeout: 10_000 }).catch(() => {});
      await this.page.waitForTimeout(500);

      // Log all menu item labels
      allItems = await menu.locator('li').allInnerTexts().catch(() => []);
      console.log(`[PO] Create dropdown items: ${JSON.stringify(allItems)}`);

      if (allItems.length > 0) break;

      // Close dropdown and retry
      console.log('[PO] Menu items empty, retrying...');
      await this.page.keyboard.press('Escape').catch(() => {});
      await this.page.waitForTimeout(1500);
    }

    // Find and click the Sublet item — try multiple label variations
    const menuLabels = ['Sublet', 'Sublet PO', 'Sublet Order', 'Sublet Purchase Order'];
    let menuClicked = false;
    for (const label of menuLabels) {
      const li = menu.locator('li', { hasText: label }).first();
      if (await li.count() && await li.isVisible()) {
        console.log(`[PO] Clicking menu item: "${label}"`);
        // Click the button inside the li, not the li itself
        const btn = li.locator('button').first();
        if (await btn.count()) {
          await btn.click();
        } else {
          await li.click();
        }
        menuClicked = true;
        break;
      }
    }
    if (!menuClicked) {
      await this.page.screenshot({ path: `sublet-menu-fail-${Date.now()}.png`, fullPage: true }).catch(() => {});
      throw new Error(`Sublet menu item not found. Items were: ${JSON.stringify(allItems)}`);
    }

    // 1b. "Create Sublet" modal appears — select "Repair Order / Without Reference" then click Create
    const subletModal = this.page.locator('[data-test-id="@tekion-parts-poDetail-subletPOFormPage-modal-title"]').first();
    await subletModal.waitFor({ state: 'visible', timeout: 10_000 });
    console.log('[PO] Create Sublet modal appeared');

    // Ensure "Repair Order / Without Reference" (SERVICE_SUBLET) radio is selected
    const serviceSubletRadio = this.page.locator('[data-test-id="@tekion-parts-purchaseOrder-poSublet-createSubletModal-createSubletModalFieldsRadio-SERVICE_SUBLET"]');
    if (await serviceSubletRadio.count()) {
      const isChecked = await serviceSubletRadio.isChecked().catch(() => false);
      if (!isChecked) {
        console.log('[PO] Selecting "Repair Order / Without Reference"');
        await serviceSubletRadio.check();
      } else {
        console.log('[PO] "Repair Order / Without Reference" already selected');
      }
    }

    // Click Create button on the modal
    const modalCreateBtn = this.page.locator('[data-test-id="@tekion-parts-poDetail-subletPOFormPage-modal-submitButton"]');
    if (await modalCreateBtn.count()) {
      console.log('[PO] Clicking Create on Sublet modal');
      await modalCreateBtn.click();
    }

    // Wait for the modal to close and the actual form to load
    await subletModal.waitFor({ state: 'hidden', timeout: 15_000 }).catch(() => {});
    await this.page.waitForTimeout(2000);
    console.log(`[PO] Sublet form loaded at ${this.page.url()}`);

    // 2. Vendor (autocomplete — type and pick first match)
    if (input.vendorName) {
      console.log(`[PO] Filling vendor: ${input.vendorName}`);
      await this.fillVendorAutocomplete(S.subletVendor, input.vendorName, input.vendorAddress, input.vendorPhone);
      await this.page.waitForTimeout(1000);

      // Vendor Site — may auto-select after vendor is picked, check first
      const siteValue = await this.page.locator('#siteSelector .tekion-select-1tce4an-singleValue, #siteSelector [class*="singleValue"]').first().innerText().catch(() => '');
      if (siteValue) {
        console.log(`[PO] Vendor site already auto-selected: ${siteValue}`);
      } else {
        // Need to manually select a site
        const siteInput = this.page.locator(S.subletVendorSite).first();
        if (await siteInput.count()) {
          console.log('[PO] Selecting vendor site (first available)');
          await siteInput.click({ force: true }).catch(() => {});
          const siteOption = this.page.locator('[role="option"]').first();
          if (await siteOption.isVisible().catch(() => false)) {
            await siteOption.click();
            await this.page.waitForTimeout(500);
          }
        }
      }
    }

    // 3. Vendor Phone
    if (input.vendorPhone) {
      console.log(`[PO] Filling vendor phone: ${input.vendorPhone}`);
      const phoneInput = this.page.locator(S.subletVendorPhone).first();
      if (await phoneInput.count()) {
        await phoneInput.fill(input.vendorPhone);
      }
    }

    // 4. Vendor Email
    if (input.vendorEmail) {
      console.log(`[PO] Filling vendor email: ${input.vendorEmail}`);
      const emailInput = this.page.locator(S.subletVendorEmail).first();
      if (await emailInput.count()) {
        await emailInput.fill(input.vendorEmail);
      }
    }

    // 5. Invoice Number
    if (input.invoiceNumber) {
      console.log(`[PO] Filling invoice number: ${input.invoiceNumber}`);
      const invInput = this.page.locator(S.subletInvoiceNumber).first();
      if (await invInput.count()) {
        await invInput.fill(input.invoiceNumber);
      }
    }

    // 6. Sublet List — line items
    for (let i = 0; i < input.items.length; i++) {
      const item = input.items[i];
      console.log(`[PO] Filling sublet line item ${i + 1}: ${item.description}`);

      // RO Number — combobox, type and pick from dropdown
      if (item.roNumber) {
        const roCell = this.page.locator(`[data-test-id*="-roNumber-cell-${i}"]`);
        if (await roCell.count()) {
          console.log(`[PO]   RO Number: ${item.roNumber}`);
          // Click the container div (not the hidden input) to open the dropdown
          await roCell.click({ force: true });
          await this.page.waitForTimeout(500);
          // Type into the input that's now visible
          const roInput = roCell.locator('input');
          if (await roInput.count()) {
            await roInput.fill(item.roNumber);
          }
          await this.page.waitForTimeout(1000);
          // Try to pick the first option
          const roOption = this.page.locator('[role="option"]').first();
          if (await roOption.isVisible().catch(() => false)) {
            await roOption.click();
            console.log('[PO]   RO Number option selected');
            await this.page.waitForTimeout(1500);
          } else {
            console.log('[PO]   RO Number: no dropdown options appeared (RO may not exist in system)');
          }
        }
      }

      // Job Number — may auto-fill after RO selection, or need manual selection
      if (item.jobNumber) {
        const jobCell = this.page.locator(`[data-test-id*="-jobNumber-cell-${i}"]`);
        if (await jobCell.count()) {
          const jobInput = jobCell.locator('input');
          const isDisabled = await jobInput.isDisabled().catch(() => true);
          if (!isDisabled) {
            // Check if already auto-filled
            const currentVal = await jobCell.locator('[class*="singleValue"]').first().innerText().catch(() => '');
            if (currentVal && currentVal !== 'Select') {
              console.log(`[PO]   Job Number already auto-selected: ${currentVal}`);
            } else {
              console.log(`[PO]   Job Number: selecting ${item.jobNumber}`);
              await jobCell.click({ force: true });
              await this.page.waitForTimeout(500);
              if (await jobInput.count()) {
                await jobInput.fill(item.jobNumber);
              }
              await this.page.waitForTimeout(500);
              const jobOption = this.page.locator('[role="option"]').first();
              if (await jobOption.isVisible().catch(() => false)) {
                await jobOption.click();
                console.log('[PO]   Job Number option selected');
                await this.page.waitForTimeout(500);
              }
            }
          } else {
            console.log('[PO]   Job Number: field is disabled, skipping');
          }
        }
      }

      // Sublet Type / Op Code — may auto-fill after RO selection
      if (item.subletType) {
        const opcodeCell = this.page.locator(`[data-test-id*="-subletOpcode-cell-${i}"]`);
        if (await opcodeCell.count()) {
          const opcodeInput = opcodeCell.locator('input');
          const isDisabled = await opcodeInput.isDisabled().catch(() => true);
          if (!isDisabled) {
            const currentVal = await opcodeCell.locator('[class*="singleValue"]').first().innerText().catch(() => '');
            if (currentVal && currentVal !== 'Type Here') {
              console.log(`[PO]   Sublet Type already auto-selected: ${currentVal}`);
            } else {
              console.log(`[PO]   Sublet Type: selecting ${item.subletType}`);
              await opcodeCell.click({ force: true });
              await this.page.waitForTimeout(500);
              if (await opcodeInput.count()) {
                await opcodeInput.fill(item.subletType);
              }
              await this.page.waitForTimeout(500);
              const opcodeOption = this.page.locator('[role="option"]').first();
              if (await opcodeOption.isVisible().catch(() => false)) {
                await opcodeOption.click();
                console.log('[PO]   Sublet Type option selected');
                await this.page.waitForTimeout(500);
              }
            }
          } else {
            console.log('[PO]   Sublet Type: field is disabled, skipping');
          }
        }
      }

      // Description — text input (always enabled)
      const descInput = this.page.locator(`[data-test-id*="-description-cell-${i}"] input`);
      if (await descInput.count()) {
        console.log(`[PO]   Description: ${item.description}`);
        await descInput.fill(item.description);
      }

      // GL Account — combobox, may be disabled until RO is selected
      if (item.glAccount) {
        const glCell = this.page.locator(`[data-test-id*="-glAccount-cell-${i}"]`);
        if (await glCell.count()) {
          const glInput = glCell.locator('input');
          // Wait for the field to become enabled after RO selection (up to 5 seconds)
          let isDisabled = await glInput.isDisabled().catch(() => true);
          if (isDisabled) {
            console.log(`[PO]   GL Account: waiting for field to enable after RO selection...`);
            for (let attempt = 0; attempt < 10 && isDisabled; attempt++) {
              await this.page.waitForTimeout(500);
              isDisabled = await glInput.isDisabled().catch(() => true);
            }
          }
          if (isDisabled) {
            console.log(`[PO]   GL Account: field still disabled after waiting, skipping`);
          } else {
            // Check if already auto-filled by RO selection
            const currentVal = await glCell.locator('[class*="singleValue"]').first().innerText().catch(() => '');
            if (currentVal && currentVal !== 'Type Here' && currentVal !== 'Select') {
              console.log(`[PO]   GL Account already auto-selected: ${currentVal}`);
            } else {
              console.log(`[PO]   GL Account: ${item.glAccount}`);
              await glCell.click({ force: true });
              await this.page.waitForTimeout(500);
              if (await glInput.count()) {
                await glInput.fill(item.glAccount);
              }
              await this.page.waitForTimeout(1000);
              const glOption = this.page.locator('[role="option"]').first();
              if (await glOption.isVisible().catch(() => false)) {
                await glOption.click();
                console.log('[PO]   GL Account option selected');
                await this.page.waitForTimeout(500);
              }
            }
          }
        }
      }

      // Labor Price — number input
      if (item.laborPrice !== undefined && item.laborPrice > 0) {
        const laborInput = this.page.locator(`[data-test-id*="-laborAmount-cell-${i}"] input`);
        if (await laborInput.count()) {
          console.log(`[PO]   Labor Price: ${item.laborPrice}`);
          await laborInput.fill(String(item.laborPrice.toFixed(2)));
        }
      }

      // Parts Price — number input
      if (item.partsPrice !== undefined && item.partsPrice > 0) {
        const partsInput = this.page.locator(`[data-test-id*="-partAmount-cell-${i}"] input`);
        if (await partsInput.count()) {
          console.log(`[PO]   Parts Price: ${item.partsPrice}`);
          await partsInput.fill(String(item.partsPrice.toFixed(2)));
        }
      }
    }

    // 7. Form filled — do not save or submit yet
    console.log('[PO] Sublet form filled. Not saving or submitting — review in browser.');
    await this.page.screenshot({ path: `sublet-filled-${Date.now()}.png`, fullPage: true }).catch(() => {});
    return { poNumber: 'PENDING', controlNumber: undefined, state: 'DRAFT' };
  }

  /**
   * Route an OCR payload to the correct PO creation method based on po_type.
   * Maps the flexible OCR JSON to the specific input types expected by each method.
   */
  async createPoFromOcr(payload: Record<string, any>): Promise<CreatedPo> {
    const poType = String(payload.po_type || '').toUpperCase();
    console.log(`[PO] createPoFromOcr -- po_type=${poType}`);

    await this.login();

    // Switch dealership — prefer dealer_name from Streamlit dropdown (exact Tekion name),
    // fall back to OCR dealership.name
    const dealerName = String(payload.dealer_name ?? payload.dealership?.name ?? '');
    if (dealerName) {
      await this.switchDealership(dealerName);
      // Navigate to PO list after dealership switch
      await this.openPurchaseOrders();
    } else {
      await this.openPurchaseOrders();
    }

    switch (poType) {
      case 'SUBLET': {
        const roNumber = String(payload.ro_number ?? payload.control_number ?? '');
        const items = (payload.po_line_items ?? payload.line_items ?? []).map((item: any) => ({
          roNumber: String(item.ro_number ?? roNumber),
          description: String(item.description ?? item.operation ?? ''),
          glAccount: String(item.gl_account ?? payload.accounting_entry?.gl_account ?? '2460 - Inv Sublet Repairs'),
          laborPrice: Number(item.labor_price ?? payload.summary?.labor_price ?? 0),
          partsPrice: Number(item.part_price ?? payload.summary?.part_price ?? 0),
        }));
        return this.createSubletOrder({
          vendorName: String(payload.vendor?.name ?? ''),
          vendorAddress: String(payload.vendor?.address ?? ''),
          vendorPhone: String(payload.vendor?.phone ?? ''),
          vendorEmail: String(payload.vendor?.email ?? ''),
          invoiceNumber: String(payload.invoice_number ?? payload.vendor_invoice_number ?? ''),
          items,
          submitStage: 'PRE_INVOICE',
        });
      }

      case 'MISCELLANEOUS': {
        const items = (payload.po_line_items ?? payload.line_items ?? []).map((item: any) => ({
          part: String(item.part ?? item.part_number ?? item.description ?? ''),
          qty: Number(item.qty ?? item.quantity ?? 1),
          unitPrice: Number(item.unit_price ?? item.part_price ?? item.extended_price ?? 0),
          glAccount: String(item.gl_account ?? ''),
          taxable: Boolean(item.taxable ?? false),
        }));
        const invoiceAmount = Number(
          payload.estimate_totals?.grand_total ??
          payload.invoice_amount ??
          payload.total ??
          (payload.po_line_items ?? payload.line_items ?? []).reduce(
            (sum: number, item: any) => sum + Number(item.extended_price ?? item.unit_price ?? 0), 0
          )
        );
        return this.createMiscOrder({
          vendorName: String(payload.vendor?.name ?? ''),
          vendorAddress: String(payload.vendor?.address ?? ''),
          vendorPhone: String(payload.vendor?.phone ?? ''),
          vendorEmail: String(payload.vendor?.email ?? ''),
          invoiceNumber: String(payload.invoice_number ?? payload.vendor_invoice_number ?? ''),
          controlNumber: String(payload.control_number ?? payload.ro_number ?? ''),
          invoiceDate: String(payload.invoice_date ?? payload.print_date ?? ''),
          invoiceDueDate: String(payload.invoice_due_date ?? ''),
          invoiceAmount,
          items,
          submitStage: 'PRE_INVOICE',
        });
      }

      default:
        throw new Error(`Automation not yet implemented for po_type: ${poType}`);
    }
  }

  /**
   * Attach the vendor-invoice document and create the Vendor Invoice.
   * Uses Playwright's native file handling so it works whether Tekion uses a
   * direct multipart POST or a presigned-URL upload.
   */
  async uploadInvoiceDocument(input: DocumentUploadInput): Promise<void> {
    console.log(`[INVOICE] Creating Vendor Invoice + attaching ${input.filePath}...`);
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
    console.log('[INVOICE] Invoice details submitted; document attached.');
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

  /**
   * Fill a vendor autocomplete and disambiguate duplicate vendor names by
   * matching each dropdown option's visible address/phone against the OCR
   * vendor's address/phone. Note: this cannot disambiguate vendor records
   * that have identical name/address/phone and differ only by internal
   * vendor code -- in that case the first option is used and a warning is
   * logged for manual verification.
   */
  private async fillVendorAutocomplete(
    selector: string,
    vendorName: string,
    vendorAddress?: string,
    vendorPhone?: string,
  ): Promise<void> {
    // Parse vendor code prefix if present (e.g. "1710_CSC - COLLISION SERVICE CENTER")
    const codeMatch = vendorName.match(/^(\d{3,5}_[A-Za-z0-9]+)\s*[-–]\s*(.+)$/);
    const vendorCode = codeMatch?.[1] ?? '';
    const cleanName = codeMatch?.[2] ?? vendorName;

    const input = this.page.locator(selector).first();
    await input.click();
    await input.fill('');

    // Search using first 1-2 words of the clean name (not the code)
    // Use pressSequentially to trigger React onChange filtering
    const searchTerm = cleanName.split(/\s+/).slice(0, 2).join(' ');
    await input.pressSequentially(searchTerm, { delay: 50 });
    await this.page.waitForTimeout(1500);

    const options = this.page.locator('[role="option"], li[role="option"]');
    await options.first().waitFor({ state: 'visible', timeout: 10_000 });
    const count = await options.count();

    if (count <= 1) {
      console.log(`[PO] Vendor "${cleanName}": ${count} match(es), no disambiguation needed`);
      await options.first().click();
      return;
    }

    // If vendor code is present, do direct full-text match — no phone/address needed
    if (vendorCode) {
      console.log(`[PO] Vendor "${cleanName}": ${count} matches, looking for code "${vendorCode}"...`);
      for (let i = 0; i < count; i++) {
        const text = (await options.nth(i).innerText().catch(() => '')) ?? '';
        if (text.includes(vendorCode)) {
          console.log(`[PO]   Matched option[${i}]: "${text.replace(/\s+/g, ' ').trim()}"`);
          await options.nth(i).click();
          return;
        }
      }
      console.log(`[PO]   No option contained code "${vendorCode}", falling back to first option -- VERIFY MANUALLY`);
      await options.first().click();
      return;
    }

    // No vendor code — fall back to phone/address scoring
    console.log(`[PO] Vendor "${cleanName}": ${count} matches found, disambiguating by address/phone...`);

    const normPhone = (p?: string) => (p ?? '').replace(/\D/g, '');
    const normAddr = (a?: string) => (a ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
    const targetPhone = normPhone(vendorPhone);
    const targetAddr = normAddr(vendorAddress);

    let bestIdx = 0;
    let bestScore = -1;
    for (let i = 0; i < count; i++) {
      const text = (await options.nth(i).innerText().catch(() => '')) ?? '';
      const optPhone = normPhone(text);
      const optAddr = normAddr(text);
      let score = 0;
      if (targetPhone && optPhone.includes(targetPhone)) score += 2;
      if (targetAddr && optAddr.includes(targetAddr.slice(0, 12))) score += 1;
      console.log(`[PO]   option[${i}]: "${text.replace(/\s+/g, ' ').trim()}" -- score=${score}`);
      if (score > bestScore) {
        bestScore = score;
        bestIdx = i;
      }
    }

    if (bestScore <= 0) {
      console.log(`[PO]   No confident match found (best score=${bestScore}), using first option -- VERIFY MANUALLY`);
    } else {
      console.log(`[PO]   Selected option[${bestIdx}] (score=${bestScore})`);
    }
    await options.nth(bestIdx).click();
  }

  /** Dump the captured endpoint map (useful after a run to confirm internal paths). */
  printApiLog(): void {
    console.log('\n-- Captured Tekion API calls this session --');
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

    console.log('\n[PO] Flow complete:', { oemPo, miscPo });
  } catch (err) {
    console.error('[PO] Flow failed:', err);
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
