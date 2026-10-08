#!/usr/bin/env bash
# Check image mapping against the actual, unmodified pinned offloader.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/lib.sh"
# shellcheck source=../upstream.env
source "$ROOT/upstream.env"
export GOTOOLCHAIN="go$GO_VERSION"
WORK="$ROOT/build/offloader"
mkdir -p "$ROOT/build"
rm -rf "$WORK"
checkout_source https://github.com/mattermost/calls-offloader.git "$OFFLOADER_TAG" "$WORK"
[ "$(git -C "$WORK" rev-parse HEAD)" = "$OFFLOADER_COMMIT" ] || {
    echo "offloader tag does not match pinned commit" >&2; exit 1
}
(cd "$WORK" && GOWORK=off go test -race -count=1 "$ROOT/scripts/tests/offloader_contract_test.go")
