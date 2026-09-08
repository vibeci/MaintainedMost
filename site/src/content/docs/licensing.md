---
title: "Licensing and legality"
description: "Why Mattermore is legal: it modifies AGPL code only, never the Source Available Licence, and documents a switch Mattermost ships itself."
order: 8
---

## The short version

Mattermore modifies AGPL-3.0 code only. It does not modify or redistribute
anything covered by the Mattermost Source Available Licence. That is the
central legal fact of this project, and everything below is detail supporting
it.

Mattermore itself is released under AGPL-3.0, the same licence as the upstream
code it builds on.

## How upstream Calls is licensed

`mattermost-plugin-calls` is dual licensed. `LICENSE.txt` in the upstream
repository grants:

- AGPL-3.0 for the source code.
- MIT for compiled versions produced by Mattermost, Inc.
- Apache-2.0 for the contents of `webapp/i18n/`.

The plugin also carries a second file, `LICENSE.enterprise`, which is the
Mattermost Source Available Licence. It covers `server/enterprise/`. That
licence permits modification for development and testing only, and requires an
E20 subscription for production use.

Those two licences cover different directories. Keeping the line between them
clean is what makes a fork like this defensible.

## What Mattermore actually changes

Group calls need no patch at all.

The licence check for group calls lives in `GroupCallsAllowed()` in
`server/enterprise/license.go`, and it already reads an environment variable:

```go
os.Getenv("MM_CALLS_GROUP_CALLS_ALLOWED") == "true"
```

Upstream ships that switch themselves, in every released version. Mattermore
documents it rather than patching around it, which is why nothing under the
Source Available Licence is touched. See the
[configuration guide](/configuration) for how to set it.

Everything Mattermore does change sits in AGPL-3.0 code, and those changes are
published under AGPL-3.0 in turn.

## Precedent

Framasoft's Mostlymatter has forked the Mattermost server the same way since
May 2024, patching only AGPL code in `server/channels/app/limits.go`.

- [framagit.org/framasoft/framateam/mostlymatter](https://framagit.org/framasoft/framateam/mostlymatter)

It is a long running, publicly maintained example of the same reading of the
same licences.

## Trademark

"Mattermost" is a trademark of Mattermost, Inc.

- Mattermore uses the name only to describe what it is compatible with, which
  is nominative fair use.
- Mattermore does not use Mattermost's logo, wordmark or brand colours.
- Mattermore is not affiliated with, endorsed by, or supported by
  Mattermost, Inc.

Note that the plugin id stays `com.mattermost.calls`, because the server
expects it. That is a technical requirement for the plugin to load, not a claim
of origin.

## Be fair to Mattermost

Mattermost open-sourced this code, and they added the environment variable
themselves. Group calls on a self-hosted server are possible because they made
that choice.

If your organisation can afford Professional, buy it. That revenue funds the
upstream work this fork depends on. Mattermore exists for the people who cannot
justify a per-user subscription for a handful of audio calls, not as an
argument against paying.

## Not legal advice

This page explains the reasoning behind the project. It is not legal advice. If
the licensing of your deployment matters to your organisation, have a lawyer
read the licences and decide for you.

Once you are satisfied, the [install guide](/install) is the next step, and
[troubleshooting](/troubleshooting) covers what to do when something does
not work.
