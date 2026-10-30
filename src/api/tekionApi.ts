/**
 * tekionApi.ts — Direct API client for Tekion (no browser automation).
 *
 * Replaces the Playwright clicking approach with direct REST calls captured
 * from the HAR. Faster, more reliable, no selector/timing issues.
 *
 * Flow:
 *   1. login()          → identity-provider → password → MFA → get dealer list + tokens
 *   2. switchDealer()   → generateToken with target dealerId
 *   3. searchVendor()   → lookup/search VENDOR by name
 *   4. searchRO()       → lookup/search REPAIR_ORDER_ASSET by RO number
 *   5. getROJobs()      → service-module/u/ro/{roId} → get job list (jobId, jobNumber)
 *   6. createSubletPO() → partTrade/u/sublet/order/v2/create
 *   7. preInvoice()     → getInvoiceDate → invoiceDueDate → postings → preInvoice/post
 *
 * Env: TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET
 */
import 'dotenv/config';
import { authenticator } from 'otplib';
import { randomUUID } from 'crypto';

const BASE = 'https://app.tekioncloud.com';
const TENANT = 'rohrmanautomotivegroup';

// ─── Types ────────────────────────────────────────────────────────────────────

export interface DealerInfo {
  dealerId: string;
  dealerDisplayName: string;
  dealerChars: string;
}

export interface VendorResult {
  id: string;
  name: string;
  displayId: string;
  siteId: string;
  phone: string;
  email: string;
}

export interface ROResult {
  id: string;
  roNumber: string;
  status: string;
}

export interface JobInfo {
  id: string;          // jobId (Mongo ObjectId)
  jobNumber: string;   // e.g. "1", "A", "BB"
  payType: string;
  subPayType: string;
  roId: string;
  roNumber: string;
}

export interface CreatedSubletPO {
  poId: number;
  poNumber: string;
  universalId: string;   // e.g. "SUBLET%9589"
  status: string;
  orderType: string;
}

export interface CreatedMiscPO {
  poId: number;
  poNumber: string;
  universalId: string;   // e.g. "MISCELLANEOUS%10144"
  status: string;
  orderType: string;
}

export interface PreInvoiceResult {
  invoiceId: string;
  status: string;
}

// ─── Client ───────────────────────────────────────────────────────────────────

export class TekionApiClient {
  private cookies: string = '';
  private apiToken: string = '';
  private userId: string = '';
  private dealerId: string = '';
  private siteId: string = '';
  dealers: DealerInfo[] = [];

  /** Currently active dealerId (after login/switchDealer). */
  get currentDealerId(): string {
    return this.dealerId;
  }

  /** Make an HTTP request with session cookies. */
  private async req(
    path: string,
    options: { method?: string; body?: unknown; headers?: Record<string, string> } = {},
  ): Promise<Response> {
    const { method = 'GET', body, headers = {} } = options;
    const url = path.startsWith('http') ? path : `${BASE}${path}`;

    const reqHeaders: Record<string, string> = {
      'Content-Type': 'application/json',
      'clientid': 'web',
      'Accept': 'application/json, text/plain, */*',
      'Origin': BASE,
      'Referer': `${BASE}/`,
      ...headers,
    };
    if (this.cookies) reqHeaders['Cookie'] = this.cookies;
    // Session-context headers required by /api/* endpoints once logged in.
    // These mirror what the browser sends on every authenticated call.
    if (this.apiToken) reqHeaders['tekion-api-token'] = this.apiToken;
    if (this.userId) {
      reqHeaders['userid'] = this.userId;
      reqHeaders['original-userid'] = this.userId;
    }
    if (this.dealerId) {
      reqHeaders['dealerid'] = this.dealerId;
      reqHeaders['roleid'] = `${this.dealerId}_APClerk`;
      reqHeaders['tek-siteid'] = this.siteId;
    }
    reqHeaders['applicationid'] = 'ARC_NA';
    reqHeaders['productids'] = 'ARC';
    reqHeaders['program'] = 'DEFAULT';
    reqHeaders['subapplicationid'] = 'US';
    reqHeaders['locale'] = 'en_US';
    reqHeaders['original-tenantid'] = TENANT;
    reqHeaders['tenantname'] = TENANT;

    const res = await fetch(url, {
      method,
      headers: reqHeaders,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

    // Capture Set-Cookie headers for session management
    let setCookies: string[] = [];
    try {
      setCookies = (res.headers as any).getSetCookie?.() ?? [];
    } catch {
      // Fallback: parse raw set-cookie header
      const raw = res.headers.get('set-cookie');
      if (raw) setCookies = raw.split(/,(?=\s*\w+=)/);
    }
    if (setCookies.length > 0) {
      const newCookies = setCookies.map((c: string) => c.split(';')[0]).join('; ');
      this.cookies = this.cookies
        ? `${this.cookies}; ${newCookies}`
        : newCookies;
      console.log(`[API] Cookies captured: ${setCookies.map(c => c.split(';')[0].split('=')[0]).join(', ')}`);
    }

    return res;
  }

  private async reqJson<T = any>(
    path: string,
    options: { method?: string; body?: unknown; headers?: Record<string, string> } = {},
  ): Promise<T> {
    const res = await this.req(path, options);
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`${options.method ?? 'GET'} ${path} → ${res.status}: ${text.slice(0, 300)}`);
    }
    return res.json() as Promise<T>;
  }

  // ── Auth ──────────────────────────────────────────────────────────────────

  /** Full login flow: identity → password → MFA. Returns dealer list. */
  async login(): Promise<DealerInfo[]> {
    const email = process.env.TEKION_USERNAME!;
    const password = process.env.TEKION_PASSWORD!;
    const totpSecret = process.env.TEKION_TOTP_SECRET!;

    if (!email || !password || !totpSecret) {
      throw new Error('TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET required in .env');
    }

    // Step 1: identity-provider (just returns provider type, no token)
    console.log('[API] Login: identity-provider…');
    await this.reqJson('/api/loginservice/p/identity-provider', {
      method: 'POST',
      body: { email },
    });

    // Step 2: password (returns mfaToken + userId)
    console.log('[API] Login: password…');
    const pwRes = await this.reqJson('/api/loginservice/p/authenticate/password', {
      method: 'POST',
      body: { email, password },
    });
    const pwData = (pwRes as any)?.data;
    const mfaResponse = pwData?.mfaResponse ?? pwData;
    const mfaToken = mfaResponse?.mfaToken;
    const userId = mfaResponse?.userId;
    if (!mfaToken) throw new Error('No mfaToken in password response');
    if (!userId) throw new Error('No userId in password response');

    // Step 3: MFA
    const otp = authenticator.generate(totpSecret.replace(/\s+/g, ''));
    console.log(`[API] Login: MFA (otp=${otp})…`);
    const mfaRes = await this.reqJson('/api/loginservice/p/authenticate/mfa', {
      method: 'POST',
      body: {
        tenantId: TENANT,
        userId,
        mfaToken,
        authenticatorType: 'GOOGLE_AUTHENTICATOR',
        otp,
      },
    });

    const loginData = (mfaRes as any)?.data?.loginData ?? (mfaRes as any)?.data?.mfaResponse?.loginData;
    if (!loginData) throw new Error('No loginData in MFA response');

    this.dealerId = loginData.dealerId;
    this.userId = loginData.id ?? userId;
    this.apiToken = loginData.access_token ?? '';
    this.dealers = (loginData.dealer ?? []).map((d: any) => ({
      dealerId: d.dealerId,
      dealerDisplayName: d.dealerDisplayName,
      dealerChars: d.dealerChars,
    }));
    this.siteId = `-1_${this.dealerId}`;

    console.log(`[API] Logged in as ${loginData.displayName}, default dealer: ${this.dealerId}`);
    console.log(`[API] Available dealers: ${this.dealers.length}`);

    return this.dealers;
  }

  /** Switch dealer context. The tekion-api-token is dealer-agnostic; only the
   * dealerid/roleid/tek-siteid headers (attached automatically in req()) change. */
  async switchDealer(dealerId: string): Promise<void> {
    this.dealerId = dealerId;
    this.siteId = `-1_${dealerId}`;
    console.log(`[API] Switched to dealer ${dealerId}`);
  }

  /** Find dealer by name (fuzzy match). Returns dealerId or null. */
  findDealerByName(name: string): string | null {
    const lower = name.toLowerCase().trim();
    // Exact match first
    const exact = this.dealers.find(d => d.dealerDisplayName.toLowerCase() === lower);
    if (exact) return exact.dealerId;
    // Contains match
    const partial = this.dealers.find(d =>
      d.dealerDisplayName.toLowerCase().includes(lower) ||
      lower.includes(d.dealerDisplayName.toLowerCase()),
    );
    if (partial) return partial.dealerId;
    // Keyword match — check if key words from the name appear in dealer name
    const keywords = lower.split(/\s+/).filter(w => w.length > 2);
    const keywordMatch = this.dealers.find(d => {
      const dn = d.dealerDisplayName.toLowerCase();
      return keywords.every(k => dn.includes(k));
    });
    return keywordMatch?.dealerId ?? null;
  }


  // ── Lookups ───────────────────────────────────────────────────────────────

  /** Search vendors by name. Returns list of matching vendors. */
  async searchVendor(searchText: string): Promise<VendorResult[]> {
    const res = await this.reqJson('/api/lookup/search', {
      method: 'POST',
      body: {
        VENDOR: {
          filters: [
            { field: 'vendorStatus', operator: 'NIN', values: ['DRAFT', 'INACTIVE', 'ON_HOLD'], key: 'vendorStatus' },
          ],
          searchText,
          pageInfo: { start: 0, rows: 20 },
          sort: [],
        },
      },
    });

    const results = this.extractLookupResults(res, 'VENDOR');
    return results.map((v: any) => {
      const site = (v.sites ?? [])[0] ?? {};
      const contact = (site.pointOfContacts ?? [])[0] ?? {};
      return {
        id: String(v.id ?? ''),
        name: v.vendorName ?? '',
        displayId: v.vendorDisplayId ?? '',
        siteId: String(site.id ?? ''),
        phone: contact.phone ?? contact.mobile ?? '',
        email: contact.email ?? '',
      };
    });
  }

  /** Search repair orders by RO number. Returns list of matching ROs. */
  async searchRO(roNumber: string): Promise<ROResult[]> {
    const res = await this.reqJson('/api/lookup/search', {
      method: 'POST',
      body: {
        REPAIR_ORDER_ASSET: {
          filters: [
            { field: 'status', operator: 'NIN', values: ['INVOICED', 'CLOSED', 'VOIDED'] },
            { field: 'siteId', operator: 'IN', values: [this.siteId] },
          ],
          searchText: roNumber,
          pageInfo: { start: 0, rows: 20 },
          sort: [],
        },
      },
    });

    const results = this.extractLookupResults(res, 'REPAIR_ORDER_ASSET', 'displayValue');
    return results.map((r: any) => ({
      id: String(r.id ?? ''),
      roNumber: String(r._displayValue ?? r.roNumber ?? r.roNo ?? ''),
      status: r.status ?? '',
    }));
  }

  /** Get jobs for an RO. Returns job list with jobId, jobNumber, payType. */
  async getROJobs(roId: string): Promise<JobInfo[]> {
    const res = await this.reqJson(`/api/service-module/u/ro/${roId}`, {
      method: 'POST',
      body: ['JOB', 'RECOMMENDATION'],
    });

    const jobs = (res as any)?.data?.jobs ?? [];
    return jobs.map((j: any) => ({
      id: j.id,
      jobNumber: String(j.jobNumber ?? ''),
      payType: j.payType ?? '',
      subPayType: j.subPayType ?? '',
      roId: j.roId ?? roId,
      roNumber: j.roNo ?? '',
    }));
  }

  // ── Sublet PO Creation ────────────────────────────────────────────────────

  /** Create a Sublet PO. Returns PO details from the response. */
  async createSubletPO(input: {
    vendorId: number;
    vendorName: string;
    vendorDisplayId: string;
    vendorSiteId: string;
    vendorPhone?: string;
    vendorEmail?: string;
    items: Array<{
      description: string;
      jobId: string;
      referenceId: string;     // RO ID
      referenceNumber: string; // RO number
      opcode: string;          // e.g. "DPS", "SUBLET"
      serviceTypeIds?: string[];
      category?: string;       // e.g. "MISCELLANEOUS"
      glAccountId?: string;
      laborAmount: number;
      partsAmount: number;
    }>;
  }): Promise<CreatedSubletPO> {
    const itemsToAdd = input.items.map(item => ({
      description: item.description,
      jobId: item.jobId,
      invoiceId: null,
      invoiceAmount: null,
      partTaxable: false,
      laborTaxable: false,
      glAccountId: item.glAccountId ?? '',
      itemId: item.jobId,
      itemType: 'job',
      referenceId: item.referenceId,
      referenceNumber: item.referenceNumber,
      subletOpcodeDetail: {
        opcode: item.opcode,
        serviceTypeIds: item.serviceTypeIds ?? [],
        category: item.category ?? 'MISCELLANEOUS',
        subletMarkUpDetails: [],
      },
      poLineItemLaborAmount: item.laborAmount,
      poLineItemPartsAmount: item.partsAmount,
    }));

    const body = {
      action: 'SUBMITTED',
      poTaxConfiguration: { taxCodeGrid: [], taxDetails: [], flatTaxDetails: [] },
      newTaxCodeEnabled: false,
      taxable: false,
      taxDetails: [
        { taxRegimeType: 'SALES_TAX', partTaxPercentage: 10, laborTaxPercentage: 10 },
      ],
      taxOverridden: false,
      flatTaxApplied: false,
      vendorId: input.vendorId,
      vendorName: input.vendorName,
      vendorDisplayId: input.vendorDisplayId,
      vendorSiteId: input.vendorSiteId,
      vendorPhone: input.vendorPhone ?? '',
      vendorEmail: input.vendorEmail ?? '',
      estimateDeliveryTime: null,
      expectedPickupDate: '',
      temporaryVendor: false,
      vendorAddress: {},
      billingAddress: {},
      itemsToAdd,
      itemsToUpdate: [],
      itemsToDelete: [],
      printConfigDto: {
        copyTypes: ['VENDOR', 'DEALER'],
        sortDetails: null,
        copyTypeVsLocale: null,
      },
    };

    console.log('[API] Creating Sublet PO…');
    const res = await this.reqJson('/api/partTrade/u/sublet/order/v2/create', {
      method: 'POST',
      body,
    });

    const data = (res as any)?.data;
    if (!data) throw new Error('No data in sublet create response');

    const poId = data.id;
    const poNumber = data.orderNumber;
    const universalId = `SUBLET%${poId}`;

    console.log(`[API] Sublet PO created: ${poNumber} (id=${poId}, status=${data.status})`);

    return {
      poId,
      poNumber,
      universalId,
      status: data.status,
      orderType: data.orderType,
    };
  }

  // ── Misc PO Creation ──────────────────────────────────────────────────────

  /** Create a Miscellaneous PO. No RO/job required — a flat list of line items
   * billed straight to the vendor. */
  async createMiscPO(input: {
    vendorId: string;
    vendorName: string;
    vendorDisplayId: string;
    vendorSiteId: string;
    vendorPhone?: string;
    vendorEmail?: string;
    shippingAddress?: Record<string, unknown>;
    billingAddress?: Record<string, unknown>;
    items: Array<{
      description: string;
      qty: number;
      price: number;         // unit price
      unit?: string;         // default "ea"
    }>;
  }): Promise<CreatedMiscPO> {
    const items = input.items.map(item => ({
      grossTotalPrice: Math.round(item.qty * item.price * 100) / 100,
      item: item.description,
      id: randomUUID(),
      qty: item.qty,
      unit: item.unit ?? 'ea',
      price: item.price,
      additional: {},
      charges: [],
    }));

    const body = {
      vendorSiteId: input.vendorSiteId,
      vendorId: input.vendorId,
      vendorPhone: input.vendorPhone ?? '',
      vendorEmail: input.vendorEmail ?? '',
      vendorName: input.vendorName,
      vendorDisplayId: input.vendorDisplayId,
      vendorAddress: {},
      temporaryVendor: false,
      estimateDeliveryTime: null,
      shippingAddress: input.shippingAddress ?? {},
      billingAddress: input.billingAddress ?? {},
      poAssetType: 'misc',
      companyId: 1,
      parts: false,
      items,
      poChargeToAdd: [],
      poChargeToUpdate: [],
      poChargeToDelete: [],
      status: 'READY_TO_SUBMIT',
      controlNumber: null,
      orderType: 'MISC',
      purchaseType: 'REGULAR',
      additional: {},
      newTaxCodeEnabled: false,
      poTaxConfiguration: { taxCodeGrid: [], taxDetails: [], flatTaxDetails: [] },
      printConfigDto: {
        copyTypes: ['VENDOR', 'DEALER'],
        sortDetails: null,
        copyTypeVsLocale: null,
      },
    };

    console.log('[API] Creating Misc PO…');
    const res = await this.reqJson('/api/partTrade/u/misc/order', {
      method: 'POST',
      body,
    });

    const data = (res as any)?.data;
    if (!data) throw new Error('No data in misc order create response');

    const poId = data.id;
    const poNumber = data.orderNumber;
    const universalId = `MISCELLANEOUS%${poId}`;

    console.log(`[API] Misc PO created: ${poNumber} (id=${poId}, status=${data.status})`);

    return {
      poId,
      poNumber,
      universalId,
      status: data.status,
      orderType: data.orderType,
    };
  }

  // ── Pre-Invoice Flow ──────────────────────────────────────────────────────

  /** Run the full pre-invoice flow: getInvoiceDate → dueDate → postings → post. */
  async preInvoice(input: {
    vendorId: string;
    vendorSiteId: string;
    vendorName: string;
    vendorDisplayId: string;
    dealerId: string;
    poId: number;
    poNumber: string;
    universalId: string;
    invoiceNumber: string;
    invoiceAmount: number;      // in dollars (will be converted to cents)
    invoiceDate?: string;       // MM/DD/YYYY — if omitted, uses today
    glAccountId: string;        // e.g. "1707_2460"
    apGlAccountId: string;      // e.g. "1707_3002"
    refText: string;            // RO number, or '' for misc
    poType: string;             // "SUBLET" | "MISCELLANEOUS"
    salesTax?: number;          // in dollars
  }): Promise<PreInvoiceResult> {
    const { vendorId, vendorSiteId, dealerId, poId, poNumber, universalId } = input;

    // Amounts in cents. accountingDetails posts the pre-tax subtotal; salesTax
    // is tracked separately so subtotal + salesTax = invoiceAmount.
    const amountCents = Math.round(input.invoiceAmount * 100);
    const taxCents = Math.round((input.salesTax ?? 0) * 100);
    const subtotalCents = amountCents - taxCents;

    // Step 1: Get invoice date
    console.log('[API] Pre-invoice: getInvoiceDate…');
    const dateRes = await this.reqJson('/api/accounting/u/poInvoice/getInvoiceDate', {
      method: 'POST',
      body: { type: 'INVOICE', vendorId },
    });
    let invoiceDateMs = (dateRes as any)?.data?.invoiceDate;
    if (!invoiceDateMs) {
      // Use provided date or today
      const dateStr = input.invoiceDate ?? new Date().toLocaleDateString('en-US');
      invoiceDateMs = Date.parse(dateStr);
    }
    console.log(`[API] Invoice date: ${new Date(invoiceDateMs).toLocaleDateString()}`);

    // Step 2: Get due date
    console.log('[API] Pre-invoice: invoiceDueDate…');
    const dueRes = await this.reqJson('/api/accounting/u/poInvoice/invoiceDueDate', {
      method: 'POST',
      body: { invoiceDate: invoiceDateMs, vendorId, vendorSiteId: Number(vendorSiteId) },
    });
    const dueDateMs = (dueRes as any)?.data?.invoiceDueDate ?? (invoiceDateMs + 30 * 24 * 60 * 60 * 1000);
    console.log(`[API] Due date: ${new Date(dueDateMs).toLocaleDateString()}`);

    // Step 3: Get postings
    console.log('[API] Pre-invoice: postings…');
    await this.reqJson('/api/accounting/u/poInvoice/preInvoicing/postings', {
      method: 'POST',
      body: {
        vendorId,
        poUniversalIds: [universalId],
        invoiceAmount: { amount: subtotalCents, currency: 'USD' },
        apGlAccountId: input.apGlAccountId,
        poNumbers: [poNumber],
      },
    });

    // Step 4: Post pre-invoice
    console.log('[API] Pre-invoice: post…');
    const postRes = await this.reqJson('/api/accounting/u/poInvoice/preInvoice/post', {
      method: 'POST',
      body: {
        dealerId,
        payeeId: Number(vendorId),
        payeeType: 'VENDOR',
        vendorSiteId,
        invoiceNumber: input.invoiceNumber,
        invoiceAmount: { amount: amountCents, currency: 'USD' },
        invoiceDate: invoiceDateMs,
        invoiceDueDate: dueDateMs,
        salesTax: { amount: taxCents, currency: 'USD' },
        taxes: [{ type: 'SALES_TAX', amount: taxCents }],
        fees: { amount: 0, currency: 'USD' },
        shippingCharges: { amount: 0, currency: 'USD' },
        discount: { amount: 0, currency: 'USD' },
        attachments: [],
        transactionDetails: null,
        accountingDetails: [
          {
            description: null,
            glAccountId: input.glAccountId,
            postingAmount: { amount: subtotalCents, currency: 'USD' },
            controlNumberList: null,
            refText: input.refText,
          },
        ],
        type: 'INVOICE',
        payeeNumber: input.vendorDisplayId,
        payeeName: input.vendorName,
        apGlAccountId: input.apGlAccountId,
        poDetails: [
          {
            poId,
            poNum: poNumber,
            poType: input.poType,
            universalId,
            poInvoicedAmount: { amount: subtotalCents, currency: 'USD' },
          },
        ],
      },
    });

    const postData = (postRes as any)?.data ?? (postRes as any);
    const invoiceId = postData?.id ?? postData?.invoiceId ?? 'unknown';
    console.log(`[API] Pre-invoice posted: invoiceId=${invoiceId}`);

    return { invoiceId: String(invoiceId), status: 'posted' };
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  /** Extract results from a lookup/search response. The real shape is
   * data.{entityType}.entities[] = [{ id, displayValue, data: {...} }]. */
  private extractLookupResults(res: any, entityType: string, displayValueField?: string): any[] {
    const entities = res?.data?.[entityType]?.entities;
    if (Array.isArray(entities)) {
      return entities.map((e: any) => {
        const d = e.data ?? e;
        if (displayValueField) d[`_${displayValueField}`] = e.displayValue;
        return d;
      });
    }
    // Fallback shapes seen elsewhere
    if (res?.data?.[entityType]?.results) return res.data[entityType].results;
    if (res?.data?.results) return res.data.results;
    if (Array.isArray(res?.data)) return res.data;
    if (res?.[entityType]?.results) return res[entityType].results;
    if (Array.isArray(res?.data?.[entityType])) return res.data[entityType];
    console.warn(`[API] Could not extract results for ${entityType} from:`, JSON.stringify(res).slice(0, 200));
    return [];
  }
}
