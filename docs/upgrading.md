# Upgrading

## Before upgrading

Back up the database, uploaded files, configuration, plugin state and secrets
together. Test a restore and rollback in isolation, keeping the previous image
references and configuration. Do not assume an older server can safely use a
database already migrated by a newer one.

Choose a release whose tests and image publication completed. The release gate
runs race-enabled regressions, the complete OIDC provider tests, Calls client
Jest tests, full frontend builds and the native transcriber build, then reuses
those artifacts for publication. It does not certify every live-browser media
path or external identity/transcription provider.

## OIDC migration checklist

These checks are stricter than earlier builds of this fork. Complete them before
replacing a server used for SSO:

1. Verify a local administrator recovery account works without the IdP. Keep
   recovery access available until new and existing SSO logins have been tested.
2. Set `DiscoveryEndpoint` to the exact issuer URL, including any trailing slash,
   or a matching standard discovery URL. Manual endpoint-only configurations
   are rejected. Discovery is authoritative even if Google/Microsoft legacy
   authorization, token or UserInfo endpoints are populated.
3. Confirm the token endpoint returns an ID token with a string `sub`, exact
   `iss`, client `aud`, valid `exp`/`iat`, and a matching `azp` if present or if
   there are multiple audiences. UserInfo must return exactly the same `sub`.
4. Confirm **UserInfo** returns a valid email and JSON boolean
   `email_verified: true`. Missing, null, false, string and numeric values fail.
   The ID token's verified-email claim is not a fallback, and matching emails
   do not automatically link existing accounts.
5. Trust the IdP's CA in the server runtime and narrowly allow any private
   endpoint hosts through `AllowedUntrustedInternalConnections`. The global
   insecure-outgoing setting cannot bypass OIDC TLS verification.
6. Test issuer, claims and recovery on a restored staging deployment before
   production. If login fails, use local recovery to fix the IdP/configuration
   or follow the tested rollback; do not weaken TLS or claim checks.

The flow authenticates tokens through a strict server-side HTTPS code exchange;
it does not perform cryptographic JWS/JWKS signature verification. See
[configuration](configuration.md) for the full contract.

## Upgrading the Calls bundle

For plugin-only installs, select the `maintainedmost-calls-*.tar.gz` and matching
checksum from the chosen release in this repository.
Follow [install](install.md) to verify and upload it. Replacing the plugin restarts
it and interrupts calls. Its id remains `com.mattermost.calls`, so existing
configuration is reused; that includes saved recording/transcription opt-outs.

For MaintainedMost Server, the image already carries the bundle. An unsigned local
prepackaged bundle is trusted with the image, but that does not waive signature
or authorization rules for an uploaded or downloaded replacement. See
[plugin trust](configuration.md#configjson-plugin-settings).

## Version numbers

The current bundle version is **1000.12.4**, built from Calls source **1.12.3**.
Fork-only fixes also advance the bundle version, so its suffix is not an exact
upstream source version. Consult `CALLS_TAG`, `CALLS_COMMIT` and `CALLS_VERSION`
in [`upstream.env`](../upstream.env) at the selected release.

The `1000.` prefix is deliberate. Mattermost ships Calls as a prepackaged
plugin, and on startup the server reinstalls its prepackaged copy over an
installed plugin only when the prepackaged version is strictly higher. That
check is `shouldPersistTransitionallyPrepackagedPlugin` in
`server/channels/app/plugin.go`.

The high version prevents replacement by lower-versioned official bundles under
this comparison. It does not guarantee compatibility with future server APIs or
protect against an explicit administrator replacement.

## Upgrading the server image

Current source pins are server 11.11.1 with a matching commit/image digest,
Calls 1.12.3 and transcriber c09a9a3. Calls 1.12.5 requires server 12 and is not
a compatible independent upgrade. Maintainers must review tags, commits and
the runtime image together; a patch conflict fails the build, never silently
drops a patch.

For repository Compose deployments, update `MAINTAINEDMOST_IMAGE` in `.env` to the
tested release and set `CALLS_IMAGE_REGISTRY` to the shared job-image namespace.
That namespace must contain the unchanged official recorder at the version
specified by the pinned Calls manifest and the patched transcriber at the
MaintainedMost release tag. See [self-hosting](selfhost.md) for the naming contract.

### Preserve an existing Compose deployment

Before replacing an existing Compose file or `.env`, record the current project
name, volume names and image references. Set `COMPOSE_PROJECT_NAME` in the new
`.env` to that exact project name. Use this variable rather than only
`docker compose -p`: the offloader's `DOCKER_NETWORK` is derived from it too.
Keeping the project name and named-volume keys avoids starting with a new,
empty database and uploads volume.

The server service is now `maintainedmost`, and its image variable is
`MAINTAINEDMOST_IMAGE`. During the maintenance window, stop the old stack using
its previous Compose configuration, without `--volumes`, before replacing the
configuration and starting the renamed service. Do not run the old and new
server containers against the same data during an upgrade, or delete named
volumes as part of the rebrand.

With backups checked, the previous server stopped and the updated configuration
pointing at the intended existing volumes:

```bash
docker compose pull
docker compose up -d
```

Check these things afterwards:

1. **The plugin is still enabled.** System Console › Plugins › Plugin
   Management. A plugin can be disabled by a failed startup.
2. **It still reports as MaintainedMost**, not official Calls. See the next
   section.
3. **Configuration survived.** Confirm the site URL, SSO, registry namespace
   and explicit opt-outs. Avoid posting environment dumps, which may contain
   API keys and other secrets.
4. **A three-person call still works.** Two participants work on stock
   Mattermost, so a two-person test proves nothing.
5. **Recording/transcription work when requested.** Test readiness and images,
   not just button visibility. Automatic recording is off by default and may
   safely skip a start with a warning if prerequisites are not met; there is
   no promised eventual retry.

If a major Mattermost release changes the plugin API, an older MaintainedMost build
can fail to start against a much newer server. Check this repository's Releases
page for a build tracking a newer upstream version before you upgrade a
long-untouched server.

## Checking which build is installed

Go to **System Console › Plugins › Calls**. The plugin reports itself as
**Calls (MaintainedMost)**. Official Calls reports as **Calls**.

The version shown next to it uses the fork's `1000.` bundle version convention.
That prefix is not proof of publisher identity.

If it says **Calls** without the suffix, inspect the installed version, release
and startup logs before assuming which features are enabled. Confirm the image
and bundle you selected rather than disabling signature checks to fix it.

## Rolling back to official Calls

Schedule a maintenance window and back up first. Install the selected official
bundle using upstream's procedure; a downgrade may require removing the patched
plugin first. Check which bundle your server prepackages: restarting a
MaintainedMost image can reinstall MaintainedMost, not official Calls. Plan the
prepackaged-plugin policy or underlying image change as part of the rollback.

Restore any signature policy you relaxed for uploads, remove job-only overrides
that no longer apply, and retest settings and call history. Shared plugin ids
are not a guarantee that every future storage or server migration is reversible.
For a server downgrade, use the matching tested backup/configuration if required.

## Related pages

- [Configuration](configuration.md) for the settings referenced here.
- [Install guide](install.md) for a first-time installation.
- [Troubleshooting](troubleshooting.md) if calls break after an upgrade.
- [Licensing and legality](licensing.md) for the licence position.
