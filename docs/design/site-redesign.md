# Site, README and brand redesign — design spec

Status: **spec approved 2026-09-23**; plan in `docs/design/site-redesign-plan.md`. Branch `stevezau/site-redesign`. Excluded from the site
build (`docs/design/`). Follows `docs/design/discoverability.md` (shipped #291–#294).

## 0. Start here (for a fresh session)

1. Read this file, then `docs/design/discoverability.md` ("Still open") for what already shipped.
2. The reference implementation is the owner's sibling project: repo `/home/data/workspace/shortlist`
   (`docs/` = Jekyll site), live at https://shortlistapp.dev. When this spec says "as Shortlist does", open
   the named Shortlist file and copy the pattern, not the Shortlist-specific copy.
3. Owner checkpoints (§11) are hard gates. Don't build past one without an explicit yes.
4. Hard constraint carried over: every Pages deploy must ship the live Jellyfin plugin manifest at
   `/jellyfin-plugin/manifest.json` (see discoverability.md "Hard constraint").

## 1. Goal and success criteria

Side by side with shortlistapp.dev, this project must not look like the lesser sibling: same product-grade
landing page, same polish in the README, llms.txt and imagery, one visual family. The first thing a visitor
sees is the product's result — a preview thumbnail over a player's scrub bar — not our admin dashboard.

Success:
- Landing page has every Shortlist section type (§4), rendered with real captures, no stock-docs chrome.
- Hero, README lead image and social card show real Plex, Jellyfin and Emby players mid-scrub with
  thumbnails this app generated.
- Every factual claim on the site is checkable; any speed number comes from our published benchmark (§8).
- Nothing already shipped regresses: manifest-safe deploys, llms-full drift test, image/title/list tests,
  structured data, Search Console, redirects from the old github.io URLs.

## 2. Decisions (owner, 2026-09-23)

- **Platform: port Shortlist's Jekyll theme** (same owner, MIT) instead of MkDocs Material.
- **Hero: real players, all three servers**, on throwaway lab servers with freely licensed films.
- **Domain: `mediapreviewgenerator.dev`** (bought 2026-09-23). `.dev` is HSTS-preloaded: HTTPS only.
- **Logo: one new filled amber mark used everywhere** (README, site, favicon, social card, app favicon
  and nav brand). **Option A (scrub preview)** chosen 2026-09-23 (owner delegated the pick).
- Name stays "Media Preview Generator". Intro/credit detection stays unmentioned until it merges.
  **Superseded 2026-09-24 (owner):** the site represents Intro & Credits (PR #241), launching after #241; see `docs/skip-intro-credits.md`.

## 3. Site architecture (Jekyll port)

Mirror Shortlist's `docs/` structure:
- `docs/_config.yml` — `url: https://mediapreviewgenerator.dev`, `permalink: pretty`, `nav:` list (drives
  sidebar, pager, breadcrumbs, llms-full order), plugins `jekyll-relative-links`, `jekyll-seo-tag`,
  `jekyll-sitemap`; `exclude:` `design/`, `README.md` (GitHub hub page), Gemfile bits.
- `docs/_layouts/{default,home,doc}.html`, `docs/_includes/{head,nav,footer}.html`,
  `docs/assets/css/main.css`, `docs/assets/js/site.js`, `docs/404.html`, `docs/robots.txt` (Liquid),
  `docs/search.json` (Liquid search index), `docs/CNAME`.
- `docs/_data/{features,tour,faq,works_with,stats}.yml` feed the landing page; `faq.yml` also feeds the
  FAQPage JSON-LD (same source as the visible FAQ).
- Keep from Shortlist: system font stack, theme set before first paint (dark default), copy buttons,
  sticky scroll tour, count-up stats, client-side search, BreadcrumbList on docs pages, footer sitemap,
  Coffee/Star pills in the nav.
- **GitHub alerts:** our docs use `> [!NOTE]`-style alerts (render on GitHub). Add a small `_plugins/`
  converter that renders them as Shortlist's `.callout` markup, so the Markdown stays GitHub-friendly.
- **Build + deploy:** Shortlist uses Pages' native build; we can't (the manifest step). `docs.yml` swaps
  the MkDocs build for Ruby + `bundle exec jekyll build` (Gemfile + Gemfile.lock committed, versions
  pinned). Everything after the build — live-manifest fetch/validate/fail-closed, reusable call from the
  plugin workflow, `pages` concurrency — stays as is.
- **Front matter:** `title` (search-shaped, keeps #294's titles), `heading` (H1), `description`,
  `facts_checked` where relevant. Remove MkDocs-only bits (`hide:`, `mkdocs.yml`, `docs_theme/`, the
  mkdocs deps) once the Jekyll site builds.

### Domain switch (inside this project, before launch)

1. DNS: apex A/AAAA to GitHub Pages, `www` CNAME to `stevezau.github.io`, GitHub domain verification TXT.
2. **Before switching:** prove Jellyfin follows a 301 for the plugin manifest (point a lab Jellyfin's plugin
   repository at a URL that redirects to the live manifest). If it doesn't, stop and redesign the switch.
3. Set the Pages custom domain (API), wait for the certificate, enforce HTTPS, then switch `url`,
   `LIVE_MANIFEST_URL`/`PREV_URL`, and every absolute URL (README, Docker Hub README, llms files,
   pyproject, repo homepage field) in one change.
4. Verify old github.io deep links 301 to the matching new page (3-4 real ones, not just `/`).
5. New Search Console property (Domain property via DNS TXT preferred), submit sitemap.

## 4. Landing page (sections in order)

Copy follows §7's voice rules. Draft copy goes to the owner at checkpoint 2 before the docs pages are ported.
1. **Nav:** logo + name, top docs sections, Search (`/`), theme toggle, worded Coffee and Star pills.
2. **Hero:** outcome headline (what the viewer gets, not "GPU-accelerated generation"), 2-sentence sub,
   buttons "Get started →" and "View source", tick line (one Docker container · Plex, Emby and Jellyfin ·
   CPU fallback built in · ⓘ needs write access where previews live), framed hero capture (§8).
3. **Stats band (checkable numbers only):** e.g. 1 decode per file, 3 media servers, 1 webhook URL,
   plus one benchmark number only if §8 produces it.
4. **The problem:** the built-in generators, in human terms with one concrete example.
5. **How it works tour** (`tour.yml`): Trigger → Resolve → Extract → Publish → Retry, each with a framed
   app capture (address bar `192.168.1.10:8080/...`, the fixture host).
6. **Same file, three servers:** the 3-up player capture with a one-line explanation of the fan-out.
7. **HDR and Dolby Vision:** before/after pair (untone-mapped vs ours) from a freely licensed HDR title.
8. **Features grid** (`features.yml`), inline SVG icons.
9. **Works with:** pipeline "Sonarr / Radarr / Tdarr / webhooks → one GPU decode → Plex BIF · Emby BIF ·
   Jellyfin trickplay", then cards grouped Required / Optional (GPU vendors, automation tools).
10. **Compared with the built-ins** teaser linking the comparison page.
11. **Quick start:** compose and `docker run` side by side with copy buttons, requirements line.
12. **FAQ:** 6-8 questions from `faq.yml`, worded the way people search, "Read the full FAQ →".
13. **Closing call to action:** "Give it a try" + Install / Star on GitHub.
14. **Footer:** columns (Documentation, How-to, Project, Related), every page listed, credits for the
    Blender and Netflix films, "Not affiliated with Plex, Emby or Jellyfin" line.

## 5. Docs pages

All existing pages move onto the doc layout (sidebar from `nav`, prev/next, breadcrumbs, TOC). Content
changes:
- **Comparison page:** first-person disclosure ("This is my project…"), "Corrections welcome" issue link,
  "First, decide which problem you have" sorting section, `facts_checked` rendered from front matter (drop
  the hand-typed date), benchmark results if §8 lands.
- **FAQ:** questions reworded as searched ("Why are my Plex preview thumbnails taking so long?").
- **Custom 404** with nav cards, as Shortlist's `docs/404.html`.

## 6. README (GitHub)

Rewrite to Shortlist's structure (`/home/data/workspace/shortlist/README.md`):
- ~9 badges, `for-the-badge`, recoloured amber `#a06a00` (value badges `color=`, status badges
  `labelColor=`); drop Contributors, Forks and the separate Sponsor badge; build badge tracks `main`.
- Logo, outcome tagline ending "Self-hosted, one Docker container.", "Explore the docs »".
- Link row: Quick start · How it compares · Ask a question · Report a bug.
- One full-width hero image (the 3-up players) with a `<sub>` caption disclosing the lab setup.
- "What it does" (problem + bold one-line answer) · "What it looks like" 2×2 captioned screenshot table ·
  features grouped under bold headings · "Where it fits" (alongside Sonarr/Radarr/Tdarr; turn off built-in
  generation) · Quick start · docs table incl. the 6 how-to pages · Support (Star first, then Ko-fi) ·
  Get help · License.
- Remove: Built With badges, the Installation table (duplicates Quick start), the Rich acknowledgement, the
  `---` rule between every section. `DOCKERHUB_README.md` gets the same content minus GitHub-only bits.

## 7. llms.txt, metadata and voice

- **Voice:** plain words, few em dashes, one concrete human example per section, no table-stakes claims
  ("easy to use", "self-hosted friendly"). Applies to site, README, llms files.
- **llms.txt** moves to `docs/llms.txt` (a page with `permalink: /llms.txt`, as Shortlist) — the repo-root
  copy is removed. Structure: opens with the searched question it answers; `llms-full.txt` link first
  ("every docs page in one fetch"); "How it differs" as one-line identities of each alternative plus one
  bold distinction claim (concessions live on the comparison page); status line; every docs link described.
- **llms-full.txt:** port Shortlist's `scripts/build_llms_full.py` approach (reads `nav` from
  `_config.yml`, flattens includes/data loops, strips front matter/JSON-LD/comments, resolves `.md` links),
  keep the drift test and add Shortlist's permalink regression test.
- **GitHub:** description "Self-hosted, GPU-accelerated preview thumbnails for Plex, Emby and Jellyfin,
  made as soon as Sonarr or Radarr import a file. HDR tone-mapped, one Docker container." Topics: add
  `selfhosted`, `radarr`, `hdr`, `dolby-vision`, `preview-thumbnails`; drop `thumbnails-preview`,
  `media-server-automation` (stay at 20).

## 8. Imagery, lab and benchmark

- **Lab (storage host):** throwaway Plex, Jellyfin and Emby containers on a private Docker network, never
  the production servers. Library in a scratch lab directory (never under `/data`): Blender open films
  (Tears of Steel, Sintel, Big Buck Bunny 1080p60, Cosmos Laundromat, Elephants Dream, Spring, Sprite
  Fright, Charge; CC BY) with their posters where freely licensed; one Netflix Open Content HDR title
  (CC BY 4.0) for the HDR pair. Attribution recorded in `docs/credits.md` and the footer.
- **Captures:** Playwright (Chrome-for-Testing, H.264-capable) at 2× device scale, each player
  mid-scrub with our thumbnail visible; 3-up composite for hero/README/social card; per-server shots for
  the site. Plex needs a claim code from the owner at capture time (4-minute validity).
- **App screenshots:** harness captures at 2×, one idea per shot (no full-page scrolls), fixture job rows
  use the lab film titles and poster art, WebP q≈88, shown in browser frames on the site.
- **Social card:** rendered from an HTML template (as Shortlist's `tests/e2e/assets/social_preview.html`)
  to a 1280×640 baseline JPEG showing the result, not the dashboard.
- **Benchmark:** on the lab library and storage's NVIDIA GPU, time Plex's built-in generation vs this app
  for the same files and settings (and Jellyfin trickplay if practical). Publish method, hardware,
  versions, raw timings (`docs/benchmark.md` + data file). Only then may the site, README or llms files
  state a speed number. If the built-in can't be timed reliably, drop the number rather than estimate.
- **Dolby Vision Profile 5:** verify the "dim without Vulkan" behaviour only if a freely licensed P5 source
  turns up cheaply; otherwise leave it recorded as unverified.

## 9. Logo

Options A (scrub preview), B (sliced play), C (frame stack): `docs/design/logo/logo-{a,b,c}.svg`, compared
at 16–256 px on dark/light and in a browser tab in `docs/design/logo/logo-options.png`. **A is chosen**: it is
the hero's own picture (a thumbnail over a scrub bar). Its 16 px render reads like a small TV, so ship a
pixel-hinted favicon variant of the same design for 16/32 px (larger frame, no tail). `logo-a.svg` moves to
`docs/assets/img/`; B and C are deleted.
Chosen mark replaces `docs/images/icon.svg` and `icon.png` everywhere: README, site logo + favicon, social
card, app web UI favicon and nav brand.

## 10. Tests (keep or port)

- Jekyll build succeeds with no warnings; built-site assertions ported from `tests/test_docs_site.py`:
  canonical + description + og image per page, SoftwareApplication/WebSite/BreadcrumbList JSON-LD,
  FAQPage question count == `faq.yml`, robots + sitemap on the canonical host, search-facing titles name
  a server, no swallowed lists, no literal `[!NOTE]`, docs build never emits the plugin manifest.
- llms-full drift + permalink tests; docs link test (as Shortlist `tests/unit/test_docs_links.py`);
  images both ways (existing test, extended to Liquid/HTML/_data forms); social card geometry/format.
- actionlint on `docs.yml` and `jellyfin-plugin.yml`.

## 11. Owner checkpoints and inputs

1. ~~Logo pick~~ — done: A.
2. **Landing page** rendered with real copy and first captures, before porting the docs pages.
3. **Hero/benchmark results** — captures and numbers before they go on the site.
4. **Final review** of the full site + README before merge and domain switch.
Inputs: DNS records + GitHub domain verification (in progress), Plex claim code at capture time.

## 12. Out of scope

Intro/credit detection content; redesigning the app UI beyond the logo swap; new product features.

**Superseded 2026-09-24 (owner):** the site represents Intro & Credits (PR #241), launching after #241; see `docs/skip-intro-credits.md`. The intro/credit exclusion above no longer applies.

## 13. Risks

- Jellyfin plugin clients not following the manifest redirect → verified before the switch (§3).
- `.dev` HSTS: site unreachable until the Pages certificate issues → switch URLs only after it's live.
- Emby web trickplay reported flaky → budget retries; ship with two servers in the hero if Emby won't
  show thumbnails after reasonable effort, and say so.
- Headless playback: use Chrome-for-Testing (H.264) and `--autoplay-policy=no-user-gesture-required`.
