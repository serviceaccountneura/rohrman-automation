/**
 * updateLead.ts — Update a lead's source info.  PUT /v4.0.0/leads/{lead-id}
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run lead:update -- <leadId>
 *   e.g. npm run lead:update -- 1157502503886266296
 *
 * ▼▼▼  EDIT THE LEAD SOURCE FIELDS HERE  ▼▼▼
 * The leadId comes from the command line. This endpoint updates the lead's
 * `source` block (where the lead came from + external references).
 */
import { apcFromEnv } from '../src/apc.js';
import { updateLead, type UpdateLeadRequest } from '../src/openapi/leads.js';
import { printResult, printSummary } from './_print.js';

const BODY: UpdateLeadRequest = {
  source: {
    sourceType: 'INTERNET',
    sourceName: 'cardekho.com',
    subSource: 'sub-source-745',
    leadEvent: 'Event-620',
    leadPromotion: 'Promotion-047',
    externalLeadId: '123456',
  },
};
// ▲▲▲  END EDIT  ▲▲▲

async function main() {
  const leadId = process.argv[2];
  if (!leadId) {
    console.error('Usage: npm run lead:update -- <leadId>');
    process.exit(1);
  }
  const apc = apcFromEnv();
  const res = await updateLead(apc, leadId, BODY);
  printResult(`Update Lead ${leadId}`, res);
  if (res.ok) {
    const d = res.body.data;
    console.log('Key fields:');
    printSummary({
      'Status': d.status,
      'Stage': d.stage,
      'Department': d.department,
      'External Lead ID': d.externalLeadId,
      'OEM name': d.oemName,
      'Modified': d.modifiedTime ? new Date(d.modifiedTime).toISOString() : undefined,
    });
  }
  process.exit(res.ok ? 0 : 1);
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
