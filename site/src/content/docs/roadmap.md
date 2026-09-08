---
title: "Roadmap"
description: "What Mattermore does today, why group video is not simply a licence check, and where the live plan is tracked."
order: 9
---

Planning happens on GitHub, not here. This page explains what works today and
why the remaining pieces are the shape they are, because that explanation is
stable. Anything with a status attached lives in the tracker, where it stays
correct without anyone remembering to update a website.

- [Project board](https://github.com/dennisklappe/mattermore/projects) for what
  is being worked on
- [Issues](https://github.com/dennisklappe/mattermore/issues) for individual
  features and bugs
- [Releases](https://github.com/dennisklappe/mattermore/releases) for what has
  actually shipped

## What works today

This list means verified on a running server, not read off the source.

Group audio calls in any channel, on any self-hosted server, with no licence.
Screen sharing works in those calls, as it already did upstream.

The other licence gates in the calls plugin are lifted and confirmed on the
same unlicensed server: call recording, transcription, host controls and the
rtcd gate. Recording and transcription still need `calls-offloader` deployed
before they do anything, which is an infrastructure job rather than a licence
one.

Single sign-on works end to end. The full OpenID Connect login was run against
Keycloak on a Mattermore server and returned a working session. See
[single sign-on](/sso).

Video works in direct messages, experimental, exactly as upstream ships it.

## Written, but not yet verified

Three things are in the tree and have not earned the word "works".

**Group video** is written: streams mapped by sender, a participant grid, and
the direct message restriction removed. It has never been run in a browser.
Until three cameras have been in one channel call and someone has looked at the
result, treat it as code rather than a feature.

**The licence badge removal** has not been type-checked. It is a small change
to web application strings and it may well be fine, but nobody has compiled it
yet.

**A Docker image** does not exist. The calls plugin bundle is what you can
download today. Running the server-side features means building Mattermore
Server yourself.

## Why group video is not just a licence check

This is the most common question, and the answer is not "Mattermost locked it".
Verified against upstream at tag `v1.12.3`:

- `webapp/src/components/expanded_view/component.tsx` lines 919 and 964 both
  carry the comment "Here we are assuming this only renders in a DM which is
  the case right now".
- The interface renders `connectedDMUser` and `otherSessions[0]`, that is
  exactly one remote participant, and holds a single `otherVideoStream` in
  state.
- `client.ts:962` returns only the last remote video track.
- `connectedDMUser` is undefined outside a direct message, so removing the DM
  check on its own would show your own camera and nothing else.

So there is no switch to flip. It needs a real port.

The encouraging part is that the transport already carries what is needed.
`client.ts:620` pushes into a `remoteVideoTracks` array, and every remote track
arrives with `TrackInfo = {type, sender_id}` from `@mattermost/calls-common`.
The client simply discards `sender_id`.

The work is to map streams by sender, replace the single-stream state with a
keyed map in both components, and write a grid that handles more than two
people. All of it in AGPL code, and all of it now written. What it has not had
is a browser, which is why it sits above under written rather than working.

## Where group video actually stands

It works, and that is from a real test rather than a guess. Three browsers in
one call with cameras on: every participant sees the other two, each stream
distinct, and a newcomer replacing someone who left appears correctly.

Getting there took two attempts. The first trusted `sender_id` from the media
map, which turns out to be wrong at the source: upstream's own
`rtcd/service/rtc/session.go` sets that field to the *receiving* session, so
every client attributed every remote track to itself and the streams collapsed
onto one key. The sender is instead recoverable from the track naming the SFU
uses, which is what Mattermore does now.

It is much newer than the audio path, so treat it as new code.

## Why some things stay out of reach

Not every gate is liftable, and it is worth being precise about which.

Some Mattermost features are gated in AGPL code but their implementation ships
in a separate private module, LDAP and SAML among them. Removing those checks
produces a menu item that fails at runtime, not a working feature. Mattermore
does not touch them.

Others sit in files carrying the Mattermost Source Available Licence rather
than the AGPL. Mattermore does not modify those, which is what keeps the
project distributable. See [licensing and legality](/licensing).

## What will not happen

No support contract, and no warranty. This is a volunteer fork under the AGPL.

If group calls are load-bearing for your organisation, buying Professional is a
reasonable answer, and it funds the upstream work this fork depends on.
