FROM docker.io/library/alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk add --no-cache bash ca-certificates git patch python3 ripgrep \
    && addgroup -g 10001 sandbox \
    && adduser -D -u 10001 -G sandbox sandbox
COPY verify.py /opt/maintainedmost/verify.py
USER 10001:10001
WORKDIR /workspace
