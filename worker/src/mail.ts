/**
 * The one HTML mail this worker sends. Kept in the same palette as
 * mattermore.dev, laid out with tables because that is what mail clients
 * actually render.
 */

const GREEN = '#0c8f63';
const INK = '#10161c';
const MUTED = '#5c6b7a';
const LINE = '#e3e8ee';

export function confirmationMail(confirmURL: string): { html: string; text: string } {
  const html = `<!doctype html>
<html lang="en"><body style="margin:0;padding:0;background:#f6f8fa;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f8fa;padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border:1px solid ${LINE};border-radius:12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
        <tr><td style="padding:28px 32px 0 32px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="vertical-align:middle;">
              <!-- PNG, not the site's SVG: Gmail and Outlook refuse to render SVG in mail. -->
              <img src="https://mattermore.dev/logo-mail.png" width="42" height="24" alt="" style="display:block;border:0;">
            </td>
            <td style="vertical-align:middle;padding-left:10px;font-size:17px;font-weight:700;color:${INK};letter-spacing:-0.01em;">Mattermore</td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:24px 32px 0 32px;">
          <h1 style="margin:0 0 12px 0;font-size:20px;line-height:1.3;color:${INK};letter-spacing:-0.01em;">Confirm your email</h1>
          <p style="margin:0 0 8px 0;font-size:15px;line-height:1.6;color:${INK};">You asked to hear about Mattermore releases and security fixes. Confirm the address and you are on the list.</p>
          <p style="margin:0;font-size:15px;line-height:1.6;color:${MUTED};">If this was not you, ignore this mail. Nothing is sent until you confirm.</p>
        </td></tr>
        <tr><td style="padding:24px 32px 0 32px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="background:${GREEN};border-radius:8px;">
            <a href="${confirmURL}" style="display:inline-block;padding:12px 22px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;">Confirm my email</a>
          </td></tr></table>
        </td></tr>
        <tr><td style="padding:20px 32px 0 32px;">
          <p style="margin:0;font-size:13px;line-height:1.6;color:${MUTED};">Or paste this into your browser:<br>
            <a href="${confirmURL}" style="color:${GREEN};word-break:break-all;">${confirmURL}</a></p>
        </td></tr>
        <tr><td style="padding:24px 32px 28px 32px;">
          <hr style="border:0;border-top:1px solid ${LINE};margin:0 0 16px 0;">
          <p style="margin:0;font-size:12px;line-height:1.6;color:${MUTED};">Mattermore is a fork of Mattermost with the paywalled features turned on.
            <a href="https://mattermore.dev" style="color:${MUTED};">mattermore.dev</a></p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>`;

  const text = [
    'Confirm your email',
    '',
    'You asked to hear about Mattermore releases and security fixes.',
    'Confirm the address and you are on the list:',
    '',
    confirmURL,
    '',
    'If this was not you, ignore this mail. Nothing is sent until you confirm.',
    '',
    'mattermore.dev',
  ].join('\n');

  return { html, text };
}
