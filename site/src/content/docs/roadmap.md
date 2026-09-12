---
title: "Roadmap"
description: "Where Mattermore's plan lives, and why group video needed more than removing a licence check."
order: 9
---

Planning happens on GitHub. This page does not repeat it, because a status
copied onto a website is wrong the moment it changes and nobody remembers to
come back.

- [Project board](https://github.com/users/dennisklappe/projects/2) for what is
  being worked on now
- [Issues](https://github.com/dennisklappe/mattermore/issues) for individual
  pieces of work, and where to report something
- [Releases](https://github.com/dennisklappe/mattermore/releases) for what has
  actually shipped, with the upstream versions each build was made from
- [Pull requests](https://github.com/dennisklappe/mattermore/pulls) for how each
  change was made and what it was verified against

What Mattermore lifts today is on the [features](/docs) pages, and each one says
whether the restriction was a licence gate or a real technical limit.

## Why group video was not just a licence check

This is the one piece of history worth keeping here, because it explains the
shape of the fork rather than its status.

Every other restriction Mattermore lifts is a check standing in front of working
code. Remove the check and the feature is there, because the implementation was
always in the open source tree.

Group video was not like that. The licence gate was real, but behind it the
sender attribution was broken in a way no configuration could fix. Upstream's
own `rtcd/service/rtc/session.go` sets `sender_id` in the media map to the
*receiving* session, so every client attributed every incoming track to itself
and all remote streams collapsed onto one key. Each participant saw their own
camera and nothing else.

The sender is recoverable from the naming the SFU gives each track, which is
what Mattermore does instead. That took two attempts and a test with three real
browsers in one call, because reading the code was not enough to see the bug.

The lesson generalises: a lifted gate is worth nothing until someone has run the
feature. Anything Mattermore claims has been run, and the pull request that
added it says what was measured.
