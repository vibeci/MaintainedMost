/**
 * The update list.
 *
 * Cloudflare Turnstile runs invisibly: the widget only draws itself when it
 * decides a visitor has to prove something, which for almost everyone is
 * never. The secret half lives in the worker, so the sitekey below is public
 * by design.
 */
export const TURNSTILE_SITEKEY = '0x4AAAAAAEsuXXwiv06tA5gD';
