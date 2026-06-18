/**
 * examples/leadIntake.ts
 * ────────────────────────────────────────────────────────────────────
 * Take a single lead-intake payload (e.g. from a website form) and turn
 * it into a Tekion CRM record using only Open API endpoints:
 *
 *   - Create Lead
 *   - Create Lead Contact
 *   - Create Lead Vehicle (vehicle of interest)
 *   - Create Lead Trade-In (optional)
 *   - Create Lead Note
 *   - Create Lead Assignees (optional)
 *
 * Run:  tsx examples/leadIntake.ts ./lead.json
 */
import { readFileSync } from 'node:fs';
import { apcFromEnv } from '../src/apc.js';
import * as leads from '../src/openapi/leads.js';

async function main(): Promise<void> {
  const path = process.argv[2];
  if (!path) {
    console.error('Usage: tsx examples/leadIntake.ts <lead.json>');
    process.exit(1);
  }
  const payload = JSON.parse(readFileSync(path, 'utf8')) as leads.IntakePayload;

  const apc = apcFromEnv();
  console.log(`Submitting lead intake for: ${payload.contact?.email ?? '<no email>'}`);

  const out = await leads.intakeLead(apc, payload);

  console.log('\n── Created ────────────────────────────────────────');
  console.log(`Lead:        ${out.lead.leadId ?? '?'}`);
  if (out.contact)           console.log(`Contact:     ${out.contact.contactId ?? '?'}`);
  if (out.vehicleOfInterest) console.log(`Vehicle:     ${out.vehicleOfInterest.leadVehicleId ?? '?'}`);
  if (out.tradeIn)           console.log(`Trade-in:    ${out.tradeIn.tradeInId ?? '?'}`);
  if (out.note)              console.log(`Note:        ${out.note.noteId ?? '?'}`);
  if (out.assignees)         console.log(`Assignees:   ${out.assignees.length}`);
}

main().catch((e) => {
  console.error('leadIntake failed:', e);
  process.exit(1);
});
