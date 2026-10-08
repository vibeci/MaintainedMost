#!/usr/bin/env bash
# Usage: TARGETARCH=amd64|arm64 ./scripts/build-transcriber.sh [image-tag]
# Uses upstream's native Whisper/Opus/ONNX/Azure build, not a Go-only substitute.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/lib.sh"
# shellcheck source=../upstream.env
source "$ROOT/upstream.env"
export GOTOOLCHAIN="go$GO_VERSION"
TARGETARCH="${TARGETARCH-amd64}"
case "$TARGETARCH" in
    amd64|arm64) ;;
    *) echo "unsupported TARGETARCH: $TARGETARCH (expected amd64 or arm64)" >&2; exit 1 ;;
esac
TAG="${1:-maintainedmost/calls-transcriber:v1.0.0-dev0}"
WORK="$ROOT/build/transcriber"
registry="${CALLS_IMAGE_REGISTRY:-${TAG%/calls-transcriber:*}}"
python3 "$ROOT/scripts/image-refs.py" --check-transcriber "$registry" "$TAG"
engine="$(container_engine)"

prepare_source https://github.com/mattermost/calls-transcriber.git \
    "$TRANSCRIBER_TAG" transcriber "$TRANSCRIBER_TAG"

if [ "${RUN_TESTS:-0}" = "1" ]; then
    "$ROOT/scripts/test-component.sh" transcriber "$WORK"
fi

# This target passes every native dependency version/hash from the pinned
# Makefile to build/Dockerfile. CI=true would otherwise PUSH upstream images.
make -C "$WORK" docker-build CI=false DOCKER="$engine" \
    ARCH="$TARGETARCH" GO_VERSION="$GO_VERSION" DOCKER_TAG="$TAG" \
    DOCKER_BUILD_PLATFORMS="linux/$TARGETARCH" DOCKER_BUILD_OUTPUT_TYPE=docker
echo "==> $TAG"
