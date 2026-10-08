# Troubleshooting

## Where to look first

Separate plugin activation/configuration, media connectivity and job-service
failures before changing settings. A missing button alone does not identify
which layer failed.

Two places tell you what is actually happening. The Mattermost server logs
cover the plugin and the RTC service. The browser console covers the webapp, so
use it when the interface misbehaves but the server looks healthy. Remove
credentials, tokens and personal data before sharing logs; do not post full
environment dumps or authentication responses.

## The call button is missing in channels

Confirm **Calls (MaintainedMost)** is installed and active in Plugin Management,
then inspect the plugin's default/channel settings and the user's permissions.
The patched checker does not require `MM_CALLS_GROUP_CALLS_ALLOWED`; setting
that variable cannot fix a failed plugin activation or a disabled channel.

Check startup logs and the selected image/bundle version. When changing Compose
environment values, recreate the server with `docker compose up -d` and reload
the client. See [configuration](configuration.md).

## The video button is missing in a channel

Check `EnableVideo`, the installed plugin version and cached browser assets.
MaintainedMost defaults experimental video on and removes the DM-only UI limit;
an existing saved setting can keep it off. Late metadata and receiver reuse
have client regression tests, but full live-browser media behavior remains
unverified. See [verification limits](README.md#verification-limits). The
official recorder does not capture these group camera streams.

## Calls connect and then drop

Check RTC reachability and browser/server media logs.

The web reverse proxy handles the interface and signaling, not the media
stream. Media needs a reachable RTC endpoint.

- UDP 8443 must be reachable from clients.
- TCP 8443 should be open as a fallback for restrictive networks.
- Open both on the host firewall and on any cloud security group in front of it.

```bash
sudo ufw allow 8443/udp
sudo ufw allow 8443/tcp
```

The repository Compose file publishes both UDP and TCP 8443. Configure the RTC
fallback if you need it; opening a firewall port alone does not publish a
container port in a custom deployment. See [self-hosting](selfhost.md).

## Calls never connect at all

Check that `MM_SERVICESETTINGS_SITEURL` matches the address users and job
containers can reach, including scheme and port. Also check WebSocket upgrades,
RTC address advertisement, firewall/NAT rules and any configured TURN service.
A correct site URL alone does not prove media connectivity.

## Recording or transcription does not start

- Check explicit `MM_CALLS_ENABLE_RECORDINGS` / `MM_CALLS_ENABLE_TRANSCRIPTIONS`
  values and saved plugin settings. Environment overrides and persisted `false`
  values are honored; a job-service URL supplies defaults only.
- Verify offloader readiness and image-pull logs. Automatic recording is off
  by default. If enabled, a failed readiness, call identity, host or permission
  check safely skips the start with a warning; no eventual retry is promised.
- The offloader permits one registry namespace and specific job-image names
  with semantic version tags. `CALLS_IMAGE_REGISTRY`, the plugin's
  `MM_CALLS_JOB_SERVICE_IMAGE_REGISTRY` and the offloader's allowed namespace
  must agree. Both the unchanged official recorder and patched transcriber must
  exist there. See [recording](recording.md); do not enable `DEV_MODE` to bypass it.
- For the OpenAI-compatible backend, use
  `MM_CALLS_TRANSCRIBER_TRANSCRIBE_API=openai/api`, not
  `MM_CALLS_TRANSCRIBE_API=openai/api`, and select the patched transcriber image.
  See [transcription](transcription.md) for all job-only variables. Interrupted
  response bodies get bounded retries; malformed complete JSON does not.

Keep the offloader API private. Publishing port 4545 or weakening its allowlist
is not a readiness fix.

## OIDC login fails after upgrading

Use the [migration checklist](upgrading.md#oidc-migration-checklist) and a tested
local administrator recovery account. `DiscoveryEndpoint` is required and must pin the exact issuer,
including a trailing slash where present; discovery overrides legacy manual
endpoints. Check ID-token issuer/audience/time claims and exact UserInfo subject
matching. UserInfo itself must return boolean `email_verified: true`; missing,
null, false, string or numeric claims fail even if the ID token says verified.

Private IdPs require both a trusted CA and a narrow outbound allowlist entry.
`EnableInsecureOutgoingConnections` cannot disable OIDC TLS checks. Existing
accounts are not automatically linked by email, so do not change subjects or
providers expecting an email match to migrate accounts. See [SSO](sso.md).

## The plugin is rejected, or official Calls comes back

Two different causes, both on the server side.

Read the error first: signature policy, permissions, compatibility and a damaged
archive are different problems. Custom trusted signing keys are supported.
MaintainedMost Server trusts unsigned local prepackaged bundles at startup and
when the marketplace selects the matching local bundle; invalid present
signatures still fail. That exception does not change remote upload,
marketplace, URL or filestore/cluster signature and authorization rules. See
[plugin trust](configuration.md#configjson-plugin-settings).

If official Calls reappears after a server upgrade, the prepackaged copy has
overwritten MaintainedMost. This should not happen, because MaintainedMost versions
its bundles above the lower-versioned official Calls that the pinned server
prepackages. Confirm what is installed in System Console > Plugins > Calls. Our
build shows as "Calls (MaintainedMost)". If it shows plain "Calls", verify the
selected bundle and follow [install](install.md) to restore it.

## No calls plugin at all after an upgrade

The call button is gone, the System Console lists no Calls plugin, and
`mmctl plugin list` shows an empty enabled list.

Images before the fix for this shipped **two** bundles for the same plugin id,
the fork's and upstream's. On start the server installed upstream's first and
then removed it again to put the fork's in place, and that removal can fail:

```
Removing existing installation of plugin before local install (existing_version 1.12.2)
removePlugin: Unable to delete plugin., unlinkat plugins/com.mattermost.calls: directory not empty
```

The install is abandoned and what is left behind is a `com.mattermost.calls`
directory holding an orphan `webapp` folder, with no manifest and no binary.

Current images skip the superseded upstream Calls archive when selecting
prepackaged plugins; the original archive still exists in the inherited image
layer. An orphan left by an older install may need separate recovery.

Back up first, stop the server, and confirm the exact plugin and client-plugin
volume paths before removing only the orphaned `com.mattermost.calls` install
directories. Do not delete whole volumes or assume example volume names match
your deployment. Restart with the corrected image, verify **Calls (MaintainedMost)**
and its `1000.x.y` version in Plugin Management, and check configuration/history
against the backup. See [upgrading](upgrading.md).

## Large calls degrade

Measure CPU, uplink, client bandwidth and the mix of cameras and screen sharing.
No measured capacity guarantee is made for this build. Set a participant limit
your deployment can sustain rather than treating `0` as unlimited resources.
