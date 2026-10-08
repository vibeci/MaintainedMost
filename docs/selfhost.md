# Self-host from scratch

This deploys MaintainedMost Server, PostgreSQL and the Calls job service. For a
plugin-only installation on an existing server, use [install](install.md).

## What you need

- A Linux/amd64 host with Docker and the Compose plugin. The server image and
  repository Compose stack target Linux/amd64.
- A tested MaintainedMost server release and its matching job images in a registry
  you trust. Building the native transcriber yourself requires Docker Buildx.
- A reachable HTTPS site URL and UDP 8443 for call media. The repository Compose
  file also publishes TCP 8443; configure the RTC fallback if you need it.

The standalone transcriber has passed a native Linux/arm64 image build and
offline inference checks. That does not verify the server, Calls, recorder,
offloader or complete Compose stack on ARM. See
[transcription](transcription.md#architecture-verification).

## 1. Create the stack

Use [`compose.yaml`](../compose.yaml) and [`.env.example`](../.env.example) from
the same repository revision as the release you selected, rather than an
independently copied stack with stale image versions. Configure these required
values in `.env`:

```ini
MAINTAINEDMOST_IMAGE=ghcr.io/OWNER/maintainedmost:v1.5.0
CALLS_IMAGE_REGISTRY=ghcr.io/OWNER/maintainedmost
POSTGRES_PASSWORD=use-a-long-random-string-here
SITE_URL=https://chat.example.com
```

Replace `OWNER` and `v1.5.0` with your chosen registry and tested release; these
are layout examples, not a claim that those images have been published. Do not
assume the previous publisher's `latest` contains the current fixes.

`CALLS_IMAGE_REGISTRY` is a namespace, without a trailing slash or image tag.
Both of these images must be available there:

- `ghcr.io/OWNER/maintainedmost/calls-recorder:<calls_recorder_version>`, mirrored
  unchanged from upstream. The version comes from the pinned Calls
  `plugin.json` property `calls_recorder_version`, not the server release tag.
- `ghcr.io/OWNER/maintainedmost/calls-transcriber:v1.5.0`, the patched transcriber
  built for this release, not the unpatched official image.

Use the job namespace selected when the server image was built. Compose passes
it to Calls as `MM_CALLS_JOB_SERVICE_IMAGE_REGISTRY` and to the offloader as
`JOBS_IMAGEREGISTRY`. The server image selects its matching patched transcriber;
changing `.env` alone does not rewrite that baked image reference. The offloader
allows one registry namespace, the expected job-image names and semantic
version tags. A server
tag with `-transcriber` appended is not the required image layout. Do not use
`DEV_MODE` to bypass validation. Release publication makes the job images
available before publishing the server.

`SITE_URL` must match the address users and job containers can reach. Keep the
database password and other credentials out of version control.

For an existing deployment, set `COMPOSE_PROJECT_NAME` to its current project
name before starting this stack. Both the named volumes and the offloader's
network depend on that name. Follow the
[Compose migration guidance](upgrading.md#preserve-an-existing-compose-deployment)
instead of accepting a new project name and inadvertently using empty volumes.

## 2. Start it

```bash
docker compose config --quiet
docker compose pull
docker compose up -d
```

Open `SITE_URL` and create the first account. That account becomes the system
administrator.

The MaintainedMost server image already includes the patched Calls plugin; no
separate upload is needed. Existing config volumes retain administrator
settings, which may differ from defaults on a new installation.

## 3. Check it works

1. Open a channel that is not a direct message
2. Confirm the call button appears in the channel header
3. Start a call and have two colleagues join

Check the plugin reports **Calls (MaintainedMost)**. Test audio, screen sharing and
any camera features you plan to use with real clients. CI is not a substitute
for live media verification on your network.

The job-service URL supplies recording/transcription defaults only; stored or
environment `false` values are respected. Automatic recording is off by default.
Test an explicitly started recording and a local transcript before relying on
them. The official recorder captures voice and shared screen, not MaintainedMost's
group camera streams. See [recording](recording.md).

## Putting it behind TLS

Mattermost serves plain HTTP on 8065. Terminate TLS in front of it, allow
WebSocket upgrades, and restrict direct access to 8065 to the intended proxy
and private network. The Compose file does not configure certificates or an
HTTPS reverse proxy for you.

Calls media does not go through the web reverse proxy. Make the advertised RTC
address and port reachable, and configure any NAT or TURN requirements for your
network rather than assuming an HTTPS proxy carries media too.

## Keep the offloader private

The offloader mounts the Docker socket and runs with effectively host-root
privileges. Its API is internal to the Compose network and uses
self-registration; do not publish port 4545 to untrusted clients. If that trust
boundary is unsuitable, isolate the job host or omit recording/transcription.
Calls do not need the offloader for audio or screen sharing.

## Backups

Back up the database, uploaded files, server configuration, plugin state and
deployment secrets as a consistent set. Use the actual volume names from your
deployment; Compose prefixes them with the project name. Test restoration in
an isolated environment, and retain the previous image references and matching
configuration for rollback. A database-only backup omits uploaded files.

## Upgrading

Read [upgrading](upgrading.md) before changing the pinned image. Test the release
with your restored data and job-image namespace first. For SSO deployments,
verify a local administrator recovery account before migrating: discovery,
issuer matching, strict TLS and verified UserInfo requirements can reject
configurations accepted by older builds.

## Troubleshooting

Check server/offloader logs, plugin activation and channel settings before
changing security controls. [Troubleshooting](troubleshooting.md) covers media,
image allowlist, OIDC and plugin-signature failures.
