/**
 * token.ts — Mint (or reuse) the 24h APC Bearer token.
 * ─────────────────────────────────────────────────────────────────────────────
 * Run:  npm run token
 *
 * Mints a token from APC_APP_ID + APC_SECRET_KEY and caches it on disk
 * (.tekion-token.json). If a still-valid token is already cached, it's reused
 * instead of minting a new one — so every other command (customer, vehicle,
 * deal, lead) shares the same 24h token and you stay well under the
 * 20-tokens-per-15-minutes limit.
 *
 * Nothing to fill in here — credentials come from .env.
 */
import { tokenManagerFromEnv } from '../src/apc.js';

async function main() {
  const tm = tokenManagerFromEnv();
  const token = await tm.getToken();
  const hours = (tm.expiresInMs() / 3_600_000).toFixed(2);

  console.log('✅ Token ready (cached in .tekion-token.json).');
  console.log(`   token (first 40): ${token.slice(0, 40)}…`);
  console.log(`   valid for       : ${hours} hours`);
  console.log('\n   All other commands will reuse this token automatically.');
}

main().catch((e) => { console.error('❌', e); process.exit(1); });
