# Security policy (SECURITY)

## Reporting

This project is a personal working repository; there is no public bug bounty and no external
service. If you find a security issue, contact the repository maintainer directly (do not open a
public issue to discuss exploitable details).

## Explicit boundaries of the threat model (consistent with the design specification, not overstated)

The following are mechanisms this SDK **does** provide:

- A single-profile / single-database / single-host boundary; the store's profile identity binding.
- Path safety (zip-slip, symlink escape) and Skill source closure validation.
- Hook sandboxing (macOS seatbelt / Linux bubblewrap; a failed probe fails closed and never
  silently degrades into running without isolation).
- An outbound network broker (domain allowlist, redirects disabled, loopback SSRF guard, TLS
  verification).
- Frozen-parameter approvals (bound to a request hash), revocation cascading immediately, and a
  non-replaceable owner destination.
- Delivery accounting (attempt journal, idempotency keys, no blind sends on unknown).
- Control-plane authentication: same-UID Unix socket peer credentials, or a `token_file` bearer
  token (constant-time comparison); the token never enters logs and is never exposed to the model
  or to Skills.
- Structured logs are redacted by default (bearer tokens / API keys / private key blocks /
  high-entropy strings).

The following are things this SDK **does not** provide and must not be claimed to provide:

- Multi-tenant isolation, cross-host HA, or exactly-once delivery over an external network.
- Protection against root: root on the same machine can read the state directory, the socket and
  the backup files.
- Safe proactive execution on an uncontrolled host: a raw shell, the network or credentials can
  all bypass the broker, so an uncontrolled host must not be used for security-sensitive
  proactive execution.
- Prompts, Skill allowed-tools and dry-run environment variables are not security boundaries.
- `approval resolve` / `grants.create` are trusted UI entry points and are not exposed to the
  model; the authenticated actor comes from the control plane, and any owner self-reported in an
  RPC request body is never trusted.

## Secrets and data

- The control-plane token file must be `0600` (`pas doctor` checks this).
- The state directory should be `0700`; backup files are forced to `0600` by the tooling and carry
  a sha256 sidecar.
- Logs are emitted as structured fields and contain no raw private content; the tests include
  sensitive-log checks.
- A backup contains all profile data and must be stored at the same sensitivity level.

## Known gaps (recorded truthfully)

- End-to-end integration for Gmail/Calendar/Webhook has not been done (no authorized credentials).
- Remote TLS HTTP for the control plane is not implemented; local Unix socket only.
- The external blob policy for large attachments is not implemented (gmail +read returns
  inventory metadata only and attachment bytes never enter PAS - this is the current boundary,
  not a feature promise).
