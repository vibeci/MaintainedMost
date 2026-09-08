---
title: "Single sign-on for self-hosted Mattermost"
description: "Why Mattermost SSO needs a paid licence, why the GitLab OAuth workaround fails, and the OpenID Connect provider Mattermore wrote to fix it."
label: "Single sign-on"
order: 7
---

Mattermost ships a lot of authentication code, and almost none of it is
reachable on an unlicensed server. Anyone searching for mattermost sso free
meets the same answer: buy a licence. This page sets out what upstream gates,
why the workaround everyone tries fails with a 501, and what Mattermore does
instead, which is not removing a check.

## Scope: this is Mattermore Server, not the calls plugin

Read this first, because it decides whether the rest of the page applies to you.

Authentication lives in the Mattermost server, not the Calls plugin, which makes
single sign-on unlike [group calls](/group-calls),
[video calls](/video-calls), [call recording](/call-recording) and
[transcription](/transcription), where uploading a plugin bundle is the entire
job. Getting mattermost single sign on self hosted without a licence means
running Mattermore Server, our build of Mattermost itself. You would be running
our binary instead of the official one, which changes how you handle upgrades,
security advisories and your own trust model. Decide that deliberately.

Nothing here announces a release. For current status, check the
[Mattermore repository](https://github.com/dennisklappe/mattermore).

## What Mattermost actually gates

SAML, LDAP and the dedicated Google, Office365 and OpenID Connect providers all
require a paid licence. The gate is not a flag sitting on top of working code:
those implementations live in a private module,
`github.com/mattermost/enterprise/oauth/*`, and are absent from the open source
repository entirely.

That is the difference between a locked door and a missing room. With
[guest accounts](/guest-accounts) the implementation is present and a check
stands in front of it. With mattermost sso there is nothing behind the
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

## What Mattermore does instead

There is no check to remove, so we did not remove one. We wrote a
standards-compliant OpenID Connect provider and contributed it upstream: a new
`server/channels/app/oauthproviders/openid` package implementing Mattermost's
four-method `OAuthProvider` interface. It is our own code, AGPL-3.0 like the
repository it lives in, not a copy of anything proprietary.

The provider supports:

- OIDC discovery. Point it at an issuer URL and it fetches the endpoints itself,
  rather than making you paste four URLs by hand.
- Standard claims: `sub`, `email`, `email_verified`, `preferred_username`,
  `name`, `given_name` and `family_name`.
- A safety check that rejects any login where the `sub` in the ID token
  disagrees with the `sub` returned by the userinfo endpoint.

Identity always comes from the userinfo response, never from an unverified ID
token. That is the conservative reading of the specification, and the one we
implemented.

Mattermost also hides the SSO login button in the web application unless the
server is licensed, in `server/config/client.go`. A working provider with an
invisible button is not much use, so Mattermore lifts that too.

## The gotcha: SSRF protection blocks a provider on an internal address

This one cost us an evening against Keycloak, so it belongs on the page.

If your identity provider is reachable only on an internal or loopback address,
a Docker network name or `127.0.0.1`, the login fails and the server log carries
this:

> address forbidden, you may need to set AllowedUntrustedInternalConnections to
> allow an integration access to your internal network

That is not a Mattermore bug and it is not your provider misbehaving. Mattermost
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
    "AllowedUntrustedInternalConnections": "keycloak 127.0.0.1"
  }
}
```

We confirmed this against Keycloak on the same Docker network: with the entry
in place, login completes end to end. Keep the list as narrow as it will go.
The setting is meant to be a named exception, not a way to switch the
protection off, and a provider on a public hostname does not need it at all.
[Configuration](/configuration) has the OpenID settings that go with it.

## Which identity providers this covers

Any provider that speaks OpenID Connect properly: mattermost keycloak and
mattermost authentik setups, Authelia, Microsoft Entra and Google. It also
covers Cloudflare Access as an identity provider in front of your server, a
common pattern for deployments that would rather not expose a login page.

Because it is real mattermost oidc rather than a GitLab impersonation, you
configure it the way you would configure any other OIDC client.

## What stays out of reach

SAML and LDAP. Plainly: their implementations are not in the open source
repository either, and unlike OIDC they are large, awkward specifications. A
working replacement would have to be written from scratch, and we have not
written one. If your organisation is committed to SAML or to an LDAP directory
as the source of truth, a paid Mattermost licence is the honest answer.

Mattermost did open source the server under the AGPL, which is the only reason
adding a provider is possible at all. If you can afford Professional, buy it: it
funds the upstream work all of this sits on. See
[Mattermore's licensing position](/licensing).

## Frequently asked questions

### Is Mattermost SSO free?

No. SAML, LDAP and the dedicated Google, Office365 and OpenID Connect providers
all require a paid licence, and their code is not in the open source repository
at all.

### Can I use OIDC with Mattermost Team Edition?

Not with the official build. The `openid` provider ships in a private enterprise
module. Mattermore Server adds an OpenID Connect provider written from the
specification instead.

### Why do I get "Gitlab SSO through OAuth 2.0 not available on this server"?

Because your OAuth scope contains `openid`, which makes Mattermost route the
request to the `openid` provider rather than the GitLab one. That provider is
not present on Team Edition, so the server returns HTTP 501.

### Can I point Mattermost's GitLab OAuth settings at Keycloak?

Not usefully. Any standards-compliant OIDC flow requires the `openid` scope, and
that scope is exactly what triggers the 501 above.

### Does Mattermore support Keycloak and Authentik?

Yes, along with Authelia, Microsoft Entra, Google and Cloudflare Access in
front of the server. Anything implementing OIDC discovery correctly should
work.

### Will Mattermore give me SAML or LDAP?

No. Those implementations are not in the open source repository and would have
to be rewritten from scratch. We have not done that.

### Why does Mattermost SSO fail with "address forbidden"?

Because your identity provider is on an internal or loopback address and
Mattermost's SSRF protection refuses to call it. Add the host to
`ServiceSettings.AllowedUntrustedInternalConnections`. That is the server
protecting itself, not a fault in the provider.

### Does the Mattermore calls plugin enable SSO?

No. Authentication lives in the server, so a plugin cannot affect it. This is
part of Mattermore Server. See [install](/install) and [docs](/docs).
