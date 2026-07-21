/**
 * webhooks.ts — Receive APC webhook events safely.
 *
 * Tekion docs spell out the contract:
 *   - Header `X-Hub-Signature-256` carries an HMAC-SHA256 of the raw body
 *     signed with the secret you set in APC → My Configurations.
 *   - On failure (non-2xx), Tekion retries up to 3× with exponential backoff,
 *     so handlers MUST be idempotent — we dedupe by `meta.eventId`.
 *   - Acknowledge with 200 fast, then process; slow handlers cause retries.
 *
 * The Set-based dedupe is in-memory and capped (best-effort for a local tool).
 * If you later host this in a real prod setting, swap `seenEventIds` for a
 * Redis SETNX or a DB unique constraint keyed on eventId.
 */
import crypto from 'node:crypto';
import type { Request, Response } from 'express';

export type WebhookEvent = {
  meta: {
    eventId: string;
    eventTime: number;
    eventType: string;
    version: string;
    subscriptionId: string;
    dealerId: string;
  };
  data: Record<string, unknown>;
};

export type WebhookHandler = (event: WebhookEvent) => void | Promise<void>;

export class WebhookRouter {
  private handlers = new Map<string, WebhookHandler[]>();
  private seenEventIds = new Set<string>();
  private readonly maxSeen = 5000;

  constructor(private readonly secret: string) {
    if (!secret) throw new Error('WebhookRouter: secret is required');
  }

  /** Register a handler for a specific eventType (e.g. "vi.status.updated") or "*" for all. */
  on(eventType: string, handler: WebhookHandler): void {
    const list = this.handlers.get(eventType) ?? [];
    list.push(handler);
    this.handlers.set(eventType, list);
  }

  private verifySignature(body: Buffer, signature: string | undefined): boolean {
    if (!signature) return false;
    const mac = crypto.createHmac('sha256', this.secret).update(body).digest('hex');
    // Some platforms send "sha256=<hex>", others just "<hex>". Accept both.
    const expected = signature.startsWith('sha256=') ? `sha256=${mac}` : mac;
    if (signature.length !== expected.length) return false;
    try {
      return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
    } catch {
      return false;
    }
  }

  private markSeen(eventId: string): boolean {
    if (this.seenEventIds.has(eventId)) return false;
    this.seenEventIds.add(eventId);
    if (this.seenEventIds.size > this.maxSeen) {
      // Drop oldest insertion (Sets preserve insertion order).
      const oldest = this.seenEventIds.values().next().value;
      if (oldest) this.seenEventIds.delete(oldest);
    }
    return true;
  }

  /**
   * Express handler. Mount with express.raw() so we get the unparsed buffer
   * for HMAC verification:
   *   app.post('/webhooks/tekion', express.raw({type:'*\/*'}), router.handler());
   */
  handler() {
    return async (req: Request, res: Response) => {
      const signature =
        req.header('X-Hub-Signature-256') ||
        req.header('x-hub-signature-256');
      const raw = req.body as Buffer;

      if (!Buffer.isBuffer(raw)) {
        return res.status(500).json({ error: 'Raw body required — mount with express.raw().' });
      }
      if (!this.verifySignature(raw, signature)) {
        return res.status(401).json({ error: 'Invalid or missing signature' });
      }

      let event: WebhookEvent;
      try {
        event = JSON.parse(raw.toString('utf8'));
      } catch {
        return res.status(400).json({ error: 'Body is not valid JSON' });
      }

      const eventId = event?.meta?.eventId;
      if (!eventId) return res.status(400).json({ error: 'Missing meta.eventId' });

      const fresh = this.markSeen(eventId);
      if (!fresh) {
        // Idempotent: already handled. Ack so Tekion stops retrying.
        return res.status(200).json({ status: 'duplicate', eventId });
      }

      // Ack first; run handlers after so a slow handler can't trigger a retry.
      res.status(200).json({ status: 'received', eventId });

      try {
        await this.dispatch(event);
      } catch (err) {
        console.error(`[webhook] handler error for ${event.meta.eventType} (${eventId}):`, err);
      }
    };
  }

  private async dispatch(event: WebhookEvent): Promise<void> {
    const targeted = this.handlers.get(event.meta.eventType) ?? [];
    const wildcard = this.handlers.get('*') ?? [];
    for (const h of [...targeted, ...wildcard]) await h(event);
  }
}
