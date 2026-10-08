# Call transcription

The default transcriber processes audio locally with whisper.cpp rather than
calling a remote speech API. MaintainedMost removes the Calls licence gate and
builds a patched transcriber with an opt-in OpenAI-compatible backend. You still
need to run the job service and choose where its containers and audio live.

## The default keeps audio on your server

`calls-transcriber` bundles whisper.cpp version 1.7.5, with the tiny, base and
small models built into the image. You can read that off `WHISPER_VERSION` and
`WHISPER_MODELS` in its Makefile. The models ship inside the container, so on
the default backend there is no download step at job time and no API endpoint
to configure.

The consequences are the point:

- The local backend does not send audio to a speech API.
- There is no per-minute cost. You pay in CPU time on hardware you already own,
  not in metered API calls.
- There is no vendor account, no API key to rotate, and no data processing
  agreement to negotiate.

Local processing avoids sending audio to a speech provider, but you still need
to protect the job host, recordings, transcripts and backups.

## Architecture verification

The standalone patched transcriber image has passed a native Linux/arm64 build
and offline checks of local Whisper inference, Opus and Silero/ONNX. Azure SDK
initialization was checked, not a live Azure transcription service.

This verifies the transcriber only. It does not verify the server, Calls,
recorder, offloader or complete Compose stack on ARM, nor a live end-to-end
recording/transcription job. The server image and repository Compose stack
still target Linux/amd64.

To build the standalone ARM transcriber from the repository root, with Docker
Buildx and the [documented build prerequisites](../README.md):

```bash
TARGETARCH=arm64 RUN_TESTS=1 GOFLAGS=-p=2 ./scripts/build-transcriber.sh
```

The script defaults to `TARGETARCH=amd64` when unset and accepts `amd64` or
`arm64`. Its default local image tag is
`maintainedmost/calls-transcriber:v1.0.0-dev0`; building it does not publish it.

## The remote backends are an opt-in, and they send your audio away

Local whisper.cpp is the default, not the only option. The backend is selected
with the `TRANSCRIBE_API` variable on the transcriber job, and it takes three
values:

- `whisper.cpp`, the default. Speech processing runs on the job host without a
  remote speech API; the job still communicates with the Mattermost server.
- `azure`, Azure Cognitive Services Speech. This one is upstream's rather than
  ours: the stock calls plugin already offers an "Azure AI" choice for its
  `TranscribeAPI` setting, next to `TranscribeAPIAzureSpeechKey` and
  `TranscribeAPIAzureSpeechRegion`.
- `openai/api`, MaintainedMost's backend for services implementing OpenAI's
  `/v1/audio/transcriptions` protocol. Provider compatibility requires a trial;
  it is not guaranteed by the API name.

Selecting a remote backend sends participants' audio to the configured service.
That may be your own server or a third party with separate retention and data
use policies. Review consent and data handling before opting in. Selecting the
patched image alone does not change the local Whisper default.

The API key is sent as a bearer token; keep it out of source control and support
reports. Live captions use local whisper.cpp independently of this setting.

The caveat we would rather you heard from us: the OpenAI-compatible backend has
only ever been tested against a mock server standing in for the API, never
against a real provider. Request handling, retries, chunking and timeouts are
covered by tests written against that mock. How an actual endpoint behaves is
not something we have verified. Run your own trial before you rely on it.
Interrupted response-body reads and other transient failures have bounded
retries (at most three attempts); invalid complete JSON is rejected without
retry. Tests cover these distinctions, not every remote provider's behavior.

## Configure the remote backend

Set these variables on the **Mattermost server/plugin process**, which passes
the job-only values to the transcriber. For Compose, add them to the
`maintainedmost` service's `environment` block, not just to `.env`:

```yaml
MM_CALLS_JOB_SERVICE_IMAGE_REGISTRY: ghcr.io/OWNER/maintainedmost
MM_CALLS_TRANSCRIBER_IMAGE: ghcr.io/OWNER/maintainedmost/calls-transcriber:v1.5.0
MM_CALLS_TRANSCRIBER_TRANSCRIBE_API: openai/api
MM_CALLS_TRANSCRIBER_OPENAI_API_BASE_URL: https://speech.example.com/v1
MM_CALLS_TRANSCRIBER_OPENAI_API_KEY: "your-provider-key"
MM_CALLS_TRANSCRIBER_OPENAI_API_MODEL: whisper-1
MM_CALLS_TRANSCRIBER_OPENAI_API_TIMEOUT_SECONDS: "60"
```

Replace the example owner/release with a tested build and inject the real key
securely. The namespace must match `CALLS_IMAGE_REGISTRY` in repository Compose
and offloader `JOBS_IMAGEREGISTRY`. It must contain both
`calls-recorder:<calls_recorder_version>` (the unchanged official image at the
pinned Calls manifest's version) and the patched `calls-transcriber:<release-tag>`.
The offloader validates job-image prefixes and semantic version tags in one
namespace; do not use `DEV_MODE` to bypass this.

Leave the plugin's `TranscribeAPI` field at its supported default,
`whisper.cpp`. **`MM_CALLS_TRANSCRIBE_API=openai/api` is invalid** in this Calls
version. The extra `TRANSCRIBER_` prefix applies the override to the job after
plugin validation. `/audio/transcriptions` is appended to the base URL, so the
example includes `/v1` but not the final endpoint path. Use HTTPS for remote
audio and credentials. See [recording](recording.md) for job-service setup.

## How it works

Transcription is a job, like [recording](recording.md). The plugin does not
do the work itself. It talks to a separate service called `calls-offloader`,
which launches a container for each job. MaintainedMost builds its patched
transcriber from the commit in `upstream.env`, using the upstream native
Whisper/Opus/ONNX/Azure build. The unmodified official
`mattermost/calls-transcriber` image does not contain the OpenAI-compatible
patch. The release workflow publishes the patched transcriber before the server
that selects it.

`calls-offloader` is dual licensed the same way the plugin is: AGPL-3.0 for the
source, MIT for compiled versions produced by Mattermost, Inc. It needs Docker
access, because launching containers is how it does its work.

## What MaintainedMost changes, and what it does not

Upstream checks `TranscriptionsAllowed()` before starting a transcription job,
and that check wants an Enterprise licence. MaintainedMost patches the check out.
The Calls checker change is in AGPL code; the transcriber is based on
Apache-2.0 upstream code. The patch policy avoids Source Available directories.
See [licensing](licensing.md) for the source and image-layer distinction.

Lifting the gate does not conjure the service. This is the sentence to take
away from this page. MaintainedMost makes transcription permitted; it does not make
it run. You still have to deploy `calls-offloader` next to Mattermost, give it
Docker access, and let it pull the transcriber image.

A configured job-service URL enables transcription by default only when no
explicit administrator choice exists. Environment overrides and persisted
`false` values are honored. The job service still has to be ready. The
[self-hosting guide](selfhost.md) covers the full stack, and [install](install.md)
covers the plugin swap on an existing server.

## Choosing a model

The image gives you tiny, base and small. Larger models are more accurate and
slower. That trade-off is the only tuning knob most people will touch, and the
right setting depends on your hardware and on how much a wrong word costs you.

No accuracy figures appear on this page because none have been measured here.
Run a real call through each model on your own hardware, read the output, and
pick. That takes an afternoon and beats any number someone quotes at you.

## What is not claimed here

Being clear about the edges is more useful than an optimistic feature list.
This page does not claim GPU acceleration, live captioning during a call,
speaker labelling, or support for any particular list of languages. None of
that has been verified for this build. Treat transcription as something that
produces a transcript from a call, and test anything beyond that yourself
before you promise it to anyone.

## Is it worth it?

More than recording is, in most cases, because the alternative is worse.
Recording competes with someone hitting record on a laptop. Transcription
competes with sending your meetings to a cloud vendor, and for a lot of
self-hosted organisations that is not an option at all.

The operational cost is real and it is the same cost as recording: a second
service, Docker on the host, and CPU headroom for the job containers. This is a
larger commitment than [group calls](install.md), which do not need a job
service. If you already run `calls-offloader`, transcription reuses it but still
needs a transcriber image and additional compute capacity.

## Frequently asked questions

### Can Mattermost transcribe calls without an Enterprise licence?

Yes, with MaintainedMost. It removes the `TranscriptionsAllowed()` check in
AGPL-3.0 code. You still need `calls-offloader` running, because the licence
check is not the only requirement.

### Does Mattermost call transcription send audio to the cloud?

Not on the default backend. `calls-transcriber` runs whisper.cpp inside the
container on your own host and no audio is sent to an external speech API. If
you select `azure` or `openai/api` for the job, audio goes to the configured
endpoint, which may be your own service or a third party. Those are opt-in.

### What speech to text engine does Mattermost use?

whisper.cpp version 1.7.5, with the tiny, base and small models built into the
patched transcriber image built from the pinned source. Check its Makefile when
upgrading rather than assuming model versions never change.

### How do I transcribe Mattermost calls locally?

Install MaintainedMost, run `calls-offloader` alongside Mattermost with Docker
access so it can launch the transcriber container, then enable transcription in
the plugin settings. The [self-hosting guide](selfhost.md) has the full stack.

### Does Mattermost whisper transcription support GPUs or speaker labels?

Not something this page will claim. GPU support, speaker labelling, live
captioning and language coverage have not been verified here, so test them
before you rely on them.

### Is call transcription available on Mattermost Cloud?

No. MaintainedMost targets self-hosted Mattermost. Cloud installations cannot load
a replacement plugin.

### What does self hosted meeting transcription cost to run?

CPU time and one extra service. On the default backend there is no per-minute
API charge. The upstream recorder and transcriber use Apache-2.0 licences;
check the release for image access and included dependencies. Your real cost is
the host that runs the job containers. A remote provider may charge separately
for API usage.
