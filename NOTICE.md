# Notice

MaintainedMost builds on the Mattermore patch-based fork of
[Mattermost](https://github.com/mattermost/mattermost),
[mattermost-plugin-calls](https://github.com/mattermost/mattermost-plugin-calls)
and [calls-transcriber](https://github.com/mattermost/calls-transcriber).
It is not affiliated with, endorsed by, or supported by Mattermost, Inc.

The original Mattermore work is by Dennis Klappe and its other contributors.
Historical author and copyright notices are retained; rebranding does not
transfer ownership of their work.

"Mattermost" is a trademark of Mattermost, Inc. The name and upstream plugin
identifiers are used to describe compatibility, not affiliation.

Upstream code retains the copyrights and licence notices of Mattermost, Inc.
and its other contributors. This repository's licence is GNU Affero General
Public License v3.0; upstream components and dependencies retain their own
licences, including the recorder and transcriber's Apache-2.0 notices.
The actual pinned server and Calls source grants name GNU AGPLv3, despite
conflicting AGPLv2 wording in an upstream FAQ. See the cited evidence in
[docs/licensing.md](docs/licensing.md#why-agplv3).

The patch policy does not modify files governed by `LICENSE.enterprise`.
Calls' Source Available `server/enterprise` dependency is replaced with an
independent checker and excluded from the patched Calls build's dependency
graph. See [patches/README.md](patches/README.md).

This source boundary is not a statement about every file in a distributed
image. The server image derives from the official Team Edition image. Original
upstream binaries, assets and prepackaged plugins remain in inherited layers,
even when overlaid, disabled or skipped at runtime. Skipping a plugin does not
remove its archive from those layers.

Review the actual artifacts and their licence notices before redistribution.
This notice does not determine that all artifacts are AGPL-only or give a legal
opinion on their redistribution.

The native transcriber also bundles Microsoft Speech SDK 1.38.0 under separate
proprietary terms. Redistribution and compatibility with its other linked
components need review; the SDK is not relicensed by this repository's LICENSE.
