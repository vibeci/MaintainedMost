---
title: "Raising the user limit on self-hosted Mattermost"
description: "Mattermost Team Edition caps an unlicensed server at 250 users. Where the cap lives, how much it tightened in v11, and how Mattermore raises it."
label: "User limit"
order: 8
---

In v11 Mattermost cut the user cap on an unlicensed self-hosted server to 250,
one of the most-cited reasons people left the project. The previous cap was
substantially higher, high enough that most self-hosted servers never came near
it. This page explains where the mattermost user limit is enforced, what
Mattermore changes, and why the 10,000 message history complaint you have read
about is a different problem with a surprising cause.

## Scope: this is Mattermore Server, not the calls plugin

Read this first. The user cap is enforced in the Mattermost server, not the
Calls plugin, so no plugin upload can affect it. That makes it unlike
[group calls](/group-calls), [video calls](/video-calls),
[call recording](/call-recording) and [transcription](/transcription).

Raising the cap means running Mattermore Server, our build of Mattermost itself,
rather than the official binary. That changes how you handle upgrades, security
advisories and your own trust model. Decide it deliberately, as you would for
[single sign-on](/sso).

Nothing here announces a release. For current status, check the
[Mattermore repository](https://github.com/dennisklappe/mattermore).

## Where the cap actually lives

The limits are defined in `server/channels/app/limits.go`. An unlicensed server
gets two numbers rather than one:

- A soft limit of 200 users. You are warned, but the server keeps working.
- A hard limit of 250 users. Past this, the server refuses.

Enforcement happens in `server/channels/app/user.go`, at two moments: creating a
user and reactivating a deactivated one. That second one catches people out.
Deactivating a leaver frees a seat, so a server at the cap looks fine until
someone returns from leave and cannot be switched back on.

So the mattermost team edition user cap is not a licence check bolted onto
the application. It is a pair of constants consulted on the account lifecycle
paths.

## It used to be much higher

Worth stating plainly, because a lot of the advice online predates the change.
Before v11 an unlicensed server could carry far more than 250 users.

We are not going to put a figure on it here. The numbers circulating in forum
posts are second-hand, and the v11 source we check everything else on this site
against only tells us what the cap is now, not what it replaced. If you need
the exact earlier value, Mattermost's own release notes are the place to get
it, not us.

The direction and the size of the change are not in doubt. The old cap was
effectively no limit for a small company or a community server. 250 is a number
you can hit. Plenty of organisations discovered the mattermost 250 user limit
the day they tried to onboard a new intake, and a good number went looking for
another chat server rather than a quote.

## What Mattermore changes

Mattermore raises both limits by a factor of 1000. Soft and hard keep their
existing ratio, which matters more than it sounds: every code path that consults
these values, including the warning banners and the admin console messaging,
still behaves exactly as upstream designed it. Nothing is stubbed out and no
enforcement branch is deleted. The constants are simply larger.

This is the same approach Framasoft take in Mostlymatter, which has done exactly
this since 2024. Worth knowing: a non-profit has been shipping and running the
change in production for years. It is not exotic.

The mattermost free user limit on a Mattermore server is therefore a
practical question about your database, hardware and uplink rather than a
constant in a Go file.

## The 10,000 message cap is not what you think

This deserves its own section, because the internet has it backwards and it
costs people their history.

The widely complained-about 10,000 message limit is **not** a Team Edition
restriction. It comes from `License.Limits.PostHistory` and applies only to
servers running an Entry licence. An unlicensed Team Edition server has
unlimited message history.

Follow that through, because it is a genuine trap. People hit the user cap or
want single sign-on, so they take an Entry licence to solve it, and move from an
unlicensed server with unlimited history to a licensed server capped at 10,000
posts. They fixed one limit and acquired a worse one.

With Mattermore you do not have to make that trade. The server stays unlicensed,
history stays unlimited, and the user cap and [SSO](/sso) are handled in the
build itself.

## Be fair to Mattermost about this

Mattermost open sourced this code under the AGPL. They did not have to. Anyone
can read `limits.go` and change it only because Mattermost published it rather
than shipping an opaque binary.

If your organisation can afford Professional, buy it. It funds the upstream work
Mattermore depends on entirely. Mattermore exists for the case where the licence
is genuinely out of reach, not as a way to avoid paying people who can be paid.
The full argument is on [licensing](/licensing).

## Frequently asked questions

### What is the Mattermost user limit?

An unlicensed self-hosted server has a soft limit of 200 users and a hard limit
of 250, defined in `server/channels/app/limits.go` and enforced when a user is
created or reactivated.

### Why did Mattermost tighten the user limit in v11?

Version 11 introduced the 200 soft and 250 hard limits for unlicensed servers,
defined in `server/channels/app/limits.go`. The cap before that was
considerably higher. We do not quote the earlier number because we cannot read
it out of the current source, so check Mattermost's release notes for it. The
change is one of the most common reasons cited by people who left the project.

### How do I remove the 250 user limit in Mattermost?

Buy a licence, or run a build with different constants. Mattermore Server raises
both limits by a factor of 1000, keeping their ratio so every existing code path
still behaves correctly.

### Does Mattermost Team Edition limit message history to 10,000 posts?

No. That cap comes from `License.Limits.PostHistory` and applies to servers
running an Entry licence. An unlicensed Team Edition server has unlimited
history.

### Should I take a free Entry licence to get past the user cap?

Be careful. Moving from unlicensed to Entry gets you features but imposes a
10,000 post history limit you did not previously have, and it swaps the
hard-coded 250 for whatever seat count the licence itself carries rather than
removing the cap. Many people discover the history part only after their
messages start disappearing.

### What happens when my server hits the user cap?

New accounts are refused, and so is reactivating a deactivated one. If you
deactivate leavers to stay under the cap, expect the reactivate path to be where
you notice.

### Does the Mattermore calls plugin raise the user limit?

No. The cap is enforced in the server, so a plugin cannot touch it. This is part
of Mattermore Server. See [install](/install), [selfhost](/selfhost) and
[docs](/docs).
