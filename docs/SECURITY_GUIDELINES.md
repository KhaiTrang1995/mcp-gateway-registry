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
  dumps, `updates`/body dicts that contain tokens, decoded id_token claims. This
  includes setup/debug scripts in `VERBOSE`/`DEBUG` modes — never echo a password
  or bearer token (not even a prefix); those logs land in CI/CloudWatch/shell
  history.
- **A hardcoded/shipped hashing or HMAC key is equivalent to an unsalted hash.**
  If a stored-credential hash (e.g. an API-key `key_hash`) is HMAC'd with a
  compile-time constant, anyone with the DB or a hash can brute-force it offline,
  and it's identical across deployments. Read the pepper from a REQUIRED
  per-deployment secret, validated fail-closed (missing/weak/short/placeholder).
  A deployment-wide pepper keeps hashes deterministic for a `UNIQUE`-column lookup
  while defeating cross-deployment/offline attack. Changing it invalidates old
  hashes — document key re-issue.
- **Write-only credentials need a separate token-free response schema.** A
  bidirectional secret stored on a config object (e.g. a `federation_token`) must
  never serialize on a GET/list response. Use a dedicated response model (or
  `SecretStr`) that omits it and exposes only `has_<secret>: bool` — not `exclude`
  bolted onto the shared read/write model, which is easy to regress.

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
- **A stricter security context must not inherit a looser guard's allowlist
  bypass.** A URL guard shared with a lower-risk path may have a trusted-domain
  allowlist that skips the IP check; on a credentialed egress path (e.g.
  federation sync attaching a bearer token) that bypass is a hole. Layer a strict
  "resolves only to public IPs" check on top for the sensitive path.
- **Keep TLS verification ON by default for privileged outbound calls.** Never
  ship `verify=False` on an admin-API client. Trust a private/self-signed cert
  via an explicit CA bundle env var (fail closed if the configured bundle is
  missing); any insecure escape hatch must be explicit opt-in, logged, default
  off.

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
- **Bind the OAuth2/OIDC authorization-code flow to the specific login.** A valid
  signature is necessary but not sufficient — a correctly-signed id_token minted
  for a different login can be replayed/injected. Send a per-login `nonce` on the
  authorization request, persist it with the flow's transient state (server-side
  store or a signed integrity-protected cookie), and after signature verification
  assert `claims["nonce"] == stored_nonce`. Add PKCE (`code_challenge=S256`;
  `code_verifier` on token exchange) and fail closed if the verifier is missing on
  callback. Route every provider branch through one verification chokepoint —
  never a userInfo fallback that skips nonce binding.
- **Authorize the exact bytes you act on — never a separately-captured copy.** If
  one component captures a request body for the authz decision and another
  forwards a different copy to the sink, they can diverge (size-triggered
  spill-to-file where the capture is empty, whitespace/newline normalization,
  content-length games) and the sink executes what the authorizer never saw.
  Re-run the scope/tool check inline on the forwarded bytes immediately before the
  outbound call; when the scope-relevant body can't be captured/parsed, FAIL
  CLOSED — do not default to an unprivileged method.
- **Redaction and access checks on reads must be uniform across the whole entity
  family.** The same internal fields (backend URLs, authz config) leak through the
  read sibling that forgot the guard — `/versions`, bulk `/all`, discovery
  projections that re-project the field under a new name, search-result shaping,
  admin-config reads. Use one shared redaction-decision helper + field-stripper;
  gate authz-model reads behind the same admin check as their writes; for an
  unauthenticated public surface fail closed to the derived URL and never emit a
  stored internal override.

## Frontend (React) URL / href handling

- **One shared URL-scheme guard, applied to every dynamic href / `window.open` /
  markdown link.** React does not block `javascript:` (or `data:`/`vbscript:`) in
  `href`. Any dynamic URL rendered from server-registration or federation data is
  a stored-XSS vector that runs in the viewer's authenticated session on click.
  Allowlist `http`/`https`/`mailto`; strip control chars + whitespace before
  scheme extraction and lowercase it (defeats `Java\tScript:`, leading-space, NUL,
  mixed case); render unsafe schemes as inert text. Enforce with the
  `react/jsx-no-script-url` lint rule so regressions are caught. (Server-side
  template `innerHTML`/inline-`onclick` is a separate sink — guard it too.)

## Deployment surface & insecure-by-default config

- **Publish sensitive ports on loopback, not `0.0.0.0`.** In compose/dev stacks,
  bind datastores, vaults, IdP admin ports, and backend services to `127.0.0.1:`;
  only the intended front door (nginx 80/443) listens on all interfaces.
- **Never ship a working default for a required secret.** Use `${VAR:?message}`
  so the stack fails fast, and reject known-weak literals in a preflight
  validator. An active `PASSWORD=admin` in `.env.example` (vs a commented
  placeholder) seeds real deployments with the weak value.
- **No secrets as Docker build `ARG`s** — they persist in image history. Inject at
  runtime.
- **Don't bind-mount broad credential dirs** (e.g. `~/.aws`) into a
  network-facing container by default; mount the single needed file or use a
  scoped role.
- **Pin image tags; never `:latest`.** And raise dependency floors above known
  CVEs even when a lockfile currently mitigates — off-lock installs are exposed;
  prefer consolidating on one library over carrying a redundant vulnerable one.
- **A dangerous operational toggle** (disabling TLS, wiping data) must require an
  explicit acknowledgement flag AND a fail-closed environment guard (e.g.
  localhost-only). Setup scripts must force credential rotation (`temporary:
  true`) and never grant privileged scopes to anonymous dynamic client
  registration.

## Canonical helpers — use these, never reinvent or copy-paste

There is exactly ONE blessed implementation per concern. Before writing a new
outbound HTTP call, auth gate, redaction, or secret check, use the helper below.
A hand-rolled or copy-pasted variant is how these vulnerabilities get reopened —
the drift between copies is the hole.

- **Outbound HTTP from any user/registry/federation-controlled URL →
  `registry/utils/url_guard.py`.** Never construct a bare `httpx.Client` /
  `httpx.AsyncClient` (or a third-party SDK client) for such fetches. Use
  `guarded_client(profile=...)` / `guarded_async_client(profile=...)` — they
  validate + pin the resolved public IP inside the transport (rebinding-safe,
  re-validated per redirect). Pick the profile: `SKILL_PROFILE` (skill/doc
  fetches) or `PROXY_PROFILE` (server/agent proxy targets). Validate at
  registration with `validate_proxy_pass_url()` / `validate_agent_url()` /
  `validate_server_path()`; reject nginx metacharacters with
  `contains_nginx_metacharacters()`. Internal targets are opt-in via
  `SSRF_ALLOWED_HOSTS` / `SSRF_ALLOWED_CIDRS`, default deny. (Legacy
  `ard_net_guard.py` predates this — prefer `url_guard`; do not add new callers
  to ad-hoc `_is_safe_url` variants.)
- **State-changing endpoint CSRF → `registry/auth/csrf.py`.** Add
  `Depends(verify_csrf_token_flexible)` (or `verify_csrf_token_header_only`) to
  every mutating route. Don't invent per-router CSRF logic; match the dependency
  every other router uses.
- **Internal service-to-service auth → `registry/auth/internal.py`.**
  `generate_internal_token()` to mint, `validate_internal_auth` /
  `validate_internal_session_secret` as the route dependency. Internal tokens are
  a distinct trust class from user tokens — do not hand-roll JWT checks that
  accept both.
- **Signing-secret validation → `registry/common/secret_key.py`**
  (`validate_secret_key` / `validate_signing_secret`). Any new secret that signs
  or seals must go through it (fail-closed: missing/weak/short). *(Arrives with
  the secret-key-hardening change; until merged, that module lives on that
  branch.)*
- **Backend-URL / authz-config redaction on reads →
  `registry/services/visibility.py`.** Use the shared access check
  (`user_can_access_*_from_doc`) and the shared redaction decision + field
  stripper for every read that serializes a server/agent entity — not a
  per-endpoint reimplementation. *(The redaction helpers arrive with the
  readonly-info-disclosure change; the access helpers are already on main.)*
- **Frontend dynamic URLs → the shared `isSafeUrl` / `SafeLink`
  (`frontend/src/utils/safeUrl.ts`, `frontend/src/components/SafeLink.tsx`).**
  Never bind a server/federation-supplied value straight into `href` /
  `window.open` / a markdown link. *(Arrives with the frontend-xss-hrefs change.)*
- **Privileged outbound TLS →** trust private certs via an explicit CA-bundle env
  var, never `verify=False`. `guarded_client`/`guarded_async_client` take
  `verify=` and default it to `True`.

When you add a NEW canonical helper, list it here so the next agent reaches for
it instead of rebuilding it.

## Cross-cutting habit

**When you fix a finding, grep for the same pattern repo-wide before closing.**
Almost every finding has siblings the report didn't list — extra fetch sinks,
extra unowned endpoints, extra log sites. The root-cause fix plus a repo-wide
sweep beats patching the single reported line.
