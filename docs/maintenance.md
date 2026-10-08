# Automated Maintenance With VibeCI

The repository includes an opt-in VibeCI integration. It is **not activated by
committing these files**: the target repository, images, model access, bot token
and branch protection must be provisioned first. Nothing here configures or
modifies the separate VibeCI source checkout.

## Update Contract

The daily [maintenance workflow](../.github/workflows/maintain.yml) runs at
07:17 UTC, or on manual dispatch. It operates only on the current default branch.

1. Check the trusted checkout against GitHub and stop if a same-repository
   maintenance proposal targeting the current default branch is already open.
2. Resolve compatible public upstream sources, verify current tag identities and
   image digests, and select complete Linux/amd64 runtime images.
3. Create a small local Git manifest with the old and new `upstream.env`, tagged
   with consecutive [maintenance versions](../maintenance/upstream-version).
4. Run one VibeCI patch-mode job with server, Calls and transcriber as three
   embedded sources. Their revisions are exact commits in the manifest. This
   handles the transcriber's branch pin without inventing upstream release tags.
   The engine always runs in dry-run mode, including when publication is enabled.
5. Let VibeCI refresh/port the existing patches and audit agent-written changes.
   Its strict reapplication is followed by the trusted offline candidate guard.
6. Stop the workers, run the trusted publication privacy gate, and reconstruct a
   neutral public commit in a fresh publisher object store. Preview runs this
   same gate without publishing.
7. Only in publishing mode, let the trusted controller push that safe SHA and open
   a PR. Optional controller merging waits for CI, then merges only that exact
   safe SHA under strict branch protection. It never queues deferred auto-merge.

The [planner](../scripts/vibeci-plan.py) computes the complete lockfile and matching
Containerfile before VibeCI creates its candidate. Verification does not repair
metadata: VibeCI does not commit verification-time edits. The guard binds the
candidate's single parent, manifest, metadata hashes and complete patch inventory,
and rejects changes to any other fork file or a dirty verification checkout.

No external manifest repository is needed. Each run owns its local manifest,
state, source copies, containers and control-socket volume. The planner checks
upstream source/image identities again on every run; ephemeral VibeCI state is
not a replacement for those checks.

Proposal branches include a fresh random suffix. A retained branch from a failed
PR-creation attempt or closed PR cannot make a later run incorrectly appear
up-to-date. Cleanup also removes and rechecks this run's broker-created workers
by their exact instance label; Compose teardown alone is not considered enough.

## Automatic Scope

- Server updates stay within the pinned major release. Major upgrades require a
  reviewed baseline, including database migrations and compatible toolchains.
- Calls follows the newest stable release compatible with that server. Missing
  or malformed compatibility metadata fails rather than being treated as approval.
- Recorder version follows the selected Calls manifest; its image is digest-pinned.
- Transcriber follows the configured branch only when the new commit is proven
  to descend from the existing pin. Rewinds and divergence require review.
- Every actual update advances the fork Calls bundle version monotonically,
  including transactions where Calls source itself is unchanged.
- Offloader, Go/Node versions, native-library dependencies, validation policy,
  artwork and workflows are not independently upgraded by the patch agent.
- Patches are never automatically dropped. Upstreamed features and renamed or
  retired required tests need a reviewed baseline change.

These are deliberate stop conditions, not claims of arbitrary upstream
compatibility. The existing [upstream advisory](../.github/workflows/upstream-check.yml)
still reports releases outside the automatic scope.

## Provisioning

Use a public or private GitHub repository you control; the helper no longer
requires public visibility. The workflow derives its repository and default
branch from the actual event; no publisher account is hardcoded.
Commit and publish the reviewed MaintainedMost baseline, including this setup,
before attempting a live run. The planner rejects uncommitted maintenance inputs.
The independent CI gate also needs its checker and inventory on the protected
base first; an initial setup PR cannot supply its own trusted policy. Publish
the reviewed bootstrap baseline before enabling maintenance or its required checks.

The maintenance job uses GitHub's Linux runner and Docker Engine 26 or newer.
Full image validation must run on native Linux/amd64 with sufficient memory and
disk for the existing [build workflow](../.github/workflows/build.yml). Do not
substitute the small patch-repair sandbox for that builder. A self-hosted
alternative must be an isolated disposable runner, not the production server.

Provision two images for the runner's architecture:

- VibeCI, built from reviewed source with patch mode. This integration was checked
  against commit `80beba360667c5fe7fa8306f70e3281ea7d79665`. Its deployment Dockerfile
  can be built from a read-only source checkout; no changes there are needed.
- The MaintainedMost sandbox, built with
  `docker build -f maintenance/Sandbox.Containerfile -t YOUR_SANDBOX_TAG maintenance`.
  It contains shell/Git/Python tools and the trusted verifier, not a Docker daemon
  or application build toolchain.

Publish the images to a registry the runner can pull without exposing publishing
credentials to repair jobs. Configure **full image digest references**, not mutable
tags. Image references supplied through repository variables must use neutral,
public-safe registry, namespace and image names, with no private provider
identifiers. For a private registry, authenticate in a separate trusted pull step;
do not pass registry credentials to the harness, broker or workers. The generated
job also pins the verifier's content hash to the trusted fork commit. Rebuild/re-pin
the sandbox when `maintenance/verify.py` changes; an outdated verifier deliberately
fails the job.

### Repository Variables

| Variable | Value |
| --- | --- |
| `VIBECI_ENABLED` | `true` to allow jobs; absent/false disables them |
| `VIBECI_PUSH` | `true` to allow privacy-gated controller publication; absent/false is preview-only. The engine always dry-runs |
| `VIBECI_AUTOMERGE` | `true` to wait for checks and request an exact-SHA protected merge; absent/false leaves PRs for review |
| `VIBECI_IMAGE` | Neutral public-safe VibeCI image reference pinned with `@sha256:` |
| `VIBECI_SANDBOX_IMAGE` | Neutral public-safe digest reference for the image built from `maintenance/Sandbox.Containerfile` |

### Secrets And Permissions

Create the GitHub environment **`maintenance`**, allow deployments only from the
reviewed default branch, and store the four secrets below in that environment.
The maintenance job alone references it. Do not duplicate these credentials in
repository or organization secrets available to ordinary build jobs: upstream
build scripts and dependency lifecycle hooks must not share the credentialed
job or its secret scope. Configure the environment before enabling the workflow.

| Secret | Scope |
| --- | --- |
| `VIBECI_MODELS` | Private JSON containing only `providers`, `models`, and `roles`, including actual provider `base_url` and model IDs |
| `VIBECI_PRIVATE_TERMS` | Required nonempty JSON array of distinct, distinctive strings of at least four characters; include private provider/company names and domain fragments |
| `VIBECI_LLM_API_KEY` | Actual provider API key; file-mounted for harness authentication, never prompt content |
| `VIBECI_GIT_TOKEN` | Dedicated fine-grained write PAT for this repository; trusted publication controller only, never generation |

**Move `VIBECI_MODELS` from a repository variable to a `maintenance` environment
secret** and remove the old variable before enabling the workflow. Move any
existing repository-level credentials into that environment as well. Actual provider names,
domains, model IDs and private terms must never appear in checked-in code, docs,
tests, examples, public logs or artifacts. Use the generic word `provider` in
public descriptions.

[models.example.json](../maintenance/models.example.json) shows the configuration
shape, **not a usable model selection**. Supply the actual endpoint and capable
audit/repair model IDs only in the secret. Each provider's `api_key` **must be
`file:/run/secrets/llm_api_key`**, never a literal key or environment reference.
An HTTPS `base_url` must not contain credentials. Multiple providers in this
adapter share the same API-key file, suitable for a common gateway. Provider and
model aliases are canonicalized to `provider-1`, `model-1`, and so on, with role
references rewritten to match; aliases are not a place to expose private names.

A synthetic `VIBECI_PRIVATE_TERMS` example is
`["private-provider-marker", "private-company-marker", "private-domain-fragment"]`.
Replace these placeholders privately with the distinctive terms to prohibit,
including provider/company/domain fragments that might appear without the full
endpoint or model ID. Do not use generic terms such as `provider`, and never
check in the real list. To prohibit an actual name without putting that name in
the repository, the operator **must set this secret**; configuration-derived
terms alone cannot identify every private name. Missing or invalid privacy
inputs fail closed, including in preview.

The Git token needs Contents and Pull Requests **read/write**, plus Administration
**read** to inspect protection for auto-merge. It must not bypass branch rules.
Prefer a dedicated identity rather than an administrator's broad token. Manage
token expiry and rotation; a static secret is not a token-renewal service.

Generation uses the built-in `${{ github.token }}` with read-only `contents`
permission for fork reads. `VIBECI_FORK_READ_TOKEN` is only a transient host-step
environment name used to write `/run/secrets/fork_read_token`'s backing file;
it is not another repository secret and must not enter Docker's environment.
The model key similarly backs `/run/secrets/llm_api_key`. Mount both read-only
only into the harness. Never substitute the built-in token for the publisher
PAT: its pushes/PRs generally do not trigger normal validation workflows.
No application credentials are needed. Do not obtain keys by reading another
project's secret directory.

### Private Runtime

Create fresh private configuration and credential files only under a unique
`RUNNER_TEMP` directory **outside the checkout**, with directory mode `0700` and
model/configuration/result file mode `0600`. Credential backing files are briefly
`0444` inside a host-private `0700` parent so the fixed non-root harness UID can
read its individual read-only mounts; they are unlinked when the engine call ends.
The broker's non-secret policy file is also `0444` for its capability-dropped UID.
Do not place private files in the source tree, dump them into logs or
artifacts, or copy credential files into source mirrors or job workspaces.
The `.gitignore` rules are a backstop, not permission to keep runtime secrets
in the checkout; they do not untrack already committed files.

The harness receives file-mounted read/API credentials, never the publisher PAT.
No Docker service receives credential environment variables. The broker and
workers receive no credentials; only the broker has Docker access, and the
harness has no Docker socket. The engine configuration always sets dry-run,
disables alert webhooks and forbids direct Git pushes even when
`VIBECI_PUSH=true`. Only the trusted controller can publish after privacy checks.

The host wrapper captures the raw engine result privately and **discards runtime
stderr**. Docker logging is `none`; raw errors are neither public diagnostics nor
retained stderr artifacts. Public logs, step summaries and uploaded evidence must
be reconstructed from allowlisted structural fields and privacy-screened bytes,
not arbitrary model/provider text. Canonicalize private input fields and aliases;
do not reflect unknown result keys or metadata into public output. Operator logs
must contain no configured provider identifiers.

GitHub log masking is defense in depth, not the publication boundary. Do not dump
configuration or sources, enable shell/debug/HTTP tracing, echo authorization
headers, or print raw model responses. Model output and error text are
unpredictable, so free-form errors and agent-generated commit text are not
published. After container/worker cleanup, any permitted publication and screened
evidence collection, always delete the private run directory, including
configuration, temporary credential files, raw results, mirrors, jobs and the
temporary publisher store. Container/worker cleanup failure must block publication;
a disposable runner remains necessary for interrupted cleanup.

### Protected Auto-Merge

Enable merge commits. GitHub's deferred auto-merge setting is not needed.
Configure **classic branch protection** for the default branch with strict
up-to-date checks and enforcement for administrators. Require both exact
contexts, bound specifically to the GitHub Actions app (`15368`):

- `MaintainedMost infrastructure tests`
- `MaintainedMost regression tests and complete images`

Rulesets alone are not accepted by the current preflight. Extra protections may
remain, but required human approvals will naturally prevent unattended merging.
Private repositories need a GitHub plan that supports environment secrets and,
for automatic merging, the required branch protection, such as Pro, Team or
Enterprise. Accepting private visibility does not waive those requirements.
The helper refuses absent, ambiguous or unbound checks and rechecks protection
while waiting and before merging. It waits up to two hours for checks and sends
the reconstructed safe SHA as an atomic merge precondition. A changed head/base,
failed, skipped or timed-out check stops the run. It never approves a PR, overrides
a failure, uses an administrator bypass or force-pushes the default branch.

## Activation Sequence

1. Provision neutral digest-pinned images and all four `maintenance` environment
   secrets, including private terms; remove old variables and repository-level
   copies. Restrict the environment to the reviewed default branch. Configure
   branch protection and confirm that the reviewed baseline passes full CI.
   Verify that the deployed workflow follows the private runtime protocol above.
2. Set `VIBECI_ENABLED=true`, leaving push and auto-merge disabled. Manually run
   **MaintainedMost Maintenance** with `preview=true`. Inspect only screened
   artifacts and structural logs, with no configured provider identifiers.
   A no-op checks current refs/images but does not exercise models, the broker
   or candidate publication; validate those before enabling unattended writes.
3. Validate a known update in preview, including a real conflict-resolution and
   audit cycle with your models and the publication privacy gate. Confirm the
   intended features/tests survive and no branch or PR is published.
4. Set `VIBECI_PUSH=true`, keep auto-merge disabled, and dispatch with
   `preview=false`. Inspect the first proposal and its complete CI result.
5. Enable `VIBECI_AUTOMERGE=true` only after reviewing the proposal path and all
   protection checks. Scheduled runs can then open and auto-merge passing updates.

Manual dispatch defaults to preview even when publishing is enabled. Preview
can consume model credits and runs the privacy gate, but does not push or create
a PR. Disable `VIBECI_ENABLED` to pause future cycles. Failure evidence is limited
to screened public plan/summary data and reconstructed structural result fields;
omit data that cannot be safely validated. Never upload model configuration,
credentials, raw results, agent-generated commit messages or whole workspaces.
Configure GitHub Actions failure notifications for the operating team, not engine
alert webhooks. This documentation change does not activate anything or provision
credentials.

## Publication Privacy Gate

The trusted [privacy helper](../scripts/vibeci-private.py) checks known secret
values, private provider configuration and `VIBECI_PRIVATE_TERMS` against the
candidate publication graph and **all reachable BASE history and trees**, not
just changed text files. It covers binary blobs, paths and historical commit
metadata, including literal values and common URL/JSON, base64/base64url and hex
encodings. Missing inputs, a match, malformed objects or exhausted scan bounds
fail closed with a generic error, never the matching value or raw diagnostics.
The same policy screens public evidence before upload or inclusion in a PR.

After rechecking the candidate's plan binding, the gate preserves the exact
verified `TREE` and single `BASE` parent but creates a new commit with a fixed
neutral public author/committer and a fixed public message template filled only
with verified plan fields. Only approved objects enter a fresh publisher store,
with no copied engine refs, configuration, hooks or alternate object stores. The
original agent commit and any additional agent history are never pushed; the
already verified BASE history is retained and scanned. The new safe SHA, not the
original engine SHA, is bound to the proposal, CI checks and optional exact-SHA
merge. Only the trusted controller uses the write PAT to push from that store
and create the PR.

## Validation Boundaries

Repair/verification workers have no network, credentials or Docker socket. They
run as UID/GID 10001 with a read-only root, dropped capabilities and resource
limits. Only the broker has Docker access; the harness has model API and read-only
fork access, not publishing authority. The small health probe receives only the
private control socket.

The runner host, trusted controller and configured provider remain trusted.
The model API receives relevant source code, patches and task context during
generation, including when the GitHub repository is private; review provider
data handling before enabling it. Never feed API keys or Git tokens into prompts.
File-mounted credentials are authentication inputs, not model context.

The privacy gate and masking are not proof against an undisclosed secret,
arbitrary encryption/encoding or a compromised host/provider. They do not make
untrusted output inherently safe or undo existing publication. The zero-reference
goal depends on a complete private terms secret and the stated trust boundary;
do not publish unscreened logs or weaken the gate to diagnose a rejection.
If any credential is ever exposed, revoke or rotate it promptly, then restrict
or remove affected logs/artifacts and review repository history and access.
Deleting a file or adding an ignore rule does not revoke a credential. This is
incident guidance, not a claim that an exposure has been observed.

Patch mode **trusts upstream source revisions**; it does not review every
upstream commit for malice. VibeCI audits its patch agent's changes. Exact pins,
protection and tests do not constitute a complete supply-chain security proof.

The [required Go inventory](../maintenance/required-tests.json) keeps the agent
from removing required tests/packages and getting green from whatever remains.
Each named test and package must complete successfully; matching skips, failures,
missing results and malformed/truncated reports fail. Selectors may add coverage
but cannot waive this inventory. After the build, a separate fresh runner checks
all six JSON reports using the protected base revision's checker and inventory.
Build scripts cannot replace that final checker by modifying their own checkout.
The required complete-images check fails if either producer fails or reports are
missing; a skipped producer is not success. For legitimate test retirement,
review the policy change first while retaining the old tests, then change the
tests in a later update.

This guards required-result validation, not report authenticity, malicious
test code or the strength of every assertion. Full CI executes upstream build
scripts on disposable workers with Docker access, not in the restricted patch
sandbox. Trust in upstream and the patch audit still matters. Jest runs the
complete selected client suite but does not yet have an equivalent protected
per-test inventory.

Full Go/Node/native builds run in the normal PR gate, not inside the broker.
If patches apply but later build/tests fail, **VibeCI patch mode does not start a
second build-repair agent**. The PR stays unmerged and blocks another proposal.
Inspect it, fix/review the baseline, or close it before replanning. A proposal
whose base becomes stale is also stopped rather than force-updated. Nothing
automatically tags releases, publishes runtime images or upgrades installations;
those remain the separate [release process](../CONTRIBUTING.md#publishing-a-fork).

Tests cover planner metadata/registry rejection, actual temporary-Git candidate
boundaries, private publication gating with synthetic canaries, and non-vacuous
Go results. The optional `VIBECI_BINARY` environment variable makes
`test_vibeci_verify.py` validate generated configuration with that actual binary,
without credentials or network.
Earlier local Docker integration exercised the real engine/broker, all three
patch roots, dry-run, synthetic branch publication and rejection of an incorrect
planned metadata hash. That is not end-to-end validation of this private runtime
protocol or a substitute for the first real-model preview and deployment trials.
