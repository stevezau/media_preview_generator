# Discoverability, docs site and SEO pass

Status: in progress (branch `stevezau/discover`). Excluded from the docs build (`docs/design/`).

Method source: the sibling-project playbook (audit → crawl plumbing → llms.txt → structured
data → intent pages → images → repo metadata → verify with real retrieval).

## Decisions

- **Name stays "Media Preview Generator".** No rename: search engines already recommend it by
  this name, and the retired `stevezzau/plex_generate_vid_previews` image still outranks the
  current one months after the last rename.
- **Domain: undecided.** Build on `https://stevezau.github.io/media_preview_generator/`. Switching
  later = `site_url` in `mkdocs.yml` + a `docs/CNAME` file + a new Search Console property.
  Candidates: mediapreviewgenerator.com (preferred), .dev, .media. previewgenerator.com was
  registered by an unknown party on 2026-09-18.
- **robots.txt is inert on a github.io project path** (crawlers read it only at the host root).
  Still generate it so the custom-domain switch needs no work; submit the sitemap in Search
  Console directly meanwhile.
- **Generator: MkDocs Material**, built and deployed by GitHub Actions.
- **Intro/credits detection is NOT shipped** (lives on `feat/markers-detection`). No page, no
  llms.txt claim, until it merges. `docs/faq.md` and `docs/getting-started.md` correctly say it
  isn't supported.
- **Funding: add Ko-fi** alongside GitHub Sponsors (username pending from owner).

## Hard constraint: the Jellyfin plugin manifest

Pages already serves `https://stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json`,
which every Jellyfin install of the plugin polls. `.github/workflows/jellyfin-plugin.yml`
(`publish-pages`) builds it by fetching the LIVE manifest and patching in the new release
(version, checksum, zip URL — values that only exist at plugin-release time). A Pages deploy
replaces the whole site, so:

- The docs deploy must fetch the live manifest and ship it at the same path, and must FAIL
  (deploy nothing) if that fetch fails.
- The plugin release must also ship the docs site, or a plugin release wipes the docs.
- Both paths go through one reusable workflow with a shared `pages` concurrency group.

## Work lanes

A. Site infra (opus): `mkdocs.yml`, theme overrides, hooks, `docs.yml` reusable Pages workflow,
   `jellyfin-plugin.yml` rewired to call it, `docs` deps, `site/` gitignored, GitHub-alert
   compatibility (`markdown-callouts`), links leaving `docs/` rewritten to absolute GitHub URLs,
   `exclude_docs` for `docs/design/` and `docs/README.md`, meta `description` on every existing
   page, canonical/og/JSON-LD (SoftwareApplication + WebSite everywhere, FAQPage from `faq.md`),
   robots.txt generated from `site_url`, `mkdocs build --strict` test.
B. llms-full.txt generator + committed output + drift test (+ test that its permalink isn't
   `/llms.txt`, and every emitted URL is a built page).
C. Content (opus, facts researched + dated first): rewrite `llms.txt` (How it differs / how it
   works / key facts incl. limits / docs / source), `docs/index.md` landing page, and these
   intent pages:
   1. Media Preview Generator vs Plex, Jellyfin and Emby built-in thumbnail generation (dated table)
   2. Why Plex video preview thumbnails take so long, and how to speed them up
   3. Generate Plex preview thumbnails with a GPU (NVIDIA, Intel, AMD)
   4. Faster Jellyfin trickplay generation
   5. Emby preview thumbnails (BIF) with GPU acceleration
   6. Generate previews as soon as Sonarr/Radarr import a file
   7. Preview thumbnails for HDR and Dolby Vision
D. Images (sonnet): fixture fixes in `tests/e2e/snapshots/` (LAN IPs instead of `your-server.local`,
   stub the "Update available" dev banner, stub the Servers-page library status so no "Checking…"
   pills, clip automation.png instead of a 3467px full page, fix the truncated Intel GPU string),
   emit WebP, regenerate, 1200x600 non-progressive JPEG og:image, test that every referenced image
   exists and no committed image is unreferenced.
E. Metadata: README badges (CI, release, image size, Ko-fi), Docker Hub `short-description`
   (current + deprecated mirror), `pyproject` Documentation URL, ~20 GitHub topics, homepage field
   → docs site once live.

## Switching to a custom domain (checklist)

1. `site_url` in `mkdocs.yml`; add `docs/CNAME` with the bare domain.
2. `LIVE_MANIFEST_URL` in `.github/workflows/docs.yml` and `PREV_URL` in `jellyfin-plugin.yml`.
3. The absolute docs URLs in `README.md`, `DOCKERHUB_README.md`, `llms.txt`, `pyproject.toml`; regenerate
   `llms-full.txt` (`python scripts/generate_llms_full.py`).
4. Existing Jellyfin installs point at the github.io manifest URL. GitHub Pages 301s project URLs to the
   custom domain; confirm Jellyfin's plugin-repository fetch follows the redirect before switching.
5. New Search Console property for the domain (the github.io property's history doesn't carry over);
   robots.txt starts working at that point.

## Code issues found during the docs audit (not fixed here — app code, separate change)

- Settings page retry copy is wrong (`web/templates/settings.html:141-147` says 30 s doubling; real
  schedule is 60 s/2 m/5 m/15 m/60 m scaled by "Initial retry delay" ÷ 30).
- Jellyfin "Vendor-side preview generation" readiness row (`servers/jellyfin.py` ~2046) always
  recommends "stopped", contradicting the scan-time-extraction check (`jellyfin.py:688-700`) that says
  keep it on without the plugin.
- A `:ro` media mount for Emby / default-layout Jellyfin fails with EROFS; the friendly "mount it
  read-write" hints (`emby_sidecar.py:75`, `jellyfin_trickplay.py:359`) only catch EACCES, and no
  readiness check warns in advance.
- Stale Dolby Vision Profile 5 warning copy: `gpu/vulkan_probe.py` (256, 295, 304, 555, 609, 615),
  `web/notifications.py:94` and `web/routes/api_vulkan.py` (273, 283, 439) still say "green overlay".
  Since f167e5ae (2026-04-12) no hardware Vulkan means frames are extracted without tone mapping (dim).
  The dim result itself has never been checked on a real Profile 5 clip (none on disk).

## Needs the owner

- Domain purchase + DNS (later). Ko-fi username. GitHub social preview upload (no API).
- Search Console property + sitemap submission.
- AlternativeTo listing, awesome-selfhosted PR, subreddit posts (respect each channel's rules).

## Baseline (2026-09-23, WebSearch + Exa)

- "best tool to generate plex preview thumbnails": #1–4 both; AI Overview names it primary recommendation.
- "generate plex preview thumbnails with GPU": web #3, Exa #1; 9+ forks dilute.
- "BIF file generation ffmpeg hardware acceleration": web not top 10, Exa #7.
- "jellyfin trickplay generation slow GPU": web #9 (retired image only), Exa not top 10.
- Third-party: JellyWatch directory only. No AlternativeTo, awesome-list or press.
