# preship-kit

A portable pre-launch review pack for coding agents (Claude Code, Codex, Cursor).
Distilled from ~15 overlapping prompt packs into one deduplicated, gated
checklist that adapts to the project instead of assuming a web SaaS.

```
CLAUDE.md              the entry point — protocol, severity, scope gates
preship/
  01-security.md       secrets, auth, authz, injection, transport, abuse,
                       uploads, payments, AI, crypto, deps, logging
  02-performance.md    assets, rendering, bundles, caching, DB, resilience
  03-ux.md             feedback, states, error copy, navigation, destructive
  04-ui.md             type scale, icons, tokens, components, charts
  05-seo.md            indexability, metadata, links, submissions
  06-launch.md         launch-readiness gate and channels
  07-release.md        build, tests, config, backups, legal, monitoring
```

## Install

**Option 1 — drop-in (simplest).** Copy `CLAUDE.md` and the `preship/` folder
into the project root. Then say:

> run the pre-ship review

If the project already has a `CLAUDE.md`, rename this one to `PRESHIP.md` and
say "follow PRESHIP.md" instead. Do **not** paste the checklists into an
existing `CLAUDE.md` — that file loads on every turn, and you only want this
pack loaded when you are actually shipping.

**Option 2 — as a Claude Code skill (`/preship`).** Copy the pack to
`.claude/skills/preship/` (or `~/.claude/skills/preship/` to get it in every
project), rename `CLAUDE.md` to `SKILL.md`, and add this frontmatter at the very
top of it:

```yaml
---
name: preship
description: Run the pre-ship review — scope the project, then audit security, performance, UX, UI, and SEO against the matching checklists, report ranked findings, and fix them in severity order. Use before a release or launch.
---
```

Then type `/preship` in any project.

## How it behaves

1. **Scopes first.** Detects whether the project has a server, a database, auth,
   payments, uploads, AI calls, a public web surface — or is a local-only
   desktop/CLI/library — and loads only the matching checklists. A desktop app
   gets the secrets/injection/crypto/filesystem/privilege checks and skips every
   CORS, cookie, and RLS section.
2. **Audits before editing.** Ranked findings with file:line, plain-English
   danger, and the exact fix. `audit only` stops here.
3. **Fixes in severity order**, one change at a time, testing after each.
4. **Reports** what was fixed, deferred, and what needs a human — with a
   SHIP / SHIP WITH RISKS / DO NOT SHIP verdict.

Anything irreversible — rotating keys, rewriting git history, live migrations,
dependency swaps, production config — is on an **Ask-first list** and never
happens autonomously.

## Useful invocations

| Say | Effect |
|-----|--------|
| `run the pre-ship review` | full pass, phases 0–3 |
| `run the pre-ship review, audit only` | findings list, zero edits |
| `pre-ship: security only` | one checklist |
| `pre-ship, P0 and P1 only` | audit everything, fix only the top tiers |
