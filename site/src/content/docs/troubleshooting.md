---
title: "Troubleshooting"
description: "Fixes for the common Mattermore problems: a missing call button, calls that drop, rejected plugin uploads and lost upgrades."
order: 7
---

## Where to look first

Almost every report falls into one of three groups:

- The call button is missing, which is an environment variable problem.
- Calls start and then drop, which is a network problem.
- The plugin will not install or keeps reverting, which is a server setting problem.

Two places tell you what is actually happening. The Mattermost server logs
cover the plugin and the RTC service. The browser console covers the webapp, so
use it when the interface misbehaves but the server looks healthy.

## The call button is missing in channels

`MM_CALLS_GROUP_CALLS_ALLOWED=true` is not reaching the server process.

Setting it in your own shell, in a `.env` file the container never reads, or in
a unit file you did not reload is the usual cause. Check the variable from
inside the container, or as the service user, not from the shell you happen to
be sitting in.

```bash
docker compose exec mattermost env | grep MM_CALLS
```

If nothing comes back, the variable is not set where it matters. Fix the
environment, then restart Mattermost. See the
[configuration guide](/configuration) for where to put it on each kind of
install.

If calls work in direct messages but not in channels, the variable is set but
the plugin started before it was. DM calls need no licence check, so they keep
working. Restart Mattermost so the plugin reads the environment again, then
reload the browser tab.

## The video button is missing in a channel

This is expected, not a fault.

- Video is limited to direct messages.
- That is a limit in the upstream interface, not a licence check.
- Mattermore's group video change is written but not yet browser-tested, so on
  a stock build the button stays DM-only. Status is on the [roadmap](/roadmap).

Audio in channels is unaffected, so a group call still works, just without
camera.

## Calls connect and then drop

Almost always the RTC port.

Media does not traverse a reverse proxy. Nginx, Traefik, Caddy and Cloudflare
handle the web interface, but the audio stream needs a direct path to the
server.

- UDP 8443 must be reachable from clients.
- TCP 8443 should be open as a fallback for restrictive networks.
- Open both on the host firewall and on any cloud security group in front of it.

```bash
sudo ufw allow 8443/udp
sudo ufw allow 8443/tcp
```

If you run the Docker stack, publish both, as shown in the
[self-host guide](/selfhost).

## Calls never connect at all

`MM_SERVICESETTINGS_SITEURL` does not match the address users actually type.

The client is told where to reach the call service based on the site URL, so a
mismatch means it dials an address that is not there. Check the scheme, the
hostname and the port, and make sure it is the public address, not `localhost`
or an internal container name.

## The plugin is rejected, or official Calls comes back

Two different causes, both on the server side.

If the upload is rejected, `RequirePluginSignature` is enabled. Mattermore
bundles are not signed with Mattermost's key, so a server that demands signed
plugins refuses them. Either turn that setting off, or sign the bundle with
your own key and add it to the server's trusted keys. The
[install guide](/install) has the details.

If official Calls reappears after a server upgrade, the prepackaged copy has
overwritten Mattermore. This should not happen, because Mattermore versions
itself as `1000.<upstream>`, which is higher than anything Mattermost
prepackages. Confirm what is installed in System Console > Plugins > Calls. Our
build shows as "Calls (Mattermore)". If it shows plain "Calls", reinstall
Mattermore.

## No calls plugin at all after an upgrade

The call button is gone, the System Console lists no Calls plugin, and
`mmctl plugin list` shows an empty enabled list.

Images before the fix for this shipped **two** bundles for the same plugin id,
Mattermore's and upstream's. On start the server installed upstream's first and
then removed it again to put Mattermore's in place, and that removal can fail:

```
Removing existing installation of plugin before local install (existing_version 1.12.2)
removePlugin: Unable to delete plugin., unlinkat plugins/com.mattermost.calls: directory not empty
```

The install is abandoned and what is left behind is a `com.mattermost.calls`
directory holding an orphan `webapp` folder, with no manifest and no binary.

Upgrading to an image that carries only one calls bundle stops it happening
again, but the leftover directory has to be cleared by hand, because that is
the thing the server cannot delete:

```bash
docker compose stop mattermost
docker run --rm -v <project>_plugins:/p -v <project>_client-plugins:/c \
  alpine sh -c 'rm -rf /p/com.mattermost.calls /c/com.mattermost.calls'
docker compose start mattermost
sleep 60
docker compose exec -T mattermost /mattermost/bin/mmctl --local plugin list
```

The last command must report `Calls (Mattermore), Version: 1000.x.y`. Nothing
else is lost: call settings live in the server config, not in the plugin
directory.

## Large calls degrade

Above roughly 50 participants, quality falls off.

That is a single server ceiling, not a Mattermore limit. The same figure
applies to upstream Calls on one node. Scaling past it means more capacity, not
a different plugin.
