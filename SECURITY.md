# Security

## Version scope

The maintained source baseline is the patch series and immutable pins in
[`upstream.env`](upstream.env): currently server 11.11.1, Calls source 1.12.3
(bundle 1000.12.4) and transcriber commit c09a9a3. Check that file at the release
tag you deploy for full commits and the matching server image digest. Older
builds and other upstream combinations are not covered by the current CI gate.
Monitor upstream advisories as well as this repository's releases; a passing
build is not a security audit or an end-to-end certification.

## Reporting

If private vulnerability reporting is enabled on the repository hosting the
build you use, open its **Security > Advisories > Report a vulnerability** form.
Do not put credentials, tokens, private keys, personal data or sensitive exploit
details in public issues. If that form is unavailable, request a private
reporting channel without disclosing the vulnerability publicly. This document
does not promise a response time or remediation deadline.

Include the release/source revision, image digest, affected component,
configuration with secrets removed, impact, reproduction steps and sanitized
logs. State whether you reproduced against the pinned source and whether a
workaround is known. Do not send production data when a minimal fixture works.

## Operator boundaries

- **Maintenance credentials:** use only the dedicated `maintenance` GitHub
  environment, restricted to the reviewed default branch. Do not duplicate model
  or publisher credentials into repository-wide secrets exposed to ordinary
  build jobs. See [maintenance setup](docs/maintenance.md#secrets-and-permissions).
- **OIDC migration:** test a backup restore and rollback, and verify a local
  administrator recovery account before changing SSO. `DiscoveryEndpoint` must
  identify the exact issuer (including a trailing slash) or its matching
  standard discovery URL. Discovery overrides manual endpoints, including
  legacy Google/Microsoft defaults; endpoint-only configurations are rejected.
- **OIDC identity:** an ID token with a string `sub` and valid `iss`, `aud`,
  `azp` where required, `exp` and `iat` is required. UserInfo must return exactly
  the same subject and boolean `email_verified: true`. Missing, null, false,
  string or numeric values are rejected, with no verified-email fallback from
  the ID token. Accounts are not linked by email automatically.
- **OIDC transport:** authentication relies on the server-side HTTPS code
  exchange, not cryptographic JWS/JWKS signature verification. Global
  `EnableInsecureOutgoingConnections` cannot disable TLS verification for this
  flow. Private identity providers need a trusted CA and narrowly scoped
  `AllowedUntrustedInternalConnections` entries, not an insecure-TLS workaround.
- **Plugin trust:** unsigned local prepackaged bundles are trusted with the
  server image at startup, for marketplace installs selecting that exact local
  bundle, and for transitional persistence. A present invalid signature is
  rejected; custom `SignaturePublicKeyFiles` remain supported. Remote uploads,
  marketplace downloads, URL installs and filestore/cluster reads keep their
  existing signature and authorization rules. Local trust does not follow an
  unsigned bundle copied into a remote install path.
- **Job service:** Docker socket access is effectively host-root access. Keep
  the offloader API private, especially when self-registration is enabled. Use
  a trusted registry namespace containing the allowed `calls-recorder` and
  `calls-transcriber` images with semantic version tags; do not enable
  `DEV_MODE` to bypass its allowlist.
- **Recording and transcription:** automatic recording is off by default and
  explicit recording/transcription opt-outs are honored. Participant notices
  remain visible. Remote transcription is opt-in and sends audio to the
  configured endpoint; review consent, data handling and provider behavior
  before enabling it. Local Whisper is the default.

## Maintenance privacy

Automated maintenance is opt-in; no credentials or activation are supplied by
this setup. Follow the [maintenance protocol](docs/maintenance.md) before enabling
it. Public and private GitHub repositories are accepted, but private branch
protection still requires an eligible plan. Repository privacy does not prevent
the configured model API from receiving relevant source code, patches and task
context. The runner host, trusted controller and provider remain trusted.

- **Private configuration:** `VIBECI_MODELS` and the other maintenance secrets
  belong only in the restricted `maintenance` environment, not repository or
  organization secrets or variables. Remove wider-scope copies. Keep actual
  provider endpoints, model IDs and identifying metadata
  out of checked-in code, docs, tests and public output. Required secret
  `VIBECI_PRIVATE_TERMS` is a nonempty JSON array of distinct, distinctive strings
  at least four characters long, including private provider/company/domain
  fragments. Operators must supply the real list privately to prohibit names
  without recording them in the repository; public examples use only generic
  placeholders and canonical aliases such as `provider-1` and `model-1`.
- **Credential separation:** the built-in `${{ github.token }}` has read-only
  `contents` scope for generation, transiently named `VIBECI_FORK_READ_TOKEN` on
  the host and mounted at `/run/secrets/fork_read_token`. The harness authenticates
  with `VIBECI_LLM_API_KEY` at `/run/secrets/llm_api_key`; models must use
  `file:/run/secrets/llm_api_key`. Never put API keys or Git tokens into prompts.
  `VIBECI_GIT_TOKEN` is a dedicated write PAT used only by the trusted publication
  controller. Docker service environments carry no credentials; the broker and
  workers get no credentials, and the harness gets no Docker socket.
- **Private runtime:** create concrete configuration and credential files only
  in a fresh private directory under `RUNNER_TEMP`, outside the checkout. Keep
  runtime image references neutral and digest-pinned; handle private registry
  access in a separate trusted pull step. Always delete private configuration,
  temporary credentials, raw results, mirrors and jobs after container cleanup
  and screened evidence collection. Ignore rules are an accidental-add backstop,
  not secret storage or protection for files already tracked by Git.
- **Publication boundary:** the engine always dry-runs, has no alert webhooks and
  never pushes directly, even with `VIBECI_PUSH=true`. Preview also runs the
  privacy gate without publishing. The gate scans the candidate publication
  graph and all reachable BASE history/trees, including paths, binary data and
  common encodings, for known secrets, provider configuration and private terms.
  It fails closed with generic errors, without printing matching values. A new
  commit uses a fixed neutral public identity and message template while preserving
  the exact verified TREE and BASE parent; only approved objects enter a fresh
  publisher store. Original agent commits and additional agent history are never
  pushed. Only the controller can push the safe SHA, create the PR and optionally
  merge that exact SHA after required CI checks.
- **Public evidence:** capture raw engine results privately, discard runtime
  stderr and use Docker logging `none`. Publish only allowlisted structural
  fields and privacy-screened artifacts/logs; canonicalize private input metadata
  rather than reflecting arbitrary fields. Operator logs must contain no
  configured provider identifiers. Never dump configuration or sources, enable
  debug/HTTP tracing, echo authorization headers, upload raw errors/model output
  or publish agent-generated commit text. GitHub log masking is defense in depth
  only.

Masking and filtering are not proof against undisclosed secrets, arbitrary
encryption/encoding or a compromised host/provider. Public logs and model outputs
can contain unpredictable text; discarding raw errors and replacing generated
commit metadata reduces exposure but does not remove that trust boundary. The
zero-reference goal requires the operator's complete private terms secret and
cannot be guaranteed for information the gate has never been given.

If a credential is ever exposed, promptly revoke or rotate it, review access and
repository history, and restrict or remove affected logs/artifacts. Removing a
file or adding an ignore rule does not revoke a credential or erase copies.
Report suspected exposure privately without repeating the value. This guidance
does not assert that any previous leak has been observed.
