/**
 * Where the hosting request form sends tickets.
 *
 * A small Cloudflare Worker holds the Balie server key and forwards the
 * request. Balie's own guidance for a static page is a public key plus a
 * Turnstile widget, but this is better: no secret reaches the browser, there
 * is no widget to configure, and it keeps working for visitors who block
 * third-party scripts.
 *
 * Source: the mattermore-form worker, in this repository under worker/.
 */
export const FORM_ENDPOINT = 'https://api.mattermore.dev';

/** Where requests land if the endpoint is unreachable. */
export const FALLBACK_EMAIL = 'hosting@mattermore.dev';
