#!/usr/bin/env bash
# Usage: ./scripts/check-upstream.sh server|calls|transcriber
# Patch conflicts set verification=conflict and exit 3; operational failures do not.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/lib.sh"
# shellcheck source=../upstream.env
source "$ROOT/upstream.env"
component="${1:?component required}"
case "$component" in
    server) repo=mattermost/mattermost; key=SERVER_TAG; current="$SERVER_TAG" ;;
    calls) repo=mattermost/mattermost-plugin-calls; key=CALLS_TAG; current="$CALLS_TAG" ;;
    transcriber) repo=mattermost/calls-transcriber; key=TRANSCRIBER_TAG; current="$TRANSCRIBER_TAG" ;;
    *) echo "unknown component: $component" >&2; exit 1 ;;
esac

output() {
    echo "$1=$2"
    if [ -n "${GITHUB_OUTPUT:-}" ]; then printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"; fi
}

output repo "$repo"
output key "$key"
output current "$current"
output server_tag "$SERVER_TAG"
url="https://github.com/$repo.git"
if [ "$component" = transcriber ]; then
    refs="$(git ls-remote --heads "$url" "refs/heads/$TRANSCRIBER_BRANCH")"
    latest="${refs%%[[:space:]]*}"
    [[ "$latest" =~ ^[0-9a-f]{40}$ ]] || { echo "no branch head found" >&2; exit 1; }
    output branch "$TRANSCRIBER_BRANCH"
else
    refs="$(git ls-remote --tags --refs "$url")"
    # Numeric tuple ordering is portable to macOS and ignores prerelease tags.
    latest="$(printf '%s\n' "$refs" | python3 -c '
import re, sys
tags = [m.group(1) for line in sys.stdin if (m := re.search(r"refs/tags/(v[0-9]+\.[0-9]+\.[0-9]+)$", line.strip()))]
if not tags:
    sys.exit("no stable tags found")
print(max(tags, key=lambda tag: tuple(map(int, tag[1:].split(".")))))
')"
fi
if [ "$latest" = "$current" ]; then
    output verification unchanged
    exit 0
fi
output new_ref "$latest"

patches=("$ROOT/patches/$component"/*.patch)
[ -f "${patches[0]}" ] || { echo "patch series is missing" >&2; exit 1; }
work="$ROOT/build/upstream-$component"
mkdir -p "$ROOT/build"
rm -rf "$work"
# Do not turn a failed fetch/checkout into a rebase alert.
checkout_source "$url" "$latest" "$work"

if [ "$component" = calls ]; then
    minimum="$(python3 - "$work/plugin.json" <<'PY'
import json, re, sys
with open(sys.argv[1]) as source:
    minimum = json.load(source).get("min_server_version", "")
print(minimum if isinstance(minimum, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", minimum) else "unknown")
PY
)"
    output min_server "$minimum"
fi

if apply_patches "$work" "$ROOT/patches/$component"; then
    output verification clean
else
    output verification conflict
    exit 3
fi
