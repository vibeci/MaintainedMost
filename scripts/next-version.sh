#!/usr/bin/env bash
# Usage: ./scripts/next-version.sh <old-upstream.env> <new-upstream.env> <current-version>
#
# Prints the next MaintainedMost release version for the pin change between two
# upstream.env files. Exits 1 silently when nothing release-worthy changed.
#
# Rule (track-server-semver): a server major bump rolls our major; a server or
# Calls minor bump rolls our minor; any other pin change (patch-level tags,
# transcriber, recorder, offloader, toolchain, bundle-only fixes) rolls patch.
set -euo pipefail

old_env="${1:?old upstream.env required}"
new_env="${2:?new upstream.env required}"
current="${3:?current version required (vMAJOR.MINOR.PATCH)}"

get() { # file key -> value or empty
    local file="$1" key="$2" line
    line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
    printf '%s' "${line#"${key}="}"
}

semver() { # vX.Y.Z -> "X Y Z" or fail
    local raw="${1:?}" stripped="$1"
    stripped="${stripped#v}"
    [[ "$stripped" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || {
        echo "not a stable vMAJOR.MINOR.PATCH tag: $raw" >&2
        return 1
    }
    printf '%s %s %s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
}

# parse aborts (exit 2) on bad input. It must run in the main shell: inside
# $() an exit would only leave the subshell, and read(1) succeeds on the
# resulting empty herestring. Results come back via $PARSED.
parse() {
    PARSED="$(semver "$1")" || exit 2
    [[ -n "$PARSED" ]] || exit 2
}

old_server="$(get "$old_env" SERVER_TAG)"; new_server="$(get "$new_env" SERVER_TAG)"
old_calls="$(get "$old_env" CALLS_TAG)"; new_calls="$(get "$new_env" CALLS_TAG)"
# shellcheck disable=SC2086
parse "$old_server"; read -r old_server_major old_server_minor _ <<<"$PARSED"
# shellcheck disable=SC2086
parse "$new_server"; read -r new_server_major new_server_minor _ <<<"$PARSED"
# shellcheck disable=SC2086
parse "$old_calls"; read -r old_calls_major old_calls_minor _ <<<"$PARSED"
# shellcheck disable=SC2086
parse "$new_calls"; read -r new_calls_major new_calls_minor _ <<<"$PARSED"
# shellcheck disable=SC2086
parse "$current"; read -r major minor patch <<<"$PARSED"

if [[ "$new_server_major" != "$old_server_major" ]]; then
    printf 'v%d.0.0\n' "$((major + 1))"
elif [[ "$new_server_minor" != "$old_server_minor" || "$new_calls_major" != "$old_calls_major" || "$new_calls_minor" != "$old_calls_minor" ]]; then
    printf 'v%d.%d.0\n' "$major" "$((minor + 1))"
else
    # Any remaining pin difference (patch tags, transcriber, recorder,
    # offloader, toolchain, bundle-only fixes) is a patch release.
    changed=0
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[A-Z_]+$ ]] || continue
        [[ "$(get "$new_env" "$key")" == "$value" ]] || changed=1
    done < <(grep -E '^[A-Z_]+=' "$old_env")
    # Keys that only exist in the new file count as a change too.
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[A-Z_]+$ ]] || continue
        [[ "$(get "$old_env" "$key")" == "$value" ]] || changed=1
    done < <(grep -E '^[A-Z_]+=' "$new_env")
    if ((changed)); then
        printf 'v%d.%d.%d\n' "$major" "$minor" "$((patch + 1))"
    else
        exit 1
    fi
fi
