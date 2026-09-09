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


FROM docker.io/mattermost/mattermost-team-edition:latest

COPY --chown=2000:2000 mattermore-server /mattermost/bin/mattermost
COPY --from=webapp --chown=2000:2000 /out /mattermost/client
COPY --from=config --chown=2000:2000 --chmod=600 /out.json /mattermost/config/config.json


# Mattermore ships its own calls bundle. Upstream's cannot be deleted from
# here: the base image has no shell to run rm with, and COPY merges into the
# destination rather than replacing it, so a file in the base layer survives.
# Two bundles for one plugin id is not harmless, because the server installs
# one and then has to remove it again to install the other, and that removal
# can fail and leave the plugin uninstalled entirely:
#
#   Removing existing installation of plugin before local install (1.12.2)
#   removePlugin: unlinkat plugins/com.mattermost.calls: directory not empty
#
# patches/server/0013 teaches the server to ignore it instead.
COPY --chown=2000:2000 mattermore-calls.tar.gz /mattermost/prepackaged_plugins/mattermore-calls-linux-amd64.tar.gz
ENV MM_PREPACKAGED_PLUGINS_SKIP=mattermost-plugin-calls-

# Group calls are switched on by upstream's own environment variable.
ENV MM_CALLS_GROUP_CALLS_ALLOWED=true

LABEL org.opencontainers.image.title="Mattermore" \
      org.opencontainers.image.description="Mattermost with the paywalled features turned back on" \
      org.opencontainers.image.url="https://mattermore.dev" \
      org.opencontainers.image.source="https://github.com/dennisklappe/mattermore" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
