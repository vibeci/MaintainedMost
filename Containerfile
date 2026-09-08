# Mattermore: Mattermost with the paywalled features turned back on.
#
# Built on the official Team Edition image. Three things change: the server
# binary, the web app, and the calls plugin.

FROM docker.io/mattermost/mattermost-team-edition:latest AS upstream

# The guest administration screens and the licence badges live in the web app,
# so upstream's prebuilt one has to be replaced or those two patches do nothing
# at all.
#
# COPY merges rather than replaces, and the base image has no shell to delete
# with, so upstream's bundles would otherwise linger: unreachable, because
# every filename is content hashed and our root.html references only ours, but
# still around 87 MB. This stage empties them, so nothing stale is reachable.
# It does not shrink the image: layers are additive, so the base layer keeps
# the originals either way.
FROM docker.io/library/alpine:3.20 AS webapp
COPY --from=upstream /mattermost/client /old
COPY client /new
RUN set -eu; \
    mkdir -p /out; \
    cp -a /new/. /out/; \
    cd /old; \
    find . -type f | while read -r f; do \
        if [ ! -e "/out/$f" ]; then \
            mkdir -p "/out/$(dirname "$f")"; \
            : > "/out/$f"; \
        fi; \
    done

# The image ships a fully written config.json, so every default our patches set
# in Go is already spelled out in that file and never reached. Patching the Go
# constant stays right for anyone building from source; this makes it true for
# the image too. Only the keys in config/overrides.json change.
FROM docker.io/library/alpine:3.20 AS config
RUN apk add --no-cache python3
COPY --from=upstream /mattermost/config/config.json /in.json
COPY config/overrides.json /overrides.json
COPY scripts/merge-config.py /merge.py
RUN python3 /merge.py /in.json /overrides.json /out.json

# The prepackaged plugins, with exactly one bundle per plugin id.
#
# Upstream's calls bundle is removed rather than left to lose on version.
# Winning on version describes where a start ends up, not how it gets there:
# with both present the server installs upstream's first and then has to remove
# it again to put ours in its place, and that removal can fail. When it does,
# the install is abandoned and the plugin directory is left holding an orphan
# webapp folder with no manifest and no binary, so the server comes up with no
# calls plugin at all:
#
#   Removing existing installation of plugin before local install (1.12.2)
#   removePlugin: unlinkat plugins/com.mattermost.calls: directory not empty
#
# The base image has no shell, so this is done in a stage and copied in, the
# same way the web app is.
FROM docker.io/library/alpine:3.20 AS plugins
COPY --from=upstream /mattermost/prepackaged_plugins /out
COPY mattermore-calls.tar.gz /out/mattermore-calls-linux-amd64.tar.gz
RUN rm -f /out/mattermost-plugin-calls-v*.tar.gz /out/mattermost-plugin-calls-v*.tar.gz.sig

FROM docker.io/mattermost/mattermost-team-edition:latest

COPY --chown=2000:2000 mattermore-server /mattermost/bin/mattermost
COPY --from=webapp --chown=2000:2000 /out /mattermost/client
COPY --from=config --chown=2000:2000 --chmod=600 /out.json /mattermost/config/config.json
COPY --from=plugins --chown=2000:2000 /out /mattermost/prepackaged_plugins


# Group calls are switched on by upstream's own environment variable.
ENV MM_CALLS_GROUP_CALLS_ALLOWED=true

LABEL org.opencontainers.image.title="Mattermore" \
      org.opencontainers.image.description="Mattermost with the paywalled features turned back on" \
      org.opencontainers.image.url="https://mattermore.dev" \
      org.opencontainers.image.source="https://github.com/dennisklappe/mattermore" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
