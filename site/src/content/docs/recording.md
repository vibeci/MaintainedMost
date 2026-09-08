---
title: Recording and transcription
description: Turn on call recording and transcription in Mattermore, and what the job service is for.
order: 6
---

Recording and transcription work in Mattermore with no licence and no console
visit, provided one extra service is running.

## Why a second service

A recording is made by a browser that joins the call and captures the window.
A transcription is Whisper run over that recording afterwards. Neither can
happen inside the plugin, because the plugin lives in the Mattermost server
process and these jobs need their own CPU, memory and a container to run in.

So a small service called `calls-offloader` starts a container per job. That is
upstream's design, not a paywall. Mattermore lifts the licence check that used
to refuse the feature; the machine to run it on is still yours.

Everything involved is open source: `calls-offloader` is AGPL v3, and the
recorder and transcriber are Apache 2.0.

## With the compose file

Nothing to configure. `compose.yaml` in the repository already runs the job
service and points Mattermore at it:

```
cp .env.example .env      # set SITE_URL and POSTGRES_PASSWORD
docker compose up -d
```

Recording and transcription turn themselves on when a job service is
configured, so the buttons are there on first login.

## On an existing server

Run the service somewhere it can reach your server, and your server can reach
it:

```
docker run -d --name calls-offloader \
  --restart unless-stopped \
  -e API_SECURITY_ALLOWSELFREGISTRATION=true \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -p 4545:4545 \
  mattermost/calls-offloader:latest
```

Then set `MM_CALLS_JOB_SERVICE_URL` on the Mattermost server, or fill in
**System Console → Plugins → Calls → Job service URL**. Both work; the
environment variable wins.

Check it is up:

```
curl http://localhost:4545/version
```

## What the Docker socket is for

The job service creates and destroys containers, so it needs the Docker API.
That is a real privilege: anything that can talk to the Docker socket can
start a container as root on that host.

If you would rather not grant it, run the job service on a separate host that
does nothing else, or leave it out. Without a job service the record button
does not appear, and calls, group video, screen sharing and everything else
keep working.

## Transcription without a GPU

Whisper runs on CPU by default and a meeting transcribes in roughly real time
on a few cores. Mattermore also accepts any OpenAI-compatible endpoint if you
would rather send the audio somewhere with a GPU, which is off unless you
configure it.
