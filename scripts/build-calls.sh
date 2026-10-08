#!/usr/bin/env bash
#
# Builds the MaintainedMost calls plugin bundle.
#
# Usage: ./scripts/build-calls.sh [output-dir]
# Requires: git, go, Node 24, upstream's npm, make, python3.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/lib.sh"
OUT="$(absolute_dir "${1:-$ROOT/dist}")"
WORK="$ROOT/build/calls"

# shellcheck source=../upstream.env
source "$ROOT/upstream.env"
export GOTOOLCHAIN="go$GO_VERSION"

prepare_source https://github.com/mattermost/mattermost-plugin-calls.git \
    "$CALLS_TAG" calls "$CALLS_COMMIT"

recorder_version="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["props"]["calls_recorder_version"])' "$WORK/plugin.json")"
if [ "$recorder_version" != "$RECORDER_TAG" ]; then
    echo "Calls selects recorder $recorder_version; update RECORDER_TAG and its verified image digest together" >&2
    exit 1
fi

# Upstream's preinstall fetches this source, then runs npm install and rewrites
# both lockfiles. Fetch the same immutable ref without the lockfile mutations.
read -r declaration assignment < "$WORK/webapp/install_mattermost_webapp.sh"
if [[ "$declaration" != readonly || ! "$assignment" =~ ^COMMITHASH=[0-9a-f]{40}$ ]]; then
    echo "could not read upstream's pinned webapp bootstrap ref" >&2
    exit 1
fi
checkout_source https://github.com/mattermost/mattermost.git \
    "${assignment#COMMITHASH=}" "$WORK/webapp/mattermost-webapp"
# Remove only the root bootstrap hook in this disposable checkout; dependency
# lifecycle scripts still run. The standalone bundle needs the webapp first.
( cd "$WORK/webapp" && npm pkg delete scripts.preinstall && npm ci --no-audit --no-fund )
( cd "$WORK/standalone" && npm ci --no-audit --no-fund )
git -C "$WORK" diff --exit-code -- webapp/package-lock.json standalone/package-lock.json

if [ "${RUN_TESTS:-0}" = "1" ]; then
    "$ROOT/scripts/test-component.sh" calls "$WORK"
fi

# Build helpers run on the host; upstream recipes set the plugin target platforms.
GOOS="$(go env GOHOSTOS)" GOARCH="$(go env GOHOSTARCH)" make -C "$WORK" dist

# The version is stamped AFTER the build on purpose. `make dist` runs
# `manifest apply`, which rewrites plugin.json from `git describe --tags`, so
# anything set beforehand is discarded. Tagging the checkout does not help
# either: git picks the upstream tag over ours when both point at HEAD.
bundles=("$WORK"/dist/*.tar.gz)
[ "${#bundles[@]}" = "1" ] && [ -f "${bundles[0]}" ] || {
    echo "build must produce exactly one bundle" >&2; exit 1
}
bundle="${bundles[0]}"

stage="$(mktemp -d "$WORK/bundle.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
tar xzf "$bundle" -C "$stage"
python3 - "$stage/com.mattermost.calls/plugin.json" "$CALLS_VERSION" "${SOURCE_URL:-}" <<'PY'
import json, sys
path, version, source_url = sys.argv[1:]
with open(path) as fh:
    m = json.load(fh)
# The plugin id is deliberately unchanged: MaintainedMost is a drop-in replacement
# for official Calls, so existing settings and call history survive the swap.
m["version"] = version
m["name"] = "Calls (MaintainedMost)"
# Never inherit another maintainer's links or infer ownership from the checkout.
m.pop("homepage_url", None)
m.pop("support_url", None)
if source_url:
    m["homepage_url"] = source_url.rstrip("/")
    m["support_url"] = source_url.rstrip("/") + "/issues"
with open(path, "w") as fh:
    json.dump(m, fh, indent=4)
    fh.write("\n")
print(f"    stamped {version}")
PY

target="$OUT/maintainedmost-calls-$CALLS_VERSION.tar.gz"
tar czf "$target" -C "$stage" com.mattermost.calls
( cd "$OUT" && shasum -a 256 "$(basename "$target")" > "$(basename "$target").sha256" )

echo "==> $target"
