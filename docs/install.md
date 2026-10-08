# Install the Calls plugin

This installs the Calls plugin only. You need system administrator access and
a maintenance window because replacing the plugin interrupts active calls.

If you do not have Mattermost running yet, start with
[self-host from scratch](selfhost.md) instead.

## Before you start

MaintainedMost replaces the calls plugin on a self-hosted Mattermost server. It does
not work on Mattermost Cloud, which allows neither plugin uploads nor custom
environment variables.

- A compatible self-hosted server. The plugin manifest requires v11.0 or later;
  the current integration target is the pinned server v11.11.1, not every later
  release.
- System administrator access to the System Console
- Shell or orchestration access for configuration and recovery

Back up your database, files, plugin state and configuration, and review
[rollback](upgrading.md#rolling-back-to-official-calls) before replacing the
plugin. The patched checker enables group calls without
`MM_CALLS_GROUP_CALLS_ALLOWED`; channel settings and permissions still apply.
Server-side SSO and user-limit changes require
MaintainedMost Server, not this plugin.

## 1. Install the bundle

Choose a tested release from this repository's Releases page, or build the
current pinned source using the [repository instructions](../README.md).
Download its `maintainedmost-calls-*.tar.gz` and matching
checksum. For bundle version 1000.12.4, the check is:

```bash
sha256sum -c maintainedmost-calls-1000.12.4.tar.gz.sha256
```

Then in Mattermost:

1. Go to **System Console › Plugins › Plugin Management**
2. Choose **Upload Plugin** and select the file
3. Enable the plugin if it does not enable itself

MaintainedMost uses the same plugin id as official Calls, `com.mattermost.calls`, so
it replaces it in place and keeps your existing configuration.

If an unsigned upload is rejected by signature policy, see
[configuration](configuration.md#configjson-plugin-settings) for trusted-key
signing options and the risks of relaxing that policy. A checksum is an
integrity check, not publisher authentication. MaintainedMost Server's exception
for its own local prepackaged bundle does not apply to an uploaded bundle.

## 2. Check it works

1. Open a channel that is not a direct message
2. Confirm the call button appears in the channel header, where stock free
   Mattermost hides it
3. Start a call and have two colleagues join

Three participants is the test that matters. Two would work on stock Mattermost
as well.

In **System Console › Plugins › Calls** the plugin reports itself as
**Calls (MaintainedMost)**, which is how you tell at a glance which build is
installed.

## If something is wrong

Check plugin activation, channel settings and media reachability; see
[troubleshooting](troubleshooting.md). Recording and transcription additionally
need a ready offloader and correctly named job images. Follow
[recording](recording.md), rather than assuming the plugin upload installs those
services too.
