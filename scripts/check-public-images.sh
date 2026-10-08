#!/usr/bin/env bash
# Usage: ./scripts/check-public-images.sh IMAGE [IMAGE...]
# Inspect remote manifests as an anonymous pull client, never the logged-in publisher.
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "at least one remote image reference is required" >&2
    exit 1
fi
docker="$(type -P docker)" || { echo "Docker CLI is required" >&2; exit 1; }
umask 077
config="$(mktemp -d "${TMPDIR:-/tmp}/maintainedmost-public-images.XXXXXX")"
trap 'rm -rf "$config"' EXIT

failed=0
for image in "$@"; do
    echo "Checking anonymous registry access: $image"
    # Empty config/HOME prevent cached auth/manifests. Clearing the environment
    # removes tokens and auth overrides; restricted PATH disables credential-helper discovery.
    if ! env -i HOME="$config" PATH="$config" \
        "$docker" --config "$config" manifest inspect -- "$image" >/dev/null; then
        printf 'Anonymous registry access failed: %s\n' "$image" >&2
        failed=1
    fi
done

if [ "$failed" -ne 0 ]; then
    printf '%s\n' \
        'Release promotion is blocked: an image is private, missing, or inaccessible anonymously.' \
        'For GHCR, set each release package (server, calls-recorder, calls-transcriber) to Public in Package settings.' \
        'Connect each package to the correct repository and grant that repository Actions write access.' \
        'Repository linkage alone does not make a private package public.' \
        'If already public, check the reference and registry/network availability.' \
        'Then rerun the failed publish job for the existing draft release; do not bypass the gate or promote latest manually.' >&2
    exit 1
fi
