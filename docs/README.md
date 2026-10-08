# Operator documentation

## What MaintainedMost is

MaintainedMost is a patch-based fork of
[Mattermost](https://github.com/mattermost/mattermost). It adds group Calls
features, a generic OIDC provider and server-side licence-gate changes. The
server's user limits are 200,000 soft and 250,000 hard, not unlimited. Full
message history is inherited from unlicensed Team Edition, not a new feature.

The Calls bundle can be installed on an existing compatible server. SSO, guest
accounts and the raised user cap require MaintainedMost Server. The repository
builds a server image and patched transcriber, and the release workflow
publishes tested artifacts. Choose a completed, tested release or build from
the pins; do not assume an older published image contains current source fixes.
See [self-hosting](selfhost.md) and the [verification limits](#verification-limits).

It is for system administrators who run their own Mattermost and want more than
two people in a call. It keeps the upstream plugin id `com.mattermost.calls`,
so existing plugin configuration is reused. Back up and test
[rollback](upgrading.md) rather than assuming every server or plugin migration is
automatically reversible.

## What you get

Compared with a stock free (Team Edition or unlicensed) Mattermost server:

| Feature | Free Mattermost | MaintainedMost |
| --- | --- | --- |
| 1:1 audio calls | Yes | Yes |
| Screen sharing | Yes | Yes |
| Group audio calls in any channel | Needs Professional | Yes |
| Video in direct messages | Yes (experimental) | Yes (experimental) |
| Group video | Client limited to DMs | Experimental; no complete live-browser verification |
| Recording and transcription | Needs Enterprise | Needs a separate service |

Group video maps streams by sender and renders a participant grid. Client
tests cover late media metadata and receiver reuse, but do not prove all live
browser/media behavior. The official recorder captures voice and shared screen,
not these group camera streams. Recording/transcription require a ready
offloader and matching images; automatic recording is off by default.

## How it works

**The Calls checker is replaced.** MaintainedMost excludes the Source Available
checker from the patched plugin's dependency graph and enables group calls
without requiring `MM_CALLS_GROUP_CALLS_ALLOWED`. Administrator settings and
permissions still apply.

**MaintainedMost uses a high bundle version.** A Mattermost server upgrade
ships a prepackaged copy of official Calls and installs it if it looks newer.
The high version avoids replacement by lower-versioned official bundles; it
does not guarantee future server compatibility. The current bundle is
1000.12.4 from Calls source 1.12.3, with fork-only fixes advancing the bundle.

**The repo holds ordered patches against immutable pins.** A conflict fails the
build; patches are never silently dropped. Server 11.11.1 is pinned to a commit
and matching image digest, Calls remains at 1.12.3 because 1.12.5 requires
server 12, and the transcriber is pinned at c09a9a3. Updating them requires
compatibility, commit and runtime-image review, plus testing.

## Requirements

- A compatible self-hosted server: the Calls manifest minimum is v11.0 and the
  current integration target is v11.11.1. The server image and repository Compose
  stack target Linux/amd64.
- System administrator access to the server.
- Deployment access for configuration, job services and recovery.

MaintainedMost does **not** work on Mattermost Cloud. Cloud allows neither plugin
uploads nor custom environment variables, so there is no way to install it or
to switch group calls on.

## Verification limits

The CI gate runs race-enabled server/app/API/config and Calls regressions, the
full OIDC provider suite, Calls client Jest tests, and transcriber API/config
tests. It also builds the complete frontends and native transcriber image.
Release publication reuses those artifacts instead of rebuilding them.

This is not full end-to-end verification. Live multi-browser camera/media
behavior, real identity-provider configurations and remote transcription
services still require deployment trials. The official recorder captures voice
and shared screen, not the added group camera streams.

Only the standalone transcriber has passed a native Linux/arm64 image build and
offline inference checks. That does not verify the server, Calls, recorder,
offloader or full Compose stack on ARM. See
[transcriber architecture verification](transcription.md#architecture-verification).

For vulnerability reporting, follow [SECURITY.md](../SECURITY.md); do not publish
secrets or sensitive exploit details in an issue.

## Where to go next

- [Install](install.md), the Calls bundle on an existing server.
- [Self-host from scratch](selfhost.md), a complete Docker Compose stack.
- [Configuration](configuration.md), plugin trust, OIDC, settings and media ports.
- [Upgrading](upgrading.md), what happens when you upgrade the server or the
  plugin.
- [Recording](recording.md), job-service setup, opt-outs and consent.
- [Transcription](transcription.md), local Whisper and opt-in remote backends.
- [Single sign-on](sso.md), OIDC requirements and migration limits.
- [Troubleshooting](troubleshooting.md), when calls do not connect.
- [Licensing and legality](licensing.md), what this does and does not change
  about your licence position.
- [Automated maintenance](maintenance.md), VibeCI provisioning, scope and gates.
