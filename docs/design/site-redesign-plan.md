# Site, README and Brand Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the MkDocs docs site with a port of Shortlist's Jekyll theme at `https://mediapreviewgenerator.dev`, with a product-grade landing page, real Plex/Jellyfin/Emby players mid-scrub on open films, a reproducible benchmark, the new logo everywhere, and a rewritten README and llms.txt, without breaking the Jellyfin plugin manifest or anything #291-#294 shipped.

**Architecture:** Jekyll 4 is built in GitHub Actions from a committed `docs/Gemfile.lock` (Ruby 3.4) inside the existing reusable Pages workflow, so the live-manifest fetch/validate/fail-closed step and the plugin-release call stay exactly as they are. One small `_plugins/` hook keeps GitHub-flavoured Markdown (alerts, relative images, the docs hub link) correct on both github.com and the site. Every picture and number comes from throwaway lab servers on `storage` holding CC BY films, driven by committed scripts; Python tests build the real site through Bundler (host Ruby or the pinned `ruby` image) and assert what readers, crawlers and Jellyfin servers depend on.

**Tech Stack:** Jekyll 4.4 (kramdown GFM, jekyll-relative-links, jekyll-seo-tag, jekyll-sitemap), Ruby 3.4 + Bundler, GitHub Actions (`ruby/setup-ruby`, `actions/deploy-pages`), Python 3.12+ with pytest, PyYAML, filelock, Pillow, Playwright 1.62 Chromium (Chrome for Testing); Docker lab: `plexinc/pms-docker`, `jellyfin/jellyfin:10.11` and `12.0`, `emby/embyserver:4.10.0.40`, this branch's image; FFmpeg/ffprobe; `gh`.

**Spec:** `docs/design/site-redesign.md` (read its §0 first). Hard constraint carried over from `docs/design/discoverability.md` ("Hard constraint: the Jellyfin plugin manifest").

## Status update (2026-09-23, after the plan was written)

- **Task 12 is superseded by #295 (merged, `78989bc5`).** Every URL already points at
  `https://mediapreviewgenerator.dev/`, `docs.yml` `LIVE_MANIFEST_URL` and `jellyfin-plugin.yml` `PREV_URL` use it,
  the repo homepage field is set, and `JellyfinServer.install_plugin` recognises the old and new manifest URLs
  (tests: none / old / new registered). Remaining from Task 12: add the "both URLs registered" test case only.
  The Jekyll `_config.yml` starts with `url: https://mediapreviewgenerator.dev` (Task 1), so no later switch.
- **Task 4 Step 1 (Jellyfin redirect proof) is done:** lab Jellyfin 10.11 loaded the plugin catalog (4 versions)
  through a cross-host HTTPS 301 (httpbin redirect-to the live manifest); the lab's repository list was restored.
  Skip the step; no result file is needed since Task 12 no longer consumes it.
- **Lab films:** Tears of Steel, Sintel, Big Buck Bunny and Elephants Dream are downloaded to
  `/home/data/lab-media/open-films/<Title (Year)>/<Title (Year)>.mp4` (outside `/data*`); Task 4 uses them from there.
- Before Task 1, merge `origin/dev` into `stevezau/site-redesign` (picks up #295).

## Facts established while writing this plan (2026-09-23)

- **The custom domain is already live.** `gh api repos/stevezau/media_preview_generator/pages` reports `cname: mediapreviewgenerator.dev`, `https_enforced: true`, certificate `approved` (expires 2026-12-22, covers apex and `www`). `https://stevezau.github.io/media_preview_generator/<path>` already 301s to `https://mediapreviewgenerator.dev/<path>`, including `/jellyfin-plugin/manifest.json` (200 after the redirect). DNS: apex A records 185.199.108-111.153, `www` CNAME `stevezau.github.io`.
  - Consequence 1: existing Jellyfin installs are already fetching the manifest through a 301. Task 4 Step 1 (the redirect proof) runs first, before anything else in the lab, and a failure goes to the owner the same hour.
  - Consequence 2: the site builds for `url: https://mediapreviewgenerator.dev`, `baseurl: ""` from Task 1. Task 12 is the code/metadata URL switch plus checks; the Pages API part is done.
  - Consequence 3: the live MkDocs site on the new domain still prints github.io canonicals. The owner may want Task 12's URL switch pulled forward as its own small PR to `dev`; by default it waits for STOP 4.
- No Ruby on the host (`which ruby bundle` prints nothing); Docker, ffmpeg, ffprobe, jq, gh, dig, curl are present. No `mediainfo` on the host (use ffprobe, or mediainfo inside the app container).
- The marker lab's live folder is `/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/` (on branch `feat/markers-detection`), not the older `docs/superpowers/specs/2026-09-13-skip-markers-evidence/lab/` copy. Its `env` file (chmod 600) holds `PLEX_TOKEN`, `JF_TOKEN`, `JF12_TOKEN`, `EMBY_TOKEN`, `EMBY_UID`, `MLAB_APP_TOKEN`, and others. Jellyfin and Emby web logins are `lab`/`lab`. Lab network `mlab`, gateway `172.18.0.1`.
- Lab containers carry ~190 per-folder bind mounts each (`mlab-plex`, `mlab-jellyfin`, `mlab-jf12` 192-193; the Embys 11). No existing mount is shared by all servers and free to hold new films, so adding the open films means recreating `mlab-plex`, `mlab-jellyfin` and `mlab-emby` with one extra `:ro` mount (Task 4, scripted from `docker inspect`, old container kept for rollback). `docker info` default runtime is `runc`; GPU containers use `--runtime=nvidia` plus `NVIDIA_*` env and `--device /dev/dri`. `mlab-plex` has no GPU today. Storage's GPU: Quadro P5000, driver 580.178.04.
- The app renders no poster art anywhere (only the BIF viewer shows frames), so "fixture job rows use the lab film titles and poster art" (spec §8) becomes: film titles in every job and worker row. The posters live in the lab libraries (local `poster.jpg`, so the servers' own library views look right); no image on the site shows one.
- `media_preview_generator/servers/jellyfin.py:431` hard-codes `PLUGIN_REPO_URL` (github.io) and `install_plugin` matches repositories by exact string (`:515`). Changing the constant without also recognising the old URL would add a second repository to every existing install (Task 12).
- `ci.yml` has `paths-ignore: docs/**, *.md` on both push and pull_request, so a docs-only change never runs the docs tests in CI today. Task 1 adds a small `docs-check.yml` that runs exactly those tests on such changes.
- GitHub allows at most 20 topics. The repo has 20; spec §7 adds 5 and drops 2 (23). Task 13 proposes 3 more drops for the owner to confirm at STOP 4.

## Global Constraints

- Site URL `https://mediapreviewgenerator.dev`, `baseurl: ""`; HTTPS only (`.dev` is HSTS-preloaded). Every template link goes through `relative_url`/`absolute_url`.
- Every Pages deploy ships the live Jellyfin manifest at `/jellyfin-plugin/manifest.json` and fails closed if it can't; the docs build never emits that file; `jellyfin-plugin.yml` keeps deploying through `docs.yml` with `manifest_artifact: jellyfin-plugin-manifest`.
- `_config.yml`: `permalink: pretty`; plugins `jekyll-relative-links`, `jekyll-seo-tag`, `jekyll-sitemap`; `exclude:` `design/`, `README.md`, Gemfile bits. `nav:` is the single source of page order (sidebar, pager, breadcrumbs, footer, llms-full).
- Page front matter: `title` (search-shaped; keep #294's titles), `heading` (H1), `description` (at most 155 characters), `facts_checked` where relevant.
- Voice (spec §7): plain words, few em dashes, one concrete human example per section, no table-stakes claims ("easy to use", "self-hosted friendly"). Applies to site, README and llms files.
- No speed number anywhere unless it is computed from `docs/benchmark/results.csv` (Task 8) and approved at STOP 3. If Plex's built-in can't be timed reliably, the site states no number.
- Intro/credit detection is not shipped: never mention it as a feature.
- Name stays "Media Preview Generator". Logo A (`docs/design/logo/logo-a.svg`) plus a pixel-hinted 16/32 px favicon variant. Amber `#e5a00d`, navy `#0f0f1a`; README badges amber `#a06a00` (value badges `color=`, status badges `labelColor=`).
- Lab: plain `docker run`, never compose. Never write under `/data*`. Media servers mount the open films `:ro`. Never touch production servers. Keep every existing lab container and library. Tokens stay in the lab env file (chmod 600): never committed, printed or put in `ps`-visible argv.
- Owner checkpoints are hard gates: **STOP 3** after Task 8 (captures + benchmark numbers, before they go on the site), **STOP 2** after Task 9 (landing page with real copy, before the docs-page content work), **STOP 4** after Task 11 (full site + README, before merge and the URL switch). No merge to `dev` and no URL switch without STOP 4.
- Before every commit: stage explicit paths (`git add <paths>`, never `-A`), dispatch the `Architecture Review` agent on the staged diff, fix HIGH findings, raise MED ones. Conventional Commits. Every commit message ends with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Python is `/home/data/.venv/bin/python`; a bare `python` in the commands below means that interpreter. In a lane worktree, prefix test runs with `PYTHONPATH=<that worktree>`. Never `pytest -m e2e -n auto`; never pipe pytest through `tail`. Run `ruff check --fix` and `ruff format` on touched Python before committing.
- After any change under `docs/` (pages, `_data/*.yml`, `nav`), run `python scripts/generate_llms_full.py`. Resolve `docs/llms-full.txt` merge conflicts by regenerating, never by hand.
- Images: player and app captures at 2x device scale, WebP quality 88 method 6; social card 1280x640 baseline JPEG. Every committed image is at most 500 KB (pre-commit `check-added-large-files --maxkb=500`); if a capture is bigger, lower the WebP quality in steps of 4 before shrinking the viewport.

## Review Focus

1. **A Pages deploy that ships without the live Jellyfin manifest**, or with one built from `docs/`: every Jellyfin install of the plugin stops seeing updates, silently. Expected: the build step swap changes nothing after "Build site", the manifest step has no `if:`, and a plugin release still deploys docs + manifest together. Pinned by Task 1 `tests/test_docs_workflow.py` (step order, unconditional fail-closed step, plugin workflow wiring, docs-check coverage, actionlint) and `test_docs_build_never_emits_jellyfin_manifest`.
2. **Jellyfin servers across the domain move.** Existing installs poll the github.io URL (already 301ing), and the app's one-click install compares repository URLs by exact string. Expected: old installs keep receiving the manifest; a reinstall on a server that has the old URL adds no second repository. Pinned by Task 4 Step 1 (`jellyfin_redirect_proof.py` on Jellyfin 10.11 and 12.0) and Task 12 `TestInstallPluginRepositoryUrl` (no repo / old URL / new URL / both registered, in either order).
3. **A docs page GitHub renders but Jekyll breaks**: Liquid eating `{{Item.Path}}` and `{{{args.inputFileObj._id}}}`, a list or table swallowed into a paragraph, `[!NOTE]` left literal, `--` turned into an en dash, relative `images/x.webp` resolving under `/page/images/`, an old URL gone. Expected: the page reads the same on both. Pinned by Task 1 `TestGithubMarkdownParity`, the plugin edge-case test and `test_every_legacy_url_still_resolves`; Task 2 `tests/test_docs_links.py`.
4. **A stale llms-full.txt after a data-file edit.** The landing FAQ, tour, features and stats live in `docs/_data/*.yml`, not in pages, so a page-only generator would miss them. Expected: editing a data file or `nav` changes the generated file, and the drift test fails until it is regenerated. Pinned by Task 2 `test_editing_a_data_file_changes_the_output` and `test_a_page_missing_from_nav_is_an_error`, run on docs-only changes by Task 1's `docs-check.yml` (ci.yml skips them).
5. **A speed number published without a benchmark source.** Expected: any "N x faster"/"N% faster" outside `docs/benchmark.md` fails the build, and a stat marked `source: benchmark` must equal `docs/benchmark/summary.json`. Pinned by Task 9 `tests/test_site_claims.py` and Task 8 `test_benchmark_page_quotes_the_summary`.

---

## Lanes, dependencies and gates

Speed over review rounds: three lanes in separate git worktrees, one review per task, deep review only for Task 1 (deploy workflow) and Task 12 (URL switch).

```
Lane A  site         (this worktree, branch stevezau/site-redesign)
        T1 ──> T2 ──> T9 ──[STOP 2]──> T10 ──> T11 ──[STOP 4]──> T12 ──> T13
Lane B  lab + imagery (../discover-lab, branch stevezau/site-redesign-lab)
        T4 ──> T5 ──> T8 ──[STOP 3]──> T7
Lane C  brand + app shots (../discover-brand, branch stevezau/site-redesign-brand)
        T3 ──> T6
```

Cross-lane dependencies (merge the producing lane into `stevezau/site-redesign` first, then merge `stevezau/site-redesign` into the consuming lane; no rebases, so lane commits keep their hashes):

- T1 Step 16 (the image test) needs T3 merged: head/nav reference the logo and favicons, and the test checks them.
- T9 Steps 1-7 need T2 and T6; T9 Step 8 (captures on the page) needs STOP 3 passed.
- T7 needs T3 merged and STOP 3 passed.
- T10 needs STOP 2, and T8 merged if a benchmark page exists.
- T11 needs T5, T6 and T7 merged.
- T12 needs STOP 4 and T4 Step 1's result file.

Worktrees (run once, from this worktree):

```bash
cd /home/data/orca/workspaces/plex_generate_vid_previews/discover
git worktree add -b stevezau/site-redesign-lab ../discover-lab stevezau/site-redesign
git worktree add -b stevezau/site-redesign-brand ../discover-brand stevezau/site-redesign
```

Merging a lane back (from this worktree, after that lane's task is reviewed and committed):

```bash
git merge --no-ff stevezau/site-redesign-brand -m "chore: merge brand lane (Task 3)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git log --oneline stevezau/site-redesign..stevezau/site-redesign-brand   # expect no output
python scripts/generate_llms_full.py --check || python scripts/generate_llms_full.py
```

(`scripts/generate_llms_full.py` exists from Task 2 on; before that, skip the last line.) Merge commits get the same Architecture Review + Co-Authored-By rule when the merge resolved conflicts.

Suggested executor models (owner rule: opus for non-trivial logic, debugging or anything a stranger shouldn't do unsupervised): T1 opus, T2 opus, T3 sonnet, T4 opus, T5 opus, T6 sonnet, T7 sonnet, T8 opus, T9 opus, T10 opus, T11 opus, T12 opus, T13 sonnet. Verification runs go to `verifier` (haiku).

## File map

Created:
- `docs/Gemfile`, `docs/Gemfile.lock`, `docs/.ruby-version`, `docs/_config.yml`, `docs/CNAME` — Jekyll toolchain and site config.
- `docs/_layouts/{default,doc,home}.html`, `docs/_includes/{head,nav,footer,faq-jsonld}.html` — theme ported from Shortlist.
- `docs/_plugins/github_markdown.rb` — alerts to `.callout`, relative `src`/`href` to site paths, docs hub link to home.
- `docs/_data/{faq,features,tour,works_with,stats}.yml` — landing page data; `faq.yml` also feeds FAQPage.
- `docs/assets/css/main.css`, `docs/assets/js/site.js`, `docs/assets/img/{logo.svg,favicon.svg,favicon-32.png,apple-touch-icon.png}`.
- `docs/404.html`, `docs/robots.txt`, `docs/search.json`, `docs/how-to.md`, `docs/credits.md`, `docs/benchmark.md`, `docs/benchmark/{results.csv,environment.json,summary.json}`.
- `docs/llms.txt`, `docs/llms-full.txt` (moved from the repo root; Jekyll pages with `permalink:`).
- `docs/design/site-redesign-lab/{films.json,openfilms.sh,add_mount.py,site_app.sh,lab_setup.py,jellyfin_redirect_proof.py,results/}` — lab tooling and evidence.
- `.github/workflows/docs-check.yml` — runs the docs tests on docs-only changes, which ci.yml skips.
- `tests/docs_toolchain.py`, `tests/test_docs_workflow.py`, `tests/test_docs_links.py`, `tests/test_brand_assets.py`, `tests/test_site_claims.py`, `tests/test_benchmark_summary.py`, `tests/test_readmes.py`, `tests/test_site_urls.py`, `tests/test_verify_live_site.py`.
- `tests/e2e/snapshots/{render_icons.py,lab_players.py,compose_site_images.py}`, `tests/e2e/snapshots/assets/social_preview.html`.
- `scripts/benchmark_previews.py`, `scripts/verify_live_site.py`.

Rewritten: `tests/test_docs_site.py`, `tests/test_llms_full.py`, `tests/test_docs_images.py`, `scripts/generate_llms_full.py`, `tests/e2e/snapshots/make_social_preview.py`, `README.md`, `DOCKERHUB_README.md`, `docs/llms.txt`.

Replaced in place (same path, new logo): `docs/images/icon.svg`, `docs/images/icon.png`, `media_preview_generator/web/static/images/{icon.svg,icon.png}`.

Modified: every `docs/*.md` page's front matter; `.github/workflows/{docs,ci,jellyfin-plugin}.yml`; `pyproject.toml`; `.pre-commit-config.yaml`; `.gitignore`; `CONTRIBUTING.md`; `.claude/CLAUDE.md`; `tests/e2e/snapshots/{regen_readme.py,readme_fixture.py}`; `scripts/regen_readme_screenshots.sh`; `media_preview_generator/web/templates/base.html`; `media_preview_generator/web/static/css/style.css`; `media_preview_generator/servers/jellyfin.py`; `tests/test_servers_jellyfin.py`; `jellyfin-plugin/README.md`; `docs/design/discoverability.md`.

Deleted: `mkdocs.yml`, `docs_theme/`, `docs/stylesheets/extra.css`, repo-root `llms.txt` and `llms-full.txt`, `docs/design/logo/logo-{b,c}.svg`, old screenshots `docs/images/{home,dashboard,servers,settings,automation}.webp`.

---
## Task 1: Jekyll site builds and deploys (theme port, page conversion, workflow)

Lane A. Deep review (deploy workflow). Suggested model: opus.

**Files:**
- Create: `docs/Gemfile`, `docs/Gemfile.lock`, `docs/.ruby-version`, `docs/_config.yml`, `docs/CNAME`, `docs/_layouts/default.html`, `docs/_layouts/doc.html`, `docs/_includes/head.html`, `docs/_includes/nav.html`, `docs/_includes/footer.html`, `docs/_includes/faq-jsonld.html`, `docs/_plugins/github_markdown.rb`, `docs/_data/faq.yml`, `docs/assets/css/main.css`, `docs/assets/js/site.js`, `docs/404.html`, `docs/robots.txt`, `docs/search.json`, `docs/how-to.md`, `tests/docs_toolchain.py`, `tests/test_docs_workflow.py`, `.github/workflows/docs-check.yml`
- Move: `llms.txt` -> `docs/llms.txt`, `llms-full.txt` -> `docs/llms-full.txt` (front matter added)
- Rewrite: `tests/test_docs_site.py`, `tests/test_docs_images.py`
- Modify: front matter of `docs/{index,getting-started,comparison,plex-preview-thumbnails-slow,plex-preview-thumbnails-gpu,jellyfin-trickplay-gpu,emby-bif-thumbnails-gpu,sonarr-radarr-preview-thumbnails,hdr-dolby-vision-thumbnails,guides,multi-server,reference,faq}.md` and `docs/guides/previews-readiness.md`; `.github/workflows/docs.yml`; `.github/workflows/ci.yml`; `pyproject.toml`; `.pre-commit-config.yaml`; `.gitignore`; `CONTRIBUTING.md` ("Editing a Docs Page"); `.claude/CLAUDE.md` (docs commands)
- Delete: `mkdocs.yml`, `docs_theme/` (hooks.py, overrides/main.html), `docs/stylesheets/extra.css`, `scripts/generate_llms_full.py` and `tests/test_llms_full.py` (both MkDocs-based; Task 2 writes their Jekyll versions — do not merge lane A to `dev` between Task 1 and Task 2)

**Interfaces:**
- Consumes: Shortlist theme files under `/home/data/workspace/shortlist/docs/` (MIT, same owner); Task 3's `docs/assets/img/{logo.svg,favicon.svg,favicon-32.png,apple-touch-icon.png}` (referenced here, created there; the build doesn't need them, the image test in Step 16 does).
- Produces:
  - `tests/docs_toolchain.py`: `REPO_ROOT: Path`, `DOCS_DIR: Path`, `RUBY_IMAGE = "ruby:3.4-bookworm"`, `toolchain() -> str` (`"bundle"` or `"docker"`, else skip/fail), `run_bundle(args: list[str], *, out_dir: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess[str]`, `build_site(dest: Path) -> subprocess.CompletedProcess[str]`, `published_sources() -> list[Path]`.
  - `tests/test_docs_site.py`: session fixtures `site() -> Path`, `build_output() -> str`, `pages() -> dict[str, Head]`; module names `CONFIG: dict`, `SITE_ROOT: str`, `strip_front_matter(text) -> str`, `front_matter(path) -> dict`, `built_page(site, source) -> Path`, `parse_head(path) -> Head`, `json_ld_types(block) -> set[str]`, `faq_data() -> list[dict]`.
  - `_config.yml` keys other tasks read: `url`, `baseurl`, `title`, `description`, `repo`, `author.{name,url}`, `demo_host`, `docker_image`, `docker_image_url`, `google_site_verification`, `nav[]` = `{title, url, blurb, header?, children?[{title, url}]}`.
  - Page front matter contract: `title`, `heading`, `description`, optional `facts_checked` (date), `faq_schema: true` (emits FAQPage from `_data/faq.yml`), `render_with_liquid: false` (pages that quote `{{ }}`).
  - `docs/_data/faq.yml`: list of `{q: str, a: str}`, 6-8 entries, every `q` also a heading on `/faq/`.
  - CSS classes the landing page (Task 9) uses: `.hero`, `.stats-band`, `.stats`, `.stat`, `.tour`, `.tour-step`, `.tour-panel`, `.frame`, `.frame--chrome`, `.frame__bar`, `.frame__title`, `.grid`, `.grid--2`, `.grid--3`, `.card`, `.card__icon`, `.pipeline`, `.supports`, `.codeblock`, `.faq`, `.cta`, `.callout`, `.section`, `.section--tint`, `.eyebrow`, `.btn`.
  - Ruby: `GithubMarkdown.convert_alerts(html) -> String`, `GithubMarkdown.absolutize(html, page_dir, baseurl) -> String`.

- [ ] **Step 1: Confirm the toolchain situation**

Run: `which ruby bundle || echo "no host ruby"; docker pull -q ruby:3.4-bookworm`
Expected: `no host ruby`, then `docker.io/library/ruby:3.4-bookworm`. Everything Ruby below runs in that image.

- [ ] **Step 2: Write the toolchain helper `tests/docs_toolchain.py`**

```python
"""Run the docs site's Ruby toolchain (Jekyll through Bundler) from tests.

Uses a host `bundle` when there is one (CI installs it with ruby/setup-ruby), otherwise the pinned
`ruby` Docker image, so a machine without Ruby can still build the site. CI sets
MPG_REQUIRE_DOCS_BUILD=1 so a missing toolchain fails the run instead of silently skipping every
docs test. MPG_DOCS_TOOLCHAIN=docker forces the Docker path even when `bundle` exists.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
RUBY_IMAGE = "ruby:3.4-bookworm"
# Gems installed by the Docker path, kept between runs (outside the repo, owned by the caller).
BUNDLE_CACHE = Path.home() / ".cache" / "mpg-docs-bundle"


def _docker_works() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


def toolchain() -> str:
    """Return "bundle" or "docker"; skip the test (or fail it under MPG_REQUIRE_DOCS_BUILD=1)."""
    if shutil.which("bundle") and os.environ.get("MPG_DOCS_TOOLCHAIN") != "docker":
        return "bundle"
    if _docker_works():
        return "docker"
    message = "no Ruby toolchain: install Ruby and Bundler, or Docker, to build the docs site"
    if os.environ.get("MPG_REQUIRE_DOCS_BUILD") == "1":
        pytest.fail(message)
    pytest.skip(message)


def run_bundle(
    args: list[str], *, out_dir: Path | None = None, timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    """Run `bundle exec <args>` in docs/.

    Args:
        args: The command after `bundle exec`, e.g. ["jekyll", "build", ...].
        out_dir: A folder the command writes to. The Docker path mounts it read-write at the same
            absolute path; the repo itself is mounted read-only.
        timeout: Seconds before the subprocess is killed.

    Returns:
        The finished process, stdout and stderr captured as text.
    """
    env = {**os.environ, "JEKYLL_ENV": "production", "BUNDLE_FROZEN": "true"}
    if toolchain() == "bundle":
        return subprocess.run(
            ["bundle", "exec", *args], cwd=DOCS_DIR, env=env, capture_output=True, text=True, timeout=timeout
        )
    BUNDLE_CACHE.mkdir(parents=True, exist_ok=True)
    mounts = ["-v", f"{REPO_ROOT}:{REPO_ROOT}:ro", "-v", f"{BUNDLE_CACHE}:/bundle"]
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)  # else Docker creates it root-owned
        mounts += ["-v", f"{out_dir}:{out_dir}"]
    script = "bundle install --quiet && bundle exec " + " ".join(shlex.quote(arg) for arg in args)
    command = [
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp", "-e", "BUNDLE_PATH=/bundle", "-e", "BUNDLE_FROZEN=true", "-e", "JEKYLL_ENV=production",
        *mounts, "-w", str(DOCS_DIR), RUBY_IMAGE, "sh", "-c", script,
    ]  # fmt: skip
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def build_site(dest: Path) -> subprocess.CompletedProcess[str]:
    """Build the site into `dest` exactly as docs.yml does (no disk cache: the source is read-only)."""
    return run_bundle(["jekyll", "build", "--disable-disk-cache", "--destination", str(dest)], out_dir=dest)


def published_sources() -> list[Path]:
    """Markdown files Jekyll turns into pages: not the GitHub hub, design docs or Jekyll's own folders."""
    sources = []
    for path in sorted(DOCS_DIR.rglob("*.md")):
        parts = path.relative_to(DOCS_DIR).parts
        if path.name == "README.md" and len(parts) == 1:
            continue
        if parts[0] in {"design", "vendor"} or any(part.startswith(("_", ".")) for part in parts):
            continue
        sources.append(path)
    return sources
```

- [ ] **Step 3: Write the workflow test `tests/test_docs_workflow.py`**

```python
"""The Pages workflows keep the Jellyfin plugin manifest safe (docs/design/discoverability.md).

A Pages deploy replaces the whole site, and every Jellyfin install of the plugin polls
/jellyfin-plugin/manifest.json on it. These pin the order that makes a deploy safe: build the
site, THEN place and validate the live manifest (failing the job if it can't), THEN upload and
deploy; and a plugin release still deploys through this same workflow.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTIONLINT_IMAGE = "rhysd/actionlint:1.7.7"
PAGES_WORKFLOWS = [".github/workflows/docs.yml", ".github/workflows/jellyfin-plugin.yml", ".github/workflows/docs-check.yml"]


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    return workflow.get("on") or workflow[True]


@pytest.fixture(scope="module")
def steps() -> list[dict]:
    return _load("docs.yml")["jobs"]["deploy"]["steps"]


def _step(steps: list[dict], name: str) -> tuple[int, dict]:
    names = [step.get("name") for step in steps]
    assert name in names, f"docs.yml has no step named {name!r}: {names}"
    index = names.index(name)
    return index, steps[index]


class TestDeployOrder:
    def test_manifest_is_placed_after_the_build_and_before_upload(self, steps: list[dict]) -> None:
        build, _ = _step(steps, "Build site")
        manifest, _ = _step(steps, "Place and validate Jellyfin plugin manifest")
        upload, _ = _step(steps, "Upload Pages artifact")
        deploy, _ = _step(steps, "Deploy to GitHub Pages")
        assert build < manifest < upload < deploy

    def test_manifest_step_always_runs_and_fails_closed(self, steps: list[dict]) -> None:
        _, step = _step(steps, "Place and validate Jellyfin plugin manifest")
        assert "if" not in step
        assert "set -euo pipefail" in step["run"]
        assert 'curl -fsSL --retry 3 "$LIVE_MANIFEST_URL"' in step["run"]
        assert "must only come from the plugin release" in step["run"]

    def test_build_writes_the_folder_that_gets_uploaded(self, steps: list[dict]) -> None:
        _, build = _step(steps, "Build site")
        _, upload = _step(steps, "Upload Pages artifact")
        assert build["working-directory"] == "docs"
        assert "bundle exec jekyll build" in build["run"]
        assert "--destination ../site" in build["run"]
        assert upload["with"]["path"] == "site"

    def test_ruby_and_gems_come_from_the_committed_lockfile(self, steps: list[dict]) -> None:
        _, ruby = _step(steps, "Setup Ruby")
        assert ruby["uses"].startswith("ruby/setup-ruby@")
        assert ruby["with"] == {"working-directory": "docs", "bundler-cache": True}
        assert (REPO_ROOT / "docs" / "Gemfile.lock").is_file()
        assert (REPO_ROOT / "docs" / ".ruby-version").is_file()

    def test_live_manifest_url_ends_at_the_manifest_path(self) -> None:
        assert _load("docs.yml")["env"]["LIVE_MANIFEST_URL"].endswith("/jellyfin-plugin/manifest.json")

    def test_deploys_are_serialised_in_the_pages_group(self) -> None:
        job = _load("docs.yml")["jobs"]["deploy"]
        assert job["concurrency"] == {"group": "pages", "cancel-in-progress": False}

    def test_push_trigger_watches_the_jekyll_sources_only(self) -> None:
        paths = _triggers(_load("docs.yml"))["push"]["paths"]
        assert "docs/**" in paths
        assert [p for p in paths if "mkdocs" in p or "docs_theme" in p or p.startswith("llms")] == []


class TestPluginReleaseShipsTheSite:
    def test_plugin_release_deploys_through_docs_yml_with_its_manifest(self) -> None:
        jobs = _load("jellyfin-plugin.yml")["jobs"]
        callers = [job for job in jobs.values() if job.get("uses") == "./.github/workflows/docs.yml"]
        assert len(callers) == 1
        assert callers[0]["with"]["manifest_artifact"] == "jellyfin-plugin-manifest"

    def test_docs_yml_accepts_what_the_plugin_release_passes(self) -> None:
        inputs = _triggers(_load("docs.yml"))["workflow_call"]["inputs"]
        assert {"manifest_artifact", "docs_ref"} <= set(inputs)


class TestDocsOnlyChangesAreTested:
    def test_docs_check_runs_the_docs_tests_on_docs_only_changes(self) -> None:
        # ci.yml's paths-ignore skips docs/** and *.md: exactly the changes that break these tests.
        workflow = _load("docs-check.yml")
        for event in ("pull_request", "push"):
            assert {"docs/**", "*.md"} <= set(_triggers(workflow)[event]["paths"]), event
        step = workflow["jobs"]["docs-tests"]["steps"][-1]
        assert step["env"]["MPG_REQUIRE_DOCS_BUILD"] == "1"
        assert "docs or llms" in step["run"]


@pytest.mark.timeout(300)
def test_pages_workflows_pass_actionlint() -> None:
    if shutil.which("actionlint"):
        command = ["actionlint", *PAGES_WORKFLOWS]
    elif shutil.which("docker"):
        command = ["docker", "run", "--rm", "-v", f"{REPO_ROOT}:/repo", "-w", "/repo", ACTIONLINT_IMAGE, *PAGES_WORKFLOWS]
    else:
        pytest.skip("neither actionlint nor docker is available")
    result = subprocess.run(command, capture_output=True, text=True, timeout=280)
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 4: Rewrite `tests/test_docs_site.py` against the Jekyll build**

Replace the whole file with:

```python
"""Docs site (Jekyll, docs/): build output, head tags, JSON-LD, crawler files, GitHub-Markdown parity.

Builds the real site once per test session with the same `bundle exec jekyll build` the Pages
deploy runs (tests/docs_toolchain.py), shared by every xdist worker through a file lock, then
inspects the output. Each assertion is something a reader, a crawler or a Jellyfin server would
trip over on the live site.

Docs pages carry no Liquid of their own: they are also read on github.com, where Liquid prints as
text. Layouts, includes and _data do the templating.
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from filelock import FileLock

from tests.docs_toolchain import DOCS_DIR, build_site, published_sources, run_bundle

pytestmark = pytest.mark.timeout(900)

CONFIG: dict = yaml.safe_load((DOCS_DIR / "_config.yml").read_text(encoding="utf-8"))
SITE_ROOT = f"{CONFIG['url']}{CONFIG.get('baseurl') or ''}/"
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
PROSE = re.compile(r'<main class="prose"[^>]*>(.*?)</main>', re.DOTALL)
PARAGRAPH = re.compile(r"<p>(.*?)</p>", re.DOTALL)

# Every URL the MkDocs site served on 2026-09-23. Pretty permalinks keep them, so bookmarks, search
# results and the github.io -> mediapreviewgenerator.dev 301s all still land on a page.
LEGACY_PATHS = [
    "",
    "getting-started/",
    "comparison/",
    "plex-preview-thumbnails-slow/",
    "plex-preview-thumbnails-gpu/",
    "jellyfin-trickplay-gpu/",
    "emby-bif-thumbnails-gpu/",
    "sonarr-radarr-preview-thumbnails/",
    "hdr-dolby-vision-thumbnails/",
    "guides/",
    "guides/previews-readiness/",
    "multi-server/",
    "reference/",
    "faq/",
    "llms.txt",
    "llms-full.txt",
    "sitemap.xml",
    "robots.txt",
]
# Pages people open to look something up, not from a search about a server: their <title> may
# leave out Plex, Emby and Jellyfin.
REFERENCE_PAGES = {
    "faq/index.html",
    "getting-started/index.html",
    "guides/index.html",
    "guides/previews-readiness/index.html",
    "reference/index.html",
    "credits/index.html",
}
# kramdown's typographic rewrites (curly quotes, dashes, ellipsis). GitHub never makes them, so a
# built page may not contain more of any of these than its Markdown source does.
TYPOGRAPHY = "“”‘’–—…"

PLUGIN_CASES = {
    "marker_alone": "<blockquote>\n  <p>[!NOTE]</p>\n  <p>Body.</p>\n</blockquote>\n",
    "marker_inline": "<blockquote>\n  <p>[!TIP]\nBody on the same paragraph.</p>\n</blockquote>\n",
    "nested": (
        "<blockquote>\n  <p>[!WARNING]\nOuter.</p>\n  <blockquote>\n    <p>Inner quote.</p>\n"
        "  </blockquote>\n  <p>After.</p>\n</blockquote>\n<p>Outside.</p>\n"
    ),
    "plain_quote": "<blockquote>\n  <p>Just a quote.</p>\n</blockquote>\n",
    "two_alerts": (
        "<blockquote>\n  <p>[!NOTE]\nOne.</p>\n</blockquote>\n<blockquote>\n  <p>[!CAUTION]\nTwo.</p>\n</blockquote>\n"
    ),
}


def strip_front_matter(text: str) -> str:
    return FRONT_MATTER.sub("", text, count=1)


def front_matter(path: Path) -> dict:
    match = FRONT_MATTER.match(path.read_text(encoding="utf-8"))
    return (yaml.safe_load(match.group(1)) or {}) if match else {}


def built_page(site: Path, source: Path) -> Path:
    relative = source.relative_to(DOCS_DIR).with_suffix("").as_posix()
    return site / "index.html" if relative == "index" else site / relative / "index.html"


def faq_data() -> list[dict]:
    return yaml.safe_load((DOCS_DIR / "_data" / "faq.yml").read_text(encoding="utf-8"))


def _exists(site: Path, relative: str) -> bool:
    if relative == "":
        return (site / "index.html").is_file()
    if relative.endswith("/"):
        return (site / relative / "index.html").is_file()
    return (site / relative).is_file()


@dataclass
class Head:
    title: str = ""
    canonical: str | None = None
    description: str | None = None
    og_image: str | None = None
    og_url: str | None = None
    twitter_card: str | None = None
    google_verification: str | None = None
    json_ld: list[dict] = field(default_factory=list)


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.head = Head()
        self._in_title = False
        self._json: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "link" and attr.get("rel") == "canonical":
            self.head.canonical = attr.get("href")
        elif tag == "meta":
            key, value = attr.get("name") or attr.get("property"), attr.get("content")
            if key == "description":
                self.head.description = value
            elif key == "og:image":
                self.head.og_image = value
            elif key == "og:url":
                self.head.og_url = value
            elif key == "twitter:card":
                self.head.twitter_card = value
            elif key == "google-site-verification":
                self.head.google_verification = value
        elif tag == "script" and attr.get("type") == "application/ld+json":
            self._json = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.head.title += data
        if self._json is not None:
            self._json.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._json is not None:
            self.head.json_ld.append(json.loads("".join(self._json)))
            self._json = None


def parse_head(path: Path) -> Head:
    parser = _HeadParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.head


def json_ld_types(block: dict) -> set[str]:
    declared = block.get("@type")
    return set(declared) if isinstance(declared, list) else {declared}


@pytest.fixture(scope="session")
def site(tmp_path_factory: pytest.TempPathFactory, worker_id: str) -> Path:
    shared = tmp_path_factory.getbasetemp()
    if worker_id != "master":
        shared = shared.parent  # one folder for every xdist worker, so the site is built once
    out = shared / "docs-site"
    log = shared / "docs-site.log"
    with FileLock(str(shared / "docs-site.lock")):
        if not log.exists():
            result = build_site(out)
            log.write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode != 0:
                pytest.fail(f"jekyll build failed:\n{result.stdout}\n{result.stderr}")
        elif not (out / "index.html").is_file():
            pytest.fail(f"an earlier jekyll build failed; its output is in {log}")
    return out


@pytest.fixture(scope="session")
def build_output(site: Path) -> str:
    return (site.parent / "docs-site.log").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def pages(site: Path) -> dict[str, Head]:
    # 404.html is served with a 404 status and has no canonical URL.
    return {
        path.relative_to(site).as_posix(): parse_head(path)
        for path in sorted(site.rglob("*.html"))
        if path.name != "404.html"
    }


class TestBuild:
    def test_build_emits_no_warnings(self, build_output: str) -> None:
        # kramdown's show_warnings is on, so an unclosed tag or a broken table lands here instead
        # of rendering wrong without a word.
        noisy = [line for line in build_output.splitlines() if re.search(r"(?i)warn|deprecat|error", line)]
        assert noisy == []

    def test_every_legacy_url_still_resolves(self, site: Path) -> None:
        assert [path for path in LEGACY_PATHS if not _exists(site, path)] == []

    def test_every_published_source_becomes_a_page(self, site: Path) -> None:
        missing = [s.relative_to(DOCS_DIR).as_posix() for s in published_sources() if not built_page(site, s).is_file()]
        assert missing == []

    def test_design_docs_hub_and_toolchain_files_are_not_published(self, site: Path) -> None:
        for name in ("design", "README", "README.html", "README.md", "Gemfile", "Gemfile.lock", ".ruby-version", "_plugins"):
            assert not (site / name).exists(), name

    def test_docs_build_never_emits_jellyfin_manifest(self, site: Path) -> None:
        # docs.yml adds the LIVE manifest at deploy time; a copy built from docs/ would shadow it.
        assert not (site / "jellyfin-plugin" / "manifest.json").exists()

    def test_llms_files_are_served_exactly_as_committed(self, site: Path) -> None:
        for name in ("llms.txt", "llms-full.txt"):
            committed = strip_front_matter((DOCS_DIR / name).read_text(encoding="utf-8"))
            assert (site / name).read_text(encoding="utf-8") == committed, name

    def test_cname_names_the_configured_host(self, site: Path) -> None:
        assert (site / "CNAME").read_text(encoding="utf-8").strip() == CONFIG["url"].removeprefix("https://")


class TestGithubMarkdownParity:
    def test_alerts_render_as_callouts(self, site: Path) -> None:
        assert 'class="callout callout--important"' in (site / "getting-started" / "index.html").read_text(encoding="utf-8")
        leftovers = [
            path.relative_to(site).as_posix()
            for path in site.rglob("*.html")
            if re.search(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", path.read_text(encoding="utf-8"))
        ]
        assert leftovers == []

    def test_plugin_handles_nesting_neighbours_and_relative_paths(self, tmp_path: Path) -> None:
        cases = tmp_path / "cases.json"
        cases.write_text(json.dumps(PLUGIN_CASES), encoding="utf-8")
        code = (
            "cases = JSON.parse(File.read(ARGV[0])); "
            "out = cases.transform_values { |h| GithubMarkdown.convert_alerts(h) }; "
            'out["links_root"] = GithubMarkdown.absolutize(%q(<img src="images/a.webp"><a href="README.md#top">Docs</a> '
            '<a href="#x">x</a> <a href="https://e.x/README.md">e</a> <a href="/abs/">a</a>), ".", ""); '
            'out["links_nested"] = GithubMarkdown.absolutize(%q(<img src="../images/b.webp"><a href="../README.md">Docs</a> '
            '<a href="data.csv?x=1">d</a>), "guides", "/base"); '
            "puts JSON.generate(out)"
        )
        result = run_bundle(
            ["ruby", "-rjekyll", "-rjson", "-r./_plugins/github_markdown.rb", "-e", code, str(cases)], out_dir=tmp_path
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout.strip().splitlines()[-1])

        alone = out["marker_alone"]
        assert 'class="callout callout--note"' in alone
        assert '<p class="callout__label">Note</p><p>Body.</p>' in alone
        assert "<p></p>" not in alone and "[!" not in alone and "<blockquote" not in alone
        inline = out["marker_inline"]
        assert "callout--tip" in inline and "Body on the same paragraph." in inline and "<blockquote" not in inline
        nested = out["nested"]
        assert nested.count("<blockquote>") == 1 and nested.count("</blockquote>") == 1
        assert nested.index("Inner quote.") < nested.index("After.") < nested.rindex("</div></div>") < nested.index("Outside.")
        assert out["plain_quote"] == PLUGIN_CASES["plain_quote"]
        assert out["two_alerts"].count('class="callout ') == 2
        assert out["links_root"] == (
            '<img src="/images/a.webp"><a href="/#top">Docs</a> <a href="#x">x</a> '
            '<a href="https://e.x/README.md">e</a> <a href="/abs/">a</a>'
        )
        assert out["links_nested"] == '<img src="/base/images/b.webp"><a href="/base/">Docs</a> <a href="/base/guides/data.csv?x=1">d</a>'

    def test_no_list_or_table_is_swallowed_into_a_paragraph(self, site: Path) -> None:
        # A "- item" or "| a | b |" line with no blank line before it renders as literal text in a <p>.
        offenders = []
        for path in site.rglob("*.html"):
            for paragraph in PARAGRAPH.findall(path.read_text(encoding="utf-8")):
                for line in paragraph.splitlines():
                    line = line.strip()
                    if re.match(r"(?:[-*+]|\d+\.)\s+\S", line) or re.match(r"\|.*\|$", line):
                        offenders.append(f"{path.relative_to(site)}: {line[:80]}")
        assert offenders == []

    def test_literal_template_braces_survive_liquid(self, site: Path) -> None:
        assert "{{Item.Path}}" in (site / "multi-server" / "index.html").read_text(encoding="utf-8")
        assert "{{{args.inputFileObj._id}}}" in (site / "guides" / "index.html").read_text(encoding="utf-8")

    def test_pages_quoting_template_braces_opt_out_of_liquid(self) -> None:
        offenders = [
            source.relative_to(DOCS_DIR).as_posix()
            for source in published_sources()
            if re.search(r"\{\{|\{%", strip_front_matter(source.read_text(encoding="utf-8")))
            and front_matter(source).get("render_with_liquid") is not False
        ]
        assert offenders == []

    def test_relative_images_and_hub_links_point_at_site_paths(self, site: Path) -> None:
        prose = PROSE.search((site / "multi-server" / "index.html").read_text(encoding="utf-8")).group(1)
        sources = re.findall(r'<img[^>]+src="([^"]+)"', prose)
        assert sources and all(src.startswith("/images/") for src in sources)
        assert all((site / src.lstrip("/")).is_file() for src in sources)
        offenders = [
            path.relative_to(site).as_posix()
            for path in site.rglob("*.html")
            if re.search(r'href="(?!https?:)[^"]*README(?:\.md|/|\.html)', path.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_markdown_typography_is_left_as_written(self, site: Path) -> None:
        offenders = []
        for source in published_sources():
            if front_matter(source).get("layout") == "home":
                continue
            prose = PROSE.search(built_page(site, source).read_text(encoding="utf-8"))
            rendered = html.unescape(prose.group(1))
            raw = source.read_text(encoding="utf-8")
            offenders += [f"{source.name}: {char!r}" for char in TYPOGRAPHY if rendered.count(char) > raw.count(char)]
        assert offenders == []


class TestCrawlerFiles:
    def test_sitemap_lists_only_published_pages_on_the_site_host(self, site: Path) -> None:
        namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [loc.text for loc in ET.parse(site / "sitemap.xml").getroot().findall("sm:url/sm:loc", namespace)]
        assert SITE_ROOT in locs
        assert all(loc.startswith(SITE_ROOT) for loc in locs), locs
        assert [loc for loc in locs if re.search(r"design/|README|llms|search\.json|404", loc)] == []

    def test_robots_allows_everything_and_names_the_absolute_sitemap(self, site: Path) -> None:
        lines = (site / "robots.txt").read_text(encoding="utf-8").splitlines()
        assert "User-agent: *" in lines and "Allow: /" in lines
        assert f"Sitemap: {SITE_ROOT}sitemap.xml" in lines


class TestHeadTags:
    def test_every_page_has_its_own_canonical_description_and_social_card(self, pages: dict[str, Head]) -> None:
        assert pages
        site_description = " ".join(CONFIG["description"].split())
        for name, head in pages.items():
            expected = SITE_ROOT + ("" if name == "index.html" else name.removesuffix("index.html"))
            assert head.canonical == expected, name
            assert head.og_url == expected, name
            assert head.description and head.description.strip(), name
            assert head.description != site_description, name
            assert len(head.description) <= 155, name
            assert head.og_image == SITE_ROOT + "images/social-preview.jpg", name
            assert head.twitter_card == "summary_large_image", name

    def test_titles_are_unique_and_search_facing_ones_name_a_server(self, pages: dict[str, Head]) -> None:
        titles = [head.title for head in pages.values()]
        assert len(titles) == len(set(titles))
        for name, head in pages.items():
            if name not in REFERENCE_PAGES:
                assert re.search(r"Plex|Emby|Jellyfin", head.title), f"{name}: {head.title!r}"

    def test_home_describes_the_app_and_the_website(self, pages: dict[str, Head]) -> None:
        blocks = pages["index.html"].json_ld
        app = next(block for block in blocks if "SoftwareApplication" in json_ld_types(block))
        website = next(block for block in blocks if "WebSite" in json_ld_types(block))
        assert app["url"] == SITE_ROOT
        assert app["author"] == {"@type": "Person", "name": CONFIG["author"]["name"], "url": CONFIG["author"]["url"]}
        assert app["offers"]["price"] == "0"
        assert app["codeRepository"] == CONFIG["repo"]
        assert app["downloadUrl"] == CONFIG["docker_image_url"]
        assert website["url"] == SITE_ROOT

    def test_home_carries_the_search_console_token(self, pages: dict[str, Head]) -> None:
        assert pages["index.html"].google_verification == CONFIG["google_site_verification"]

    def test_docs_pages_carry_a_breadcrumb_ending_on_themselves(self, pages: dict[str, Head]) -> None:
        for name, head in pages.items():
            if name == "index.html":
                continue
            crumbs = [block for block in head.json_ld if "BreadcrumbList" in json_ld_types(block)]
            assert len(crumbs) == 1, name
            items = crumbs[0]["itemListElement"]
            assert [item["position"] for item in items] == list(range(1, len(items) + 1)), name
            assert items[0]["item"] == SITE_ROOT, name
            assert items[-1]["item"] == head.canonical, name
            assert all(item["name"] for item in items), name


class TestFaq:
    def test_faq_json_ld_matches_the_data_file(self, pages: dict[str, Head]) -> None:
        expected = [entry["q"] for entry in faq_data()]
        assert 6 <= len(expected) <= 8
        carriers = {
            name: [block for block in head.json_ld if "FAQPage" in json_ld_types(block)] for name, head in pages.items()
        }
        carriers = {name: blocks for name, blocks in carriers.items() if blocks}
        assert "faq/index.html" in carriers
        assert set(carriers) <= {"faq/index.html", "index.html"}
        for name, blocks in carriers.items():
            assert len(blocks) == 1, name
            entities = blocks[0]["mainEntity"]
            assert [entity["name"] for entity in entities] == expected, name
            assert all(entity["acceptedAnswer"]["text"].strip() for entity in entities), name

    def test_every_faq_data_question_is_a_heading_on_the_faq_page(self, site: Path) -> None:
        page = (site / "faq" / "index.html").read_text(encoding="utf-8")
        headings = {
            html.unescape(re.sub(r"<[^>]+>", "", heading)).strip()
            for heading in re.findall(r"<h[23][^>]*>(.*?)</h[23]>", page, re.DOTALL)
        }
        assert [entry["q"] for entry in faq_data() if entry["q"] not in headings] == []
```

- [ ] **Step 5: Run the new tests and watch them fail**

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_site.py tests/test_docs_workflow.py -x -q`
Expected: FAIL at import time with `FileNotFoundError: .../docs/_config.yml` (the module reads the config at import). `filelock` and `yaml` already import from the shared venv.

- [ ] **Step 6: Gemfile, Ruby version, lock file**

`docs/Gemfile`:

```ruby
# frozen_string_literal: true

# Built by .github/workflows/docs.yml with `bundle exec jekyll build`, not by Pages' own builder:
# every deploy also has to add the live Jellyfin plugin manifest. Exact versions are in Gemfile.lock.
source "https://rubygems.org"

gem "jekyll", "~> 4.4"
gem "kramdown-parser-gfm", "~> 1.1"

group :jekyll_plugins do
  gem "jekyll-relative-links", "~> 0.7"
  gem "jekyll-seo-tag", "~> 2.8"
  gem "jekyll-sitemap", "~> 1.4"
end
```

`docs/.ruby-version` contains the single line `3.4`.

Generate the lock in the Ruby image (no Ruby on the host):

```bash
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e BUNDLE_PATH=/tmp/bundle \
  -v "$PWD/docs:/docs" -w /docs ruby:3.4-bookworm \
  sh -c 'bundle lock --add-platform x86_64-linux && bundle install --quiet && bundle exec jekyll --version'
grep -A3 '^PLATFORMS' docs/Gemfile.lock; grep -E '^    (jekyll|jekyll-seo-tag|jekyll-sitemap|jekyll-relative-links|kramdown-parser-gfm) ' docs/Gemfile.lock
```

Expected: `jekyll 4.4.x`; `PLATFORMS` lists `x86_64-linux` (possibly also `x86_64-linux-gnu`); the five gems appear with exact versions; a `BUNDLED WITH` line at the end. The GitHub runner is x86_64 glibc, so these platforms cover CI.

- [ ] **Step 7: Write `docs/_config.yml`**

```yaml
title: Media Preview Generator
tagline: Preview thumbnails for Plex, Emby and Jellyfin, made on your GPU
description: >-
  Self-hosted, GPU-accelerated preview thumbnails for Plex, Emby and Jellyfin, made as soon as
  Sonarr or Radarr import a file. HDR tone-mapped, one Docker container.

# Where the site lives. Every link derives from these two through relative_url / absolute_url, so
# moving the site is these two lines and nothing else inside docs/. The Pages custom domain has been
# mediapreviewgenerator.dev since 2026-09-23 (certificate approved, HTTPS enforced); the old
# github.io project URLs 301 here, path for path.
url: https://mediapreviewgenerator.dev
baseurl: ""

repo: https://github.com/stevezau/media_preview_generator
author:
  name: Steven Adams
  url: https://github.com/stevezau

# The host in the landing page's fake address bars: the LAN address the app screenshots' fixture
# uses (tests/e2e/snapshots/readme_fixture.py APP_HOST:APP_PORT), with the real default port.
demo_host: 192.168.1.10:8080
# The Docker Hub account doubles the z (stevezzau); GitHub is stevezau. Don't "fix" it.
docker_image: stevezzau/media_preview_generator
docker_image_url: https://hub.docker.com/r/stevezzau/media_preview_generator

permalink: pretty

# og:image for every page. jekyll-seo-tag reads page.image, never site.image, hence defaults.
defaults:
  - scope:
      path: ""
    values:
      image: /images/social-preview.jpg
      layout: doc

# Search Console HTML-tag token. Public by design: it is a meta tag on every page.
google_site_verification: 4mF2CtkEhiwWMtZzzHyTfMPWE6WNR0OYHCcRjSjFmk8

# Reading order: drives the sidebar, prev/next, breadcrumbs, the footer and llms-full.txt.
# `header: true` puts a section in the top bar; `blurb` is its card text on the 404 page.
nav:
  - title: Getting started
    url: /getting-started/
    blurb: Docker install, GPU setup, mounts and the first run
    header: true
  - title: How it compares
    url: /comparison/
    blurb: Plex, Jellyfin and Emby built-in generation, side by side
    header: true
  - title: How-to
    url: /how-to/
    blurb: Slow Plex previews, GPUs, Jellyfin trickplay, Emby BIFs, Sonarr and Radarr, HDR
    header: true
    children:
      - title: Why Plex previews are slow
        url: /plex-preview-thumbnails-slow/
      - title: Plex previews with a GPU
        url: /plex-preview-thumbnails-gpu/
      - title: Faster Jellyfin trickplay
        url: /jellyfin-trickplay-gpu/
      - title: Emby BIF previews with a GPU
        url: /emby-bif-thumbnails-gpu/
      - title: Previews on Sonarr or Radarr import
        url: /sonarr-radarr-preview-thumbnails/
      - title: HDR and Dolby Vision previews
        url: /hdr-dolby-vision-thumbnails/
  - title: Guides
    url: /guides/
    blurb: The web UI, webhooks, schedules and troubleshooting
    header: true
    children:
      - title: Previews Readiness
        url: /guides/previews-readiness/
  - title: Multi-server
    url: /multi-server/
    blurb: Plex, Emby and Jellyfin from one instance
  - title: Reference
    url: /reference/
    blurb: Every setting, environment variable and API endpoint
    header: true
  - title: FAQ
    url: /faq/
    blurb: Windows, GPUs, speed, RAM and skipped files
    header: true

plugins:
  - jekyll-relative-links
  - jekyll-seo-tag
  - jekyll-sitemap

# The .md links these pages use for each other work on github.com; this rewrites them for the site.
relative_links:
  enabled: true
  collections: false

# The same pages are read on github.com, so render them the way GitHub does: no typographic
# rewrites (-- to an en dash, straight to curly quotes), which GitHub never makes.
kramdown:
  input: GFM
  hard_wrap: false
  gfm_quirks: [paragraph_end, no_auto_typographic]
  smart_quotes: [apos, apos, quot, quot]
  syntax_highlighter: rouge
  show_warnings: true

strict_front_matter: true
liquid:
  error_mode: strict
  strict_filters: true

exclude:
  - README.md
  - design/
  - Gemfile
  - Gemfile.lock
  - .ruby-version
  - vendor/
  - .bundle/
```

`docs/CNAME` contains the single line `mediapreviewgenerator.dev`. (Pages takes the domain from the repo setting for Actions deploys; the file documents it and costs nothing.)

- [ ] **Step 8: Port the theme from Shortlist**

Copy verbatim, then apply exactly these edits:

```bash
S=/home/data/workspace/shortlist/docs
mkdir -p docs/_layouts docs/_includes docs/assets/css docs/assets/js
cp "$S/_layouts/default.html" "$S/_layouts/doc.html" docs/_layouts/
cp "$S/_includes/nav.html" "$S/_includes/footer.html" docs/_includes/
cp "$S/assets/css/main.css" docs/assets/css/main.css
cp "$S/assets/js/site.js" docs/assets/js/site.js
cp "$S/404.html" "$S/robots.txt" "$S/search.json" docs/
```

`docs/_layouts/default.html`, `docs/404.html`, `docs/robots.txt`, `docs/search.json`: no edits.

`docs/_includes/head.html`: write new (Shortlist's JSON-LD is Shortlist's):

```html
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

{% seo %}

<link rel="icon" href="{{ '/assets/img/favicon.svg' | relative_url }}" type="image/svg+xml">
<link rel="icon" href="{{ '/assets/img/favicon-32.png' | relative_url }}" type="image/png" sizes="32x32">
<link rel="apple-touch-icon" href="{{ '/assets/img/apple-touch-icon.png' | relative_url }}">
<link rel="stylesheet" href="{{ '/assets/css/main.css' | relative_url }}">
<meta name="theme-color" content="#08080a" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#fcfcfb" media="(prefers-color-scheme: light)">

{%- comment -%}
Set the theme before first paint, so nobody sees the wrong palette flash on every page load.
{%- endcomment -%}
<script>
  (function () {
    try {
      var stored = localStorage.getItem("mpg-theme");
      var prefersLight = window.matchMedia("(prefers-color-scheme: light)").matches;
      document.documentElement.dataset.theme = stored || (prefersLight ? "light" : "dark");
    } catch (e) {
      document.documentElement.dataset.theme = "dark";
    }
  })();
</script>

{%- comment -%}
What this software is, for search engines and AI crawlers. jekyll-seo-tag adds WebSite (home) or
WebPage (elsewhere); this adds the application itself on every page.
{%- endcomment -%}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": ["SoftwareApplication", "SoftwareSourceCode"],
  "name": {{ site.title | jsonify }},
  "description": {{ site.description | strip_newlines | jsonify }},
  "url": {{ '/' | absolute_url | jsonify }},
  "applicationCategory": "MultimediaApplication",
  "applicationSubCategory": "Video preview thumbnail generator for Plex, Emby and Jellyfin",
  "operatingSystem": "Linux (Docker, amd64 and arm64)",
  "isAccessibleForFree": true,
  "offers": { "@type": "Offer", "price": "0", "priceCurrency": "USD" },
  "license": "https://opensource.org/licenses/MIT",
  "author": { "@type": "Person", "name": {{ site.author.name | jsonify }}, "url": {{ site.author.url | jsonify }} },
  "downloadUrl": {{ site.docker_image_url | jsonify }},
  "codeRepository": {{ site.repo | jsonify }},
  "softwareRequirements": "Docker; Plex Media Server, Emby Server, or Jellyfin 10.10 or newer",
  "featureList": [
    "Video preview thumbnails for Plex (BIF), Emby (BIF) and Jellyfin (trickplay)",
    "GPU decoding on NVIDIA, Intel and AMD, with automatic CPU fallback",
    "Starts per file from Sonarr, Radarr, Tdarr, media-server or custom webhooks",
    "Decodes each file once when several servers hold it",
    "Tone-maps HDR10, HLG and HDR10+ previews and uses the HDR10 layer of Dolby Vision 7 and 8",
    "Web UI and REST API"
  ]
}
</script>
```

`docs/_includes/nav.html` edits:
1. Brand block becomes:
   ```html
       <a class="brand" href="{{ '/' | relative_url }}">
         <img class="brand__mark" src="{{ '/assets/img/logo.svg' | relative_url }}" alt="" width="27" height="27">
         <span class="brand__name">Media Preview Generator</span>
       </a>
   ```
2. Inside the `{%- for item in site.nav -%}` loop of `.nav__links`, wrap the `<a class="nav__link" …>…</a>` in `{%- if item.header -%}` … `{%- endif -%}`.
3. Star pill: `aria-label` and `title` become `Star Media Preview Generator on GitHub`.

`docs/_includes/footer.html` edits:
1. Brand block: same markup as the nav brand above.
2. `.footer__blurb` text: `Makes the preview thumbnails Plex, Emby and Jellyfin show while you scrub, on your GPU, as each new file arrives.`
3. Both `'Plex how-to'` literals become `'How-to'`; in the long Liquid comment above them, replace "Plex how-to" with "How-to".
4. In the Project list, the "Ask a question" link becomes `{{ site.repo }}/discussions`.
5. The Related list becomes: Plex `https://www.plex.tv/`, Jellyfin `https://jellyfin.org/`, Emby `https://emby.media/`, Sonarr `https://sonarr.tv/`, Radarr `https://radarr.video/`, Shortlist `https://shortlistapp.dev/`.
6. `.footer__base` becomes:
   ```html
       <div class="footer__base">
         <span>MIT licensed · © {{ 'now' | date: "%Y" }} Steven Adams</span>
         <span>Not affiliated with Plex, Emby or Jellyfin. Their names are trademarks of their owners.</span>
       </div>
   ```

`docs/_layouts/doc.html` edits:
1. Sidebar: replace the line `{%- if item.children and page.url contains item.url -%}` with:
   ```liquid
             {%- assign open = false -%}
             {%- if page.url contains item.url -%}{%- assign open = true -%}{%- endif -%}
             {%- for child in item.children -%}{%- if page.url == child.url -%}{%- assign open = true -%}{%- endif -%}{%- endfor -%}
             {%- if item.children and open -%}
   ```
   (The How-to children live at top-level URLs, so "page.url contains item.url" alone never opens that section on its own pages.)
2. In `.prose__head`, after the description line, add:
   ```liquid
         {%- if page.facts_checked %}<p class="prose__facts">Facts checked {{ page.facts_checked | date: "%-d %B %Y" }}.</p>{% endif -%}
   ```
3. Append at the end of the file:
   ```liquid
   {%- if page.faq_schema -%}{% include faq-jsonld.html %}{%- endif -%}
   ```

`docs/assets/css/main.css` edits:
1. First comment: "Shortlist docs" becomes "Media Preview Generator docs"; leave the rest of that comment.
2. Delete the whole section headed `/* ------------------------------------------------------ row templates -- */` up to (not including) the `/* --------------------------------------------------------- pipeline -- */` header, and the whole section headed `/* ------------------------------------------------- privacy-order flow -- */` up to (not including) `/* -------------------------------------------------------- closing call -- */`. Check: `grep -nE "\.template|\.flow" docs/assets/css/main.css` prints nothing.
3. Add `--bad: #f87171;` after `--warn` in the `:root, :root[data-theme="dark"]` block, and `--bad: #b91c1c;` in the `:root[data-theme="light"]` block (if that block has no `--warn`, add `--warn: #b45309;` too).
4. Append:
   ```css
   /* ------------------------------------------------ GitHub alert callouts -- */
   /* docs/_plugins/github_markdown.rb turns `> [!NOTE]` and friends into these boxes. The label says
      which kind it is in words; the colour only repeats it. */
   .callout__label {
     margin: 0 0 0.3rem;
     font-size: 0.8rem;
     font-weight: 700;
     letter-spacing: 0.02em;
     text-transform: uppercase;
     color: var(--accent-text);
   }
   .prose .callout {
     margin: 1.4rem 0;
   }
   .callout--warning {
     border-left-color: var(--warn);
   }
   .callout--warning svg,
   .callout--warning .callout__label {
     color: var(--warn);
   }
   .callout--caution {
     border-left-color: var(--bad);
   }
   .callout--caution svg,
   .callout--caution .callout__label {
     color: var(--bad);
   }

   /* "Facts checked" date under a page's heading, rendered from front matter. */
   .prose__facts {
     margin-top: 0.5rem;
     color: var(--muted);
     font-size: 0.85rem;
   }

   /* Three words of brand name push the nav buttons off the bar on a phone. */
   @media (max-width: 520px) {
     .nav .brand__name {
       display: none;
     }
   }
   ```

`docs/assets/js/site.js` edits: first comment "Shortlist docs" becomes "Media Preview Generator docs"; `"shortlist-theme"` becomes `"mpg-theme"`. Check: `grep -rni shortlist docs/_layouts docs/_includes docs/assets` prints only the footer's Related link to shortlistapp.dev.

- [ ] **Step 9: Write the Markdown plugin `docs/_plugins/github_markdown.rb`**

```ruby
# frozen_string_literal: true

require "pathname"

# Keeps the GitHub-flavoured Markdown in docs/ reading the same on the site as on github.com.
#
# 1. GitHub alerts (`> [!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]`, `[!CAUTION]`) become the
#    theme's .callout boxes. kramdown renders an alert as a plain blockquote whose first paragraph
#    starts with the literal marker, so this works on the converted HTML: find that blockquote, find
#    its matching close tag (blockquotes nest), and swap the wrapper.
# 2. Relative src/href values are resolved from the page's own source folder into site paths.
#    Pretty permalinks put getting-started.md at /getting-started/, where a relative
#    "images/x.webp" would otherwise load /getting-started/images/x.webp. Links to docs/README.md
#    (the GitHub docs hub, excluded from the site) point at the site home instead.
#
# Runs on :post_convert: after Markdown -> HTML, before the layout, so layouts are never touched.
module GithubMarkdown
  ALERT_START = %r{<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(?:<br\s*/?>)?\s*}
  ICONS = {
    "NOTE" => '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5M12 8h.01"/>',
    "TIP" => '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3Z"/>',
    "IMPORTANT" => '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"/><path d="M12 7v4M12 14h.01"/>',
    "WARNING" => '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>' \
                 '<path d="M12 9v4M12 17h.01"/>',
    "CAUTION" => '<path d="M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9Z"/><path d="M12 8v4M12 16h.01"/>'
  }.freeze
  # src/href values that are relative paths: no scheme, not site-absolute, not a same-page anchor.
  RELATIVE_ATTR = /\b(src|href)="(?![a-z][a-z0-9+.-]*:|\/|#)([^"]*)"/i
  CLOSE = "</blockquote>"

  module_function

  def convert_alerts(html)
    out = +""
    pos = 0
    while (match = ALERT_START.match(html, pos))
      close = matching_close(html, match.end(0))
      raise "GitHub alert without a closing </blockquote> near #{html[match.begin(0), 80].inspect}" unless close

      body = "<p>#{html[match.end(0)...close]}".sub(%r{\A<p>\s*</p>\s*}, "")
      out << html[pos...match.begin(0)] << callout(match[1], body)
      pos = close + CLOSE.length
    end
    out << html[pos..]
  end

  def matching_close(html, from)
    depth = 1
    cursor = from
    loop do
      next_close = html.index(CLOSE, cursor)
      return nil unless next_close

      next_open = html.index("<blockquote", cursor)
      if next_open && next_open < next_close
        depth += 1
        cursor = next_open + 1
      else
        depth -= 1
        return next_close if depth.zero?

        cursor = next_close + 1
      end
    end
  end

  def callout(kind, body)
    %(<div class="callout callout--#{kind.downcase}" role="note">) +
      %(<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">#{ICONS.fetch(kind)}</svg>) +
      %(<div><p class="callout__label">#{kind.capitalize}</p>#{body}</div></div>)
  end

  def absolutize(html, page_dir, baseurl)
    html.gsub(RELATIVE_ATTR) do
      attr = Regexp.last_match(1)
      target = Regexp.last_match(2)
      next Regexp.last_match(0) if target.empty?

      path, separator, rest = target.partition(/[?#]/)
      resolved = Pathname.new("/#{page_dir}").join(path).cleanpath.to_s
      resolved = "/" if resolved == "/README.md"
      %(#{attr}="#{baseurl}#{resolved}#{separator}#{rest}")
    end
  end
end

Jekyll::Hooks.register :pages, :post_convert do |page|
  next unless page.ext == ".md"

  page_dir = File.dirname(page.relative_path)
  page.content = GithubMarkdown.absolutize(GithubMarkdown.convert_alerts(page.content), page_dir, page.site.baseurl.to_s)
end
```

- [ ] **Step 10: FAQ data and its JSON-LD include**

`docs/_includes/faq-jsonld.html`:

```liquid
{%- comment -%}
FAQPage structured data from _data/faq.yml, the file the landing page's FAQ renders, so the markup
and the visible questions can't drift. Every q in faq.yml must also be a heading on /faq/ (tested).
Google retired the FAQ rich result; this stays because AI crawlers and other indexes read
schema.org types to work out what this software does. It is not ranking work.
{%- endcomment -%}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {%- for q in site.data.faq -%}
    {
      "@type": "Question",
      "name": {{ q.q | jsonify }},
      "acceptedAnswer": { "@type": "Answer", "text": {{ q.a | markdownify | strip_html | normalize_whitespace | strip | jsonify }} }
    }{%- unless forloop.last -%},{%- endunless -%}
    {%- endfor -%}
  ]
}
</script>
```

`docs/_data/faq.yml`: seeded with six questions that are already `###` headings in `docs/faq.md`, answers copied from that page (rewording into searched questions is Task 9's job). Relative `.md` links become site paths, because jekyll-relative-links doesn't rewrite data files:

```yaml
# Short FAQ: the landing page shows these, and they are the FAQPage structured data on / and /faq/.
# Every q here must also be a heading on docs/faq.md (tests/test_docs_site.py checks it), so the
# visible FAQ and the structured data can't disagree. Keep 6-8 entries. Links are site paths
# (/getting-started/), not .md files: data files aren't rewritten by jekyll-relative-links.
- q: What does this tool do?
  a: >-
    Generates video preview thumbnails for **Plex, Emby, and Jellyfin** — alone or in any combination.
    These are the small images you see when scrubbing through videos. It runs preview generation off
    the media server, on a machine of your choosing, using every GPU it finds. When two or more of
    your servers contain the same file, FFmpeg runs only once and the output is written in each
    server's expected format (Plex stores it as a **BIF** bundle, Emby reads a **BIF** sidecar file
    next to the video, Jellyfin reads a folder of JPG tiles called **trickplay**).
- q: Does this work on Windows?
  a: >-
    Yes — run the Docker image on Docker Desktop with the WSL2 backend. If you have an **NVIDIA** GPU
    it is accelerated: the NVIDIA Windows driver exposes CUDA and NVDEC into WSL2, so `--gpus all`
    works much as it does on Linux (best-effort — WSL2 GPU detection is less reliable than native
    Linux). **AMD and Intel** GPUs are not accelerated under Docker (D3D11VA can't be reached from
    Docker's Linux VM), so those setups process on CPU — raise **CPU Workers** in Settings, or run the
    container on a Linux host. There's no separate Windows native build. See
    [Getting Started — Windows](/getting-started/#windows).
- q: Can I use this without a GPU?
  a: >-
    Yes. In **Settings** → **Processing Options**, disable all GPUs (or set workers to 0) and set
    **CPU Workers** to your desired value (e.g. `4` or `8`).
- q: Is Docker required? Is there a standalone .exe?
  a: >-
    Docker is required. There is no standalone executable and no from-source install path — the
    container bundles the FFmpeg build and codec support the app depends on, so Docker is the only
    supported deployment. See [Getting Started](/getting-started/) for setup. It runs on Linux,
    Windows (Docker Desktop, WSL2 backend), macOS, Unraid, Synology, and anywhere else Docker runs.
- q: Can I run this on a different machine than my media server(s)?
  a: >-
    Yes. The tool can run anywhere that can reach your servers' APIs over the network. For Plex you
    also need access to the Plex config directory (NFS, SMB, shared volume, etc.); for Emby/Jellyfin
    you just need the media files visible. See [Networking](/getting-started/#networking) for setup
    details.
- q: Does this work with Jellyfin or Emby?
  a: >-
    Yes. The app supports Plex, Emby, and Jellyfin — alone or in any combination. Each server is added
    under **Settings → Media Servers**. When two or more servers contain the same file, FFmpeg runs
    only once and the result is written in each server's expected format (Plex stores it as a BIF
    bundle, Emby reads a BIF sidecar file next to the video, Jellyfin reads a folder of JPG tiles
    called trickplay). See the [Multi-Server guide](/multi-server/) for setup, webhook routing, and
    per-server library/exclude rules.
```

The two anchors exist today: `docs/getting-started.md` has `### Windows` (line 313) and `## Networking` (line 525), kramdown ids `windows` and `networking`. Nothing tests anchors inside data files, so re-check them if those headings are ever renamed.

- [ ] **Step 11: Convert every page's front matter (one-off script, not committed)**

```bash
/home/data/.venv/bin/python - <<'PY'
import re
from pathlib import Path

import yaml

DOCS = Path("docs")
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
for page in sorted(DOCS.rglob("*.md")):
    parts = page.relative_to(DOCS).parts
    if parts[0] == "design" or page == DOCS / "README.md" or page.name == "how-to.md":
        continue
    text = page.read_text(encoding="utf-8")
    match = FRONT.match(text)
    front = (yaml.safe_load(match.group(1)) or {}) if match else {}
    lines = (text[match.end():] if match else text).split("\n")
    in_fence, heading = False, None
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("# "):
            heading = line[2:].strip()
            del lines[i]
            while i < len(lines) and not lines[i].strip():
                del lines[i]
            break
    if heading is None:
        raise SystemExit(f"{page}: no H1")
    front.pop("hide", None)
    front.setdefault("title", heading)  # the five reference pages keep MkDocs's H1-as-title
    front["heading"] = heading
    body = "\n".join(lines).lstrip("\n")
    if "{{" in body or "{%" in body:
        front["render_with_liquid"] = False
    order = ["title", "heading", "description", "facts_checked"]
    ordered = {k: front[k] for k in order if k in front} | {k: v for k, v in front.items() if k not in order}
    dumped = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=120)
    page.write_text(f"---\n{dumped}---\n\n{body}", encoding="utf-8")
    print(f"{page}: heading={heading!r} liquid_off={'render_with_liquid' in ordered}")
PY
git diff --stat -- docs
```

Expected: 14 pages printed; `liquid_off=True` for exactly `docs/guides.md` and `docs/multi-server.md`; `hide:` gone from `comparison.md`. Then add `faq_schema: true` to the front matter of `docs/faq.md` by hand.

- [ ] **Step 12: The how-to hub, the llms files, and the MkDocs removal**

`docs/how-to.md`:

```markdown
---
title: Plex, Jellyfin and Emby preview thumbnail how-to guides
heading: How-to
description: Short guides for slow Plex previews, GPU thumbnails for Plex and Emby, faster Jellyfin trickplay, Sonarr and Radarr triggers, and HDR previews.
---

Each guide starts from one problem people search for, then shows the setting or the container change that fixes it.

- [Why Plex video preview thumbnails take so long](plex-preview-thumbnails-slow.md)
- [Generate Plex preview thumbnails with a GPU](plex-preview-thumbnails-gpu.md)
- [Faster Jellyfin trickplay generation](jellyfin-trickplay-gpu.md)
- [Emby preview thumbnails (BIF) with GPU acceleration](emby-bif-thumbnails-gpu.md)
- [Previews as soon as Sonarr or Radarr imports a file](sonarr-radarr-preview-thumbnails.md)
- [HDR and Dolby Vision preview thumbnails](hdr-dolby-vision-thumbnails.md)
```

Move the llms files into the site and point their URLs at the site host:

```bash
git mv llms.txt docs/llms.txt
git mv llms-full.txt docs/llms-full.txt
for f in docs/llms.txt docs/llms-full.txt; do
  permalink="/$(basename "$f")"
  { printf -- '---\nlayout: null\nsitemap: false\npermalink: %s\nrender_with_liquid: false\n---\n' "$permalink"; cat "$f"; } > "$f.new" && mv "$f.new" "$f"
  sed -i 's#https://stevezau.github.io/media_preview_generator/#https://mediapreviewgenerator.dev/#g' "$f"
done
head -7 docs/llms.txt
```

Expected: the front matter block, then `# Media Preview Generator`.

Remove MkDocs:

```bash
git rm -r -q mkdocs.yml docs_theme docs/stylesheets/extra.css scripts/generate_llms_full.py tests/test_llms_full.py
```

`pyproject.toml`: delete the `docs = [...]` extra and the two comment lines above it; in the `test` extra replace the two lines

```toml
    # tests/test_docs_site.py builds the docs site; CI installs only `.[test]`.
    "media-preview-generator[docs]",
```

with

```toml
    # tests/test_docs_site.py builds the Jekyll site (docs/) through tests/docs_toolchain.py and
    # shares one build across xdist workers with a file lock; the llms-full generator reads YAML.
    "filelock>=3.12",
    "pyyaml>=6.0",
```

`.pre-commit-config.yaml`: under `check-yaml`, delete the two comment lines and `exclude: ^mkdocs\.yml$`.

`.gitignore`: after the existing `site/` line add:

```
docs/_site/
docs/.jekyll-cache/
docs/.bundle/
docs/vendor/
```

- [ ] **Step 13: Build and iterate until the site tests pass**

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_site.py -q`
Expected on first run: failures to fix at the source, never by loosening a test:
- `test_build_emits_no_warnings`: kramdown warnings name the file and line; fix the Markdown (usually a raw HTML block missing a blank line, or `<` in prose that needs `&lt;` or backticks).
- `test_no_list_or_table_is_swallowed_into_a_paragraph`: add the missing blank line before the list/table in the named page.
- `test_markdown_typography_is_left_as_written`: if curly quotes still appear, the `smart_quotes` line in `_config.yml` is not taking effect; fix the config, don't edit pages.
- `test_every_faq_data_question_is_a_heading_on_the_faq_page`: a character differs between `faq.yml` and the `###` heading; copy the heading again.
- A Ruby line like `warning: logger was loaded from the standard library` means a default gem must be listed: add `gem "logger"` (or the gem it names) to `docs/Gemfile` and re-run Step 6's lock command.
Re-run until: `23 passed`, no skips.

- [ ] **Step 14: Swap the build in `.github/workflows/docs.yml` and wire CI**

In `docs.yml`, change the push `paths:` list to:

```yaml
    paths:
      - "docs/**"
      - ".github/workflows/docs.yml"
```

Replace the steps from `Setup Python` through `Build site` with:

```yaml
      - name: Setup Python
        # Only for the manifest validation below; the site itself is Jekyll.
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Setup Ruby
        # Ruby version from docs/.ruby-version; gems from docs/Gemfile.lock, cached.
        uses: ruby/setup-ruby@v1
        with:
          working-directory: docs
          bundler-cache: true

      - name: Build site
        working-directory: docs
        env:
          JEKYLL_ENV: production
        run: bundle exec jekyll build --destination ../site
```

Everything from `Download plugin manifest artifact` to the end of the file stays byte-for-byte the same. Check: `git diff -U0 .github/workflows/docs.yml` shows changes only in the header comment's first lines (if any), `paths:`, and the three steps above.

In `.github/workflows/ci.yml`, job `test`: after `Set up Python` add

```yaml
      - name: Set up Ruby (tests/test_docs_site.py builds the Jekyll site)
        uses: ruby/setup-ruby@v1
        with:
          working-directory: docs
          bundler-cache: true
```

and give `Run tests` an `env:` block with `MPG_REQUIRE_DOCS_BUILD: "1"`. In job `lint`, add after `Ruff format check`:

```yaml
      - name: actionlint (Pages workflows)
        uses: docker://rhysd/actionlint:1.7.7
        with:
          args: -color .github/workflows/docs.yml .github/workflows/jellyfin-plugin.yml
```

`ci.yml` skips docs-only changes (`paths-ignore: docs/**, *.md`), which are the changes that break the docs tests, so add `.github/workflows/docs-check.yml`:

```yaml
name: Docs check

# ci.yml ignores docs-only changes (paths-ignore: docs/**, *.md), which are exactly the changes that
# break the docs tests. This runs just those tests, with Ruby for the Jekyll build. The -k expression
# picks tests by module name, so a new tests/test_docs_*.py (or llms/site_claims/brand/readmes/
# benchmark_summary/site_urls) module is included without editing this file.
on:
  pull_request:
    branches: [main, dev]
    paths:
      - "docs/**"
      - "*.md"
      - "unraid-templates/**"
      - "scripts/generate_llms_full.py"
      - "scripts/benchmark_previews.py"
      - "tests/docs_toolchain.py"
      - "tests/test_*.py"
      - ".github/workflows/docs*.yml"
      - ".github/workflows/jellyfin-plugin.yml"
  push:
    branches: [dev]
    paths:
      - "docs/**"
      - "*.md"
      - "unraid-templates/**"
      - "scripts/generate_llms_full.py"
      - "scripts/benchmark_previews.py"
      - "tests/docs_toolchain.py"
      - "tests/test_*.py"
      - ".github/workflows/docs*.yml"
      - ".github/workflows/jellyfin-plugin.yml"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  docs-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # setuptools-scm needs full history

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: pyproject.toml

      - name: Set up Ruby
        uses: ruby/setup-ruby@v1
        with:
          working-directory: docs
          bundler-cache: true

      - name: Install dependencies
        run: pip install -e ".[test]"

      - name: Docs tests
        env:
          MPG_REQUIRE_DOCS_BUILD: "1"
        run: >-
          pytest --no-cov -n 0
          -k "docs or llms or site_claims or brand or readmes or benchmark_summary or site_urls or verify_live"
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_workflow.py -q`
Expected: `11 passed`. If actionlint reports shellcheck findings in `docs.yml` or `jellyfin-plugin.yml`, fix the script lines it names.

- [ ] **Step 15: Update the contributor docs**

`CONTRIBUTING.md`, section "Editing a Docs Page", becomes:

````markdown
### Editing a Docs Page

The docs site is Jekyll (`docs/_config.yml`, theme in `docs/_layouts`, `docs/_includes`, `docs/assets`).
Pages are plain GitHub-flavoured Markdown so they also read well on github.com: no Liquid in pages
(add `render_with_liquid: false` to a page that quotes `{{ }}` template syntax).

Preview it with Docker, no Ruby needed:

```bash
docker run --rm -it -p 4000:4000 -u "$(id -u):$(id -g)" -e HOME=/tmp -e BUNDLE_PATH=/tmp/bundle \
  -v "$PWD/docs:/docs" -w /docs ruby:3.4-bookworm \
  sh -c 'bundle install --quiet && bundle exec jekyll serve --host 0.0.0.0'
```

After changing anything under `docs/` (a page, `_data/*.yml`, or the `nav` in `_config.yml`),
regenerate `docs/llms-full.txt` (every docs page in one file, for AI agents):

```bash
python scripts/generate_llms_full.py         # writes docs/llms-full.txt
python scripts/generate_llms_full.py --check # CI-style: exits non-zero if it's stale
```

`tests/test_llms_full.py` fails the build if `docs/llms-full.txt` drifts. `tests/test_docs_site.py`
builds the real site (host Bundler, or the `ruby` Docker image when there is no Ruby).
````

`.claude/CLAUDE.md`, in the Commands block, the `# Docs` lines become:

```bash
# Docs (Jekyll, docs/) — after editing anything under docs/ (pages, _data/*.yml, nav), regenerate llms-full
python scripts/generate_llms_full.py            # writes docs/llms-full.txt
python scripts/generate_llms_full.py --check    # CI-style: non-zero exit if stale
pytest --no-cov -n 0 tests/test_docs_site.py    # builds the site via host Bundler or the ruby:3.4 image
```

(Both commands work from Task 2 on.)

- [ ] **Step 16: Rewrite `tests/test_docs_images.py` (needs lane C's Task 3 merged)**

```python
"""Image references across the README, Docker Hub README, docs, site templates and Unraid templates.

Two failures nothing else catches: a reference to an image that isn't there (Jekyll and GitHub both
render a broken picture without failing the build), and an image in docs/images/ or
docs/assets/img/ that nothing uses (dead weight, or a stray capture). Sources write references in
two shapes, each with its own pattern; one that missed a shape would hide a whole file's worth.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
IMAGE_DIRS = {"images": DOCS / "images", "assets/img": DOCS / "assets" / "img"}
EXT = r"(?:png|jpe?g|webp|svg|gif|ico)"
# images/x.webp or assets/img/x.svg after any prefix: Markdown, HTML src/href, Liquid
# '/images/x.webp' | relative_url, and raw.githubusercontent.com / github.com URLs.
PATH_REF = re.compile(rf"(?<![\w-])(images|assets/img)/([A-Za-z0-9._-]+\.{EXT})\b")
# _data/*.yml names a bare file that the layout prefixes with /images/: `image: tour-extract.webp`.
DATA_REF = re.compile(rf"^\s*-?\s*image:\s*['\"]?([A-Za-z0-9._-]+\.{EXT})['\"]?\s*$", re.MULTILINE)
MAX_BYTES = 500 * 1024  # .pre-commit-config.yaml check-added-large-files --maxkb=500


def _sources() -> list[Path]:
    fixed = [REPO_ROOT / "README.md", REPO_ROOT / "DOCKERHUB_README.md", DOCS / "_config.yml", DOCS / "404.html"]
    globbed = [
        *DOCS.glob("_data/*.yml"),
        *DOCS.glob("_layouts/*.html"),
        *DOCS.glob("_includes/*.html"),
        *(REPO_ROOT / "unraid-templates").glob("*.xml"),
        *(p for p in DOCS.rglob("*.md") if not {"design", "vendor"} & set(p.relative_to(DOCS).parts)),
    ]
    return sorted({path for path in fixed + globbed if path.is_file()})


def _references(source: Path) -> set[Path]:
    text = source.read_text(encoding="utf-8", errors="ignore")
    refs = {IMAGE_DIRS[folder] / name for folder, name in PATH_REF.findall(text)}
    if source.parent.name == "_data":
        refs |= {IMAGE_DIRS["images"] / name for name in DATA_REF.findall(text)}
    return refs


def _committed_images() -> list[Path]:
    return sorted(path for folder in IMAGE_DIRS.values() for path in folder.iterdir() if path.is_file())


@pytest.mark.parametrize("source", _sources(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_every_referenced_image_exists(source: Path) -> None:
    missing = sorted(p.relative_to(REPO_ROOT).as_posix() for p in _references(source) if not p.is_file())
    assert not missing, f"{source.relative_to(REPO_ROOT)} points at images that don't exist: {missing}"


def test_no_committed_image_is_unreferenced() -> None:
    referenced = set().union(*(_references(source) for source in _sources()))
    orphans = [p.relative_to(REPO_ROOT).as_posix() for p in _committed_images() if p not in referenced]
    assert not orphans, f"committed but referenced nowhere: {orphans}"


def test_committed_images_fit_under_the_pre_commit_cap() -> None:
    big = [f"{p.relative_to(REPO_ROOT)} ({p.stat().st_size // 1024} KB)" for p in _committed_images() if p.stat().st_size > MAX_BYTES]
    assert not big


def test_social_preview_is_a_2to1_baseline_jpeg() -> None:
    with Image.open(DOCS / "images" / "social-preview.jpg") as image:
        assert image.format == "JPEG"
        assert image.width == 2 * image.height
        assert not image.info.get("progressive") and not image.info.get("progression")
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_images.py -q`
Expected: all pass once Task 3 is merged (logo, favicons and `docs/images/icon.png` referenced by `head.html`, `nav.html`, README and the Unraid templates). A failure naming `docs/assets/img/*` means Task 3 isn't merged yet: merge lane C first. (The old version of this test scanned `mkdocs.yml` and `docs_theme/`, which are gone, so it can't stay as it was.)

- [ ] **Step 17: Full suite and a look at the result**

Dispatch `verifier`: "From `/home/data/orca/workspaces/plex_generate_vid_previews/discover`, run `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest` and report pass/fail counts and failures only." Expected: all pass (the docs tests share one build).

Serve and screenshot three pages for the reviewer (scratchpad, not committed):

```bash
docker run -d --rm --name mpg-docs-preview -p 127.0.0.1:4000:4000 -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -e BUNDLE_PATH=/bundle -v "$HOME/.cache/mpg-docs-bundle:/bundle" -v "$PWD/docs:/docs" -w /docs ruby:3.4-bookworm \
  sh -c 'bundle install --quiet && bundle exec jekyll serve --host 0.0.0.0 --disable-disk-cache'
sleep 20
/home/data/.venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright
out = "/tmp/claude-1000/-home-data-orca-workspaces-plex-generate-vid-previews-discover/051f57ad-9232-4c9d-9878-c5ef2df9e88f/scratchpad"
with sync_playwright() as p:
    b = p.chromium.launch()
    for path, name in [("/getting-started/", "t1-getting-started"), ("/faq/", "t1-faq"), ("/nope/", "t1-404")]:
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(f"http://127.0.0.1:4000{path}")
        pg.screenshot(path=f"{out}/{name}.png", full_page=False)
    b.close()
PY
docker stop mpg-docs-preview
```

Expected: the Shortlist-style dark theme, sidebar with How-to collapsed, an amber "IMPORTANT" callout near the top of Getting started, and 404 nav cards.

- [ ] **Step 18: Deep review, then commit**

```bash
git add docs/Gemfile docs/Gemfile.lock docs/.ruby-version docs/_config.yml docs/CNAME docs/_layouts docs/_includes \
  docs/_plugins docs/_data/faq.yml docs/assets/css docs/assets/js docs/404.html docs/robots.txt docs/search.json \
  docs/how-to.md docs/llms.txt docs/llms-full.txt docs/*.md docs/guides/previews-readiness.md \
  tests/docs_toolchain.py tests/test_docs_site.py tests/test_docs_workflow.py tests/test_docs_images.py \
  .github/workflows/docs.yml .github/workflows/docs-check.yml .github/workflows/ci.yml pyproject.toml .pre-commit-config.yaml .gitignore \
  CONTRIBUTING.md .claude/CLAUDE.md
git status --short   # the git rm/git mv entries are already staged
```

Dispatch `Architecture Review` on the staged diff, plus an opus reviewer with this brief: "Read `.github/workflows/docs.yml` before and after (`git diff --cached`). Confirm nothing after `Build site` changed, the manifest step still fails closed, `jellyfin-plugin.yml` still calls this workflow, and `site/` is still what gets uploaded." Fix HIGH findings, then:

```bash
git commit -F - <<'EOF'
feat(docs): move the docs site from MkDocs to a Jekyll port of the Shortlist theme

Jekyll builds in docs.yml from a pinned Gemfile.lock; the live Jellyfin manifest step and the
plugin-release call are unchanged. A small _plugins hook keeps GitHub alerts, relative images and
the docs hub link working on both github.com and the site.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 2: llms-full generator and the docs link test

Lane A, after Task 1. Suggested model: opus.

**Files:**
- Create: `scripts/generate_llms_full.py` (new, Jekyll-based), `tests/test_llms_full.py`, `tests/test_docs_links.py`
- Regenerate: `docs/llms-full.txt`
- Modify: any `docs/*.md` whose links or anchors the link test flags

**Interfaces:**
- Consumes: Task 1's `_config.yml` (`url`, `baseurl`, `title`, `nav`), `docs/llms.txt` (must keep a `## Docs` heading: the header is everything before it), `tests.test_docs_site.site` fixture.
- Produces: `scripts/generate_llms_full.py` with `DOCS_DIR: Path`, `OUTPUT_NAME = "llms-full.txt"`, `class LlmsFullError(RuntimeError)`, `load_config(docs_dir: Path) -> dict`, `site_root(config: dict) -> str`, `nav_urls(config: dict) -> list[str]`, `published_sources(docs_dir: Path) -> list[Path]`, `generate(docs_dir: Path = DOCS_DIR) -> str`, `main(argv: list[str] | None = None) -> int`. Output format: front matter, the llms.txt header, then one section per page: `# <title>` / `Source: <url>` / blank / description / optional `Facts checked: <d Month yyyy>.` / blank / body, sections joined by `\n\n---\n\n`. The index section is followed by the landing data (`_data/stats.yml`, `tour.yml`, `features.yml`, `works_with.yml`, `faq.yml`, each only if present) as Markdown.

- [ ] **Step 1: Write the failing llms-full tests `tests/test_llms_full.py`**

```python
"""docs/llms-full.txt: drift, where it publishes, and that every link in it is real.

scripts/generate_llms_full.py builds it from _config.yml's nav, the pages, and the landing page's
_data files. These fail the way a stale or broken file would bite an agent reading it: a section
missing, a link that 404s, Liquid it can't read, a data-file edit that never reached the file.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

from scripts.generate_llms_full import (
    DOCS_DIR,
    OUTPUT_NAME,
    LlmsFullError,
    generate,
    load_config,
    nav_urls,
    published_sources,
    site_root,
)

# Session fixtures reused from test_docs_site.py (one Jekyll build per run). A test parameter
# named `site` requests the fixture; the per-line noqa silences the "redefinition" warning.
from tests.test_docs_site import build_output, site  # noqa: F401

pytestmark = pytest.mark.timeout(900)

COMMITTED = DOCS_DIR / OUTPUT_NAME
ROOT = site_root(load_config(DOCS_DIR))
# docs.yml adds this at deploy time; a local build never has it.
DEPLOY_TIME_PATHS = {"jellyfin-plugin/manifest.json"}


@pytest.fixture(scope="module")
def generated() -> str:
    return generate()


def _docs_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "docs"
    shutil.copytree(DOCS_DIR, copy, ignore=shutil.ignore_patterns("design", "_site", ".jekyll-cache", "vendor", ".bundle"))
    return copy


class TestDrift:
    def test_committed_file_matches_generator_output(self, generated: str) -> None:
        assert COMMITTED.read_text(encoding="utf-8") == generated, (
            "docs/llms-full.txt is out of date. Regenerate with: python scripts/generate_llms_full.py"
        )

    def test_publishes_at_its_own_url_not_over_llms_txt(self) -> None:
        # The header is lifted from llms.txt, itself a page with `permalink: /llms.txt`. Copied with its
        # front matter, it would publish llms-full.txt ON TOP of llms.txt (Shortlist shipped exactly that).
        head = COMMITTED.read_text(encoding="utf-8")[:200]
        assert "permalink: /llms-full.txt" in head
        assert "permalink: /llms.txt" not in head
        assert "render_with_liquid: false" in head

    def test_editing_a_data_file_changes_the_output(self, tmp_path: Path) -> None:
        docs = _docs_copy(tmp_path)
        faq_path = docs / "_data" / "faq.yml"
        faq = yaml.safe_load(faq_path.read_text(encoding="utf-8"))
        faq[0]["q"] = "Sentinel question 7f3a?"
        faq_path.write_text(yaml.safe_dump(faq, sort_keys=False, allow_unicode=True), encoding="utf-8")
        assert "Sentinel question 7f3a?" in generate(docs)

    def test_a_page_missing_from_nav_is_an_error(self, tmp_path: Path) -> None:
        docs = _docs_copy(tmp_path)
        config_path = docs / "_config.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["nav"] = [entry for entry in config["nav"] if entry["url"] != "/faq/"]
        config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
        with pytest.raises(LlmsFullError, match="faq.md"):
            generate(docs)

    def test_a_dead_relative_link_is_an_error(self, tmp_path: Path) -> None:
        docs = _docs_copy(tmp_path)
        page = docs / "faq.md"
        page.write_text(page.read_text(encoding="utf-8") + "\n[gone](no-such-page.md)\n", encoding="utf-8")
        with pytest.raises(LlmsFullError, match="no-such-page.md"):
            generate(docs)


class TestContent:
    def test_every_published_page_appears_once_in_nav_order(self, generated: str) -> None:
        expected = [ROOT] + [ROOT + url.strip("/") + "/" for url in nav_urls(load_config(DOCS_DIR))]
        assert re.findall(r"^Source: (\S+)$", generated, re.MULTILINE) == expected
        assert len(expected) == len(published_sources(DOCS_DIR))

    def test_no_liquid_an_agent_cannot_read(self, generated: str) -> None:
        # Template syntax the docs QUOTE (Jellyfin's {{Item.Path}}, Tdarr's {{{args...}}}) is content.
        assert re.findall(r"\{%|\{\{\s*(?:site|page)\.", generated) == []

    def test_quoted_template_syntax_survives(self, generated: str) -> None:
        assert "{{Item.Path}}" in generated
        assert "{{{args.inputFileObj._id}}}" in generated

    def test_no_relative_links_remain(self, generated: str) -> None:
        targets = re.findall(r"\]\(([^)\s]+)\)", generated)
        assert [t for t in targets if not t.startswith(("http://", "https://", "mailto:"))] == []

    def test_no_markup_noise(self, generated: str) -> None:
        body = generated.split("\n---\n", 1)[1]  # everything after the file's own front matter
        assert "<!--" not in body
        assert '<a id="' not in body
        assert "application/ld+json" not in body

    def test_llms_txt_advertises_the_full_file(self) -> None:
        assert f"{ROOT}llms-full.txt" in (DOCS_DIR / "llms.txt").read_text(encoding="utf-8")

    def test_every_site_url_is_a_built_file(
        self,
        generated: str,
        site: Path,  # noqa: F811
    ) -> None:
        urls = {match.rstrip(".,;:") for match in re.findall(re.escape(ROOT) + r"[^\s)\"'<>]*", generated)}
        assert urls
        missing = []
        for url in sorted(urls):
            relative = url[len(ROOT) :].split("#", 1)[0].split("?", 1)[0]
            if relative in DEPLOY_TIME_PATHS:
                continue
            target = site / relative
            if not (target.is_file() or (target / "index.html").is_file()):
                missing.append(url)
        assert missing == []
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_llms_full.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'scripts.generate_llms_full'`.

- [ ] **Step 2: Write `scripts/generate_llms_full.py`**

```python
#!/usr/bin/env python3
"""Build docs/llms-full.txt: every docs page's text in one file, for AI agents.

docs/llms.txt is the index (what this is, how it differs, a described list of links). This is the
companion an agent fetches when it wants the whole corpus in one request. Ported from the sibling
project's scripts/build_llms_full.py (Shortlist):

- Page order comes from `nav` in docs/_config.yml, the list that drives the sidebar, pager and
  breadcrumbs. A published page missing from nav is an error, never a silent drop.
- The landing page is built from docs/_data/*.yml, so its stats, tour, features, works-with and FAQ
  are flattened from those files into the home section. Editing a data file changes this output,
  and the drift test catches a forgotten regeneration.
- Front matter, Liquid comments, JSON-LD, HTML comments and heading anchors are stripped; `.md` and
  image links become absolute site URLs, resolved from the linking page's own folder.

Committed rather than built by Jekyll, so it can be diffed in review. Jekyll serves it verbatim at
/llms-full.txt (`render_with_liquid: false`: the docs quote template syntax such as {{Item.Path}}
that Liquid would otherwise eat).

Usage:
    python scripts/generate_llms_full.py           # write docs/llms-full.txt
    python scripts/generate_llms_full.py --check   # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
OUTPUT_NAME = "llms-full.txt"
OUTPUT_FRONT_MATTER = "---\nlayout: null\nsitemap: false\npermalink: /llms-full.txt\nrender_with_liquid: false\n---\n"

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
LIQUID_COMMENT = re.compile(r"\{%-?\s*comment\s*-?%\}.*?\{%-?\s*endcomment\s*-?%\}", re.DOTALL)
JSON_LD = re.compile(r'<script type="application/ld\+json">.*?</script>', re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Explicit heading anchors (`## Connection  <a id="connection"></a>`): markup, not prose.
ANCHOR_TAG = re.compile(r'[ \t]*<a id="[^"]*"></a>')
INCLUDE = re.compile(r"\{%-?\s*include\s+([\w.-]+)\s*-?%\}")
RELATIVE_URL = re.compile(r"""\{\{\s*['"]([^'"]+)['"]\s*\|\s*relative_url\s*\}\}""")
PAGE_VAR = re.compile(r"\{\{\s*page\.(\w+)(?:\s*\|[^}]*)?\}\}")
SITE_VAR = re.compile(r"\{\{\s*site\.(\w+)(?:\s*\|[^}]*)?\}\}")
LEFTOVER_LIQUID = re.compile(r"\{%|\{\{\s*(?:site|page)\.")
HTML_TAG = re.compile(r"<[^>]+>")
SVG = re.compile(r"<svg\b.*?</svg>", re.DOTALL)
# `[text](target)` and `![alt](target)`; targets in these docs never contain spaces.
MD_LINK = re.compile(r"(!?\[[^\]]*\]\()([^)\s]+)(\))")
SITE_ABSOLUTE_LINK = re.compile(r"\]\(/")
BLANK_RUN = re.compile(r"\n{3,}")
SKIP_TARGET = ("http://", "https://", "mailto:", "{{")


class LlmsFullError(RuntimeError):
    """The docs can't be flattened faithfully: a page missing from nav, a dead link, unknown Liquid."""


def load_config(docs_dir: Path) -> dict:
    """Parse docs/_config.yml."""
    return yaml.safe_load((docs_dir / "_config.yml").read_text(encoding="utf-8"))


def site_root(config: dict) -> str:
    """The site's absolute root URL with a trailing slash, e.g. https://mediapreviewgenerator.dev/."""
    return f"{config['url']}{config.get('baseurl') or ''}/"


def nav_urls(config: dict) -> list[str]:
    """Every page URL in nav, depth-first: the site's own reading order."""
    urls: list[str] = []
    for entry in config.get("nav", []):
        urls.append(entry["url"])
        urls.extend(child["url"] for child in entry.get("children", []))
    return urls


def published_sources(docs_dir: Path) -> list[Path]:
    """Markdown files Jekyll publishes: not docs/README.md, design docs or Jekyll's own folders."""
    sources = []
    for path in sorted(docs_dir.rglob("*.md")):
        parts = path.relative_to(docs_dir).parts
        if path.name == "README.md" and len(parts) == 1:
            continue
        if parts[0] in {"design", "vendor"} or any(part.startswith(("_", ".")) for part in parts):
            continue
        sources.append(path)
    return sources


def _page_url(docs_dir: Path, source: Path, root: str) -> str:
    slug = source.relative_to(docs_dir).with_suffix("").as_posix()
    return root if slug == "index" else f"{root}{slug}/"


def _format(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return f"{value.day} {value:%B %Y}"
    return str(value)


def _resolve(target: str, source: Path, docs_dir: Path, root: str) -> str:
    """One relative link or image target to an absolute site URL."""
    path, suffix = re.match(r"([^?#]*)(.*)", target).groups()
    if not path:  # "#anchor" on the same page
        return _page_url(docs_dir, source, root) + suffix
    resolved = (source.parent / path).resolve()
    where = source.relative_to(docs_dir)
    if docs_dir not in resolved.parents:
        raise LlmsFullError(f"{where}: {target!r} points outside docs/")
    if not resolved.exists():
        raise LlmsFullError(f"{where}: {target!r} does not exist")
    relative = resolved.relative_to(docs_dir).as_posix()
    if relative == "README.md":
        return root + suffix  # the GitHub docs hub; its job on the site is the home page's
    if resolved.suffix == ".md":
        return _page_url(docs_dir, resolved, root) + suffix
    return root + relative + suffix


def _absolute_links(body: str, source: Path, docs_dir: Path, root: str) -> str:
    def _one(match: re.Match[str]) -> str:
        target = match.group(2)
        if target.startswith(SKIP_TARGET):
            return match.group(0)
        return f"{match.group(1)}{_resolve(target, source, docs_dir, root)}{match.group(3)}"

    return MD_LINK.sub(_one, body)


def _include_as_text(docs_dir: Path, name: str) -> str:
    """Flatten an HTML include to its readable text, one block per line."""
    raw = (docs_dir / "_includes" / name).read_text(encoding="utf-8")
    raw = SVG.sub("", LIQUID_COMMENT.sub("", raw))
    raw = re.sub(r"</(li|figcaption|p|div|h[1-6])>", "\n", raw)
    text = HTML_TAG.sub("", raw).replace("&rsquo;", "'").replace("&amp;", "&").replace("&nbsp;", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _render(body: str, front: dict, config: dict, source: Path, docs_dir: Path, root: str) -> str:
    body = ANCHOR_TAG.sub("", HTML_COMMENT.sub("", body))
    if front.get("render_with_liquid") is not False:
        body = JSON_LD.sub("", LIQUID_COMMENT.sub("", body))
        body = INCLUDE.sub(lambda m: _include_as_text(docs_dir, m.group(1)), body)
        body = RELATIVE_URL.sub(lambda m: root + m.group(1).lstrip("/"), body)
        body = SITE_VAR.sub(lambda m: str(config.get(m.group(1), m.group(0))), body)
        body = PAGE_VAR.sub(lambda m: _format(front.get(m.group(1))), body)
        leftover = LEFTOVER_LIQUID.search(body)
        if leftover:
            raise LlmsFullError(f"{source.relative_to(docs_dir)}: Liquid this script can't flatten: {leftover.group(0)!r}")
    body = _absolute_links(body, source, docs_dir, root)
    return BLANK_RUN.sub("\n\n", body).strip()


def _data(docs_dir: Path, name: str) -> list:
    path = docs_dir / "_data" / f"{name}.yml"
    if not path.is_file():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _squash(text: object) -> str:
    return " ".join(str(text).split())


def _landing_text(docs_dir: Path, root: str) -> str:
    """The landing page's data-driven sections, in page order, as Markdown."""
    parts: list[str] = []
    if stats := _data(docs_dir, "stats"):
        rows = [f"- **{s['value']}{s.get('unit') or ''} {s['label']}**: {_squash(s['note'])}" for s in stats]
        parts.append("## At a glance\n\n" + "\n".join(rows))
    if tour := _data(docs_dir, "tour"):
        rows = [f"{i}. **{s['title']}.** {_squash(s['body'])}" for i, s in enumerate(tour, 1)]
        parts.append("## How it works\n\n" + "\n".join(rows))
    if features := _data(docs_dir, "features"):
        parts.append("## Features\n\n" + "\n".join(f"- **{f['title']}.** {_squash(f['body'])}" for f in features))
    if works := _data(docs_dir, "works_with"):
        rows = [f"- **{g['group']}:** " + "; ".join(f"{i['name']} ({_squash(i['note'])})" for i in g["items"]) for g in works]
        parts.append("## Works with\n\n" + "\n".join(rows))
    if faq := _data(docs_dir, "faq"):
        parts.append("## Common questions\n\n" + "\n\n".join(f"**{q['q']}**\n{_squash(q['a'])}" for q in faq))
    # Data files can't use .md links (jekyll-relative-links only rewrites pages): they write site paths.
    return SITE_ABSOLUTE_LINK.sub(f"]({root}", "\n\n".join(parts))


def _page_section(source: Path, config: dict, docs_dir: Path, root: str) -> str:
    raw = source.read_text(encoding="utf-8")
    match = FRONT_MATTER.match(raw)
    if not match:
        raise LlmsFullError(f"{source.relative_to(docs_dir)} has no front matter")
    front = yaml.safe_load(match.group(1)) or {}
    body = _render(raw[match.end() :], front, config, source, docs_dir, root)
    if source == docs_dir / "index.md":
        body = "\n\n".join(part for part in (body, _landing_text(docs_dir, root)) if part)
    parts = [f"# {front['title']}", f"Source: {_page_url(docs_dir, source, root)}", "", _squash(front["description"])]
    if front.get("facts_checked"):
        parts.append(f"Facts checked: {_format(front['facts_checked'])}.")
    if body:
        parts += ["", body]
    return "\n".join(parts)


def generate(docs_dir: Path = DOCS_DIR) -> str:
    """Build the full llms-full.txt content (front matter included, one trailing newline).

    Args:
        docs_dir: The Jekyll source folder (tests pass an edited copy).

    Returns:
        The file content.

    Raises:
        LlmsFullError: A page is missing from nav, nav names a missing page, a link is dead or
            leaves docs/, or a page uses Liquid this script doesn't flatten.
    """
    docs_dir = docs_dir.resolve()
    config = load_config(docs_dir)
    root = site_root(config)
    ordered = [docs_dir / "index.md"] + [docs_dir / f"{url.strip('/')}.md" for url in nav_urls(config)]
    if absent := [p.relative_to(docs_dir).as_posix() for p in ordered if not p.is_file()]:
        raise LlmsFullError(f"nav names pages that don't exist: {', '.join(absent)}")
    if unlisted := [p.relative_to(docs_dir).as_posix() for p in published_sources(docs_dir) if p not in ordered]:
        raise LlmsFullError(f"page(s) not in _config.yml nav, so they would be dropped: {', '.join(unlisted)}")
    index = FRONT_MATTER.sub("", (docs_dir / "llms.txt").read_text(encoding="utf-8"), count=1)
    header = index.split("\n## Docs")[0].rstrip()
    sections = [_page_section(page, config, docs_dir, root) for page in ordered]
    return (
        OUTPUT_FRONT_MATTER
        + f"{header}\n\n# Full documentation\n\n"
        + f"Every page of the {config['title']} documentation, in the site's own reading order. "
        + f"The index version of this file is at {root}llms.txt\n\n"
        + "\n\n---\n\n".join(sections)
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: write docs/llms-full.txt, or with --check report whether it is stale."""
    parser = argparse.ArgumentParser(description="Build docs/llms-full.txt from the docs site.")
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = parser.parse_args(argv)
    try:
        content = generate()
    except LlmsFullError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = DOCS_DIR / OUTPUT_NAME
    if args.check:
        if not out.is_file() or out.read_text(encoding="utf-8") != content:
            print(f"{out} is stale. Regenerate with:\n    python scripts/generate_llms_full.py", file=sys.stderr)
            return 1
        print(f"{out} is up to date.")
        return 0
    out.write_text(content, encoding="utf-8")
    print(f"wrote {out}: {len(content):,} bytes, {content.count(chr(10) + 'Source: ')} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Regenerate and run the llms tests**

Run: `/home/data/.venv/bin/python scripts/generate_llms_full.py && PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_llms_full.py -q`
Expected: `wrote .../docs/llms-full.txt: …, 15 pages`, then `12 passed`. A `LlmsFullError` naming a dead link means a real broken link in that page: fix the page.

- [ ] **Step 4: Write the failing link test `tests/test_docs_links.py`**

```python
"""Every relative link in docs/ lands on a file that exists, inside docs/, at a heading that exists.

Jekyll publishes a link to a missing page or a renamed heading without complaint; the reader finds
out by clicking. Heading ids are kramdown's (GFM parser), which is what the site serves. Links also
have to stay inside docs/: a relative ../CONTRIBUTING.md works on github.com and 404s on the site,
so those use the absolute GitHub URL.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
EXPLICIT_ID = re.compile(r"\{#([A-Za-z0-9_-]+)\}\s*$")
HTML_ID = re.compile(r"""\sid=["']([A-Za-z0-9_-]+)["']""")
HTML_TAG = re.compile(r"<[^>]+>")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "{{", "{%")


def _slugify(text: str) -> str:
    """The id kramdown's GFM parser gives a heading: what GitHub Pages and our Jekyll build serve.

    Checked against Shortlist's deployed pages (its tests/unit/test_docs_links.py): underscores are
    kept, leading non-letters are dropped, and runs are not collapsed ("A / B" -> "a--b").
    """
    text = HTML_TAG.sub("", text).strip()
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^[^a-zA-Z]+", "", text)
    text = re.sub(r"[^a-zA-Z0-9 _-]", "", text)
    return text.replace(" ", "-").lower()


def _anchors(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8", errors="replace"))  # "# comment" in code isn't a heading
    found = set(HTML_ID.findall(text))
    seen: dict[str, int] = {}
    for raw in HEADING.findall(text):
        if explicit := EXPLICIT_ID.search(raw):
            found.add(explicit.group(1))
            continue
        slug = _slugify(raw)
        count = seen.get(slug, 0)
        found.add(slug if count == 0 else f"{slug}-{count}")  # kramdown numbers repeats: x, x-1, x-2
        seen[slug] = count + 1
    return found


def _pages() -> list[Path]:
    return sorted(
        path
        for path in DOCS.rglob("*.md")
        if not any(part.startswith(("_", ".")) or part in {"design", "vendor"} for part in path.relative_to(DOCS).parts)
    )


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(DOCS).as_posix())
def test_every_relative_link_resolves(page: Path) -> None:
    broken: list[str] = []
    text = FENCE.sub("", page.read_text(encoding="utf-8", errors="replace"))
    for target in LINK.findall(text):
        if target.startswith(SKIP_PREFIXES):
            continue
        url, _, fragment = target.partition("#")
        if not url:
            if fragment and fragment not in _anchors(page):
                broken.append(f"#{fragment}: this page has no such heading")
            continue
        if url.startswith("/"):
            continue  # a site path; Jekyll's permalinks decide it
        resolved = (page.parent / url.split("?", 1)[0]).resolve()
        if DOCS.resolve() not in resolved.parents:
            broken.append(f"{url}: leaves docs/, so it 404s on the site (use the absolute GitHub URL)")
        elif not resolved.exists():
            broken.append(f"{url}: no such file")
        elif fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
            broken.append(f"{url}#{fragment}: {resolved.name} has no such heading")
    assert not broken, f"{page.relative_to(DOCS)} links nowhere:\n  " + "\n  ".join(broken)
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_links.py -q`
Expected: some FAIL rows where MkDocs's GitHub-style slug differs from kramdown's (most often a heading that starts with a digit). Fix each at the source: point the link at the kramdown id; if GitHub readers need the old id too, add `<a id="old-id"></a>` at the end of that heading line (the style `docs/guides/previews-readiness.md` already uses). Re-run until all pass.

- [ ] **Step 5: Full suite, review, commit**

Dispatch `verifier`: run `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest`; report counts and failures only. Expected: all pass.

```bash
git add scripts/generate_llms_full.py tests/test_llms_full.py tests/test_docs_links.py docs/llms-full.txt docs/*.md docs/guides/*.md
```

Dispatch `Architecture Review` on the staged diff; fix HIGH; then:

```bash
git commit -F - <<'EOF'
feat(docs): generate llms-full.txt from the Jekyll nav and data files, and check doc links

Ported from Shortlist's build_llms_full.py. The landing page's _data files are flattened into the
home section, so a data-file edit shows up in the drift test. The link test uses kramdown's heading
ids and rejects relative links that leave docs/.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---
## Task 3: Logo A everywhere, plus the hinted favicon

Lane C (`../discover-brand`). Standard review. Suggested model: sonnet.

**Files:**
- Move: `docs/design/logo/logo-a.svg` -> `docs/assets/img/logo.svg`
- Create: `docs/assets/img/favicon.svg`, `docs/assets/img/favicon-32.png`, `docs/assets/img/apple-touch-icon.png`, `media_preview_generator/web/static/images/favicon.svg`, `media_preview_generator/web/static/images/favicon-32.png`, `tests/e2e/snapshots/render_icons.py`, `tests/test_brand_assets.py`, `docs/design/logo/favicon-check.png`
- Replace contents (same paths, so the Unraid templates' raw GitHub URLs and README keep working): `docs/images/icon.svg`, `docs/images/icon.png`, `media_preview_generator/web/static/images/icon.svg`, `media_preview_generator/web/static/images/icon.png`
- Modify: `media_preview_generator/web/templates/base.html:24-26,45-47`, `media_preview_generator/web/static/css/style.css` (after `.navbar-brand` at line 710)
- Delete: `docs/design/logo/logo-b.svg`, `docs/design/logo/logo-c.svg`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `docs/assets/img/logo.svg` (logo A, 64x64 viewBox), `docs/assets/img/favicon.svg` (32x32 viewBox), PNG renders at the paths above. Task 1's head/nav and Task 7's social card reference these paths; Task 6's app captures show the new nav mark.

- [ ] **Step 1: Write the failing test `tests/test_brand_assets.py`**

```python
"""One mark everywhere: the docs site, the README, the Unraid template and the app's own UI.

The logo is option A from docs/design/logo/ (spec §9), plus a favicon cut of it drawn on a 2-unit
grid so it lands on whole pixels at 16 and 32 px. These fail when one copy of the mark is updated
and another isn't, or a rendered PNG no longer has the size its <link> declares.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_IMG = REPO_ROOT / "docs" / "assets" / "img"
APP_IMG = REPO_ROOT / "media_preview_generator" / "web" / "static" / "images"
BASE_HTML = REPO_ROOT / "media_preview_generator" / "web" / "templates" / "base.html"
LOGO_DESIGN = REPO_ROOT / "docs" / "design" / "logo"
# The preview-frame path that makes option A option A.
LOGO_A_FRAME = "M25 8H51A5 5 0 0 1 56 13V27A5 5 0 0 1 51 32H42L38 36L34 32H25A5 5 0 0 1 20 27V13A5 5 0 0 1 25 8Z"


def test_every_copy_of_the_logo_is_logo_a() -> None:
    logo = (SITE_IMG / "logo.svg").read_bytes()
    assert LOGO_A_FRAME.encode() in logo
    assert (REPO_ROOT / "docs" / "images" / "icon.svg").read_bytes() == logo
    assert (APP_IMG / "icon.svg").read_bytes() == logo


def test_site_and_app_share_one_favicon() -> None:
    favicon = (SITE_IMG / "favicon.svg").read_bytes()
    assert b'viewBox="0 0 32 32"' in favicon
    assert (APP_IMG / "favicon.svg").read_bytes() == favicon


@pytest.mark.parametrize(
    ("path", "size"),
    [
        (SITE_IMG / "favicon-32.png", 32),
        (SITE_IMG / "apple-touch-icon.png", 180),
        (REPO_ROOT / "docs" / "images" / "icon.png", 512),
        (APP_IMG / "favicon-32.png", 32),
        (APP_IMG / "icon.png", 512),
    ],
    ids=["site-favicon-32", "apple-touch-180", "docs-icon-512", "app-favicon-32", "app-icon-512"],
)
def test_rendered_pngs_have_their_declared_size(path: Path, size: int) -> None:
    with Image.open(path) as image:
        assert image.size == (size, size)
        assert image.mode == "RGBA"


def test_app_pages_use_the_favicon_and_show_the_mark() -> None:
    base = BASE_HTML.read_text(encoding="utf-8")
    assert "filename='images/favicon.svg'" in base
    assert "filename='images/favicon-32.png'" in base
    assert 'class="navbar-brand-mark' in base
    assert "bi-film" not in base


def test_unchosen_logo_options_are_gone() -> None:
    assert not (LOGO_DESIGN / "logo-b.svg").exists()
    assert not (LOGO_DESIGN / "logo-c.svg").exists()
    assert not (LOGO_DESIGN / "logo-a.svg").exists()  # moved to docs/assets/img/logo.svg
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_brand_assets.py -q`
Expected: FAIL (`docs/assets/img/logo.svg` missing).

- [ ] **Step 2: Place the logo and write the favicon**

```bash
mkdir -p docs/assets/img
git mv docs/design/logo/logo-a.svg docs/assets/img/logo.svg
git rm -q docs/design/logo/logo-b.svg docs/design/logo/logo-c.svg
cp docs/assets/img/logo.svg docs/images/icon.svg
cp docs/assets/img/logo.svg media_preview_generator/web/static/images/icon.svg
```

`docs/assets/img/favicon.svg` (then `cp` it to `media_preview_generator/web/static/images/favicon.svg`):

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="Media Preview Generator">
  <!-- The 16/32 px cut of logo.svg (spec §9). Every edge sits on the 2-unit grid, so at 16 px it lands
       on whole pixels; the preview frame is larger and has no pointer tail, because at 16 px the tail
       made the frame read as a small TV. -->
  <rect width="32" height="32" rx="6" fill="#e5a00d"/>
  <rect x="6" y="4" width="22" height="14" rx="2" fill="#0f0f1a"/>
  <path d="M14 8v6l6-3z" fill="#e5a00d"/>
  <rect y="22" width="32" height="4" fill="#0f0f1a"/>
  <circle cx="18" cy="24" r="4" fill="#0f0f1a"/>
</svg>
```

- [ ] **Step 3: Write `tests/e2e/snapshots/render_icons.py` and render**

```python
#!/usr/bin/env python3
"""Render the logo and the favicon to the PNG sizes the site, the app and Unraid use.

    /home/data/.venv/bin/python tests/e2e/snapshots/render_icons.py

Chromium draws the SVGs (the engine that shows them in a browser tab), so the PNGs match what people
see. Also writes docs/design/logo/favicon-check.png: both marks at 16, 32 and 64 px on dark and light,
at 1x, for judging the favicon at the sizes it is actually shown.
"""

from __future__ import annotations

import base64
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
LOGO = REPO_ROOT / "docs/assets/img/logo.svg"
FAVICON = REPO_ROOT / "docs/assets/img/favicon.svg"
APP_IMAGES = REPO_ROOT / "media_preview_generator/web/static/images"
RENDERS = [
    (FAVICON, 32, REPO_ROOT / "docs/assets/img/favicon-32.png"),
    (LOGO, 180, REPO_ROOT / "docs/assets/img/apple-touch-icon.png"),
    (LOGO, 512, REPO_ROOT / "docs/images/icon.png"),
    (LOGO, 512, APP_IMAGES / "icon.png"),
    (FAVICON, 32, APP_IMAGES / "favicon-32.png"),
]
CHECK_SHEET = REPO_ROOT / "docs/design/logo/favicon-check.png"
CELL = 96


def _data_uri(svg: Path) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.read_bytes()).decode()


def _render(page: Page, svg: Path, size: int, out: Path) -> None:
    page.set_viewport_size({"width": size, "height": size})
    page.set_content(
        f'<body style="margin:0"><img src="{_data_uri(svg)}" width="{size}" height="{size}" style="display:block"></body>'
    )
    page.wait_for_function("() => document.images[0].complete && document.images[0].naturalWidth > 0")
    page.screenshot(path=str(out), omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size})
    print(f"wrote {out.relative_to(REPO_ROOT)} ({size}x{size})")


def _check_sheet(page: Page) -> None:
    cells = "".join(
        f'<div class="cell" style="background:{background}"><img src="{_data_uri(svg)}" width="{px}" height="{px}"></div>'
        for background in ("#0f0f1a", "#ffffff")
        for svg in (LOGO, FAVICON)
        for px in (16, 32, 64)
    )
    page.set_viewport_size({"width": 6 * CELL, "height": 2 * CELL})
    page.set_content(
        f"<style>body{{margin:0;display:grid;grid-template-columns:repeat(6,{CELL}px)}}"
        f".cell{{width:{CELL}px;height:{CELL}px;display:grid;place-items:center}}</style>{cells}"
    )
    page.wait_for_function("() => [...document.images].every((i) => i.complete && i.naturalWidth > 0)")
    page.screenshot(path=str(CHECK_SHEET))
    print(f"wrote {CHECK_SHEET.relative_to(REPO_ROOT)}")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(device_scale_factor=1)
        for svg, size, out in RENDERS:
            _render(page, svg, size, out)
        _check_sheet(page)
        browser.close()


if __name__ == "__main__":
    main()
```

Run: `/home/data/.venv/bin/python tests/e2e/snapshots/render_icons.py`
Expected: six `wrote …` lines. Open `docs/design/logo/favicon-check.png` (Read tool): at 16 px the favicon (columns 4-6) must read as a frame over a bar with a knob, not a TV; if it doesn't, adjust `favicon.svg` on the 2-unit grid and re-render.

- [ ] **Step 4: The app's favicon and nav brand**

`media_preview_generator/web/templates/base.html` lines 24-26 become:

```html
    <link rel="icon" type="image/svg+xml" href="{{ url_for('static', filename='images/favicon.svg') }}">
    <link rel="icon" type="image/png" sizes="32x32" href="{{ url_for('static', filename='images/favicon-32.png') }}">
    <link rel="apple-touch-icon" href="{{ url_for('static', filename='images/icon.png') }}">
```

and the brand link (lines 45-47) becomes:

```html
            <a class="navbar-brand" href="{{ url_for('main.index') }}">
                <img class="navbar-brand-mark me-2" src="{{ url_for('static', filename='images/icon.svg') }}" alt="" width="26" height="26">Media Preview Generator
            </a>
```

`media_preview_generator/web/static/css/style.css`, after the `.navbar-brand { … }` rule:

```css
/* The logo mark in the nav brand: optically centred on the wordmark's x-height. */
.navbar-brand-mark {
    vertical-align: -6px;
}
```

- [ ] **Step 5: Run the tests, look at the app**

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_brand_assets.py tests/test_docs_images.py -q`
Expected: all pass (`docs/images/icon.svg` and `icon.png` stay referenced by README and the Unraid templates).

Run: `/home/data/.venv/bin/python tests/e2e/snapshots/collect.py --out /tmp/claude-1000/-home-data-orca-workspaces-plex-generate-vid-previews-discover/051f57ad-9232-4c9d-9878-c5ef2df9e88f/scratchpad/t3-shots/`
Expected: dashboard shots in light and dark with the amber mark left of "Media Preview Generator" in the nav, aligned with the text. Then dispatch `verifier` for `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest -m e2e -n 8 --no-cov` (never `-n auto`); expected: pass.

- [ ] **Step 6: Review and commit**

```bash
git add docs/assets/img docs/images/icon.svg docs/images/icon.png docs/design/logo/favicon-check.png \
  media_preview_generator/web/static/images media_preview_generator/web/templates/base.html \
  media_preview_generator/web/static/css/style.css tests/e2e/snapshots/render_icons.py tests/test_brand_assets.py
```

Dispatch `Architecture Review` on the staged diff; fix HIGH; then:

```bash
git commit -F - <<'EOF'
feat: new logo (scrub preview mark) for the docs, README, Unraid and the app

Logo A everywhere, plus a favicon cut drawn on a 2-unit grid so it stays crisp at 16 and 32 px.
PNG sizes are rendered by Chromium from the SVGs (tests/e2e/snapshots/render_icons.py).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

Merge lane C into `stevezau/site-redesign` now (see "Merging a lane back"), because Task 1's image test waits for it.

---

## Task 4: Lab open-films library, previews from this branch, Jellyfin redirect proof

Lane B (`../discover-lab`). Standard review, but the lab-container recreation (Step 6) is done by opus and checked line by line against `docker inspect` output. Suggested model: opus.

**Files (all new):**
- `docs/design/site-redesign-lab/jellyfin_redirect_proof.py`
- `docs/design/site-redesign-lab/films.json`
- `docs/design/site-redesign-lab/openfilms.sh`
- `docs/design/site-redesign-lab/add_mount.py`
- `docs/design/site-redesign-lab/site_app.sh`
- `docs/design/site-redesign-lab/lab_setup.py`
- `docs/design/site-redesign-lab/results/jellyfin-redirect-proof.json`, `docs/design/site-redesign-lab/results/outputs.json`

**Interfaces:**
- Consumes: the marker lab (`mlab-plex` 127.0.0.1:32402, `mlab-jellyfin` 18097, `mlab-jf12` 18098, `mlab-emby` 18096, network `mlab`) and its env file, default `MLAB_ENV=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/env` (keys `PLEX_TOKEN`, `JF_TOKEN`, `JF12_TOKEN`, `EMBY_TOKEN`, `EMBY_UID`; this task appends `MLAB_SITE_APP_TOKEN`).
- Produces (Tasks 5 and 8 rely on these exact names):
  - Host folder `OPENFILMS_DIR=/home/data/mlab-openfilms`; films at `Movies/<Title> (<Year>)/<Title> (<Year>).<ext>` with `poster.jpg`; seen by the servers as `/media/openfilms/Movies/...` (read-only) and by the app read-write. Captures go to `/home/data/mlab-openfilms/captures/` (shared by all worktrees, never committed from there).
  - A library named `Open Films` on `mlab-plex` (Plex's own preview generation off for it), `mlab-jellyfin`, `mlab-emby`.
  - Container `mlab-site-app` (image `plex-previews:site-lab`, 127.0.0.1:18083, token `MLAB_SITE_APP_TOKEN`) with servers `site-plex`, `site-jellyfin`, `site-emby` named "Home Plex", "Home Jellyfin", "Home Emby", only `Open Films` enabled.
  - `lab_setup.py` subcommands `libraries`, `servers`, `generate`, `verify`; module constants `PLEX`, `JELLYFIN`, `EMBY`, `APP`, `MEDIA`, `MEDIA_HOST`, `LIBRARY`; helpers `plex()`, `jellyfin()`, `emby()`, `app()`, `plex_section() -> str | None`, `films() -> list[str]`, `wait_until(what, check, timeout, every)`, `parse_timestamp(iso: str) -> float`; constant `VIDEO_SUFFIXES`.
  - `films.json`: list of `{slug, title, year, video, poster, licence, licence_url, attribution, source_page, hdr, required}`; Task 10's credits page and its test read it.

- [ ] **Step 1 (run first, today): prove Jellyfin follows the manifest redirect**

The github.io manifest URL already 301s (see "Facts established"). Write `docs/design/site-redesign-lab/jellyfin_redirect_proof.py`:

```python
#!/usr/bin/env python3
"""Prove Jellyfin's plugin-repository fetch follows the github.io -> mediapreviewgenerator.dev 301.

Every Jellyfin install of Media Preview Bridge was registered with the github.io manifest URL. Since
the Pages custom domain went live (2026-09-23) that URL answers 301 to
https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json (https to https, another host). If
Jellyfin's HttpClient didn't follow it, those installs would stop seeing plugin updates.

For each lab Jellyfin (10.11 and 12.0): register the OLD URL as a repository unless it already is,
ask for the package catalogue, and check Media Preview Bridge is listed from that repository. The
server's repository list is restored afterwards. Writes results/jellyfin-redirect-proof.json and
exits 1 if any server fails.

    ./jellyfin_redirect_proof.py
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = Path(
    os.environ.get("MLAB_ENV", "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/env")
)
OLD_URL = "https://stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json"
PLUGIN_NAME = "Media Preview Bridge"
SERVERS = {"mlab-jellyfin": ("http://127.0.0.1:18097", "JF_TOKEN"), "mlab-jf12": ("http://127.0.0.1:18098", "JF12_TOKEN")}


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.startswith("#"):
            env[key.strip()] = value.strip()
    return env


def call(base: str, token: str, method: str, path: str, body: object = None) -> object:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f'MediaBrowser Token="{token}"', "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
    return json.loads(raw) if raw.strip() else None


def probe(name: str, base: str, token: str) -> dict:
    version = call(base, token, "GET", "/System/Info/Public")["Version"]
    original = call(base, token, "GET", "/Repositories")
    added = not any(repo.get("Url") == OLD_URL for repo in original)
    if added:
        call(base, token, "POST", "/Repositories", [*original, {"Name": "redirect proof (temporary)", "Url": OLD_URL, "Enabled": True}])
    try:
        packages = call(base, token, "GET", "/Packages")
    finally:
        if added:
            call(base, token, "POST", "/Repositories", original)
    versions = [
        version_info.get("version")
        for package in packages
        if package.get("name") == PLUGIN_NAME
        for version_info in package.get("versions", [])
        if version_info.get("repositoryUrl") == OLD_URL
    ]
    return {
        "server": name,
        "jellyfin_version": version,
        "repository_url": OLD_URL,
        "temporarily_added": added,
        "versions_listed": versions,
        "follows_redirect": bool(versions),
    }


def main() -> int:
    env = load_env(ENV_FILE)
    results = [probe(name, base, env[key]) for name, (base, key) in SERVERS.items()]
    out = HERE / "results" / "jellyfin-redirect-proof.json"
    out.parent.mkdir(exist_ok=True)
    record = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "results": results}
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    for result in results:
        print(f"{result['server']} (Jellyfin {result['jellyfin_version']}): follows_redirect={result['follows_redirect']}")
    return 0 if all(result["follows_redirect"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `chmod +x docs/design/site-redesign-lab/*.py && docs/design/site-redesign-lab/jellyfin_redirect_proof.py; echo "exit=$?"`
Expected: `mlab-jellyfin (Jellyfin 10.11.x): follows_redirect=True`, `mlab-jf12 (Jellyfin 12.0.x): follows_redirect=True`, `exit=0`. Cross-check: `docker logs --since 5m mlab-jellyfin 2>&1 | grep -iE "manifest|repositor" | tail -5` shows no fetch error for the old URL.

**If either line says False: stop this lane and tell the owner at once.** Existing installs have not been able to read the manifest since the domain went live. Their options: remove the custom domain in the Pages settings (github.io serves again, no redirect) until a fix ships, or accept it and publish the new URL for manual re-adding. Task 12 cannot proceed as written.

- [ ] **Step 2: Film list with licences checked at the source**

`docs/design/site-redesign-lab/films.json` (the `licence`/`attribution` values are what each source said when this plan was written; Step 2's commands re-read them, and the source wins over this file):

```json
[
  {"slug": "tears-of-steel", "title": "Tears of Steel", "year": 2012,
   "video": "https://archive.org/download/Tears-of-Steel/tears_of_steel_1080p.mp4",
   "poster": "https://mango.blender.org/wp-content/uploads/2011/12/mango_DVD_sm.jpg",
   "licence": "CC BY 3.0", "licence_url": "https://creativecommons.org/licenses/by/3.0/",
   "attribution": "(CC) Blender Foundation | mango.blender.org",
   "source_page": "https://archive.org/details/Tears-of-Steel", "hdr": false, "required": true},
  {"slug": "sintel", "title": "Sintel", "year": 2010,
   "video": "https://archive.org/download/sintel.-2010.1080p/Sintel.2010.1080p.mp4",
   "poster": "https://archive.org/download/Sintel/Poster.jpg",
   "licence": "CC BY 3.0", "licence_url": "https://creativecommons.org/licenses/by/3.0/",
   "attribution": "(c) copyright Blender Foundation | durian.blender.org",
   "source_page": "https://archive.org/details/sintel.-2010.1080p", "hdr": false, "required": true},
  {"slug": "big-buck-bunny", "title": "Big Buck Bunny", "year": 2008,
   "video": "https://archive.org/download/BigBuckBunnyFULLHD60FPS/Big%20Buck%20Bunny%20-%20FULL%20HD%2060FPS.mp4",
   "poster": "https://commons.wikimedia.org/wiki/Special:FilePath/Big_buck_bunny_poster_big.jpg",
   "licence": "CC BY 3.0", "licence_url": "https://creativecommons.org/licenses/by/3.0/",
   "attribution": "(c) copyright 2008, Blender Foundation / www.bigbuckbunny.org",
   "source_page": "https://archive.org/details/BigBuckBunnyFULLHD60FPS", "hdr": false, "required": true},
  {"slug": "elephants-dream", "title": "Elephants Dream", "year": 2006,
   "video": "https://archive.org/download/ElephantsDream/ed_hd.mp4",
   "poster": "https://commons.wikimedia.org/wiki/Special:FilePath/ElephantsDreamPoster.jpg",
   "licence": "CC BY 2.5", "licence_url": "https://creativecommons.org/licenses/by/2.5/",
   "attribution": "(c) copyright 2006, Blender Foundation / Netherlands Media Art Institute / www.elephantsdream.org",
   "source_page": "https://archive.org/details/ElephantsDream", "hdr": false, "required": true},
  {"slug": "cosmos-laundromat", "title": "Cosmos Laundromat", "year": 2015,
   "video": null,
   "poster": "https://commons.wikimedia.org/wiki/Special:FilePath/Cosmos_Laundromat_Victor_Poster.jpg",
   "licence": "CC BY 4.0", "licence_url": "https://creativecommons.org/licenses/by/4.0/",
   "attribution": "(CC) Blender Foundation | gooseberry.blender.org",
   "source_page": null, "hdr": false, "required": true}
]
```

Fill Cosmos Laundromat's `video` and `source_page` from archive.org:

```bash
curl -s "https://archive.org/advancedsearch.php?q=title%3A%28Cosmos+Laundromat%29+AND+mediatype%3Amovies&fl%5B%5D=identifier&fl%5B%5D=licenseurl&rows=20&output=json" | jq '.response.docs'
# for the item whose licenseurl is creativecommons.org/licenses/by/…:
curl -s "https://archive.org/metadata/<identifier>" | jq -r '.files[] | select(.name | test("mp4$"; "i")) | "\(.name)\t\(.size)\t\(.height // "")"'
```

Pick the 1080p mp4 (height 1080, or "1080" in the name); `video` = `https://archive.org/download/<identifier>/<url-encoded name>`, `source_page` = `https://archive.org/details/<identifier>`. If no CC BY 1080p item exists, set `"required": false` and leave `video` null (the film is then skipped and not credited).

Optional, time-boxed to 15 minutes in total: run the same search for `Spring`, `Sprite Fright` and `Charge` (Blender Studio); add an entry with `"required": false` only for an item whose `licenseurl` is a CC BY licence and that has a 1080p mp4 under 2 GB.

Verify every licence at its source:

```bash
cd docs/design/site-redesign-lab
for id in $(jq -r '.[] | select(.source_page != null) | .source_page | sub(".*/details/"; "")' films.json); do
  printf '%s\t' "$id"; curl -s "https://archive.org/metadata/$id" | jq -r '[.metadata.licenseurl // "NO LICENCE URL", .metadata.title] | @tsv'
done
for f in Big_buck_bunny_poster_big.jpg ElephantsDreamPoster.jpg Cosmos_Laundromat_Victor_Poster.jpg; do
  printf '%s\t' "$f"
  curl -s "https://commons.wikimedia.org/w/api.php?action=query&titles=File:$f&prop=imageinfo&iiprop=extmetadata&format=json" \
    | jq -r '.query.pages[].imageinfo[0].extmetadata | [.LicenseShortName.value, (.Artist.value | gsub("<[^>]*>"; ""))] | @tsv'
done
curl -s https://mango.blender.org/about/ | grep -io 'creative commons[^<]*' | head -3
curl -s https://archive.org/metadata/Sintel | jq -r '.metadata.licenseurl'
```

Expected: every line names a Creative Commons BY licence. Update `licence`, `licence_url` and `attribution` in `films.json` to exactly what each source states. Any film or poster without a verifiable CC BY licence: set its `poster` to null (the film can stay; the credits say "no poster") or drop the film.

- [ ] **Step 3: Download the films**

`docs/design/site-redesign-lab/openfilms.sh`:

```bash
#!/bin/bash
# Download the films in films.json into the lab media folder, laid out the way Plex, Jellyfin and
# Emby expect movies: Movies/<Title> (<Year>)/<Title> (<Year>).<ext> plus poster.jpg. Idempotent: a
# file that is already there is skipped. Never writes under /data*.
#
#   ./openfilms.sh      download what's missing, then print each film's ffprobe facts
set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly MEDIA_ROOT="${OPENFILMS_DIR:-/home/data/mlab-openfilms}"
readonly MOVIES="${MEDIA_ROOT}/Movies"

if [[ "$MEDIA_ROOT" == /data* ]]; then
    echo "refusing to write under /data*: ${MEDIA_ROOT}" >&2
    exit 1
fi
mkdir -p "$MOVIES" "${MEDIA_ROOT}/captures"

fetch() {
    local url="$1" out="$2"
    if [[ -s "$out" ]]; then
        echo "have   ${out#"$MEDIA_ROOT"/}"
        return
    fi
    echo "fetch  ${out#"$MEDIA_ROOT"/}"
    curl -fL --retry 3 --retry-delay 5 -o "${out}.part" "$url"
    mv "${out}.part" "$out"
}

jq -r '.[] | select(.video != null) | [.title, (.year | tostring), .video, (.poster // "")] | @tsv' "${HERE}/films.json" |
    while IFS=$'\t' read -r title year video poster; do
        dir="${MOVIES}/${title} (${year})"
        ext="${video##*.}"
        mkdir -p "$dir"
        fetch "$video" "${dir}/${title} (${year}).${ext,,}"
        if [[ -n "$poster" ]]; then
            fetch "$poster" "${dir}/poster.jpg"
        fi
    done

find "$MOVIES" -type f \( -name '*.mp4' -o -name '*.mkv' -o -name '*.mov' \) -print0 | sort -z |
    while IFS= read -r -d '' file; do
        printf '%s\t' "$(basename "$file")"
        ffprobe -v error -select_streams v:0 \
            -show_entries stream=codec_name,profile,width,height,r_frame_rate,pix_fmt,color_transfer,color_primaries,color_space:format=duration \
            -of compact=p=0:nk=1 "$file"
    done
```

Run: `chmod +x docs/design/site-redesign-lab/openfilms.sh && docs/design/site-redesign-lab/openfilms.sh`
Expected: `fetch` lines, then one ffprobe line per film. Big Buck Bunny must report `1920|1080` and about 60 fps (`60/1`); if it reports 640x360 the wrong archive.org item was used. `ls -la /home/data/mlab-openfilms/Movies/*/` shows each video and `poster.jpg`, owned by uid 1000.

- [ ] **Step 4: Pick and check the HDR title**

Find the Netflix Open Content files (CC BY 4.0):

```bash
for prefix in CosmosLaundromat Meridian; do
  curl -s "https://download.opencontent.netflix.com.s3.amazonaws.com/?list-type=2&prefix=${prefix}/" | grep -o '<Key>[^<]*HDR[^<]*\.mp4</Key>'
done
```

If the bucket listing is refused, open https://opencontent.netflix.com in a browser and copy the download link of `CosmosLaundromat_2k24p_HDR_P3PQ.mp4` (about 696 MB, preferred: smaller, 2K) or `Meridian_UHD4k5994_HDR_P3PQ.mp4` (about 850 MB). Download the first candidate into its own folder (`Cosmos Laundromat HDR (2015)` or `Meridian (2016)`) with `curl -fL -o …`, then:

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,profile,pix_fmt,color_transfer,color_primaries,color_space,width,height -of default=nw=1 "<file>"
```

- `color_transfer=smpte2084`: tagged PQ. Use it.
- Transfer missing or `unknown`, and the file name and Netflix's page both say PQ (`P3PQ`): the true transfer is PQ, so tagging it is honest. Remux, copying the video: `ffmpeg -v error -i in.mp4 -map 0 -c copy -bsf:v hevc_metadata=transfer_characteristics=16:colour_primaries=12:matrix_coefficients=9 out.mp4` (`h264_metadata` instead of `hevc_metadata` if `codec_name=h264`; 12 = P3-D65, 9 = BT.2020 non-constant). Use a different matrix only if Netflix's page for the title states one. Re-run ffprobe: expect `color_transfer=smpte2084`.
- The file won't decode (`ffmpeg -v error -i <file> -t 2 -f null -` prints errors), or Netflix doesn't state PQ: try the other candidate. If neither works, the HDR pair is dropped and Task 9's HDR section uses copy only; say so at STOP 3.

Add the chosen title to `films.json` with `"hdr": true`, `"licence": "CC BY 4.0"`, `"licence_url": "https://creativecommons.org/licenses/by/4.0/"`, `"attribution"` exactly as Netflix's page asks (it names Netflix and the production), `"source_page"` = that page, `"poster": null`, and a note field `"tags_added": true|false` recording whether Step 4 remuxed it.

- [ ] **Step 5: Write `docs/design/site-redesign-lab/add_mount.py`**

```python
#!/usr/bin/env python3
"""Recreate a lab container with one extra read-only bind mount, keeping everything else identical.

The marker-lab servers (docs/design/intro-credits/evidence/lab/up.sh on feat/markers-detection)
carry ~190 per-folder bind mounts each, so retyping their `docker run` is how a mount gets lost. This
rebuilds the run arguments from `docker inspect`, runs the SAME image ID (the tag may have moved
since), adds the new mount, and keeps the old container, stopped and renamed `<name>-pre-openfilms`,
for rollback. Named volumes hold all server state, so the new container is the same server.

    ./add_mount.py add mlab-jellyfin /home/data/mlab-openfilms/Movies /media/openfilms/Movies \
        --health http://127.0.0.1:18097/System/Info/Public
    ./add_mount.py add mlab-plex /home/data/mlab-openfilms/Movies /media/openfilms/Movies --gpu \
        --health http://127.0.0.1:32402/identity
    ./add_mount.py rollback mlab-jellyfin

Refuses a host path under /data*, a container that already mounts the target, and container settings
it doesn't reproduce (--mount, --gpus, a custom entrypoint, more than one network).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request

BACKUP_SUFFIX = "-pre-openfilms"
SECRET_HINTS = ("TOKEN", "CLAIM", "KEY", "PASS", "SECRET")
# The NVIDIA runtime the lab's GPU containers use (docker's default runtime here is runc).
GPU_ARGS = [
    "--runtime=nvidia", "-e", "NVIDIA_VISIBLE_DEVICES=all", "-e", "NVIDIA_DRIVER_CAPABILITIES=all",
    "--device", "/dev/dri:/dev/dri",
]  # fmt: skip


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"docker {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def inspect(name: str) -> dict:
    return json.loads(docker("inspect", name))[0]


def exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def run_args(info: dict) -> list[str]:
    """`docker run` arguments that recreate the container described by `info`, as it is now."""
    config, host = info["Config"], info["HostConfig"]
    image = json.loads(docker("image", "inspect", info["Image"]))[0]["Config"]
    require(not host.get("Mounts"), "container uses --mount; extend add_mount.py first")
    require(not host.get("DeviceRequests"), "container uses --gpus; extend add_mount.py first")
    require(config.get("Entrypoint") == image.get("Entrypoint"), "container overrides the entrypoint; extend add_mount.py first")
    networks = list(info["NetworkSettings"]["Networks"])
    require(len(networks) == 1, f"expected exactly one network, found {networks}")
    args = ["run", "-d", "--name", info["Name"].lstrip("/"), "--network", networks[0]]
    if config.get("User"):
        args += ["--user", config["User"]]
    image_env = set(image.get("Env") or [])
    for entry in config.get("Env") or []:
        if entry not in image_env:
            args += ["-e", entry]
    for port, bindings in sorted((host.get("PortBindings") or {}).items()):
        for binding in bindings or []:
            host_side = f"{binding['HostIp']}:{binding['HostPort']}" if binding.get("HostIp") else binding["HostPort"]
            args += ["-p", f"{host_side}:{port}"]
    for bind in host.get("Binds") or []:
        args += ["-v", bind]
    if host.get("Runtime") and host["Runtime"] != "runc":
        args += ["--runtime", host["Runtime"]]
    for device in host.get("Devices") or []:
        args += ["--device", f"{device['PathOnHost']}:{device['PathInContainer']}:{device['CgroupPermissions']}"]
    restart = (host.get("RestartPolicy") or {}).get("Name")
    if restart and restart != "no":
        args += ["--restart", restart]
    args.append(info["Image"])  # the sha256 image ID, so a moved tag can't upgrade the server
    if config.get("Cmd") != image.get("Cmd"):
        args += config.get("Cmd") or []
    return args


def redact(args: list[str]) -> str:
    shown = []
    for arg in args:
        key, sep, _ = arg.partition("=")
        shown.append(f"{key}=<redacted>" if sep and any(hint in key for hint in SECRET_HINTS) else arg)
    return " ".join(shown)


def wait_healthy(url: str, timeout: float = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(3)
    raise SystemExit(f"{url} did not answer 200 within {timeout:.0f}s")


def add(name: str, host_path: str, container_path: str, gpu: bool, health: str, extra_env: list[str]) -> None:
    require(not host_path.startswith("/data"), f"refusing a host path under /data*: {host_path}")
    backup = name + BACKUP_SUFFIX
    require(not exists(backup), f"{backup} already exists: roll back or remove it first")
    before = inspect(name)
    binds = before["HostConfig"].get("Binds") or []
    require(all(bind.split(":")[1] != container_path for bind in binds), f"{name} already mounts {container_path}")
    args = run_args(before)
    new_bind = f"{host_path}:{container_path}:ro"
    image_at = args.index(before["Image"])
    env_args = [arg for entry in extra_env for arg in ("-e", entry)]  # later -e wins over an inherited empty one
    args[image_at:image_at] = ["-v", new_bind, *(GPU_ARGS if gpu else []), *env_args]
    print("docker " + redact(args))
    docker("stop", name)
    docker("rename", name, backup)
    try:
        docker(*args)
        wait_healthy(health)
    except BaseException:
        docker("rm", "-f", name, check=False)
        docker("rename", backup, name)
        docker("start", name)
        raise
    after = inspect(name)
    require(sorted(after["HostConfig"]["Binds"]) == sorted([*binds, new_bind]), "bind mounts are not the old set plus one")
    require(after["Image"] == before["Image"], "the image changed")
    require(after["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"], "port bindings changed")
    print(f"{name}: {len(binds)} -> {len(binds) + 1} binds{' + GPU' if gpu else ''}; old container kept as {backup} (stopped)")


def rollback(name: str) -> None:
    backup = name + BACKUP_SUFFIX
    require(exists(backup), f"no {backup} to roll back to")
    docker("rm", "-f", name, check=False)
    docker("rename", backup, name)
    docker("start", name)
    print(f"{name}: restored from {backup}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser = sub.add_parser("add")
    add_parser.add_argument("name")
    add_parser.add_argument("host_path")
    add_parser.add_argument("container_path")
    add_parser.add_argument("--gpu", action="store_true", help="also give it the NVIDIA runtime and /dev/dri")
    add_parser.add_argument("--health", required=True, help="URL that answers 200 once the server is up")
    add_parser.add_argument("--env", action="append", default=[], help="extra KEY=VALUE, e.g. a Plex claim code")
    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("name")
    args = parser.parse_args()
    if args.command == "add":
        add(args.name, args.host_path, args.container_path, args.gpu, args.health, args.env)
    else:
        rollback(args.name)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Recreate the three capture servers with the films mounted**

Dry look first: `docker inspect -f '{{len .HostConfig.Binds}} {{.Image}} {{.HostConfig.Runtime}}' mlab-plex mlab-jellyfin mlab-emby` (record the output in the task report). Then, one at a time, confirming each is healthy before the next:

```bash
L=docs/design/site-redesign-lab
$L/add_mount.py add mlab-jellyfin /home/data/mlab-openfilms/Movies /media/openfilms/Movies --health http://127.0.0.1:18097/System/Info/Public
$L/add_mount.py add mlab-emby /home/data/mlab-openfilms/Movies /media/openfilms/Movies --health http://127.0.0.1:18096/emby/System/Info/Public
$L/add_mount.py add mlab-plex /home/data/mlab-openfilms/Movies /media/openfilms/Movies --gpu --health http://127.0.0.1:32402/identity
curl -s http://127.0.0.1:32402/identity | grep -o 'claimed="[01]"'
docker exec mlab-plex nvidia-smi -L
```

Expected: each prints `<name>: N -> N+1 binds…; old container kept as <name>-pre-openfilms (stopped)`; Plex still `claimed="1"` (the claim lives in the config volume, so recreation keeps it); `nvidia-smi -L` lists the Quadro P5000 (Plex gets the GPU so the Task 8 benchmark can't be accused of starving it). If Plex shows `claimed="0"`: ask the owner for a claim code from https://plex.tv/claim (valid 4 minutes), then `$L/add_mount.py rollback mlab-plex` and repeat the Plex line with `--env PLEX_CLAIM=<code>` added. If anything else looks wrong, `add_mount.py rollback <name>` puts the old container back. The `*-pre-openfilms` containers stay, stopped, until the owner says to remove them (listed in the Task 13 report).

- [ ] **Step 7: Build this branch's image and start `mlab-site-app`**

`docs/design/site-redesign-lab/site_app.sh`:

```bash
#!/bin/bash
# This branch's app as `mlab-site-app`: it makes the open films' previews for the site's captures
# and benchmark. Separate from the marker lab's mlab-app (own config volume, own port 18083). The
# films are mounted read-WRITE here, because Emby BIFs and Jellyfin trickplay are written next to
# each video; the media servers mount the same folder read-only.
#
#   ./site_app.sh            create mlab-site-app if missing
#   ./site_app.sh recreate   remove and create it again (config volume kept), e.g. after a rebuild
#
# Build the image first, from the lane worktree root:  nice -n 19 docker build -t plex-previews:site-lab .
set -euo pipefail

readonly ENV_FILE="${MLAB_ENV:-/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/env}"
readonly MEDIA_ROOT="${OPENFILMS_DIR:-/home/data/mlab-openfilms}"
readonly IMAGE="${SITE_APP_IMAGE:-plex-previews:site-lab}"
readonly NAME=mlab-site-app

[[ -f "$ENV_FILE" ]] || { echo "no lab env file at ${ENV_FILE}" >&2; exit 1; }
if ! grep -q '^MLAB_SITE_APP_TOKEN=' "$ENV_FILE"; then
    printf 'MLAB_SITE_APP_TOKEN=%s\n' "$(openssl rand -hex 24)" >>"$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "generated MLAB_SITE_APP_TOKEN in ${ENV_FILE}"
fi

if [[ "${1:-}" == "recreate" ]]; then
    docker rm -f "$NAME" >/dev/null 2>&1 || true
fi
if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "${NAME} already exists (use: $0 recreate)"
    exit 0
fi

# The token goes in through an env file, never the argv that `ps` shows every user on the host.
envfile="$(mktemp)"
trap 'rm -f "$envfile"' EXIT
chmod 600 "$envfile"
printf 'WEB_AUTH_TOKEN=%s\n' "$(sed -n 's/^MLAB_SITE_APP_TOKEN=//p' "$ENV_FILE" | tail -1)" >"$envfile"

docker volume create mlab_site_app_config >/dev/null
docker run --rm -v mlab_site_app_config:/config alpine chown 1000:1000 /config
docker run -d --name "$NAME" --network mlab -p 127.0.0.1:18083:8080 \
    -e PUID=1000 -e PGID=1000 -e TZ=UTC --env-file "$envfile" \
    --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all --device /dev/dri:/dev/dri \
    -v mlab_site_app_config:/config:nocopy -v mlab_plex_config:/plexcfg \
    -v "${MEDIA_ROOT}/Movies:/media/openfilms/Movies" \
    "$IMAGE" >/dev/null
echo "${NAME} on http://127.0.0.1:18083"
```

Run (from `../discover-lab`):

```bash
nice -n 19 docker build -t plex-previews:site-lab .
chmod +x docs/design/site-redesign-lab/site_app.sh && docs/design/site-redesign-lab/site_app.sh
sleep 20; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18083/login
```

Expected: build succeeds; `mlab-site-app on http://127.0.0.1:18083`; `200`.

- [ ] **Step 8: Write `docs/design/site-redesign-lab/lab_setup.py`**

```python
#!/usr/bin/env python3
"""Set up the Open Films library on the lab servers and make its previews with this branch's app.

    ./lab_setup.py libraries   # create "Open Films" on mlab-plex, mlab-jellyfin and mlab-emby (once)
    ./lab_setup.py servers     # register the three servers in mlab-site-app, Open Films only
    ./lab_setup.py generate    # send every film through the custom webhook and wait for the job
    ./lab_setup.py verify      # check each server shows OUR previews; writes results/outputs.json

Plex's own preview generation is switched off for the library (enableBIFGeneration=0), and Jellyfin
and Emby see the films read-only, so any preview those servers show came from this app. Existing
libraries are never touched. Tokens come from the lab env file and are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = Path(
    os.environ.get("MLAB_ENV", "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/env")
)
MEDIA_HOST = Path(os.environ.get("OPENFILMS_DIR", "/home/data/mlab-openfilms")) / "Movies"
MEDIA = "/media/openfilms/Movies"
LIBRARY = "Open Films"
PLEX = "http://127.0.0.1:32402"
JELLYFIN = "http://127.0.0.1:18097"
EMBY = "http://127.0.0.1:18096"
APP = "http://127.0.0.1:18083"
PLEX_CONFIG = "/plexcfg/Library/Application Support/Plex Media Server"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov"}
NO_FETCHERS = [{"Type": "Movie", "MetadataFetchers": [], "ImageFetchers": []}]


def load_env() -> dict[str, str]:
    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.startswith("#"):
            env[key.strip()] = value.strip()
    return env


ENV = load_env()


def http(method: str, url: str, *, headers: dict | None = None, body: object = None, timeout: int = 120) -> object:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw.strip() else None


def plex(method: str, path: str, **params: object) -> dict:
    query = urllib.parse.urlencode(params)
    return http(method, f"{PLEX}{path}{'?' + query if query else ''}", headers={"X-Plex-Token": ENV["PLEX_TOKEN"]})


def jellyfin(method: str, path: str, body: object = None) -> object:
    return http(method, f"{JELLYFIN}{path}", headers={"Authorization": f'MediaBrowser Token="{ENV["JF_TOKEN"]}"'}, body=body)


def emby(method: str, path: str, body: object = None) -> object:
    separator = "&" if "?" in path else "?"
    return http(method, f"{EMBY}/emby{path}{separator}api_key={ENV['EMBY_TOKEN']}", body=body)


def app(method: str, path: str, body: object = None) -> object:
    return http(method, f"{APP}{path}", headers={"X-Auth-Token": ENV["MLAB_SITE_APP_TOKEN"]}, body=body)


def films() -> list[str]:
    """Container paths of every film in the library folder."""
    return sorted(
        f"{MEDIA}/{path.parent.name}/{path.name}"
        for path in MEDIA_HOST.glob("*/*")
        if path.suffix.lower() in VIDEO_SUFFIXES
    )


def wait_until(what: str, check: Callable[[], object], timeout: float = 600, every: float = 5) -> object:
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise SystemExit(f"timed out waiting for {what}")
        time.sleep(every)


def plex_section() -> str | None:
    for section in plex("GET", "/library/sections")["MediaContainer"].get("Directory", []):
        if any(location["path"] == MEDIA for location in section.get("Location", [])):
            return section["key"]
    return None


def _virtual_folder(call: Callable[..., object]) -> dict | None:
    return next((folder for folder in call("GET", "/Library/VirtualFolders") if MEDIA in folder.get("Locations", [])), None)


def _count(call: Callable[..., object], parent_id: str) -> int:
    return call("GET", f"/Items?ParentId={parent_id}&Recursive=true&IncludeItemTypes=Movie")["TotalRecordCount"]


def libraries() -> None:
    count = len(films())
    if not plex_section():
        plex("POST", "/library/sections", name=LIBRARY, type="movie", agent="tv.plex.agents.none",
             scanner="Plex Movie", language="xn", location=MEDIA)  # fmt: skip
    key = plex_section()
    plex("PUT", f"/library/sections/{key}/prefs", enableBIFGeneration=0)
    plex("GET", f"/library/sections/{key}/refresh")
    wait_until("Plex to list every film",
               lambda: len(plex("GET", f"/library/sections/{key}/all")["MediaContainer"].get("Metadata", [])) >= count)  # fmt: skip
    print(f"Plex: section {key} '{LIBRARY}', {count} films, Plex's own preview generation off for it")

    for name, call, options in (
        ("Jellyfin", jellyfin, {"EnableTrickplayImageExtraction": True, "ExtractTrickplayImagesDuringLibraryScan": False,
                                "SaveTrickplayWithMedia": True, "EnableRealtimeMonitor": False, "TypeOptions": NO_FETCHERS}),
        ("Emby", emby, {"EnableRealtimeMonitor": False, "TypeOptions": NO_FETCHERS}),
    ):  # fmt: skip
        if not _virtual_folder(call):
            query = urllib.parse.urlencode({"name": LIBRARY, "collectionType": "movies", "paths": MEDIA, "refreshLibrary": "true"})
            call("POST", f"/Library/VirtualFolders?{query}", {"LibraryOptions": options})
        folder = wait_until(f"{name} to create the library", lambda call=call: _virtual_folder(call))
        wait_until(f"{name} to list every film", lambda call=call, folder=folder: _count(call, folder["ItemId"]) >= count)
        print(f"{name}: '{LIBRARY}' lists {count} films (metadata and image fetchers off: local posters only)")


SERVERS = [
    {"id": "site-plex", "type": "plex", "name": "Home Plex", "url": "http://mlab-plex:32400",
     "auth": {"method": "token", "token": ENV["PLEX_TOKEN"]}, "output": {"plex_config_folder": PLEX_CONFIG}},
    {"id": "site-jellyfin", "type": "jellyfin", "name": "Home Jellyfin", "url": "http://mlab-jellyfin:8096",
     "auth": {"method": "api_key", "api_key": ENV["JF_TOKEN"]}},
    {"id": "site-emby", "type": "emby", "name": "Home Emby", "url": "http://mlab-emby:8096",
     "auth": {"method": "api_key", "api_key": ENV["EMBY_TOKEN"], "user_id": ENV["EMBY_UID"]}},
]  # fmt: skip


def servers() -> None:
    app("POST", "/api/setup/complete")
    listed = app("GET", "/api/servers")
    existing = {server["id"] for server in (listed.get("servers", []) if isinstance(listed, dict) else listed)}
    for entry in SERVERS:
        if entry["id"] in existing:
            app("PUT", f"/api/servers/{entry['id']}", {k: v for k, v in entry.items() if k != "id"})
        else:
            app("POST", "/api/servers", entry)
        app("POST", f"/api/servers/{entry['id']}/refresh-libraries")
        stored = app("GET", f"/api/servers/{entry['id']}")
        libraries = (stored.get("server", stored) or {}).get("libraries", [])
        for library in libraries:
            library["enabled"] = (library.get("title") or library.get("name")) == LIBRARY
        if not any(library["enabled"] for library in libraries):
            raise SystemExit(f"{entry['id']}: no library named {LIBRARY!r} after refresh")
        app("PUT", f"/api/servers/{entry['id']}", {"libraries": libraries})
        print(f"{entry['id']}: registered, only '{LIBRARY}' enabled")


def parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def generate() -> None:
    paths = films()
    sent_at = time.time()
    app("POST", "/api/webhooks/custom", {"file_paths": paths, "title": "Open films"})

    def finished() -> list[dict] | None:
        jobs = [job for job in app("GET", "/api/jobs?page=1&per_page=50")["jobs"] if parse_timestamp(job["created_at"]) >= sent_at - 5]
        done = [job for job in jobs if job["status"] in ("completed", "failed", "cancelled")]
        return done if jobs and len(done) == len(jobs) else None

    for job in wait_until("the generation job(s)", finished, timeout=3600, every=10):
        print(f"job {job['id']}: {job['status']} ({job.get('library_name')})")


def verify() -> None:
    record: dict = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "plex": {}, "jellyfin": {}, "emby": {}}
    key = plex_section()
    for item in plex("GET", f"/library/sections/{key}/all")["MediaContainer"]["Metadata"]:
        meta = plex("GET", f"/library/metadata/{item['ratingKey']}")["MediaContainer"]["Metadata"][0]
        record["plex"][meta["title"]] = meta["Media"][0]["Part"][0].get("indexes") == "sd"
    folder = _virtual_folder(jellyfin)
    items = jellyfin("GET", f"/Items?ParentId={folder['ItemId']}&Recursive=true&IncludeItemTypes=Movie&Fields=Trickplay")["Items"]
    for item in items:
        trickplay = item.get("Trickplay") or {}
        record["jellyfin"][item["Name"]] = bool(next(iter(trickplay.values()), {}))
    for film in sorted(MEDIA_HOST.iterdir()):
        record["emby"][film.name] = any(film.glob("*.bif"))
    out = HERE / "results" / "outputs.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failures = [f"{server}: {title}" for server in ("plex", "jellyfin", "emby") for title, ok in record[server].items() if not ok]
    print("all previews present" if not failures else "missing previews:\n  " + "\n  ".join(failures))
    raise SystemExit(1 if failures else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["libraries", "servers", "generate", "verify"])
    {"libraries": libraries, "servers": servers, "generate": generate, "verify": verify}[parser.parse_args().command]()


if __name__ == "__main__":
    main()
```

- [ ] **Step 9: Create the libraries, register the servers, fix readiness, generate, verify**

```bash
L=docs/design/site-redesign-lab; chmod +x $L/lab_setup.py
$L/lab_setup.py libraries
$L/lab_setup.py servers
```

Expected: three `… lists N films` lines, then three `registered, only 'Open Films' enabled` lines.

Readiness, by hand in the app at http://127.0.0.1:18083 (token: `MLAB_SITE_APP_TOKEN` from the env file): Servers, then each server's Previews Readiness. Apply every Must-fix and Recommended fix whose scope is the Open Films library. Do not apply a server-wide fix (for example Plex's server-level "Generate video preview thumbnails"): these servers belong to the marker lab. Note each fix applied in the task report.

```bash
$L/lab_setup.py generate
$L/lab_setup.py verify
```

Expected: every job `completed`, then `all previews present`. If Jellyfin items show no trickplay, run a library scan in Jellyfin (the app's readiness card says whether scan-time extraction must stay on without the plugin), wait for it, and re-run `verify`. Provenance check: `stat -c '%U %y' /home/data/mlab-openfilms/Movies/*/*.bif` shows uid 1000 and times during the job (the Emby container can't write there).

- [ ] **Step 10: Review and commit**

```bash
git add docs/design/site-redesign-lab/{jellyfin_redirect_proof.py,films.json,openfilms.sh,add_mount.py,site_app.sh,lab_setup.py} \
  docs/design/site-redesign-lab/results/jellyfin-redirect-proof.json docs/design/site-redesign-lab/results/outputs.json
MLAB_ENV=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/env
cut -d= -f2- "$MLAB_ENV" | grep -v '^$' | while read -r value; do git diff --cached | grep -qF -- "$value" && echo "LAB VALUE IN DIFF"; done; echo "checked"
```

Expected: only `checked` (no lab env value, token or id, appears in the staged diff). Dispatch `Architecture Review`; fix HIGH; then:

```bash
git commit -F - <<'EOF'
chore(lab): open-films library on the lab servers, previews from this branch, Jellyfin redirect proof

Scripts recreate the lab servers with one extra read-only mount (old containers kept for rollback),
create an Open Films library, and generate its previews with a site-lab build of this branch.
Jellyfin 10.11 and 12.0 both follow the github.io -> mediapreviewgenerator.dev manifest redirect.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 5: Players mid-scrub on all three servers, the 3-up, and the HDR pair

Lane B, after Task 4. Standard review. Suggested model: opus (player DOM selectors need live debugging).

**Files:**
- Create: `tests/e2e/snapshots/lab_players.py`, `tests/e2e/snapshots/compose_site_images.py`, `docs/design/site-redesign-lab/results/captures.json`
- Output (NOT committed here; reviewed at STOP 3, copied into `docs/images/` by the tasks that reference them): `/home/data/mlab-openfilms/captures/{player-plex,player-jellyfin,player-emby,players-3up,hdr-before-after}.webp`

**Interfaces:**
- Consumes: Task 4's lab, `lab_setup.py` helpers (imported by path), env keys `PLEX_TOKEN`, `JF_TOKEN`, `EMBY_TOKEN`.
- Produces: capture files above (2x, WebP q88); `captures.json` = `{"captured_at", "film", "scrub_fraction", "captures": [{"server", "file", "proof_requests": [url without tokens], "slider_box"}], "hdr": {"title", "seconds", "bif", "frame_index"}}`. Task 9 uses `player-plex.webp` (hero, unless the owner picks another at STOP 3), `players-3up.webp`, `hdr-before-after.webp`; Task 10 uses the three per-server shots; Task 7 uses `players-3up.webp`.

- [ ] **Step 1: Let the capture browser into Plex Web without a sign-in**

Playwright on the host reaches `127.0.0.1:32402` through Docker's port proxy, so Plex sees the `mlab` gateway as the client:

```bash
GW=$(docker network inspect mlab -f '{{(index .IPAM.Config 0).Gateway}}'); echo "$GW"
TOKEN=$(sed -n 's/^PLEX_TOKEN=//p' /home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/env)
curl -s "http://127.0.0.1:32402/:/prefs?X-Plex-Token=$TOKEN" -H 'Accept: application/json' | jq -r '.MediaContainer.Setting[] | select(.id=="allowedNetworks") | .value' > /home/data/mlab-openfilms/captures/plex-allowedNetworks.before
curl -s -X PUT "http://127.0.0.1:32402/:/prefs?allowedNetworks=${GW}/255.255.255.255&X-Plex-Token=$TOKEN" -o /dev/null -w '%{http_code}\n'
```

Expected: `172.18.0.1`, then `200`. Restore the saved value in Step 6.

- [ ] **Step 2: Write `tests/e2e/snapshots/lab_players.py`**

```python
#!/usr/bin/env python3
"""Capture Plex, Jellyfin and Emby web players mid-scrub, showing thumbnails this app generated.

    /home/data/.venv/bin/python tests/e2e/snapshots/lab_players.py --server all
    /home/data/.venv/bin/python tests/e2e/snapshots/lab_players.py --server emby --at 0.55 --dump

Runs against the site lab (docs/design/site-redesign-lab/, Task 4): the same film on every server,
paused, with the pointer held over the seek bar at --at (a fraction of its width). A capture only
counts if the browser fetched the server's preview data while hovering (Plex: /indexes/sd, Jellyfin:
/Trickplay/, Emby: a .bif), which is the proof the thumbnail on screen came from those files. 2x
device scale, Chrome for Testing (H.264-capable), autoplay allowed. --dump saves the page HTML next to
each screenshot for fixing selectors when a server's web client changes.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PIL import Image
from playwright.sync_api import Browser, Page, sync_playwright

LAB = Path(__file__).resolve().parents[3] / "docs" / "design" / "site-redesign-lab"
sys.path.insert(0, str(LAB))
import lab_setup  # noqa: E402  (the lab's API helpers and constants)

CAPTURES = Path(os.environ.get("OPENFILMS_DIR", "/home/data/mlab-openfilms")) / "captures"
FILM = "Tears of Steel"
VIEWPORT = {"width": 1280, "height": 720}
LAB_USER = ("lab", "lab")
PROOF = {"plex": re.compile(r"/indexes/sd", re.I), "jellyfin": re.compile(r"/Trickplay/", re.I), "emby": re.compile(r"bif", re.I)}
# Query parameters that carry credentials on Plex, Jellyfin and Emby; compared lower-cased.
SECRET_PARAMS = {"x-plex-token", "x-emby-token", "x-mediabrowser-token", "api_key", "apikey", "token", "accesstoken"}
# The seek bar: the widest range input or ARIA slider in the bottom 40% of the player.
SLIDER_JS = """() => {
  const h = window.innerHeight, w = window.innerWidth;
  const found = [...document.querySelectorAll('input[type=range], [role=slider]')]
    .map((el) => el.getBoundingClientRect())
    .filter((r) => r.width > w * 0.4 && r.top > h * 0.6 && r.height > 0)
    .sort((a, b) => b.width - a.width);
  return found.length ? {x: found[0].x, y: found[0].y, width: found[0].width, height: found[0].height} : null;
}"""


class CaptureError(RuntimeError):
    """A server's player couldn't be driven to a proven mid-scrub thumbnail."""


def _scrub(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, "<redacted>" if k.lower() in SECRET_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _play_and_hover(page: Page, server: str, at: float, out: Path, dump: bool) -> dict:
    page.wait_for_function(
        "() => { const v = document.querySelector('video'); return v && v.readyState >= 2 && v.currentTime > 1; }",
        timeout=90_000,
    )
    page.evaluate("() => { const v = document.querySelector('video'); v.pause(); v.currentTime = v.duration * 0.2; }")
    page.wait_for_timeout(1500)
    page.mouse.move(640, 360)
    page.mouse.move(660, 620)  # wake the on-screen controls
    page.wait_for_timeout(800)
    box = page.evaluate(SLIDER_JS)
    if not box:
        raise CaptureError(f"{server}: no seek bar found (run with --dump and inspect {out.with_suffix('.html')})")
    x, y = box["x"] + box["width"] * at, box["y"] + box["height"] / 2
    page.mouse.move(x - 60, y)
    page.mouse.move(x, y, steps=10)
    page.wait_for_timeout(2500)
    if dump:
        out.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    png = page.screenshot()
    Image.open(io.BytesIO(png)).convert("RGB").save(out, "WEBP", quality=88, method=6)
    return {"slider_box": box}


def _login_jellyfin_style(page: Page, base: str) -> None:
    page.goto(f"{base}/web/#/login.html")
    page.wait_for_timeout(3000)
    page.fill("#txtManualName", LAB_USER[0])
    page.fill("#txtManualPassword", LAB_USER[1])
    page.click("button[type=submit]")
    page.wait_for_timeout(4000)


def _item_id(call) -> str:
    found = call("GET", f"/Items?Recursive=true&IncludeItemTypes=Movie&SearchTerm={FILM.replace(' ', '%20')}")["Items"]
    if not found:
        raise CaptureError(f"{FILM} is not in the library")
    return found[0]["Id"]


def open_jellyfin(page: Page) -> None:
    _login_jellyfin_style(page, lab_setup.JELLYFIN)
    page.goto(f"{lab_setup.JELLYFIN}/web/#/details?id={_item_id(lab_setup.jellyfin)}")
    page.wait_for_timeout(4000)
    page.click("button.btnPlay, .detailButton.btnPlay, button[data-action=resume], button[title=Play]", timeout=15_000)


def open_emby(page: Page) -> None:
    page.goto(f"{lab_setup.EMBY}/web/index.html")
    page.wait_for_timeout(6000)
    page.click(f"button.cardMediaInfoItem:has-text('{LAB_USER[0]}')")
    page.wait_for_timeout(2500)
    page.fill("input[type=password]", LAB_USER[1])
    page.keyboard.press("Enter")
    page.wait_for_timeout(6000)
    server_id = lab_setup.emby("GET", "/System/Info")["Id"]
    page.goto(f"{lab_setup.EMBY}/web/index.html#!/item?id={_item_id(lab_setup.emby)}&serverId={server_id}")
    page.wait_for_timeout(5000)
    page.get_by_role("button", name="Play", exact=True).first.click(timeout=15_000)


def open_plex(page: Page) -> None:
    machine = lab_setup.plex("GET", "/identity")["MediaContainer"]["machineIdentifier"]
    items = lab_setup.plex("GET", f"/library/sections/{lab_setup.plex_section()}/all")["MediaContainer"]["Metadata"]
    item = next((m for m in items if m["title"] == FILM), None)
    if item is None:
        raise CaptureError(f"{FILM} is not in the Plex library")
    part = lab_setup.plex("GET", f"/library/metadata/{item['ratingKey']}")["MediaContainer"]["Metadata"][0]["Media"][0]["Part"][0]
    if part.get("indexes") != "sd":
        raise CaptureError("Plex has no preview index for the film; run lab_setup.py verify")
    page.goto(f"{lab_setup.PLEX}/web/index.html#!/server/{machine}/details?key=%2Flibrary%2Fmetadata%2F{item['ratingKey']}")
    page.wait_for_timeout(6000)
    if page.get_by_text(re.compile(r"^Sign in", re.I)).count():
        raise CaptureError("Plex Web asks for a sign-in: allowedNetworks did not take (Step 1), see the fallback in Step 4")
    page.get_by_role("button", name=re.compile(r"^Play", re.I)).first.click(timeout=15_000)


OPENERS = {"plex": open_plex, "jellyfin": open_jellyfin, "emby": open_emby}


def capture(browser: Browser, server: str, at: float, dump: bool) -> dict:
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
    page = context.new_page()
    page.set_default_timeout(60_000)
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    out = CAPTURES / f"player-{server}.webp"
    try:
        OPENERS[server](page)
        hover_from = len(requests)
        details = _play_and_hover(page, server, at, out, dump)
    finally:
        context.close()
    proof = [_scrub(url) for url in requests[hover_from:] if PROOF[server].search(url)]
    if not proof:
        raise CaptureError(f"{server}: no preview data was fetched while hovering, so the picture proves nothing")
    return {"server": server, "file": out.name, "proof_requests": proof[:3], **details}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", choices=["plex", "jellyfin", "emby", "all"], default="all")
    parser.add_argument("--at", type=float, default=0.42, help="where on the seek bar to hover, 0-1")
    parser.add_argument("--dump", action="store_true", help="save each page's HTML next to its screenshot")
    args = parser.parse_args()
    CAPTURES.mkdir(parents=True, exist_ok=True)
    servers = ["plex", "jellyfin", "emby"] if args.server == "all" else [args.server]
    record_path = LAB / "results" / "captures.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {"captures": []}
    failures = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium", args=["--autoplay-policy=no-user-gesture-required"])
        probe = browser.new_page()
        if probe.evaluate("document.createElement('video').canPlayType('video/mp4; codecs=\"avc1.640028\"')") != "probably":
            raise SystemExit("this Chromium can't play H.264; install Playwright's Chrome for Testing build")
        probe.close()
        for server in servers:
            try:
                result = capture(browser, server, args.at, args.dump)
            except CaptureError as exc:
                failures += 1
                print(f"FAIL {exc}")
                continue
            record["captures"] = [c for c in record["captures"] if c["server"] != server] + [result]
            print(f"ok   {server}: {CAPTURES / result['file']}  proof: {result['proof_requests'][0]}")
        browser.close()
    record.update(captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), film=FILM, scrub_fraction=args.at)
    record_path.parent.mkdir(exist_ok=True)
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Capture, one server at a time**

Run: `/home/data/.venv/bin/python tests/e2e/snapshots/lab_players.py --server jellyfin --dump`
Expected: `ok   jellyfin: …/player-jellyfin.webp  proof: http://127.0.0.1:18097/Videos/…/Trickplay/320/0.jpg?…`. Look at the file (Read tool): the Jellyfin player, paused, a thumbnail bubble above the seek bar. If it fails, open the dumped HTML, fix that server's opener or `SLIDER_JS` (for example a different Play button), and re-run. Then `--server plex`, then `--server emby`.

- [ ] **Step 4: Fallbacks, in order, when a server won't cooperate**

- Plex asks for a sign-in: re-check Step 1 (`curl -s "http://127.0.0.1:32402/:/prefs?X-Plex-Token=$TOKEN" -H 'Accept: application/json' | jq '.MediaContainer.Setting[] | select(.id=="allowedNetworks")'`). If the value is right and Plex Web still asks, sign in inside the capture context with the owner's Plex account from 1Password (`op item list --categories Login`, `op item get "<name>" --fields username,password --reveal`), typed into the page by the script and never written to disk or output. If `op` isn't signed in, ask the owner to run `op-login` in an SSH terminal. If the account has 2FA, ask the owner.
- Emby shows no thumbnail after three attempts plus one library scan in between (spec §13 budget): stop trying. The 3-up becomes a 2-up (Plex, Jellyfin) and every caption says Emby is not pictured. Record `"emby": "not captured: <reason>"` in `captures.json` and raise it at STOP 3.
- A thumbnail appears but no proof request was seen: the capture is rejected by design. Look for the request in `--dump` output's network log; never relax `PROOF` without the owner's say-so.

- [ ] **Step 5: Write `tests/e2e/snapshots/compose_site_images.py` and compose**

```python
#!/usr/bin/env python3
"""Compose the site's player 3-up and the HDR before/after pair from the lab captures.

    /home/data/.venv/bin/python tests/e2e/snapshots/compose_site_images.py players
    /home/data/.venv/bin/python tests/e2e/snapshots/compose_site_images.py hdr --video <host path> --seconds 125

Both are laid out in HTML with the docs site's own tokens and rendered by Chromium at 2x, so the type
matches the site. The HDR pair is honest by construction: the left half is a plain FFmpeg frame grab
with no tone mapping (what a generator that ignores HDR produces), the right half is the frame at the
same moment read straight out of the BIF this app wrote next to the video.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
from media_preview_generator.bif_reader import read_bif_frame, read_bif_metadata  # noqa: E402

CAPTURES = Path("/home/data/mlab-openfilms/captures")
RECORD = REPO_ROOT / "docs/design/site-redesign-lab/results/captures.json"
# docs/assets/css/main.css dark tokens, repeated because this page never loads the site stylesheet.
STYLE = """
  :root { --bg:#08080a; --surface:#121216; --border:rgba(255,255,255,.09); --text:#f4f4f5; --muted:#94949f;
          --amber:#e5a00d; --font:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  * { box-sizing:border-box; } body { margin:0; padding:24px; background:var(--bg); color:var(--text);
  font-family:var(--font); -webkit-font-smoothing:antialiased; display:inline-flex; gap:20px; }
  figure { margin:0; } img { display:block; border-radius:10px; border:1px solid var(--border); }
  figcaption { margin-top:10px; font-size:15px; font-weight:600; color:var(--muted); text-align:center; }
  figcaption b { color:var(--amber); font-weight:700; }
"""


def _uri(path: Path) -> str:
    kind = "webp" if path.suffix == ".webp" else "jpeg" if path.suffix in (".jpg", ".jpeg") else "png"
    return f"data:image/{kind};base64," + base64.b64encode(path.read_bytes()).decode()


def _render(html: str, out: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(device_scale_factor=2, viewport={"width": 3200, "height": 1200})
        page.set_content(f"<style>{STYLE}</style>{html}")
        page.wait_for_function("() => [...document.images].every((i) => i.complete && i.naturalWidth > 0)")
        shot = page.locator("body").screenshot()
        browser.close()
    Image.open(io.BytesIO(shot)).convert("RGB").save(out, "WEBP", quality=88, method=6)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


def players(allow_two: bool) -> None:
    shots = [(name, CAPTURES / f"player-{server}.webp") for name, server in (("Plex", "plex"), ("Jellyfin", "jellyfin"), ("Emby", "emby"))]
    present = [(name, path) for name, path in shots if path.is_file()]
    if len(present) < 3 and not allow_two:
        raise SystemExit(f"only {[n for n, _ in present]} captured; pass --allow-two to compose without Emby")
    figures = "".join(
        f'<figure><img src="{_uri(path)}" width="520"><figcaption><b>{name}</b> web player</figcaption></figure>'
        for name, path in present
    )
    _render(figures, CAPTURES / "players-3up.webp")


def hdr(video: Path, seconds: float) -> None:
    bifs = sorted(video.parent.glob("*.bif"))
    if not bifs:
        raise SystemExit(f"no BIF next to {video}: run lab_setup.py generate first")
    meta = read_bif_metadata(str(bifs[0]))
    index = min(round(seconds * 1000 / meta.frame_interval_ms), meta.frame_count - 1)
    with tempfile.TemporaryDirectory() as tmp:
        ours = Path(tmp) / "ours.jpg"
        ours.write_bytes(read_bif_frame(str(bifs[0]), index, meta))
        width = Image.open(ours).width
        naive = Path(tmp) / "naive.png"
        at = index * meta.frame_interval_ms / 1000
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-2", str(naive)],
            check=True,
        )
        figures = (
            f'<figure><img src="{_uri(naive)}" width="{width}"><figcaption>Without tone mapping</figcaption></figure>'
            f'<figure><img src="{_uri(ours)}" width="{width}"><figcaption><b>Media Preview Generator</b></figcaption></figure>'
        )
        _render(figures, CAPTURES / "hdr-before-after.webp")
    record = json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.is_file() else {}
    record["hdr"] = {"title": video.parent.name, "seconds": at, "bif": bifs[0].name, "frame_index": index, "bif_width": width}
    RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    players_parser = sub.add_parser("players")
    players_parser.add_argument("--allow-two", action="store_true")
    hdr_parser = sub.add_parser("hdr")
    hdr_parser.add_argument("--video", type=Path, required=True)
    hdr_parser.add_argument("--seconds", type=float, required=True)
    args = parser.parse_args()
    if args.command == "players":
        players(args.allow_two)
    else:
        hdr(args.video, args.seconds)


if __name__ == "__main__":
    main()
```

Both halves are drawn at the BIF frame's own width in CSS pixels (320 by default), the size the servers show them at; the 2x render doubles both identically and sharpens neither.

Run:

```bash
/home/data/.venv/bin/python tests/e2e/snapshots/compose_site_images.py players
/home/data/.venv/bin/python tests/e2e/snapshots/compose_site_images.py hdr --video "/home/data/mlab-openfilms/Movies/<HDR folder>/<HDR file>" --seconds 120
ls -la /home/data/mlab-openfilms/captures/*.webp
```

Expected: `players-3up.webp` and `hdr-before-after.webp`, each at most 500 KB (if not, lower `quality` to 84, then 80). Pick `--seconds` at a bright, colourful moment (look at 3-4 candidates with the naive grab first). The left half should look grey and flat, the right natural.

- [ ] **Step 6: Restore Plex's allowedNetworks, review, commit**

```bash
MLAB_ENV=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/env
TOKEN=$(sed -n 's/^PLEX_TOKEN=//p' "$MLAB_ENV")
BEFORE=$(cat /home/data/mlab-openfilms/captures/plex-allowedNetworks.before)
curl -s -X PUT "http://127.0.0.1:32402/:/prefs?allowedNetworks=$(printf %s "$BEFORE" | jq -sRr @uri)&X-Plex-Token=$TOKEN" -o /dev/null -w '%{http_code}\n'
git add tests/e2e/snapshots/lab_players.py tests/e2e/snapshots/compose_site_images.py docs/design/site-redesign-lab/results/captures.json
```

Expected: `200`. `captures.json` holds redacted URLs only: `grep -iE "(token|api_?key)=[^<&\"]" docs/design/site-redesign-lab/results/captures.json` prints nothing, and `cut -d= -f2- "$MLAB_ENV" | grep -v '^$' | grep -cFf - docs/design/site-redesign-lab/results/captures.json` prints `0` (no lab token value anywhere in the file). Dispatch `Architecture Review`; fix HIGH; then:

```bash
git commit -F - <<'EOF'
chore(lab): capture Plex, Jellyfin and Emby players mid-scrub, and the HDR before/after pair

A capture only counts if the browser fetched the server's preview data (BIF index, trickplay tiles,
Emby BIF) while hovering. The HDR pair puts a plain FFmpeg grab next to the frame from this app's BIF.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 6: App screenshots, one idea per shot, at 2x

Lane C, after Task 3. Standard review. Suggested model: sonnet.

**Files:**
- Modify: `tests/e2e/snapshots/regen_readme.py` (context at 2x, WebP q88, five tour captures replacing the five old ones, retry-job stub, film titles in the worker stub), `tests/e2e/snapshots/readme_fixture.py` (film-title job rows, per-server file results for the Publish shot), `scripts/regen_readme_screenshots.sh` (counts and names)
- Create: `docs/images/tour-trigger.webp`, `docs/images/tour-resolve.webp`, `docs/images/tour-extract.webp`, `docs/images/tour-publish.webp`, `docs/images/tour-retry.webp`
- Delete: `docs/images/{home,dashboard,servers,settings,automation}.webp`
- Modify references: `README.md` screenshot table (lines 88-97), `docs/index.md` (line with `images/dashboard.webp`), `docs/multi-server.md` (line with `images/servers.webp`)

**Interfaces:**
- Consumes: Task 3's nav mark (shows in every shot).
- Produces: five tour images, each 2x (CSS size = half the pixel size), used by Task 9's `docs/_data/tour.yml` (`image: tour-<step>.webp`, `path:` shown in the frame bar) and Task 11's README 2x2 table. Print their pixel sizes for Task 9: `python -c "from PIL import Image; import glob; [print(p, Image.open(p).size) for p in sorted(glob.glob('docs/images/tour-*.webp'))]"`.

- [ ] **Step 1: Film titles in the fixture**

In `tests/e2e/snapshots/readme_fixture.py`, `seed_jobs()`: replace the six `completed_fixtures` tuples with the lab films (single-file Radarr imports, plus one scheduled library pass so the table still shows scale):

```python
        completed_fixtures = [
            ("Tears of Steel (2012)", "plex-home", "Home Plex", "plex", 1, 1),
            ("Sintel (2010)", "jellyfin-home", "Home Jellyfin", "jellyfin", 1, 1),
            ("Big Buck Bunny (2008)", "emby-home", "Home Emby", "emby", 1, 1),
            ("Elephants Dream (2006)", "plex-home", "Home Plex", "plex", 1, 1),
            ("Cosmos Laundromat (2015)", "jellyfin-home", "Home Jellyfin", "jellyfin", 1, 1),
            ("Movies", "plex-home", "Home Plex", "plex", 842, 842),
        ]
```

Then, after the rows are saved, record per-server results for the Tears of Steel job so the job's Files tab shows one pill per server (the Publish shot). The dict shape is exactly what `Worker._capture_publishers` builds (`media_preview_generator/jobs/worker.py:528-566`):

```python
    from media_preview_generator.web.jobs import JobManager

    tears = next(row for row in rows if row.library_name == "Tears of Steel (2012)")
    video = "/media/movies/Tears of Steel (2012)/Tears of Steel (2012).mp4"
    publishers = [
        {"server_id": "plex-home", "server_name": "Home Plex", "server_type": "plex", "adapter_name": "plex_bundle",
         "status": "published", "message": "", "frame_source": "extracted", "canonical_path": video,
         "output_paths": ["/plex/Media/localhost/3/f1c2a9e0d4b7.bundle/Contents/Indexes/index-sd.bif"]},
        {"server_id": "jellyfin-home", "server_name": "Home Jellyfin", "server_type": "jellyfin",
         "adapter_name": "jellyfin_trickplay", "status": "published", "message": "", "frame_source": "extracted",
         "canonical_path": video, "output_paths": ["/media/movies/Tears of Steel (2012)/Tears of Steel (2012).trickplay"]},
        {"server_id": "emby-home", "server_name": "Home Emby", "server_type": "emby", "adapter_name": "emby_sidecar",
         "status": "published", "message": "", "frame_source": "extracted", "canonical_path": video,
         "output_paths": ["/media/movies/Tears of Steel (2012)/Tears of Steel (2012)-320-10.bif"]},
    ]  # fmt: skip
    JobManager(config_dir=str(config_dir)).record_file_result(
        tears.id, video, "generated", worker="GPU Worker 1 (NVIDIA TITAN RTX)", servers=publishers
    )
```

Also set the three Tears of Steel rows' progress `total_items`/`processed_items` to 1 so the job reads as one file.

- [ ] **Step 2: 2x context, WebP q88, and the five tour captures in `regen_readme.py`**

- In `regenerate()`, the context becomes `browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=2)`.
- `_to_webp(png_path, quality=88, method=6)` (default quality 85 -> 88; update its docstring and the module docstring).
- `_fake_worker_statuses()`: busy workers' `current_title` values become `"Tears of Steel (2012)"`, `"Sintel (2010)"`, `"Big Buck Bunny (2008)"`; their `library_name` `"Movies"`.
- Add the retry stub, installed on the context before any capture (`/api/jobs?page=…` is the one list the dashboard polls; `app.js` filters it to `status === 'running'` for Active Jobs and shows "Waiting to retry" when `progress.retry_eta` is in the future):

```python
def _install_retry_job_stub(ctx: BrowserContext) -> None:
    """Add one job waiting out a retry to the dashboard's job list.

    The retry state lasts minutes in real life and can't be seeded (JobManager turns stored RUNNING
    rows into FAILED at startup), so the real /api/jobs response is fetched and one job appended.
    """
    def handle(route) -> None:
        response = route.fetch()
        data = response.json()
        now = datetime.now(timezone.utc)
        data["jobs"].insert(0, {
            "id": "retry-demo", "status": "running", "paused": False,
            "library_name": "Sintel (2010)", "server_id": "jellyfin-home", "server_name": "Home Jellyfin",
            "server_type": "jellyfin", "created_at": (now - timedelta(minutes=2)).isoformat(),
            "started_at": (now - timedelta(minutes=2)).isoformat(),
            "progress": {"percent": 0, "total_items": 1, "processed_items": 0,
                         "retry_eta": (now + timedelta(seconds=100)).isoformat(), "retry_wait_total": 120},
            "config": {"trigger": "webhook", "is_retry_chain": True, "max_retries": 5, "path_count": 1},
        })  # fmt: skip
        route.fulfill(response=response, json=data)

    ctx.route(re.compile(r".*/api/jobs\?page="), handle)
```

(imports: `re`, `from datetime import datetime, timedelta, timezone`.)

- Replace `_capture_surface`, `_capture_dashboard_hero`, `_capture_settings_processing` and `_capture_automation_triggers` with one element-screenshot helper and five captures:

```python
def _capture_element(page: Page, app_url: str, path: str, selector: str, out_path: Path, *, ready: str | None = None) -> None:
    """Screenshot one element: one idea per shot, no full-page scrolls."""
    page.goto(f"{app_url}{path}", wait_until="domcontentloaded", timeout=15_000)
    page.wait_for_selector(selector, state="visible", timeout=15_000)
    if ready:
        page.wait_for_function(ready, timeout=15_000)
    page.wait_for_timeout(1200)
    page.locator(selector).first.screenshot(path=str(out_path), animations="disabled")
    print(f"[regen_readme] wrote {out_path.name}", file=sys.stderr)


TOUR_SHOTS = [
    # (file, app path, element, ready condition)
    ("tour-trigger", "/automation", "#section-webhooks-sonarr-radarr", None),
    ("tour-resolve", "/servers", "#serverList", "() => document.querySelectorAll('#serverList .card').length >= 3"),
    ("tour-extract", "/", "#workerStatusContainer", "() => !!document.querySelector('#workerStatusContainer .progress')"),
    ("tour-retry", "/", "#activeJobsContainer", "() => /Waiting to retry/.test(document.getElementById('activeJobsContainer').innerText)"),
]


def _capture_publish(page: Page, app_url: str, job_id: str, out_path: Path) -> None:
    """The Tears of Steel job's Files tab: one pill per server that received a preview."""
    page.goto(f"{app_url}/?job={job_id}", wait_until="domcontentloaded", timeout=15_000)
    page.click("#filesTab", timeout=15_000)
    page.wait_for_selector("#filesTabPane .badge", state="visible", timeout=15_000)
    page.wait_for_timeout(800)
    page.locator("#filesTabPane").screenshot(path=str(out_path), animations="disabled")
    print(f"[regen_readme] wrote {out_path.name}", file=sys.stderr)
```

In `regenerate()`, after the context and stubs exist: loop `TOUR_SHOTS` through `_capture_element` into `out_dir / f"{name}.png"` then `_to_webp`, and call `_capture_publish` with the Tears of Steel job id (have `seed_jobs` return a dict `{"count": n, "tears_job_id": id}`; update its one caller). Delete the code that wrote `home`, `dashboard`, `servers`, `settings` and `automation`. If the Files tab renders its per-server pills under a different element than `.badge`, use the element the page actually uses (inspect with `page.content()` once) and say so in the docstring.

`scripts/regen_readme_screenshots.sh`: "5 README screenshots" becomes "5 tour screenshots (docs site and README)".

- [ ] **Step 3: Capture, swap references, delete the old shots**

```bash
/home/data/.venv/bin/python tests/e2e/snapshots/regen_readme.py --out docs/images/
git rm -q docs/images/home.webp docs/images/dashboard.webp docs/images/servers.webp docs/images/settings.webp docs/images/automation.webp
/home/data/.venv/bin/python -c "from PIL import Image; import glob, os; [print(p, Image.open(p).size, os.path.getsize(p)//1024, 'KB') for p in sorted(glob.glob('docs/images/tour-*.webp'))]"
```

Expected: five files, each twice its CSS size (for example 2560 px wide for a 1280 px element), each at most 500 KB. Read each image: film titles visible, no "Checking…" pills, no update banner, the new logo in any nav that is in frame.

References: in `README.md`'s screenshot table use `tour-extract.webp` (alt "Dashboard with GPU workers making previews for three films"), `tour-resolve.webp` ("Servers page with one card each for Plex, Jellyfin and Emby"), `tour-publish.webp` ("One film's previews published to Plex, Jellyfin and Emby"), `tour-trigger.webp` ("Sonarr and Radarr webhook setup on the Automation page"); `docs/index.md`: `images/dashboard.webp` becomes `images/tour-extract.webp`; `docs/multi-server.md`: `images/servers.webp` becomes `images/tour-resolve.webp`. `tour-retry.webp` is referenced from `docs/multi-server.md` under its retries section: `![A Jellyfin job waiting to retry while the server indexes a new file](images/tour-retry.webp)`.

- [ ] **Step 4: Tests, review, commit**

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_images.py -q`
Expected: pass (every image referenced, none missing, all under 500 KB).

```bash
git add tests/e2e/snapshots/regen_readme.py tests/e2e/snapshots/readme_fixture.py scripts/regen_readme_screenshots.sh \
  docs/images/tour-*.webp README.md docs/index.md docs/multi-server.md
```

Dispatch `Architecture Review`; fix HIGH; then:

```bash
git commit -F - <<'EOF'
docs: app screenshots at 2x, one idea per shot, with the open films as the jobs

Five tour captures (trigger, resolve, extract, publish, retry) replace the five full-page shots.
Jobs and workers show the lab's open films; the retry state is stubbed on the job list.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

Merge lane C into `stevezau/site-redesign`.

---
## Task 7: Social card from an HTML template

Lane B, after STOP 3 and with Task 3 in lane B (`git merge stevezau/site-redesign` inside `../discover-lab`). Standard review. Suggested model: sonnet.

**Files:**
- Create: `tests/e2e/snapshots/assets/social_preview.html`
- Rewrite: `tests/e2e/snapshots/make_social_preview.py`
- Replace: `docs/images/social-preview.jpg`
- Modify: `tests/test_docs_images.py` (exact 1280x640, size cap, template uses the logo)

**Interfaces:**
- Consumes: approved `players-3up.webp` (from `docs/images/` if Task 9 already committed it, else `/home/data/mlab-openfilms/captures/`); logo A markup from `docs/assets/img/logo.svg`.
- Produces: `docs/images/social-preview.jpg`, 1280x640 baseline JPEG, at most 300 KB (the og:image of every page via `_config.yml` defaults; also the file the owner uploads as the GitHub social preview in Task 13).

- [ ] **Step 1: Tighten the test first**

In `tests/test_docs_images.py`, replace `test_social_preview_is_a_2to1_baseline_jpeg` with:

```python
SOCIAL_TEMPLATE = REPO_ROOT / "tests" / "e2e" / "snapshots" / "assets" / "social_preview.html"


def test_social_preview_is_a_1280x640_baseline_jpeg_under_300kb() -> None:
    path = DOCS / "images" / "social-preview.jpg"
    with Image.open(path) as image:
        assert image.format == "JPEG"
        assert image.size == (1280, 640)
        assert not image.info.get("progressive") and not image.info.get("progression")
    assert path.stat().st_size <= 300 * 1024


def test_social_card_template_draws_the_current_logo() -> None:
    template = SOCIAL_TEMPLATE.read_text(encoding="utf-8")
    logo = (DOCS / "assets" / "img" / "logo.svg").read_text(encoding="utf-8")
    frame = re.search(r'<path d="(M25 8H51[^"]+)"', logo).group(1)
    assert frame in template
    assert 'id="players"' in template
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_images.py -q`
Expected: FAIL (template missing; current card is 1200x600).

- [ ] **Step 2: Write `tests/e2e/snapshots/assets/social_preview.html`**

```html
<!doctype html>
<!--
  Source for docs/images/social-preview.jpg: the card Slack, Discord, Reddit and X show when someone
  shares a docs link, and the image the owner uploads at GitHub > Settings > Social preview.
  Rendered by tests/e2e/snapshots/make_social_preview.py at exactly 1280x640 (2:1, device scale 1).
  The player strip is the approved lab capture, injected into #players at render time so this file
  has no external references. Colours are docs/assets/css/main.css's dark tokens; the mark is
  docs/assets/img/logo.svg inlined.
-->
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Media Preview Generator social preview</title>
    <style>
      :root {
        --bg: #08080a;
        --border: rgba(255, 255, 255, 0.09);
        --text: #f4f4f5;
        --muted: #94949f;
        --amber: #e5a00d;
        --font: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      }
      * {
        box-sizing: border-box;
      }
      body {
        position: relative;
        width: 1280px;
        height: 640px;
        margin: 0;
        padding: 52px 64px 0;
        overflow: hidden;
        background: var(--bg);
        color: var(--text);
        font-family: var(--font);
        -webkit-font-smoothing: antialiased;
      }
      body::before {
        content: "";
        position: absolute;
        inset: -240px auto auto -200px;
        width: 760px;
        height: 620px;
        background: radial-gradient(closest-side, rgba(229, 160, 13, 0.16), transparent);
        pointer-events: none;
      }
      .brand {
        display: flex;
        align-items: center;
        gap: 16px;
        font-size: 26px;
        font-weight: 700;
      }
      .brand svg {
        width: 56px;
        height: 56px;
      }
      h1 {
        max-width: 980px;
        margin: 30px 0 0;
        font-size: 50px;
        line-height: 1.12;
        font-weight: 750;
        letter-spacing: -0.02em;
      }
      h1 em {
        font-style: normal;
        color: var(--amber);
      }
      .ticks {
        display: flex;
        gap: 28px;
        margin: 22px 0 0;
        padding: 0;
        list-style: none;
        color: var(--muted);
        font-size: 20px;
        font-weight: 500;
      }
      .ticks li::before {
        content: "✓ ";
        color: var(--amber);
        font-weight: 700;
      }
      .strip {
        position: absolute;
        left: 64px;
        right: 64px;
        bottom: -8px;
        height: 250px;
        -webkit-mask-image: linear-gradient(to bottom, transparent, black 38%);
        mask-image: linear-gradient(to bottom, transparent, black 38%);
      }
      .strip img {
        display: block;
        width: 100%;
        height: 100%;
        object-fit: cover;
        object-position: center 30%;
        border-radius: 14px 14px 0 0;
        border: 1px solid var(--border);
      }
    </style>
  </head>
  <body>
    <div class="brand">
      <svg viewBox="0 0 64 64" aria-hidden="true">
        <defs><clipPath id="mpg-a-clip"><rect width="64" height="64" rx="15" /></clipPath></defs>
        <rect width="64" height="64" rx="15" fill="#e5a00d" />
        <g fill="#0f0f1a" clip-path="url(#mpg-a-clip)">
          <path d="M25 8H51A5 5 0 0 1 56 13V27A5 5 0 0 1 51 32H42L38 36L34 32H25A5 5 0 0 1 20 27V13A5 5 0 0 1 25 8Z" />
          <rect x="0" y="44" width="64" height="8" />
          <circle cx="38" cy="48" r="8" />
        </g>
        <path fill="#e5a00d" d="M34 14V26L44 20Z" />
      </svg>
      Media Preview Generator
    </div>
    <h1>Preview thumbnails for Plex, Emby and Jellyfin, <em>made on your GPU</em></h1>
    <ul class="ticks">
      <li>Starts when Sonarr or Radarr imports a file</li>
      <li>HDR tone-mapped</li>
      <li>One Docker container</li>
    </ul>
    <div class="strip"><img id="players" alt="" /></div>
  </body>
</html>
```

- [ ] **Step 3: Rewrite `tests/e2e/snapshots/make_social_preview.py`**

```python
#!/usr/bin/env python3
"""Render docs/images/social-preview.jpg, the card shown when someone shares a docs link.

    /home/data/.venv/bin/python tests/e2e/snapshots/make_social_preview.py

The layout is tests/e2e/snapshots/assets/social_preview.html (edit that, not this). The player strip
is the owner-approved 3-up from the lab, injected at render time. Rendered at exactly 1280x640 CSS px
with device scale 1: that is the pixel size unfurlers want, and they crop anything taller than 2:1.
Saved as a baseline JPEG: some unfurlers read only the first bytes to size an image, and WebP support
among them is still patchy.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = Path(__file__).resolve().parent / "assets" / "social_preview.html"
PLAYER_STRIPS = [REPO_ROOT / "docs" / "images" / "players-3up.webp", Path("/home/data/mlab-openfilms/captures/players-3up.webp")]
OUT = REPO_ROOT / "docs" / "images" / "social-preview.jpg"
MAX_BYTES = 300 * 1024


def main() -> int:
    strip = next((path for path in PLAYER_STRIPS if path.is_file()), None)
    if strip is None:
        print("no players-3up.webp in docs/images/ or the lab captures folder (plan Task 5)", file=sys.stderr)
        return 1
    uri = "data:image/webp;base64," + base64.b64encode(strip.read_bytes()).decode()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 640}, device_scale_factor=1)
        page.goto(TEMPLATE.as_uri())
        page.eval_on_selector("#players", "(img, src) => { img.src = src; }", uri)
        page.wait_for_function("() => { const i = document.getElementById('players'); return i.complete && i.naturalWidth > 0; }")
        shot = page.screenshot()
        browser.close()
    image = Image.open(io.BytesIO(shot)).convert("RGB")
    for quality in (90, 86, 82, 78):
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=False)
        if buffer.tell() <= MAX_BYTES:
            break
    OUT.write_bytes(buffer.getvalue())
    print(f"wrote {OUT} ({image.width}x{image.height}, quality {quality}, {OUT.stat().st_size // 1024} KB, strip from {strip})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `/home/data/.venv/bin/python tests/e2e/snapshots/make_social_preview.py && PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_images.py -q`
Expected: `wrote …/social-preview.jpg (1280x640, quality 90, …)`, then all pass. Read the image: headline fully visible, the three players readable at the bottom, nothing clipped at the right edge.

- [ ] **Step 4: Review and commit**

```bash
git add tests/e2e/snapshots/assets/social_preview.html tests/e2e/snapshots/make_social_preview.py docs/images/social-preview.jpg tests/test_docs_images.py
```

Dispatch `Architecture Review`; fix HIGH; then:

```bash
git commit -F - <<'EOF'
docs: social card rendered from an HTML template, showing the players mid-scrub

1280x640 baseline JPEG with the new mark and the approved three-player strip, replacing the
dashboard crop.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

Merge lane B into `stevezau/site-redesign`.

---

## Task 8: Benchmark: Plex's built-in generation vs this app, same files, same machine

Lane B, after Task 4 (Task 5 may run first or alongside; they share the lab but not the same minutes: never run a capture while a benchmark run is timing). Suggested model: opus.

**Files:**
- Create: `scripts/benchmark_previews.py`, `tests/test_benchmark_summary.py`, `docs/benchmark/results.csv`, `docs/benchmark/environment.json`, `docs/benchmark/summary.json`
- After STOP 3 approval only: `docs/benchmark.md`, `docs/_config.yml` (nav entry), `docs/llms-full.txt` (regenerated)

**Interfaces:**
- Consumes: Task 4's lab (`lab_setup.py` helpers imported by path; `mlab-plex` with the GPU; `mlab-site-app`; servers `site-plex`, `site-jellyfin`, `site-emby`); the marker lab's `plexdb.sh` (`/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/plexdb.sh`, read-only SELECTs).
- Produces: `scripts/benchmark_previews.py` with `TOOLS = ("plex-builtin", "app-gpu", "app-cpu")`, `MIN_RUNS = 3`, `MAX_SPREAD = 0.25`, `read_rows(path: Path) -> list[dict[str, str]]`, `summarize(rows: list[dict[str, str]]) -> dict`; `summary.json` = `{"tools": {tool: {"runs": int, "timed": int, "median_seconds": float | None}}, "ratio_plex_over_app_gpu": float | None, "ratio_display": str | None, "reason": str}`. Task 9's stat and `tests/test_site_claims.py` read `ratio_display`.

- [ ] **Step 1: Write the failing summary tests `tests/test_benchmark_summary.py`**

```python
"""The benchmark's summary: the only place a speed number on the site may come from.

summarize() turns docs/benchmark/results.csv into docs/benchmark/summary.json. It must refuse to
produce a Plex-vs-app ratio whenever the Plex side wasn't timed in every run, there are too few runs,
or the runs disagree too much to call one number representative (spec §8: drop the number rather
than estimate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.benchmark_previews import MIN_RUNS, read_rows, summarize

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = REPO_ROOT / "docs" / "benchmark" / "results.csv"
SUMMARY = REPO_ROOT / "docs" / "benchmark" / "summary.json"
PAGE = REPO_ROOT / "docs" / "benchmark.md"


def _rows(tool: str, seconds: list[float], status: str = "timed") -> list[dict[str, str]]:
    return [{"tool": tool, "run": str(i), "status": status, "wall_seconds": str(s)} for i, s in enumerate(seconds, 1)]


class TestSummarize:
    def test_ratio_is_median_plex_time_over_median_gpu_time(self) -> None:
        rows = _rows("plex-builtin", [600, 620, 610]) + _rows("app-gpu", [100, 98, 102]) + _rows("app-cpu", [300, 310, 305])
        summary = summarize(rows)
        assert summary["tools"]["plex-builtin"]["median_seconds"] == 610
        assert summary["tools"]["app-gpu"]["median_seconds"] == 100
        assert summary["ratio_plex_over_app_gpu"] == 6.1
        assert summary["ratio_display"] == "6.1"

    def test_no_ratio_when_any_plex_run_was_untimed(self) -> None:
        rows = _rows("plex-builtin", [600, 620]) + _rows("plex-builtin", [0], status="untimed") + _rows("app-gpu", [100, 98, 102])
        summary = summarize(rows)
        assert summary["ratio_plex_over_app_gpu"] is None
        assert summary["ratio_display"] is None
        assert "Plex" in summary["reason"]

    def test_no_ratio_with_too_few_runs(self) -> None:
        rows = _rows("plex-builtin", [600] * (MIN_RUNS - 1)) + _rows("app-gpu", [100] * MIN_RUNS)
        assert summarize(rows)["ratio_display"] is None

    def test_no_ratio_when_runs_disagree_by_more_than_a_quarter(self) -> None:
        rows = _rows("plex-builtin", [400, 600, 800]) + _rows("app-gpu", [100, 98, 102])
        summary = summarize(rows)
        assert summary["ratio_display"] is None
        assert "varied" in summary["reason"]

    def test_display_rounds_to_one_decimal(self) -> None:
        rows = _rows("plex-builtin", [1000, 1000, 1000]) + _rows("app-gpu", [300, 300, 300])
        assert summarize(rows)["ratio_display"] == "3.3"


class TestCommittedResults:
    def test_summary_is_computed_from_the_committed_results(self) -> None:
        if not RESULTS.is_file():
            pytest.skip("no benchmark results committed yet")
        assert json.loads(SUMMARY.read_text(encoding="utf-8")) == summarize(read_rows(RESULTS))

    def test_benchmark_page_quotes_the_summary(self) -> None:
        if not PAGE.is_file():
            pytest.skip("no benchmark page yet")
        summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
        page = PAGE.read_text(encoding="utf-8")
        if summary["ratio_display"] is None:
            assert "not timed reliably" in page
        else:
            assert f"{summary['ratio_display']}×" in page
            for tool in ("plex-builtin", "app-gpu"):
                assert f"{summary['tools'][tool]['median_seconds']:.0f} s" in page
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_benchmark_summary.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'scripts.benchmark_previews'`.

- [ ] **Step 2: Write `scripts/benchmark_previews.py`**

```python
#!/usr/bin/env python3
"""Time Plex's built-in preview generation against this app, on the same files and the same machine.

Runs on `storage` against the site lab (docs/design/site-redesign-lab/, plan Task 4): the lab Plex
`mlab-plex` (given the GPU, hardware acceleration on) and this branch's app `mlab-site-app`, the
Open Films library, the same frame interval. Appends raw timings to docs/benchmark/results.csv and
records the setup in docs/benchmark/environment.json; `summary` writes docs/benchmark/summary.json.
docs/benchmark.md explains the method. The site may quote a speed number only from summary.json.

    scripts/benchmark_previews.py environment
    scripts/benchmark_previews.py plex --runs 3
    scripts/benchmark_previews.py app-gpu --runs 3
    scripts/benchmark_previews.py app-cpu --runs 3
    scripts/benchmark_previews.py summary

Every run starts with no preview files for these films (this script deletes the lab's BIFs in Plex's
data folder, inside the lab's own Docker volume) and with the films already in the page cache (each
is read once first), so disk speed isn't what gets measured.

- Plex: the clock starts when the first `Plex Transcoder` process working on one of the films shows
  up in `docker top mlab-plex` (polled every 0.25 s) and stops at the newest index-sd.bif mtime. No
  such process within 180 s of starting Plex's GenerateMediaIndexFiles task = `untimed`.
- App: the job's own started_at to completed_at, Plex output only (Jellyfin and Emby are disabled
  in the app for the run, so the app does the same work Plex does).

While a run is going, Plex's preview generation is on for Open Films only: every other library on
the lab Plex is switched off and restored afterwards, as are the server-level settings touched.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAB = REPO_ROOT / "docs" / "design" / "site-redesign-lab"
BENCH = REPO_ROOT / "docs" / "benchmark"
RESULTS = BENCH / "results.csv"
SUMMARY = BENCH / "summary.json"
ENVIRONMENT = BENCH / "environment.json"
PLEXDB = Path("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/plexdb.sh")
PLEX_DATA = "/config/Library/Application Support/Plex Media Server"
TOOLS = ("plex-builtin", "app-gpu", "app-cpu")
MIN_RUNS = 3
MAX_SPREAD = 0.25
APP_CPU_WORKERS = 4
FIELDS = ["tool", "run", "status", "files", "wall_seconds", "started_at", "finished_at", "notes"]


def read_rows(path: Path) -> list[dict[str, str]]:
    """Rows of results.csv, or an empty list if it doesn't exist yet."""
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def summarize(rows: list[dict[str, str]]) -> dict:
    """Medians per tool and the Plex / app-GPU ratio, or no ratio when it can't be trusted.

    Args:
        rows: results.csv rows (`tool`, `run`, `status`, `wall_seconds`, ...).

    Returns:
        {"tools": {tool: {"runs", "timed", "median_seconds"}}, "ratio_plex_over_app_gpu",
        "ratio_display", "reason"}; the two ratio fields are None unless both Plex and app-GPU have
        at least MIN_RUNS runs, every one of them timed, each set within MAX_SPREAD of its median.
    """
    tools: dict[str, dict] = {}
    spread: dict[str, float] = {}
    for tool in TOOLS:
        mine = [row for row in rows if row["tool"] == tool]
        timed = [float(row["wall_seconds"]) for row in mine if row["status"] == "timed"]
        median = statistics.median(timed) if timed else None
        tools[tool] = {"runs": len(mine), "timed": len(timed), "median_seconds": median}
        spread[tool] = (max(timed) - min(timed)) / median if timed and median else 0.0

    def refuse(reason: str) -> dict:
        return {"tools": tools, "ratio_plex_over_app_gpu": None, "ratio_display": None, "reason": reason}

    plex, gpu = tools["plex-builtin"], tools["app-gpu"]
    if plex["runs"] < MIN_RUNS or plex["timed"] != plex["runs"]:
        return refuse(f"Plex's built-in generation was not timed reliably in {MIN_RUNS} runs")
    if gpu["runs"] < MIN_RUNS or gpu["timed"] != gpu["runs"]:
        return refuse(f"the app's GPU runs did not all complete ({MIN_RUNS} needed)")
    if spread["plex-builtin"] > MAX_SPREAD or spread["app-gpu"] > MAX_SPREAD:
        return refuse(f"runs varied by more than {MAX_SPREAD:.0%} of their median")
    ratio = plex["median_seconds"] / gpu["median_seconds"]
    return {
        "tools": tools,
        "ratio_plex_over_app_gpu": round(ratio, 2),
        "ratio_display": f"{ratio:.1f}",
        "reason": f"{MIN_RUNS}+ timed runs each, within {MAX_SPREAD:.0%}",
    }


# ------------------------------------------------------------------------------------------- lab I/O


def _lab():
    sys.path.insert(0, str(LAB))
    import lab_setup  # the lab's API helpers, tokens and constants

    return lab_setup


def _sh(*command: str, check: bool = True) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=check).stdout


def _now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _append(row: dict) -> None:
    BENCH.mkdir(parents=True, exist_ok=True)
    new = not RESULTS.is_file()
    with RESULTS.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)
    print(f"{row['tool']} run {row['run']}: {row['status']} {row['wall_seconds']} s {row['notes']}")


def _part_hashes() -> dict[str, str]:
    """Film path -> Plex bundle hash, read from the lab Plex's own database."""
    out = _sh(str(PLEXDB), "select file, hash from media_parts where file like '/media/openfilms/%';")
    return dict(line.split("|", 1) for line in out.splitlines() if "|" in line)


def _bif(hash_: str) -> str:
    return f"{PLEX_DATA}/Media/localhost/{hash_[0]}/{hash_[1:]}.bundle/Contents/Indexes/index-sd.bif"


def _bif_mtimes(bifs: list[str]) -> list[float | None]:
    times = []
    for bif in bifs:
        out = subprocess.run(["docker", "exec", "mlab-plex", "date", "-r", bif, "+%s.%N"], capture_output=True, text=True)
        times.append(float(out.stdout) if out.returncode == 0 else None)
    return times


def _clear_previews() -> list[str]:
    bifs = [_bif(h) for h in _part_hashes().values()]
    _sh("docker", "exec", "mlab-plex", "rm", "-f", *bifs)
    return bifs


def _warm_cache(lab) -> None:
    for path in sorted(lab.MEDIA_HOST.glob("*/*")):
        if path.suffix.lower() in lab.VIDEO_SUFFIXES:
            subprocess.run(["cat", str(path)], stdout=subprocess.DEVNULL, check=True)


def _transcoder_on_films() -> bool:
    out = _sh("docker", "top", "mlab-plex", "-eo", "pid,args", check=False)
    return any("Plex Transcoder" in line and "/media/openfilms/" in line for line in out.splitlines())


def _gpu_used_by_plex() -> bool:
    out = _sh("nvidia-smi", "--query-compute-apps=process_name", "--format=csv,noheader", check=False)
    return "Plex Transcoder" in out


def _plex_prefs(lab) -> dict[str, dict]:
    return {s["id"]: s for s in lab.plex("GET", "/:/prefs")["MediaContainer"]["Setting"]}


# ------------------------------------------------------------------------------------------------ runs


def run_plex(run: int) -> None:
    lab = _lab()
    prefs = _plex_prefs(lab)
    needed = ("GenerateBIFBehavior", "GenerateBIFFrameInterval", "HardwareAcceleratedCodecs")
    missing = [pref for pref in needed if pref not in prefs]
    if missing:
        ids = sorted(pref for pref in prefs if "BIF" in pref or "Hardware" in pref)
        raise SystemExit(f"Plex has no pref {missing}; BIF/hardware prefs it does have: {ids}. Update this script.")
    sections = lab.plex("GET", "/library/sections")["MediaContainer"]["Directory"]
    ours = lab.plex_section()
    saved_server = {pref: prefs[pref]["value"] for pref in ("GenerateBIFBehavior", "HardwareAcceleratedCodecs")}
    saved_sections = {}
    for section in sections:
        section_prefs = {s["id"]: s["value"] for s in lab.plex("GET", f"/library/sections/{section['key']}/prefs")["MediaContainer"]["Setting"]}
        saved_sections[section["key"]] = section_prefs.get("enableBIFGeneration")
    films = lab.films()
    bifs = _clear_previews()
    for item in lab.plex("GET", f"/library/sections/{ours}/all")["MediaContainer"]["Metadata"]:
        lab.plex("PUT", f"/library/metadata/{item['ratingKey']}/analyze")
    _warm_cache(lab)
    notes = f"interval={prefs['GenerateBIFFrameInterval']['value']}s"
    try:
        for key in saved_sections:
            lab.plex("PUT", f"/library/sections/{key}/prefs", enableBIFGeneration=1 if key == ours else 0)
        lab.plex("PUT", "/:/prefs", GenerateBIFBehavior="scheduled", HardwareAcceleratedCodecs=1)
        lab.plex("POST", "/butler/GenerateMediaIndexFiles")
        asked = time.time()
        started = None
        while time.time() - asked < 180:
            if _transcoder_on_films():
                started = time.time()
                break
            time.sleep(0.25)
        if started is None:
            _append({"tool": "plex-builtin", "run": run, "status": "untimed", "files": len(films), "wall_seconds": "",
                     "started_at": _now_iso(asked), "finished_at": "", "notes": "no Plex Transcoder on the films within 180 s"})  # fmt: skip
            return
        gpu_seen = False
        while True:
            gpu_seen = gpu_seen or _gpu_used_by_plex()
            mtimes = _bif_mtimes(bifs)
            if all(m is not None for m in mtimes) and not _transcoder_on_films():
                break
            if time.time() - started > 4 * 3600:
                raise SystemExit("Plex took over 4 hours; stopping (run recorded nothing)")
            time.sleep(1)
        finished = max(mtimes)
        _append({"tool": "plex-builtin", "run": run, "status": "timed", "files": len(films),
                 "wall_seconds": f"{finished - started:.1f}", "started_at": _now_iso(started),
                 "finished_at": _now_iso(finished), "notes": f"{notes} plex_gpu_seen={gpu_seen}"})  # fmt: skip
    finally:
        for key, value in saved_sections.items():
            if value is not None:
                lab.plex("PUT", f"/library/sections/{key}/prefs", enableBIFGeneration=value)
        lab.plex("PUT", "/:/prefs", **saved_server)


def run_app(mode: str, run: int) -> None:
    lab = _lab()
    interval = int(_plex_prefs(lab)["GenerateBIFFrameInterval"]["value"])
    saved = lab.app("GET", "/api/settings")
    gpus = saved.get("gpu_config") or []
    if mode == "gpu" and not any(g.get("enabled") for g in gpus):
        raise SystemExit("the app has no enabled GPU; check Settings > Processing Options")
    settings = {"thumbnail_interval": interval}
    if mode == "gpu":
        settings |= {"cpu_threads": 0}
    else:
        settings |= {"gpu_config": [{**g, "enabled": False} for g in gpus], "cpu_threads": APP_CPU_WORKERS}
    films = lab.films()
    for server in ("site-jellyfin", "site-emby"):
        lab.app("PUT", f"/api/servers/{server}", {"enabled": False})
    try:
        lab.app("POST", "/api/settings", settings)
        _clear_previews()
        _warm_cache(lab)
        sent = time.time()
        lab.app("POST", "/api/webhooks/custom", {"file_paths": films, "title": f"benchmark {mode} {run}"})

        def done() -> list[dict] | None:
            jobs = [j for j in lab.app("GET", "/api/jobs?page=1&per_page=50")["jobs"] if lab.parse_timestamp(j["created_at"]) >= sent - 5]
            finished = [j for j in jobs if j["status"] in ("completed", "failed", "cancelled")]
            return finished if jobs and len(finished) == len(jobs) else None

        jobs = lab.wait_until("the benchmark job", done, timeout=4 * 3600, every=2)
        started = min(lab.parse_timestamp(j["started_at"]) for j in jobs)
        finished = max(lab.parse_timestamp(j["completed_at"]) for j in jobs)
        ok = all(j["status"] == "completed" for j in jobs)
        workers = f"gpu_workers={sum(g.get('workers', 0) for g in gpus if g.get('enabled'))}" if mode == "gpu" else f"cpu_workers={APP_CPU_WORKERS}"
        _append({"tool": f"app-{mode}", "run": run, "status": "timed" if ok else "failed", "files": len(films),
                 "wall_seconds": f"{finished - started:.1f}", "started_at": _now_iso(started),
                 "finished_at": _now_iso(finished), "notes": f"interval={interval}s {workers}"})  # fmt: skip
    finally:
        lab.app("POST", "/api/settings", {k: saved[k] for k in ("thumbnail_interval", "cpu_threads", "gpu_config") if k in saved})
        for server in ("site-jellyfin", "site-emby"):
            lab.app("PUT", f"/api/servers/{server}", {"enabled": True})


def environment() -> None:
    lab = _lab()
    prefs = _plex_prefs(lab)
    files = []
    for path in sorted(lab.MEDIA_HOST.glob("*/*")):
        if path.suffix.lower() not in lab.VIDEO_SUFFIXES:
            continue
        probe = json.loads(_sh("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=codec_name,profile,width,height,r_frame_rate,color_transfer:format=duration,size",
                               "-of", "json", str(path)))  # fmt: skip
        files.append({"file": path.name, **probe["streams"][0], **probe["format"]})
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host_cpu": _sh("sh", "-c", "lscpu | sed -n 's/^Model name: *//p'").strip(),
        "host_cpus": _sh("nproc").strip(),
        "gpu": _sh("nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader").strip(),
        "plex_version": lab.plex("GET", "/identity")["MediaContainer"]["version"],
        "plex_prefs": {k: prefs[k]["value"] for k in prefs if "BIF" in k or k == "HardwareAcceleratedCodecs"},
        "app_image": _sh("docker", "inspect", "mlab-site-app", "--format", "{{.Config.Image}} {{.Image}}").strip(),
        "app_commit": _sh("git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD").strip(),
        "app_ffmpeg": _sh("docker", "exec", "mlab-site-app", "sh", "-c", "ffmpeg -version | head -1").strip(),
        "app_settings": {k: v for k, v in lab.app("GET", "/api/settings").items() if k in ("gpu_config", "cpu_threads", "thumbnail_interval", "thumbnail_quality")},
        "files": files,
    }
    BENCH.mkdir(parents=True, exist_ok=True)
    ENVIRONMENT.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {ENVIRONMENT}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["environment", "plex", "app-gpu", "app-cpu", "summary"])
    parser.add_argument("--runs", type=int, default=MIN_RUNS)
    args = parser.parse_args()
    if args.what == "environment":
        environment()
    elif args.what == "summary":
        summary = summarize(read_rows(RESULTS))
        SUMMARY.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
    else:
        done = len([row for row in read_rows(RESULTS) if row["tool"] == ("plex-builtin" if args.what == "plex" else args.what)])
        for run in range(done + 1, done + args.runs + 1):
            run_plex(run) if args.what == "plex" else run_app(args.what.removeprefix("app-"), run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```


Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_benchmark_summary.py -q && ruff check scripts/benchmark_previews.py && ruff format --check scripts/benchmark_previews.py`
Expected: `5 passed, 2 skipped`, ruff clean.

- [ ] **Step 3: Record the environment, then run the benchmark**

Nothing else may use the GPU or the lab during a run (no captures, no marker-lab jobs): check `nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader` is empty and `docker ps --filter name=mlab-app -q` jobs are idle first.

```bash
chmod +x scripts/benchmark_previews.py
scripts/benchmark_previews.py environment
scripts/benchmark_previews.py app-gpu --runs 3
scripts/benchmark_previews.py app-cpu --runs 3
scripts/benchmark_previews.py plex --runs 3
scripts/benchmark_previews.py summary
PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_benchmark_summary.py -q
```

Expected: nine result lines, a summary, then `6 passed, 1 skipped`. Run Plex last: its runs are the slowest and, if Plex never starts (`untimed`), the app numbers are still recorded. After the Plex runs, confirm the prefs came back: `curl -s "http://127.0.0.1:32402/:/prefs?X-Plex-Token=$TOKEN" -H 'Accept: application/json' | jq -r '.MediaContainer.Setting[] | select(.id=="GenerateBIFBehavior") | .value'` equals `environment.json`'s `plex_prefs.GenerateBIFBehavior`. Then re-run `docs/design/site-redesign-lab/lab_setup.py generate` and `verify` so the lab's previews are this app's again (the Plex runs left Plex-made BIFs behind).

If `summary` prints `"ratio_display": null`, that is a valid result: the site states no speed number. Do not re-run until a number appears.

- [ ] **Step 4: STOP — owner checkpoint 3 (captures and numbers, before anything goes on the site)**

Send the owner, in one message:
- The five images from `/home/data/mlab-openfilms/captures/` (`player-plex.webp`, `player-jellyfin.webp`, `player-emby.webp` or the reason it's missing, `players-3up.webp`, `hdr-before-after.webp`), with the proof URL for each from `captures.json`.
- Which image is proposed for the hero (default `player-plex.webp`) and which scrub position/moment was used.
- `docs/benchmark/summary.json` and `environment.json` in plain words: hardware, Plex version and settings (interval, hardware acceleration, whether Plex used the GPU), app settings, per-tool medians, the ratio or the reason there is none.
- The films and licences from `films.json`, and whether the HDR title needed its tags added.

Wait for an explicit yes. Changes they ask for loop back to Task 5 or Step 3 here. Nothing from this list is committed into `docs/images/`, `docs/_data/` or any page before that yes.

- [ ] **Step 5 (after the yes): the benchmark page**

`docs/benchmark.md` (numbers are filled from `summary.json` and `environment.json` exactly; the test in Step 1 checks the ratio and both medians appear):

````markdown
---
title: Plex preview thumbnail generation benchmark, built-in vs GPU
heading: Benchmark
description: Plex's built-in preview thumbnail generation against Media Preview Generator on the same files and machine, with the method and every raw timing.
facts_checked: <date of the runs, YYYY-MM-DD>
---

This is my project, so treat this page as a claim to check, not a review. The method, the raw timings
and the script are all here, so you can run it on your own hardware.

## Result

<!-- one of the two paragraphs below, matching summary.json -->
On these films and this machine, Plex's built-in generator took a median of **<plex median> s** and
Media Preview Generator on the GPU took **<app-gpu median> s**: **<ratio_display>× as long** for Plex.
On the CPU alone (4 workers) the app took <app-cpu median> s.

Plex's built-in generation was not timed reliably, so this page gives no comparison. <reason from summary.json>.

## What was measured

- Files: the <n> films in the lab library (see [credits](credits.md)), <total duration> of video, <resolutions and codecs from environment.json>.
- Machine: <host_cpu> (<host_cpus> threads), <gpu>.
- Plex Media Server <plex_version>, one frame every <GenerateBIFFrameInterval> s (Plex's default), hardware acceleration on, the GPU passed to the container. Plex <did / did not> run its transcoder on the GPU during the runs.
- Media Preview Generator <app_commit>, one frame every <same interval> s to match Plex, default GPU settings (<gpu_workers> worker). Only Plex output, so both did the same work.
- Three runs each. Every run started with no preview files and with the films already read once, so disk speed was not part of it.

## How the clock works

- Plex: from the moment Plex's transcoder starts on the first film to the moment the last preview file is written.
- This app: the job's own start and finish times.

## Limits

- One machine and one GPU. A different GPU, CPU or disk changes both numbers.
- Plex and this app don't pick frames the same way, so the files aren't byte-for-byte equivalent work.
- Plex normally runs this job in its maintenance window, and optionally when media is added. The benchmark starts it on demand.

## Raw data and how to run it

- [results.csv](benchmark/results.csv): every run.
- [environment.json](benchmark/environment.json): versions, settings and the files.
- [summary.json](benchmark/summary.json): the numbers above, computed from results.csv.
- Script: [scripts/benchmark_previews.py](https://github.com/stevezau/media_preview_generator/blob/dev/scripts/benchmark_previews.py).
````

Every `<…>` above is a value to copy from `summary.json`/`environment.json` when writing the page; keep only the matching Result paragraph and delete the HTML comment. Add to `docs/_config.yml` `nav`, after FAQ:

```yaml
  - title: Benchmark
    url: /benchmark/
    blurb: Plex's built-in generation vs this app, same files, raw timings
```

`docs/credits.md` doesn't exist until Task 10; until then link the films line to `https://github.com/stevezau/media_preview_generator/blob/dev/docs/design/site-redesign-lab/films.json` and switch it to `credits.md` in Task 10.

Run: `python scripts/generate_llms_full.py && PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_benchmark_summary.py tests/test_llms_full.py tests/test_docs_links.py -q`
Expected: all pass.

- [ ] **Step 6: Review and commit**

```bash
git add scripts/benchmark_previews.py tests/test_benchmark_summary.py docs/benchmark docs/benchmark.md docs/_config.yml docs/llms-full.txt
```

Dispatch `Architecture Review` (ask it to read `summarize()` against the tests with particular care: this function decides whether a number may be published); fix HIGH; then:

```bash
git commit -F - <<'EOF'
docs: benchmark Plex's built-in preview generation against the app, with raw timings

Same files, same machine, three runs each; the summary refuses a ratio unless every run was timed
and the runs agree within 25%. The page quotes summary.json, which a test recomputes from results.csv.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 9: Landing page

Lane A, after Task 2, with lane C (Task 6) merged. Steps 1-7 don't need the player captures; Step 8 needs STOP 3 passed. Suggested model: opus (layout and copy).

**Files:**
- Create: `docs/_layouts/home.html`, `docs/_data/stats.yml`, `docs/_data/tour.yml`, `docs/_data/features.yml`, `docs/_data/works_with.yml`, `tests/test_site_claims.py`
- Rewrite: `docs/_data/faq.yml` (8 questions worded as searched)
- Modify: `docs/index.md` (layout home, body removed), `docs/faq.md` (a "Common questions" section; two headings renamed), `docs/assets/css/main.css` (figure rules), `tests/test_docs_site.py` (landing assertions; FAQPage required on home too), `docs/llms-full.txt`
- Copy in (Step 8): `docs/images/player-plex.webp`, `docs/images/players-3up.webp`, `docs/images/hdr-before-after.webp`

**Interfaces:**
- Consumes: Task 1 theme classes and `faq-jsonld.html`; Task 6 `docs/images/tour-*.webp`; Task 5 captures (after STOP 3); Task 8 `docs/benchmark/summary.json` (after STOP 3).
- Produces: `docs/_data/stats.yml` entries `{value: str, unit?: str, label: str, note: str, source: "fact" | "benchmark"}`; `tour.yml` entries `{title, body, image, alt, path, width, height}` (width/height = half the image's pixel size); `features.yml` `{title, body, icon}`; `works_with.yml` `{group, items: [{name, note}]}`; section ids on `/`: `how-it-works`, `three-servers`, `hdr`, `features`, `works-with`, `compare`, `install`, `faq`.

- [ ] **Step 1: Write the failing tests**

`tests/test_site_claims.py`:

```python
"""Speed claims on the site, the READMEs and llms.txt must come from the benchmark.

Spec rule (docs/design/site-redesign.md §1, §8): a speed number is published only from
docs/benchmark/summary.json, which a test recomputes from the raw results. A "5x faster" typed into a
card or a README has no source and goes stale the day the benchmark is re-run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
STATS = DOCS / "_data" / "stats.yml"
SUMMARY = DOCS / "benchmark" / "summary.json"
CLAIM = re.compile(
    r"(?i)\b\d+(?:\.\d+)?\s*(?:x|×|times)\s+(?:faster|quicker|slower|as fast)\b"
    r"|\b\d+(?:\.\d+)?\s*%\s+(?:faster|quicker|slower|less time)\b"
)


def _sources() -> list[Path]:
    pages = [p for p in DOCS.rglob("*.md") if not {"design", "vendor"} & set(p.relative_to(DOCS).parts) and p.name != "benchmark.md"]
    return sorted(
        [REPO_ROOT / "README.md", REPO_ROOT / "DOCKERHUB_README.md", DOCS / "llms.txt", *pages,
         *DOCS.glob("_data/*.yml"), *DOCS.glob("_layouts/*.html"), *DOCS.glob("_includes/*.html")]  # fmt: skip
    )


@pytest.mark.parametrize("source", _sources(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_no_speed_claim_outside_the_benchmark(source: Path) -> None:
    claims = CLAIM.findall(source.read_text(encoding="utf-8"))
    assert not claims, f"{source.relative_to(REPO_ROOT)} states a speed number: {claims}. Use a stat with source: benchmark."


def test_every_stat_names_its_source() -> None:
    stats = yaml.safe_load(STATS.read_text(encoding="utf-8"))
    assert stats
    assert all(stat.get("source") in {"fact", "benchmark"} for stat in stats)


def test_benchmark_stats_equal_the_summary() -> None:
    benchmark_stats = [s for s in yaml.safe_load(STATS.read_text(encoding="utf-8")) if s["source"] == "benchmark"]
    if not benchmark_stats:
        return
    assert SUMMARY.is_file(), "a benchmark stat needs docs/benchmark/summary.json"
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    assert summary["ratio_display"] is not None, f"no ratio to publish: {summary['reason']}"
    assert [s["value"] for s in benchmark_stats] == [summary["ratio_display"]] * len(benchmark_stats)
```

In `tests/test_docs_site.py`:
1. In `TestFaq.test_faq_json_ld_matches_the_data_file`, replace `assert "faq/index.html" in carriers` and the `<=` line with `assert set(carriers) == {"faq/index.html", "index.html"}`.
2. Add:

```python
LANDING_SECTIONS = ["how-it-works", "three-servers", "hdr", "features", "works-with", "compare", "install", "faq"]


class TestLanding:
    def test_sections_appear_in_spec_order(self, site: Path) -> None:
        home = (site / "index.html").read_text(encoding="utf-8")
        positions = [home.find(f'id="{section}"') for section in LANDING_SECTIONS]
        assert -1 not in positions, dict(zip(LANDING_SECTIONS, positions))
        assert positions == sorted(positions)

    def test_stats_come_from_the_data_file(self, site: Path) -> None:
        stats = yaml.safe_load((DOCS_DIR / "_data" / "stats.yml").read_text(encoding="utf-8"))
        assert (site / "index.html").read_text(encoding="utf-8").count('<li class="stat">') == len(stats)

    def test_landing_pictures_reserve_half_their_pixel_size(self, site: Path) -> None:
        # width/height stop the page reflowing as pictures load; a stale pair does the opposite.
        from PIL import Image

        home = (site / "index.html").read_text(encoding="utf-8")
        tags = re.findall(r'<img[^>]+src="/images/[^"]+"[^>]*>', home)
        sized = re.findall(r'<img[^>]+src="/images/([^"]+)"[^>]*width="(\d+)" height="(\d+)"', home)
        assert len(sized) == len(tags), "a landing picture has no numeric width/height"
        for name, width, height in sized:
            with Image.open(DOCS_DIR / "images" / name) as image:
                assert image.size == (2 * int(width), 2 * int(height)), name

    def test_every_landing_picture_has_alt_text(self, site: Path) -> None:
        home = (site / "index.html").read_text(encoding="utf-8")
        pictures = re.findall(r'<img[^>]+src="/images/[^"]+"[^>]*>', home)
        assert pictures
        assert [tag for tag in pictures if not re.search(r'alt="[^"]+"', tag)] == []
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_site_claims.py tests/test_docs_site.py -q`
Expected: FAIL (`stats.yml` missing; no landing sections).

- [ ] **Step 2: Landing data files**

`docs/_data/stats.yml`:

```yaml
# The numbers under the hero. Each is a count you can check on this site, or (source: benchmark) the
# ratio in docs/benchmark/summary.json, which tests/test_site_claims.py compares it against.
- value: "1"
  label: decode per file
  note: Several servers hold the same file? It is read once, and each server gets its own format.
  source: fact
- value: "3"
  label: media servers
  note: Plex, Emby and Jellyfin, in any mix, from one container.
  source: fact
- value: "1"
  label: webhook URL
  note: Sonarr, Radarr, Tdarr and the servers all post to the same address. The app works out who sent it.
  source: fact
```

`docs/_data/tour.yml` (after writing, set every `width`/`height` to half the pixel size that Task 6 printed; the Step 1 test checks it):

```yaml
# The landing page's tour: what happens to one new file, in order, each step beside the screen where
# it happens. Captured by tests/e2e/snapshots/regen_readme.py against a fixture (fake servers, the lab
# films as titles). width/height are the CSS size the page reserves: half the 2x capture's pixels.
# `path` is the app path shown in the frame's address bar.
- title: A new file arrives
  body: >-
    Sonarr, Radarr, Tdarr or the media server itself posts to one webhook URL, and the app works out
    who sent it. Scheduled scans and a hand-picked list work too.
  image: tour-trigger.webp
  alt: The Automation page showing the webhook URL and the steps to add it in Sonarr and Radarr
  path: /automation
  width: 640
  height: 400
- title: It finds every server with that file
  body: >-
    The path Sonarr reports is mapped to the path the container sees. Then every Plex, Emby and
    Jellyfin library that holds the file is found.
  image: tour-resolve.webp
  alt: The Servers page with one card each for Plex, Jellyfin and Emby
  path: /servers
  width: 640
  height: 400
- title: One GPU pass per file
  body: >-
    FFmpeg decodes the video on the GPU, jumps between key frames when the file allows it, and
    tone-maps HDR. If the GPU can't decode a file, the same worker tries again on the CPU.
  image: tour-extract.webp
  alt: GPU workers on the dashboard, each making previews for a different film
  path: /
  width: 640
  height: 400
- title: Each server gets its own format
  body: >-
    A BIF in Plex's data folder, a BIF next to the video for Emby, trickplay tiles for Jellyfin. Then
    each server is told to pick them up.
  image: tour-publish.webp
  alt: One film's job, showing previews published to Plex, Jellyfin and Emby
  path: /
  width: 640
  height: 400
- title: It waits for slow servers
  body: >-
    A server that hasn't indexed the new file yet gets another try after 1, 2 and 5 minutes. Next
    time, files whose previews are current are skipped.
  image: tour-retry.webp
  alt: A Jellyfin job waiting to retry while the server indexes a new film
  path: /
  width: 640
  height: 400
```

`docs/_data/features.yml`:

```yaml
# The feature grid: one idea per card, plain words, nothing every self-hosted app has.
# `icon` is inline SVG (the site loads no icon font).
- title: Uses the GPU you have
  body: >-
    NVIDIA, Intel and AMD on Linux; NVIDIA on Windows through WSL2. Every GPU passed to the container
    gets its own workers.
  icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>
- title: Falls back to the CPU by itself
  body: >-
    A file the GPU can't decode is retried on the CPU by the same worker. There is no second pool to
    set up.
  icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.2L3 16M3 21v-5h5"/></svg>
- title: Starts per file
  body: >-
    Sonarr, Radarr, Sportarr, Tdarr, FileFlows, Plex, Emby and Jellyfin webhooks, or any JSON with a
    path. A new episode gets its previews without waiting for a nightly task.
  icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>
- title: One decode, every server
  body: >-
    With Plex and Jellyfin on the same library, each file is read once and both servers get their own
    output.
  icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/></svg>
- title: Skips what's already done
  body: >-
    A small file next to each preview records the source's size and date. A quality upgrade from
    Radarr is redone; an untouched file is skipped.
  icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>
- title: Says why previews don't show
  body: >-
    Previews Readiness checks each server's settings, explains the ones that stop previews appearing,
    and offers to fix them one library at a time.
  icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4V3h6v1M9 13l2 2 4-4"/></svg>
```

`docs/_data/works_with.yml`:

```yaml
# Everything the app talks to, grouped by whether you need it. Three groups: one row of three cards.
- group: Required
  items:
    - name: Plex, Emby or Jellyfin
      note: any mix; Jellyfin 10.10 or newer
    - name: Docker
      note: linux/amd64 or linux/arm64, Unraid template included
    - name: Write access where previews live
      note: Plex's data folder, or the media folder for Emby and Jellyfin
- group: GPUs (optional)
  items:
    - name: NVIDIA
      note: CUDA decoding, also on Windows through WSL2
    - name: Intel and AMD
      note: VAAPI decoding on Linux
    - name: No GPU
      note: CPU workers do the same job
- group: Automation (optional)
  items:
    - name: Sonarr and Radarr
      note: import and upgrade webhooks
    - name: Tdarr and FileFlows
      note: a webhook after each transcode
    - name: The servers themselves
      note: Plex (Plex Pass), Emby (Premiere) and Jellyfin webhooks, or Recently Added polling
```

`docs/_data/faq.yml` (replaces Task 1's seed; every answer is backed by the page it links):

```yaml
# Short FAQ: the landing page shows these, and they are the FAQPage structured data on / and /faq/.
# Worded the way people type them into a search box. Every q must also be a heading on docs/faq.md
# (tested). Links are site paths: data files aren't rewritten by jekyll-relative-links.
- q: Why are my Plex preview thumbnails taking so long?
  a: >-
    Plex makes them inside the server, on the CPU, one frame every 2 seconds by default, during its
    scheduled maintenance window and optionally when media is added. Plex's own help page says a
    large library can take days. You can space the frames out in Plex, or hand the job to a GPU with
    this app, which starts on each file as it arrives. [More](/plex-preview-thumbnails-slow/)
- q: Can I generate Plex preview thumbnails with a GPU?
  a: >-
    Not with Plex itself: Plex documents no GPU option for this job. Media Preview Generator runs it
    with FFmpeg on an NVIDIA, Intel or AMD GPU and writes the result where Plex expects it, in Plex's
    data folder, which it needs read-write. [More](/plex-preview-thumbnails-gpu/)
- q: How do I make Jellyfin trickplay generation faster?
  a: >-
    Start with Jellyfin's own settings: hardware decoding, key-frame-only extraction, and more FFmpeg
    threads than the default of 1. If it still falls behind, this app can make the trickplay tiles on
    a GPU, on another machine if you like, and tell Jellyfin about them. [More](/jellyfin-trickplay-gpu/)
- q: Does Emby's thumbnail extraction use the GPU?
  a: >-
    No. Emby's built-in BIF extraction has no GPU option. This app makes Emby BIF files on a GPU and
    saves them next to each video, where Emby looks for them, so the media folder must be writable.
    [More](/emby-bif-thumbnails-gpu/)
- q: Can previews be made as soon as Sonarr or Radarr imports a file?
  a: >-
    Yes. Add a webhook in Sonarr or Radarr for import and upgrade. The app waits for a 60-second quiet
    period, makes the previews, and retries while the media server is still indexing the new file.
    [More](/sonarr-radarr-preview-thumbnails/)
- q: Why are my HDR or Dolby Vision preview thumbnails washed out or green?
  a: >-
    The frames were grabbed without tone mapping, so HDR colours land in an ordinary picture. This app
    tone-maps HDR10, HLG and HDR10+, and uses the HDR10 layer of Dolby Vision profiles 7 and 8. Profile
    5 needs a GPU with a hardware Vulkan driver in the container. [More](/hdr-dolby-vision-thumbnails/)
- q: Do I need a GPU to use Media Preview Generator?
  a: >-
    No. Without one it runs on CPU workers; set how many in Settings, Processing Options. With a GPU,
    decoding moves off the CPU, and a file the GPU can't decode is retried on the CPU by the same worker.
- q: Does it run on Windows or Unraid?
  a: >-
    Yes, in Docker. There is an Unraid Community Applications template. On Windows (Docker Desktop with
    WSL2) only NVIDIA GPUs are used; Intel and AMD GPUs there, and everything on macOS, run on the CPU.
    [More](/getting-started/#windows)
```

- [ ] **Step 3: Put the eight questions on the FAQ page**

In `docs/faq.md`:
- After the "## Related Docs" section, add `## Common questions` containing, for each of the first six `faq.yml` entries, `### <q>` followed by the same answer text, with the trailing `[More](/x/)` turned into a Markdown link to the page (`[More: Why Plex previews are slow](plex-preview-thumbnails-slow.md)`), so the page stays GitHub-friendly.
- Rename `### Can I use this without a GPU?` to `### Do I need a GPU to use Media Preview Generator?` and start its answer with "No." plus the existing text.
- Rename `### Does this work on Windows?` to `### Does it run on Windows or Unraid?` and add to its answer: `There is an Unraid Community Applications template: see [Unraid](getting-started.md#unraid).`
- Add `- [Common questions](#common-questions)` at the top of the "## Contents" list.
- `grep -rn "can-i-use-this-without-a-gpu\|does-this-work-on-windows" docs README.md` and point any hit at the new anchors (`do-i-need-a-gpu-to-use-media-preview-generator`, `does-it-run-on-windows-or-unraid`).

- [ ] **Step 4: Write `docs/_layouts/home.html`**

```html
---
layout: default
---
{%- comment -%}
The landing page. Section order is docs/design/site-redesign.md §4. House style (§7): plain words,
few em dashes, one concrete example per section, nothing every self-hosted app has, and no speed
number that doesn't come from docs/benchmark/ (tests/test_site_claims.py). Copy lives here and in
_data/*.yml; the pictures are real captures from the lab (docs/design/site-redesign-lab/).
{%- endcomment -%}
<main id="main">

  <section class="hero">
    <div class="wrap">
      <div class="hero__inner">
        <h1>Scrub to the right scene <em>on the first try</em></h1>
        <p class="hero__sub">
          Media Preview Generator makes the preview thumbnails Plex, Emby and Jellyfin show above the
          timeline while you drag. It does the work on your GPU in one Docker container, and starts on
          each new file as soon as Sonarr or Radarr imports it.
        </p>
        <div class="hero__cta">
          <a class="btn btn--primary btn--lg" href="{{ '/getting-started/' | relative_url }}">
            Get started
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>
          </a>
          <a class="btn btn--ghost btn--lg" href="{{ site.repo }}">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 .5a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2c-3.3.7-4-1.6-4-1.6-.6-1.4-1.4-1.8-1.4-1.8-1.1-.7 0-.7 0-.7 1.2.1 1.9 1.2 1.9 1.2 1.1 1.9 2.9 1.3 3.6 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2 0-.4-.5-1.6.2-3.2 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0C17.4 4.7 18.4 5 18.4 5c.7 1.6.2 2.8.1 3.2.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A12 12 0 0 0 12 .5Z"/></svg>
            View source
          </a>
        </div>
        <p class="hero__meta">
          <span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg> One Docker container</span>
          <span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg> Plex, Emby and Jellyfin</span>
          <span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg> CPU fallback built in</span>
          {%- comment -%} A requirement, not a reassurance: an info mark, and a link to the mounts it means. {%- endcomment -%}
          <a href="{{ '/getting-started/#volume-mounts' | relative_url }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 16v-5M12 8h.01"/></svg> Needs write access where previews live</a>
        </p>
      </div>

      <figure class="hero__shot">
        <div class="frame">
          <img src="{{ '/images/player-plex.webp' | relative_url }}" width="1280" height="720"
               alt="Plex's web player paused on Tears of Steel, with a preview thumbnail above the seek bar">
        </div>
        <figcaption>
          Plex's web player on a test server, mid-scrub. The thumbnail above the timeline came from this
          app. Tears of Steel, (CC) Blender Foundation.
        </figcaption>
      </figure>
    </div>
  </section>

  <section class="stats-band" aria-label="Media Preview Generator at a glance">
    <div class="wrap">
      <ul class="stats" role="list">
        {%- for s in site.data.stats -%}
        <li class="stat">
          <p class="stat__value"><span data-countup>{{ s.value }}</span>{% if s.unit %}<span class="stat__unit">{{ s.unit }}</span>{% endif %}</p>
          <p class="stat__label">{{ s.label }}</p>
          <p class="stat__note">{{ s.note }}{% if s.source == "benchmark" %} <a href="{{ '/benchmark/' | relative_url }}">How it was measured</a>{% endif %}</p>
        </li>
        {%- endfor -%}
      </ul>
    </div>
  </section>

  <section class="section section--tint">
    <div class="wrap reveal">
      <div class="section__head section__head--center" style="margin-bottom:0">
        <span class="eyebrow">The problem</span>
        <h2>The built-in generators fall behind</h2>
        <p class="section__lede">
          Plex, Emby and Jellyfin can make these thumbnails themselves. They do it inside the media server,
          mostly on the CPU, and mostly on a schedule; Plex's own help page warns that a large library can
          take days. Until then, the episode that arrived on Friday night has a timeline with no pictures on
          it, and finding the scene where you fell asleep means guessing.
        </p>
        <p class="section__lede" style="margin-top:1rem">
          Media Preview Generator does the job on the GPU, one file at a time, as each file arrives.
        </p>
      </div>
    </div>
  </section>

  <section class="section" id="how-it-works">
    <div class="wrap">
      <div class="section__head section__head--center">
        <span class="eyebrow">How it works</span>
        <h2>From a new file to a preview on every server</h2>
        <p class="section__lede">You set it up once. After that, every new file goes through these five steps on its own.</p>
      </div>
      {%- comment -%} Markup and behaviour are Shortlist's tour, unchanged: see its comments in main.css and site.js. {%- endcomment -%}
      <div class="tour" style="--tour-steps:{{ site.data.tour | size }}">
        <div class="tour__rail" aria-hidden="true"><span class="tour__rail-fill"></span></div>
        {%- for s in site.data.tour -%}
        <div class="tour-step">
          <p class="tour-step__num" aria-hidden="true">{{ forloop.index }}</p>
          <h3 class="tour-step__title">{{ s.title }}</h3>
          <p class="tour-step__body">{{ s.body }}</p>
        </div>
        <figure class="tour-panel">
          <div class="frame frame--chrome">
            <div class="frame__bar">
              <span class="frame__dot"></span><span class="frame__dot"></span><span class="frame__dot"></span>
              <span class="frame__title">{{ site.demo_host }}{{ s.path }}</span>
            </div>
            <img src="{{ s.image | prepend: '/images/' | relative_url }}" alt="{{ s.alt }}" loading="lazy" width="{{ s.width }}" height="{{ s.height }}">
          </div>
        </figure>
        {%- endfor -%}
      </div>
    </div>
  </section>

  <section class="section section--tint" id="three-servers">
    <div class="wrap reveal">
      <div class="section__head section__head--center">
        <span class="eyebrow">One file, three servers</span>
        <h2>Plex, Jellyfin and Emby from one decode</h2>
        <p class="section__lede">
          Run more than one server and the app reads each file once, then writes what each one expects:
          a BIF for Plex, a BIF next to the video for Emby, trickplay tiles for Jellyfin.
        </p>
      </div>
      <figure class="players">
        <div class="frame">
          <img src="{{ '/images/players-3up.webp' | relative_url }}" width="PLAYERS_WIDTH" height="PLAYERS_HEIGHT" loading="lazy"
               alt="Tears of Steel in the Plex, Jellyfin and Emby web players, each showing a preview thumbnail over the seek bar">
        </div>
        <figcaption>The same film on three test servers, each scrubbed to the same moment. Tears of Steel, (CC) Blender Foundation.</figcaption>
      </figure>
    </div>
  </section>

  <section class="section" id="hdr">
    <div class="wrap reveal">
      <div class="section__head section__head--center">
        <span class="eyebrow">HDR and Dolby Vision</span>
        <h2>Previews in normal colour, not grey or green</h2>
        <p class="section__lede">
          A frame grabbed from HDR video without tone mapping comes out flat and grey, and Dolby Vision can
          come out green. The app tone-maps HDR10, HLG and HDR10+, and uses the HDR10 layer of Dolby Vision
          profiles 7 and 8. Profile 5 needs a GPU with a hardware Vulkan driver.
        </p>
      </div>
      <figure class="pair">
        <div class="frame">
          <img src="{{ '/images/hdr-before-after.webp' | relative_url }}" width="PAIR_WIDTH" height="PAIR_HEIGHT" loading="lazy"
               alt="The same HDR frame twice: grey and flat without tone mapping, natural colour from this app">
        </div>
        <figcaption>HDR_CAPTION</figcaption>
      </figure>
      <p class="center mt-lg">
        <a class="btn btn--ghost" href="{{ '/hdr-dolby-vision-thumbnails/' | relative_url }}">
          HDR and Dolby Vision in detail
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>
        </a>
      </p>
    </div>
  </section>

  <section class="section section--tint" id="features">
    <div class="wrap reveal">
      <div class="section__head section__head--center">
        <span class="eyebrow">Features</span>
        <h2>What you get</h2>
      </div>
      <div class="grid grid--3 grid--trio">
        {%- for f in site.data.features -%}
        <div class="card">
          <div class="card__icon">{{ f.icon }}</div>
          <h3>{{ f.title }}</h3>
          <p>{{ f.body }}</p>
        </div>
        {%- endfor -%}
      </div>
    </div>
  </section>

  <section class="section" id="works-with">
    <div class="wrap reveal">
      <div class="section__head section__head--center">
        <span class="eyebrow">Works with</span>
        <h2>What goes in, and what comes out</h2>
      </div>
      <div class="pipeline">
        <div class="pipeline__stage">
          <h3>Something new arrives</h3>
          <p>Sonarr · Radarr · Tdarr · server webhooks · schedules</p>
        </div>
        <span class="pipeline__arrow" aria-hidden="true">→</span>
        <div class="pipeline__stage pipeline__stage--core">
          <h3>One GPU decode</h3>
          <p>NVIDIA, Intel or AMD, or the CPU</p>
        </div>
        <span class="pipeline__arrow" aria-hidden="true">→</span>
        <div class="pipeline__stage">
          <h3>Each server's own format</h3>
          <p>Plex BIF · Emby BIF · Jellyfin trickplay</p>
        </div>
      </div>
      <p class="center" style="color:var(--muted);font-size:0.92rem;margin:1.75rem 0 0">
        Only a media server and Docker are required. Everything else is used if you already run it.
      </p>
      <div class="grid grid--3 grid--trio">
        {%- for g in site.data.works_with -%}
        <div class="card">
          <h3>{{ g.group }}</h3>
          <ul class="supports">
            {%- for i in g.items -%}
            <li><strong>{{ i.name }}</strong><span>{{ i.note }}</span></li>
            {%- endfor -%}
          </ul>
        </div>
        {%- endfor -%}
      </div>
    </div>
  </section>

  <section class="section section--tint" id="compare">
    <div class="wrap reveal">
      <div class="section__head section__head--center">
        <span class="eyebrow">Compared with the built-ins</span>
        <h2>Keep the built-in if it keeps up</h2>
        <p class="section__lede">
          The generators in Plex, Emby and Jellyfin need no setup and cost nothing extra. This app is for
          when they don't keep up with your library, and it costs some setup: a container, GPU passthrough,
          and write access to where each server keeps its previews.
        </p>
      </div>
      <p class="center">
        <a class="btn btn--ghost" href="{{ '/comparison/' | relative_url }}">
          See the side-by-side comparison
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>
        </a>
      </p>
    </div>
  </section>

  <section class="section" id="install">
    <div class="wrap reveal">
      <div class="section__head section__head--center">
        <span class="eyebrow">Quick start</span>
        <h2>Start the container, open the web UI</h2>
        <p class="section__lede">
          Open <code>http://your-server:8080</code>, paste the token from <code>docker logs</code>, and the
          setup wizard connects your servers.
        </p>
      </div>
      <div class="grid grid--2">
        <div class="codeblock">
          <div class="codeblock__bar">
            <span>docker compose</span>
            <button class="copy" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg><span class="copy__label">Copy</span></button>
          </div>
<pre><code>mkdir media-preview-generator &amp;&amp; cd media-preview-generator
curl -fsSLO https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml   # set your paths in it
docker compose up -d</code></pre>
        </div>
        <div class="codeblock">
          <div class="codeblock__bar">
            <span>docker run</span>
            <button class="copy" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg><span class="copy__label">Copy</span></button>
          </div>
<pre><code>docker run -d --name media-preview-generator \
  --restart unless-stopped -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -e PUID=1000 -e PGID=1000 \
  -v /path/to/media:/media \
  -v /path/to/plex/config:/plex \
  -v /path/to/app/config:/config \
  {{ site.docker_image }}:latest</code></pre>
        </div>
      </div>
      <p class="center mt-lg" style="color:var(--muted);font-size:0.92rem">
        NVIDIA? Use <code>--gpus all -e NVIDIA_DRIVER_CAPABILITIES=all</code> instead of <code>--device /dev/dri</code>.
        Plex needs its data folder read-write; Emby and Jellyfin need the media folder read-write.
        <a href="{{ '/getting-started/' | relative_url }}">Full requirements &rarr;</a>
      </p>
    </div>
  </section>

  <section class="section section--tint" id="faq">
    <div class="wrap">
      <div class="section__head section__head--center">
        <span class="eyebrow">Questions</span>
        <h2>Common questions</h2>
      </div>
      <div class="faq">
        {%- for q in site.data.faq -%}
        <details>
          <summary>{{ q.q }}</summary>
          <div class="faq__body">{{ q.a | markdownify }}</div>
        </details>
        {%- endfor -%}
      </div>
      <p class="center mt-lg">
        <a class="btn btn--ghost" href="{{ '/faq/' | relative_url }}">
          Read the full FAQ
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>
        </a>
      </p>
    </div>
  </section>

  <section class="section">
    <div class="wrap reveal">
      <div class="cta">
        <h2>Give it a try</h2>
        <p>It's free and MIT licensed. If something doesn't work, open an issue and say what you tried.</p>
        <div class="hero__cta">
          <a class="btn btn--primary btn--lg" href="{{ '/getting-started/' | relative_url }}">Install it</a>
          <a class="btn btn--ghost btn--lg" href="{{ site.repo }}">Star it on GitHub</a>
        </div>
      </div>
    </div>
  </section>

</main>

{% include faq-jsonld.html %}
```

`PLAYERS_WIDTH`, `PLAYERS_HEIGHT`, `PAIR_WIDTH`, `PAIR_HEIGHT` and `HDR_CAPTION` are filled in Step 8, when the approved images arrive; until then the three figures show broken images in local builds, which is expected before STOP 3. (Check the compose line's URL once: `curl -s -o /dev/null -w '%{http_code}' https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docker-compose.example.yml` prints `200`.)

Append to `docs/assets/css/main.css`:

```css
/* ------------------------------------------------- landing figures -- */
/* The three-player strip and the HDR pair: a framed picture with a caption under it. */
.players,
.pair {
  margin: 0;
}
.pair {
  max-width: 52rem;
  margin-inline: auto;
}
.players figcaption,
.pair figcaption {
  margin-top: 0.9rem;
  text-align: center;
  color: var(--muted);
  font-size: 0.9rem;
}
.hero__meta a {
  color: inherit;
  text-decoration: underline dotted;
}
```

- [ ] **Step 5: Switch the home page to the landing layout**

`docs/index.md` becomes only:

```markdown
---
layout: home
title: GPU video preview thumbnails for Plex, Emby and Jellyfin
heading: Media Preview Generator
description: Media Preview Generator creates Plex, Emby and Jellyfin video preview thumbnails with GPU-accelerated FFmpeg. Install guide, settings, API and FAQ.
---
```

(The old body's facts now live in the landing sections and data files. `docs/README.md` stays the github.com entry point.)

- [ ] **Step 6: Regenerate llms-full and run the tests**

Run: `python scripts/generate_llms_full.py && PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_site_claims.py tests/test_docs_site.py tests/test_llms_full.py tests/test_docs_links.py -q`
Expected: all pass except `TestLanding::test_landing_pictures_reserve_half_their_pixel_size`, which needs the lab pictures (Step 8). `grep -c "## Common questions" docs/llms-full.txt` prints 2 (the FAQ page and the landing data). Nothing is committed until Step 10: the task's one commit carries the approved pictures.

- [ ] **Step 7: Render the page at desktop and phone widths**

```bash
docker run -d --rm --name mpg-docs-preview -p 127.0.0.1:4000:4000 -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -e BUNDLE_PATH=/bundle -v "$HOME/.cache/mpg-docs-bundle:/bundle" -v "$PWD/docs:/docs" -w /docs ruby:3.4-bookworm \
  sh -c 'bundle install --quiet && bundle exec jekyll serve --host 0.0.0.0 --disable-disk-cache'
sleep 20
/home/data/.venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright
out = "/tmp/claude-1000/-home-data-orca-workspaces-plex-generate-vid-previews-discover/051f57ad-9232-4c9d-9878-c5ef2df9e88f/scratchpad"
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, width in (("desktop", 1440), ("phone", 390)):
        for theme in ("dark", "light"):
            ctx = b.new_context(viewport={"width": width, "height": 900}, device_scale_factor=1)
            ctx.add_init_script(f"localStorage.setItem('mpg-theme', '{theme}')")
            pg = ctx.new_page()
            pg.goto("http://127.0.0.1:4000/")
            pg.wait_for_timeout(1500)
            pg.screenshot(path=f"{out}/landing-{name}-{theme}.png", full_page=True)
            overflow = pg.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
            print(name, theme, "horizontal overflow!" if overflow else "fits")
            ctx.close()
    b.close()
PY
docker stop mpg-docs-preview
```

Expected: four `fits` lines. Read the four PNGs: every section present in order, the tour pinned on desktop and stacked on the phone, no text over images.

- [ ] **Step 8 (after STOP 3 approval): the approved pictures, and the benchmark stat**

```bash
cp /home/data/mlab-openfilms/captures/{player-plex,players-3up,hdr-before-after}.webp docs/images/
/home/data/.venv/bin/python -c "from PIL import Image; [print(n, Image.open(f'docs/images/{n}').size) for n in ('player-plex.webp','players-3up.webp','hdr-before-after.webp')]"
jq -r '.[] | select(.hdr) | "\(.title): \(.attribution), \(.licence)"' docs/design/site-redesign-lab/films.json
```

(If the owner picked a different hero at STOP 3, copy that file instead and change the hero `src` and alt text.) In `home.html` replace `PLAYERS_WIDTH`/`PLAYERS_HEIGHT` and `PAIR_WIDTH`/`PAIR_HEIGHT` with half the printed pixel sizes, check the hero's `width="1280" height="720"` is half of `player-plex.webp`'s size, and replace `HDR_CAPTION` with: `The same moment of <title>: left, a plain FFmpeg frame grab; right, the frame from the preview file this app made. <title>, <attribution>, <licence>.` using the `jq` line's values. If the HDR pair was dropped at STOP 3, delete the `<figure class="pair">` block.

If STOP 3 approved a benchmark number, append to `docs/_data/stats.yml`:

```yaml
- value: "6.4"  # must equal ratio_display in docs/benchmark/summary.json (tested)
  unit: "×"
  label: faster than Plex's built-in, on our test films
  note: Median of three runs each on the same films and machine.
  source: benchmark
```

with `value` set to `summary.json`'s `ratio_display` (the `6.4` shown is an example, the test fails on any other value).

Run: `python scripts/generate_llms_full.py`, then dispatch `verifier` for the full suite (`PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest`). Expected: all pass, `test_landing_pictures_reserve_half_their_pixel_size` included. Re-run Step 7's render.

- [ ] **Step 9: STOP — owner checkpoint 2 (landing page with real copy and the captures)**

Send the owner the four landing screenshots from Step 7 (re-rendered after Step 8), and list for them: the hero headline and sub, the stats, the eight FAQ questions, and anything left out on purpose (for example Emby if it wasn't captured). Wait for an explicit yes; apply their edits in `home.html`/`_data/*.yml`, re-run Step 6's tests, and re-render. Task 10 (docs-page content) does not start before the yes.

- [ ] **Step 10: Architecture review and commit**

```bash
git add docs/_layouts/home.html docs/_data docs/index.md docs/faq.md docs/assets/css/main.css docs/llms-full.txt \
  docs/images/player-plex.webp docs/images/players-3up.webp docs/images/hdr-before-after.webp \
  tests/test_site_claims.py tests/test_docs_site.py
```

Dispatch `Architecture Review`; fix HIGH; then:

```bash
git commit -F - <<'EOF'
feat(docs): landing page in the Shortlist layout, with real players from the lab

Hero, stats, problem, five-step tour, three servers, HDR pair, features, works-with, comparison
teaser, quick start, FAQ and closing call. FAQ questions are worded as searched and shared with /faq/
and the FAQPage data; a test keeps speed numbers to the benchmark. Approved at checkpoint 2.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---
## Task 10: Docs pages: comparison, FAQ wording, credits, per-server pictures

Lane A, after STOP 2 (and with lane B merged, for `docs/benchmark.md` if it exists). Suggested model: opus.

**Files:**
- Create: `docs/credits.md`
- Modify: `docs/comparison.md`, `docs/faq.md`, `docs/_config.yml` (nav), `docs/_includes/footer.html` (film credits line), `docs/plex-preview-thumbnails-gpu.md`, `docs/jellyfin-trickplay-gpu.md`, `docs/emby-bif-thumbnails-gpu.md`, `docs/README.md` (hub rows), `docs/benchmark.md` (credits link), `tests/test_docs_site.py`, `docs/llms-full.txt`
- Copy in: `docs/images/player-jellyfin.webp`, `docs/images/player-emby.webp` (if Emby was captured)

**Interfaces:**
- Consumes: `docs/design/site-redesign-lab/films.json` (Task 4), approved captures (Task 5), `docs/benchmark.md` (Task 8, optional).
- Produces: `/credits/` page and nav entry `{title: Credits, url: /credits/}`; footer credits line.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_docs_site.py`)

```python
FILMS = json.loads((DOCS_DIR / "design" / "site-redesign-lab" / "films.json").read_text(encoding="utf-8"))


class TestDocsContent:
    def test_comparison_date_comes_from_front_matter(self, site: Path) -> None:
        source = DOCS_DIR / "comparison.md"
        checked = front_matter(source)["facts_checked"]
        assert "Facts checked" not in strip_front_matter(source.read_text(encoding="utf-8"))
        expected = f"Facts checked {checked.day} {checked:%B %Y}."
        assert expected in (site / "comparison" / "index.html").read_text(encoding="utf-8")

    def test_comparison_discloses_authorship_and_invites_corrections(self, site: Path) -> None:
        page = (site / "comparison" / "index.html").read_text(encoding="utf-8")
        assert "This is my project" in page
        assert "https://github.com/stevezau/media_preview_generator/issues/new" in page
        assert 'id="first-decide-which-problem-you-have"' in page

    def test_credits_name_every_film_with_its_licence(self) -> None:
        credits = (DOCS_DIR / "credits.md").read_text(encoding="utf-8")
        for film in (f for f in FILMS if f["video"]):
            assert film["title"] in credits, film["title"]
            assert film["attribution"] in credits, film["title"]
            assert film["licence_url"] in credits, film["title"]

    def test_every_page_footer_credits_the_films(self, site: Path) -> None:
        footer = (site / "index.html").read_text(encoding="utf-8").split('<footer class="footer">', 1)[1]
        assert "Blender Foundation" in footer
        assert "/credits/" in footer
        if any(film.get("hdr") for film in FILMS if film["video"]):
            assert "Netflix" in footer
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_site.py -k TestDocsContent -q`
Expected: 4 FAIL.

- [ ] **Step 2: The comparison page**

In `docs/comparison.md`:
1. Directly under the front matter, before the first paragraph, add:
   ```markdown
   This is my project, so weigh this page accordingly. I've tried to say plainly where the built-in generators are the better choice, and every fact about them links to its source. Spot something wrong or out of date? [Open an issue](https://github.com/stevezau/media_preview_generator/issues/new?labels=docs&title=Comparison%20correction) and I'll fix it.
   ```
2. Delete the words `**Facts checked 23 September 2026.**` (the layout prints the date from `facts_checked`); keep the rest of that paragraph.
3. Before `## Side by side`, add:
   ```markdown
   ## First, decide which problem you have

   - **Previews take days to appear across a big library.** The built-in job runs on the CPU, mostly on a schedule. Tune it first (see the rows below); if it still can't keep up, move the work to a GPU.
   - **A new episode has no previews for hours.** You want previews per file, when Sonarr or Radarr imports it. The built-ins work on a schedule or a library scan.
   - **HDR previews look grey, or Dolby Vision looks green.** That's tone mapping, and the built-ins handle it differently (see the HDR row).
   - **You run more than one of Plex, Emby and Jellyfin.** Each built-in decodes the file again for its own server.
   - **None of these.** Keep the built-in. It needs no setup.
   ```
4. If `docs/benchmark.md` exists, add after the table: `Measured times for Plex's built-in generator and this app on one machine are on the [benchmark page](benchmark.md), with the method and raw data.` (No number here: numbers live on the benchmark page and the stats band only.)

- [ ] **Step 3: FAQ questions worded as searched**

Rename these `###` headings in `docs/faq.md` (answers unchanged unless noted); the Common questions section and the two headings renamed in Task 9 stay as they are:

- `What does this tool do?` -> `What does Media Preview Generator do?`
- `What Plex/Emby/Jellyfin settings should I use?` -> `Which Plex, Emby and Jellyfin settings should I change?`
- `Does this generate chapter thumbnails?` -> `Does it make chapter thumbnails too?`
- `Is Docker required? Is there a standalone .exe?` -> `Is there a Windows .exe, or do I need Docker?`
- `Can I run this on a different machine than my media server(s)?` -> `Can it run on a different machine from Plex, Emby or Jellyfin?`
- `Does this work with Jellyfin or Emby?` -> `Does it work with Jellyfin and Emby, not just Plex?`
- `How do I know which GPUs are detected?` -> `How do I check which GPUs it found?`
- `Can I use multiple GPUs?` -> `Can it use more than one GPU?`
- `Which GPU should I use?` -> `Which GPU is best for preview thumbnails?`; replace its table's "Best For" cells, which make unmeasured speed claims, with: NVIDIA `CUDA decoding; also works on Windows through WSL2`, Intel iGPU `Low power; common on Unraid`, AMD `VAAPI decoding on Linux`, CPU-only `Works everywhere; the CPU does all the decoding`.
- `HDR / Dolby Vision support?` -> `Does it handle HDR and Dolby Vision?`
- `How many threads should I use?` -> `How many workers and threads should I set?`
- `What's thumbnail quality 1-10?` -> `What does thumbnail quality 1-10 change?`
- `Generation feels disk-bound on my multi-disk setup (unraid/mergerfs/JBOD) — how do I speed it up?` -> `Why is generation slow on my Unraid or mergerfs array?`
- `How do I get the authentication token?` -> `Where do I find the login token?`
- `Can I process specific libraries only?` -> `Can I make previews for some libraries only?`
- `How do I regenerate existing thumbnails?` -> `How do I redo previews that already exist?`
- `Why is it "skipping" some files?` -> `Why does it skip some files?`

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_docs_links.py -q`
Expected: failures for inbound links to the old anchors (from other pages and `docs/README.md`); point each at the new kramdown id and re-run until green.

- [ ] **Step 4: Credits page, nav, footer**

Print the film lines from the verified list:

```bash
jq -r '.[] | select(.video != null) | "- **\(.title)** (\(.year)). \(.attribution). Licence: [\(.licence)](\(.licence_url)). [Source](\(.source_page))." + (if .poster then " Poster from the same source licence (see [its page](\(.poster)))." else "" end)' docs/design/site-redesign-lab/films.json
```

`docs/credits.md` (paste the printed lines under "Films"):

```markdown
---
title: Credits for the films and images on this site
heading: Credits
description: The open films in this site's screenshots and preview thumbnails, their licences and authors, and the work this site builds on.
---

The player screenshots on this site were taken on test servers holding films released under Creative Commons licences. Thank you to the people who made them.

## Films

<the jq output, one line per film>

Changes made: frames were extracted and scaled down to make preview thumbnails, and the players were photographed showing those thumbnails. The HDR comparison places a plain frame grab next to a tone-mapped one.

## Everything else

- Site design adapted from [Shortlist](https://shortlistapp.dev)'s docs theme (MIT, same author).
- Plex, Emby and Jellyfin are trademarks of their owners. Their web players appear in screenshots to show what this app's output looks like; this project isn't affiliated with any of them.
```

`docs/_config.yml` nav, last entry:

```yaml
  - title: Credits
    url: /credits/
    blurb: The open films in the screenshots, and their licences
```

`docs/_includes/footer.html`, in `.footer__base`, add a third span (drop the Netflix part if no HDR film is on the site):

```html
      <span>Films in the screenshots: Blender Foundation (CC BY) and Netflix Open Content (CC BY 4.0). <a href="{{ '/credits/' | relative_url }}">Credits</a></span>
```

In `docs/benchmark.md` (if it exists) point the films line at `credits.md`.

- [ ] **Step 5: A real player picture on each server's how-to page**

```bash
cp /home/data/mlab-openfilms/captures/player-jellyfin.webp docs/images/
cp /home/data/mlab-openfilms/captures/player-emby.webp docs/images/ 2>/dev/null || echo "no Emby capture (approved at STOP 3)"
```

After each page's first paragraph add the picture and a one-line caption:
- `docs/plex-preview-thumbnails-gpu.md`: `![Plex's web player mid-scrub, showing a preview thumbnail this app made](images/player-plex.webp)` then `*Plex's web player on a test server. Tears of Steel, (CC) Blender Foundation.*`
- `docs/jellyfin-trickplay-gpu.md`: the same with `player-jellyfin.webp` and "Jellyfin's web player".
- `docs/emby-bif-thumbnails-gpu.md`: the same with `player-emby.webp` and "Emby's web player" (skip if there's no Emby capture).

`docs/README.md` (the GitHub hub), add rows to "Choose Your Path": `**Fix a specific problem**: slow Plex previews, GPUs, Jellyfin trickplay, Emby, Sonarr/Radarr, HDR` -> `[How-to](how-to.md)`; `**See measured speed**` -> `[Benchmark](benchmark.md)` (only if it exists); `**Film and image credits**` -> `[Credits](credits.md)`.

- [ ] **Step 6: Regenerate, test, review, commit**

Run: `python scripts/generate_llms_full.py`, then dispatch `verifier` for `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest`. Expected: all pass.

```bash
git add docs/comparison.md docs/faq.md docs/credits.md docs/_config.yml docs/_includes/footer.html \
  docs/plex-preview-thumbnails-gpu.md docs/jellyfin-trickplay-gpu.md docs/emby-bif-thumbnails-gpu.md docs/README.md \
  docs/images/player-jellyfin.webp docs/llms-full.txt tests/test_docs_site.py
git add docs/images/player-emby.webp docs/benchmark.md 2>/dev/null || true
```

Dispatch `Architecture Review`; fix HIGH; then:

```bash
git commit -F - <<'EOF'
docs: comparison sorting and disclosure, FAQ worded as searched, film credits

The comparison page opens with who wrote it and how to correct it, then helps a reader pick which
problem they have; its date now comes from front matter. FAQ headings match what people search.
A credits page and footer line name every film in the screenshots and its licence.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 11: README, Docker Hub README and llms.txt

Lane A, after Task 10, with lanes B and C merged. Suggested model: opus (copy).

**Files:**
- Rewrite: `README.md`, `DOCKERHUB_README.md`, `docs/llms.txt`
- Create: `tests/test_readmes.py`
- Regenerate: `docs/llms-full.txt`

**Interfaces:**
- Consumes: `docs/images/{icon.svg,players-3up.webp,tour-*.webp}`, `_config.yml` `nav`, `docs/benchmark/summary.json` (for whether a benchmark exists).
- Produces: the files above. `docs/llms.txt` keeps a `## Docs` heading (Task 2's generator takes everything above it as the llms-full header).

- [ ] **Step 1: Write the failing tests `tests/test_readmes.py`**

```python
"""README.md, DOCKERHUB_README.md and docs/llms.txt: the shape spec §6-§7 asks for.

Each fails on a regression someone would actually ship: a relative link on Docker Hub (which has no
repo to resolve it against), a README section that drifted back, a badge in the wrong colour, or an
llms.txt that stopped listing a docs page.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
DOCKERHUB = (REPO_ROOT / "DOCKERHUB_README.md").read_text(encoding="utf-8")
LLMS = (REPO_ROOT / "docs" / "llms.txt").read_text(encoding="utf-8")
CONFIG = yaml.safe_load((REPO_ROOT / "docs" / "_config.yml").read_text(encoding="utf-8"))
ROOT = f"{CONFIG['url']}{CONFIG.get('baseurl') or ''}/"
LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)|(?:src|href)=\"([^\"]+)\"")
README_SECTIONS = ["What it does", "What it looks like", "Features", "Where it fits", "Quick start",
                   "Documentation", "Support the project", "Get help", "License"]  # fmt: skip


def _targets(text: str) -> list[str]:
    return [a or b for a, b in LINK_TARGET.findall(text)]


class TestReadme:
    def test_sections_in_order(self) -> None:
        headings = re.findall(r"^## (.+)$", README, re.MULTILINE)
        assert headings == README_SECTIONS

    def test_badges_are_amber_and_trimmed(self) -> None:
        shields = re.findall(r"^\[[\w-]+-shield\]: (\S+)$", README, re.MULTILINE)
        assert 8 <= len(shields) <= 10
        coloured = [url for url in shields if "logo=kofi" not in url and "AI--Assisted" not in url]
        assert all("a06a00" in url for url in coloured), coloured
        assert "branch=main" in next(url for url in shields if "actions/workflow/status" in url)
        assert not re.search(r"contributors-shield|forks-shield|sponsor-shield", README)
        assert "Built With" not in README

    def test_hero_is_the_players_image_with_a_disclosing_caption(self) -> None:
        assert "docs/images/players-3up.webp" in README
        assert re.search(r"<sub>[^<]*test servers[^<]*</sub>", README)

    def test_no_horizontal_rules_between_sections(self) -> None:
        assert "\n---\n" not in README

    def test_links_to_the_docs_site(self) -> None:
        assert f'href="{ROOT}"' in README


class TestDockerHubReadme:
    def test_every_link_and_image_is_absolute(self) -> None:
        relative = [t for t in _targets(DOCKERHUB) if not t.startswith(("http://", "https://", "#", "mailto:"))]
        assert relative == []

    def test_fits_docker_hubs_limit_and_skips_github_only_syntax(self) -> None:
        assert len(DOCKERHUB) <= 25_000
        assert not re.search(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", DOCKERHUB)

    def test_carries_the_readme_story(self) -> None:
        for section in ("What it does", "Features", "Quick start"):
            assert f"## {section}" in DOCKERHUB


class TestLlmsTxt:
    def test_opens_with_the_question_it_answers(self) -> None:
        body = LLMS.split("---\n", 2)[2]
        assert body.startswith("# Media Preview Generator\n\n> ")
        assert "?" in body.split("\n\n", 2)[1]

    def test_full_file_is_the_first_link(self) -> None:
        body = LLMS.split("---\n", 2)[2]
        first = re.search(r"https?://\S+", body).group(0)
        assert first.rstrip(".,)") == f"{ROOT}llms-full.txt"

    def test_every_docs_page_is_linked_with_a_description(self) -> None:
        docs = LLMS.split("\n## Docs", 1)[1]
        urls = [ROOT] + [ROOT + url.strip("/") + "/" for url in _nav_urls()]
        for url in urls:
            assert re.search(rf"^- \[[^\]]+\]\({re.escape(url)}\): \S", docs, re.MULTILINE), url

    def test_has_a_status_line_and_a_bold_distinction(self) -> None:
        assert re.search(r"^Status: ", LLMS, re.MULTILINE)
        how = LLMS.split("## How it differs", 1)[1].split("\n## ", 1)[0]
        assert re.search(r"^\*\*[^*]+\*\*", how, re.MULTILINE), "no bold distinction paragraph"  # bullets start with "- "


def _nav_urls() -> list[str]:
    urls = []
    for entry in CONFIG["nav"]:
        urls.append(entry["url"])
        urls.extend(child["url"] for child in entry.get("children", []))
    return urls
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_readmes.py -q`
Expected: most FAIL against the current files.

- [ ] **Step 2: Rewrite `README.md`**

````markdown
<!-- PROJECT SHIELDS: reference-style (defined at the bottom), amber like the docs site. -->
<div align="center">

[![Build][build-shield]][build-url]
[![Release][release-shield]][release-url]
[![Coverage][codecov-shield]][codecov-url]
[![Docker Pulls][docker-shield]][docker-url]
[![Image Size][size-shield]][docker-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![AI-Assisted][ai-shield]][ai-url]
[![Buy me a coffee][kofi-shield]][kofi-url]

</div>

<div align="center">
  <img src="docs/images/icon.svg" alt="" width="110" height="110">

  <h1 align="center">Media Preview Generator</h1>

  <p align="center">
    The preview thumbnails <strong>Plex, Emby and Jellyfin</strong> show while you scrub, made on your
    GPU as soon as Sonarr or Radarr imports a file.
    <br />
    Self-hosted, one Docker container.
    <br />
    <br />
    <a href="https://mediapreviewgenerator.dev/"><strong>Explore the docs »</strong></a>
    <br />
    <br />
    <a href="#quick-start">Quick start</a>
    &middot;
    <a href="https://mediapreviewgenerator.dev/comparison/">How it compares</a>
    &middot;
    <a href="https://github.com/stevezau/media_preview_generator/discussions">Ask a question</a>
    &middot;
    <a href="https://github.com/stevezau/media_preview_generator/issues/new?labels=bug">Report a bug</a>
  </p>
</div>

![Tears of Steel in the Plex, Jellyfin and Emby web players, each showing a preview thumbnail over the seek bar](docs/images/players-3up.webp)

<sub>Plex, Jellyfin and Emby web players on test servers, each paused mid-scrub on the same film. The
thumbnails were made by this app. Tears of Steel, (CC) Blender Foundation.</sub>

## What it does

Plex, Emby and Jellyfin can make preview thumbnails themselves, but they do it inside the server, mostly
on the CPU, and mostly on a schedule. On a big library that takes days, and the episode that arrived
tonight can sit with a blank timeline until the next maintenance window.

**Media Preview Generator makes them on your GPU, one file at a time, as each file arrives.** When more
than one server holds the same file, it decodes it once and writes each server's own format.

## What it looks like

| A new file sets it off | It finds every server with that file |
| --- | --- |
| ![Sonarr and Radarr webhook setup on the Automation page](docs/images/tour-trigger.webp) | ![Servers page with one card each for Plex, Jellyfin and Emby](docs/images/tour-resolve.webp) |

| One GPU pass per file | Each server gets its own format |
| --- | --- |
| ![Dashboard with GPU workers making previews for three films](docs/images/tour-extract.webp) | ![One film's previews published to Plex, Jellyfin and Emby](docs/images/tour-publish.webp) |

<sub>App screenshots come from a test setup with made-up servers; the job titles are open films.</sub>

## Features

**GPU first, CPU when needed**
- NVIDIA, Intel and AMD GPUs on Linux; NVIDIA on Windows through WSL2. Every GPU passed to the container gets workers.
- A file the GPU can't decode is retried on the CPU by the same worker.
- Jumps between key frames when a file's key-frame spacing allows it.

**Starts on its own**
- One webhook URL for Sonarr, Radarr, Sportarr, Tdarr, FileFlows, Plex, Emby and Jellyfin, or any JSON with a path.
- Recently Added polling, cron and interval schedules, or a hand-picked list.
- Retries after 1, 2 and 5 minutes while a server indexes a new file, and skips files whose previews are current.

**Every server from one decode**
- A BIF in Plex's data folder, a BIF next to the video for Emby, and Jellyfin trickplay tiles (next to the video, or in Jellyfin's data folder with the companion plugin).
- Previews Readiness checks each server's settings and offers fixes one library at a time.

**Right colours**
- Tone-maps HDR10, HLG and HDR10+, and uses the HDR10 layer of Dolby Vision profiles 7 and 8. Profile 5 needs a GPU with a hardware Vulkan driver.

## Where it fits

It sits next to Sonarr, Radarr and Tdarr and takes over the media server's own preview job. Once it
runs, turn the built-in generation off so the server doesn't do the same work twice: the app's
[Previews Readiness](docs/guides/previews-readiness.md) panel says which setting that is on each server.
If your server's own generator keeps up, you don't need this: see
[how it compares](https://mediapreviewgenerator.dev/comparison/).

## Quick start

```bash
docker run -d \
  --name media-preview-generator \
  --restart unless-stopped \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -e PUID=1000 -e PGID=1000 \
  -v /path/to/media:/media \
  -v /path/to/plex/config:/plex \
  -v /path/to/app/config:/config \
  stevezzau/media_preview_generator:latest
```

- Intel or AMD: `--device /dev/dri` as above. NVIDIA: `--gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all` instead.
- Plex writes into `/plex`, so the media can be `:ro`. Emby and Jellyfin write next to each video, so their media mount must be read-write.
- Open `http://YOUR_IP:8080`, get the token with `docker logs media-preview-generator | grep "Token:"`, and follow the setup wizard.

Docker Compose, Unraid and GPU details: [Getting started](docs/getting-started.md).

## Documentation

| Page | What's in it |
| --- | --- |
| [Getting started](docs/getting-started.md) | Docker, GPUs, mounts, Unraid, networking |
| [How it compares](docs/comparison.md) | Plex, Jellyfin and Emby built-in generation, side by side |
| [Guides](docs/guides.md) | The web UI, webhooks, schedules, troubleshooting |
| [Multi-server](docs/multi-server.md) | Plex, Emby and Jellyfin from one instance |
| [Reference](docs/reference.md) | Every setting, environment variable and API endpoint |
| [FAQ](docs/faq.md) | Windows, GPUs, RAM, skipped files |
| [Why Plex previews are slow](docs/plex-preview-thumbnails-slow.md) | What Plex does, and what to change |
| [Plex previews with a GPU](docs/plex-preview-thumbnails-gpu.md) | Moving Plex's preview job to a GPU |
| [Faster Jellyfin trickplay](docs/jellyfin-trickplay-gpu.md) | Jellyfin's own settings first, then a GPU |
| [Emby BIF previews with a GPU](docs/emby-bif-thumbnails-gpu.md) | Emby BIFs made on a GPU, next to each video |
| [Previews on Sonarr or Radarr import](docs/sonarr-radarr-preview-thumbnails.md) | Webhooks, quiet period, retries |
| [HDR and Dolby Vision previews](docs/hdr-dolby-vision-thumbnails.md) | Tone mapping, and the Dolby Vision 5 caveat |

## Support the project

It's free and MIT licensed. Helping is optional.

- **[Star it on GitHub](https://github.com/stevezau/media_preview_generator)**: free, and it's how other server owners find it.
- **[Buy me a coffee on Ko-fi](https://ko-fi.com/stevezau)**: no account needed.

## Get help

- **Previews don't show up?** Open the server's Previews Readiness panel in the app. It names the setting that's in the way.
- **Not sure it's a bug?** Ask in [Discussions](https://github.com/stevezau/media_preview_generator/discussions).
- **Found a bug?** [Open an issue](https://github.com/stevezau/media_preview_generator/issues/new?labels=bug) with the app version and what you tried.

## License

MIT, see [LICENSE](LICENSE). Recent development is AI-assisted (Claude); every change is reviewed and tested.

[build-shield]: https://img.shields.io/github/actions/workflow/status/stevezau/media_preview_generator/ci.yml?branch=main&style=for-the-badge&label=build&labelColor=a06a00
[build-url]: https://github.com/stevezau/media_preview_generator/actions/workflows/ci.yml
[release-shield]: https://img.shields.io/github/v/release/stevezau/media_preview_generator?filter=!plugin-v*&style=for-the-badge&label=release&color=a06a00
[release-url]: https://github.com/stevezau/media_preview_generator/releases
[codecov-shield]: https://img.shields.io/codecov/c/github/stevezau/media_preview_generator?style=for-the-badge&labelColor=a06a00
[codecov-url]: https://codecov.io/gh/stevezau/media_preview_generator
[docker-shield]: https://img.shields.io/docker/pulls/stevezzau/media_preview_generator?style=for-the-badge&color=a06a00
[docker-url]: https://hub.docker.com/r/stevezzau/media_preview_generator
[size-shield]: https://img.shields.io/docker/image-size/stevezzau/media_preview_generator/latest?style=for-the-badge&label=image&color=a06a00
[stars-shield]: https://img.shields.io/github/stars/stevezau/media_preview_generator.svg?style=for-the-badge&color=a06a00
[stars-url]: https://github.com/stevezau/media_preview_generator/stargazers
[issues-shield]: https://img.shields.io/github/issues/stevezau/media_preview_generator.svg?style=for-the-badge&labelColor=a06a00
[issues-url]: https://github.com/stevezau/media_preview_generator/issues
[license-shield]: https://img.shields.io/github/license/stevezau/media_preview_generator.svg?style=for-the-badge&color=a06a00
[license-url]: https://github.com/stevezau/media_preview_generator/blob/main/LICENSE
[ai-shield]: https://img.shields.io/badge/AI--Assisted-Claude-8A2BE2?style=for-the-badge&logo=anthropic&logoColor=white
[ai-url]: #license
[kofi-shield]: https://img.shields.io/badge/Buy%20me%20a%20coffee-FF5E5B?style=for-the-badge&logo=kofi&logoColor=white
[kofi-url]: https://ko-fi.com/stevezau
````

If Emby wasn't captured (STOP 3), change the hero alt text and `<sub>` to name only Plex and Jellyfin and add "Emby isn't pictured: its web player didn't show thumbnails in headless capture."

- [ ] **Step 3: Rewrite `DOCKERHUB_README.md`**

The same text as `README.md` from `# Media Preview Generator` (as a Markdown `#` heading, no `<div>`/`<p align>` HTML) through `## License`, with these changes, because Docker Hub has no repository to resolve anything against:
- No badge block. The logo as `![](https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docs/images/icon.png)` (PNG: Docker Hub's renderer is unreliable with SVG).
- Every `docs/images/…` becomes `https://raw.githubusercontent.com/stevezau/media_preview_generator/main/docs/images/…`.
- Every `docs/<page>.md` link becomes the site URL (`https://mediapreviewgenerator.dev/<page>/`), `#quick-start` stays, `LICENSE` becomes `https://github.com/stevezau/media_preview_generator/blob/main/LICENSE`.
- After "Quick start", keep the current file's `## Image Tags` table verbatim (Docker Hub readers need it; it's the one Docker-only addition).

(The raw URLs resolve once the images are on `main`, which is when Docker Hub syncs this file: ci.yml `update-dockerhub-description` runs on release tags.)

- [ ] **Step 4: Rewrite `docs/llms.txt`**

Keep the front matter block from Task 1. Body (fill the two conditional lines from the actual state):

```markdown
# Media Preview Generator

> How do I make the video preview thumbnails for Plex, Emby or Jellyfin faster, or on a GPU? Media Preview Generator is a free, MIT-licensed, self-hosted Docker app that makes them (the small images shown while you drag a video's timeline) with GPU-accelerated FFmpeg, per file, as soon as Sonarr, Radarr or the media server reports a new file.

Every docs page in one fetch: https://mediapreviewgenerator.dev/llms-full.txt

## How it differs from the tools usually suggested first

- **Plex's built-in "Generate video preview thumbnails"**: a Library setting, no extra software, described by Plex as CPU-intensive, with no GPU option.
- **Jellyfin's built-in trickplay (10.9+)**: built in; optional hardware decoding and thread settings, off and 1 thread by default.
- **Emby's built-in thumbnail (BIF) extraction**: built in, no GPU option.
- **Emby community BIF tools** ("MediaInfo for Emby", "Bif Generator"): Emby-only add-ons.
- **Small BIF command-line tools** (`entrez/bifgen`, `amankumarsingh77/bif-generator`, scripts around Roku's `biftool`): one video to one BIF file, no GPU path, no media-server integration.

**What sets it apart: each new file is decoded once, on a GPU, and every configured server gets its own native output in the place it reads from (a Plex BIF in Plex's data folder, an Emby BIF next to the video, Jellyfin trickplay tiles).** When the built-ins are the better choice is on the comparison page.

Status: maintained, released as Docker images (linux/amd64, linux/arm64); releases at https://github.com/stevezau/media_preview_generator/releases. It does not detect intros or credits, make chapter images, or transcode for playback.

## How it works

1. **Trigger.** Webhooks from Sonarr, Radarr, Sportarr, Plex, Emby, Jellyfin, Tdarr, FileFlows or any JSON `{"path": ...}` arrive at one URL that detects the sender. Also: Recently Added polling, cron or interval schedules, or a manual pick.
2. **Resolve.** Each path is mapped to what the container sees, then every configured server whose enabled libraries hold the file is found.
3. **Extract.** One FFmpeg run per file on a GPU worker: hardware decode, key-frame-only decoding when the file allows it, GPU scaling, HDR-to-SDR tone mapping. If the GPU can't decode the file, the same worker retries on the CPU.
4. **Publish.** Plex: `index-sd.bif` in Plex's data folder. Emby: `<video>-<width>-<interval>.bif` next to the video. Jellyfin 10.10+: trickplay tiles next to the video, or in Jellyfin's data folder with the companion plugin. Each server is then asked to pick them up.
5. **Retry and skip.** A server that hasn't indexed a new file yet is retried after 1, 2 and 5 minutes. A small companion file records each source's size and date, so later triggers skip current previews and redo replaced files.

## Key facts

- Runs as a Docker container, including an Unraid Community Applications template. Web UI plus a token-authenticated REST API; no CLI.
- Needs network access to each server's API, and write access where previews go: Plex's data folder for Plex; the media folder for Emby and Jellyfin's default layout. Jellyfin must be 10.10 or newer.
- GPUs: NVIDIA (CUDA), Intel and AMD (VAAPI) on Linux; only NVIDIA on Windows (WSL2); none on macOS. Without a GPU it runs on CPU workers.
- HDR10, HLG and HDR10+ are tone-mapped; Dolby Vision 7 and 8 use their HDR10 layer; Dolby Vision 5 needs a hardware Vulkan driver in the container.
- Frame interval 1-60 s (default 10; Plex's own default is 2). Thumbnail quality 1-10 (default 4).
- Speed: measured on one machine against Plex's built-in generator at https://mediapreviewgenerator.dev/benchmark/ (method and raw timings there).

## Docs

- [llms-full.txt](https://mediapreviewgenerator.dev/llms-full.txt): every docs page below, in one file
- [Home](https://mediapreviewgenerator.dev/): what it does, how it works, quick start and common questions
- [Getting started](https://mediapreviewgenerator.dev/getting-started/): Docker, GPU setup, volume mounts, Unraid, networking
- [How it compares](https://mediapreviewgenerator.dev/comparison/): Plex, Jellyfin and Emby built-in generation side by side, dated and sourced
- [How-to](https://mediapreviewgenerator.dev/how-to/): the six problem-first guides below
- [Why Plex preview thumbnails take so long](https://mediapreviewgenerator.dev/plex-preview-thumbnails-slow/): Plex's CPU job, its settings, and moving it to a GPU
- [Plex preview thumbnails with a GPU](https://mediapreviewgenerator.dev/plex-preview-thumbnails-gpu/): NVIDIA, Intel or AMD in Docker
- [Faster Jellyfin trickplay](https://mediapreviewgenerator.dev/jellyfin-trickplay-gpu/): Jellyfin's own settings first, then a GPU container
- [Emby BIF previews with a GPU](https://mediapreviewgenerator.dev/emby-bif-thumbnails-gpu/): BIF files made on a GPU next to each video
- [Previews on Sonarr or Radarr import](https://mediapreviewgenerator.dev/sonarr-radarr-preview-thumbnails/): webhooks, the 60 s quiet period, retries
- [HDR and Dolby Vision previews](https://mediapreviewgenerator.dev/hdr-dolby-vision-thumbnails/): tone mapping and the Profile 5 caveat
- [Guides and troubleshooting](https://mediapreviewgenerator.dev/guides/): web UI, webhooks, schedules, HDR, CPU fallback
- [Previews Readiness](https://mediapreviewgenerator.dev/guides/previews-readiness/): every server setting that decides whether previews show up
- [Multi-server](https://mediapreviewgenerator.dev/multi-server/): output formats, webhook routing, retries, the Jellyfin plugin
- [Reference](https://mediapreviewgenerator.dev/reference/): settings, environment variables, path mappings, REST API
- [FAQ](https://mediapreviewgenerator.dev/faq/): Windows, GPUs, RAM, skipped files
- [Benchmark](https://mediapreviewgenerator.dev/benchmark/): Plex's built-in generation against this app, method and raw data
- [Credits](https://mediapreviewgenerator.dev/credits/): the open films in the screenshots and their licences

## Source

- GitHub: https://github.com/stevezau/media_preview_generator
- Docker image: https://hub.docker.com/r/stevezzau/media_preview_generator (the Docker Hub account is `stevezzau`, with a double z)
- Licence (MIT): https://github.com/stevezau/media_preview_generator/blob/main/LICENSE
```

If Task 8 produced no benchmark page, delete the "Speed" key fact's link sentence and write `- Speed: there is no published benchmark against the built-in generators.`, and drop the Benchmark line under Docs. Keep the Docs list in the same order as `nav`.

- [ ] **Step 5: Tests, llms-full, review, commit**

Run: `python scripts/generate_llms_full.py && PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_readmes.py tests/test_llms_full.py tests/test_docs_images.py tests/test_site_claims.py -q`
Expected: all pass. Then dispatch `verifier` for the full suite.

```bash
git add README.md DOCKERHUB_README.md docs/llms.txt docs/llms-full.txt tests/test_readmes.py
```

Dispatch `Architecture Review`; fix HIGH; then:

```bash
git commit -F - <<'EOF'
docs: README, Docker Hub README and llms.txt rewritten around the product's result

The README opens with the players mid-scrub and follows Shortlist's structure with amber badges.
Docker Hub gets the same story with absolute links. llms.txt opens with the question it answers and
lists every docs page with a description.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

- [ ] **Step 6: STOP — owner checkpoint 4 (full site and README, before merge and the URL switch)**

Push the branch so the owner can read the README as GitHub renders it: `git push -u origin stevezau/site-redesign` (a feature-branch push, no PR yet; say so when asking). Render every page (home plus each nav page) at 1440 px dark and 390 px light with the Step 7 script from Task 9 (loop over `nav` URLs) into the scratchpad. Send the owner:
- the README link on the branch, the screenshots, and the Docker Hub README text;
- the proposed GitHub description (spec §7) and topics: add `selfhosted`, `radarr`, `hdr`, `dolby-vision`, `preview-thumbnails`; drop `thumbnails-preview`, `media-server-automation`, and, to stay within GitHub's 20-topic limit, three more: proposed `self-hosted` (duplicate of `selfhosted`), `thumbnails`, `media-server`;
- what Task 12 will change (every absolute URL to mediapreviewgenerator.dev; the app's plugin repository URL, which reaches users in the next app release) and that the merge will deploy the new site.

Wait for an explicit yes to merge and switch. Edits loop back to Tasks 9-11.

---

## Task 12: Switch every URL to mediapreviewgenerator.dev

Lane A, after STOP 4. Deep review. Suggested model: opus.

**Files:**
- Modify: `.github/workflows/docs.yml` (`LIVE_MANIFEST_URL`, header comment), `.github/workflows/jellyfin-plugin.yml` (lines 105, 134, 203), `pyproject.toml:116` (`Documentation`), `jellyfin-plugin/README.md:14`, `docs/multi-server.md:194`, `docs/jellyfin-trickplay-gpu.md:81`, `media_preview_generator/servers/jellyfin.py:431,515-547`, `tests/test_servers_jellyfin.py`, `docs/llms-full.txt`
- Create: `tests/test_site_urls.py`

**Interfaces:**
- Consumes: `docs/design/site-redesign-lab/results/jellyfin-redirect-proof.json` (all `follows_redirect: true`), STOP 4 approval.
- Produces: `JellyfinServer.PLUGIN_REPO_URL = "https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json"`, `JellyfinServer.LEGACY_PLUGIN_REPO_URLS: tuple[str, ...]`.

- [ ] **Step 1: Preconditions**

```bash
jq -e '[.results[].follows_redirect] | all' docs/design/site-redesign-lab/results/jellyfin-redirect-proof.json
gh api repos/stevezau/media_preview_generator/pages --jq '[.cname, .https_enforced, .https_certificate.state] | @tsv'
dig +short mediapreviewgenerator.dev A | sort
```

Expected: `true`; `mediapreviewgenerator.dev`, `true`, `approved` (tab-separated); the four GitHub Pages addresses 185.199.108-111.153. Any other result: stop and tell the owner.

- [ ] **Step 2: Write the failing tests**

`tests/test_site_urls.py`:

```python
"""Every public URL points at mediapreviewgenerator.dev, apart from the few that must name the old host.

The github.io project URLs 301 to the custom domain, so a stale one still works, but through a
redirect that costs a round trip, splits search signals, and would break outright if the custom
domain were ever removed. Each allowed exception says why it needs the old host.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
OLD_HOST = "stevezau.github.io/media_preview_generator"
NEW_MANIFEST = "https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json"
ALLOWED = {
    "media_preview_generator/servers/jellyfin.py": "LEGACY_PLUGIN_REPO_URLS recognises installs made before the move",
    "tests/test_servers_jellyfin.py": "tests that recognition",
    "scripts/verify_live_site.py": "checks the old deep links still 301",
    "tests/test_verify_live_site.py": "tests that check",
    "tests/test_site_urls.py": "this file",
}


def test_no_old_host_outside_the_allowlist() -> None:
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    offenders = []
    for relative in filter(None, tracked):
        if relative.startswith("docs/design/") or relative in ALLOWED:
            continue  # design docs are dated records and keep the URLs they were written with
        try:
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        if OLD_HOST in text:
            offenders.append(relative)
    assert offenders == []


def test_site_and_manifest_urls_use_the_custom_domain() -> None:
    config = yaml.safe_load((REPO_ROOT / "docs" / "_config.yml").read_text(encoding="utf-8"))
    assert config["url"] == "https://mediapreviewgenerator.dev"
    docs_yml = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8"))
    assert docs_yml["env"]["LIVE_MANIFEST_URL"] == NEW_MANIFEST
    assert f'PREV_URL="{NEW_MANIFEST}"' in (REPO_ROOT / ".github" / "workflows" / "jellyfin-plugin.yml").read_text(encoding="utf-8")
```

In `tests/test_servers_jellyfin.py`, add (the 3-cell matrix: nothing registered / the old URL / the new URL):

```python
class TestInstallPluginRepositoryUrl:
    """install_plugin must recognise the plugin repository under both hosts (matrix: none / old / new / both).

    Installs set up before the docs moved to mediapreviewgenerator.dev registered the github.io URL,
    which now 301s to the new one (Jellyfin follows it: docs/design/site-redesign-lab/results/
    jellyfin-redirect-proof.json). Matching only the new URL would append a second repository for the
    same plugin on every reinstall, so the install call must name whichever URL the server already has.
    """

    @staticmethod
    def _install(jelly: JellyfinServer, registered: list[dict]) -> tuple[dict, list[tuple[str, str, dict]]]:
        calls: list[tuple[str, str, dict]] = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            response = MagicMock(status_code=200, raise_for_status=MagicMock())
            response.json.return_value = registered if (method, url) == ("GET", "/Repositories") else {}
            return response

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            result = jelly.install_plugin()
        return result, calls

    @staticmethod
    def _repository_writes(calls):
        return [call for call in calls if call[:2] == ("POST", "/Repositories")]

    @staticmethod
    def _install_params(calls) -> dict:
        return next(call for call in calls if call[0] == "POST" and call[1].startswith("/Packages/Installed/"))[2]["params"]

    def test_adds_the_new_url_when_no_repository_is_registered(self, jelly):
        result, calls = self._install(jelly, [])
        writes = self._repository_writes(calls)
        assert len(writes) == 1
        assert writes[0][2]["json_body"] == [
            {"Name": "Media Preview Bridge", "Url": JellyfinServer.PLUGIN_REPO_URL, "Enabled": True}
        ]
        assert self._install_params(calls) == {
            "assemblyGuid": JellyfinServer.PLUGIN_GUID,
            "repositoryUrl": JellyfinServer.PLUGIN_REPO_URL,
        }
        assert result["ok"] is True

    def test_reuses_the_github_io_url_an_older_install_registered(self, jelly):
        legacy = JellyfinServer.LEGACY_PLUGIN_REPO_URLS[0]
        result, calls = self._install(jelly, [{"Name": "Media Preview Bridge", "Url": legacy, "Enabled": True}])
        assert self._repository_writes(calls) == []
        assert self._install_params(calls)["repositoryUrl"] == legacy
        assert result["ok"] is True

    def test_reuses_the_new_url_when_it_is_already_registered(self, jelly):
        new = JellyfinServer.PLUGIN_REPO_URL
        result, calls = self._install(jelly, [{"Name": "Media Preview Bridge", "Url": new, "Enabled": True}])
        assert self._repository_writes(calls) == []
        assert self._install_params(calls)["repositoryUrl"] == new
        assert result["ok"] is True

    def test_prefers_the_new_url_when_both_are_registered_in_any_order(self, jelly):
        legacy, new = JellyfinServer.LEGACY_PLUGIN_REPO_URLS[0], JellyfinServer.PLUGIN_REPO_URL
        for order in ([legacy, new], [new, legacy]):
            repos = [{"Name": "Media Preview Bridge", "Url": url, "Enabled": True} for url in order]
            result, calls = self._install(jelly, repos)
            assert self._repository_writes(calls) == []
            assert self._install_params(calls)["repositoryUrl"] == new, order
            assert result["ok"] is True

    def test_urls_are_the_site_manifest_and_the_old_github_io_one(self):
        assert JellyfinServer.PLUGIN_REPO_URL == "https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json"
        assert JellyfinServer.LEGACY_PLUGIN_REPO_URLS == (
            "https://stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json",
        )
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_site_urls.py tests/test_servers_jellyfin.py::TestInstallPluginRepositoryUrl -q`
Expected: FAIL (old host in several files; `LEGACY_PLUGIN_REPO_URLS` missing). Five `TestInstallPluginRepositoryUrl` tests: none, old, new, both (either order), and the URL constants.

- [ ] **Step 3: The app: recognise both repository URLs**

`media_preview_generator/servers/jellyfin.py`:

```python
    PLUGIN_REPO_URL = "https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json"
    # Installs registered before the docs site moved (2026-09) point at the github.io address,
    # which 301s to PLUGIN_REPO_URL. Treat it as ours, so a reinstall doesn't add a second repository.
    LEGACY_PLUGIN_REPO_URLS: tuple[str, ...] = (
        "https://stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json",
    )
```

In `install_plugin`, replace step 2's `if not any(... r.get("Url") == self.PLUGIN_REPO_URL ...)` block with:

```python
        # 2. Append our repo unless it is already there, under either host.
        # Prefer the current URL when a server somehow has both, so the result never depends on list order.
        registered = {r.get("Url") for r in repos if isinstance(r, dict)}
        repository_url = next((url for url in (self.PLUGIN_REPO_URL, *self.LEGACY_PLUGIN_REPO_URLS) if url in registered), None)
        if repository_url is None:
            repository_url = self.PLUGIN_REPO_URL
            new_repos = [*repos, {"Name": "Media Preview Bridge", "Url": repository_url, "Enabled": True}]
            try:
                self._request("POST", "/Repositories", json_body=new_repos).raise_for_status()
                _record("add_repository", True, "appended")
            except Exception as exc:
                result["error"] = f"could not add repository: {exc}"
                _record("add_repository", False, str(exc))
                return result
        else:
            _record("add_repository", True, f"already present ({repository_url})")
```

and in step 3 pass `"repositoryUrl": repository_url` instead of `self.PLUGIN_REPO_URL`. Update the docstring's step 2 line to "append our plugin's manifest URL unless it, or the pre-2026-09 github.io URL, is already there".

- [ ] **Step 4: Every other absolute URL**

```bash
git grep -l "stevezau.github.io/media_preview_generator" -- ':!docs/design'
sed -i 's#https://stevezau.github.io/media_preview_generator/#https://mediapreviewgenerator.dev/#g' \
  .github/workflows/docs.yml .github/workflows/jellyfin-plugin.yml pyproject.toml jellyfin-plugin/README.md \
  docs/multi-server.md docs/jellyfin-trickplay-gpu.md
```

Then edit by hand: `docs.yml`'s header comment and the comment above `LIVE_MANIFEST_URL` ("When the site moves to a custom domain, update this URL…" becomes "The site's host; jellyfin-plugin.yml's PREV_URL must match."). Re-run the grep: only the allowlisted files may remain. Regenerate: `python scripts/generate_llms_full.py`.

- [ ] **Step 5: Tests and the live redirect check**

Dispatch `verifier` for `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest` and `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest -m e2e -n 8 --no-cov`. Expected: all pass.

```bash
for p in /getting-started/ /comparison/ /guides/previews-readiness/ /jellyfin-plugin/manifest.json; do
  printf '%s  ' "$p"; curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "https://stevezau.github.io/media_preview_generator$p"
done
```

Expected: four `301 https://mediapreviewgenerator.dev<same path>` lines.

- [ ] **Step 6: Deep review, then commit**

```bash
git add .github/workflows/docs.yml .github/workflows/jellyfin-plugin.yml pyproject.toml jellyfin-plugin/README.md \
  docs/multi-server.md docs/jellyfin-trickplay-gpu.md docs/llms-full.txt \
  media_preview_generator/servers/jellyfin.py tests/test_servers_jellyfin.py tests/test_site_urls.py
```

Dispatch `Architecture Review`, and an opus reviewer with: "Check the `install_plugin` change against every cell of the repository matrix (none / old URL / new URL / both present), that the install call names the URL the server actually has, and that the workflows' manifest URLs still end at `/jellyfin-plugin/manifest.json`." Fix HIGH; then:

```bash
git commit -F - <<'EOF'
fix: point every URL at mediapreviewgenerator.dev and keep old Jellyfin installs on one repository

The docs, workflows, pyproject and the plugin README use the custom domain. The app's plugin
repository URL moves too, and install_plugin recognises the old github.io URL (which 301s, and
Jellyfin follows) so a reinstall never adds a second repository.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 13: Launch and verify the live site

Lane A, after Task 12. Suggested model: sonnet (opus if a check fails and needs diagnosis; use `superpowers:systematic-debugging` first).

**Files:**
- Create: `scripts/verify_live_site.py`, `tests/test_verify_live_site.py`
- Modify: `docs/design/discoverability.md` (decisions and "Still open"), `docs/design/site-redesign.md` (status line)

**Interfaces:**
- Consumes: everything above; `docs/_config.yml` (`url`), committed `docs/llms.txt`/`docs/llms-full.txt`.
- Produces: `scripts/verify_live_site.py` with `legacy_target(base: str, legacy_path: str) -> str`, `json_ld_types(html: str) -> set[str]`, `strip_front_matter(text: str) -> str`, `main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing unit tests `tests/test_verify_live_site.py`**

```python
"""The pure helpers of scripts/verify_live_site.py (the network checks run by hand at launch)."""

from __future__ import annotations

from scripts.verify_live_site import json_ld_types, legacy_target, strip_front_matter


def test_legacy_deep_link_maps_to_the_same_path_on_the_new_host() -> None:
    assert legacy_target("https://mediapreviewgenerator.dev", "/guides/previews-readiness/") == (
        "https://mediapreviewgenerator.dev/guides/previews-readiness/"
    )
    assert legacy_target("https://mediapreviewgenerator.dev/", "/jellyfin-plugin/manifest.json") == (
        "https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json"
    )


def test_json_ld_types_collects_list_and_single_types() -> None:
    html = (
        '<script type="application/ld+json">{"@type": ["SoftwareApplication", "SoftwareSourceCode"]}</script>'
        '<script type="application/ld+json">{"@type": "WebSite"}</script>'
    )
    assert json_ld_types(html) == {"SoftwareApplication", "SoftwareSourceCode", "WebSite"}


def test_strip_front_matter_leaves_the_served_body() -> None:
    assert strip_front_matter("---\nlayout: null\npermalink: /llms.txt\n---\n# Title\n") == "# Title\n"
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_verify_live_site.py -q`
Expected: FAIL, module missing.

- [ ] **Step 2: Write `scripts/verify_live_site.py`**

```python
#!/usr/bin/env python3
"""Check the deployed docs site the way a crawler, an AI agent and a Jellyfin server see it.

    python scripts/verify_live_site.py
    python scripts/verify_live_site.py --base https://mediapreviewgenerator.dev

Prints PASS/FAIL per check and exits 1 if any failed:
- /llms.txt and /llms-full.txt are byte-for-byte the committed files minus their front matter;
- /robots.txt names this host's sitemap;
- every sitemap URL and every site URL inside llms-full.txt answers 200;
- every sitemap page has a <title> naming the site; the home page carries SoftwareApplication,
  WebSite and FAQPage JSON-LD, /faq/ FAQPage and BreadcrumbList, other pages BreadcrumbList;
- /jellyfin-plugin/manifest.json is a non-empty JSON list that lists the newest plugin-v* release;
- old github.io deep links answer 301 to the same path on this host.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
OLD_BASE = "https://stevezau.github.io/media_preview_generator"
LEGACY_PATHS = ["/getting-started/", "/comparison/", "/guides/previews-readiness/", "/jellyfin-plugin/manifest.json"]
RELEASES_API = "https://api.github.com/repos/stevezau/media_preview_generator/releases?per_page=50"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
JSON_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
USER_AGENT = "mpg-verify-live-site/1"


def legacy_target(base: str, legacy_path: str) -> str:
    """Where an old github.io deep link must land on the new host."""
    return base.rstrip("/") + legacy_path


def json_ld_types(html: str) -> set[str]:
    """Every schema.org @type declared in the page's JSON-LD blocks."""
    types: set[str] = set()
    for block in JSON_LD.findall(html):
        declared = json.loads(block).get("@type")
        types |= set(declared) if isinstance(declared, list) else {declared}
    return types


def strip_front_matter(text: str) -> str:
    """The body Jekyll serves for a page with front matter."""
    return FRONT_MATTER.sub("", text, count=1)


def _fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _first_hop(url: str) -> tuple[int, str]:
    parts = urlsplit(url)
    connection = http.client.HTTPSConnection(parts.netloc, timeout=30)
    connection.request("GET", parts.path or "/", headers={"User-Agent": USER_AGENT})
    response = connection.getresponse()
    return response.status, response.getheader("Location") or ""


class _Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, what: str) -> None:
        print(("PASS " if ok else "FAIL ") + what)
        self.failures += 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """Run every live check against --base (default: _config.yml's url)."""
    config = yaml.safe_load((DOCS / "_config.yml").read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=f"{config['url']}{config.get('baseurl') or ''}")
    base = parser.parse_args(argv).base.rstrip("/")
    report = _Report()

    for name in ("llms.txt", "llms-full.txt"):
        status, body = _fetch(f"{base}/{name}")
        committed = strip_front_matter((DOCS / name).read_text(encoding="utf-8")).encode()
        report.check(status == 200 and body == committed, f"/{name} serves the committed file ({status}, {len(body)} bytes)")

    status, body = _fetch(f"{base}/robots.txt")
    report.check(status == 200 and f"Sitemap: {base}/sitemap.xml".encode() in body, "/robots.txt names this host's sitemap")

    status, body = _fetch(f"{base}/sitemap.xml")
    pages = [loc.text for loc in ET.fromstring(body).findall("sm:url/sm:loc", SITEMAP_NS)] if status == 200 else []
    report.check(bool(pages), f"/sitemap.xml lists {len(pages)} pages")

    full = strip_front_matter((DOCS / "llms-full.txt").read_text(encoding="utf-8"))
    cited = {url.rstrip(".,;:").split("#", 1)[0] for url in re.findall(re.escape(base) + r"/[^\s)\"'<>]*", full)}
    urls = sorted(set(pages) | cited)
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = dict(zip(urls, pool.map(lambda url: _fetch(url)[0], urls)))
    for url, code in codes.items():
        if code != 200:
            report.check(False, f"{url} answered {code}")
    report.check(all(code == 200 for code in codes.values()), f"all {len(urls)} sitemap and llms-full URLs answer 200")

    for url in pages:
        html = _fetch(url)[1].decode("utf-8", errors="replace")
        title = TITLE.search(html)
        report.check(bool(title) and config["title"] in title.group(1), f"{url} <title> names the site")
        path = urlsplit(url).path
        expected = {"/": {"SoftwareApplication", "WebSite", "FAQPage"}, "/faq/": {"BreadcrumbList", "FAQPage"}}.get(path, {"BreadcrumbList"})
        found = json_ld_types(html)
        report.check(expected <= found, f"{url} JSON-LD has {sorted(expected)} (found {sorted(found)})")

    status, body = _fetch(f"{base}/jellyfin-plugin/manifest.json")
    manifest = json.loads(body) if status == 200 else None
    valid = isinstance(manifest, list) and bool(manifest)
    report.check(valid, f"/jellyfin-plugin/manifest.json is a non-empty JSON list ({status})")
    releases = json.loads(_fetch(RELEASES_API)[1] or b"[]")
    tags = [r["tag_name"] for r in releases if r["tag_name"].startswith("plugin-v") and not r.get("draft")]
    newest = tags[0].removeprefix("plugin-v") if tags else None
    listed = [v.get("version") for v in manifest[0].get("versions", [])] if valid else []
    report.check(newest is not None and newest in listed, f"manifest lists the newest plugin release ({newest})")

    for legacy in LEGACY_PATHS:
        code, location = _first_hop(OLD_BASE + legacy)
        report.check(code == 301 and location == legacy_target(base, legacy), f"{OLD_BASE}{legacy} -> {code} {location}")

    print(f"\n{report.failures} failure(s)")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `PYTHONPATH=$PWD /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_verify_live_site.py tests/test_site_urls.py -q && ruff check scripts/verify_live_site.py`
Expected: pass, ruff clean. Commit both files (Architecture Review first) with `test: live-site verification script for the launch` plus the Co-Authored-By line.

- [ ] **Step 3: Open the PR and merge it (authorised by STOP 4)**

```bash
git push origin stevezau/site-redesign
gh pr create --base dev --head stevezau/site-redesign --title "docs: new site, README and brand (Jekyll, mediapreviewgenerator.dev)" --body-file - <<'EOF'
The docs site moves from MkDocs to a Jekyll port of the Shortlist theme at mediapreviewgenerator.dev,
with a landing page built from real Plex, Jellyfin and Emby players on open films, a benchmark page,
the new logo (site, README, Unraid, app), and rewritten README, Docker Hub README and llms files.

- Pages deploys keep the live Jellyfin manifest (docs.yml's fail-closed step is unchanged).
- Old github.io URLs 301 to the same paths; Jellyfin 10.11 and 12.0 follow the manifest redirect.
- The app's plugin install recognises both repository URLs (tests cover the matrix).

Spec: docs/design/site-redesign.md. Plan: docs/design/site-redesign-plan.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
gh pr checks --watch
```

Expected: all checks green, including `Docs check`. Then merge the way the repo merges to `dev` (recent history shows squash merges with the PR number): `gh pr merge --squash`.

- [ ] **Step 4: Watch the deploy, then verify the live site**

```bash
sleep 20; gh run list --workflow docs.yml --branch dev --limit 1
gh run watch "$(gh run list --workflow docs.yml --branch dev --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
/home/data/.venv/bin/python scripts/verify_live_site.py
```

Expected: the run succeeds and its summary says `Jellyfin manifest from: live https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json`; the script ends `0 failure(s)`. A failure: `superpowers:systematic-debugging` first, then fix forward on a new branch (a Pages deploy is replaceable; the manifest step protects Jellyfin installs meanwhile).

- [ ] **Step 5: Repository metadata (spec §7)**

```bash
gh repo edit stevezau/media_preview_generator \
  --description "Self-hosted, GPU-accelerated preview thumbnails for Plex, Emby and Jellyfin, made as soon as Sonarr or Radarr import a file. HDR tone-mapped, one Docker container." \
  --homepage "https://mediapreviewgenerator.dev/" \
  --add-topic selfhosted --add-topic radarr --add-topic hdr --add-topic dolby-vision --add-topic preview-thumbnails \
  --remove-topic thumbnails-preview --remove-topic media-server-automation \
  --remove-topic self-hosted --remove-topic thumbnails --remove-topic media-server
gh api repos/stevezau/media_preview_generator --jq '{description, homepage, topics: (.topics | length)}'
```

Use the three extra removals the owner confirmed at STOP 4. Expected: the new description, the new homepage, `"topics": 20`.

- [ ] **Step 6: Records and hand-back**

`docs/design/discoverability.md`: under Decisions, "Domain: undecided…" becomes "Domain: mediapreviewgenerator.dev, live since 2026-09-23; the docs moved to Jekyll in the site redesign (docs/design/site-redesign.md)". In "Still open", replace "Domain purchase + the switch checklist above (later)" with the owner items below, keep the 2026-10-21 search baseline re-run, and keep "Dolby Vision Profile 5 'dim without Vulkan' has never been checked on a real P5 clip" (no freely licensed Profile 5 sample turned up during this work). `docs/design/site-redesign.md` status line: "shipped <date> (#<PR number>)".

Commit (Architecture Review first): `docs: record the site launch and what the owner still has to do` plus the Co-Authored-By line; push to `dev` only through a PR, the same way as above.

Report to the owner, in this order:
- Live checks: `verify_live_site.py` result, the deploy run link.
- Owner actions: add `mediapreviewgenerator.dev` as a Search Console Domain property (DNS TXT) and submit `https://mediapreviewgenerator.dev/sitemap.xml`; upload `docs/images/social-preview.jpg` at GitHub, Settings, Social preview; the Docker Hub description syncs on the next release tag; the app's new plugin repository URL reaches users in the next app release; decide when to remove the stopped `mlab-plex-pre-openfilms`, `mlab-jellyfin-pre-openfilms`, `mlab-emby-pre-openfilms` containers; re-run the search baseline around 2026-10-21.

---

## Self-review notes (for the executor and the reviewer)

Spec coverage, section by section:
- §1 goal and success criteria: Tasks 9 (landing), 5 (real players), 8 (benchmark source), 1-2 and 12-13 (nothing shipped regresses: manifest, drift, images, titles, lists, JSON-LD, Search Console, 301s).
- §2 decisions: Jekyll port (T1), real players on three servers (T4-T5), domain (T1 `url`, T12, T13), logo A (T3), name kept and no intro/credit claims (Global Constraints; llms.txt status line says it doesn't detect them).
- §3 architecture: `_config.yml`, layouts, includes, assets, 404, robots, search, CNAME (T1); `_data` files (T1 faq, T9 the rest); kept Shortlist features, including BreadcrumbList, footer sitemap, count-up, sticky tour, search, copy buttons, pills (T1 port, T9 use); alerts plugin (T1); Jekyll build in `docs.yml` (T1); front matter and MkDocs removal (T1). Domain switch steps 1-5: DNS and certificate already done by the owner (verified in T12 Step 1); Jellyfin 301 proof (T4 Step 1); URL switch (T1 + T12); deep-link 301s (T12 Step 5, T13 Step 4); Search Console (T13 owner action).
- §4 landing sections 1-14: nav (T1), 2-13 (T9), footer with film credits and the not-affiliated line (T1 + T10).
- §5 docs pages: doc layout (T1), comparison (T10), FAQ wording (T9 + T10), 404 cards (T1).
- §6 README and Docker Hub README (T11).
- §7 voice (Global Constraints, `test_site_claims.py`), llms.txt (T11), llms-full port with drift and permalink tests (T2), GitHub description and topics (T13).
- §8 lab (T4), captures (T5), app screenshots (T6), social card (T7), benchmark (T8), Dolby Vision 5 left unverified (T13 Step 6).
- §9 logo (T3). §10 tests: T1 (build, head tags, JSON-LD, FAQPage == faq.yml, robots and sitemap, titles, lists, alerts, no manifest, actionlint), T2 (llms-full, links, images), T7 (social card geometry). §11 gates: STOP 3 (T8 Step 4), STOP 2 (T9 Step 9), STOP 4 (T11 Step 6). §13 risks: T4 Step 1, T5 Step 4.

Deviations the owner should know about:
- STOP 3 comes before STOP 2 in time: the landing page is reviewed with the approved captures on it, because §11.3 says captures don't go on the site before they're approved.
- The app shows no poster art anywhere, so the "poster art" half of the fixture requirement (§8) is met by the lab libraries' local posters and the players, not the app screenshots.
- Spring, Sprite Fright and Charge are optional (time-boxed search in T4 Step 2): no verified download URLs were known.
- GitHub caps topics at 20; the spec's add-five/drop-two gives 23, so three more removals are proposed at STOP 4.
