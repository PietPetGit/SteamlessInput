# 07 — Release readiness

**Gate:** always, scaled to the project. A weekend CLI tool needs §A and §E; a
product holding user data needs all of it. Each sub-section carries its own gate.

This is the "can we actually press the button" pass — the operational state
around the code rather than the code itself. Several items here are **Needs
you** by nature: verify and report, do not fabricate an account or a policy.

---

## A. Build & tests
**Gate:** always.

- **P0 — Builds from clean.** The release artefact builds from a fresh clone or
  clean checkout, with no uncommitted local file, no manual step performed once
  months ago and forgotten, and no dependency resolved from a local cache that
  is not in the lockfile. *Verify:* actually run it clean, do not assume.
- **P0 — The suite passes.** Run it. Report the real result. A failing test is
  a finding, not something to work around — and never weaken an assertion to get
  green (see working rule 7 in `CLAUDE.md`).
- **P1 — No silently skipped tests.** Check for `skip`, `xfail`, `only`,
  commented-out suites, and tests excluded by config. Each one is either
  restored or reported with the reason it is disabled.
- **P1 — Critical paths are covered.** Signup, login, payment, and the primary
  user task have at least one test that would fail if they broke. Coverage
  percentage is not the metric; those flows existing is.
- **P2 — CI runs what you ran.** The pipeline builds and tests the same thing
  locally-run commands do, and is currently green on the branch being shipped.
- **P2 — Version and changelog** bumped, with the release notes written.

## B. Configuration & environments
**Gate:** the project has more than one environment, or any runtime config.

- **P0 — No dev defaults in production.** Debug mode off, verbose stack traces
  off, seed/demo data absent, test accounts and default passwords removed,
  developer backdoors and feature bypasses gone. *Verify:* grep for debug flags
  and check what they default to when the env var is unset.
- **P0 — Every required env var is documented and present.** The app fails fast
  and loudly at startup on a missing critical variable rather than running in a
  degraded or insecure state. `.env.example` lists every key (names only —
  values live in `01-security.md` §A).
- **P1 — Pointed at the right things.** Production config references production
  database, storage, queue, and third-party keys — not staging, not a personal
  sandbox. Test/live keys for payment and email providers are not mixed.
- **P1 — Staging resembles production** closely enough that passing there means
  something.
- **P2 — Feature flags** default to a safe state, and anything half-finished is
  behind one and off.

## C. Data, backups & migrations
**Gate:** the project stores persistent data.

- **P0 — A backup exists *and* has been restored.** An untested backup is not a
  backup. Confirm the schedule, the retention window, where it lives (not only
  on the same host as the database), and that a restore has been performed at
  least once. Report the date of the last successful restore test.
- **P0 — Migrations are reversible or forward-safe.** Every pending migration
  has a rollback path, or is written so an older app version can still run
  against the new schema. Destructive migrations (drop column, drop table,
  type narrowing) are called out explicitly — those are Ask-first.
- **P1 — Migrations tested against a copy of real data**, not an empty dev
  database, and the runtime is known (a table lock on a large table is an
  outage).
- **P1 — Data deletion works.** If users can delete their account or data, the
  deletion actually removes it — including from backups policy, caches, search
  indexes, logs, and third-party processors — or the retention is documented.

## D. Legal & compliance
**Gate:** the project is public, collects personal data, or takes money.

- **P1 — Privacy policy and terms** exist, are reachable from the product, and
  describe what is actually collected — including analytics, error tracking, and
  anything sent to an AI provider (`01-security.md` §K).
- **P1 — Consent where required.** Cookie/tracking consent in the regions you
  serve, with analytics genuinely gated behind it rather than firing regardless.
- **P1 — Data-subject rights** have a route: export, correction, deletion, and a
  contact address that a human reads.
- **P2 — Dependency licences** are compatible with how this project is
  distributed. Check for copyleft obligations if shipping a binary or a hosted
  service, and confirm required attribution notices are included.
- **P2 — Your own licence** is present, correct, and consistent between the
  `LICENSE` file, package metadata, and any headers.

## E. Monitoring & operations
**Gate:** the project runs somewhere you cannot attach a debugger to.

- **P0 — Errors are captured somewhere you will look.** Crash and exception
  reporting is wired up and *proven* to work — trigger a test error and confirm
  it arrives. Silent failure in production is the default state otherwise.
- **P1 — Alerts on what matters** — error-rate spikes, auth anomalies, payment
  and webhook failures, spend anomalies, and the app being down. Routed to
  something that interrupts a person, not a dashboard nobody opens. (Security
  event logging itself is `01-security.md` §N — this is the alerting on top.)
- **P1 — A health check** the platform can poll, that fails when a critical
  dependency is unreachable rather than always returning 200.
- **P1 — Rollback plan.** You know the exact command or button that reverts the
  release, roughly how long it takes, and what it does to in-flight data.
  Write it down before shipping, not during the incident.
- **P2 — Log level and volume** sane in production — enough to investigate, not
  so much it costs money or buries the signal.
- **P2 — A way to reach users** if something goes wrong: status page, email
  list, or an in-app banner.

---

## Release verdict inputs

Report these as explicit lines in the final report — each one is either verified
or "not verified", never assumed:

```
Clean build:      pass / fail / not verified
Test suite:       n passed, n failed, n skipped
Backup restored:  <date> / never / n/a
Rollback plan:    <one line> / none
Error reporting:  confirmed receiving / not confirmed / n/a
Blocking config:  <anything pointed at the wrong environment>
```
