# MaintainedMost

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="brand/wordmark-white.svg">
  <img src="brand/wordmark-dark.svg" alt="MaintainedMost" width="480">
</picture>

**Mattermost, with the follow-through.**

A patch-based fork of [Mattermost](https://github.com/mattermost/mattermost)
for self-hosted collaboration, building on the original Mattermore patch set.
The aim is straightforward: keep the useful features, fix the rough edges, and
make upstream updates routine. A promising first release is a good start;
we would quite like there to be a second act.

The code, documentation, issues and release history live in this repository.
There is no separate website or hosted-service offering.

## What It Does

| Capability | MaintainedMost | Requirements or limits |
| --- | --- | --- |
| Audio calls and screen sharing | One-to-one and group calls | Calls plugin and reachable media ports |
| Group camera video | Experimental | Client regression tests, not complete live-browser verification |
| Recording | No paid licence check | Offloader and recorder; captures voice and shared screen, not group cameras |
| Transcription | Local Whisper by default | Patched transcriber; remote APIs are opt-in |
| OIDC single sign-on | Generic provider | Discovery, validated token claims and verified UserInfo |
| Guest accounts and channel moderation | Licence gates removed | Administrator settings and permissions still apply |
| User limits | 200,000 soft / 250,000 hard | A raised limit, not a capacity guarantee |
| Message history | No licence-based history cap | Inherited from unlicensed Team Edition |

LDAP, SAML and other private enterprise implementations are not supplied.
Removing a menu's licence check does not make its missing backend appear.

## Install

For a full server, follow [self-hosting](docs/selfhost.md). For just Calls on
an existing compatible Mattermost server, follow [plugin installation](docs/install.md).

The Compose stack requires a tested server image and its matching job-image
namespace in `.env`, alongside the site URL and database password:

```ini
MAINTAINEDMOST_IMAGE=ghcr.io/OWNER/maintainedmost:v1.5.0
CALLS_IMAGE_REGISTRY=ghcr.io/OWNER/maintainedmost
```

`OWNER` and `v1.5.0` illustrate the naming scheme, not an already published
release. Both `calls-recorder:<upstream-version>` and
`calls-transcriber:<release-tag>` must exist in that namespace. Choose a
completed release or build the pinned source; do not assume an older image
contains the current fixes.

**Existing installations:** read [upgrading](docs/upgrading.md) first. Keep your
old `COMPOSE_PROJECT_NAME` when reusing existing volumes. OIDC now requires
discovery and boolean `email_verified: true` in UserInfo; test a local
administrator recovery account before migrating SSO.

The offloader's Docker socket grants host-level privileges. Keep its API
private. Recording and transcription still require ready services, matching
images and sufficient resources. Explicit opt-outs are honored; automatic
recording is off by default.

## Maintenance Approach

- Ordered patches against pinned upstream commits, with a matching server
  runtime-image digest. A conflicting patch fails the build; it is not quietly
  omitted from the release.
- Upstream monitoring distinguishes a patch conflict from a failed checkout
  and checks plugin minimum-server requirements before suggesting an upgrade.
- Opt-in [VibeCI maintenance](docs/maintenance.md) ports all three patch sets as
  one pinned transaction, with protected CI before any automatic merge.
- Race-enabled Go regressions, the full OIDC provider suite, Calls webapp tests,
  native build checks and complete production builds gate releases.
- Releases reuse the tested artifacts and check anonymous access to the job
  images before promoting the server image or publishing the release.
- Known limitations and migration requirements stay in the documentation.

A green badge is reassuring. We prefer it to mean the tests ran, rather than
that the patch files were successfully located.

[`upstream.env`](upstream.env) is authoritative. The current baseline is server
**11.11.1**, Calls source **1.12.3** with bundle **1000.12.4**, and transcriber
commit **c09a9a3**. Calls 1.12.5 requires server 12 and is not a compatible
independent upgrade.

## Build

Use the Go and Node versions in `upstream.env`, plus Git, Make, Python 3 and
Docker with Buildx. Frontend builds retain upstream's locked dependencies.

```bash
RUN_TESTS=1 ./scripts/build-calls.sh
RUN_TESTS=1 ./scripts/build-server.sh
RUN_TESTS=1 ./scripts/build-image.sh maintainedmost:dev
```

The full server image currently targets **Linux/amd64**. The standalone
transcriber also supports a native ARM build:

```bash
TARGETARCH=arm64 RUN_TESTS=1 GOFLAGS=-p=2 ./scripts/build-transcriber.sh
```

Its native **Linux/arm64** image has passed offline Whisper inference,
Silero/ONNX speech detection and Opus decoding with the real runtime libraries.
That is evidence for the transcriber, not a claim that the entire server stack
has been validated on ARM. The earlier amd64 build failed under local QEMU
emulation; native amd64 verification remains separate. See
[architecture verification](docs/transcription.md#architecture-verification).

Live-browser media, real identity providers and complete recording jobs still
need deployment testing. There is no support SLA or promise of compatibility
with arbitrary upstream versions.

## Documentation

- [Operator guide](docs/README.md)
- [Configuration](docs/configuration.md) and [single sign-on](docs/sso.md)
- [Recording](docs/recording.md) and [transcription](docs/transcription.md)
- [Upgrading and rollback](docs/upgrading.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing and publishing](CONTRIBUTING.md)
- [Automated maintenance setup](docs/maintenance.md)
- [Security reporting](SECURITY.md)

## Licence And Credits

GNU AGPLv3 for this project, with upstream component licences and notices
preserved. Both the pinned Mattermost server and Calls source explicitly grant
GNU AGPLv3; the conflicting AGPLv2 wording in Mattermost's FAQ is documented in
[the licensing notes](docs/licensing.md#why-agplv3).

This is not a claim that every container dependency is AGPL-only. Read
[LICENSE](LICENSE), [NOTICE.md](NOTICE.md) and the [distribution caveats](docs/licensing.md)
before redistributing artifacts.

Mattermost and its contributors built the software this project depends on.
Dennis Klappe and the Mattermore contributors supplied the original fork's
patches. Their attribution remains intact. If a commercial Mattermost
subscription suits your organisation, it also funds the upstream work.

MaintainedMost is not affiliated with, endorsed by, or supported by Mattermost,
Inc. "Mattermost" is their trademark, used here to describe compatibility.
