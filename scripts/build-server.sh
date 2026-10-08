#!/usr/bin/env bash
#
# Builds the MaintainedMost server binary.
#
# Usage: ./scripts/build-server.sh [output-path]
# Requires: git, go, Node 24 and upstream's npm (package-lock.json is authoritative).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/lib.sh"
OUT="${1:-$ROOT/dist/maintainedmost-server}"
OUT="$(absolute_dir "$(dirname "$OUT")")/$(basename "$OUT")"
WORK="$ROOT/build/server"

# shellcheck source=../upstream.env
source "$ROOT/upstream.env"
export GOTOOLCHAIN="go$GO_VERSION"

prepare_source https://github.com/mattermost/mattermost.git \
    "$SERVER_TAG" server "$SERVER_COMMIT"

# The marks are images, not code, so they are copied rather than patched. They
# are committed already rendered, so this needs no image tooling.
"$ROOT/scripts/brand-assets.sh" apply "$WORK"

# Upstream's own build does this and gitignores the result: the server module
# depends on server/public, and without a workspace Go resolves it to the
# published version, which lags the tree and fails to compile.
( cd "$WORK/server" && go work init . ./public )

if [ "${RUN_TESTS:-0}" = "1" ]; then
    "$ROOT/scripts/test-component.sh" server "$WORK"
fi

# Native by default; build-image.sh supplies Linux/amd64 with CGO disabled.
( cd "$WORK/server" && go build -trimpath -tags production -o "$OUT" ./cmd/mattermost )

# Two patches change only the web app, and the official image ships a prebuilt
# one. Without this step those patches are inert: the guest administration
# screens stay hidden and the licence badges stay put. Roughly four minutes.
if [ "${SKIP_WEBAPP:-}" != "1" ]; then
    echo "==> building the web app"
    ( cd "$WORK/webapp" && npm ci --no-audit --no-fund )
    ( cd "$WORK/webapp" && NODE_OPTIONS=--max-old-space-size=6144 npm run build )
    client="$(dirname "$OUT")/client"
    if [ -e "$client" ] || [ -L "$client" ]; then
        if [ -L "$client" ] || [ ! -f "$client/.maintainedmost-generated" ]; then
            echo "refusing to replace non-generated webapp directory: $client" >&2
            exit 1
        fi
        rm -rf "$client"
    fi
    cp -R "$WORK/webapp/channels/dist" "$client"
    touch "$client/.maintainedmost-generated"
    echo "==> $client"
fi

echo "==> $OUT"
