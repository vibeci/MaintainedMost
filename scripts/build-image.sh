#!/usr/bin/env bash
#
# Builds the server and patched transcriber, and locally tags the pinned recorder.
#
# Usage: ./scripts/build-image.sh [tag]
# Requires: docker with buildx, plus the component build dependencies.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/lib.sh"
TAG="${1:-maintainedmost:dev}"
STAGE="$ROOT/build/image"

# shellcheck source=../upstream.env
source "$ROOT/upstream.env"
export GOTOOLCHAIN="go$GO_VERSION"

# Fail before cloning, deleting outputs or building if any requested ref is invalid.
job_images "$TAG"

engine="$(container_engine)"
export CONTAINER_ENGINE="$engine"

mkdir -p "$ROOT/build"
rm -rf "$STAGE"
mkdir -p "$STAGE"
"$ROOT/scripts/build-calls.sh" "$STAGE/plugin"
cp "$STAGE/plugin/maintainedmost-calls-$CALLS_VERSION.tar.gz" "$STAGE/maintainedmost-calls.tar.gz"
# The pinned Team Edition runtime is Linux/amd64 only. A static build avoids
# host libc and cross-C compiler dependencies on macOS (including Apple Silicon).
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 SKIP_WEBAPP=0 \
    "$ROOT/scripts/build-server.sh" "$STAGE/maintainedmost-server"
# The combined stack shares the pinned runtime's architecture; standalone
# transcriber builds can select ARM64 independently with TARGETARCH.
TARGETARCH=amd64 "$ROOT/scripts/build-transcriber.sh" "$TRANSCRIBER_IMAGE"
# Pulling by digest verifies the upstream payload. Both job images must be under
# the same namespace because the offloader accepts only one registry prefix.
"$engine" pull --platform linux/amd64 "$RECORDER_SOURCE_IMAGE"
"$engine" tag "$RECORDER_SOURCE_IMAGE" "$RECORDER_IMAGE"
cp "$ROOT/Containerfile" "$STAGE/Containerfile"

# The image rewrites the config upstream ships, so both halves of that go into
# the build context.
mkdir -p "$STAGE/config" "$STAGE/scripts"
cp "$ROOT/config/overrides.json" "$STAGE/config/overrides.json"
cp "$ROOT/scripts/merge-config.py" "$STAGE/scripts/merge-config.py"
cp "$ROOT/config/image-context.ignore" "$STAGE/.dockerignore"

"$engine" build --platform linux/amd64 -t "$TAG" \
    --build-arg "SERVER_IMAGE=$SERVER_IMAGE" \
    --build-arg "CALLS_IMAGE_REGISTRY=$CALLS_IMAGE_REGISTRY" \
    --build-arg "TRANSCRIBER_IMAGE=$TRANSCRIBER_IMAGE" \
    --build-arg "SOURCE_URL=${SOURCE_URL:-}" \
    -f "$STAGE/Containerfile" "$STAGE"
echo "==> $TAG"
echo "==> $RECORDER_IMAGE"
