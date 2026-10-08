# MaintainedMost Patch Obligations

Port the existing behavior, not just the patch syntax. The three patch sets are
one compatibility transaction: server, Calls and transcriber source revisions
come from a trusted immutable manifest. Do not weaken checks to get a green run.

- Keep every patch and its intent. Do not drop patches or tests. If upstream
  incorporates a change, report that it needs a reviewed baseline update rather
  than silently removing it.
- OIDC uses discovered HTTPS endpoints and authenticated server-side code
  exchange. Preserve exact issuer/audience/authorized-party/time checks, subject
  binding, boolean verified UserInfo email, strict TLS, redirect/SSRF policy,
  request-local credentials and transport cleanup. It does not claim JWS/JWKS
  signature verification; do not substitute unverified claims for UserInfo.
- Trust unsigned plugins only when they are local prepackaged image content.
  Present invalid signatures and remote/upload/URL/filestore/cluster installs
  must retain their existing signature and authorization enforcement.
- Keep the intended guest-account, channel-moderation and user-cap changes
  without removing administrator permissions or configuration controls. User
  caps are 200000 soft and 250000 hard. AI remains off until explicitly enabled.
- Calls keeps the plugin ID com.mattermost.calls and the independent licence
  checker. Do not import private enterprise implementations or the Source
  Available enterprise checker to make a build work.
- Recording respects explicit environment and stored opt-outs. Automatic
  recording is off by default and checks service readiness, current call/host,
  membership and permissions. Failed checks must not start jobs or broadcasts.
- Group video retains sender attribution through late metadata, receiver reuse,
  replacement and teardown. Preserve listener/timer cleanup and existing media
  handlers. The recorder does not capture the added group-camera streams.
- Keep recording and transcription image runners valid both at startup and
  later dispatch. Image-selection settings must not leak into job environments.
  Whisper stays the default; remote transcription backends are opt-in.
- Remote transcription retains bounded retry, interrupted-body/timeout handling,
  cancellation, option/timestamp propagation and redacted errors. Malformed
  complete responses are not retryable.
- Keep real Whisper, Opus, ONNX and Azure native libraries. Preserve the generic
  x86-64/SSE2 CPU baseline, the ARM branch and bounded compilation. Do not replace
  native integrations with stubs or bypass tests.
- Preserve MaintainedMost branding, official protocol/module names, upstream
  copyright/authorship and licences. Do not restore retired website services.

The fork's scripts, workflows, test inventory, artwork and deployment policy are
not patch-agent outputs. A trusted planner changes pins and the image default;
the agent changes only the existing patch files. The clean-room verifier checks
that boundary, and protected GitHub CI builds/tests the proposed merge before
it can reach the default branch. If an API/toolchain change cannot preserve
these obligations, stop and explain the failure.
