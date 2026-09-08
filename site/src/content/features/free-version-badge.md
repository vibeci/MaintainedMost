---
title: "Remove the Free Version badge from self-hosted Mattermost"
description: "Mattermost shows a Free Version label and an unsupported version notice in the web app. Mattermore removes both. Cosmetic, honestly described."
label: "Licence badges"
order: 10
---

Self-hosted Mattermost puts licence-tier branding in the interface. A "Free
Version" label sits in the product branding, and an "unsupported version"
notice appears on the login and signup screens. Neither changes what the server
does. Both remind you, every day, on software you host and operate yourself,
that you are running the tier that did not pay. Mattermore removes them in the
web app. This is a small change and this page treats it as one.

## What the branding actually is

Two separate things get grouped together under **mattermost free version
badge**:

- The free edition label, which marks the product as the unpaid tier in the
  interface chrome.
- The unsupported version notice, which tells you the build you are running is
  not covered by commercial support.

Neither is a functional restriction. Nothing is gated behind them, no feature
is switched off, and removing them unlocks nothing. They are strings that
render.

That is exactly why they irritate people. A licence gate you can at least argue
with. A permanent label on your own server, doing nothing but reminding you of
your tier, is harder to justify.

## What people do about it today

The usual approaches, none of them great:

- Edit the source strings and rebuild the web app, which means maintaining a
  patch across every upgrade.
- Inject CSS in the browser to hide the elements, which is per-user, per-device
  and breaks whenever the markup shifts.
- Install a third-party plugin that strips the branding, which adds another
  dependency to your server for a cosmetic result.

All three work, and all three cost more attention than the problem deserves.
That is the case for doing it once, in the build.

## Where the strings live

The relevant strings sit in `webapp/channels/src/i18n/en.json`. They render in
three places:

1. The start-trial menu item, which advertises the paid tier from inside the
   product menu.
2. The free edition product branding component, which carries the "Free
   Version" label itself.
3. The header used by the login and signup routes, which is where the
   unsupported version notice appears to anyone reaching your server.

Mattermore removes them at those three points. That covers **mattermost remove
unsupported version banner** and mattermost hide free edition label in one
change, because they share the same rendering surface.

The change is confined to the web app: no server behaviour, no licence
handling, nothing that affects what your users can do.

One honest caveat about its state. This change has not been type-checked yet,
let alone run. It is a few string edits and it may well be fine, but nobody has
compiled it, so it does not belong in the same column as the parts of Mattermore
that were verified on a running server. The [roadmap](/roadmap) tracks which is
which.

## Keep this in proportion

We would rather undersell this than oversell it.

This is cosmetic. It is not a capability, it does not belong in the same
category as the [user limit](/user-limit), [single sign-on](/sso),
[guest accounts](/guest-accounts) or [group calls](/group-calls), and nobody
should pick a build over it. It is included because it costs almost nothing and
removes a daily irritation, not because it matters.

If you are evaluating Mattermore, evaluate it on the features that change what
your organisation can do. This one is a rough edge sanded off.

## Being fair about the unsupported version notice

The notice exists for a reason, and it is a legitimate one.

Warning administrators that they are running a build outside commercial support
is a real safety function. Old Mattermost builds accumulate known security
issues like any other server software, and a visible nudge is a reasonable way
to catch the admin who has not checked their version in a year.

Removing the notice removes that nudge. So the responsibility moves to you: a
Mattermore server still needs keeping up to date, and nothing about hiding a
label makes an old build safer. Mattermore tracks upstream releases for exactly
that reason, so following our builds keeps you current rather than stranding
you on whatever you first installed. See [self-hosting](/selfhost) and
[configuration](/configuration) for how that fits into running the server.

## Scope: Mattermore Server, not the calls plugin

The branding lives in the Mattermost web application, which ships with the
server. It is part of Mattermore Server, our build of Mattermost, and is not
affected by the calls plugin in any way.

Getting this therefore requires running our build rather than the official one,
with the upgrade and trust implications that carries. For a purely cosmetic
change that is not a trade worth making on its own. It is a small bonus for
people already running Mattermore Server. See [install](/install), the
[documentation](/docs) and our [licensing position](/licensing).

## Frequently asked questions

### How do I remove the Free Version badge in Mattermost?

On a stock server you would edit the strings in
`webapp/channels/src/i18n/en.json` and rebuild the web app, hide the elements
with browser CSS, or use a third-party plugin. Mattermore Server removes them
in the build, though that change has not been type-checked yet.

### How do I remove the unsupported version banner in Mattermost?

It renders in the header used by the login and signup routes, from the same
string file as the free edition label. Mattermore removes both together.

### Can I hide the free edition label without editing the source?

Yes, with a browser CSS override or a third-party plugin, but both are
workarounds. CSS applies per browser and breaks when the markup changes, and a
plugin is another dependency for a cosmetic result.

### Does removing the badge unlock any features?

No. The label and the notice are display strings. Nothing is gated behind them,
and removing them changes no server behaviour whatsoever.

### Is it safe to remove the unsupported version notice?

Only if you keep your server updated. The notice has a genuine warning
function, so hiding it moves that responsibility onto you. Mattermore tracks
upstream releases so staying current is straightforward.

### Will a Mattermost upgrade bring the branding back?

On a stock server, yes, any manual edit is overwritten by the next build. With
Mattermore Server the change is in the build itself, so it carries forward.

### Does the Mattermore calls plugin remove the Free Version badge?

No. The branding is in the web application that ships with the server, so only
Mattermore Server affects it.
