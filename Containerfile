# MaintainedMost: Mattermost with the paywalled features turned back on.
#
# Built on the official Team Edition image. Three things change: the server
# binary, the web app, and the calls plugin.

# Kept in sync with upstream.env; build-image.sh passes this explicitly too.
ARG SERVER_IMAGE=docker.io/mattermost/mattermost-team-edition:11.11.1@sha256:6ad5912b45875ef2fafc41886b5a70a3ec1b6c17ec706e0492566e8a2fd8aa85
FROM ${SERVER_IMAGE} AS upstream

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
FROM docker.io/library/alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 AS webapp
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
FROM docker.io/library/alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 AS config
RUN apk add --no-cache python3
COPY --from=upstream /mattermost/config/config.json /in.json
COPY config/overrides.json /overrides.json
COPY scripts/merge-config.py /merge.py
RUN python3 /merge.py /in.json /overrides.json /out.json


FROM upstream

ARG CALLS_IMAGE_REGISTRY=maintainedmost
ARG TRANSCRIBER_IMAGE=${CALLS_IMAGE_REGISTRY}/calls-transcriber:v1.0.0-dev0
# Set explicitly by CI or the builder; a local build assumes no repository owner.
ARG SOURCE_URL=
ENV MM_CALLS_JOB_SERVICE_IMAGE_REGISTRY=${CALLS_IMAGE_REGISTRY}
ENV MM_CALLS_TRANSCRIBER_IMAGE=${TRANSCRIBER_IMAGE}

COPY --chown=2000:2000 maintainedmost-server /mattermost/bin/mattermost
COPY --from=webapp --chown=2000:2000 /out /mattermost/client
COPY --from=config --chown=2000:2000 --chmod=600 /out.json /mattermost/config/config.json


# MaintainedMost ships its own calls bundle. Upstream's cannot be deleted from
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
COPY --chown=2000:2000 maintainedmost-calls.tar.gz /mattermost/prepackaged_plugins/maintainedmost-calls-linux-amd64.tar.gz
# Playbooks is skipped for a different reason: it ships prepackaged and then
# refuses to activate, because it wants a Professional licence. It has never
# worked here, so all it contributes is an error on every start. Lifting it is
# tracked separately: its own licence checker is Source Available, so it needs
# the same treatment the calls one got, and a fourth upstream to track.
ENV MM_PREPACKAGED_PLUGINS_SKIP=mattermost-plugin-calls-,mattermost-plugin-playbooks-

# Retained for official Calls compatibility; MaintainedMost's checker does not need it.
ENV MM_CALLS_GROUP_CALLS_ALLOWED=true

LABEL org.opencontainers.image.title="MaintainedMost" \
      org.opencontainers.image.description="Mattermost with the paywalled features turned back on" \
      org.opencontainers.image.url="${SOURCE_URL}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
