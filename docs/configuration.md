# Configuration

## Calls defaults

The patched Calls plugin enables group calls without a licence check. It does
not require `MM_CALLS_GROUP_CALLS_ALLOWED`, although the image and Compose file
retain that upstream variable. Calls and experimental video default on;
existing administrator and channel settings still matter.

Recording and transcription default on only when a nonblank job-service URL
is configured and no administrator choice exists. Explicit environment values
override stored values, and stored `false` is not replaced by a default. To
opt out, set these on the server process or container:

```yaml
environment:
  MM_CALLS_ENABLE_RECORDINGS: "false"
  MM_CALLS_ENABLE_TRANSCRIPTIONS: "false"
```

Automatic recording is a separate setting and is off by default. See
[recording](recording.md) for readiness, consent and image requirements.

For repository Compose deployments, `.env` must specify `MAINTAINEDMOST_IMAGE`,
`CALLS_IMAGE_REGISTRY`, `SITE_URL` and `POSTGRES_PASSWORD`; see
[self-hosting](selfhost.md). Other environment settings must be passed to the
server explicitly, not merely added to `.env`. After changing container
environment settings, use `docker compose up -d` to recreate the container.

## config.json plugin settings

Keep signature verification enabled for external plugin installs where
possible. MaintainedMost Server separately trusts unsigned bundles packaged
locally with its image:

```json
{
  "PluginSettings": {
    "AutomaticPrepackagedPlugins": true,
    "RequirePluginSignature": true
  }
}
```

The local exception applies at startup, to marketplace installs selecting the
matching local bundle, and to transitional persistence. If a signature is
present, it must verify even when `RequirePluginSignature` is false. An invalid,
empty or unreadable signature is not treated as an unsigned bundle. Trusted
custom signing keys are supported through `PluginSettings.SignaturePublicKeyFiles`.

Uploads, remote marketplace downloads, URL installs and filestore/cluster reads
retain their existing signature policy and administrator authorization checks.
An unsigned local bundle does not grant trust to a remote bundle with the same
plugin id. A persisted copy is not exempt from later filestore verification.

For plugin-only installs on an official server, use the upstream signing and
install flow with a trusted key, or explicitly accept the risks of allowing
unsigned uploads. Disabling `RequirePluginSignature` weakens a server-wide
control; it is not required for MaintainedMost's bundled local plugin. Verify the
selected release's checksum as an integrity check, but remember that a checksum
from the same source is not proof of publisher identity.

`AutomaticPrepackagedPlugins: false` disables automatic installation for all
prepackaged plugins, not just Calls. It is not required by MaintainedMost.

## Plugin settings in the System Console

MaintainedMost keeps the upstream plugin id `com.mattermost.calls`, so its settings
live where they always did: **System Console › Plugins › Calls**. The settings
that still apply:

- **Max call participants** (`MaxCallParticipants`). `0` means no configured
  participant cap, not unlimited server capacity. Load-test your hardware and
  uplink before choosing a limit.
- **Enable video** (`EnableVideo`). Experimental camera video, including group
  calls. MaintainedMost defaults it on, but a saved setting may keep it off. Client
  regressions cover late media metadata; full live-browser media behavior has
  not been verified. See [verification limits](README.md#verification-limits).
- **Allow screen sharing** (`AllowScreenSharing`). Works in group calls.
- **Enable ringing** (`EnableRinging`). Notification sound and call popup for
  incoming calls.

Recording/transcription and automatic-recording settings are described in
[recording](recording.md). The job service must be ready before jobs can start.

## Single sign-on with OpenID Connect

This one is MaintainedMost Server rather than the calls plugin: authentication
lives in the Mattermost server, so no plugin upload can provide it. The reason
the stock server cannot do this, and the GitLab workaround that returns a 501,
are on [single sign-on](sso.md).

The settings live under `OpenIdSettings` in `config.json`, and in the System
Console under **Authentication › OpenID Connect**:

```json
{
  "OpenIdSettings": {
    "Enable": true,
    "DiscoveryEndpoint": "https://id.example.com/realms/main",
    "Id": "mattermost",
    "Secret": "the client secret your provider issued",
    "ButtonText": "Log in with SSO",
    "ButtonColor": "#145DBF"
  }
}
```

What each one is for:

- `Enable` turns the provider on. It is off by default.
- `Id` and `Secret` are the client credentials from your identity provider.
- `DiscoveryEndpoint` is required. Use the exact HTTPS issuer URL, including
  its path and any trailing slash, or a standard
  `/.well-known/openid-configuration` URL whose suffix removal yields that
  exact issuer. If the issuer ends in `/`, prefer the issuer URL itself to
  avoid ambiguity. The discovered `issuer` must match exactly.
- Discovery supplies the authoritative authorization, token and UserInfo
  endpoints, even if `AuthEndpoint`, `TokenEndpoint` or `UserAPIEndpoint` are
  already populated with legacy Google/Microsoft values. Manual endpoint-only
  configurations are no longer accepted. Discovery is cached for an hour.
- `Scope` defaults to `profile openid email`. `openid` has to stay in it.
- `ButtonText` is the label on the login button. Leave it empty for upstream's
  default wording. `ButtonColor` is the button's colour as a hex value,
  `#145DBF` unless you change it.
- `UsePreferredUsername` takes the Mattermost username from the
  `preferred_username` claim rather than the local part of the email address.

On the provider side, register the redirect URI as your site URL plus
`/signup/openid/complete`:

```
https://chat.example.com/signup/openid/complete
```

### Required identity claims

The token endpoint must return an ID token with a nonempty string `sub`, an
exactly matching `iss`, an `aud` containing the client id, valid `exp` and `iat`,
and an `azp` matching the client id whenever present. Multiple audiences require
`azp`. The UserInfo `sub` must match the ID token exactly, without normalization.

UserInfo must also return a valid email and JSON boolean `email_verified: true`.
Missing, null, false, string (including `"true"`) and numeric values are rejected.
A verified-email claim in the ID token is not a fallback. Existing accounts are
not automatically linked by matching email addresses.

Authentication uses the server-side authorization-code exchange over strictly
verified HTTPS. ID-token claims are checked, but the provider does **not**
cryptographically verify JWS signatures using JWKS. It is not a verifier for
arbitrary browser-supplied or stored JWTs. Before migrating, verify a local
administrator recovery account and follow the
[upgrade checklist](upgrading.md#oidc-migration-checklist).

### If the provider is on an internal address

If an endpoint resolves to a private or loopback address without an outbound
allowlist entry, login fails and the server log can say:

```
address forbidden, you may need to set AllowedUntrustedInternalConnections
```

Mattermost is protecting itself, not breaking. It makes the discovery, token
and userinfo requests through an HTTP client that refuses reserved and loopback
address ranges, which is what stops a server being talked into fetching things
from your internal network. Name the host explicitly to allow it:

```json
{
  "ServiceSettings": {
    "AllowedUntrustedInternalConnections": "id.example.com"
  }
}
```

The value is a space or comma separated list of hostnames, IP addresses and
CIDR ranges. Keep it to the internal discovery, token and UserInfo hosts that
need it. Publicly routable endpoints need no internal-network exception.

An allowlist entry does not bypass TLS: the provider needs a certificate valid
for its hostname and a CA trusted by the server runtime. Global
`ServiceSettings.EnableInsecureOutgoingConnections` cannot disable verification
for OIDC. Install the private CA in the runtime's trust store instead.

## Network and ports

Calls media does not travel over HTTP and does not pass through your reverse
proxy. It needs a direct path to the server:

- **UDP 8443**, the primary media port.
- **TCP 8443**, for clients that cannot use UDP. The repository Compose file
  publishes both UDP and TCP; configure the RTC fallback for your deployment.
- **TCP 443 or 8065**, whatever already serves the web interface.

```bash
ufw allow 8443/udp
ufw allow 8443/tcp
```

Do not send RTC media through the web HTTP proxy. If using NAT, a separate RTC
service or TURN, configure the advertised media address and ports accordingly.

The other half of the network configuration is the site URL:

```bash
MM_SERVICESETTINGS_SITEURL=https://chat.example.com
```

It must match the address users and job containers can reach, including scheme
and any port. Check WebSocket and RTC connectivity separately; a correct site
URL alone does not guarantee a reachable media path.

## Where to go next

- [Install guide](install.md) for getting the plugin onto an existing server.
- [Self-host from scratch](selfhost.md) for a complete Compose stack.
- [Upgrading](upgrading.md) for what happens on a server upgrade.
- [Troubleshooting](troubleshooting.md) if calls do not connect.
- [Single sign-on](sso.md) for why OpenID Connect needs MaintainedMost Server and
  what it does not cover.
- [Licensing and legality](licensing.md) for what this does and does not
  change about your licence position.
