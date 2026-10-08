# Patches

Each directory holds an ordered patch series applied to a clean checkout of
the source pin named in `upstream.env`:

| Directory | Upstream | Produces |
| --- | --- | --- |
| `calls/` | `mattermost/mattermost-plugin-calls` | the MaintainedMost calls plugin bundle |
| `server/` | `mattermost/mattermost` | the MaintainedMost server binary and image |
| `transcriber/` | `mattermost/calls-transcriber` | the transcriber image |

## Ordered, fail-closed builds

Patches apply in filename order; later patches may depend on earlier ones.
Any conflict fails the build. No patch is silently dropped, and checking each
patch separately is not a substitute for applying the complete series.

Source updates require tag/commit review, a matching server image digest,
compatibility checks and the full CI gate. The current pins are server 11.11.1,
Calls source 1.12.3 and transcriber commit c09a9a3. Calls 1.12.5 requires server
12; bundle version 1000.12.4 advances for fork fixes without changing Calls
source. `upstream.env` is authoritative for full hashes and toolchain versions.

Patches delete conditions rather than adding code wherever possible. Removing
an `if` survives upstream refactoring far better than anything else.

## What is never patched

Nothing under a directory carrying `LICENSE.enterprise`, the Mattermost Source
Available Licence. In the server that is `server/enterprise/metrics`,
`elasticsearch` and `message_export/shared`.

The calls plugin went further. Upstream's `server/enterprise` package is
compiled into the plugin binary, so a bundle built from an unmodified tree
would redistribute Source Available code. `calls/0001` replaces it with an
independent AGPL implementation and drops the import, so the package is no
longer in the dependency graph at all:

```
go list -deps ./server/ | grep -c server/enterprise   # 0
```

This describes the patched Calls dependency graph, not every distributed
artifact. The official server base image and its prepackaged plugin archives
remain in inherited image layers, even when skipped at runtime. Consult
[NOTICE.md](../NOTICE.md) and the artifacts' licences before redistribution.

## Features whose code does not ship

Some gates are in AGPL code while the implementation lives in Mattermost's
private module: LDAP, SAML, and the dedicated Google, Office365 and OpenID
providers. Removing those checks yields a menu item that fails at runtime, not
a working feature, so they are left alone.

Single sign-on is the exception: `server/0001` adds a generic OpenID Connect
provider and a request-local authorization-code exchange. It requires
authoritative discovery, strict HTTPS, validated ID-token claims and a matching
UserInfo subject with boolean `email_verified: true`. It uses the token
endpoint's TLS identity, not JWS/JWKS signature verification. Endpoint-only
configurations are no longer accepted; see the
[migration guide](../docs/upgrading.md).

## Regression coverage

`RUN_TESTS=1` invokes `scripts/test-component.sh` during builds. CI runs
race-enabled server/app/API/config regressions, the full OIDC provider suite,
Calls Go and client Jest tests, and transcriber API/config tests. It also builds
the complete frontends and native transcriber image. Release publication reuses
those artifacts; it does not rebuild an untested variant. Browser media and
external-provider trials still need separate operator verification.

The native ARM64 transcriber has also passed offline inference with its actual
runtime libraries. The standalone build accepts `TARGETARCH=arm64`; this is not
a multi-architecture guarantee for the server image or the complete stack.
