# Contributing

## The rule that matters

Every change to upstream code is a patch file in `patches/`. Each component's
series is applied in filename order to a clean checkout of the immutable pin
in `upstream.env`; later patches may depend on earlier ones.

A conflict fails the build. Never silently skip a patch to make a release
pass. An upstream update requires reviewing compatibility, commit pins and the
matching server image digest, not just changing a tag.

Keep patches small. Prefer deleting a condition over adding code: removing an
`if` survives upstream refactoring far better than anything else.

## Never patch

Anything under a directory carrying `LICENSE.enterprise`, the Mattermost
Source Available Licence. If a feature seems to need it, open an issue instead
of working around it. The calls plugin goes further and does not even compile
that package in, see `patches/README.md`.

Also leave alone any gate whose implementation is not in the open repository.
LDAP, SAML and the dedicated Google and Office365 providers are gated in AGPL
code but implemented in a private module, so removing the check produces a
broken menu item rather than a feature.

## Building

```bash
./scripts/build-calls.sh        # plugin bundle
./scripts/build-server.sh       # server binary and webapp
./scripts/build-transcriber.sh  # patched native transcriber image
./scripts/build-image.sh        # complete server and job images
```

Use the Go and Node versions in `upstream.env`, plus Git, Make, Python 3 and
Docker with Buildx. The transcriber needs its native Whisper/Opus/ONNX/Azure
build, not a Go-only substitute. Server images target Linux/amd64. Build the
standalone transcriber for ARM with `TARGETARCH=arm64`; its full native image
and offline inference have been verified. This does not establish ARM support
for the full server stack. Whisper's amd64 build uses
a generic x86-64/SSE2 baseline instead of the build machine's CPU features;
this trades SIMD performance for portability. Use a native amd64 builder to
validate release images rather than relying only on emulation.

Set `RUN_TESTS=1` to run the component regression gate during builds. The server
gate uses mock-backed integration tests; Calls tests provision PostgreSQL through
testcontainers and need a working Docker daemon. See `scripts/test-component.sh` and
`.github/workflows/build.yml` for the exact test selectors and setup.

The shared CI gate runs race-enabled server/app/API/config regressions, the
entire OIDC provider suite, Calls Go and client Jest tests, transcriber API/config
tests, and full frontend and native image builds. Releases publish the resulting
artifacts without a second build. A fresh runner validates the six Go JSON
reports against the protected base's required inventory, independently of the
build worker. This is not a full upstream test suite or
live-browser end-to-end certification; describe additional testing in the PR.

## Documentation

MaintainedMost is repository-only. Operator guides live in `docs/` and use
relative Markdown links; there is no website, mailing-list backend or hosted
service to deploy. Keep migration instructions and verification limits current
when changing behavior. Documentation improvements do not need a Go toolchain.

## Picking up a new upstream release

A scheduled workflow reports newer stable server/Calls releases and transcriber
branch commits after checking the full patch series. A clean application is
advisory, not proof of runtime compatibility.

Review and update the source tag and commit together, and the matching server
runtime image digest when changing the server. Check plugin minimum-server
requirements and native dependencies before rebasing patches. Calls source
1.12.3 is deliberately retained with server 11.11.1: Calls 1.12.5 needs server
12. Bundle version 1000.12.4 includes fork fixes and is not a source-tag bump.

Run the complete CI gate, test operator migrations, and document remaining
verification gaps before releasing. Publish the patched transcriber and the
unchanged official recorder in the same offloader-allowed registry namespace
before the server image. Do not use development-mode allowlist bypasses.

The opt-in [VibeCI integration](docs/maintenance.md) automates compatible updates
as one manifest-backed patch transaction. It starts disabled and needs separate
model/image/credential and branch-protection setup. Its patch agent cannot edit
the fork's workflows, scripts or [required test inventory](maintenance/required-tests.json).
Retiring a patch/test or changing toolchains requires a reviewed baseline update.

## Publishing a fork

The release workflow derives registry names from the repository running it.
For local builds, `SOURCE_URL` can name the repository containing the exact
source being built. It supplies source metadata and plugin support links; when
unset those links remain empty rather than pointing at someone else's project.
Configure operator-facing source links as well to meet applicable network
source-offer obligations; an image label alone is not a user-facing offer.

Configure the server, `calls-recorder` and `calls-transcriber` GHCR packages as
**Public**, connect each package to the correct repository, and grant that
repository Actions write access. A public repository does not make a newly
created package public automatically.

The workflow checks anonymous access to both job images before pushing the
server, then checks the server before promoting `latest` or publishing the draft
release. A first release may stop at each of these steps while packages are
private. Correct their settings and rerun the failed `publish` job for the
existing draft. Do not bypass the checks: the offloader pulls job images without
registry credentials.

Report security issues through the process in [SECURITY.md](SECURITY.md), not
in a public issue containing credentials or exploit details.

## No em dashes

Anywhere. Commit messages, comments and docs. Use a comma, brackets,
a colon or a full stop.
