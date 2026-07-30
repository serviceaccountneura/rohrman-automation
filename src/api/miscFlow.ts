/**
 * miscFlow.ts — End-to-end Miscellaneous PO creation via API from OCR JSON.
 *
 * Run:  npm run api:misc -- <ocr-json-path>
 *
 * Unlike Sublet POs, Misc POs have no RO/job requirement — a flat list of
 * line items billed directly to a vendor.
 *
 * Reads the OCR extraction JSON (from vision_extract.py), then:
 *   1. Login to Tekion
 *   2. Match dealership name from OCR → switch dealer
 *   3. Search vendor by name from OCR
 *   4. Create Misc PO
 *   5. Submit pre-invoice
 *
 * Env: TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET
 */
import 'dotenv/config';
import { TekionApiClient } from './tekionApi.js';
import {
  loadOcrJson,
  getVendorName,
  getDealershipName,
  getInvoiceNumber,
  getTotalAmount,
  getRawLineItems,
  getSalesTax,
} from './ocrHelpers.js';

// GL account defaults — general expense = 0021, AP liability = 3002
const DEFAULT_MISC_GL = '0021';
const AP_GL = '3002';

/** Extract misc-specific details from OCR data (for LLM classification). */
function getMiscDetails(ocr: any): { glAccount: string } {
  const md = ocr?.misc_details ?? {};
  return {
    glAccount: md.gl_account ?? DEFAULT_MISC_GL,
  };
}

async function main() {
  const ocrPath = process.argv[2];
  if (!ocrPath) {
    console.error('Usage: npm run api:misc -- <ocr-json-path>');
    process.exit(1);
  }

  const ocr = loadOcrJson(ocrPath);
  const dealershipName = getDealershipName(ocr);
  const vendorName = getVendorName(ocr);
  const invoiceNumber = getInvoiceNumber(ocr);
  const totalAmount = getTotalAmount(ocr);
  const salesTax = getSalesTax(ocr);
  const lineItems = getRawLineItems(ocr);
  const misc = getMiscDetails(ocr);

  console.log('═══════════════════════════════════════════════════════════════');
  console.log('  Misc PO Creation via API');
  console.log('═══════════════════════════════════════════════════════════════');
  console.log(`  Dealership:  ${dealershipName}`);
  console.log(`  Vendor:      ${vendorName}`);
  console.log(`  Invoice #:   ${invoiceNumber}`);
  console.log(`  Total:       $${totalAmount.toFixed(2)}`);
  console.log(`  Line items:  ${lineItems.length}`);
  console.log(`  GL Account:  ${misc.glAccount}`);
  console.log('═══════════════════════════════════════════════════════════════\n');

  if (!vendorName) throw new Error('No vendor name found in OCR data');
  if (!totalAmount) throw new Error('No total amount found in OCR data');

  const client = new TekionApiClient();

  // 1. Login
  console.log('\n── Step 1: Login ──────────────────────────────────────────────');
  const dealers = await client.login();

  // 2. Switch dealer based on OCR dealership name
  console.log('\n── Step 2: Dealer Selection ───────────────────────────────────');
  if (dealershipName) {
    const dealerId = client.findDealerByName(dealershipName);
    if (dealerId) {
      console.log(`  Matched "${dealershipName}" → dealerId=${dealerId}`);
      await client.switchDealer(dealerId);
    } else {
      console.warn(`  ⚠ Could not match "${dealershipName}" to any dealer. Using default.`);
      console.warn('  Available dealers:');
      dealers.forEach(d => console.warn(`    ${d.dealerId} - ${d.dealerDisplayName}`));
    }
  } else {
    console.log('  No dealership name in OCR, using default dealer.');
  }

  // 3. Search vendor
  console.log('\n── Step 3: Vendor Search ──────────────────────────────────────');
  const vendors = await client.searchVendor(vendorName);
  if (!vendors.length) {
    const shortName = vendorName.split(/\s+/).slice(0, 2).join(' ');
    console.log(`  No results for "${vendorName}", trying "${shortName}"…`);
    const more = await client.searchVendor(shortName);
    if (!more.length) throw new Error(`No vendor found for "${vendorName}"`);
    vendors.push(...more);
  }
  const vendor = vendors[0];
  console.log(`  Found: ${vendor.name} (id=${vendor.id}, siteId=${vendor.siteId}, displayId=${vendor.displayId})`);

  // 4. Create Misc PO
  console.log('\n── Step 4: Create Misc PO ──────────────────────────────────────');
  const items = lineItems.length > 0
    ? lineItems.map(item => ({
        description: item.description,
        qty: item.qty,
        price: item.unitPrice || item.totalPrice,
      }))
    : [{ description: 'Misc purchase', qty: 1, price: totalAmount }];

  const po = await client.createMiscPO({
    vendorId: vendor.id,
    vendorName: vendor.name,
    vendorDisplayId: vendor.displayId,
    vendorSiteId: vendor.siteId,
    vendorPhone: vendor.phone,
    vendorEmail: vendor.email,
    items,
  });

  // 5. Pre-invoice
  console.log('\n── Step 5: Pre-Invoice ───────────────────────────────────────');
  const dealerId = client.currentDealerId;
  const glAccountId = `${dealerId}_${misc.glAccount}`;
  const apGlAccountId = `${dealerId}_${AP_GL}`;

  const result = await client.preInvoice({
    vendorId: vendor.id,
    vendorSiteId: vendor.siteId,
    vendorName: vendor.name,
    vendorDisplayId: vendor.displayId,
    dealerId,
    poId: po.poId,
    poNumber: po.poNumber,
    universalId: po.universalId,
    invoiceNumber,
    invoiceAmount: totalAmount,
    glAccountId,
    apGlAccountId,
    refText: '',
    poType: 'MISCELLANEOUS',
    salesTax,
  });

  console.log('\n═══════════════════════════════════════════════════════════════');
  console.log('  ✅ Misc PO Created Successfully');
  console.log('═══════════════════════════════════════════════════════════════');
  console.log(`  PO Number:    ${po.poNumber}`);
  console.log(`  PO ID:        ${po.poId}`);
  console.log(`  PO Status:    ${po.status}`);
  console.log(`  Invoice ID:   ${result.invoiceId}`);
  console.log(`  Vendor:       ${vendor.name}`);
  console.log(`  Invoice #:    ${invoiceNumber}`);
  console.log(`  Amount:       $${totalAmount.toFixed(2)}`);
  console.log(`  GL Account:   ${glAccountId}`);
  console.log('═══════════════════════════════════════════════════════════════\n');
}

main().catch(e => { console.error('❌', e); process.exit(1); });
