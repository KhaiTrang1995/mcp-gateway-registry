# Security Guidelines

Security invariants and lessons for this codebase, distilled from remediating
security-review findings. **Read this before touching authentication,
authorization, outbound requests, credential handling, config generation, or
logging.** CLAUDE.md carries a compact always-loaded checklist; this doc is the
detailed, categorized reference behind it.

## How to use / extend this doc

- These are enforceable rules, not suggestions. Code review should reject changes
  that violate them.
- **When you find or fix a NEW category of security issue, add a lesson here**
  (one entry per pattern, not per individual finding): the mistake, why it
  happened, and the rule that prevents recurrence. Keep the always-loaded
  checklist in CLAUDE.md in sync when a new category is added.
- Prefer a single shared, hardened utility per concern (URL validation, secret
  validation, privileged-scope checks) over per-call-site reimplementations —
  the drift between copies is where the gaps live.

## Fail-closed principle

On any error, missing config, or ambiguous state, DENY. Never fall back to an
allow, a permissive default, or a "temporary" bypass. A check that can be
silently skipped (optional param, truthiness on a value that can be empty,
sanitizer that isn't called) is equivalent to no check.

## Secrets & signing keys

- **Validate a shared signing secret for MISSING *and* WEAK, at every entrypoint
  that signs.** Presence checks are insufficient (a long shipped placeholder
  passes a length check). Reject unset/empty/whitespace-only; reject `< 32` chars
  on the *stripped* value; reject a denylist of known-weak literals (e.g. the
  `.env.example` default, `development-secret-key`, `changeme`). Run the
  weak-value check BEFORE the length check so a known placeholder produces the
  right error. Normalize (strip) and reuse the same value everywhere so all
  services derive an identical key (avoids cross-replica signature mismatches).
  Wire the validator into EVERY signing entrypoint — grep for all of them.
- **Never ship a vendor/default credential fallback in code.**
  `os.environ.get("PASS", "<default>")` is a silent foot-gun. Use one chokepoint
  that raises when unset, and denylist the known-weak value so an env var
  explicitly set to it is also rejected (fail closed even if compose passes
  `${PASS:-weak}`). A dev container's own provisioned password is deployment
  config; the application code must never supply it as a default.
- **Example credentials in help text/docs must be unmistakably fake** (`YOUR_*`,
  `EXAMPLE_*`). Do not alter functional lookup keys that merely resemble IDs.
- **Never log secrets, tokens, PII, or full credential/claim payloads.** Redact
  before logging; log identifiers/counts, not values. Watch: request-header
  dumps, `updates`/body dicts that contain tokens, decoded id_token claims.

## SSRF & outbound requests from user/registry-controlled input

- **One hardened URL guard, used by every outbound fetch.** Block RFC-1918,
  loopback, link-local, reserved, multicast, unspecified, and cloud-metadata
  (`169.254.169.254` — never allowlistable); unwrap IPv4-mapped IPv6; require
  http/https. Fail closed. Do not create per-call-site `_is_safe_url` variants.
- **Validate at registration (structural) AND pin the resolved IP at fetch
  time.** A pre-fetch check followed by a separate client call re-resolves DNS =
  TOCTOU / DNS-rebinding. Pin the validated public IP into the transport
  (preserve Host header + TLS SNI) and re-validate on every redirect hop.
- **Never build or attach credentials for a target that fails validation.**
  Validate the URL before decrypting/attaching stored credentials, so a
  malicious registered URL cannot exfiltrate them.
- **Every fetch path uses the guarded client** — grep for raw `httpx`/SDK clients
  and third-party SDKs that own their own client. Internal targets are opt-in via
  an explicit allowlist (`SSRF_ALLOWED_HOSTS`/`SSRF_ALLOWED_CIDRS`), default deny.

## Injection (nginx config generation, NoSQL/regex)

- **Sanitize at EVERY interpolation site AND validate at the source.** A
  sanitizer that exists but isn't called is worthless. Apply the nginx-value
  sanitizer to every user/registry value entering a generated directive
  (`proxy_pass_url`, backend, host, path), and reject metacharacters + non-http(s)
  schemes at registration.
- **Escape user input used in `$regex`/regex-match queries** (`re.escape`), and
  never fall back to a raw user string when tokenization yields nothing.

## Authorization & ownership

- **Deny by default; never treat a broad or execute scope as admin.** A helper
  meant for "can edit my own resource" must not gate admin-only operations.
  Require explicit `is_admin` or a named admin group/scope from centralized
  privileged constants.
- **Enforce ownership server-side before EVERY mutation — across the whole
  endpoint family, not just the reported one.** If register-overwrite needs an
  ownership check, so do the version, rename, auth-credential, and delete
  siblings. Add CSRF (or non-cookie auth) to all state-changing endpoints.
- **`getattr(a_dict, "key", None)` always returns None** (dicts don't expose keys
  as attributes) — the guard becomes dead code that never denies. Use
  `dict.get("key")`; watch for dict-vs-Pydantic-model confusion.
- **No substring matching for privilege decisions** (`"unrestricted" in scope`
  accepted access scopes as admin). Match exact, centralized constants.
- **Attach a shared/global credential only on explicit opt-in.** Make the
  privileged code path default to not attaching it and gate its use behind an
  admin check.
- **Verify externally-supplied JWTs** (signature/issuer/audience/expiry) against
  the IdP JWKS before trusting any claim. Never `verify_signature=False` on a
  token whose claims drive identity or authorization.

## Cross-cutting habit

**When you fix a finding, grep for the same pattern repo-wide before closing.**
Almost every finding has siblings the report didn't list — extra fetch sinks,
extra unowned endpoints, extra log sites. The root-cause fix plus a repo-wide
sweep beats patching the single reported line.
