/**
 * subletFlow.ts — End-to-end Sublet PO creation via API from OCR JSON.
 *
 * Run:  npm run api:sublet -- <ocr-json-path>
 *
 * Reads the OCR extraction JSON (from vision_extract.py), then:
 *   1. Login to Tekion
 *   2. Match dealership name from OCR → switch dealer
 *   3. Search vendor by name from OCR
 *   4. Search RO by control number from OCR
 *   5. Get RO jobs → pick job (by jobNumber or first available)
 *   6. Create Sublet PO
 *   7. Submit pre-invoice
 *
 * Env: TEKION_USERNAME, TEKION_PASSWORD, TEKION_TOTP_SECRET
 */
import 'dotenv/config';
import { TekionApiClient } from './tekionApi.js';
import {
  loadOcrJson,
  getVendorName,
  getDealershipName,
  getControlNumber,
  getInvoiceNumber,
  getTotalAmount,
  getRawLineItems,
  getSalesTax,
} from './ocrHelpers.js';

// GL account defaults — Sublet repairs = 2460, AP liability = 3002
const DEFAULT_SUBLET_GL = '2460';
const AP_GL = '3002';

/** Extract sublet-specific details from OCR data (for LLM classification). */
function getSubletDetails(ocr: any): {
  jobType: string | null;
  opcode: string;
  category: string;
  glAccount: string;
  laborAmount: number;
  partsAmount: number;
} {
  const sd = ocr?.sublet_details ?? {};
  return {
    jobType: sd.job_type ?? null,           // 'A' or 'BB' — LLM classifies
    opcode: sd.sublet_opcode ?? 'SUBLET',   // operation code
    category: sd.sublet_category ?? 'MISCELLANEOUS',
    glAccount: sd.gl_account ?? DEFAULT_SUBLET_GL,
    laborAmount: sd.labor_amount ?? 0,
    partsAmount: sd.parts_amount ?? 0,
  };
}

/** Map raw OCR line items to sublet items with labor/parts amounts. */
function getLineItems(ocr: any): Array<{ description: string; laborAmount: number; partsAmount: number }> {
  return getRawLineItems(ocr).map(item => {
    const amount = item.totalPrice || item.unitPrice;
    // If description contains "labor" treat as labor amount, else parts
    const desc = item.description.toLowerCase();
    const isLabor = desc.includes('labor') || desc.includes('r/r') || desc.includes('remove');
    return {
      description: item.description,
      laborAmount: isLabor ? amount : 0,
      partsAmount: isLabor ? 0 : amount,
    };
  });
}

async function main() {
  const ocrPath = process.argv[2];
  if (!ocrPath) {
    console.error('Usage: npm run api:sublet -- <ocr-json-path>');
    process.exit(1);
  }

  const ocr = loadOcrJson(ocrPath);
  const dealershipName = getDealershipName(ocr);
  const vendorName = getVendorName(ocr);
  const controlNumber = getControlNumber(ocr);
  const invoiceNumber = getInvoiceNumber(ocr);
  const totalAmount = getTotalAmount(ocr);
  const salesTax = getSalesTax(ocr);
  const lineItems = getLineItems(ocr);
  const sublet = getSubletDetails(ocr);

  console.log('═══════════════════════════════════════════════════════════════');
  console.log('  Sublet PO Creation via API');
  console.log('═══════════════════════════════════════════════════════════════');
  console.log(`  Dealership:  ${dealershipName}`);
  console.log(`  Vendor:      ${vendorName}`);
  console.log(`  RO Number:   ${controlNumber}`);
  console.log(`  Invoice #:   ${invoiceNumber}`);
  console.log(`  Total:       $${totalAmount.toFixed(2)}`);
  console.log(`  Line items:  ${lineItems.length}`);
  console.log(`  Job Type:    ${sublet.jobType ?? '(not classified)'}`);
  console.log(`  Sublet Op:   ${sublet.opcode}`);
  console.log(`  Category:    ${sublet.category}`);
  console.log(`  GL Account:  ${sublet.glAccount}`);
  console.log('═══════════════════════════════════════════════════════════════\n');

  if (!vendorName) throw new Error('No vendor name found in OCR data');
  if (!controlNumber) throw new Error('No RO/control number found in OCR data');
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
    // Try shorter search
    const shortName = vendorName.split(/\s+/).slice(0, 2).join(' ');
    console.log(`  No results for "${vendorName}", trying "${shortName}"…`);
    const more = await client.searchVendor(shortName);
    if (!more.length) throw new Error(`No vendor found for "${vendorName}"`);
    vendors.push(...more);
  }
  const vendor = vendors[0];
  console.log(`  Found: ${vendor.name} (id=${vendor.id}, siteId=${vendor.siteId}, displayId=${vendor.displayId})`);

  // 4. Search RO
  console.log('\n── Step 4: RO Search ─────────────────────────────────────────');
  const ros = await client.searchRO(controlNumber);
  if (!ros.length) throw new Error(`No RO found for "${controlNumber}"`);
  const ro = ros[0];
  console.log(`  Found: RO ${ro.roNumber} (id=${ro.id}, status=${ro.status})`);

  // 5. Get RO jobs
  console.log('\n── Step 5: RO Jobs ───────────────────────────────────────────');
  const jobs = await client.getROJobs(ro.id);
  if (!jobs.length) throw new Error('No jobs found on RO');
  console.log(`  Jobs on RO:`);
  jobs.forEach(j => console.log(`    ${j.jobNumber} - payType=${j.payType} (id=${j.id})`));
  // Pick job by jobType from OCR if available, else first job
  let job = jobs[0];
  if (sublet.jobType) {
    const matched = jobs.find(j => j.jobNumber === sublet.jobType);
    if (matched) {
      job = matched;
      console.log(`  Selected: job ${job.jobNumber} (matched by sublet_details.job_type)`);
    } else {
      console.warn(`  ⚠ Job type "${sublet.jobType}" not found on RO. Available: ${jobs.map(j => j.jobNumber).join(', ')}`);
      console.log(`  Selected: job ${job.jobNumber} (first available)`);
    }
  } else {
    console.log(`  Selected: job ${job.jobNumber} (first available — no job_type in OCR)`);
  }

  // 6. Create Sublet PO
  console.log('\n── Step 6: Create Sublet PO ──────────────────────────────────');
  const items = lineItems.length > 0 ? lineItems : [{ description: 'Sublet repair', laborAmount: 0, partsAmount: totalAmount }];

  const po = await client.createSubletPO({
    vendorId: Number(vendor.id),
    vendorName: vendor.name,
    vendorDisplayId: vendor.displayId,
    vendorSiteId: vendor.siteId,
    vendorPhone: vendor.phone,
    vendorEmail: vendor.email,
    items: items.map(item => ({
      description: item.description,
      jobId: job.id,
      referenceId: ro.id,
      referenceNumber: ro.roNumber,
      opcode: sublet.opcode,
      category: sublet.category,
      laborAmount: item.laborAmount,
      partsAmount: item.partsAmount,
    })),
  });

  // 7. Pre-invoice
  console.log('\n── Step 7: Pre-Invoice ───────────────────────────────────────');
  const dealerId = client.currentDealerId;
  const glAccountId = `${dealerId}_${sublet.glAccount}`;
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
    refText: ro.roNumber,
    poType: 'SUBLET',
    salesTax,
  });

  console.log('\n═══════════════════════════════════════════════════════════════');
  console.log('  ✅ Sublet PO Created Successfully');
  console.log('═══════════════════════════════════════════════════════════════');
  console.log(`  PO Number:    ${po.poNumber}`);
  console.log(`  PO ID:        ${po.poId}`);
  console.log(`  PO Status:    ${po.status}`);
  console.log(`  Invoice ID:   ${result.invoiceId}`);
  console.log(`  Vendor:       ${vendor.name}`);
  console.log(`  RO Number:    ${ro.roNumber}`);
  console.log(`  Invoice #:    ${invoiceNumber}`);
  console.log(`  Amount:       $${totalAmount.toFixed(2)}`);
  console.log(`  GL Account:   ${glAccountId}`);
  console.log('═══════════════════════════════════════════════════════════════\n');
}

main().catch(e => { console.error('❌', e); process.exit(1); });
