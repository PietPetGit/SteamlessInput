# 02 — Performance

Measure before, measure after. Every item below is only "done" when there is a
before/after number: bundle size, request count, query count, p95 latency, CLS,
memory, startup time. No number, no claim.

Fix what is actually slow. Do not add caches, memoization, or indexes
speculatively — profile first, then target the hotspots.

Each section has a **Gate**. Skip non-matching sections and say so.

---

## A. Assets & media
**Gate:** the project ships images, fonts, or icons to a client.

- **Optimize images.** Convert raw `<img>` to the framework's optimized
  component (`next/image` or equivalent), serve WebP/AVIF, size them for their
  real display size, and lazy-load anything below the fold. Report each image
  changed with before/after file size.
- **Set intrinsic dimensions.** Every image and embed has explicit
  `width`/`height` or an `aspect-ratio` so space is reserved before it loads —
  including responsive images. This is the single biggest layout-shift source.
- **Inline tiny critical SVGs.** Small icons above the fold that cost a separate
  round trip get inlined into the markup or a sprite. Keep large or rarely-used
  graphics external — inlining a big SVG just bloats the HTML.
- **Font loading.** Use a `font-display` strategy that avoids reflow, preload
  the critical face, and match fallback metrics so swapping does not move text.

## B. Rendering & perceived speed
**Gate:** the project has a UI.

- **Cumulative Layout Shift.** Find every source of post-render movement:
  unsized media, late fonts, injected banners, and content inserted *above*
  existing content. Reserve space with correctly-sized skeletons; never push
  existing content down. *Verify:* CLS lands in the good range and the page is
  visually stable through load.
- **Critical rendering path.** Inline or prioritize the CSS needed above the
  fold, defer non-critical CSS and JS, add `preconnect`/`dns-prefetch` for
  critical third-party origins, and remove render-blocking requests. Report the
  loading-sequence change and its effect on first meaningful paint.
- **Unnecessary re-renders.** Profile for components updating when their data
  has not changed — new object/array/function references each render, overly
  broad state or context updates, missing memoization. Fix the *measured*
  hotspots; do not memoize everything, which costs more than it saves.
- **Skeletons and optimistic UI** are covered in `03-ux.md` — they are perceived
  performance, and the checks live there. Do not duplicate them here.

## C. Bundles & code delivery
**Gate:** the project ships a JS bundle.

- **Analyze the bundle** and identify the largest contributors before changing
  anything.
- **Route-based code splitting** so each page loads only its own code, with the
  initial bundle limited to the first view's needs. Add suspense/loading
  boundaries around lazy routes.
- **Component-level splitting** for heavy widgets (editors, charts, maps, video)
  and anything not needed for first paint.
- **Drop or replace heavy dependencies** where a lighter option or a small
  amount of local code does the job (dependency swaps are Ask-first).
  *Verify:* report initial bundle size before and after.

## D. Network & transport
**Gate:** the project serves HTTP responses.

- **Compression in transit.** gzip or brotli for JSON and text responses above a
  small size threshold, negotiated via `Accept-Encoding`. Do not double-compress
  already-compressed payloads. *Verify:* transfer sizes drop and responses still
  parse.
- **Client request cache.** Find screens that refetch identical data on revisit
  or back-navigation. Introduce a client cache (or a data-fetching library that
  provides one, e.g. TanStack Query) keyed by request parameters with sensible
  `staleTime` and invalidation, replacing manual `useEffect` + `useState`
  fetching. Mutations must invalidate the affected entries.
  *Verify:* revisiting a screen issues no redundant request.
- **Request waterfalls.** Sequential dependent requests on a critical path are
  parallelized or collapsed into one endpoint.

## E. Caching (server-side)
**Gate:** the project has a server or expensive computed output.

- **In-memory cache layer** for frequently-read, slow-to-fetch data (Redis, or
  an in-process cache for a single instance). Explicit keys, TTLs matched to
  volatility, a safe miss-and-populate path, and a fallback to the source on
  cache failure. Mind consistency across multiple instances.
- **Cache hot read queries** that run constantly with few distinct parameters
  over slow-changing data. Key on the parameters, invalidate on relevant writes,
  and make misses safe under concurrency (no stampede).
  *Verify:* query volume for those reads drops sharply.
- **Cache rendered pages or fragments** whose output is identical across many
  users and changes infrequently. Regenerate on a schedule or on content change,
  keep personalized regions dynamic via holes or client hydration, and include
  meaningful variations (locale, theme) in the cache key.
- **Parse once, not per request.** Templates, config, schema definitions, regex
  compilation, and locale data are compiled at startup and reused, reloading
  only when the source changes. *Verify:* per-request work drops, output
  unchanged.

## F. Database
**Gate:** the project talks to a database.

- **Indexes.** For every column filtered, sorted, joined, or used in a foreign
  key, confirm an index exists. Produce the exact `CREATE INDEX` statements
  (applying them to a live DB is Ask-first) and confirm usage with `EXPLAIN` /
  `EXPLAIN ANALYZE`.
- **Pagination.** Any query that can return an unbounded list gets limit/offset
  or cursor pagination, with a server-side maximum.
- **N+1 queries.** Find loops issuing one query per item; replace with a join,
  an `IN` batch, or the ORM's eager-loading. Count queries per request before
  and after.
- **Batch writes.** Many individual `INSERT`/`UPDATE` statements in a loop
  become a bulk/multi-row operation inside one transaction, chunked so batches
  do not become oversized statements or long locks.
- **Connection pooling.** Confirm the app reuses a pool instead of opening a
  connection per request. Size the pool to the database's capacity and the app's
  concurrency; in serverless or high-fan-out setups, use a dedicated pooler.
  *Verify:* connection churn drops and the DB is not saturated by connections.
- **Select only what you need** — no `SELECT *` on wide tables in hot paths.

## G. Backend workload & resilience
**Gate:** the project has a server or background processing.

- **Offload slow work.** Email sending, image/file processing, export
  generation, and slow third-party calls move out of the request path into a
  background job queue, with status tracking or notification. Jobs must be
  retryable and idempotent. *Verify:* user-facing requests return fast and the
  deferred work completes reliably.
- **Circuit breakers and timeouts.** Every external dependency has a timeout,
  bounded concurrency, and a breaker that trips when it is failing or slow —
  fast-failing or serving a fallback until it recovers. *Verify:* a degraded
  dependency no longer drags down unrelated parts of the app, and recovery is
  clean.
- **No unbounded work per request** — no full-table scans, no unbounded loops
  over user-controlled counts (see request caps in `01-security.md` §H).

## H. Local & desktop apps
**Gate:** the project is a desktop app, CLI, daemon, or driver.

- **Startup time** measured and attributed — lazy-import heavy modules, defer
  non-essential initialization until after the first frame or first output.
- **Idle cost.** Measure idle CPU and wakeups. Poll loops get the longest
  interval that still feels instant; prefer events over polling.
- **Leaks.** Watch memory and OS handles (file handles, GDI/window objects,
  sockets, threads) across a long session and across repeated open/close of the
  heaviest UI surface. A steady climb is a P1 bug, not a rounding error.
- **Render only what changed.** Dirty-flag or invalidate-region rendering rather
  than repainting everything on a timer.
- **I/O batching.** Config and state writes are debounced and batched instead of
  written on every change.
