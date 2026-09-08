---
title: "Channel moderation on self-hosted Mattermost"
description: "Why per-channel moderation needs a paid Mattermost licence, and what lifting that gate on a self-hosted server involves."
label: "Channel moderation"
order: 6
---

Channel moderation controls what members and guests may do inside one specific
channel: post messages, reply in threads, use reactions, manage members. It is
how you make an announcements channel read-only, or stop guests inviting other
guests. On a self-hosted server the feature is fully implemented in the open
source code and refused by a licence check.

## What the restriction is

Two API endpoints, `GET` and `PUT` on
`/api/v4/channels/{channel_id}/moderations`, begin by asking whether a licence
exists. Without one they return 403 and the message you have probably seen:

```
Your license does not support channel moderation
```

The System Console hides the panel for the same reason. Nothing you set in
configuration makes it appear, because the gate is not a setting.

## Is it a licence gate or a real limit

A licence gate, and a thin one. Channel moderation is not a separate
subsystem: it reads and writes the channel's own permission scheme, which the
server maintains for every channel on every server, licensed or not. The
scheme exists, the permissions resolve, the enforcement runs. The only thing
that ever refused was the check at the top of the handler.

## What lifting it involves

Removing the licence check from both endpoints, and the matching check in the
System Console that decides whether to render the panel. Both files are AGPL
licensed, so this is a change to open source code rather than anything under
Mattermost's Source Available Licence.

Mattermore does exactly that. The permission logic underneath is untouched:
the same scheme, the same enforcement, the same audit records.

## Frequently asked questions

### What can I actually control per channel?

Whether members can post, reply in threads, react to posts and manage channel
members, and separately whether guests can do the same. Each is a checkbox
against the channel's permission scheme.

### Does this weaken permissions in any way?

No. Lifting the gate exposes the panel that edits the scheme. What the server
does with that scheme afterwards is unchanged, including who is allowed to
edit it.

### Is the channel moderation code in the open source repository?

Yes. The endpoints, the permission schemes and the System Console panel are
all present. The licence check is the only thing standing between the code and
a working feature.

### Do I need the Mattermore plugin for this?

No. Channel moderation lives in the server, so the calls plugin cannot affect
it. You need the Mattermore server image or a build from our patches.

### Where do I start if I want to try this?

Read [Mattermore's licensing position](/licensing) first, then the
[installation guide](/install) and the rest of the [documentation](/docs).
