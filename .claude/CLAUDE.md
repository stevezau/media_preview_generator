# Media Preview Generator

GPU-accelerated video preview thumbnail (BIF) generation for Plex Media Server. Uses FFmpeg hardware decoding (CUDA, VAAPI, QSV, VideoToolbox) and parallel workers. Docker-only deployment with a web UI as the sole interface (no CLI).

## Commands

```bash
# Install
pip install -e ".[dev]"

# Run (web UI)
gunicorn media_preview_generator.web.wsgi:app --bind 0.0.0.0:8080 --worker-class gthread --workers 1

# Test — default runs parallel (xdist, worksteal), excludes gpu + e2e, keeps coverage
pytest                                          # ~100s, 12604 tests, ~89% cov
pytest --no-cov tests/test_config.py            # Single file, skip coverage
pytest -m e2e -n 8 --no-cov                     # E2E: cap at 8 workers, NOT -n auto (see below)
pytest -m e2e -n 0 --no-cov                     # E2E serial (also fine)
pytest -n 0                                     # Serial mode (for debugging)

# Docs (Jekyll, docs/) — after editing anything under docs/ (pages, _data/*.yml, nav), regenerate llms-full
python scripts/generate_llms_full.py            # writes docs/llms-full.txt
python scripts/generate_llms_full.py --check    # CI-style: non-zero exit if stale
pytest --no-cov -n 0 tests/test_docs_site.py    # builds the site via host Bundler or the ruby:3.4 image
```

**Tests run under `/dev/shm`:** `tests/conftest.py` points pytest's temp root (`tmp_path`,
`PYTEST_DEBUG_TEMPROOT`) at `/dev/shm` when there's ~4GB+ free, since `/tmp` on this box is
ext4 on a loop device (~11.7ms/fsync) and the suite's sqlite-heavy fixtures fsync thousands of
times — that alone was 84% of wall time (11m23s -> ~100s once moved to tmpfs, plus
`--dist worksteal` instead of `load` so one xdist worker doesn't get stuck with a slow tail).
Falls back to `/tmp` automatically (slower, still correct) when `/dev/shm` is small or missing,
e.g. Docker's default 64MB `/dev/shm` — or set `PYTEST_DEBUG_TEMPROOT` yourself to opt out.

**E2E parallelism cap:** Do NOT run `pytest -m e2e -n auto` on a multi-core box.
Each xdist worker spawns ~5 chromium processes; each chrome process reserves
~1.4 TB virtual memory (chrome's normal V8 heap reservation). With 24 workers
× 5 = ~120 chrome processes, the system's virtual-memory commit ceiling
(set by `vm.overcommit_memory=0` to ~50% of physical RAM) is exceeded. The
kernel OOM killer fires and picks chrome-headless (oom_score_adj=300) as
victim, killing browser processes mid-test → "Not properly terminated"
xdist failures. Verified via journalctl kernel logs during diagnostic runs
in commit f856944 follow-up.

The CI ships a different pattern: pytest-shard splits the e2e suite across
4 GitHub Actions runners, each running `-n 0` (serial) on its own slice —
411 e2e tests today, so ~100-107 per shard (measured 103/107/100/101; the
largest takes 110s serially on this box). Locally, `-n 8` is empirically
stable (verified 33/33 pass).

CI's unit-test job is also sharded: the `unit` matrix job runs 3 shards (each still
`-n auto --dist worksteal`, pytest-shard splits by test count), uploading one
`.coverage.unit-<n>` data file per shard. The required `test` check (its id/name has to
stay `test` for the repo ruleset) then `needs: unit`, downloads the 3 artifacts,
`coverage combine`s them and enforces the 70% floor once over the combined total.

```bash

# Lint and format
ruff check . --fix
ruff format .

# Docker
docker build -t plex-previews:dev .
```

## Architecture

```
media_preview_generator/
├── config.py              # @dataclass Config, loads from settings.json
├── plex_client.py         # Plex API: library queries, path resolution, retry
├── worker.py              # ThreadPool workers, GPU task assignment
├── media_processing.py    # FFmpeg execution, BIF generation, HDR detection
├── processing.py          # Job orchestration
├── gpu_detection.py       # GPU discovery (NVIDIA/AMD/Intel/Apple)
├── bif_reader.py          # BIF file parsing for web viewer
├── utils.py               # Path sanitization, Docker detection
├── logging_config.py      # Loguru + Rich console setup
├── version_check.py       # GitHub release version checking
├── upgrade.py             # Settings migration / schema upgrades
└── web/
    ├── wsgi.py            # Gunicorn entry point
    ├── app.py             # App factory, SocketIO init (async_mode=threading)
    ├── auth.py            # Token authentication (@login_required, @api_token_required)
    ├── jobs.py            # Job state management + SocketIO events
    ├── settings_manager.py# settings.json persistence, env migration, gpu_config
    ├── scheduler.py       # APScheduler with SQLAlchemy jobstore
    ├── webhooks.py        # Radarr/Sonarr/Tdarr webhook handlers
    ├── routes/            # Modular API routes (api_bif, api_jobs, api_plex, api_schedules, api_settings, api_system, job_runner, pages)
    ├── templates/         # Jinja2 HTML (base, index, settings, setup, login, logs, bif_viewer, webhooks)
    └── static/            # CSS, JS, images
```

**Data flow**: Web UI -> `settings_manager` (settings.json) -> `load_config()` -> `job_runner` builds workers from `gpu_config` -> `WorkerPool` -> `process_item()` -> FFmpeg -> BIF

## Code Style

- **Formatter/Linter**: `ruff format` and `ruff check` (config in pyproject.toml)
- **Imports**: stdlib -> third-party -> local (relative imports within package)
- **Type hints**: Required on function parameters and return types
- **Docstrings**: Google-style with Args, Returns, Raises sections
- **Logging**: `from loguru import logger` (never stdlib `logging`)
- **Max line length**: 120 chars

## Conventions

- **Configuration**: `settings.json` is the sole source of truth. Env vars are one-time seed values migrated on first start. Infrastructure vars (`CONFIG_DIR`, `WEB_PORT`, `PUID`, `PGID`, `TZ`, `CORS_ORIGINS`) remain active.
- **GPU config**: Per-GPU in settings (`gpu_config`: enabled, workers, ffmpeg_threads per device).
- **Error handling**: Custom exceptions + `retry_plex_call()` with backoff for Plex API. `CodecNotSupportedError` for FFmpeg fallback.
- **Commits**: Follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- **Architecture Review — by risk, not by habit.** The agent
  (`.claude/agents/architecture-review.md`) runs on the strong model and costs ~100k tokens a run,
  so spend it where bugs are expensive. Dispatch it, and block on HIGH findings, when the diff:
  - **writes anything to a Plex config directory** (BIF output paths, `Media/localhost/**`,
    index files) — a wrong path corrupts someone's library;
  - touches **FFmpeg command construction, codec fallback, or HDR detection**;
  - touches **GPU detection, the worker pool, or anything concurrent** — lazy-init races are
    bug shape 3 and have shipped before;
  - changes the **`settings.json` schema or `upgrade.py` migrations**;
  - touches **auth, tokens, or the `@login_required` / `@api_token_required` decorators**;
  - changes **path sanitization** (`sanitize_path`, `_safe_resolve_within`) or **webhook handlers**,
    both of which take untrusted input;
  - is a **release commit or a Dockerfile change**, whatever it contains.

  Skip it for docs, comments, logging, test-only, template/CSS-only, and dependency-bump commits —
  `ruff`, the 1321-test suite and CI already cover those, and a review there finds style, not bugs.

  Block on HIGH severity findings; discuss MED before committing; LOW is informational. This catches
  the eight production-bug shapes that have shipped before — bug-blind tests, un-wrapped
  failure_scope, lazy-init races, vestigial blocking work, comments-vs-code drift.
- **Docker awareness**: Check `utils.is_docker_environment()` for container-specific behavior.

## Security

- Never log Plex tokens. Tokens come via `PLEX_TOKEN` env var.
- Web endpoints use `@login_required` or `@api_token_required` decorators.
- File paths sanitized via `utils.sanitize_path()` and `_safe_resolve_within()`.
- Write only preview outputs: Plex BIFs into the Plex config dir; Emby BIFs and default-layout Jellyfin
  trickplay next to the video (so those users need a writable media mount); off-media Jellyfin trickplay
  into Jellyfin's config dir. Never write anything else under media paths.

## BIF File Format

BIF (Base Index Frame) is Roku's format for video preview thumbnails, also used by Plex.

```
Header (64 bytes): Magic (8) + Version uint32 (4) + Image count uint32 (4) + Frame interval ms uint32 (4) + Reserved (44)
Index table: 8 bytes per image (timestamp uint32 + offset uint32) + 8-byte terminator (0xffffffff + final offset)
Image data: Concatenated JPEG files
```

Generation: FFmpeg extracts frames -> numbered `.jpg` files -> `generate_bif()` packs into `.bif`
Output: `{plex_config}/Media/localhost/{hash}/Indexes/index-sd.bif`

## Key Dependencies

Python >=3.11 | Flask 3.x | Flask-SocketIO | plexapi | loguru | APScheduler 3.x | SQLAlchemy 2.x | gunicorn | pymediainfo | requests

## Test Fixtures

Use mocks from `tests/conftest.py`: `mock_config`, `mock_plex_server`, `tmp_path` (pytest built-in). External dependencies (Plex API, FFmpeg, filesystem) must be mocked. See `.claude/rules/testing.md` for patterns.
