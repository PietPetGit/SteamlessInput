# 01 — Security

Audit-first. Produce the ranked findings list before changing a line. Rank
most-to-least dangerous; for each finding give file:line, the plain-English
danger, and the exact fix.

Every section carries a **Gate**. If the gate does not match this project, skip
the whole section and say so in the scope table.

Sections **A** (secrets), **E** in part (command injection, input validation,
path traversal), **L** (crypto), **M** (dependencies) and **N** (logging) apply
to almost everything — including offline desktop apps, CLI tools, and libraries.
Everything else here assumes a server, a browser, or a hosted database; for a
local-only project, use the desktop/CLI list in `CLAUDE.md` as the authoritative
subset — it adds the checks with no web equivalent (filesystem confinement,
privilege scope, update and distribution integrity).

---

## A. Secrets & credentials
**Gate:** always.

- **P0 — Hardcoded secrets.** Scan the *entire* tree for API keys, tokens,
  passwords, database URLs, private keys, and connection strings in plain text.
  Include client-side bundles, mobile apps, config files, notebooks, test
  fixtures, CI configs, and Docker files. Move each to an environment variable,
  list the `.env` entries needed, and update the code to read from env.
  *Verify:* grep the built/bundled output too — a secret imported into client
  code ships to every visitor.
- **P0 — Secrets in git history.** Anything ever committed must be treated as
  compromised even if since deleted. Identify what was exposed, in which commits,
  and produce the **rotation list**. Purging history and rotating keys are
  Ask-first actions — prepare the commands, do not run them.
- **P1 — `.gitignore` hygiene.** `.env*`, credential files, local DB files, key
  material, and build artefacts containing secrets are ignored. A committed
  `.env.example` holds names only, never values.
- **P1 — Right key in the right place.** Client code uses the public/anon key
  only. Service-role, admin, and server-only keys never appear in anything
  shipped to a browser or a user's machine.
- **P2 — Runtime secret handling.** Secrets are not logged, not included in
  error responses or crash reports, and not echoed into debug output.

## B. Authentication
**Gate:** the project has user accounts or a login.

- **P0 — Password storage.** Passwords are hashed with a slow, salted KDF
  (bcrypt, scrypt, Argon2). Never MD5/SHA1/SHA256-alone, never encrypted,
  never reversible.
- **P1 — Password policy.** Minimum length 12, and compromised passwords
  rejected via the provider's breached-password check (HaveIBeenPwned or the
  built-in equivalent). Surface a clear strength error in the sign-up UI.
  *Verify:* a known-breached password is rejected.
- **P1 — Email verification.** Required, and unverified accounts are blocked
  from writes and sensitive actions by a **server-side** guard, with a clean
  "please verify" redirect.
- **P1 — Brute-force protection.** Rate limiting *and* temporary lockout on
  login, signup, and password reset — e.g. 5 attempts / 15 min per IP+email,
  with a defined lockout duration. *Verify:* the 6th attempt is blocked.
- **P1 — User enumeration.** Login, signup, reset, and "email in use" responses
  are identical for existing and non-existing accounts — same message, same
  status code, comparable timing.
- **P1 — Reset links.** Single-use, short expiry, invalidated on use and on
  password change, high-entropy tokens, never logged or sent in a URL that
  leaks via referrer.
- **P1 — Credential-change side effects.** Changing a password (or email, or
  disabling MFA) invalidates every other active session and refresh token.
- **P2 — Bot protection** on signup and other abusable public forms.

## C. Sessions & cookies
**Gate:** the project has sessions or auth tokens.

- **P0 — No tokens in `localStorage`/`sessionStorage`.** Session tokens live in
  `httpOnly`, `Secure`, `SameSite=Lax` (or `Strict`) cookies. *Verify:* nothing
  auth-related is readable from `window.localStorage` in DevTools.
- **P1 — Cookie flags** set on every auth/session cookie: `httpOnly`, `Secure`,
  `SameSite`, a scoped `Path`, and no over-broad `Domain`.
- **P1 — Session lifetime.** Both an **idle timeout** and an **absolute maximum
  lifetime**, both enforced server-side, both actually invalidating the session
  server-side rather than just clearing a client cookie.
- **P1 — Logout** revokes server-side and clears cookies; a stolen token from
  before logout no longer works.
- **P1 — CSRF protection on every mutation.** Any request that creates, updates,
  or deletes needs protection appropriate to the architecture — synchronizer
  tokens validated server-side for form/cookie-based apps, paired with the
  `SameSite` attribute. Cover *every* mutating endpoint, and do not needlessly
  block safe read-only requests. List the endpoints secured and the mechanism.
  (Pure bearer-token APIs with no cookie auth are not CSRF-exposed — say so
  rather than adding tokens for nothing.)

## D. Authorization
**Gate:** the project has a server, an API, or multi-user data.

- **P0 — Server-side enforcement.** Every endpoint, route handler, and server
  action independently re-verifies identity **and** role from the server session.
  Hiding a button or guarding a client route is not authorization. Enumerate
  *every* route and report the unprotected ones as a complete list.
  *Verify:* call the endpoint directly with a logged-out client and with a
  wrong-role user.
- **P0 — Object-level access (IDOR).** Any request carrying a record id checks
  ownership/membership before reading or writing. Never trust an id from the
  client to imply the right to it.
- **P0 — Row-level security.** If the database supports RLS (e.g. Supabase),
  it is *enabled* on every table with a policy per operation, and the policies
  are tested — not left permissive because "the app checks it".
- **P1 — Mass assignment / field tampering.** Writes accept an explicit
  allowlist of fields. A client cannot set `role`, `is_admin`, `credits`,
  `price`, `user_id`, `verified`, or any other privileged column by adding it to
  the payload.
- **P1 — Admin surfaces.** Default admin routes and dashboards from templates
  or frameworks are removed or moved, and are role-gated server-side.
- **P2 — Database least privilege.** The app's DB user has only the permissions
  it needs — no superuser, no DDL in production, no access to unrelated schemas.

## E. Input handling & injection
**Gate:** always (the sub-items each have their own trigger).

- **P0 — SQL injection.** Every query is parameterized. No string concatenation
  or interpolation of user input into SQL — including inside ORM raw fragments,
  `WHERE` builders, and dynamic conditions.
- **P0 — ORM escape hatches.** Raw-query/raw-fragment/`unsafe` APIs fed user
  input are replaced with parameterized equivalents. User input that chooses a
  **column name, sort field, direction, or operator** is validated against an
  allowlist — those cannot be parameterized.
- **P0 — OS command injection.** Find every place the app runs a shell command
  or external process. Avoid the shell entirely; pass arguments as an array, not
  a concatenated string; strictly validate anything user-influenced. Refactor
  away shelling out where it is not truly needed. Report every execution site.
- **P0 — NoSQL operator injection.** User input is validated and cast to the
  expected type so an attacker cannot smuggle in query operators or expressions
  (turning a value into an object that always matches). Reject unexpected
  structures rather than merging them into the query.
- **P1 — Server-side template injection.** Untrusted input is always supplied as
  *data* to a static template, never concatenated into template source or passed
  anywhere the engine will evaluate it. Auto-escaping is on.
- **P1 — Validate everything at the boundary.** Every external input — body,
  query, params, headers, webhooks, uploaded file contents, imported data — is
  validated against an explicit schema: type, length, range, format, allowed
  values. Validate on the server; client validation is UX only.
- **P1 — Sanitize before storing.** Content is validated and normalised on the
  way in, not only on the way out, so bad data never reaches the database.
- **P2 — Regex denial of service.** Regexes running against user input with
  nested or overlapping repetition can backtrack catastrophically. Rewrite to
  linear-time patterns or non-regex parsing, cap input length before matching,
  and set a match timeout where supported.
- **P1 — Server-side request forgery.** Audit every feature where the server
  fetches a URL derived from user input — webhooks, link previews, importers,
  image fetchers, PDF/screenshot renderers. Restrict the target so it cannot
  reach internal addresses, loopback, link-local, or cloud metadata endpoints;
  use a host allowlist where possible; block redirects to disallowed targets.
  The checks must survive DNS rebinding and redirect chains — resolve then
  validate, and re-validate after every hop. Report each fetch secured.
- **P2 — Path traversal.** Any user-supplied filename or path is normalised and
  confined to an intended directory; `..`, absolute paths, and symlinks cannot
  escape it.

## F. Output & data exposure
**Gate:** the project renders user-supplied content, or returns API responses.

- **P0 — Stored XSS.** Trace every path where user-submitted content is saved
  and later shown to *other* users. Sanitize on store, encode consistently on
  render. Check the unobvious surfaces: display names, file names, notification
  text, error messages echoing input, CSV/PDF exports, and admin views.
- **P0 — Raw-HTML bypasses.** Audit every `dangerouslySetInnerHTML`, `v-html`,
  `bypassSecurityTrust*`, `innerHTML`, `@html`, or equivalent. For each, decide
  whether untrusted input can reach it; remove the bypass or run a strict
  sanitizer first. Document every occurrence and how it was handled.
- **P1 — Over-exposed fields.** Endpoints return an explicit output shape, not
  whole serialized DB records. Strip password hashes, tokens, internal flags,
  soft-delete columns, other users' data, and unnecessary nested relations.
  Pay particular attention to user/account objects and embedded relations.
- **P2 — Error responses** return a generic message and an id; stack traces,
  SQL, file paths, and framework internals never reach the client in production.

## G. Transport & HTTP headers
**Gate:** the project serves HTTP.

- **P0 — HTTPS everywhere.** HTTP redirects to HTTPS; no mixed content; no
  plaintext API calls from the client.
- **P1 — HSTS** with a sensible `max-age` and `includeSubDomains`, once HTTPS is
  known-good on every subdomain.
- **P1 — CORS.** No wildcard origin — and never a wildcard combined with
  credentials. Explicit allowlist; only the methods and headers actually needed;
  never reflect the incoming `Origin` header back without validating it against
  the allowlist. Credentials permitted only for trusted origins.
- **P1 — Content Security Policy.** Restrict script/style/resource sources to
  trusted origins, eliminate or tightly control inline scripts (nonce or hash),
  set `frame-ancestors`. Roll out in report-only mode to find violations, then
  enforce. Explain each directive chosen.
- **P2 — Security headers present:** `X-Content-Type-Options: nosniff`,
  `Referrer-Policy`, `Permissions-Policy`, frame protection, and a same-origin
  `Cross-Origin-Opener-Policy` where applicable.
- **P2 — Remove revealing headers.** Strip or genericize `Server`,
  `X-Powered-By`, framework identifiers, and version numbers, at the app or
  server layer so it covers every response. Give a before/after of the headers.
- **P2 — Directory listing disabled**, and source/config/backup files
  (`.git/`, `.env`, `*.bak`, source maps in production) are not fetchable.

## H. Abuse & resource limits
**Gate:** the project accepts requests, or processes user-supplied work.

- **P1 — Request body & payload caps.** Maximum body size at the server or
  framework level, per-file and per-field upload caps, maximum array lengths,
  and maximum JSON nesting depth, so one payload cannot exhaust memory or CPU.
  Return a clear error on breach. State the limits and where they are enforced.
- **P1 — Rate limiting** on every expensive, abusable, or auth-adjacent
  endpoint — not just login: search, export, email sending, AI calls, uploads,
  webhooks, and anything unauthenticated.
- **P2 — Pagination caps.** No endpoint returns an unbounded list; `limit` is
  clamped to a maximum server-side.

## I. File uploads
**Gate:** the project accepts uploads.

- **P0 — Type allowlist** validated by actual content sniffing, not just the
  extension or the client-supplied MIME type.
- **P0 — Never executable.** Uploads are stored outside the web root or in
  object storage, served with a fixed safe content type and
  `Content-Disposition`, and never executed or included by the server.
- **P1 — Size caps** per file and per request; storage quota per user.
- **P1 — Randomized stored names**; the original filename is treated as
  untrusted text (escape on display, never use it as a path).

## J. Payments & third-party webhooks
**Gate:** the project takes money, or receives webhooks.

- **P0 — Prices are server-side.** Amounts, currencies, discounts, quantities,
  and plan entitlements are computed and enforced on the server from trusted
  data. A client-supplied price or plan id is never trusted.
- **P0 — Verify webhook signatures.** Validate the provider's signature against
  the **raw, unparsed** request body with the shared secret and a constant-time
  comparison; reject anything that fails. Applies to payment, auth, email, and
  every other inbound webhook.
- **P1 — Replay protection** on webhooks via timestamp tolerance and/or stored
  event ids, and handlers made idempotent so a duplicate delivery cannot
  double-grant or double-charge.
- **P1 — Entitlement source of truth** is the provider's event/subscription
  state, re-fetched or verified — not a client claim that a purchase happened.

## K. AI / LLM features
**Gate:** the project calls a model provider.

- **P1 — Prompt injection defence.** Untrusted content (user input, fetched
  pages, documents, tool output) is delivered as *data*, never as instructions.
  Tools available to the model are allowlisted and least-privilege; model output
  is validated before it drives an action, a query, a command, or a render.
  Treat model output as untrusted input for every other section of this file.
- **P1 — Usage caps.** Per-user and global spend/token/request limits with a
  hard stop, plus rate limiting on model-backed endpoints, so a single actor
  cannot run up the bill. Alert on abnormal spend.
- **P2 — Data handling.** Be deliberate about what user data leaves for the
  provider; keep secrets and other users' data out of prompts and context.

## L. Cryptography & data at rest
**Gate:** the project hashes, encrypts, signs, or stores sensitive data.

- **P0 — No weak primitives.** Replace MD5, SHA1, DES/3DES, RC4, ECB mode,
  hardcoded keys or IVs, predictable/`Math.random` IVs and tokens, and any
  homegrown crypto with a vetted algorithm from a standard library. Report every
  weak primitive found and its replacement.
- **P1 — Randomness.** Tokens, ids, and IVs come from a CSPRNG, never a
  general-purpose RNG.
- **P1 — Sensitive data encrypted** at rest where warranted (PII, tokens, keys),
  with keys held outside the codebase and rotatable.

## M. Dependencies & supply chain
**Gate:** always.

- **P1 — Vulnerability scan.** Run the ecosystem's audit tool; fix or explicitly
  accept every high/critical finding. Dependency upgrades are Ask-first.
- **P2 — Lockfile committed**, versions pinned, and unused dependencies removed.
- **P2 — Provenance.** No packages installed from unpinned URLs, forks, or
  typo-adjacent names; postinstall scripts from unfamiliar packages reviewed.

## N. Logging & monitoring
**Gate:** always (scaled to project size).

- **P1 — Security events logged:** login success/failure, lockouts, password and
  email changes, permission denials, admin actions, payment events, and webhook
  failures — with timestamp, actor, and source, and retained long enough to
  investigate.
- **P1 — No secrets in logs.** Tokens, passwords, keys, full card data, and
  personal data are redacted from logs, crash dumps, and error trackers.
- **P2 — Alerting** exists for the failures that matter (auth spikes, error-rate
  spikes, payment/webhook failures, spend anomalies).

---

## Final security sweep

After the fixes, do one adversarial read as an attacker, not a reviewer: pick
the three most valuable things in the system (money, other users' data, admin
access) and describe how you would reach each one from an anonymous or
low-privilege position. Anything that works is a P0 regardless of which section
it belongs to.
