---
title: "Unlimited message history on self-hosted Mattermost"
description: "Mattermost v11 caps history at 10,000 messages under an Entry licence. Here is where the cap lives, and why a Team Edition base never hits it."
label: "Message history"
order: 9
---

Mattermost v11 introduced a 10,000 message history cap. Your older posts are
still sitting in your own PostgreSQL database, untouched. The interface just
stops showing them to you. That detail is why this is the single most
complained-about restriction in self-hosted Mattermost: it is your server, your
database and your data, and the product has decided you cannot read part of it.
This page explains where the cap comes from, when it applies, and why a
Mattermore server is not subject to it.

## What the Mattermost message limit actually does

The behaviour is narrow and worth stating precisely. Nothing is deleted.
Channels keep working, new posts keep arriving, and backups are unchanged. What
stops is reading: scrollback, search and permalinks refuse to return posts
older than the cap, and the client shows a wall where the rest of the
conversation used to be.

So a mattermost message history cap is a read restriction, not a retention
policy. Data retention settings, exports and the database itself are not
involved. Lift the restriction and the old posts reappear, because they never
went anywhere. That is also why people find it so frustrating: a deletion is at
least honest about what it costs you.

## Where the Mattermost 10000 message limit is enforced

We checked this in the Mattermost source rather than relying on documentation.

The limit is a licence value, `License.Limits.PostHistory`. The enforcement
path is short:

- `server/channels/app/limits.go` holds the limit handling.
- `GetLastAccessiblePostTime` in `server/channels/app/post.go` turns that limit
  into a cutoff timestamp, the point in time before which posts stop being
  readable.
- `server/channels/app/post_helpers.go` applies the filtering, so reads come
  back trimmed at the cutoff.

Nothing in that chain touches the rows. The posts stay where they are and the
server declines to hand them over.

## The part that matters: the cap needs an Entry licence

Here is the whole reason this page exists.

`License.Limits.PostHistory` is a licence field, and it carries a value only
when the server runs an Entry licence. An unlicensed Team Edition server has no
such licence, so there is no limit to compute a cutoff from and
`GetLastAccessiblePostTime` has nothing to trim against.

Mattermore builds on unlicensed Team Edition. That means **mattermost unlimited
history self hosted** is not something Mattermore adds. It is something a Team
Edition server already has, and never lost. There is no patch here, no modified
enforcement path, and nothing to keep working across upgrades.

We would rather say that plainly than dress it up. Most of this site documents
gates Mattermore lifts. This one documents a gate you are not behind.

## The trap people actually fall into

If Team Edition has full history, why does anyone hit the cap?

Because of the trade between the two free tiers, and it is a genuinely awkward
one:

- **Entry** gives you single sign-on and caps history at 10,000 messages. It
  does not give you an unlimited number of users, which is worth correcting
  because the internet often says it does. `server/channels/app/limits.go`
  hard-codes 200 and 250 only when there is no licence at all. With a licence
  that enforces seats, the cap becomes `license.Features.Users`, and the hard
  limit that number plus `license.ExtraUsers`. A licence moves the seat count
  rather than removing it, and what Entry is actually issued with is a question
  for Mattermost, not something the source tells you.
- **Team** gives you full mattermost entry edition history in the sense
  that it is not capped at all, plus a 250 user limit and no SSO.

Neither is a clean win, and the move between them is not always deliberate.
Upgrading can silently convert a Free server into an Entry one, and admins
report discovering the change only when old messages, pinned ones included,
stop opening. Nobody chose to trade their archive for SSO. They upgraded a
server.

## What Mattermore changes here

Mattermore does not patch the history cap. It removes the reason you would ever
accept it.

The two things Entry offers that Team does not are exactly the two things
Mattermore Server addresses: the [user limit](/user-limit) is raised in the
build rather than swapped for a licensed seat count, and
[single sign-on](/sso) is provided. Both sit on a Team Edition base that never
had a mattermost message limit to begin with.

So the choice stops being a trade. Full history, your users and SSO, on one
server. See [self-hosting](/selfhost) for what running that build involves, and
[licensing](/licensing) for the reasoning behind it.

## Scope: this is Mattermore Server, not the calls plugin

Message history is implemented in the Mattermost server, so the calls plugin
and [group calls](/group-calls) have nothing to do with it. What matters is
which build of Mattermost you run.

Running Mattermore Server means running our build instead of the official one,
which changes how you handle upgrades and security advisories. Decide that
deliberately. [Installation](/install), [configuration](/configuration) and the
[documentation](/docs) cover the practicalities.

## Frequently asked questions

### Does Mattermost limit message history?

On a self-hosted server running an Entry licence, yes. Mattermost v11
introduced a 10,000 message cap enforced through `License.Limits.PostHistory`.
An unlicensed Team Edition server is not affected.

### What is the Mattermost 10000 message limit?

It is a licence limit that stops the server returning posts older than the most
recent 10,000. Scrollback, search and permalinks stop there.

### Does the Mattermost message history cap delete old messages?

No. The posts remain in your PostgreSQL database exactly as they were. The cap
is a read filter applied when the server answers a request, not a deletion or a
retention rule.

### Does Mattermost Team Edition have a message history cap?

No. The cap comes from a licence field, and an unlicensed Team Edition server
does not carry one. Full history is the normal behaviour there.

### Why did my old Mattermost messages disappear after an upgrade?

Most likely your server moved onto an Entry licence during the upgrade. Entry
carries the 10,000 message limit, so older posts, including pinned ones, stop
being readable even though they are still stored.

### Does Mattermore remove the Mattermost message history cap?

Not by patching it. Mattermore builds on Team Edition, which has no cap to
remove. This page is documentation rather than a feature claim, and we would
rather be clear about that than overclaim.

### Can I have unlimited history, SSO and no user limit on one server?

That is the point of Mattermore Server. Full history comes from the Team
Edition base, while [single sign-on](/sso) and the lifted
[user limit](/user-limit) come from Mattermore.
