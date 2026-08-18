# 05 — SEO & discoverability

**Gate:** the project has a publicly indexable web page — marketing site,
landing page, docs, or blog. Skip entirely for internal tools, apps behind a
login, desktop apps, and CLIs. If the *product* is a desktop app but it has a
marketing site, this file applies to that site only.

Split the work: some of this is code (do it), some of it is account-based (list
it under **Needs you** with exact steps).

---

## A. Indexability — code

- **`robots.txt`** exists, allows the pages that should be indexed, and blocks
  what should not be (admin, staging, search-result pages, duplicate params).
- **`sitemap.xml`** exists, is generated from real routes, lists canonical URLs
  only, updates on build, and is referenced from `robots.txt`.
- **No accidental `noindex`** left over from staging — check meta tags *and* the
  `X-Robots-Tag` header on every environment that is publicly reachable.
- **Canonical URLs** set on every page; one host and one scheme (redirect
  www↔apex and http→https to the canonical form consistently).
- **Clean, stable URLs** — readable slugs, no session ids or tracking params in
  canonical links, and permanent redirects for anything moved.
- **Server-rendered content.** Confirm the primary content is present in the
  initial HTML rather than only after client-side hydration.

## B. On-page metadata — code

- **Title tags.** Unique per page, with the main keyword near the front, under
  ~60 characters, following one consistent pattern. (The per-route title
  mechanism itself is in `03-ux.md` §E — implement it once, use it here.)
- **Add the location** to titles, headings, and copy if the business serves a
  specific city or region.
- **Meta descriptions.** Unique per page, ~150–160 characters, written as a
  reason to click rather than a keyword list.
- **One `<h1>` per page** that matches the page's actual subject, with a sane
  heading hierarchy below it.
- **Open Graph and Twitter card tags** with a correctly sized preview image, so
  shared links do not render as a bare URL.
- **Structured data** (JSON-LD) where a type genuinely applies — Organization,
  Product, Article, FAQ, LocalBusiness. Validate it; do not fabricate markup for
  content that is not on the page.
- **Image `alt` text** that describes the image (this is also an accessibility
  requirement — see `03-ux.md` §G).

## C. Content & links — code

- **Internal links** connect related pages with descriptive anchor text, so
  every important page is reachable within a few clicks of the home page. No
  orphan pages.
- **No broken links** internal or outbound; no redirect chains.
- **Image weight.** Compressed and modern-format — the same work as
  `02-performance.md` §A. Do it once; SEO benefits from it, do not duplicate the
  audit.
- **Core Web Vitals.** LCP, CLS, and INP in the good range — covered by
  `02-performance.md` §A–C. Record the numbers here as the SEO evidence.
- **Mobile-friendly** and readable without zoom, with a correct viewport meta.

## D. Accounts & submissions — **Needs you** (human-run)

These cannot be done from the codebase. List them in the final report with what
is ready and what the human must click.

- **Google Search Console** — verify the property (the verification file or DNS
  TXT record can be prepared in-repo).
- **Submit the sitemap** in Search Console once verified; check the coverage
  report a few days later.
- **Analytics** installed and confirmed firing (the snippet or SDK is code; the
  account and property are not). Respect consent requirements in the target
  region.
- **Google Business Profile** — only if the business is local. Claim it,
  complete every field, add photos and hours.
- **Backlinks** — the long game: partners, directories, communities, guest
  posts, and anywhere the product legitimately belongs. Overlaps with
  `06-launch.md`; treat the launch channels there as the first wave.
