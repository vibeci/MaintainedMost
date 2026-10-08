#!/usr/bin/env bash
# Shared by the build scripts and the upstream checker. No network on sourcing.

absolute_dir() {
    mkdir -p "$1" || return
    (cd "$1" && pwd -P)
}

job_images() {
    local refs
    refs="$(python3 "$ROOT/scripts/image-refs.py" "$1" "$RECORDER_TAG" \
        "${CALLS_IMAGE_REGISTRY:-}" "${TRANSCRIBER_IMAGE:-}")" || return
    read -r CALLS_IMAGE_REGISTRY TRANSCRIBER_IMAGE RECORDER_IMAGE <<< "$refs"
    export CALLS_IMAGE_REGISTRY TRANSCRIBER_IMAGE RECORDER_IMAGE
}

checkout_source() {
    local repo="$1" ref="$2" work="$3"
    if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
        git init --quiet "$work" &&
            git -C "$work" remote add origin "$repo" &&
            git -C "$work" fetch --quiet --depth 1 origin "$ref" &&
            git -C "$work" checkout --quiet --detach FETCH_HEAD
    else
        git clone --quiet --depth 1 --branch "$ref" "$repo" "$work"
    fi
}

apply_patches() {
    local work="$1" patch_dir patch
    patch_dir="$(cd "$2" && pwd -P)" || return
    local patches=("$patch_dir"/*.patch)
    if [ ! -f "${patches[0]}" ]; then
        echo "no patches found in $patch_dir" >&2
        return 1
    fi
    # Apply, not just --check: later patches can depend on earlier patches.
    for patch in "${patches[@]}"; do
        echo "    $(basename "$patch")"
        git -C "$work" apply "$patch" || return
    done
}

prepare_source() {
    local repo="$1" ref="$2" component="$3" expected="${4:-}"
    case "$component" in
        server|calls|transcriber) ;;
        *) echo "unknown component: $component" >&2; return 1 ;;
    esac
    local work="$ROOT/build/$component"
    echo "==> $component upstream $ref"
    # Only these generated checkouts are disposable, never a caller's source tree.
    mkdir -p "$ROOT/build"
    rm -rf "$work"
    checkout_source "$repo" "$ref" "$work" || return
    if [ -n "$expected" ] && [ "$(git -C "$work" rev-parse HEAD)" != "$expected" ]; then
        echo "$ref does not match pinned commit $expected" >&2
        return 1
    fi
    apply_patches "$work" "$ROOT/patches/$component"
}

container_engine() {
    local engine="${CONTAINER_ENGINE:-}"
    if [ -z "$engine" ]; then
        engine="$(command -v docker || command -v podman)" || {
            echo "needs docker or podman" >&2; return 1
        }
    fi
    engine="$(command -v "$engine")" || { echo "no such engine: $engine" >&2; return 1; }
    if ! "$engine" buildx version >/dev/null; then
        echo "the upstream transcriber build requires Docker Buildx" >&2
        return 1
    fi
    printf '%s\n' "$engine"
}
