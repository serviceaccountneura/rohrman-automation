/**
 * totp.ts — Generate Tekion's TOTP code from TEKION_TOTP_SECRET (base32 seed).
 *
 * The seed is the second factor itself: anyone holding it can log into Max's
 * account. It lives ONLY in .env (gitignored) and must never be committed,
 * logged, or shared in chat. Rotate at the slightest suspicion of exposure.
 *
 * Equivalent of the Python:
 *     totp = pyotp.TOTP(secret)
 *     print(totp.now())
 *
 * CLI:  npm run otp                 → prints "123456  (rotates in 14s)"
 * Programmatic:  currentTotp()      → { code, secondsRemaining }
 */
import 'dotenv/config';
import { authenticator } from 'otplib';

export function currentTotp(secretOverride?: string): { code: string; secondsRemaining: number } {
  const raw = (secretOverride ?? process.env.TEKION_TOTP_SECRET ?? '').replace(/\s+/g, '');
  if (!raw) throw new Error('TEKION_TOTP_SECRET is not set (see .env.example).');
  const code = authenticator.generate(raw);
  const secondsRemaining = authenticator.timeRemaining();
  return { code, secondsRemaining };
}

if (process.argv[1]?.endsWith('totp.ts')) {
  const { code, secondsRemaining } = currentTotp();
  console.log(`${code}  (rotates in ${secondsRemaining}s)`);
}
