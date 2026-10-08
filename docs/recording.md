# Recording and transcription

MaintainedMost removes the paid-licence checks for recording and transcription.
Jobs still need a ready offloader, reachable server, correctly named images
and sufficient resources. A visible button is not proof that a job can run.

## Why a second service

A recorder joins the call as a headless client and captures voice and shared
screen. The unchanged official recorder does **not** capture MaintainedMost's new
group camera streams. Transcription uses local Whisper by default. These jobs
run in separate containers with their own CPU and memory needs.

So a small service called `calls-offloader` starts a container per job. That is
upstream's design, not a paywall. MaintainedMost lifts the licence check that used
to refuse the feature; the machine to run it on is still yours.

The offloader source is AGPL v3, and the upstream recorder and transcriber have
Apache-2.0 licences. See [licensing](licensing.md) for the distinction between
source changes and the contents of distributed images.

## With the compose file

The repository's [`compose.yaml`](../compose.yaml) runs the job service and
points MaintainedMost at it. Configure `.env` with `SITE_URL`, `POSTGRES_PASSWORD`, a tested
`MAINTAINEDMOST_IMAGE` and the shared `CALLS_IMAGE_REGISTRY` namespace before starting:

```bash
docker compose config --quiet
docker compose up -d
```

The namespace must contain the unchanged official
`calls-recorder:<calls_recorder_version>` from the pinned Calls manifest and
the release's patched `calls-transcriber:<release-tag>`. The offloader allows
one registry namespace and enforces job-image names and semantic version tags.
Do not bypass the allowlist with `DEV_MODE`. See [self-hosting](selfhost.md) for
the full image layout; do not replace the patched transcriber with the official
image if you need the OpenAI-compatible backend.

## Defaults and opt-outs

A nonblank job-service URL sets the **default availability** of recording and
transcription only when there is no explicit choice. Valid environment values
take precedence over stored configuration, and stored `false` values are
preserved. For example, these server environment settings remain off even with
a job service configured:

```ini
MM_CALLS_ENABLE_RECORDINGS=false
MM_CALLS_ENABLE_TRANSCRIPTIONS=false
```

`RecordCallsByDefault` (or `MM_CALLS_RECORD_CALLS_BY_DEFAULT`) controls automatic
recording separately and is **off by default**. If enabled, automatic start
checks readiness, the original call identity, current host and permissions.
An unavailable/unready offloader, changed call or other failed prerequisite
causes a safe skip with a warning, not a promise of eventual retry. Check logs
and start a recording explicitly when appropriate.

Participants retain recording banners/notices. Enabling automatic recording
does not remove consent, notice or retention obligations.

## On an existing server

Use the pinned offloader configuration in the repository as the reference. Keep
its API on a private network reachable only by authorized servers, and ensure
job containers can reach the server's HTTPS site URL. Do not publish its
self-registration API on port 4545 to untrusted clients.

Then set `MM_CALLS_JOB_SERVICE_URL` on the Mattermost server, or fill in
**System Console → Plugins → Calls → Job service URL**. Both work; the
environment variable wins.

For a plugin-only install, set offloader `JOBS_IMAGEREGISTRY` and server
`MM_CALLS_JOB_SERVICE_IMAGE_REGISTRY` to the same namespace. Set
`MM_CALLS_TRANSCRIBER_IMAGE` on the server to the patched
`calls-transcriber:<release-tag>` image in that namespace. The recorder
keeps the version declared in the Calls manifest. Check service readiness and
image pulls in the logs, then test a real recording and transcript.

## What the Docker socket is for

The job service creates and destroys containers, so it needs the Docker API.
That is a real privilege: anything that can talk to the Docker socket can
start a container as root on that host.

If you would rather not grant it, run the job service on a separate host that
does nothing else, or leave it out and explicitly disable recording and
transcription. Audio calls and screen sharing do not require this service.
Removing a service does not clear existing saved settings automatically.

## Transcription without a GPU

Local Whisper is the default; throughput depends on the model and hardware,
so measure it with representative calls. The OpenAI-compatible backend is
opt-in through server-side job environment overrides, not the plugin's
`TranscribeAPI` field. See [transcription](transcription.md) for the exact variables
and provider-testing limitations.
