# Discoverability, docs site and SEO pass

Status: shipped 2026-09-23 (#291 docs site; app-code fixes below in a follow-up PR). Live at
https://mediapreviewgenerator.dev/. Owner follow-ups and outreach drafts:
`docs/design/discoverability-outreach.md`. Excluded from the docs build (`docs/design/`).

Method source: the sibling-project playbook (audit → crawl plumbing → llms.txt → structured
data → intent pages → images → repo metadata → verify with real retrieval).

## Decisions

- **Name stays "Media Preview Generator".** No rename: search engines already recommend it by
  this name, and the retired `stevezzau/plex_generate_vid_previews` image still outranks the
  current one months after the last rename.
- **Domain: `mediapreviewgenerator.dev`**, live 2026-09-23 (Route 53 DNS, GitHub-verified, Let's Encrypt
  cert via Pages). github.io URLs 301 to the matching page; a lab Jellyfin 10.11 was shown to follow a
  cross-host 301 for the plugin manifest before the switch. The app recognises both manifest URLs.
- **Generator: MkDocs Material**, built and deployed by GitHub Actions.
- **Intro/credits detection is NOT shipped** (lives on `feat/markers-detection`). No page, no
  llms.txt claim, until it merges. `docs/faq.md` and `docs/getting-started.md` correctly say it
  isn't supported.
  **Superseded 2026-09-24 (owner):** the site represents Intro & Credits (PR #241), launching after #241; see `docs/skip-intro-credits.md`.
- **Funding: add Ko-fi** alongside GitHub Sponsors (username pending from owner).

## Hard constraint: the Jellyfin plugin manifest

Pages serves `https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json` (older installs use the
github.io address, which 301s there),
which every Jellyfin install of the plugin polls. A Pages deploy replaces the whole site, so:

- The manifest is built from the `plugin-v*` GitHub releases on every deploy, docs or plugin
  release (`scripts/build_jellyfin_manifest.py`: version, MD5 and zip URL from each release's
  assets, plugin metadata from `jellyfin-plugin/manifest.template.json`), and shipped at the same
  path. The deploy must FAIL (deploy nothing) on a download error, a digest mismatch or zero
  versions. Nothing reads the live copy back, so a cancelled or failed deploy loses no version:
  the next deploy lists the releases itself. (Until 2026-09-24 docs deploys re-shipped the live
  manifest and only the plugin release added versions, so GitHub cancelling that pending deploy
  dropped a version for good.)
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

## Switching to a custom domain (checklist — done 2026-09-23)

1. `site_url` in `mkdocs.yml`; the domain itself is set in the repo's Pages settings (Actions deploys, no CNAME file).
2. `LIVE_MANIFEST_URL` in `.github/workflows/docs.yml` and `PREV_URL` in `jellyfin-plugin.yml`.
3. The absolute docs URLs in `README.md`, `DOCKERHUB_README.md`, `llms.txt`, `pyproject.toml`; regenerate
   `llms-full.txt` (`python scripts/generate_llms_full.py`).
4. Existing Jellyfin installs point at the github.io manifest URL. GitHub Pages 301s project URLs to the
   custom domain; confirm Jellyfin's plugin-repository fetch follows the redirect before switching.
5. New Search Console property for the domain (the github.io property's history doesn't carry over);
   robots.txt starts working at that point.

## Code issues found during the docs audit (fixed in #292)

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

Done 2026-09-23: GitHub social preview uploaded; Search Console URL-prefix property verified
(token in `mkdocs.yml`), sitemap submitted, indexing requested for home + comparison; retired
Docker Hub image's short description set to "RETIRED — moved to …".

Still open:
- Outreach posts — drafts and per-channel rules in `docs/design/discoverability-outreach.md`
  (awesome-selfhosted-data rejects AI-written entries; r/PleX bans self-promotion).
- Current image's Docker Hub short description fills in on the next release tag (ci.yml syncs it
  from `pyproject.toml`), or paste it by hand.
- Re-run the baseline queries below around 2026-10-21, once Google has indexed the site.
- Search Console: add a Domain property for mediapreviewgenerator.dev (DNS TXT) and submit the sitemap.
- Settings label "Initial retry delay" reads 30 s while the first retry waits 1 min (copy under
  the slider explains it); renaming is the owner's call.
- Dolby Vision Profile 5 "dim without Vulkan" has never been checked on a real P5 clip.

## Baseline (2026-09-23, WebSearch + Exa)

- "best tool to generate plex preview thumbnails": #1–4 both; AI Overview names it primary recommendation.
- "generate plex preview thumbnails with GPU": web #3, Exa #1; 9+ forks dilute.
- "BIF file generation ffmpeg hardware acceleration": web not top 10, Exa #7.
- "jellyfin trickplay generation slow GPU": web #9 (retired image only), Exa not top 10.
- Third-party: JellyWatch directory only. No AlternativeTo, awesome-list or press.
