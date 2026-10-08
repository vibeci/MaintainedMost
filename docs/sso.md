# Single sign-on

MaintainedMost adds a generic OpenID Connect provider to its Team Edition server
build. This page explains its scope, authentication requirements and migration
limits; it does not claim compatibility with every identity provider.

## Scope: this is MaintainedMost Server, not the calls plugin

Read this first, because it decides whether the rest of the page applies to you.

Authentication lives in the Mattermost server, not the Calls plugin, which makes
single sign-on unlike [Calls](install.md), [recording](recording.md) and
[transcription](transcription.md), which are plugin features with separate job
services where needed. OIDC requires MaintainedMost Server, our build of Mattermost
itself. You would be running our binary instead of the official one, which
changes how you handle upgrades,
security advisories and your own trust model. Decide that deliberately.

Nothing here announces a release. For current status, check the
[repository README](../README.md).

## What Mattermost actually gates

SAML, LDAP and the dedicated Google, Office365 and OpenID Connect providers all
require a paid licence. The gate is not a flag sitting on top of working code:
those implementations live in a private module,
`github.com/mattermost/enterprise/oauth/*`, and are absent from the open source
repository entirely.

That is the difference between a locked door and a missing room. With
guest accounts the implementation is present and a check
stands in front of it. With Mattermost SSO there is nothing behind the
check to reach.

## The GitLab workaround, and why it returns 501

A GitLab OAuth provider does ship in the open repository, at
`server/channels/app/oauthproviders/gitlab`. Hence the popular advice: point the
GitLab OAuth settings at Keycloak or Authentik and let Mattermost think it is
talking to GitLab.

It does not work, for a reason that is easy to miss. If your OAuth scope
contains `openid`, Mattermost routes the request to the `openid` provider rather
than the GitLab one. The `openid` provider is part of the private enterprise
module, so on Team Edition it does not exist. The server answers with HTTP 501
and the message "Gitlab SSO through OAuth 2.0 not available on this server."

Since `openid` is a required scope for any standards-compliant OIDC provider,
you cannot simply drop it and carry on. The workaround dies there.

## What MaintainedMost does instead

The patch adds `server/channels/app/oauthproviders/openid`, implementing
Mattermost's provider interface and a request-local authorization-code exchange.
It is an independent implementation, not a copy of the private provider.

The provider supports:

- Required OIDC discovery pinned to the exact issuer URL, including its path and
  any trailing slash, or the matching standard discovery URL. Discovery is
  authoritative even when legacy Google/Microsoft endpoints are configured;
  manual endpoint-only configurations are rejected.
- Standard claims: `sub`, `email`, `email_verified`, `preferred_username`,
  `name`, `given_name` and `family_name`.
- Required ID-token string `sub`, exact `iss`, client `aud`, valid `exp` and
  `iat`, and matching `azp` whenever present or required by multiple audiences.
- Exact ID-token/UserInfo `sub` binding and a valid email with boolean
  `email_verified: true` in **UserInfo**. Missing, null, false, string and numeric
  values fail; a verified-email claim in the ID token does not replace it.

The ID token is obtained through an authenticated server-side HTTPS code
exchange. Its claims are validated, but this implementation does **not** perform
cryptographic JWS/JWKS signature verification. UserInfo supplies the account
identity and verified email; cosmetic names may fall back to the token. This
is not a general-purpose JWT verifier, and matching email addresses do not
automatically link existing accounts.

Before upgrading an existing SSO deployment, test a backup restore and rollback,
verify a local administrator recovery account, and confirm your provider emits
these claims. See [configuration](configuration.md) and the
[OIDC migration checklist](upgrading.md#oidc-migration-checklist).

Mattermost also hides the SSO login button in the web application unless the
server is licensed, in `server/config/client.go`. A working provider with an
invisible button is not much use, so MaintainedMost lifts that too.

## The gotcha: SSRF protection blocks a provider on an internal address

If your identity provider is reachable only on an internal or loopback address,
a Docker network name or `127.0.0.1`, the login fails and the server log carries
this:

> address forbidden, you may need to set AllowedUntrustedInternalConnections to
> allow an integration access to your internal network

That is not a MaintainedMost bug and it is not your provider misbehaving. Mattermost
makes the discovery, token and userinfo calls through its own HTTP client in
`server/public/shared/httpservice`, which refuses to connect to reserved and
loopback address ranges. It is deliberate SSRF protection: without it, anything
that can talk the server into fetching a URL can reach into your internal
network. Webhooks and other integrations hit exactly the same wall.

The fix is to name the host in `ServiceSettings.AllowedUntrustedInternalConnections`,
a space or comma separated list of hostnames, IP addresses and CIDR ranges the
server is allowed to reach:

```json
{
  "ServiceSettings": {
    "AllowedUntrustedInternalConnections": "id.example.com"
  }
}
```

Keep this list narrow. A private provider also needs HTTPS with a certificate
valid for its hostname and a CA trusted by the MaintainedMost server runtime.
Global `EnableInsecureOutgoingConnections` cannot bypass OIDC TLS verification.
An internal-network exception alone does not make a private certificate trusted.
[Configuration](configuration.md) has the settings and issuer-matching rules.

## Which identity providers this covers

Providers must expose matching discovery metadata, support a confidential
authorization-code flow, and return the required ID-token and UserInfo claims.
In particular, some deployments do not emit boolean `email_verified` in
UserInfo. A familiar provider name, including Keycloak, Authentik, Entra or
Google, is not a substitute for testing your tenant's configuration. The full
provider and app callback regression tests run in CI, but are not live-provider
end-to-end certification.

## What stays out of reach

SAML and LDAP. Plainly: their implementations are not in the open source
repository either, and unlike OIDC they are large, awkward specifications. A
working replacement would have to be written from scratch, and we have not
written one. If your organisation is committed to SAML or to an LDAP directory
as the source of truth, a paid Mattermost licence is the honest answer.

Mattermost did open source the server under the AGPL, which is the only reason
adding a provider is possible at all. If you can afford Professional, buy it: it
funds the upstream work all of this sits on. See
[the licensing guidance](licensing.md).

## Frequently asked questions

### Is Mattermost SSO free?

No. SAML, LDAP and the dedicated Google, Office365 and OpenID Connect providers
all require a paid licence, and their code is not in the open source repository
at all.

### Can I use OIDC with Mattermost Team Edition?

Not with the official build. The `openid` provider ships in a private enterprise
module. MaintainedMost Server adds an OpenID Connect provider written from the
specification instead.

### Why do I get "Gitlab SSO through OAuth 2.0 not available on this server"?

Because your OAuth scope contains `openid`, which makes Mattermost route the
request to the `openid` provider rather than the GitLab one. That provider is
not present on Team Edition, so the server returns HTTP 501.

### Can I point Mattermost's GitLab OAuth settings at Keycloak?

Not usefully. Any standards-compliant OIDC flow requires the `openid` scope, and
that scope is exactly what triggers the 501 above.

### Does MaintainedMost support Keycloak and Authentik?

They can be configured as OIDC providers if their discovery, TLS, token and
UserInfo responses meet the requirements above. Test your actual configuration
before migrating; discovery alone is not enough.

### Will MaintainedMost give me SAML or LDAP?

No. Those implementations are not in the open source repository and would have
to be rewritten from scratch. We have not done that.

### Why does Mattermost SSO fail with "address forbidden"?

Because your identity provider is on an internal or loopback address and
Mattermost's SSRF protection refuses to call it. Add the host to
`ServiceSettings.AllowedUntrustedInternalConnections`, and separately ensure
the server trusts the provider's CA. Do not turn off TLS verification.

### Does the MaintainedMost calls plugin enable SSO?

No. Authentication lives in the server, so a plugin cannot affect it. This is
part of MaintainedMost Server. See [install](install.md) and the
[documentation overview](README.md).
