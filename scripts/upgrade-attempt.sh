#!/usr/bin/env bash
# Usage: ./scripts/upgrade-attempt.sh <server|calls|transcriber> <results-file>
#
# One mechanical upgrade step for the Upgrade workflow: verify the patch
# series against the newest upstream ref, resolve commit/image pins, and apply
# scripts/bump-pins.sh. Appends one tab-separated result line:
#   <component> <bumped|unchanged|needs-agent> <old> <new> <detail>
#
# Exit 0 when the result is recorded (even for needs-agent); exit 1 on
# operational failures (network, tooling) so the workflow retries later.
# A needs-agent outcome leaves the working tree untouched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
component="${1:?component required}"
results="${2:?results file required}"

record() {
    printf '%s\t%s\t%s\t%s\t%s\n' "$component" "$1" "$2" "$3" "$4" >> "$results"
}

check_out="$ROOT/build/upgrade-check-$component.txt"
mkdir -p "$ROOT/build"
set +e
"$ROOT/scripts/check-upstream.sh" "$component" >"$check_out" 2>"$check_out.err"
code=$?
set -e
if ((code == 0)); then
    :
elif ((code == 3)); then
    detail="$(tail -n 3 "$check_out.err" | tr '\n' ' ' | cut -c1-200)"
    record needs-agent "" "" "patch conflict: ${detail}"
    echo "needs-agent: ${component} patch series conflicts"
    exit 0
else
    echo "upgrade check failed operationally for ${component}:" >&2
    cat "$check_out.err" >&2
    exit 1
fi

value() { grep -E "^$1=" "$check_out" | tail -n 1 | cut -d= -f2-; }
new_ref="$(value new_ref)"
verification="$(value verification)"
if [[ "$verification" = unchanged ]]; then
    record unchanged "$(value current)" "$(value current)" ""
    echo "unchanged: ${component} $(value current)"
    exit 0
fi
[[ "$verification" = clean && -n "$new_ref" ]] || { echo "unexpected check output" >&2; cat "$check_out" >&2; exit 1; }

resolve_commit() { # repo tag -> 40-hex sha
    local sha
    sha="$(git ls-remote "https://github.com/$1.git" "refs/tags/$2" | cut -f1)"
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "cannot resolve $2 in $1" >&2; exit 1; }
    printf '%s' "$sha"
}

image_digest() { # image-without-digest -> sha256 hex
    local raw
    raw="$(docker buildx imagetools inspect --raw "$1" 2>/dev/null)" || { echo "cannot inspect $1" >&2; exit 1; }
    printf '%s' "$raw" | sha256sum | cut -d' ' -f1
}

case "$component" in
    server)
        old_tag="$(grep -E '^SERVER_TAG=' "$ROOT/upstream.env" | cut -d= -f2)"
        commit="$(resolve_commit mattermost/mattermost "$new_ref")"
        bare="${new_ref#v}"
        image="docker.io/mattermost/mattermost-team-edition:${bare}@sha256:$(image_digest "docker.io/mattermost/mattermost-team-edition:${bare}")"
        bump_args=(server --tag "$new_ref" --commit "$commit" --image "$image")
        ;;
    calls)
        old_tag="$(grep -E '^CALLS_TAG=' "$ROOT/upstream.env" | cut -d= -f2)"
        commit="$(resolve_commit mattermost/mattermost-plugin-calls "$new_ref")"
        clone="$ROOT/build/upgrade-calls-manifest"
        rm -rf "$clone"
        git clone --depth 1 --branch "$new_ref" "https://github.com/mattermost/mattermost-plugin-calls.git" "$clone" >&2
        manifest="$clone/plugin.json"
        min_server="$(python3 - "$manifest" <<'PY'
import json, re, sys
with open(sys.argv[1]) as source:
    manifest = json.load(source)
minimum = manifest.get("min_server_version", "")
print(minimum if isinstance(minimum, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", minimum) else "unknown")
PY
)"
        recorder="$(python3 - "$manifest" <<'PY'
import json, sys
with open(sys.argv[1]) as source:
    manifest = json.load(source)
print(manifest.get("props", {}).get("calls_recorder_version", ""))
PY
)"
        bump_args=(calls --tag "$new_ref" --commit "$commit")
        if [[ "$min_server" != unknown ]]; then
            bump_args+=(--min-server "$min_server")
        fi
        pinned_recorder="$(grep -E '^RECORDER_TAG=' "$ROOT/upstream.env" | cut -d= -f2)"
        if [[ -n "$recorder" && "$recorder" != "$pinned_recorder" ]]; then
            recorder_image="docker.io/mattermost/calls-recorder:${recorder}@sha256:$(image_digest "docker.io/mattermost/calls-recorder:${recorder}")"
            bump_args+=(--recorder-tag "$recorder" --recorder-image "$recorder_image")
        fi
        ;;
    transcriber)
        old_tag="$(grep -E '^TRANSCRIBER_TAG=' "$ROOT/upstream.env" | cut -d= -f2)"
        commit="$new_ref"
        [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "transcriber head is not a sha: $commit" >&2; exit 1; }
        bump_args=(transcriber --commit "$commit")
        ;;
    *)
        echo "unknown component: $component" >&2
        exit 2
        ;;
esac

set +e
bump_out="$("$ROOT/scripts/bump-pins.sh" "${bump_args[@]}" 2>&1)"
bump_code=$?
set -e
if ((bump_code == 0)); then
    record bumped "$old_tag" "$new_ref" "$(printf '%s' "$bump_out" | tr '\n' ';' | cut -c1-240)"
    printf '%s\n' "$bump_out"
    exit 0
fi
# An agent decision (stale prose, minimum-server, out-of-sync tree): reset the
# partial edit and record it instead of failing the workflow.
if ((bump_code == 1)); then
    git -C "$ROOT" checkout -- upstream.env Containerfile README.md SECURITY.md CONTRIBUTING.md docs/ .github/ || true
    record needs-agent "$old_tag" "$new_ref" "$(printf '%s' "$bump_out" | tr '\n' ' ' | cut -c1-240)"
    echo "needs-agent: ${component}: ${bump_out}"
    exit 0
fi
printf '%s\n' "$bump_out" >&2
exit 1
