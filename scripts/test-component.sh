#!/usr/bin/env bash
# Usage: ./scripts/test-component.sh server|calls|transcriber patched-source-dir
# Selectors can expand coverage, but cannot waive the protected test inventory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../upstream.env
source "$ROOT/upstream.env"
export GOTOOLCHAIN="go$GO_VERSION"
component="${1:?component required}"
WORK="$(cd "${2:?patched source directory required}" && pwd -P)"

# Race tests run on the host even when the caller is cross-compiling an image.
GOOS="$(go env GOHOSTOS)"
GOARCH="$(go env GOHOSTARCH)"
export GOOS GOARCH CGO_ENABLED=1
read -r -a extra_flags <<< "${GO_TEST_FLAGS:-}"

go_suite() {
    local suite="$1" required="$2" selector="$3" package
    shift 3
    for package in "$@"; do
        case "$package" in
            -short) ;;
            -*) echo "test package selectors must not contain flags: $package" >&2; return 1 ;;
        esac
    done
    mkdir -p "$ROOT/build/test-results"
    # Explicit flags override GOFLAGS and GO_TEST_FLAGS, including cache/race overrides.
    go test ${extra_flags[@]+"${extra_flags[@]}"} -json -race -count=1 -run "$selector" "$@" |
        tee "$ROOT/build/test-results/$suite.jsonl" |
        python3 "$ROOT/scripts/check-go-tests.py" "$required" \
            --inventory "$ROOT/maintenance/required-tests.json" --suite "$suite"
}

case "$component" in
    server)
        cd "$WORK/server"
        if [ ! -f go.work ]; then go work init . ./public; fi
        read -r -a packages <<< "${SERVER_TEST_PACKAGES:-./channels/app ./channels/api4}"
        # These app/API tests use mocks; full TestMain setup starts unrelated SQL/Redis services.
        go_suite server-api '^TestMaintainedMost' "${SERVER_TEST_PATTERN:-^TestMaintainedMost}" -short "${packages[@]}"
        # Do not filter out upstream OIDC tests: the entire provider is a trust boundary.
        go_suite server-oidc '^Test' '.' ./channels/app/oauthproviders/openid
        go_suite server-config '^Test' '^Test(GetClientConfig|GetLimitedClientConfig)$' -short ./config
        cd public
        go_suite server-model '^Test' "${SERVER_CONFIG_TEST_PATTERN:-^Test(MaintainedMost|Config|ServiceSettings|TeamSettings|GuestAccounts)}" ./model
        ;;
    calls)
        cd "$WORK"
        # Tests import the generated Go/TypeScript manifests even on a clean checkout.
        make setup-go-work
        make apply
        read -r -a packages <<< "${CALLS_TEST_PACKAGES:-./server ./server/license}"
        go_suite calls '^TestMaintainedMost' \
            "${CALLS_TEST_PATTERN:-^Test(MaintainedMost|GetClientConfig|AddUserSession|HandleJoin|HandleBotGetProfileForSession|HandleBotUploadData|ConfigurationIsValid|ConfigurationWillBeSaved|ApplyEnvOverrides|FieldNameToEnvKey|SetFieldFromEnv|SetOverridesDeprecatedRTCDURL|JobServiceApplyEnvOverrides)}" \
            "${packages[@]}"
        cd webapp
        # Run every upstream and MaintainedMost suite unless the caller explicitly filters.
        jest_args=(--ci --runInBand --coverage=false)
        if [ -n "${CALLS_JEST_TEST_PATTERN:-}" ]; then jest_args+=("$CALLS_JEST_TEST_PATTERN"); fi
        ./node_modules/.bin/jest "${jest_args[@]}"
        ;;
    transcriber)
        cd "$WORK"
        read -r -a packages <<< "${TRANSCRIBER_TEST_PACKAGES:-./cmd/transcriber/apis/openai ./cmd/transcriber/config}"
        go_suite transcriber '^Test' "${TRANSCRIBER_TEST_PATTERN:-.}" "${packages[@]}"
        ;;
    *) echo "unknown component: $component" >&2; exit 1 ;;
esac
