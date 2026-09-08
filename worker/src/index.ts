/**
 * The one bit of server mattermore.dev has.
 *
 * POST /            a hosting request, turned into a Balie ticket
 * POST /subscribe   an address asking for release mail, pending until confirmed
 * GET  /confirm     the link from the confirmation mail
 * GET  /unsubscribe the link in the footer of every mail we send
 *
 * The site is static, so it cannot hold a secret. Keeping the keys here means
 * nothing sensitive reaches the browser and there is no third-party widget to
 * configure.
 */

import { confirmationMail } from './mail';

interface Env {
  BALIE_API_KEY: string;
  EMAILIT_API_KEY?: string;
  TURNSTILE_SECRET?: string;
  DB: D1Database;
}

const ALLOWED = new Set(['https://mattermore.dev', 'https://www.mattermore.dev']);
const BALIE = 'https://app.balie.net/api/v1/tickets';
const EMAILIT = 'https://api.emailit.com/v1/emails';
const TURNSTILE = 'https://challenges.cloudflare.com/turnstile/v0/siteverify';
const SITE = 'https://mattermore.dev';
// The worker answers here, so the links we mail are on our own domain.
const API = 'https://api.mattermore.dev';
const FROM = 'Mattermore <noreply@mattermore.dev>';
const FALLBACK_TO = 'dennisklappe@gmail.com';

function cors(origin: string | null): Record<string, string> {
  // Echo the origin only when we recognise it, so the header cannot be used
  // to make this worker a general purpose proxy.
  const allow = origin && ALLOWED.has(origin) ? origin : SITE;
  return {
    'Access-Control-Allow-Origin': allow,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
    Vary: 'Origin',
  };
}

function json(body: unknown, status: number, origin: string | null): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...cors(origin) },
  });
}

const str = (v: unknown, max: number): string =>
  typeof v === 'string' ? v.trim().slice(0, max) : '';

const token = (): string =>
  [...crypto.getRandomValues(new Uint8Array(24))]
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');

/** Enough to rate limit on, not enough to identify anyone. */
async function hashIP(ip: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`mattermore:${ip}`));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 32);
}

/**
 * Turnstile runs invisibly, so most people never see it and a failed check is
 * the only time a box appears. Until the secret is set the check is skipped
 * rather than refusing everyone.
 */
async function passesTurnstile(env: Env, response: string, ip: string): Promise<boolean> {
  if (!env.TURNSTILE_SECRET) return true;
  if (!response) return false;
  try {
    const res = await fetch(TURNSTILE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ secret: env.TURNSTILE_SECRET, response, remoteip: ip }),
    });
    const body = (await res.json()) as { success?: boolean };
    return body.success === true;
  } catch (err) {
    // A Cloudflare outage should not close the form.
    console.error('turnstile unreachable', err);
    return true;
  }
}

async function sendMail(
  env: Env,
  to: string,
  subject: string,
  parts: { html?: string; text: string },
  extra: Record<string, string> = {},
): Promise<boolean> {
  if (!env.EMAILIT_API_KEY) return false;
  try {
    const res = await fetch(EMAILIT, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        Authorization: `Bearer ${env.EMAILIT_API_KEY}`,
      },
      body: JSON.stringify({ from: FROM, to, subject, ...parts, ...extra }),
    });
    if (!res.ok) console.error('emailit rejected the mail', res.status, await res.text());
    return res.ok;
  } catch (err) {
    console.error('emailit unreachable', err);
    return false;
  }
}

/** When Balie cannot take a ticket, mail it instead. A lead is worth more than a tidy pipeline. */
function mailFallback(env: Env, subject: string, body: string, replyTo: string): Promise<boolean> {
  return sendMail(env, FALLBACK_TO, `[fallback] ${subject}`, { text: body }, { reply_to: replyTo });
}

function redirect(path: string): Response {
  return Response.redirect(`${SITE}${path}`, 302);
}

async function hostingRequest(request: Request, env: Env, origin: string | null): Promise<Response> {
  let payload: Record<string, unknown>;
  try {
    payload = (await request.json()) as Record<string, unknown>;
  } catch {
    return json({ error: 'invalid_json' }, 400, origin);
  }

  const name = str(payload.name, 120);
  const email = str(payload.email, 200);
  const subdomain = str(payload.subdomain, 40).toLowerCase();
  const size = str(payload.size, 20);
  const notes = str(payload.notes, 4000);

  if (!email.includes('@') || email.length < 5) return json({ error: 'invalid_email' }, 400, origin);
  if (!/^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/.test(subdomain)) {
    return json({ error: 'invalid_subdomain' }, 400, origin);
  }
  // The honeypot field is never shown to anyone, so anything in it is a bot.
  if (str(payload.website, 200)) return json({ ok: true }, 200, origin);

  const ip = request.headers.get('CF-Connecting-IP') ?? '';
  if (!(await passesTurnstile(env, str(payload.turnstile, 2048), ip))) {
    return json({ error: 'challenge_failed' }, 403, origin);
  }

  const server = `${subdomain}.mattermore.dev`;
  const message = [`Server: ${server}`, `People: ${size || 'not said'}`, '', notes || '(no further detail)'].join('\n');
  const subject = `Hosting request: ${server}`;
  const detail = [`Name: ${name || 'not said'}`, `Email: ${email}`, message].join('\n');

  let ok = false;
  try {
    const res = await fetch(BALIE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${env.BALIE_API_KEY}` },
      body: JSON.stringify({
        email,
        name: name || undefined,
        subject,
        message,
        form: 'hosting',
        metadata: { subdomain: server, size },
      }),
    });
    ok = res.ok;
    // Do not leak Balie's response to the browser, but keep it in the logs.
    if (!ok) console.error('balie rejected the ticket', res.status, await res.text());
  } catch (err) {
    console.error('balie unreachable', err);
  }

  if (!ok && !(await mailFallback(env, subject, detail, email))) {
    return json({ error: 'upstream_failed' }, 502, origin);
  }
  return json({ ok: true }, 200, origin);
}

async function subscribe(request: Request, env: Env, origin: string | null): Promise<Response> {
  let payload: Record<string, unknown>;
  try {
    payload = (await request.json()) as Record<string, unknown>;
  } catch {
    return json({ error: 'invalid_json' }, 400, origin);
  }

  const email = str(payload.email, 200).toLowerCase();
  const source = str(payload.source, 40) || 'unknown';

  if (!/^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(email)) return json({ error: 'invalid_email' }, 400, origin);
  if (str(payload.website, 200)) return json({ ok: true }, 200, origin);

  const ip = request.headers.get('CF-Connecting-IP') ?? '';
  if (!(await passesTurnstile(env, str(payload.turnstile, 2048), ip))) {
    return json({ error: 'challenge_failed' }, 403, origin);
  }

  const existing = await env.DB.prepare('SELECT status, token FROM subscribers WHERE email = ?')
    .bind(email)
    .first<{ status: string; token: string }>();

  // Someone already confirmed does not need another mail, and saying so would
  // tell a stranger who is on the list. Both cases answer the same way.
  if (existing?.status === 'confirmed') return json({ ok: true }, 200, origin);

  const t = existing?.token ?? token();
  await env.DB.prepare(
    `INSERT INTO subscribers (email, status, token, source, ip_hash)
     VALUES (?, 'pending', ?, ?, ?)
     ON CONFLICT(email) DO UPDATE SET status = 'pending', source = excluded.source`,
  )
    .bind(email, t, source, await hashIP(ip))
    .run();

  const { html, text } = confirmationMail(`${API}/confirm?t=${t}`);
  await sendMail(env, email, 'Confirm your email', { html, text });

  return json({ ok: true }, 200, origin);
}

/** Both links are one GET with a token, so both live here. */
async function actOnToken(url: URL, env: Env, action: 'confirm' | 'unsubscribe'): Promise<Response> {
  const t = url.searchParams.get('t') ?? '';
  if (!/^[a-f0-9]{48}$/.test(t)) return redirect('/link-expired');

  const row = await env.DB.prepare('SELECT email FROM subscribers WHERE token = ?')
    .bind(t)
    .first<{ email: string }>();
  if (!row) return redirect('/link-expired');

  if (action === 'confirm') {
    await env.DB.prepare(
      `UPDATE subscribers SET status = 'confirmed', confirmed_at = datetime('now') WHERE token = ?`,
    )
      .bind(t)
      .run();
    return redirect('/subscribed');
  }

  await env.DB.prepare(`UPDATE subscribers SET status = 'unsubscribed' WHERE token = ?`).bind(t).run();
  return redirect('/unsubscribed');
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const origin = request.headers.get('Origin');
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '') || '/';

    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors(origin) });

    if (request.method === 'GET') {
      if (path === '/confirm') return actOnToken(url, env, 'confirm');
      if (path === '/unsubscribe') return actOnToken(url, env, 'unsubscribe');
      return json({ error: 'not_found' }, 404, origin);
    }

    if (request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405, origin);
    if (origin && !ALLOWED.has(origin)) return json({ error: 'forbidden_origin' }, 403, origin);

    if (path === '/subscribe') return subscribe(request, env, origin);
    if (path === '/') return hostingRequest(request, env, origin);
    return json({ error: 'not_found' }, 404, origin);
  },
};
