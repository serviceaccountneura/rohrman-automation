/**
 * uploadInvoice.ts — Attach an invoice image/PDF to a Tekion record via the UI.
 *
 * File uploads are best driven through Playwright's native file handling rather
 * than replaying raw multipart/presigned calls: it works whether Tekion uses a
 * direct multipart POST or a presigned-URL flow, because we let the page do
 * exactly what it does for a human.
 *
 * Run:  tsx src/uploadInvoice.ts <imagePath> <poUrl> [uploadButtonSelector]
 *   e.g. tsx src/uploadInvoice.ts ./invoices/INV-558720.pdf \
 *          "https://app.tekioncloud.com/parts/purchase-orders/PO-2026-004417"
 *
 * Reuses auth.json (run `npm run auth` first). Runs headed so you can watch and
 * confirm the upload took. Adjust the two selectors below once you've inspected
 * the actual upload widget on the live page (they are placeholders).
 */
import 'dotenv/config';
import { chromium } from 'playwright';
import { existsSync } from 'node:fs';
import { proxyFromEnv } from './proxy.js';

const BASE_URL = process.env.TEKION_BASE_URL || 'https://app.tekioncloud.com';

// ── Confirm these against the real upload widget (DevTools → inspect the button
//    and the hidden <input type="file">). ────────────────────────────────────
const DEFAULT_UPLOAD_BUTTON = 'text=/upload|attach|add document/i';
const DEFAULT_FILE_INPUT = 'input[type="file"]';

export type UploadArgs = {
  filePath: string;
  poUrl: string;
  /** the visible button that opens the file picker */
  uploadButtonSelector?: string;
  /** the underlying <input type=file>, if it can be set directly */
  fileInputSelector?: string;
  headless?: boolean;
};

export async function uploadInvoiceImage(args: UploadArgs): Promise<void> {
  if (!existsSync('auth.json')) throw new Error('auth.json missing — run `npm run auth`.');
  if (!existsSync(args.filePath)) throw new Error(`File not found: ${args.filePath}`);

  const button = args.uploadButtonSelector || DEFAULT_UPLOAD_BUTTON;
  const fileInput = args.fileInputSelector || DEFAULT_FILE_INPUT;

  const browser = await chromium.launch({
    headless: args.headless ?? false,
    proxy: proxyFromEnv(),
  });
  const context = await browser.newContext({ storageState: 'auth.json' });
  const page = await context.newPage();

  await page.goto(args.poUrl, { waitUntil: 'domcontentloaded' });

  // Strategy 1: the file input exists in the DOM (even if visually hidden) →
  // set it directly. This is the most reliable when it works.
  const directInput = page.locator(fileInput).first();
  if (await directInput.count()) {
    await directInput.setInputFiles(args.filePath);
    console.log('📎 Set file on existing <input type="file">.');
  } else {
    // Strategy 2: clicking the button opens a native OS file chooser → intercept it.
    const [chooser] = await Promise.all([
      page.waitForEvent('filechooser'),
      page.click(button),
    ]);
    await chooser.setFiles(args.filePath);
    console.log('📎 Handled native file chooser via the upload button.');
  }

  // Give the upload (multipart or presigned PUT) time to finish; surface the call.
  page.on('requestfinished', (req) => {
    if (/attach|upload|document|file|presign|s3|storage/i.test(req.url()))
      console.log(`  [${req.method()}] ${req.url().replace(/\?.*$/, '')}`);
  });
  await page.waitForTimeout(4000);

  console.log('✅ Upload attempted. Verify the attachment is listed on the PO.');
  if (!(args.headless ?? false)) await page.waitForTimeout(3000);
  await browser.close();
}

// CLI entry
if (process.argv[1]?.endsWith('uploadInvoice.ts')) {
  const [filePath, poUrl, btn] = process.argv.slice(2);
  if (!filePath || !poUrl) {
    console.error('Usage: tsx src/uploadInvoice.ts <imagePath> <poUrl> [uploadButtonSelector]');
    process.exit(1);
  }
  await uploadInvoiceImage({ filePath, poUrl, uploadButtonSelector: btn });
}
