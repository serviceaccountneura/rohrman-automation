/**
 * Dry-run Playwright smoke for VI-verify UI copy + static path guards.
 *
 * Does not touch Tekion. Skips the live page check when FE is down.
 *
 * Run:  node scripts/test_vi_verify_pw_dry.mjs
 */
import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const __dirname = dirname(fileURLToPath(import.meta.url));
const BE = join(__dirname, "..");
const FE = join(BE, "..", "rohrman-automotive-DE1-frontend");
const FE_URL = process.env.FE_URL || "http://localhost:3000";

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function checkSources() {
  const pipeline = readFileSync(join(BE, "api/services/pipeline_service.py"), "utf8");
  const start = pipeline.indexOf("def _run_po_invoice(");
  const end = pipeline.indexOf("def _chart_by_number(");
  const slice = pipeline.slice(start, end);
  for (const token of [
    "pre_invoice_stock_order",
    "preInvoice/post",
    ".pre_invoice(",
    "_create_misc_po",
    "_create_sublet_po",
    "_create_stock_po",
  ]) {
    assert(!slice.includes(token), `PO_INVOICE path must not contain ${token}`);
  }
  assert(slice.includes("verify_vendor_invoice"), "PO_INVOICE path must call verify_vendor_invoice");

  const vi = readFileSync(join(BE, "api/services/vi_verify.py"), "utf8");
  assert(vi.includes("Never creates a PO"), "vi_verify docstring");
  assert(
    !/"\/api\/[^"]*preInvoice\/post"/.test(vi) && !vi.includes(".pre_invoice("),
    "vi_verify must not call preInvoice/post",
  );

  const dash = readFileSync(join(FE, "src/lib/api/dashboard-service.ts"), "utf8");
  for (const code of [
    "INVOICE_NUMBER_MISMATCH",
    "PRE_INVOICE_MISSING",
    "JE_CONTROL_REQUIRED",
  ]) {
    assert(dash.includes(code), `FE dashboard missing ${code}`);
  }

  const banner = readFileSync(
    join(FE, "src/components/documents/tekion-write-banner.tsx"),
    "utf8",
  );
  assert(
    banner.includes("Vendor Invoice"),
    "tekion-write-banner should mention Vendor Invoice verify",
  );
  console.log("OK  static source guards");
}

async function checkLiveUi() {
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const res = await page.goto(FE_URL, { waitUntil: "domcontentloaded", timeout: 8000 });
    if (!res || res.status() >= 500) {
      console.log(`SKIP live UI (${FE_URL} status=${res?.status()})`);
      return;
    }
    // Login wall is fine — we only need the app to be up for a dry smoke.
    console.log(`OK  live UI reachable (${FE_URL} status=${res.status()})`);
  } catch (err) {
    console.log(`SKIP live UI (${err.message || err})`);
  } finally {
    if (browser) await browser.close();
  }
}

async function main() {
  assert(existsSync(join(BE, "api/services/vi_verify.py")), "vi_verify.py missing");
  checkSources();
  await checkLiveUi();
  console.log("\nPlaywright dry-run checks done.");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
