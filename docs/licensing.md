# Licensing

MaintainedMost's project licence remains GNU AGPLv3. Upstream components keep
their own grants, exceptions and notices. The source licence is not a claim
that every file in a container image has identical terms.

## Why AGPLv3

Mattermost's licensing FAQ contains the sentence that its server source "is
and always has been made available under the AGPLv2 license". This wording is
present in the [pinned FAQ](https://github.com/mattermost/mattermost/blob/3acb3a7f684d11ccfcec4e5bd11c79f64e3eabf9/docs/main/product-overview/faq-license.mdx#L159-L167).

The actual source licence files say otherwise:

- [Mattermost server 11.11.1](https://github.com/mattermost/mattermost/blob/3acb3a7f684d11ccfcec4e5bd11c79f64e3eabf9/LICENSE.txt#L9-L15)
  grants use under the Free Software Foundation's **GNU AGPL v3.0**, subject to
  its listed exceptions, or under a commercial licence.
- [Calls 1.12.3](https://github.com/mattermost/mattermost-plugin-calls/blob/eb708b4c2ea2ccbd7d99ef6b1b067a4a0bdfdeb5/LICENSE.txt#L9-L14)
  likewise explicitly names **GNU AGPL v.3.0** or a commercial licence.
- Mattermost's [first published LICENSE.txt in June 2015](https://github.com/mattermost/mattermost/blob/3c5760f5a298d701035d4d3ee3abbb626c614970/LICENSE.txt)
  already named GNU AGPL v3.0. The server 11.11.0 licence used by the original
  Mattermore build has the same relevant terms as 11.11.1.
- [Mattermore's first-added LICENSE](https://github.com/dennisklappe/mattermore/blob/28c23bd42fba7c5a7a96594642e1a49b2d8c5320/LICENSE#L1-L2)
  was GNU AGPLv3, not a later replacement of an AGPLv2 file.

The evidence supports a version-number error in the FAQ, not the claim that
Mattermore improperly converted AGPLv2 source to AGPLv3. MaintainedMost follows
the explicit source grants and does not change to v2. Written clarification
from Mattermost would resolve the conflicting documentation more definitively.

The older **Affero GPL version 2** should not be confused with GNU GPLv2 or
GNU AGPLv3. Its [archived publisher text](https://web.archive.org/web/20111006061250/http://www.affero.org/agpl2.html)
was transitional and expressly permitted distribution under GNU AGPL version 3
or later. The [GNU AGPLv3 preamble](https://www.gnu.org/licenses/agpl-3.0.html)
also distinguishes the older Affero licence. That historical detail does not
replace the grants in these particular upstream repositories.

## Component Terms

- Server and Calls source use the AGPLv3/commercial grants above, with stated
  exceptions. Their MIT grant for compiled versions is specifically for
  versions **produced by Mattermost**, not a blanket MIT grant for independently
  compiled forks. See the [compiled-license notice](https://github.com/mattermost/mattermost/blob/3acb3a7f684d11ccfcec4e5bd11c79f64e3eabf9/server/build/MIT-COMPILED-LICENSE.md#L1-L9).
- Server webapp, public interfaces, translations and other designated portions
  have their own exceptions, including Apache-2.0. Preserve their notices.
- The [transcriber](https://github.com/mattermost/calls-transcriber/blob/c09a9a3c88ea29b2560002999f9b48e7d92f515f/LICENSE)
  and recorder have Apache-2.0 root licences. Their linked dependencies have
  additional terms; these root files do not settle the complete binary licence.
- Source Available enterprise directories have separate restricted grants.
  An AGPL licence at the repository root does not override those grants.

Original patches and modifications can carry their own permitted licence
terms, but must preserve applicable upstream copyrights, notices and
obligations. MaintainedMost retains Mattermost and original Mattermore
attribution, including in the rebranded UI components.

## Patch Boundary

The patch policy avoids Source Available directories. Calls patch `0001`
removes the enterprise checker import and provides a replacement checker; that
package is excluded from the patched plugin's production dependency graph.
This is a technical dependency check, not proof of clean-room authorship or a
legal determination about every historical release.

The server image still inherits the official Team Edition base. Original
binaries, assets and plugin archives remain in inherited layers even when
overlaid, disabled or skipped at runtime. Skipping a plugin is not the same as
removing its files from the distributed image.

## Distribution Checks

Before redistributing an image or operating a modified network service:

- Retain applicable copyright, licence and third-party notice files.
- Make the corresponding source for the actual build available as required,
  including the pinned upstream sources, patches and necessary build material.
- Provide remote users the source-access opportunity required by AGPL section
  13 where applicable. Repository metadata alone is not a prominent in-app offer.
- Review all inherited image layers and native dependencies, not just the
  packages directly changed by these patches.
- Review Microsoft Speech SDK **1.38.0**, which the transcriber links and
  bundles under separate proprietary terms. Its redistribution restrictions
  and compatibility with AGPL-covered linked components have not been legally
  established here. Changing this repository's licence would not relicense
  that SDK or resolve the question.

These are remaining artifact-compliance checks, not evidence of the alleged
AGPL version change. This document records inspected source grants and known
uncertainties; it is not a legal opinion certifying a release for redistribution.

## Names And Credit

"Mattermost" is a trademark of Mattermost, Inc. MaintainedMost is independent
and uses upstream names and identifiers to describe compatibility. The Calls
plugin ID stays `com.mattermost.calls` to replace the existing plugin and retain
its settings, not to claim Mattermost authorship or endorsement.

Mattermost and its contributors wrote the upstream software. Dennis Klappe and
the Mattermore contributors provided the original fork's work. See
[NOTICE.md](../NOTICE.md) and [LICENSE](../LICENSE).
