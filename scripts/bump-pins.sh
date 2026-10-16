#!/usr/bin/env bash
# Usage:
#   scripts/bump-pins.sh server --tag vX.Y.Z --commit <sha> --image <ref@digest>
#   scripts/bump-pins.sh calls --tag vX.Y.Z --commit <sha> [--min-server X.Y.Z]
#       [--recorder-tag vA.B.C --recorder-image <ref>] [--bundle 1000.x.y]
#   scripts/bump-pins.sh transcriber --commit <sha>
#
# Mechanically advances one component's pins in upstream.env, the Containerfile
# server default, and the prose references that track them. Fails without
# changing anything when a stale reference would remain: that means a human or
# agent needs to look (docs rewrites, incompatible minimums, recorder moves).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV="$ROOT/upstream.env"
CONTAINERFILE="$ROOT/Containerfile"

get() {
    local key="$1" line
    line="$(grep -E "^${key}=" "$ENV" | tail -n 1 || true)"
    [[ -n "$line" ]] || { echo "missing $key in upstream.env" >&2; return 1; }
    printf '%s' "${line#"${key}="}"
}

set_key() {
    local key="$1" value="$2"
    python3 - "$ENV" "$key" "$value" <<'PY'
import re, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as source:
    text = source.read()
updated, count = re.subn(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, count=1, flags=re.MULTILINE)
if not count:
    sys.exit(f"missing {key} in upstream.env")
with open(path, "w") as target:
    target.write(updated)
PY
}

valid_tag() { [[ "$1" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; }
valid_sha() { [[ "$1" =~ ^[0-9a-f]{40}$ ]]; }
valid_image() { [[ "$1" =~ ^[^[:space:]]+:[^[:space:]]+@sha256:[0-9a-f]{64}$ ]]; }

# replace_all <old> <new> <file...> — fixed-string substitution, then prove
# no stale occurrence of the old token remains in those files.
replace_all() {
    local old="$1" new="$2"
    shift 2
    [[ "$old" != "$new" ]] || return 0
    local file
    for file in "$@"; do
        [[ -f "$file" ]] || { echo "missing file: $file" >&2; return 1; }
    done
    # Fixed-string replacement via python3: portable across BSD/GNU sed.
    python3 - "$old" "$new" "$@" <<'PY'
import sys
old, new, paths = sys.argv[1], sys.argv[2], sys.argv[3:]
for path in paths:
    with open(path) as source:
        text = source.read()
    with open(path, "w") as target:
        target.write(text.replace(old, new))
PY
    for file in "$@"; do
        if grep -qF "$old" "$file"; then
            echo "stale reference to '${old}' remains in $file" >&2
            return 1
        fi
    done
}

# compare_versions <a> <b>: exit 0 if a >= b (dotted numeric, any length).
version_at_least() {
    python3 - "$1" "$2" <<'PY'
import sys
a = [int(p) for p in sys.argv[1].split(".")]
b = [int(p) for p in sys.argv[2].split(".")]
n = max(len(a), len(b))
a += [0] * (n - len(a))
b += [0] * (n - len(b))
sys.exit(0 if a >= b else 1)
PY
}

component="${1:?component required (server|calls|transcriber)}"
shift
tag="" commit="" image="" min_server="" recorder_tag="" recorder_image="" bundle=""
while (($#)); do
    case "$1" in
        --tag) tag="${2:?}"; shift 2 ;;
        --commit) commit="${2:?}"; shift 2 ;;
        --image) image="${2:?}"; shift 2 ;;
        --min-server) min_server="${2:?}"; shift 2 ;;
        --recorder-tag) recorder_tag="${2:?}"; shift 2 ;;
        --recorder-image) recorder_image="${2:?}"; shift 2 ;;
        --bundle) bundle="${2:?}"; shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

case "$component" in
    server)
        valid_tag "$tag" || { echo "bad --tag: $tag" >&2; exit 2; }
        valid_sha "$commit" || { echo "bad --commit: $commit" >&2; exit 2; }
        valid_image "$image" || { echo "bad --image: $image" >&2; exit 2; }
        old_tag="$(get SERVER_TAG)"; old_commit="$(get SERVER_COMMIT)"; old_image="$(get SERVER_IMAGE)"
        old_bare="${old_tag#v}"; new_bare="${tag#v}"
        [[ "$image" == *":${new_bare}@"* ]] || { echo "--image tag does not match --tag" >&2; exit 2; }
        set_key SERVER_TAG "$tag"
        set_key SERVER_COMMIT "$commit"
        set_key SERVER_IMAGE "$image"
        # Containerfile default must track upstream.env exactly.
        grep -qF "ARG SERVER_IMAGE=${old_image}" "$CONTAINERFILE" || { echo "Containerfile default out of sync" >&2; exit 1; }
        replace_all "ARG SERVER_IMAGE=${old_image}" "ARG SERVER_IMAGE=${image}" "$CONTAINERFILE"
        replace_all "$old_commit" "$commit" "$ROOT/docs/licensing.md"
        replace_all "$old_bare" "$new_bare" \
            "$ROOT/CONTRIBUTING.md" "$ROOT/README.md" "$ROOT/SECURITY.md" \
            "$ROOT/docs/README.md" "$ROOT/docs/install.md" "$ROOT/docs/licensing.md" "$ROOT/docs/upgrading.md" \
            "$ROOT/.github/ISSUE_TEMPLATE/feature_request.yml" \
            "$ROOT/.github/ISSUE_TEMPLATE/bug_report.yml" \
            "$ROOT/.github/PULL_REQUEST_TEMPLATE.md"
        echo "server: ${old_tag} -> ${tag} (${commit:0:12})"
        ;;
    calls)
        valid_tag "$tag" || { echo "bad --tag: $tag" >&2; exit 2; }
        valid_sha "$commit" || { echo "bad --commit: $commit" >&2; exit 2; }
        old_tag="$(get CALLS_TAG)"; old_commit="$(get CALLS_COMMIT)"
        old_bundle="$(get CALLS_VERSION)"
        old_bare="${old_tag#v}"; new_bare="${tag#v}"
        if [[ -n "$min_server" ]]; then
            pinned="$(get SERVER_TAG)"; pinned="${pinned#v}"
            version_at_least "$pinned" "$min_server" || {
                echo "calls ${tag} needs server >= ${min_server}; pinned ${pinned}" >&2
                exit 1
            }
        fi
        if [[ -z "$bundle" ]]; then
            bundle="1000.$(printf '%s' "$new_bare" | cut -d. -f2).$(printf '%s' "$new_bare" | cut -d. -f3)"
        fi
        [[ "$bundle" =~ ^1000\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || { echo "bad bundle: $bundle" >&2; exit 2; }
        set_key CALLS_TAG "$tag"
        set_key CALLS_COMMIT "$commit"
        set_key CALLS_VERSION "$bundle"
        replace_all "$old_commit" "$commit" "$ROOT/docs/licensing.md"
        # Bundle first: the bare source version is a substring of the bundle.
        replace_all "$old_bundle" "$bundle" \
            "$ROOT/CONTRIBUTING.md" "$ROOT/README.md" "$ROOT/SECURITY.md" \
            "$ROOT/docs/README.md" "$ROOT/docs/install.md" "$ROOT/docs/upgrading.md" \
            "$ROOT/.github/ISSUE_TEMPLATE/feature_request.yml" \
            "$ROOT/.github/ISSUE_TEMPLATE/bug_report.yml"
        replace_all "$old_bare" "$new_bare" \
            "$ROOT/CONTRIBUTING.md" "$ROOT/README.md" "$ROOT/SECURITY.md" \
            "$ROOT/docs/README.md" "$ROOT/docs/licensing.md" "$ROOT/docs/upgrading.md"
        if [[ -n "$recorder_tag" ]]; then
            valid_tag "$recorder_tag" || { echo "bad --recorder-tag" >&2; exit 2; }
            valid_image "$recorder_image" || { echo "bad --recorder-image" >&2; exit 2; }
            old_recorder_tag="$(get RECORDER_TAG)"; old_recorder_image="$(get RECORDER_SOURCE_IMAGE)"
            set_key RECORDER_TAG "$recorder_tag"
            set_key RECORDER_SOURCE_IMAGE "$recorder_image"
            echo "recorder: ${old_recorder_tag} -> ${recorder_tag}"
        fi
        echo "calls: ${old_tag} -> ${tag} (${commit:0:12}), bundle ${old_bundle} -> ${bundle}"
        ;;
    transcriber)
        valid_sha "$commit" || { echo "bad --commit: $commit" >&2; exit 2; }
        old_full="$(get TRANSCRIBER_TAG)"
        valid_sha "$old_full" || { echo "pinned transcriber is not a sha" >&2; exit 2; }
        set_key TRANSCRIBER_TAG "$commit"
        replace_all "$old_full" "$commit" "$ROOT/docs/licensing.md"
        replace_all "${old_full:0:7}" "${commit:0:7}" \
            "$ROOT/README.md" "$ROOT/SECURITY.md" \
            "$ROOT/docs/README.md" "$ROOT/docs/upgrading.md"
        echo "transcriber: ${old_full:0:12} -> ${commit:0:12}"
        ;;
    *)
        echo "unknown component: $component" >&2
        exit 2
        ;;
esac
