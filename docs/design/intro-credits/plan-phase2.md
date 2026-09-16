# Intro & Credits — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** TV episodes that chapters and online sources don't cover get Skip Intro markers from season audio matching
(weekly releases included), Emby servers receive markers through a new self-healing Bridge plugin, a periodic
reconcile puts back markers a server dropped, the Inspector gains a Season view, and an in-repo accuracy harness proves
the numbers against spec §5.3 and against Plex's own markers.

**Architecture:** `markers/audio/` adds a chromaprint fingerprint cache in `markers.db`, a numpy port of the v3 season
matcher and the season step. The season step is a `LocalDetectorSpec`: fingerprinting runs on the existing GPU/CPU
workers (CPU ffmpeg, at most 2 at once), matching cached fingerprints runs inline on the checking threads, and
episodes of the same season that a new fingerprint could change are re-decided by a small "Season" follow-up job.
Emby gets `emby-plugin/` (C#, netstandard2.0, Emby 4.9 and 4.10 builds) and `publishers/emby.py`. `markers/reconcile.py`
is a 12-hourly LOW Intro & Credits job that reads back every published item in bulk and re-runs only drifted files.
`tools/markers_eval/` is the accuracy harness (never in CI).

**Tech Stack:** Python 3.12 (image) / 3.14 (dev venv), numpy (new runtime dependency), SQLite (stdlib), ffmpeg with
the chromaprint muxer (`/usr/lib/jellyfin-ffmpeg/ffmpeg`), Flask, APScheduler 3, C# netstandard2.0 against
`MediaBrowser.Server.Core` (Emby), Bootstrap 5 + vanilla JS, pytest, pytest-recording (VCR), Playwright.

**Spec:** `docs/design/intro-credits/spec.md` (Revision 3) — read §0 first, then §3.3, §5.3, §5.5, §6.1–§6.4, §7.1–§7.4,
§10, §12, §13, §14. `docs/design/intro-credits/plex-item-publishing.md` binds every publisher change. Roadmap and its
Global Constraints: `docs/design/intro-credits/plan-roadmap.md`. `plan-phase1.md` is history: where its code blocks
disagree with the code on the branch, the code and spec §14 win. Phase-1 ledger (rulings, parked items):
`.superpowers/sdd/plan-phase1/progress.md`.

## Global Constraints

Every task implicitly includes these. The roadmap's Global Constraints still bind; the lines that matter in phase 2
are repeated so an implementer who sees only one task has them.

**Carried over (owner rules, spec §0 and roadmap):**
- Feature is **off until turned on per server** (`media_servers[].markers.enabled`, default `false`); nothing is written
  to a server whose switch is off. **Precision over coverage:** a missing marker is acceptable; a wrong one is not.
- Default publish rule `publish_when = "high"`: chapters (unless two agreeing independent sources contradict them), or
  two independent sources agree (intro/recap **end** within **5 s**; credits/preview **start** within **10 s**).
  Server markers never decide alone and never supply times (they may shorten a skip). IntroDB + TheIntroDB are one
  source. A locked user marker always wins. Results never depend on input order.
- File identity = canonical path + size + mtime. A change invalidates fingerprints, evidence and unlocked markers.
  All marker times are integer **milliseconds**; Jellyfin/Emby ticks = ms × 10,000.
- Resource rules: marker items run on the **existing** WorkerPool (no extra workers); ffmpeg `-threads 2`; fingerprints
  ≤ **2** in parallel; online lookups paced from response headers; webhook-triggered jobs at **NORMAL**, backfill,
  schedules and reconcile at **LOW**. Storage is shared: one heavy job at a time, `nice -n 19` for lab/harness runs.
- Prove server behaviour on the **lab servers on storage** only (`docs/design/intro-credits/evidence/lab/`: `mlab-plex`
  claimed with Plex Pass, `mlab-jellyfin` 10.11, `mlab-jf12` 12.0, `mlab-emby` 4.10.0.40, `mlab-app`), never on the
  prod Plex on `plex`. The **prod Plex DB is read-only**: `sqlite3 "file:<db>?mode=ro"` over ssh.
- **Never delete or write files under `/data*`** (the owner's library, both hosts). Lab mounts are `:ro`; the harness
  and lab rows only `stat`/`ffprobe`/`ffmpeg`-read real files and write their caches outside `/data*`.
- **TheIntroDB API key is a secret:** masked as `****` in every API response, never logged, round-trips unchanged when
  `****` is posted back. **Never log tokens** (Plex, Jellyfin, Emby, `evidence/lab/env`); lab scripts scrub them with
  `phase1_matrix.scrub`. Never commit `evidence/lab/env` or any other secret.
- Tests follow `.claude/rules/testing.md`: mock Plex/FFmpeg/HTTP/filesystem in unit tests, assert the kwargs/SQL the
  code controls (not call counts), cover every cell of a branchy matrix. Tests needing real ffmpeg, real media or real
  servers are marked `integration` and never run in default CI.
- Code style: ruff (line 120), type hints everywhere, Google docstrings on public APIs, `from loguru import logger`,
  comments explain *why* only. Implementers run `pre-commit run --files <staged files>` before reporting.
- UI: wording and layout from the owner-approved design artifact (`evidence/design/index.html`, section "Inspector →
  Season view"); every non-obvious control gets an ⓘ tooltip (`.info-icon`, `_initBootstrapTooltips`). Any wording or
  layout that differs from the mockup → screenshot to the owner **before** the commit.
- Git: branch `feat/markers-detection` (PR #241 → `dev`). Conventional Commits. **Before every commit run the
  `Architecture Review` agent** (`.claude/agents/architecture-review.md`) on the staged diff; HIGH blocks, MED is
  discussed. Commit with `PATH="/home/data/.venv/bin:$PATH" git commit`. Never commit to `dev`/`main`. **Nothing merges
  into `dev` until the owner says it is fully tested.** Releases (plugin tags, manifests, catalog entries, app tags)
  only on the owner's explicit word "release".
- Update the spec (not just code) whenever a decision changes; add a dated line to spec §14.
- Test commands (storage): `/home/data/.venv/bin/python -m pytest --no-cov <files>` for a task;
  `/home/data/.venv/bin/python -m pytest` (full) before each push; `/home/data/.venv/bin/python -m pytest -m e2e -n 8
  --no-cov` for UI tasks (never `-n auto`). In a worktree lane: `PYTHONPATH=<worktree> ... -n 4`.

**Phase-2 additions:**
- `numpy>=2.0,<3` becomes a runtime dependency (`pyproject.toml`). It is imported only inside `markers/audio/` and
  `tools/markers_eval/`, never at app start-up.
- Chromaprint exists only in jellyfin-ffmpeg (amd64 image). No chromaprint ffmpeg → the season audio detector is not
  registered, jobs log one warning, and Settings shows the source as unavailable with the reason (spec roadmap P2).
- The v3 matcher port must equal `tools/markers_eval/fp3_reference.py` (a verbatim copy of
  `evidence/detect/fp3.py`'s logic) run for run on the same fingerprints, and the harness must reproduce spec §5.3
  (91 useful / 13 wrong / 14 missed of 118) before the season audio detector is enabled by default (Task 6 gate).
- Emby plugin: Emby catalog submission needs the owner's forum thread / developer id (roadmap checkpoint 4). Until the
  catalog accepts it, installs are manual (DLL copy + restart) and the app says so.
- A second Emby lab container `mlab-emby49` (`emby/embyserver:4.9.1.90`, port 18099) is added for the 4.9 build.
- Open owner rulings this plan implements with a recommended default (each marked **OWNER RULING** in its task):
  R1 → owner (2026-09-14): Emby always gets credits (Task 10); R2 → season audio never decides alone in phase 2,
  harness measures it (Tasks 7, 15); R3 season matching inline + Season follow-up jobs instead of the dispatcher
  completion callback (Tasks 3, 7, 8); R4 → owner: "Publish N to M servers" queues a normal job for the season
  (Tasks 12, 14); R5 → owner: "Check servers" is a user-scheduled job, nothing scheduled by default (Task 11). The
  **OWNER DECISION** paragraphs in Tasks 7, 10 and 11 supersede the ruling text below them.
- Open questions this plan measures but doesn't decide (the owner rules on the numbers): G3 season audio and Plex's
  own intro detection both match audio, yet count as two independent sources at "High" (Task 17 row 3 records Plex's
  own markers on the synth season); G4 on the 118-episode set "High" can't publish more intros than Plex's own markers
  by construction, so Task 15 gates on "Medium" beats Plex and "High" is no more wrong; G5 spec §5.3 was measured on
  at most 8 episodes per season while the app matches the whole folder (Task 15 reports both modes).

## Execution model

- **Lanes (owner: parallel worktree lanes, 2026-09-14).** Tasks marked `[lane-parallel]` share no files with the other
  tasks of their wave and run in git worktrees under the session scratchpad (`PYTHONPATH=<worktree>` so tests import
  the lane's code). The controller commits in the lane branch, cherry-picks onto `feat/markers-detection`, runs the
  full suite, pushes. `[sequential]` tasks start from the branch head after their dependencies landed.
- **Reviews (owner, ledger 2026-09-14).** One combined review per task (spec compliance + Architecture Review shapes).
  `[high-risk]` tasks get a deep adversarial review (opus; mutants, real data or lab, fuzz where it fits) and a scoped
  re-review only after a HIGH. Whole-branch audits at two milestones: after Task 12 (backend complete) and before
  Task 17 (pre-lab).
- **Architecture Review agent** runs on every staged diff before its commit, in parallel with the task review.

## Task order and dependencies

| # | Task | Depends on | Lane | Risk |
|---|---|---|---|---|
| 1 | Audio store tables, fingerprint cache, keyed locks | — | wave 1 · lane-parallel | |
| 2 | v3 season matcher (numpy port) + fp3 reference | — | wave 1 · lane-parallel | high-risk |
| 3 | Pipeline plumbing for local detectors | — | wave 1 · lane-parallel | high-risk |
| 4 | Emby Bridge plugin (C#), 4.9 + 4.10 builds, lab proof | — | wave 1 · lane-parallel | high-risk |
| 5 | Marker API contract cassettes (Plex `get_markers`, Jellyfin Bridge + `/MediaSegments`) | — | wave 1 · lane-parallel | |
| 6 | Harness skeleton + matcher reproduction gate (118 episodes) | 1, 2 | sequential | |
| 7 | Season step + season audio detector (+ previous-season hint source) | 1, 2, 3, 6 | sequential | high-risk |
| 8 | Season follow-up jobs + vendor-webhook season grouping | 7 | sequential | high-risk |
| 9 | Plex version drift in read-back | 3 | sequential (after 3, parallel to 6–8) | high-risk |
| 10 | Emby server client + `EmbyMarkerPublisher` + install route | 4, 9 | sequential | high-risk |
| 11 | Reconcile sweep (bulk read-back, 12-hourly "Check servers" job, on-demand route) | 9, 10 | sequential | high-risk |
| 12 | Markers API — season payload, season publish, local source status, evidence labels | 7, 10 | sequential | |
| 13 | UI — season audio source live, Emby status and install, Inspector audio evidence | 10, 12 | sequential · parallel to 14 | |
| 14 | UI — Inspector → Season view | 12 | sequential · parallel to 13 | |
| 15 | Harness — full report (decisions vs Plex's own markers, online cases, credits chapter rules) | 6, 7 | sequential · parallel to 9–14 | |
| 16 | Docs, spec amendments, Emby catalog text | 1–15 | sequential | |
| 17 | Phase-2 lab matrix (`phase2_matrix.py`) — the phase-2 "done when" | 1–16 | sequential | high-risk |
| 18 | PR, image, close-out | 17 | sequential | |

Wave 1 (Tasks 1–5) can start together. After wave 1: lane A runs 6 → 7 → 8, lane B runs 9 → 10 → 11, lane C runs 15
once 7 lands. Tasks 7 and 9 both touch `pipeline.py` in different functions (`build_context`/`_decision_order` vs
`_publish_to`); rebase the later one onto the branch head before its commit. Tasks 13 and 14 both edit
`markers_inspector.js` in different functions (Task 13: `SOURCES`, `segmentLane`, `serverLines`; Task 14:
`loadMarkersInspector`, `render`); only Task 14 edits `bif_viewer.html`. Rebase the later one before its commit.

## File map (phase 2)

```
media_preview_generator/
  markers/
    locks.py                       # T1  KeyedLocks (moved out of pipeline.py)
    audio/
      __init__.py                  # T1/T2 POINT_S (identical file in both tasks)
      fingerprint.py               # T1  chromaprint via ffmpeg, cached in markers.db, ≤ 2 at once
      matcher.py                   # T2  v3 matcher (numpy port of evidence/detect/fp3.py)
      season.py                    # T7  season group, previous season, season audio detector spec
    store.py                       # T1 fingerprints/pairs/detector runs; T9 item versions; T11 published items
    models.py                      # T7  Source.SEASON_AUDIO_PREVIOUS
    decide.py                      # T7  previous-season hint: same group as season audio, agreement only
    pipeline.py                    # T3 detector plumbing; T7 detector registration; T9 version drift; T10 projection
    job_runner.py                  # T8 season follow-ups, config re-read; T11 reconcile items
    triggers.py                    # T8 season grouping; T11 reconcile flag; T12 season publish
    reconcile.py                   # T11 drift scan, scheduled entry point
    inspect.py                     # T10 Emby status; T12 season payload, evidence labels
    publishers/
      base.py                      # T9 Shown.VERSIONS_CHANGED, item_files; T10 projection duration; T11 shows_many
      plex_db.py                   # T9 version files; T11 bulk read-back
      jellyfin.py                  # T9 shows(item_files) signature
      emby.py                      # T10 Emby Bridge publisher
      factory.py                   # T10 Emby branch
  servers/_embyish.py              # T10 shared Bridge ping/access probe
  servers/jellyfin.py              # T10 (moved methods out)
  servers/emby.py                  # T10 Emby Bridge client, catalog install
  web/routes/api_markers.py        # T11 reconcile route; T12 season + local source routes
  web/routes/api_servers.py        # T10 install-plugin for Emby
  web/scheduler.py, web/app.py     # T11 reconcile interval job
  web/templates/settings.html      # T13 season audio source live
  web/templates/bif_viewer.html    # T14 Season view pane
  web/static/js/markers_server_tab.js, markers_inspector.js   # T13
  web/static/js/markers_season.js, css/pages/markers_inspector.css   # T14
emby-plugin/                       # T4  Media Preview Bridge for Emby
.github/workflows/plugins-ci.yml   # T4  Emby builds; emby-plugin.yml (release workflow, never run without "release")
tools/markers_eval/                # T2 fp3_reference; T6 gate; T15 full report
tests/markers/audio/, tests/markers_eval/, tests/markers/test_*.py, tests/e2e/test_intro_credits_*.py
docs/design/intro-credits/evidence/lab/   # T4 up.sh emby49; T17 synth_audio.sh, phase2_matrix.py, phase2-results.md
```

---
## Task 1: Audio store tables, fingerprint cache, keyed locks

`[lane-parallel]` — spec §5.3 (fingerprint command, window, 0.1238 s/point), §5.6 (≤ 2 fingerprints in parallel,
`-threads 2`), §6.1 (`fingerprints` table; identity change invalidates), roadmap phase 2 bullet 1.

The `fingerprints` table already exists in `store.py` `_SCHEMA` (phase 1 created it, nothing reads or writes it).
This task adds the reads/writes, the pair cache and detector-run signatures the season step (Task 7) needs, and moves
`_KeyedLocks` out of `pipeline.py` so fingerprinting can use it without importing the pipeline.

**Files:**
- Create: `media_preview_generator/markers/locks.py`, `media_preview_generator/markers/audio/__init__.py`,
  `media_preview_generator/markers/audio/fingerprint.py`
- Modify: `media_preview_generator/markers/store.py` (`_SCHEMA` tuple ~28-137: two tables; `upsert_file` ~373-382:
  invalidation; new `StoredFingerprint` dataclass after `PublishStateRow` ~208; new methods after `get_server_kind`
  ~713), `media_preview_generator/markers/pipeline.py` (delete `class _KeyedLocks` ~191-220, import it from
  `locks.py` under the same private name so `tests/markers/test_pipeline.py` and `test_publisher_contract.py` keep
  working), `pyproject.toml` (`dependencies`)
- Test: `tests/markers/test_locks.py`, `tests/markers/test_store_audio.py`, `tests/markers/audio/__init__.py` (empty),
  `tests/markers/audio/test_fingerprint.py`

**Interfaces:**
- Consumes: `MarkerStore`, `FileRecord`, `FileIdentity`, `Source` (phase 1).
- Produces (Tasks 6, 7, 15 rely on these names):
```python
# markers/locks.py
class KeyedLocks:
    def __init__(self, lock_factory: Callable[[], Any] = threading.Lock) -> None
    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]

# markers/audio/__init__.py
POINT_S: float                                   # 4096 / 11025 / 3

# markers/audio/fingerprint.py
WINDOW = "intro"; ALGORITHM = 1; MAX_WINDOW_S = 900.0; WINDOW_FRACTION = 0.35; FFMPEG_THREADS = 2; MAX_PARALLEL = 2
JELLYFIN_FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
class FingerprintError(Exception)
def window_s(duration_ms: int) -> float
def has_chromaprint(ffmpeg: str) -> bool                           # lru_cache'd `ffmpeg -hide_banner -muxers`
def chromaprint_ffmpeg(configured: str | None) -> str | None       # configured → jellyfin-ffmpeg → PATH ffmpeg
def chromaprint_status(configured: str | None) -> tuple[str | None, str]   # (ffmpeg or None, user-facing reason)
def fingerprint_command(ffmpeg: str, path: str, length_s: float) -> list[str]
def compute_fingerprint(path: str, duration_ms: int, *, ffmpeg: str,
                        cancel_check: Callable[[], bool] | None = None, timeout_s: float = 300.0) -> np.ndarray
def ensure_fingerprint(store: MarkerStore, rec: FileRecord, *, ffmpeg: str,
                       cancel_check: Callable[[], bool] | None = None) -> np.ndarray | None
def points_of(stored: StoredFingerprint) -> np.ndarray

# markers/store.py
@dataclass(frozen=True) class StoredFingerprint:
    file_id: int; window: str; start_s: float; length_s: float; algorithm: int; points: bytes
MarkerStore.set_fingerprint(self, file_id: int, *, size: int, mtime_ns: int, window: str, start_s: float,
                            length_s: float, algorithm: int, points: bytes) -> bool
MarkerStore.get_fingerprint(self, file_id: int, window: str) -> StoredFingerprint | None
MarkerStore.get_season_pair(self, file_a: int, file_b: int, matcher_version: int) -> list[tuple[float, float, float, float]] | None
MarkerStore.set_season_pair(self, file_a: int, file_b: int, matcher_version: int,
                            runs: list[tuple[float, float, float, float]]) -> bool
MarkerStore.get_detector_run(self, file_id: int, source: Source) -> str | None
MarkerStore.set_detector_run(self, file_id: int, source: Source, signature: str) -> None
```
`file_a`/`file_b` are ordered by the caller: `file_a` is the matcher's first argument (the v3 matcher is not symmetric,
see Task 2; Task 7 pairs same-season files in path order and a new season's episode first against the previous season).

- [ ] **Step 1: Write the failing lock and store tests**

```python
# tests/markers/test_locks.py
"""KeyedLocks: one lock per key, forgotten once nobody holds or waits for it."""

import threading

from media_preview_generator.markers.locks import KeyedLocks


def test_same_key_is_exclusive_and_different_keys_are_not():
    locks = KeyedLocks()
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def first():
        with locks.hold("a"):
            order.append("first-in")
            entered.set()
            release.wait(5)
            order.append("first-out")

    def second_holder():
        with locks.hold("a"):
            order.append("second-in")

    t = threading.Thread(target=first)
    t.start()
    assert entered.wait(5)
    with locks.hold("b"):
        order.append("other-key")
    second = threading.Thread(target=second_holder)
    second.start()
    second.join(0.2)
    assert order == ["first-in", "other-key"]
    release.set()
    t.join(5)
    second.join(5)
    assert order == ["first-in", "other-key", "first-out", "second-in"]


def test_lock_entry_is_dropped_when_released():
    locks = KeyedLocks()
    with locks.hold(7):
        assert 7 in locks._locks
    assert locks._locks == {}


def test_lock_factory_is_used():
    made = []

    def factory():
        made.append(threading.Lock())
        return made[-1]

    locks = KeyedLocks(lock_factory=factory)
    with locks.hold("x"):
        pass
    assert len(made) == 1
```

```python
# tests/markers/test_store_audio.py
"""markers.db audio tables: fingerprints gated on file identity, pair cache, detector runs, invalidation."""

from __future__ import annotations

import pytest

from media_preview_generator.markers.models import FileIdentity, Source
from media_preview_generator.markers.store import MarkerStore, StoredFingerprint


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _file(store, name="/m/S01E01.mkv", size=100, mtime=1):
    return store.upsert_file(FileIdentity(name, size, mtime), duration_ms=1_300_000, season_key="/m", is_movie=False)


def _fp(store, rec, points=b"\x01\x00\x00\x00\x02\x00\x00\x00", **overrides):
    kwargs = dict(size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0, length_s=455.0, algorithm=1)
    kwargs.update(overrides)
    return store.set_fingerprint(rec.id, points=points, **kwargs)


class TestFingerprints:
    def test_round_trip(self, store):
        rec = _file(store)
        assert _fp(store, rec) is True
        assert store.get_fingerprint(rec.id, "intro") == StoredFingerprint(
            rec.id, "intro", 0.0, 455.0, 1, b"\x01\x00\x00\x00\x02\x00\x00\x00"
        )
        assert store.get_fingerprint(rec.id, "other") is None

    def test_refused_when_the_file_row_has_another_identity(self, store):
        rec = _file(store)
        _file(store, size=200, mtime=2)  # the file was replaced while ffmpeg ran
        assert _fp(store, rec) is False
        assert store.get_fingerprint(rec.id, "intro") is None

    def test_empty_points_record_a_file_without_audio(self, store):
        rec = _file(store)
        assert _fp(store, rec, points=b"") is True
        assert store.get_fingerprint(rec.id, "intro").points == b""

    def test_identity_change_clears_fingerprint_pairs_and_detector_runs(self, store):
        a, b = _file(store, "/m/S01E01.mkv"), _file(store, "/m/S01E02.mkv")
        _fp(store, a), _fp(store, b)
        assert store.set_season_pair(a.id, b.id, 3, [(1.0, 2.0, 3.0, 4.0)]) is True
        store.set_detector_run(b.id, Source.SEASON_AUDIO, "sig")
        _file(store, "/m/S01E01.mkv", size=999, mtime=9)
        assert store.get_fingerprint(a.id, "intro") is None
        assert store.get_season_pair(a.id, b.id, 3) is None
        # b itself didn't change: its own fingerprint and run stay, the pair with the changed file doesn't.
        assert store.get_fingerprint(b.id, "intro") is not None
        assert store.get_detector_run(b.id, Source.SEASON_AUDIO) == "sig"
        _file(store, "/m/S01E02.mkv", size=5, mtime=5)
        assert store.get_detector_run(b.id, Source.SEASON_AUDIO) is None


class TestSeasonPairs:
    def test_round_trip_keeps_float_runs_exactly(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a), _fp(store, b)
        runs = [(0.12383900928792571, 24.891640866873065, 10.030959752321982, 34.79876160990712)]
        assert store.set_season_pair(a.id, b.id, 3, runs) is True
        assert store.get_season_pair(a.id, b.id, 3) == runs
        assert store.get_season_pair(b.id, a.id, 3) is None  # order matters: the matcher isn't symmetric
        assert store.get_season_pair(a.id, b.id, 4) is None  # another matcher version is recomputed

    def test_empty_run_list_is_a_stored_answer(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a), _fp(store, b)
        store.set_season_pair(a.id, b.id, 3, [])
        assert store.get_season_pair(a.id, b.id, 3) == []

    def test_refused_when_a_fingerprint_is_gone(self, store):
        a, b = _file(store, "/m/a.mkv"), _file(store, "/m/b.mkv")
        _fp(store, a)
        assert store.set_season_pair(a.id, b.id, 3, [(1.0, 2.0, 3.0, 4.0)]) is False
        assert store.get_season_pair(a.id, b.id, 3) is None


class TestDetectorRuns:
    def test_round_trip_and_replace(self, store):
        rec = _file(store)
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) is None
        store.set_detector_run(rec.id, Source.SEASON_AUDIO, "one")
        store.set_detector_run(rec.id, Source.SEASON_AUDIO, "two")
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) == "two"
        assert store.get_detector_run(rec.id, Source.CREDITS_TEXT) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_locks.py tests/markers/test_store_audio.py -q`
Expected: FAIL — `ModuleNotFoundError: media_preview_generator.markers.locks`, `ImportError: StoredFingerprint`.

- [ ] **Step 3: Implement `locks.py` and move the class out of the pipeline**

```python
# media_preview_generator/markers/locks.py
"""Per-key process locks (one app process serves everything: a single gunicorn worker)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from typing import Any


class KeyedLocks:
    """One lock per key, kept only while a caller holds or waits for it.

    Lock order used by Intro & Credits, never taken the other way round: a path's lock (the whole run on one file), then
    an item's lock (one server item's publish), then a file's fingerprint lock, then Plex's database lock (inside the
    publisher), then markers.db's own lock (every store call).
    """

    def __init__(self, lock_factory: Callable[[], Any] = threading.Lock) -> None:
        """Create the lock table.

        Args:
            lock_factory: Builds the lock for a new key (tests pass spies).
        """
        self._lock_factory = lock_factory
        self._guard = threading.Lock()
        self._locks: dict[Hashable, list] = {}  # key → [lock, callers holding or waiting]

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        """Hold ``key``'s lock for the duration of the block."""
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = self._locks[key] = [self._lock_factory(), 0]
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[key]
```

In `media_preview_generator/markers/pipeline.py`: delete the whole `class _KeyedLocks:` block (its lock-order docstring
now lives on `KeyedLocks`), add `from .locks import KeyedLocks as _KeyedLocks` to the local imports, and keep
`_PATH_LOCKS = _KeyedLocks()` / `_ITEM_LOCKS = _KeyedLocks()` unchanged. Drop `Hashable`, `Iterator` and
`contextmanager` from the pipeline imports if nothing else uses them (`ruff check` says).

- [ ] **Step 4: Implement the store additions**

Append to `_SCHEMA` (after the `server_kinds` table, before `source_usage`):

```python
    # Runs the v3 matcher found between two fingerprinted files. file_a is the matcher's first argument: the matcher
    # isn't symmetric, and the season step always pairs two files the same way round (Task 7).
    """CREATE TABLE IF NOT EXISTS season_pairs (
        file_a INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        file_b INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        matcher_version INTEGER NOT NULL,
        runs_json TEXT NOT NULL,
        PRIMARY KEY (file_a, file_b))""",
    "CREATE INDEX IF NOT EXISTS idx_season_pairs_b ON season_pairs(file_b)",
    # What a local detector's last answer for a file was based on (season audio: the season's fingerprinted files), so
    # it runs again only when that changes.
    """CREATE TABLE IF NOT EXISTS detector_runs (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        signature TEXT NOT NULL,
        run_at TEXT NOT NULL,
        PRIMARY KEY (file_id, source))""",
```

In `upsert_file`, the changed-identity branch: add `"detector_runs"` to the table tuple and, right after the loop,

```python
                    conn.execute("DELETE FROM season_pairs WHERE file_a=? OR file_b=?", (file_id, file_id))
```

and extend the method's docstring sentence "A changed identity clears evidence (and its versions), fingerprints, …" to
name season pairs and detector runs.

Add after `PublishStateRow`:

```python
@dataclass(frozen=True)
class StoredFingerprint:
    """A cached chromaprint fingerprint (raw little-endian uint32 points; empty for a file without audio)."""

    file_id: int
    window: str
    start_s: float
    length_s: float
    algorithm: int
    points: bytes
```

Add after `get_server_kind`:

```python
    def set_fingerprint(
        self,
        file_id: int,
        *,
        size: int,
        mtime_ns: int,
        window: str,
        start_s: float,
        length_s: float,
        algorithm: int,
        points: bytes,
    ) -> bool:
        """Store a fingerprint computed from the file with identity ``(size, mtime_ns)``.

        Returns:
            False (nothing stored) when the file's row has another identity now: the file was replaced while ffmpeg
            ran, and its own next run fingerprints the new file.
        """
        with self._tx() as conn:
            row = conn.execute("SELECT size, mtime_ns FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None or (row["size"], row["mtime_ns"]) != (size, mtime_ns):
                return False
            conn.execute(
                "INSERT OR REPLACE INTO fingerprints (file_id, window, start_s, length_s, algorithm, points) "
                "VALUES (?,?,?,?,?,?)",
                (file_id, window, start_s, length_s, algorithm, points),
            )
        return True

    def get_fingerprint(self, file_id: int, window: str) -> StoredFingerprint | None:
        """The cached fingerprint of a file, or None when it was never fingerprinted (or changed since)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM fingerprints WHERE file_id=? AND window=?", (file_id, window)
            ).fetchone()
        if r is None:
            return None
        return StoredFingerprint(
            r["file_id"], r["window"], r["start_s"], r["length_s"], r["algorithm"], bytes(r["points"])
        )

    def get_season_pair(
        self, file_a: int, file_b: int, matcher_version: int
    ) -> list[tuple[float, float, float, float]] | None:
        """Cached matcher runs between two files (``file_a`` was the matcher's first argument); None when not computed."""
        with self._lock:
            r = self._conn.execute(
                "SELECT runs_json FROM season_pairs WHERE file_a=? AND file_b=? AND matcher_version=?",
                (file_a, file_b, matcher_version),
            ).fetchone()
        return None if r is None else [tuple(run) for run in json.loads(r["runs_json"])]

    def set_season_pair(
        self, file_a: int, file_b: int, matcher_version: int, runs: list[tuple[float, float, float, float]]
    ) -> bool:
        """Cache matcher runs between two files.

        Returns:
            False (nothing stored) when either file's fingerprint is gone: one of them changed while matching.
        """
        with self._tx() as conn:
            have = conn.execute(
                "SELECT COUNT(*) FROM fingerprints WHERE file_id IN (?, ?) AND window='intro'", (file_a, file_b)
            ).fetchone()[0]
            if have != 2:
                return False
            conn.execute(
                "INSERT OR REPLACE INTO season_pairs (file_a, file_b, matcher_version, runs_json) VALUES (?,?,?,?)",
                (file_a, file_b, matcher_version, json.dumps([list(run) for run in runs])),
            )
        return True

    def get_detector_run(self, file_id: int, source: Source) -> str | None:
        """The signature a local detector's stored answer for a file was based on, or None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT signature FROM detector_runs WHERE file_id=? AND source=?", (file_id, source.value)
            ).fetchone()
        return r["signature"] if r else None

    def set_detector_run(self, file_id: int, source: Source, signature: str) -> None:
        """Record what a local detector's answer for a file was based on."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO detector_runs (file_id, source, signature, run_at) VALUES (?,?,?,?)",
                (file_id, source.value, signature, self._now()),
            )
```

`json.dumps` writes Python floats with `repr`, so `json.loads` gives back identical floats (the round-trip test pins it).

- [ ] **Step 5: Run the lock and store tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_locks.py tests/markers/test_store_audio.py tests/markers/test_store.py tests/markers/test_pipeline.py -q`
Expected: PASS (the pipeline tests prove the lock move changed nothing).

- [ ] **Step 6: Write the failing fingerprint tests**

```python
# tests/markers/audio/test_fingerprint.py
"""Chromaprint fingerprints: window, command, ffmpeg choice, caching, identity gate, cancel, ≤ 2 in parallel."""

from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S, fingerprint as fpmod
from media_preview_generator.markers.models import FileIdentity
from media_preview_generator.markers.store import MarkerStore


@pytest.fixture(autouse=True)
def _fresh_chromaprint_cache():
    fpmod.has_chromaprint.cache_clear()
    yield
    fpmod.has_chromaprint.cache_clear()


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _proc(stdout=b"", stderr=b"", returncode=0, hang=False):
    proc = MagicMock()
    proc.returncode = returncode
    calls = {"n": 0}

    def communicate(timeout=None):
        calls["n"] += 1
        if hang and proc.kill.call_count == 0:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return stdout, stderr

    proc.communicate.side_effect = communicate
    return proc


def test_point_duration_is_the_measured_value():
    assert POINT_S == pytest.approx(0.12384, abs=1e-5)


@pytest.mark.parametrize(
    ("duration_ms", "expected"), [(1_321_472, 462.5152), (2_400_000, 840.0), (3_000_000, 900.0), (120_000, 42.0)]
)
def test_window_is_35_percent_capped_at_900_s(duration_ms, expected):
    assert fpmod.window_s(duration_ms) == pytest.approx(expected)


def test_command_is_the_spec_command_with_thread_cap():
    assert fpmod.fingerprint_command("/usr/lib/jellyfin-ffmpeg/ffmpeg", "/m/a.mkv", 462.5152) == [
        "/usr/lib/jellyfin-ffmpeg/ffmpeg", "-nostdin", "-v", "error", "-threads", "2",
        "-ss", "0", "-t", "462.515", "-i", "/m/a.mkv",
        "-vn", "-sn", "-dn", "-ac", "2", "-f", "chromaprint", "-algorithm", "1", "-fp_format", "raw", "-",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("stdout", "returncode", "found"),
    [
        (" D  webm_chunk  WebM Chunk Muxer\n E  chromaprint     Chromaprint\n", 0, True),
        (" E  mp4             MP4 (MPEG-4 Part 14)\n", 0, False),
        (" E  chromaprint     Chromaprint\n", 1, False),
    ],
)
def test_has_chromaprint_reads_the_muxer_list(stdout, returncode, found):
    with patch.object(fpmod.subprocess, "run", return_value=MagicMock(stdout=stdout, returncode=returncode)) as run:
        assert fpmod.has_chromaprint("/x/ffmpeg") is found
    assert run.call_args.args[0] == ["/x/ffmpeg", "-hide_banner", "-muxers"]


def test_chromaprint_ffmpeg_prefers_configured_then_jellyfin_then_path(tmp_path):
    configured, jellyfin, on_path = (tmp_path / n for n in ("conf", "jf", "path"))
    for p in (configured, jellyfin, on_path):
        p.write_text("")
        p.chmod(0o755)
    with (
        patch.object(fpmod, "JELLYFIN_FFMPEG", str(jellyfin)),
        patch.object(fpmod.shutil, "which", return_value=str(on_path)),
        patch.object(fpmod, "has_chromaprint", side_effect=lambda f: f != str(configured)),
    ):
        assert fpmod.chromaprint_ffmpeg(str(configured)) == str(jellyfin)
    with (
        patch.object(fpmod, "JELLYFIN_FFMPEG", str(tmp_path / "missing")),
        patch.object(fpmod.shutil, "which", return_value=str(on_path)),
        patch.object(fpmod, "has_chromaprint", return_value=False),
    ):
        assert fpmod.chromaprint_ffmpeg(None) is None
        found, reason = fpmod.chromaprint_status(None)
    assert found is None and "chromaprint" in reason


def test_compute_returns_the_raw_points():
    raw = np.array([1, 2, 0xFFFFFFFF], dtype="<u4").tobytes() + b"\x07"  # a torn last point is dropped
    with patch.object(fpmod.subprocess, "Popen", return_value=_proc(stdout=raw)) as popen:
        points = fpmod.compute_fingerprint("/m/a.mkv", 1_321_472, ffmpeg="ffmpeg")
    assert points.tolist() == [1, 2, 0xFFFFFFFF] and points.dtype == np.dtype("<u4")
    assert popen.call_args.args[0] == fpmod.fingerprint_command("ffmpeg", "/m/a.mkv", fpmod.window_s(1_321_472))


def test_file_without_audio_gives_an_empty_fingerprint():
    err = b"Output file #0 does not contain any stream\n"
    with patch.object(fpmod.subprocess, "Popen", return_value=_proc(stderr=err, returncode=1)):
        assert fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg").size == 0


def test_other_ffmpeg_errors_raise():
    with patch.object(fpmod.subprocess, "Popen", return_value=_proc(stderr=b"Invalid data found", returncode=1)):
        with pytest.raises(fpmod.FingerprintError, match="exited 1"):
            fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg")


def test_cancel_kills_ffmpeg():
    proc = _proc(hang=True)
    with patch.object(fpmod.subprocess, "Popen", return_value=proc):
        with pytest.raises(fpmod.FingerprintError, match="cancelled"):
            fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg", cancel_check=lambda: True)
    proc.kill.assert_called_once()


def _record(store, tmp_path, name="S01E01.mkv"):
    path = tmp_path / name
    path.write_bytes(b"x" * 10)
    st = path.stat()
    return store.upsert_file(
        FileIdentity(str(path), st.st_size, st.st_mtime_ns), duration_ms=300_000, season_key=str(tmp_path), is_movie=False
    )


def test_ensure_computes_once_then_reads_the_cache(store, tmp_path):
    rec = _record(store, tmp_path)
    with patch.object(fpmod, "compute_fingerprint", return_value=np.array([5, 6], dtype="<u4")) as compute:
        first = fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg")
        second = fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg")
    assert first.tolist() == second.tolist() == [5, 6]
    compute.assert_called_once_with(rec.canonical_path, 300_000, ffmpeg="ffmpeg", cancel_check=None)
    stored = store.get_fingerprint(rec.id, "intro")
    assert (stored.start_s, stored.length_s, stored.algorithm) == (0.0, 105.0, 1)


def test_ensure_stores_nothing_for_a_file_replaced_meanwhile(store, tmp_path):
    rec = _record(store, tmp_path)
    store.upsert_file(FileIdentity(rec.canonical_path, 999, 1), duration_ms=300_000, season_key="x", is_movie=False)
    with patch.object(fpmod, "compute_fingerprint", return_value=np.array([5], dtype="<u4")):
        assert fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg") is None
    assert store.get_fingerprint(rec.id, "intro") is None


def test_at_most_two_fingerprints_run_at_once(store, tmp_path):
    recs = [_record(store, tmp_path, f"S01E0{i}.mkv") for i in range(1, 6)]
    running, peak, guard = [0], [0], threading.Lock()

    def slow(*_a, **_kw):
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with guard:
            running[0] -= 1
        return np.array([1], dtype="<u4")

    with patch.object(fpmod, "compute_fingerprint", side_effect=slow):
        threads = [threading.Thread(target=fpmod.ensure_fingerprint, args=(store, r), kwargs={"ffmpeg": "f"}) for r in recs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert peak[0] == 2
```

- [ ] **Step 7: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/audio/test_fingerprint.py -q`
Expected: FAIL — `ModuleNotFoundError: media_preview_generator.markers.audio`.

- [ ] **Step 8: Implement the audio package and fingerprints**

```python
# media_preview_generator/markers/audio/__init__.py
"""Season audio matching for TV intros (spec §5.3)."""

# Seconds per chromaprint point: a 1365-sample hop at 11025 Hz (measured 0.1238 s, spec §5.3).
POINT_S = 4096 / 11025 / 3
```

(Task 2 creates the identical file; the cherry-pick of an identical add merges cleanly.)

```python
# media_preview_generator/markers/audio/fingerprint.py
"""Chromaprint fingerprints of an episode's opening (spec §5.3), cached per file identity in markers.db.

CPU only (there is no GPU chromaprint), ``-threads 2`` and at most two at a time across the whole app (spec §5.6).
Only jellyfin-ffmpeg carries the chromaprint muxer in the image; the arm64 image has none, so season audio is then
unavailable rather than failing every episode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from functools import lru_cache

import numpy as np
from loguru import logger

from ..locks import KeyedLocks
from ..store import FileRecord, MarkerStore, StoredFingerprint

WINDOW = "intro"
ALGORITHM = 1
MAX_WINDOW_S = 900.0
WINDOW_FRACTION = 0.35
FFMPEG_THREADS = 2
MAX_PARALLEL = 2
JELLYFIN_FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
_POLL_S = 0.5
# ffmpeg's wording when a file has no audio stream to fingerprint: a stored empty answer, not a retryable failure.
_NO_AUDIO_HINTS = ("does not contain any stream", "matches no streams", "Output file is empty")
_PARALLEL = threading.BoundedSemaphore(MAX_PARALLEL)
_FILE_LOCKS = KeyedLocks()


class FingerprintError(Exception):
    """ffmpeg couldn't fingerprint the file. Nothing is stored, so the next run tries again."""


def window_s(duration_ms: int) -> float:
    """Seconds fingerprinted from the start: 35% of the file, at most 900 s (spec §5.3)."""
    return min(MAX_WINDOW_S, WINDOW_FRACTION * duration_ms / 1000.0)


@lru_cache(maxsize=8)
def has_chromaprint(ffmpeg: str) -> bool:
    """Whether an ffmpeg binary has the chromaprint muxer (asked once per binary per process)."""
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-muxers"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and any(line.split()[1:2] == ["chromaprint"] for line in proc.stdout.splitlines())


def chromaprint_ffmpeg(configured: str | None) -> str | None:
    """The first ffmpeg with chromaprint: the configured one, jellyfin-ffmpeg, then ``ffmpeg`` on PATH."""
    for candidate in dict.fromkeys(c for c in (configured, JELLYFIN_FFMPEG, shutil.which("ffmpeg")) if c):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK) and has_chromaprint(candidate):
            return candidate
    return None


def chromaprint_status(configured: str | None) -> tuple[str | None, str]:
    """The ffmpeg season audio uses and a sentence for Settings.

    Returns:
        ``(path, "")`` when found; ``(None, reason)`` otherwise.
    """
    found = chromaprint_ffmpeg(configured)
    if found:
        return found, ""
    return None, "Needs an ffmpeg with the chromaprint muxer (jellyfin-ffmpeg in the amd64 image); none was found"


def fingerprint_command(ffmpeg: str, path: str, length_s: float) -> list[str]:
    """The spec §5.3 command: raw algorithm-1 chromaprint of the first ``length_s`` seconds, stereo."""
    return [
        ffmpeg, "-nostdin", "-v", "error", "-threads", str(FFMPEG_THREADS),
        "-ss", "0", "-t", f"{length_s:.3f}", "-i", path,
        "-vn", "-sn", "-dn", "-ac", "2", "-f", "chromaprint", "-algorithm", str(ALGORITHM), "-fp_format", "raw", "-",
    ]  # fmt: skip


def compute_fingerprint(
    path: str,
    duration_ms: int,
    *,
    ffmpeg: str,
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = 300.0,
) -> np.ndarray:
    """Run ffmpeg and return the fingerprint points.

    Args:
        path: Media file (read only).
        duration_ms: File duration, for the window.
        ffmpeg: An ffmpeg with chromaprint.
        cancel_check: True once the job is cancelled; ffmpeg is killed.
        timeout_s: Hard limit (a hung network mount must not hold a worker).

    Returns:
        uint32 points (little-endian); empty for a file without an audio stream.

    Raises:
        FingerprintError: ffmpeg failed, timed out or the job was cancelled.
    """
    name = os.path.basename(path)
    command = fingerprint_command(ffmpeg, path, window_s(duration_ms))
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            out, err = proc.communicate(timeout=_POLL_S)
            break
        except subprocess.TimeoutExpired:
            cancelled = bool(cancel_check and cancel_check())
            if cancelled or time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                why = "cancelled" if cancelled else f"timed out after {timeout_s:.0f} s"
                raise FingerprintError(f"Fingerprinting {name} {why}") from None
    if proc.returncode != 0:
        text = (err or b"").decode("utf-8", errors="replace")
        if any(hint in text for hint in _NO_AUDIO_HINTS):
            return np.zeros(0, dtype="<u4")
        raise FingerprintError(f"ffmpeg exited {proc.returncode} fingerprinting {name}: {text.strip()[-200:]}")
    usable = len(out) - len(out) % 4
    return np.frombuffer(out[:usable], dtype="<u4").copy()


def points_of(stored: StoredFingerprint) -> np.ndarray:
    """The points of a cached fingerprint."""
    return np.frombuffer(stored.points, dtype="<u4").copy()


def ensure_fingerprint(
    store: MarkerStore, rec: FileRecord, *, ffmpeg: str, cancel_check: Callable[[], bool] | None = None
) -> np.ndarray | None:
    """A file's fingerprint from the cache, computing and storing it when missing.

    Two callers asking for one file share one ffmpeg run; at most ``MAX_PARALLEL`` run app-wide.

    Args:
        store: The markers store.
        rec: The file's record; its identity must match the file on disk (callers check).
        ffmpeg: An ffmpeg with chromaprint.
        cancel_check: True once the job is cancelled.

    Returns:
        The points, or None when the file's row changed identity while ffmpeg ran (nothing stored).

    Raises:
        FingerprintError: The file has no known duration, or ffmpeg failed.
    """
    with _FILE_LOCKS.hold(rec.id):
        stored = store.get_fingerprint(rec.id, WINDOW)
        if stored is not None:
            return points_of(stored)
        if not rec.duration_ms:
            raise FingerprintError(f"No known duration for {os.path.basename(rec.canonical_path)}")
        while not _PARALLEL.acquire(timeout=_POLL_S):
            if cancel_check and cancel_check():
                raise FingerprintError(f"Fingerprinting {os.path.basename(rec.canonical_path)} cancelled")
        try:
            points = compute_fingerprint(rec.canonical_path, rec.duration_ms, ffmpeg=ffmpeg, cancel_check=cancel_check)
        finally:
            _PARALLEL.release()
        saved = store.set_fingerprint(
            rec.id,
            size=rec.size,
            mtime_ns=rec.mtime_ns,
            window=WINDOW,
            start_s=0.0,
            length_s=window_s(rec.duration_ms),
            algorithm=ALGORITHM,
            points=points.tobytes(),
        )
        if not saved:
            logger.debug("{} changed while it was fingerprinted; not cached", os.path.basename(rec.canonical_path))
            return None
        return points
```

`test_ensure_computes_once_then_reads_the_cache` asserts `compute_fingerprint` is called with `cancel_check=None`
as a keyword — keep the call exactly as written.

In `pyproject.toml` `dependencies`, after `"pillow>=12.2.0,<14",` add:

```toml
    # numpy vectorises the season intro matcher (markers/audio/matcher.py); imported only by Intro & Credits.
    "numpy>=2.0,<3",
```

- [ ] **Step 9: Run the task's tests and the markers suite**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers -q`
Expected: PASS.

- [ ] **Step 10: Real-ffmpeg smoke (integration, local only)**

```python
# tests/markers/audio/test_fingerprint_integration.py
"""Real ffmpeg: a generated 60 s stereo tone file fingerprints to ~window/POINT_S points."""

import shutil
import subprocess

import pytest

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio.fingerprint import chromaprint_ffmpeg, compute_fingerprint, window_s

pytestmark = pytest.mark.integration


def test_real_ffmpeg_fingerprint_length(tmp_path):
    ffmpeg = chromaprint_ffmpeg(shutil.which("ffmpeg"))
    if ffmpeg is None:
        pytest.skip("no ffmpeg with chromaprint")
    media = tmp_path / "tone.mka"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anoisesrc=color=pink:seed=7:d=60", "-ac", "2", str(media)],
        check=True,
    )
    points = compute_fingerprint(str(media), 60_000, ffmpeg=ffmpeg)
    assert abs(len(points) - window_s(60_000) / POINT_S) < 20
```

Run: `nice -n 19 /home/data/.venv/bin/python -m pytest --no-cov -n 0 -m integration tests/markers/audio/test_fingerprint_integration.py -q`
Expected: PASS on storage (`/usr/bin/ffmpeg` has chromaprint).

- [ ] **Step 11: Commit**

Stage the files above; Architecture Review on the staged diff; then
`PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): fingerprint cache, season pair and detector-run tables"`.

---
## Task 2: v3 season matcher (numpy port) + fp3 reference

`[lane-parallel]` `[high-risk]` — spec §5.3 "Matcher (v3)", roadmap phase 2 bullet 2 ("must reproduce
`evidence/eval/eval_results_v3.json` segment-for-segment on cached fingerprints before use").

The measured algorithm is `docs/design/intro-credits/evidence/detect/fp3.py` (`runs` + `analyse`, intro window). It is
pure Python and not symmetric: the top-40 shifts are ranked by count with ties in first-seen order while scanning `a`,
overlapping runs are de-duplicated on side `a`, and each file's candidate list is built in pair-loop order before the
strict "greater key wins" pick. The port must keep every one of those orders, or segments differ on real seasons
where counts tie. This task commits a verbatim reference (`tools/markers_eval/fp3_reference.py`) and a vectorised port,
and proves them identical on synthetic seasons (including repeated-value "silence" blocks that create ties). Task 6
proves them identical on the 118 real episodes.

**Files:**
- Create: `media_preview_generator/markers/audio/__init__.py` (identical to Task 1's), `media_preview_generator/markers/audio/matcher.py`,
  `tools/__init__.py` (empty), `tools/markers_eval/__init__.py`, `tools/markers_eval/fp3_reference.py`
- Test: `tests/markers/audio/__init__.py` (empty; identical to Task 1's), `tests/markers/audio/test_matcher.py`

**Interfaces:**
- Consumes: `POINT_S` from `markers/audio/__init__.py`.
- Produces (Tasks 6, 7, 15):
```python
# markers/audio/matcher.py
MATCHER_VERSION = 3
MAX_BIT_DIFF = 6; MAX_GAP_S = 3.5; MIN_RUN_S = 8.0; MAX_INTRO_S = 120.0; PREFERRED_MIN_S = 15.0
CLUSTER_TOLERANCE_S = 4.0; QUORUM = 0.5; TOP_SHIFTS = 40
class Run(NamedTuple): a_start_s: float; a_end_s: float; b_start_s: float; b_end_s: float
class IntroSegment(NamedTuple): start_s: float; end_s: float; support: int
Hit = tuple[float, float, str]                   # (start_s, end_s, partner key)
def pair_runs(a: np.ndarray, b: np.ndarray) -> list[Run]
def file_hits(target: str, files: Sequence[str], runs_between: Callable[[str, str], list[Run]]) -> list[Hit]
def intro_for(hits: Sequence[Hit], others: int) -> IntroSegment | None
def season_intros(points: Mapping[str, np.ndarray]) -> dict[str, IntroSegment | None]

# tools/markers_eval/fp3_reference.py
def runs(a: np.ndarray, b: np.ndarray, min_pts: int) -> list[tuple]
def analyse_points(fps: dict[str, np.ndarray], files: list[str], max_len: float = 120) -> dict[str, dict]
```
`files` is always sorted by key (canonical path); `runs_between(x, y)` is only ever asked with `x` before `y`.

- [ ] **Step 1: Commit the reference as data (no test yet)**

```python
# tools/markers_eval/fp3_reference.py
"""Reference copy of evidence/detect/fp3.py's v3 intro matcher (spec §5.3), taking fingerprints instead of files.

Pure Python and slow on purpose: media_preview_generator/markers/audio/matcher.py must return exactly what this
returns on the same fingerprints (tests/markers/audio/test_matcher.py, and the Task 6 harness gate on 118 real
episodes). Don't tidy or speed this file up: it is the measured algorithm.
"""

import numpy as np

POINT_S = 4096 / 11025 / 3
MAX_BIT_DIFF = 6
MAX_GAP_S = 3.5
MIN_S = 8
QUORUM = 0.5
_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def popcount32(x):
    b = x.view(np.uint8).reshape(-1, 4)
    return _POP[b].sum(axis=1)


def runs(a, b, min_pts):
    index = {}
    for i, v in enumerate(b.tolist()):
        index.setdefault(v, []).append(i)
    shifts = {}
    for i, v in enumerate(a.tolist()):
        for d in (-2, -1, 0, 1, 2):
            for j in index.get(v + d, ()):
                shifts[j - i] = shifts.get(j - i, 0) + 1
    gap = int(MAX_GAP_S / POINT_S)
    out = []
    for s, c in sorted(shifts.items(), key=lambda kv: -kv[1])[:40]:
        a0, b0 = (0, s) if s >= 0 else (-s, 0)
        n = min(len(a) - a0, len(b) - b0)
        if n <= 0:
            continue
        ok = popcount32(a[a0 : a0 + n] ^ b[b0 : b0 + n]) <= MAX_BIT_DIFF
        idx = np.flatnonzero(ok)
        if len(idx) < 2:
            continue
        br = np.flatnonzero(np.diff(idx) > gap)
        st = np.concatenate(([idx[0]], idx[br + 1]))
        en = np.concatenate((idx[br], [idx[-1]]))
        for x, y in zip(st, en):
            if y - x >= min_pts:
                out.append(((a0 + x) * POINT_S, (a0 + y) * POINT_S, (b0 + x) * POINT_S, (b0 + y) * POINT_S))
    out.sort(key=lambda r: -(r[1] - r[0]))
    keep = []
    for r in out:
        if all(not (r[0] < k[1] and k[0] < r[1]) for k in keep):
            keep.append(r)
    return keep


def analyse_points(fps, files, max_len=120):
    hits = {f: [] for f in files}
    min_pts = int(MIN_S / POINT_S)
    for i, fa in enumerate(files):
        for fb in files[i + 1 :]:
            for a0, a1, b0, b1 in runs(fps[fa], fps[fb], min_pts):
                if a1 - a0 > max_len:
                    continue
                hits[fa].append((a0, a1, fb))
                hits[fb].append((b0, b1, fa))
    out = {}
    others = len(files) - 1
    for f in files:
        best = None
        for s, e, _ in hits[f]:
            sup = {p for s2, e2, p in hits[f] if abs(s2 - s) <= 4 and abs(e2 - e) <= 4}
            key = ((e - s) >= 15, len(sup), e - s)
            if best is None or key > best_key:  # noqa: F821 - set on the first pass, as in fp3.py
                best_key = key
                grp = [(s2, e2) for s2, e2, p in hits[f] if abs(s2 - s) <= 4 and abs(e2 - e) <= 4]
                best = (float(np.median([g[0] for g in grp])), float(np.median([g[1] for g in grp])), len(sup))
        if best and best[2] < max(1, QUORUM * others):
            best = None
        out[f] = {"pairs": len(hits[f]), "segment": best}
    return out
```

`tools/__init__.py` is empty. `tools/markers_eval/__init__.py`:

```python
"""Intro & Credits accuracy harness (spec §10.2). Runs by hand on storage, never in CI."""
```

Compare `runs`/`analyse_points` line by line against `evidence/detect/fp3.py` `runs`/`analyse` (intro window: `oa`/`ob`
are 0) before moving on; the only allowed differences are the fingerprint source and the dropped `duration` key.

- [ ] **Step 2: Write the failing matcher tests**

```python
# tests/markers/audio/test_matcher.py
"""v3 matcher port == fp3 reference, run for run and segment for segment; planted intros are found."""

from __future__ import annotations

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio import matcher as m
from tools.markers_eval import fp3_reference as ref

MIN_PTS = int(ref.MIN_S / ref.POINT_S)


def _key(e: int) -> str:
    return f"/tv/Show (2020)/Season 01/Show (2020) - S01E{e:02d}.mkv"


def _season(seed: int, *, episodes: int = 4, length: int = 900, intro_len: int = 200, silence: int = 40):
    """Random fingerprints sharing one intro (with single-bit noise) and one repeated-value block (ties)."""
    rng = np.random.default_rng(seed)
    intro = rng.integers(0, 2**32, size=intro_len, dtype=np.uint64).astype("<u4")
    silence_value = np.uint32(rng.integers(0, 2**32, dtype=np.uint64))
    out, offsets = {}, {}
    for e in range(1, episodes + 1):
        body = rng.integers(0, 2**32, size=length, dtype=np.uint64).astype("<u4")
        at = int(rng.integers(0, length - intro_len))
        noisy = intro.copy()
        flip = rng.random(intro_len) < 0.3
        noisy[flip] ^= np.uint32(1) << rng.integers(0, 32, size=int(flip.sum())).astype(np.uint32)
        body[at : at + intro_len] = noisy
        if silence:
            s_at = int(rng.integers(0, length - silence))
            body[s_at : s_at + silence] = silence_value
        out[_key(e)] = body
        offsets[_key(e)] = at
    return out, offsets


@pytest.mark.parametrize("seed", range(12))
def test_pair_runs_equal_the_reference(seed):
    fps, _ = _season(seed, episodes=2)
    a, b = fps[_key(1)], fps[_key(2)]
    assert [tuple(r) for r in m.pair_runs(a, b)] == ref.runs(a, b, MIN_PTS)
    assert [tuple(r) for r in m.pair_runs(b, a)] == ref.runs(b, a, MIN_PTS)


@pytest.mark.parametrize(("seed", "episodes"), [(s, n) for s in range(6) for n in (2, 3, 5)])
def test_season_intros_equal_the_reference(seed, episodes):
    fps, _ = _season(100 + seed, episodes=episodes)
    files = sorted(fps)
    expected = {f: row["segment"] for f, row in ref.analyse_points(fps, files).items()}
    got = {f: (tuple(seg) if seg else None) for f, seg in m.season_intros(fps).items()}
    assert got == expected


def test_constant_fingerprints_rank_tied_shifts_like_the_reference():
    # Every shift s and -s ties on count; the reference keeps the one first seen while scanning a.
    a = np.full(300, 12345, dtype="<u4")
    b = np.full(300, 12345, dtype="<u4")
    assert [tuple(r) for r in m.pair_runs(a, b)] == ref.runs(a, b, MIN_PTS)


def test_planted_intro_is_found_where_it_was_planted():
    fps, offsets = _season(7, episodes=3, silence=0)
    result = m.season_intros(fps)
    for key, seg in result.items():
        assert seg is not None
        assert abs(seg.start_s - offsets[key] * POINT_S) <= 4 * POINT_S
        assert abs(seg.end_s - (offsets[key] + 199) * POINT_S) <= 4 * POINT_S
        assert seg.support == 2


def test_one_other_episode_is_enough():
    fps, _ = _season(8, episodes=2, silence=0)
    assert all(seg is not None and seg.support == 1 for seg in m.season_intros(fps).values())


def test_runs_longer_than_120_s_are_not_intros():
    fps, _ = _season(9, episodes=3, intro_len=int(121 / POINT_S) + 2, length=1400, silence=0)
    assert all(seg is None for seg in m.season_intros(fps).values())


def test_runs_shorter_than_8_s_are_ignored():
    fps, _ = _season(10, episodes=2, intro_len=int(8 / POINT_S) - 2, silence=0)
    assert m.pair_runs(fps[_key(1)], fps[_key(2)]) == []


def test_quorum_needs_half_of_the_other_episodes():
    fps, _ = _season(11, episodes=2, silence=0)
    rng = np.random.default_rng(99)
    for e in (3, 4, 5):  # three unrelated episodes: 1 supporter of 4 others < 50%
        fps[_key(e)] = rng.integers(0, 2**32, size=900, dtype=np.uint64).astype("<u4")
    result = m.season_intros(fps)
    assert result[_key(1)] is None and result[_key(2)] is None


def test_empty_fingerprint_matches_nothing():
    a = np.zeros(0, dtype="<u4")
    b = np.arange(500, dtype="<u4")
    assert m.pair_runs(a, b) == [] and m.pair_runs(b, a) == []


def test_file_hits_order_is_the_pair_loop_order():
    calls = []

    def runs_between(x, y):
        calls.append((x, y))
        return [m.Run(1.0, 20.0, 2.0, 21.0)] if (x, y) != ("b", "c") else []

    hits = m.file_hits("b", ["a", "b", "c", "d"], runs_between)
    assert hits == [(2.0, 21.0, "a"), (1.0, 20.0, "d")]
    assert calls == [("a", "b"), ("b", "c"), ("b", "d")]


def test_runs_over_120_s_are_dropped_from_hits():
    hits = m.file_hits("a", ["a", "b"], lambda x, y: [m.Run(0.0, 121.0, 0.0, 121.0), m.Run(0.0, 30.0, 5.0, 35.0)])
    assert hits == [(0.0, 30.0, "b")]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/audio/test_matcher.py -q`
Expected: FAIL — `ImportError: cannot import name 'matcher'`.

- [ ] **Step 4: Implement the port**

```python
# media_preview_generator/markers/audio/matcher.py
"""Season intro matcher v3 (spec §5.3): a vectorised port of evidence/detect/fp3.py.

For every pair of episodes: the 40 best alignment shifts (inverted index, values within ±2), runs of points whose
fingerprints differ in ≤ 6 bits with gaps ≤ 3.5 s, 8–120 s long, all non-overlapping runs kept. Per episode: cluster
its runs within ±4 s, rank by (≥ 15 s, supporting episodes, length), and require support from half of the other
episodes (at least one). Orders that break ties are the reference's (tools/markers_eval/fp3_reference.py); the tests
compare the two run for run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

import numpy as np

from . import POINT_S

MATCHER_VERSION = 3
MAX_BIT_DIFF = 6
MAX_GAP_S = 3.5
MIN_RUN_S = 8.0
MAX_INTRO_S = 120.0
PREFERRED_MIN_S = 15.0
CLUSTER_TOLERANCE_S = 4.0
QUORUM = 0.5
TOP_SHIFTS = 40
_VALUE_SHIFTS = (-2, -1, 0, 1, 2)
_MIN_PTS = int(MIN_RUN_S / POINT_S)
_GAP_PTS = int(MAX_GAP_S / POINT_S)
_POPCOUNT8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

Hit = tuple[float, float, str]


class Run(NamedTuple):
    """Matching stretch between episode a and episode b (seconds from each file's start)."""

    a_start_s: float
    a_end_s: float
    b_start_s: float
    b_end_s: float


class IntroSegment(NamedTuple):
    """An episode's intro and how many other episodes support it."""

    start_s: float
    end_s: float
    support: int


def _popcount32(x: np.ndarray) -> np.ndarray:
    return _POPCOUNT8[x.view(np.uint8).reshape(-1, 4)].sum(axis=1)


def _top_shifts(a: np.ndarray, b: np.ndarray) -> list[int]:
    """The best alignment shifts (j - i), most matching values first; ties keep the order first seen scanning ``a``.

    The reference counts shifts in a dict while looping i over ``a``, d over -2..2 and j over ``b``'s positions of
    that value, then sorts stably by count: the key below is that loop's position, so the first sighting of each
    shift is its minimum key.
    """
    if len(a) == 0 or len(b) == 0:
        return []
    order = np.argsort(b, kind="stable")
    sorted_b = b[order].astype(np.int64)
    a64 = a.astype(np.int64)
    width = len(b) + 1
    shift_parts: list[np.ndarray] = []
    key_parts: list[np.ndarray] = []
    for d_index, d in enumerate(_VALUE_SHIFTS):
        wanted = a64 + d
        left = np.searchsorted(sorted_b, wanted, side="left")
        counts = np.searchsorted(sorted_b, wanted, side="right") - left
        total = int(counts.sum())
        if total == 0:
            continue
        i = np.repeat(np.arange(len(a), dtype=np.int64), counts)
        within = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(counts) - counts, counts)
        j = order[np.repeat(left, counts) + within].astype(np.int64)
        shift_parts.append(j - i)
        key_parts.append((i * len(_VALUE_SHIFTS) + d_index) * width + j)
    if not shift_parts:
        return []
    shifts = np.concatenate(shift_parts)
    keys = np.concatenate(key_parts)
    unique, inverse, counts = np.unique(shifts, return_inverse=True, return_counts=True)
    grouped = np.lexsort((keys, inverse.ravel()))
    group_starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    first_seen = keys[grouped[group_starts]]
    ranked = np.lexsort((first_seen, -counts))
    return [int(unique[k]) for k in ranked[:TOP_SHIFTS]]


def pair_runs(a: np.ndarray, b: np.ndarray) -> list[Run]:
    """Every non-overlapping matching run between two fingerprints, longest first on side ``a``.

    Args:
        a: The first episode's points (its path sorts first).
        b: The second episode's points.

    Returns:
        Runs of at least 8 s; overlaps on side ``a`` resolved in favour of the longer run.
    """
    a = np.ascontiguousarray(a, dtype="<u4")
    b = np.ascontiguousarray(b, dtype="<u4")
    found: list[Run] = []
    for s in _top_shifts(a, b):
        a0, b0 = (0, s) if s >= 0 else (-s, 0)
        n = min(len(a) - a0, len(b) - b0)
        if n <= 0:
            continue
        idx = np.flatnonzero(_popcount32(a[a0 : a0 + n] ^ b[b0 : b0 + n]) <= MAX_BIT_DIFF)
        if len(idx) < 2:
            continue
        breaks = np.flatnonzero(np.diff(idx) > _GAP_PTS)
        starts = np.concatenate(([idx[0]], idx[breaks + 1]))
        ends = np.concatenate((idx[breaks], [idx[-1]]))
        for x, y in zip(starts.tolist(), ends.tolist(), strict=True):
            if y - x >= _MIN_PTS:
                found.append(Run((a0 + x) * POINT_S, (a0 + y) * POINT_S, (b0 + x) * POINT_S, (b0 + y) * POINT_S))
    found.sort(key=lambda r: -(r.a_end_s - r.a_start_s))
    kept: list[Run] = []
    for run in found:
        if all(not (run.a_start_s < k.a_end_s and k.a_start_s < run.a_end_s) for k in kept):
            kept.append(run)
    return kept


def file_hits(target: str, files: Sequence[str], runs_between: Callable[[str, str], list[Run]]) -> list[Hit]:
    """One episode's intro-length runs against every other episode, in the reference's pair-loop order.

    Args:
        target: The episode (one of ``files``).
        files: The group, sorted.
        runs_between: Runs for ``(earlier, later)``; asked only in that order.

    Returns:
        ``(start_s, end_s, partner)`` on the target's side, runs over 120 s left out.
    """
    k = list(files).index(target)
    hits: list[Hit] = []
    for earlier in files[:k]:
        hits.extend((r.b_start_s, r.b_end_s, earlier) for r in runs_between(earlier, target) if not _too_long(r))
    for later in files[k + 1 :]:
        hits.extend((r.a_start_s, r.a_end_s, later) for r in runs_between(target, later) if not _too_long(r))
    return hits


def _too_long(run: Run) -> bool:
    return run.a_end_s - run.a_start_s > MAX_INTRO_S


def intro_for(hits: Sequence[Hit], others: int) -> IntroSegment | None:
    """The best supported intro among one episode's hits.

    Args:
        hits: From :func:`file_hits`.
        others: How many other episodes were compared.

    Returns:
        Median start/end of the winning cluster and its support, or None below the quorum.
    """
    tol = CLUSTER_TOLERANCE_S
    best: IntroSegment | None = None
    best_key: tuple | None = None
    for s, e, _ in hits:
        cluster = [(s2, e2, p) for s2, e2, p in hits if abs(s2 - s) <= tol and abs(e2 - e) <= tol]
        support = len({p for _, _, p in cluster})
        key = ((e - s) >= PREFERRED_MIN_S, support, e - s)
        if best_key is None or key > best_key:
            best_key = key
            best = IntroSegment(
                float(np.median([c[0] for c in cluster])), float(np.median([c[1] for c in cluster])), support
            )
    if best is not None and best.support < max(1, QUORUM * others):
        return None
    return best


def season_intros(points: Mapping[str, np.ndarray]) -> dict[str, IntroSegment | None]:
    """Intros for a whole group at once (the harness; the pipeline decides one episode at a time).

    Args:
        points: Fingerprint per episode key.

    Returns:
        Intro (or None) per key.
    """
    files = sorted(points)
    cache: dict[tuple[str, str], list[Run]] = {}

    def runs_between(x: str, y: str) -> list[Run]:
        if (x, y) not in cache:
            cache[(x, y)] = pair_runs(points[x], points[y])
        return cache[(x, y)]

    return {f: intro_for(file_hits(f, files, runs_between), len(files) - 1) for f in files}
```

- [ ] **Step 5: Run the tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/audio/test_matcher.py -q`
Expected: PASS. If `test_pair_runs_equal_the_reference` fails on a seed, print both run lists and the two shift
rankings (reference: `sorted(shifts.items(), key=lambda kv: -kv[1])[:40]`) before touching the port —
`superpowers:systematic-debugging` first; the reference is never the thing that changes.

- [ ] **Step 6: Mutation self-check (high-risk)**

Flip each of these one at a time, run the file, confirm at least one test fails, revert: `kind="stable"` → default
argsort; `np.lexsort((first_seen, -counts))` → `np.argsort(-counts)`; `> MAX_INTRO_S` → `>=`; `key > best_key` → `>=`;
`best.support < max(1, QUORUM * others)` → `<=`; the de-duplication overlap test `<` → `<=`. A mutant no test catches
gets a new reference-comparison case (a crafted fingerprint pair where that order matters) before the commit, unless
the reviewer agrees it is equivalent. Record the results in the task report.

- [ ] **Step 7: Commit**

`PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): v3 season intro matcher with fp3 reference"` (after the Architecture Review).

---
## Task 3: Pipeline plumbing for local detectors

`[lane-parallel]` `[high-risk]` — spec §6.2 steps 3–4, §6.4 items 3–5; ledger parked items "forced check run returns at
the first pending local detector and skips later sources' refresh" (progress.md line 204) and "local detectors re-run
while undecided" (line 165).

Phase 1 wired `LocalDetectorSpec` but registered no detector. Before season audio plugs in, the pipeline needs:
1. **Stored answers with a version**, so a detector isn't re-run on every run while its type stays undecided.
2. **A `due` hook**, so a detector can say its answer is stale for reasons beyond its version (season audio: the
   season's fingerprinted files changed).
3. **A `needs_worker` hook**, so a detector whose inputs are cached (matching cached fingerprints) runs on the checking
   thread instead of taking a GPU/CPU worker slot (spec §6.4 item 5: season matching isn't worker work).
4. **Per-source forced refresh**: today a forced run marks the whole path refreshed when the check stage hands the item
   to a worker, so sources after the detector (markers already on servers) are never refreshed.
5. **Several stored sources per detector** (season audio also yields the previous-season hint, Task 7).
6. **Follow-up requests**: a detector can name other files whose decision its answer may change; the job runner queues
   them (Task 8).
7. **`DetectorUnavailableError`**: a detector that can't answer this time stores nothing (retried next run) instead of
   failing the file or storing "nothing there".

**Files:**
- Modify: `media_preview_generator/markers/pipeline.py` (`LocalDetectorSpec` ~106-118, `PipelineContext` ~136-179,
  `_attempt` probe/refresh/evidence loop ~945-1029)
- Test: `tests/markers/test_pipeline_detectors.py` (new); existing `tests/markers/test_pipeline.py::TestStages` must
  pass unchanged

**Interfaces:**
- Consumes: `MarkerStore.replace_evidence(..., version=)`, `evidence_version` (phase 1).
- Produces (Tasks 7, 8 rely on these):
```python
class DetectorUnavailableError(Exception)
@dataclass(frozen=True) class LocalDetectorSpec:
    source: Source; types: frozenset[MarkerType]; detect: LocalDetector
    stores: frozenset[Source] = frozenset()                     # empty → {source}
    version: int = 1
    due: Callable[[FileRecord, PipelineContext], bool] | None = None
    needs_worker: Callable[[FileRecord, PipelineContext], bool] | None = None
    @property stored_sources -> frozenset[Source]
PipelineContext.request_followups(self, paths: Iterable[str]) -> None
PipelineContext.take_followups(self) -> list[str]            # sorted, then cleared
# detector call (unchanged): detect(rec, *, ctx, gpu, gpu_device_path, phase_callback, cancel_check, pause_check)
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/markers/test_pipeline_detectors.py
"""Local detectors in the pipeline: versioned answers, due checks, inline runs, per-source forced refresh, follow-ups."""

from __future__ import annotations

from unittest.mock import MagicMock

from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.pipeline import DetectorUnavailableError, LocalDetectorSpec
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import ready_publisher, server_config
from tests.markers.test_pipeline import (  # noqa: F401 - media and store are fixtures
    DUR,
    INTRO_ONLY,
    _ctx,
    _media_root,
    _registry,
    _run,
    media,
    store,
)

T = MarkerType
AUDIO_INTRO = Candidate(T.INTRO, 126_000, 158_000, Source.SEASON_AUDIO, 1.0, "2/2")
TEXT_CREDITS = Candidate(T.CREDITS, 1_300_000, DUR, Source.CREDITS_TEXT, 1.0, "")


def _spec(detector, **kwargs):
    return LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), detector, **kwargs)


def _pubs():
    return {"plex-1": ready_publisher()}


class TestStoredAnswers:
    def test_answer_is_stored_under_each_source_with_the_detector_version(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO, TEXT_CREDITS])
        spec = _spec(detector, stores=frozenset({Source.SEASON_AUDIO, Source.CREDITS_TEXT}), version=4)
        _run(_ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        rec = store.get_file(media)
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) == 4
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == 4
        assert {AUDIO_INTRO, TEXT_CREDITS} <= set(store.get_evidence(rec.id))

    def test_empty_answer_records_nothing_there_for_every_stored_source(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        spec = _spec(MagicMock(return_value=[]), stores=frozenset({Source.SEASON_AUDIO, Source.CREDITS_TEXT}))
        _run(_ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        rec = store.get_file(media)
        rows = {(r.source, r.type) for r in store.evidence_rows(rec.id)}
        assert {(Source.SEASON_AUDIO, None), (Source.CREDITS_TEXT, None)} <= rows

    def test_a_current_answer_is_not_asked_again_while_the_type_stays_undecided(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])  # one source: the intro stays in review at "High"
        ctx = _ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY)
        first, _ = _run(ctx, media, _pubs(), stage="process")
        second, _ = _run(ctx, media, _pubs(), stage="process")
        assert first.outcome_key == second.outcome_key == FileOutcome.NEEDS_REVIEW.value
        assert detector.call_count == 1

    def test_a_new_detector_version_runs_it_again(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        _run(_ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        spec2 = _spec(detector, version=2)
        _run(_ctx(store, reg, detectors=(spec2,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        assert detector.call_count == 2

    def test_due_hook_runs_it_again_and_gets_the_record_and_context(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        due = MagicMock(return_value=True)
        ctx = _ctx(store, reg, detectors=(_spec(detector, due=due),), settings_raw=INTRO_ONLY)
        _run(ctx, media, _pubs(), stage="process")
        _run(ctx, media, _pubs(), stage="process")
        assert detector.call_count == 2
        rec, passed_ctx = due.call_args.args
        assert rec.canonical_path == media and passed_ctx is ctx

    def test_unavailable_detector_stores_nothing_and_is_asked_next_run(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(side_effect=[DetectorUnavailableError("no chromaprint"), [AUDIO_INTRO]])
        ctx = _ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY)
        out, _ = _run(ctx, media, _pubs(), stage="process")
        rec = store.get_file(media)
        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) is None
        _run(ctx, media, _pubs(), stage="process")
        assert detector.call_count == 2 and AUDIO_INTRO in store.get_evidence(rec.id)


class TestWhereItRuns:
    def test_detector_that_needs_no_worker_runs_on_the_checking_thread(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        needs = MagicMock(return_value=False)
        ctx = _ctx(store, reg, detectors=(_spec(detector, needs_worker=needs),), settings_raw=INTRO_ONLY)
        out, _ = _run(ctx, media, _pubs())
        assert out is not None and out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        kwargs = detector.call_args.kwargs
        assert (kwargs["gpu"], kwargs["gpu_device_path"], kwargs["pause_check"]) == (None, None, None)
        rec, passed_ctx = needs.call_args.args
        assert rec.canonical_path == media and passed_ctx is ctx

    def test_check_defers_when_any_pending_detector_needs_a_worker(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        inline, worker = MagicMock(return_value=[]), MagicMock(return_value=[])
        specs = (
            _spec(inline, needs_worker=lambda _r, _c: False),
            _spec(worker, stores=frozenset({Source.CREDITS_TEXT})),
        )
        out, _ = _run(_ctx(store, reg, detectors=specs, settings_raw=INTRO_ONLY), media, _pubs())
        assert out is None
        inline.assert_not_called()
        worker.assert_not_called()


class TestForcedRefresh:
    def test_forced_run_reads_markers_on_servers_again_in_the_worker_stage(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        root = _media_root(media)
        reg.configs_by_id["jellyfin-1"] = server_config(
            "jellyfin-1", ServerType.JELLYFIN, root=root, markers={"enabled": False, "library_ids": None}
        )
        spec = _spec(MagicMock(return_value=[]))
        _run(_ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        reads = reg.get("jellyfin-1").get_media_segments
        assert reads.call_count == 1
        forced = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY, force=True)
        assert _run(forced, media, _pubs())[0] is None  # handed to a worker at season audio, before server markers
        _run(forced, media, _pubs(), stage="process")
        assert reads.call_count == 2

    def test_forced_run_runs_a_detector_whose_answer_is_current(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[])
        spec = _spec(detector, due=lambda _r, _c: False)
        _run(_ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        forced = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY, force=True)
        _run(forced, media, _pubs(), stage="process")
        assert detector.call_count == 2


class TestFollowups:
    def test_requests_are_merged_sorted_and_taken_once(self, store, media):
        ctx = _ctx(store, _registry(media, ServerType.PLEX))
        ctx.request_followups(["/tv/b.mkv", "/tv/a.mkv"])
        ctx.request_followups(["/tv/a.mkv"])
        assert ctx.take_followups() == ["/tv/a.mkv", "/tv/b.mkv"]
        assert ctx.take_followups() == []

    def test_a_detector_can_request_followups_through_its_context(self, store, media):
        reg = _registry(media, ServerType.PLEX)

        def detect(rec, *, ctx, **_kw):
            ctx.request_followups(["/tv/sibling.mkv"])
            return []

        ctx = _ctx(store, reg, detectors=(_spec(detect),), settings_raw=INTRO_ONLY)
        _run(ctx, media, _pubs(), stage="process")
        assert ctx.take_followups() == ["/tv/sibling.mkv"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_pipeline_detectors.py -q`
Expected: FAIL — `ImportError: cannot import name 'DetectorUnavailableError'`.

- [ ] **Step 3: Implement the spec, context and helpers**

Replace `LocalDetectorSpec` in `pipeline.py` with:

```python
class DetectorUnavailableError(Exception):
    """A local detector couldn't answer this time (its tool failed, the job was cancelled). Nothing is stored for it,
    so the next run asks it again."""


@dataclass(frozen=True)
class LocalDetectorSpec:
    """A detector that reads the file itself (phase 2: season audio; phase 3: credit text).

    Attributes:
        source: The source whose place in the user's order the detector runs at.
        types: Marker types it can decide.
        detect: ``detect(file, *, ctx, gpu, gpu_device_path, phase_callback, cancel_check, pause_check)``; raises
            ``DetectorUnavailableError`` when it can't answer this time.
        stores: Sources its candidates are stored under, each candidate under its own ``source``; empty = ``source``.
        version: Stored with its answer; an answer from another version is asked again.
        due: ``due(file, ctx)``: whether a stored answer of this version is out of date anyway (None: never).
        needs_worker: ``needs_worker(file, ctx)``: whether it needs a GPU/CPU worker now (None: always). One that
            doesn't runs on the checking thread.
    """

    source: Source
    types: frozenset[MarkerType]
    detect: LocalDetector
    stores: frozenset[Source] = frozenset()
    version: int = 1
    due: Callable[[FileRecord, PipelineContext], bool] | None = None
    needs_worker: Callable[[FileRecord, PipelineContext], bool] | None = None

    @property
    def stored_sources(self) -> frozenset[Source]:
        """The sources this detector's answers are stored under."""
        return self.stores or frozenset({self.source})
```

In `PipelineContext`: change the `_refreshed` field and add the follow-up fields and methods (the dataclass keeps its
other fields and docstring; add `Iterable` to the `collections.abc` import):

```python
    # (path, source) pairs a forced run already refreshed, so the worker stage doesn't ask those sources twice and
    # still refreshes the sources after the detector that handed the item to a worker.
    _refreshed: set[tuple[str, Source]] = field(default_factory=set, repr=False)
    _followups: set[str] = field(default_factory=set, repr=False)
    _followups_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def request_followups(self, paths: Iterable[str]) -> None:
        """Ask the job to run these files again after it finishes (their decision may change with this job's work)."""
        with self._followups_guard:
            self._followups.update(paths)

    def take_followups(self) -> list[str]:
        """The requested files, sorted, and forget them."""
        with self._followups_guard:
            taken = sorted(self._followups)
            self._followups.clear()
        return taken
```

Add near `_stale_evidence`:

```python
def _refreshing(ctx: PipelineContext, path: str, source: Source) -> bool:
    """Whether a forced run still has to ask ``source`` again for this file."""
    return ctx.force and (path, source) not in ctx._refreshed


def _mark_refreshed(ctx: PipelineContext, path: str, source: Source) -> None:
    if ctx.force:
        ctx._refreshed.add((path, source))


def _detector_due(ctx: PipelineContext, rec: FileRecord, spec: LocalDetectorSpec) -> bool:
    """Whether a detector's stored answer is missing, from another version, or out of date by its own ``due``."""
    if any(ctx.store.evidence_version(rec.id, source) != spec.version for source in spec.stored_sources):
        return True
    return bool(spec.due and spec.due(rec, ctx))


def _needs_worker(ctx: PipelineContext, rec: FileRecord, spec: LocalDetectorSpec) -> bool:
    return spec.needs_worker is None or spec.needs_worker(rec, ctx)


def _run_detector(
    ctx: PipelineContext,
    rec: FileRecord,
    spec: LocalDetectorSpec,
    *,
    gpu: str | None,
    gpu_device_path: str | None,
    phase: Callable[[str], None],
    cancel_check: Callable[[], bool] | None,
    pause_check: Callable[[], bool] | None,
) -> None:
    try:
        found = list(
            spec.detect(
                rec,
                ctx=ctx,
                gpu=gpu,
                gpu_device_path=gpu_device_path,
                phase_callback=phase,
                cancel_check=cancel_check,
                pause_check=pause_check,
            )
        )
    except DetectorUnavailableError as exc:
        logger.info("{} had no answer for {} this time: {}", spec.source.value, os.path.basename(rec.canonical_path), exc)
        return
    stray = [c for c in found if c.source not in spec.stored_sources]
    if stray:
        logger.warning("{} returned candidates for sources it doesn't store: {}", spec.source.value, stray)
    for source in sorted(spec.stored_sources, key=lambda s: s.value):
        ctx.store.replace_evidence(rec.id, source, [c for c in found if c.source is source], version=spec.version)
```

- [ ] **Step 4: Rewire `_attempt`**

Replace `refresh = ctx.force and path not in ctx._refreshed` and the probe condition with:

```python
    refresh_probe = _refreshing(ctx, path, Source.CHAPTERS)
    existing = ctx.store.get_file(path)
    unchanged = existing is not None and (existing.size, existing.mtime_ns) == (st.st_size, st.st_mtime_ns)
    probe = None
    stale_rules = unchanged and ctx.store.evidence_version(existing.id, Source.CHAPTERS) != CHAPTER_RULES_VERSION
    if refresh_probe or not unchanged or not existing.duration_ms or stale_rules:
        phase("Reading chapters…")
        try:
            probe = probe_media(path, ffprobe=ctx.ffprobe)
        except ProbeError as exc:
            return ItemOutcome(FileOutcome.FAILED.value, f"Couldn't read the file: {exc}")
    _mark_refreshed(ctx, path, Source.CHAPTERS)
```

Replace the evidence loop (from `for source_id in ctx.settings.ordered_enabled_sources():` through the trailing
`if refresh: ctx._refreshed.add(path)`) with:

```python
    for source_id in ctx.settings.ordered_enabled_sources():
        source = Source(source_id)
        refresh = _refreshing(ctx, path, source)
        if not gather_all and _all_decided(decisions, types) and not _stale_evidence(ctx, rec, source):
            continue
        if cancelled():
            return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
        if source in _ONLINE_LABELS:
            client = ctx.clients.get(source_id)
            if (
                source is Source.THEINTRODB
                and not gather_all
                and ctx.priority() >= PRIORITY_LOW
                and _only_confirming_chapters(decisions, types)
            ):
                continue  # the only daily-budgeted source is kept for files it could still decide
            if lookups_allowed and client is not None and _needs_lookup(ctx, rec, source, refresh):
                lookup_ids = lookup_ids or _lookup_ids(ids, servers)
                phase(f"Looking up {_ONLINE_LABELS[source]}…")
                _lookup(client, source, lookup_ids, rec, ctx, cancel_check)
        elif source is Source.SERVER_MARKERS:
            phase("Reading markers already on servers…")
            _read_server_markers(ctx, rec, servers, refresh)
        else:
            pending = [
                spec
                for spec in ctx.local_detectors
                if spec.source is source
                and any(gather_all or decisions[t].status is not DecisionStatus.DECIDED for t in spec.types & types)
                and (gather_all or _detector_due(ctx, rec, spec))
            ]
            if not local and any(_needs_worker(ctx, rec, spec) for spec in pending):
                return None  # sources already refreshed stay marked; the worker refreshes the rest
            for spec in pending:
                _run_detector(
                    ctx,
                    rec,
                    spec,
                    gpu=gpu,
                    gpu_device_path=gpu_device_path,
                    phase=phase,
                    cancel_check=cancel_check,
                    pause_check=pause_check,
                )
        _mark_refreshed(ctx, path, source)
        if not gather_all:
            decisions = _decide(ctx, rec, types)
```

(The THEINTRODB `continue` skips `_mark_refreshed` only on a normal run, where marking does nothing.) Update the module
docstring's last sentence to: "it returns None only when a registered local detector that needs a worker could still
decide something; one that doesn't runs right there". Update `LocalDetectorSpec` references in `check_item`'s docstring
the same way.

- [ ] **Step 5: Run the new and the existing pipeline tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers/test_pipeline_detectors.py tests/markers/test_pipeline.py tests/markers/test_publisher_contract.py -q`
Expected: PASS, including `TestStages::test_forced_evidence_is_refreshed_once_across_check_and_worker` (probe not
repeated, one lookup per online source) and `test_forced_run_hands_detector_sources_to_the_worker_even_when_decided`.

- [ ] **Step 6: Mutation self-check (high-risk)**

One at a time, confirm a test fails, revert: drop `and (gather_all or _detector_due(...))`; `_mark_refreshed` before
the source is handled instead of after; `spec.stores or` → `spec.stores`; `not local and any(` → `not local and all(`;
catch `Exception` instead of `DetectorUnavailableError` (must break
`test_detector_errors_reach_the_worker_for_its_cpu_fallback`).

- [ ] **Step 7: Commit**

`PATH="/home/data/.venv/bin:$PATH" git commit -m "feat(markers): versioned local detector answers, inline detectors, per-source forced refresh"`
(after the Architecture Review).

---
## Task 4: Emby Bridge plugin (C#), 4.9 + 4.10 builds, lab proof

`[lane-parallel]` `[high-risk]` (it rewrites Emby's chapter rows, which are user data) — spec §3.3 (Chapters3,
`MarkerType`, `SaveChapters` keeping `Chapter` rows, FullRefresh wipes, self-heal on `ItemUpdated`, 4.9/4.10 builds),
§6.3 EmbyMarkerPublisher, §10.4, §14 2026-09-14 Jellyfin plugin lines (file-size guard, removed-item cleanup) which
this plugin mirrors. Prototype: `evidence/plugins/emby-4.10/Plugin.cs` (lab route `/markerslab/set`).

**HTTP contract (Task 10 codes against exactly this; Emby's ServiceStack keeps PascalCase property names):**
- `GET /MediaPreviewBridge/Ping` (anonymous) → `200 {"Ok": true, "Version": "1.0.0.0", "Features": ["markers"]}`
- `GET /MediaPreviewBridge/Markers/{Id}` (admin) → `200 MarkersResponse`
- `POST /MediaPreviewBridge/Markers/{Id}` (admin), body `{"IntroStartTicks": long|null, "IntroEndTicks": long|null,
  "CreditsStartTicks": long|null, "FileSize": long|null}` → `200 MarkersResponse` (`Stored` = marker rows written)
- `DELETE /MediaPreviewBridge/Markers/{Id}` (admin) → `200 MarkersResponse` (`Stored` = 0)
- `MarkersResponse = {"Id": str, "Found": bool, "Error": str|null, "IntroStartTicks": long|null,
  "IntroEndTicks": long|null, "CreditsStartTicks": long|null, "FileSize": long|null, "Stale": bool, "Stored": int}`
- Unknown or non-numeric id → `200` with `Found=false`, `Error="item not found"`. Invalid body → `200` with
  `Found=true`, `Error="<why>"`, nothing stored. (Emby's own error pages are HTML and differ between versions; a JSON
  200 is the one shape both builds answer the same way.) Unauthenticated → Emby's 401; non-admin → Emby's 401/403.
- Rules: intro start and end both set or both null; `0 ≤ IntroStartTicks < IntroEndTicks`; `CreditsStartTicks ≥ 0`;
  `FileSize` null or > 0; at least one of intro/credits set (an all-null POST is refused: DELETE clears).
- Stored per item as `markers/<InternalId>.json` in the plugin data folder. Markers are served (written into
  Chapters3) only while the file on disk has the stored `FileSize` (or either size is unknown); `Stale` says when not.
- `ItemUpdated`: when the item has stored, non-stale markers and none of its chapter rows is a marker any more
  (FullRefresh wiped them) → write them again. `ItemRemoved`: delete the store file.

**Files:**
- Create: `emby-plugin/MediaPreviewBridge.Emby.csproj`, `emby-plugin/Plugin.cs`, `emby-plugin/Markers/StoredMarkers.cs`,
  `emby-plugin/Markers/MarkerStore.cs`, `emby-plugin/Markers/MarkerChapters.cs`, `emby-plugin/Markers/MarkerHealer.cs`,
  `emby-plugin/Api/BridgeService.cs`, `emby-plugin/README.md`, `.github/workflows/emby-plugin.yml`
- Modify: `.github/workflows/plugins-ci.yml` (Emby matrix job, `paths` filter), `.gitignore` (`emby-plugin/bin/`,
  `emby-plugin/obj/`), `docs/design/intro-credits/evidence/lab/up.sh` (`mlab-emby49`; pin `mlab-emby` to
  `emby/embyserver:4.10.0.40` instead of `latest`)
- Lab evidence: `docs/design/intro-credits/evidence/lab/emby_plugin_check.py`, results appended to
  `docs/design/intro-credits/evidence/lab/phase2-results.md` (created here, "Task 4" section)

**Interfaces:**
- Consumes: nothing from the app.
- Produces: the HTTP contract above; plugin name **"Media Preview Bridge for Emby"** (the catalog install name, Task 10),
  plugin id `8d6c1b3e-2f4a-4c5d-9e7f-0a1b2c3d4e5f`, DLL `MediaPreviewBridge.Emby.dll`, builds per `EmbyAbi` (`4.9` →
  `MediaBrowser.Server.Core 4.9.1.90`; `4.10` → `4.10.0.24-beta2`, the newest package on nuget.org, proven on 4.10.0.40
  in Step 6).

- [ ] **Step 1: Project, plugin class, store**

```xml
<!-- emby-plugin/MediaPreviewBridge.Emby.csproj -->
<Project Sdk="Microsoft.NET.Sdk">

  <PropertyGroup>
    <!-- Build per Emby ABI: `dotnet build -c Release -p:EmbyAbi=4.9` (4.9 and 4.10 plugins aren't interchangeable). -->
    <EmbyAbi Condition="'$(EmbyAbi)' == ''">4.10</EmbyAbi>
    <TargetFramework>netstandard2.0</TargetFramework>
    <RootNamespace>MediaPreviewBridge.Emby</RootNamespace>
    <AssemblyName>MediaPreviewBridge.Emby</AssemblyName>
    <LangVersion>latest</LangVersion>
    <Version>1.0.0.0</Version>
    <FileVersion>$(Version)</FileVersion>
    <AssemblyVersion>$(Version)</AssemblyVersion>
  </PropertyGroup>

  <PropertyGroup Condition="'$(EmbyAbi)' == '4.9'">
    <EmbyServerCoreVersion>4.9.1.90</EmbyServerCoreVersion>
  </PropertyGroup>

  <PropertyGroup Condition="'$(EmbyAbi)' == '4.10'">
    <EmbyServerCoreVersion>4.10.0.24-beta2</EmbyServerCoreVersion>
  </PropertyGroup>

  <ItemGroup>
    <!-- Emby supplies these at runtime; never copy them next to the plugin. -->
    <PackageReference Include="MediaBrowser.Server.Core" Version="$(EmbyServerCoreVersion)" ExcludeAssets="runtime" />
  </ItemGroup>

</Project>
```

```csharp
// emby-plugin/Plugin.cs
using System;
using System.IO;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace MediaPreviewBridge.Emby
{
    /// <summary>No settings: everything is driven by Media Preview Generator.</summary>
    public class PluginConfiguration : BasePluginConfiguration
    {
    }

    /// <summary>Receives Skip Intro / Skip Credits markers and keeps them through Emby's metadata refreshes.</summary>
    public class Plugin : BasePlugin<PluginConfiguration>
    {
        /// <summary>Plugin id; never change it (Emby keys the install on it).</summary>
        public static readonly Guid PluginId = new Guid("8d6c1b3e-2f4a-4c5d-9e7f-0a1b2c3d4e5f");

        /// <summary>Initializes a new instance of the <see cref="Plugin"/> class.</summary>
        public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
            : base(applicationPaths, xmlSerializer)
        {
            Instance = this;
        }

        /// <summary>Gets the running instance.</summary>
        public static Plugin Instance { get; private set; }

        /// <inheritdoc />
        public override string Name => "Media Preview Bridge for Emby";

        /// <inheritdoc />
        public override string Description =>
            "Shows Skip Intro and Skip Credits markers sent by Media Preview Generator, and puts them back when a metadata refresh removes them.";

        /// <inheritdoc />
        public override Guid Id => PluginId;

        /// <summary>Gets the folder holding one JSON file per item with markers.</summary>
        public string MarkerStoreDir => Path.Combine(DataFolderPath, "markers");
    }
}
```

```csharp
// emby-plugin/Markers/StoredMarkers.cs
namespace MediaPreviewBridge.Emby.Markers
{
    /// <summary>What Media Preview Generator sent for one item (ticks). Emby has no credits end.</summary>
    public class StoredMarkers
    {
        public long? IntroStartTicks { get; set; }

        public long? IntroEndTicks { get; set; }

        public long? CreditsStartTicks { get; set; }

        /// <summary>Gets or sets the size in bytes of the file the markers were detected on (null: not sent).</summary>
        public long? FileSize { get; set; }

        /// <summary>Why these markers can't be stored, or null when they can.</summary>
        public static string Problem(StoredMarkers m)
        {
            if (m == null) return "body is required";
            if (m.IntroStartTicks.HasValue != m.IntroEndTicks.HasValue) return "intro start and end must be sent together";
            if (m.IntroStartTicks.HasValue && (m.IntroStartTicks.Value < 0 || m.IntroEndTicks.Value <= m.IntroStartTicks.Value))
                return "invalid intro ticks";
            if (m.CreditsStartTicks.HasValue && m.CreditsStartTicks.Value < 0) return "invalid credits ticks";
            if (m.FileSize.HasValue && m.FileSize.Value <= 0) return "invalid fileSize";
            if (!m.IntroStartTicks.HasValue && !m.CreditsStartTicks.HasValue) return "no markers; use DELETE to clear";
            return null;
        }

        /// <summary>Whether the markers were detected on a different file than the one on disk now.</summary>
        public static bool IsStale(StoredMarkers m, long? currentFileSize) =>
            m != null && m.FileSize.HasValue && currentFileSize.HasValue && m.FileSize.Value != currentFileSize.Value;
    }
}
```

```csharp
// emby-plugin/Markers/MarkerStore.cs
using System;
using System.IO;
using MediaBrowser.Model.Logging;
using MediaBrowser.Model.Serialization;

namespace MediaPreviewBridge.Emby.Markers
{
    /// <summary>One JSON file per item, so markers survive restarts and can be written back after a refresh.</summary>
    public class MarkerStore
    {
        private static readonly object Gate = new object();
        private readonly IJsonSerializer _json;
        private readonly ILogger _log;

        public MarkerStore(IJsonSerializer json, ILogger log)
        {
            _json = json;
            _log = log;
        }

        private static string FileFor(long internalId) =>
            Path.Combine(Plugin.Instance.MarkerStoreDir, internalId.ToString(System.Globalization.CultureInfo.InvariantCulture) + ".json");

        public void Save(long internalId, StoredMarkers markers)
        {
            lock (Gate)
            {
                Directory.CreateDirectory(Plugin.Instance.MarkerStoreDir);
                var path = FileFor(internalId);
                var tmp = path + ".tmp";
                _json.SerializeToFile(markers, tmp);
                if (File.Exists(path)) File.Delete(path);
                File.Move(tmp, path);
            }
        }

        /// <summary>Stored markers, or null when none (a missing, unreadable or invalid file counts as none).</summary>
        public StoredMarkers Load(long internalId)
        {
            lock (Gate)
            {
                var path = FileFor(internalId);
                if (!File.Exists(path)) return null;
                try
                {
                    var markers = _json.DeserializeFromFile<StoredMarkers>(path);
                    if (StoredMarkers.Problem(markers) == null) return markers;
                    _log.Warn("Media Preview Bridge: ignoring invalid marker file for {0}", internalId);
                }
                catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is FormatException || ex is InvalidOperationException)
                {
                    // Type name only: the message can quote the file's content.
                    _log.Warn("Media Preview Bridge: ignoring unreadable marker file for {0} ({1})", internalId, ex.GetType().Name);
                }

                return null;
            }
        }

        public bool Delete(long internalId)
        {
            lock (Gate)
            {
                var path = FileFor(internalId);
                if (!File.Exists(path)) return false;
                File.Delete(path);
                return true;
            }
        }
    }
}
```

(`File.Move(tmp, path, overwrite)` doesn't exist in netstandard2.0, hence delete-then-move inside the lock. If the
serializer throws a type the `when` filter doesn't list on the lab containers, Step 6 records it and the filter gains
it — never a bare `catch`.)

- [ ] **Step 2: Chapter merge, healer, service**

```csharp
// emby-plugin/Markers/MarkerChapters.cs
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Model.Entities;

namespace MediaPreviewBridge.Emby.Markers
{
    /// <summary>Emby keeps markers as chapter rows with a MarkerType; the file's real chapters must stay untouched.</summary>
    public static class MarkerChapters
    {
        /// <summary>The item's plain chapters plus <paramref name="markers"/> (none: just the plain chapters), by start.</summary>
        public static List<ChapterInfo> Merge(IEnumerable<ChapterInfo> existing, StoredMarkers markers)
        {
            var list = (existing ?? Enumerable.Empty<ChapterInfo>()).Where(c => c.MarkerType == MarkerType.Chapter).ToList();
            if (markers != null)
            {
                if (markers.IntroStartTicks.HasValue)
                {
                    list.Add(new ChapterInfo { Name = "Intro", StartPositionTicks = markers.IntroStartTicks.Value, MarkerType = MarkerType.IntroStart });
                    list.Add(new ChapterInfo { Name = "Intro End", StartPositionTicks = markers.IntroEndTicks.Value, MarkerType = MarkerType.IntroEnd });
                }

                if (markers.CreditsStartTicks.HasValue)
                {
                    list.Add(new ChapterInfo { Name = "Credits", StartPositionTicks = markers.CreditsStartTicks.Value, MarkerType = MarkerType.CreditsStart });
                }
            }

            return list.OrderBy(c => c.StartPositionTicks).ToList();
        }

        public static bool HasMarkerRows(IEnumerable<ChapterInfo> chapters) =>
            chapters != null && chapters.Any(c => c.MarkerType != MarkerType.Chapter);

        public static int MarkerRowCount(StoredMarkers markers) =>
            markers == null ? 0 : (markers.IntroStartTicks.HasValue ? 2 : 0) + (markers.CreditsStartTicks.HasValue ? 1 : 0);

        /// <summary>The item's file size on disk, or null when it can't be read.</summary>
        public static long? CurrentFileSize(BaseItem item)
        {
            try
            {
                return string.IsNullOrEmpty(item.Path) || !File.Exists(item.Path) ? (long?)null : new FileInfo(item.Path).Length;
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                return null;
            }
        }
    }
}
```

```csharp
// emby-plugin/Markers/MarkerHealer.cs
using System;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Model.Logging;
using MediaBrowser.Model.Serialization;

namespace MediaPreviewBridge.Emby.Markers
{
    /// <summary>
    /// Emby's FullRefresh ("Replace all metadata", "Search for missing metadata") deletes every marker row. The item is
    /// updated in the same refresh, so markers are written back right there when they vanished and the file is still
    /// the one they were detected on. Also forgets removed items.
    /// </summary>
    public class MarkerHealer : IServerEntryPoint
    {
        private readonly ILibraryManager _libraryManager;
        private readonly IItemRepository _itemRepository;
        private readonly MarkerStore _store;
        private readonly ILogger _log;

        public MarkerHealer(ILibraryManager libraryManager, IItemRepository itemRepository, IJsonSerializer json, ILogManager logManager)
        {
            _libraryManager = libraryManager;
            _itemRepository = itemRepository;
            _log = logManager.GetLogger("MediaPreviewBridge");
            _store = new MarkerStore(json, _log);
        }

        public void Run()
        {
            _log.Info("Media Preview Bridge: marker store {0}", Plugin.Instance.MarkerStoreDir);
            _libraryManager.ItemUpdated += OnItemUpdated;
            _libraryManager.ItemRemoved += OnItemRemoved;
        }

        public void Dispose()
        {
            _libraryManager.ItemUpdated -= OnItemUpdated;
            _libraryManager.ItemRemoved -= OnItemRemoved;
        }

        private void OnItemUpdated(object sender, ItemChangeEventArgs e)
        {
            try
            {
                if (!(e.Item is Video item)) return;
                var stored = _store.Load(item.InternalId);
                if (stored == null || StoredMarkers.IsStale(stored, MarkerChapters.CurrentFileSize(item))) return;
                var chapters = _itemRepository.GetChapters(item);
                if (MarkerChapters.HasMarkerRows(chapters)) return;
                _itemRepository.SaveChapters(item.InternalId, MarkerChapters.Merge(chapters, stored));
                _log.Info("Media Preview Bridge: markers written back for item {0} ({1})", item.InternalId, e.UpdateReason);
            }
            catch (Exception ex)
            {
                // An event handler that throws breaks Emby's own refresh of the item.
                _log.ErrorException("Media Preview Bridge: writing markers back failed for an item", ex);
            }
        }

        private void OnItemRemoved(object sender, ItemChangeEventArgs e)
        {
            try
            {
                if (e.Item != null && _store.Delete(e.Item.InternalId))
                {
                    _log.Info("Media Preview Bridge: forgot markers of removed item {0}", e.Item.InternalId);
                }
            }
            catch (Exception ex)
            {
                _log.ErrorException("Media Preview Bridge: removing stored markers failed", ex);
            }
        }
    }
}
```

```csharp
// emby-plugin/Api/BridgeService.cs
using System.Globalization;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Net;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Model.Logging;
using MediaBrowser.Model.Serialization;
using MediaBrowser.Model.Services;
using MediaPreviewBridge.Emby.Markers;

namespace MediaPreviewBridge.Emby.Api
{
    [Route("/MediaPreviewBridge/Ping", "GET", Summary = "Media Preview Bridge presence and features")]
    [Unauthenticated]
    public class PingRequest : IReturn<PingResponse>
    {
    }

    public class PingResponse
    {
        public bool Ok { get; set; }

        public string Version { get; set; }

        public string[] Features { get; set; }
    }

    [Route("/MediaPreviewBridge/Markers/{Id}", "GET", Summary = "Stored markers for an item")]
    [Authenticated(Roles = "admin")]
    public class GetMarkersRequest : IReturn<MarkersResponse>
    {
        public string Id { get; set; }
    }

    [Route("/MediaPreviewBridge/Markers/{Id}", "POST", Summary = "Replace an item's markers")]
    [Authenticated(Roles = "admin")]
    public class SetMarkersRequest : StoredMarkers, IReturn<MarkersResponse>
    {
        public string Id { get; set; }
    }

    [Route("/MediaPreviewBridge/Markers/{Id}", "DELETE", Summary = "Remove an item's markers")]
    [Authenticated(Roles = "admin")]
    public class DeleteMarkersRequest : IReturn<MarkersResponse>
    {
        public string Id { get; set; }
    }

    public class MarkersResponse
    {
        public string Id { get; set; }

        public bool Found { get; set; }

        public string Error { get; set; }

        public long? IntroStartTicks { get; set; }

        public long? IntroEndTicks { get; set; }

        public long? CreditsStartTicks { get; set; }

        public long? FileSize { get; set; }

        public bool Stale { get; set; }

        public int Stored { get; set; }
    }

    public class BridgeService : IService
    {
        private readonly ILibraryManager _libraryManager;
        private readonly IItemRepository _itemRepository;
        private readonly MarkerStore _store;
        private readonly ILogger _log;

        public BridgeService(ILibraryManager libraryManager, IItemRepository itemRepository, IJsonSerializer json, ILogManager logManager)
        {
            _libraryManager = libraryManager;
            _itemRepository = itemRepository;
            _log = logManager.GetLogger("MediaPreviewBridge");
            _store = new MarkerStore(json, _log);
        }

        public object Get(PingRequest request) =>
            new PingResponse { Ok = true, Version = Plugin.Instance.Version.ToString(), Features = new[] { "markers" } };

        public object Get(GetMarkersRequest request)
        {
            var item = Find(request.Id);
            if (item == null) return NotFound(request.Id);
            return Describe(item, _store.Load(item.InternalId), 0);
        }

        public object Post(SetMarkersRequest request)
        {
            var item = Find(request.Id);
            if (item == null) return NotFound(request.Id);
            if (!(item is Video)) return new MarkersResponse { Id = request.Id, Found = true, Error = "item is not a video" };
            var markers = new StoredMarkers
            {
                IntroStartTicks = request.IntroStartTicks,
                IntroEndTicks = request.IntroEndTicks,
                CreditsStartTicks = request.CreditsStartTicks,
                FileSize = request.FileSize,
            };
            var problem = StoredMarkers.Problem(markers);
            if (problem != null) return new MarkersResponse { Id = request.Id, Found = true, Error = problem };
            _store.Save(item.InternalId, markers);
            var stale = StoredMarkers.IsStale(markers, MarkerChapters.CurrentFileSize(item));
            // A stale set is stored (the app sees Stale=true and says why) but never shown on a different file.
            _itemRepository.SaveChapters(item.InternalId, MarkerChapters.Merge(_itemRepository.GetChapters(item), stale ? null : markers));
            _log.Info("Media Preview Bridge: stored markers for item {0}", item.InternalId);
            return Describe(item, markers, stale ? 0 : MarkerChapters.MarkerRowCount(markers));
        }

        public object Delete(DeleteMarkersRequest request)
        {
            var item = Find(request.Id);
            if (item == null) return NotFound(request.Id);
            _store.Delete(item.InternalId);
            _itemRepository.SaveChapters(item.InternalId, MarkerChapters.Merge(_itemRepository.GetChapters(item), null));
            return Describe(item, null, 0);
        }

        private BaseItem Find(string id) =>
            long.TryParse(id, NumberStyles.None, CultureInfo.InvariantCulture, out var internalId) ? _libraryManager.GetItemById(internalId) : null;

        private static MarkersResponse NotFound(string id) => new MarkersResponse { Id = id, Found = false, Error = "item not found" };

        private static MarkersResponse Describe(BaseItem item, StoredMarkers markers, int stored) => new MarkersResponse
        {
            Id = item.InternalId.ToString(CultureInfo.InvariantCulture),
            Found = true,
            IntroStartTicks = markers?.IntroStartTicks,
            IntroEndTicks = markers?.IntroEndTicks,
            CreditsStartTicks = markers?.CreditsStartTicks,
            FileSize = markers?.FileSize,
            Stale = StoredMarkers.IsStale(markers, MarkerChapters.CurrentFileSize(item)),
            Stored = stored,
        };
    }
}
```

`emby-plugin/README.md`: what the plugin does, the HTTP contract table above, the two builds, manual install (copy
`MediaPreviewBridge.Emby.dll` for your Emby version into Emby's `plugins` folder — `/config/plugins` in the official
container — and restart Emby), and that Media Preview Generator's Intro & Credits tab shows the plugin as ready once
Emby restarted.

- [ ] **Step 3: Build both ABIs locally**

```bash
cd /home/data/workspace/plex_generate_vid_previews/emby-plugin
for abi in 4.9 4.10; do
  nice -n 19 docker run --rm -v "$PWD":/src -w /src mcr.microsoft.com/dotnet/sdk:9.0 \
    dotnet build -c Release -p:EmbyAbi=$abi -o "out/$abi" 2>&1 | tail -3
done
ls -l out/4.9/MediaPreviewBridge.Emby.dll out/4.10/MediaPreviewBridge.Emby.dll
```
Expected: `Build succeeded.` twice, two DLLs, no `MediaBrowser.*.dll` copied into `out/` (`ExcludeAssets="runtime"`).
Add `emby-plugin/out/` to `.gitignore` too. If `[Unauthenticated]` or `IItemRepository.GetChapters(BaseItem)` doesn't
exist in a package version, stop: record the compiler error and the members that do exist (`docker run ... dotnet
build` output) and ask the controller — don't guess another API.

- [ ] **Step 4: CI**

Append to `.github/workflows/plugins-ci.yml` (and add `"emby-plugin/**"` to its `pull_request.paths`):

```yaml
  emby:
    name: Emby ${{ matrix.abi }}
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        abi: ["4.9", "4.10"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-dotnet@v4
        with:
          dotnet-version: "9.0.x"
      - name: Build
        working-directory: emby-plugin
        run: dotnet build -c Release -p:EmbyAbi=${{ matrix.abi }}
```

Create `.github/workflows/emby-plugin.yml`: on `push: tags: ["emby-plugin-v*"]` and `workflow_dispatch`, build both
ABIs, rename the outputs `MediaPreviewBridge.Emby-<abi>.dll`, and attach them to the GitHub release for the tag with
`softprops/action-gh-release@v2` (the same action `jellyfin-plugin.yml` uses; copy its permissions block). It is never
triggered in this phase: tags are releases (owner's word only).

- [ ] **Step 5: Lab containers**

In `docs/design/intro-credits/evidence/lab/up.sh`: pin `mlab-emby` to `emby/embyserver:4.10.0.40` (what the lab runs
today; `latest` would move under the tests), add `mlab-emby49` to the `recreate` loop, and add:

```bash
exists mlab-emby49 || docker run -d --name mlab-emby49 --network mlab -e UID=1000 -e GID=1000 \
    -p 127.0.0.1:18099:8096 -v mlab_emby49_config:/config "${MV[@]}" emby/embyserver:4.9.1.90
```

Then `./up.sh` (creates only the missing container). First start of `mlab-emby49`: open `http://127.0.0.1:18099`
in a browser, finish the wizard with user `lab` / password `lab`, add a TV library "Synth Chapters" at
`/media/synth-chapters`, create an API key (Settings → API Keys) and append `EMBY49_TOKEN=<key>` and
`EMBY49_UID=<lab user id>` to `evidence/lab/env` (chmod 600, never printed).

- [ ] **Step 6: Lab proof on both Emby versions**

Install by hand on each container (the lab volumes are ours, not `/data`):

```bash
cd /home/data/workspace/plex_generate_vid_previews/emby-plugin
docker cp out/4.10/MediaPreviewBridge.Emby.dll mlab-emby:/config/plugins/ && docker restart mlab-emby
docker cp out/4.9/MediaPreviewBridge.Emby.dll mlab-emby49:/config/plugins/ && docker restart mlab-emby49
```

Remove the prototype `MarkersLabEmby.dll` from `mlab-emby:/config/plugins/` first (back it up into the lab folder's
git-ignored `synth/_backup/`), so two plugins never write the same chapter rows.

Write `evidence/lab/emby_plugin_check.py` (reads `env`, scrubs with `phase1_matrix.scrub`, prints one line per check
and writes `results/emby-plugin-<container>.json`). For each of `mlab-emby` (18096) and `mlab-emby49` (18099), on
Synth Chapters S01E01 (item id from `GET /Items?Recursive=true&IncludeItemTypes=Episode&SearchTerm=Synth Chapters`):

| # | Check | Pass when |
|---|---|---|
| 1 | `GET /MediaPreviewBridge/Ping` without a token | 200, `Ok` true, `Features == ["markers"]` |
| 2 | `GET /MediaPreviewBridge/Markers/999999999` with the admin key | 200, `Found` false, `Error == "item not found"` |
| 3 | Same with no token; with a non-admin user's token (create user `viewer`) | 401 / 401 or 403 (record which) |
| 4 | POST intro 10 s–40 s, credits 100 s, `FileSize` = the file's size on the host | 200, `Stored == 3`; `GET /Users/{uid}/Items/{id}?Fields=Chapters` lists `IntroStart` 100000000, `IntroEnd` 400000000, `CreditsStart` 1000000000 **and every original `Chapter` row with its name** |
| 5 | POST again with intro 12 s–42 s, no credits | `Stored == 2`; chapters show the new intro, no `CreditsStart`, original chapters intact |
| 6 | Invalid bodies: intro start without end; end ≤ start; negative credits; `FileSize` 0; all null | each 200 with `Error` set, chapters unchanged (compare to check 5) |
| 7 | Refresh `?MetadataRefreshMode=FullRefresh&ReplaceAllMetadata=true` | within 60 s the marker rows are back (poll), plugin log line "markers written back" |
| 8 | Refresh `Default`, `ValidationOnly`; library scan; `docker restart` | markers still there after each |
| 9 | POST with `FileSize` = size + 1 | 200, `Stale` true, `Stored == 0`, chapters have no marker rows; FullRefresh doesn't write them back |
| 10 | DELETE | 200, `Stored == 0`, no marker rows, original chapters intact, store file gone (`docker exec <container> ls <dir>`, `<dir>` from the plugin's start-up log line "marker store …") |
| 11 | Remove the item (delete the synth copy used for this check from the lab folder, scan) | store file for its id deleted |
| 12 | Emby web player (4.10 only): `emby_client.py <item id> shot.png` after check 4's POST | "Skip Intro" button found at 0:22 (script prints `skip found: True`) |

Use a disposable copy of S01E01 for checks 9–11 (`cp` inside `lab/synth/Synth Chapters (2021)/Season 01/`), never a
real library file. Paste the table with results into `phase2-results.md` → "Task 4 — Emby plugin". Any failed check →
`superpowers:systematic-debugging` on the lab container, fix, rebuild, re-run the whole table.

- [ ] **Step 7: Commit**

Stage `emby-plugin/` (no `bin/`, `obj/`, `out/`), the two workflows, `.gitignore`, `up.sh`, `emby_plugin_check.py`,
`phase2-results.md`; Architecture Review; commit
`feat(emby-plugin): Media Preview Bridge for Emby with markers endpoint and self-heal`.

---
## Task 5: Marker API contract cassettes (Plex `get_markers`, Jellyfin Bridge + `/MediaSegments`)

`[lane-parallel]` — ledger parked items "get_markers VCR cassette from claimed lab Plex" (progress.md line 144) and
"VCR cassettes for Jellyfin Bridge/MediaSegments … from the lab" (line 155); `tests/cassettes/README.md`. Emby's
cassettes come with its client in Task 10.

The marker readers and the Jellyfin publisher are unit-tested against autospec'd clients; nothing pins the URLs and
response shapes the real servers accept. These cassettes are recorded once against the lab and replayed offline.

**Files:**
- Create: `tests/test_servers_markers_vcr.py`, cassettes under `tests/cassettes/test_servers_markers_vcr/`
- Modify: `tests/cassettes/README.md` (a "Markers (lab)" recording section)

**Interfaces:**
- Consumes: `PlexServer._resolve_one_path(path)`, `PlexServer.get_markers(item_id)`,
  `JellyfinServer._uncached_resolve_remote_path_to_item_id(path)`, `get_bridge_marker_state`, `put_bridge_markers`,
  `get_media_segments`, `delete_bridge_markers` (all phase 1).
- Produces: nothing new in code.

- [ ] **Step 1: Write the tests**

```python
# tests/test_servers_markers_vcr.py
"""Cassette-backed contract tests for the Intro & Credits vendor calls (recorded against the storage lab).

Pins the URLs and response shapes of Plex's marker read (``includeMarkers=1``) and the Media Preview Bridge markers
routes plus Jellyfin's core ``/MediaSegments``. Item ids are resolved through the same recorded calls, so replay needs
no ids in this file. Recording: tests/cassettes/README.md → "Markers (lab)".
"""

from __future__ import annotations

import os

import pytest

from media_preview_generator.servers import JellyfinServer, Library, PlexServer, ServerConfig, ServerType

pytestmark = [pytest.mark.vcr]

SYNTH_E01 = "/media/synth-chapters/Synth Chapters (2021)/Season 01/Synth Chapters (2021) - S01E01.webm"
TICKS = 10_000


@pytest.fixture
def plex_lab():
    cfg = ServerConfig(
        id="plex-vcr-markers",
        type=ServerType.PLEX,
        name="Plex VCR",
        enabled=True,
        url=os.environ.get("PLEX_URL", "http://fake-plex.local:32400"),
        auth={"token": os.environ.get("PLEX_TOKEN", "fake-token"), "method": "token"},
        verify_ssl=False,
        libraries=[],
        path_mappings=[],
    )
    return PlexServer(cfg)


@pytest.fixture
def jellyfin_lab():
    cfg = ServerConfig(
        id="jellyfin-vcr-markers",
        type=ServerType.JELLYFIN,
        name="Jellyfin VCR",
        enabled=True,
        url=os.environ.get("JELLYFIN_URL", "http://fake-jellyfin.local:8096"),
        auth={"method": "api_key", "api_key": os.environ.get("JELLYFIN_TOKEN", "fake-token")},
        verify_ssl=False,
        libraries=[Library(id="1", name="Synth Chapters", remote_paths=("/media/synth-chapters",), enabled=True)],
    )
    return JellyfinServer(cfg)


@pytest.mark.real_plex_server
class TestPlexMarkerReadContract:
    def test_markers_of_a_synth_episode(self, plex_lab):
        rating_key = plex_lab._resolve_one_path(SYNTH_E01)
        assert rating_key, "the synth episode must be in the lab Plex library when recording"
        markers = plex_lab.get_markers(rating_key)
        assert markers, "record after the phase-1 lab backfill published the synth chapters to Plex"
        assert {m["type"] for m in markers} <= {"intro", "credits"}
        for m in markers:
            assert set(m) == {"type", "start_ms", "end_ms", "final"}
            assert isinstance(m["start_ms"], int) and isinstance(m["end_ms"], int) and m["end_ms"] > m["start_ms"]

    def test_unknown_item_reads_as_none(self, plex_lab):
        assert plex_lab.get_markers("999999999") is None


class TestJellyfinBridgeMarkersContract:
    def test_store_serve_read_delete_round_trip(self, jellyfin_lab):
        item_id = jellyfin_lab._uncached_resolve_remote_path_to_item_id(SYNTH_E01)
        assert item_id, "the synth episode must be in the lab Jellyfin library when recording"
        before = jellyfin_lab.get_bridge_marker_state(item_id)
        assert before is not None and set(before) == {"segments", "fileSize", "stale"}

        segments = [
            {"type": "Intro", "startTicks": 10_000 * TICKS, "endTicks": 40_000 * TICKS},
            {"type": "Outro", "startTicks": 100_000 * TICKS, "endTicks": 120_000 * TICKS},
        ]
        resp = jellyfin_lab.put_bridge_markers(item_id, segments, file_size=before["fileSize"])
        assert resp.status_code == 200

        served = jellyfin_lab.get_media_segments(item_id)
        keys = {(r["Type"], r["StartTicks"], r["EndTicks"]) for r in served}
        assert {(s["type"], s["startTicks"], s["endTicks"]) for s in segments} <= keys

        state = jellyfin_lab.get_bridge_marker_state(item_id)
        assert state["segments"] == segments and state["stale"] is False

        assert jellyfin_lab.delete_bridge_markers(item_id).status_code == 204
        assert jellyfin_lab.get_bridge_marker_state(item_id)["segments"] == []

        # Put back what the lab had, so recording leaves the lab as it found it.
        if before["segments"]:
            jellyfin_lab.put_bridge_markers(item_id, before["segments"], file_size=before["fileSize"])

    def test_unknown_item_is_unknown_not_empty(self, jellyfin_lab):
        assert jellyfin_lab.get_bridge_marker_state("ffffffffffffffffffffffffffffffff") is None
        assert jellyfin_lab.get_media_segments("ffffffffffffffffffffffffffffffff") in (None, [])
```

- [ ] **Step 2: Record against the lab**

The phase-1 lab state must be in place (plugins installed, synth chapters published — `phase1-results.md` run 2).
Scrubbing is on by default (`tests/conftest.py` `vcr_config`); check that `X-Plex-Token`, `Authorization` and the
Jellyfin `api_key` never appear in the YAML before staging.

```bash
cd /home/data/workspace/plex_generate_vid_previews
set -a; . docs/design/intro-credits/evidence/lab/env; set +a
PLEX_URL=http://127.0.0.1:32402 JELLYFIN_URL=http://127.0.0.1:18097 JELLYFIN_TOKEN="$JF_TOKEN" \
  /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_servers_markers_vcr.py --record-mode=once
grep -rlE "$PLEX_TOKEN|$JF_TOKEN" tests/cassettes/test_servers_markers_vcr/ && echo "TOKEN LEAK — delete and fix scrubbing" || echo "clean"
```
Expected: `4 passed`, `clean`.

- [ ] **Step 3: Replay offline**

Run: `env -u PLEX_URL -u JELLYFIN_URL /home/data/.venv/bin/python -m pytest --no-cov tests/test_servers_markers_vcr.py -q`
Expected: `4 passed` with no network (the default suite runs it).

- [ ] **Step 4: README**

Add to `tests/cassettes/README.md` a "Markers (lab)" section with the Step 2 command, the phase-1 lab state it needs
and the token-leak grep.

- [ ] **Step 5: Commit**

`test(markers): contract cassettes for Plex marker reads and the Jellyfin Bridge markers routes`.

---
## Task 6: Harness skeleton + matcher reproduction gate (118 episodes)

`[sequential]` (after Tasks 1, 2) — spec §5.3 measured table (v3 alg1 stereo: **91 useful / 13 wrong / 14 missed**
of 118; "useful" = end within 5 s and start within 15 s), §10.2, roadmap phase 2 ("must reproduce
`evidence/eval/eval_results_v3.json` segment-for-segment on cached fingerprints before use").

This is the gate before Task 7 turns season audio on: on the real episodes the eval used, (a) the numpy port must
equal the fp3 reference exactly on the same fingerprints, and (b) the tally must be at least the spec's numbers.
Segment differences from the stored JSON that the reference shares are ffmpeg-version drift, reported, not a port bug.

**Resource and data rules for every harness run:** `nice -n 19`, one heavy job at a time on storage; media files are
only read by ffprobe/ffmpeg; fingerprints are cached under `$MARKERS_EVAL_CACHE` (default `~/.cache/markers_eval`),
never under `/data*`. The truth files are local-only (git-ignored) in the main checkout's
`docs/design/intro-credits/evidence/`; in a worktree set `MARKERS_EVAL_EVIDENCE` to that folder. Committed summaries
contain counts and show names only, never file paths.

**Files:**
- Create: `tools/markers_eval/data.py`, `tools/markers_eval/score.py`, `tools/markers_eval/cache.py`,
  `tools/markers_eval/intros.py`, `tools/markers_eval/__main__.py`, `tools/markers_eval/README.md`,
  `docs/design/intro-credits/evidence/eval/phase2-harness.md` (committed summary)
- Test: `tests/markers_eval/__init__.py` (empty), `tests/markers_eval/test_score.py`, `tests/markers_eval/test_intros.py`

**Interfaces:**
- Consumes: `markers.audio.matcher.season_intros`, `IntroSegment`; `markers.audio.fingerprint.compute_fingerprint`,
  `chromaprint_ffmpeg`, `window_s`, `ALGORITHM`; `markers.probe.probe_media`, `ffprobe_path_for`;
  `tools.markers_eval.fp3_reference.analyse_points`.
- Produces (Task 15 extends):
```python
# tools/markers_eval/data.py
def evidence_dir() -> Path
@dataclass(frozen=True) class EvalEpisode:
    season: str; file: str; truth_intro: tuple[float, float] | None; truth_credits: tuple[float, float] | None
    v3_segment: tuple[float, float, int] | None
def load_v3_results(path: Path | None = None) -> list[EvalEpisode]
def by_season(episodes: list[EvalEpisode]) -> dict[str, list[EvalEpisode]]      # seasons sorted, files sorted
# tools/markers_eval/score.py
USEFUL_END_S = 5.0; USEFUL_START_S = 15.0
@dataclass class Tally: useful: int = 0; wrong: int = 0; missed: int = 0
    def add(self, verdict: str) -> None; def as_dict(self) -> dict[str, int]; def at_least(self, useful: int, wrong: int) -> bool
def judge_intro(segment: tuple[float, float] | None, truth: tuple[float, float]) -> str
# tools/markers_eval/cache.py
class FingerprintCache:
    def __init__(self, root: Path, *, ffmpeg: str, ffprobe: str) -> None
    def points(self, path: str) -> np.ndarray
# tools/markers_eval/intros.py
SPEC_V3 = (91, 13, 14)
@dataclass class ReproductionReport:
    tally: Tally; port_vs_reference: list[dict]; drift: list[dict]; seasons: int; episodes: int
    @property def passed(self) -> bool
def reproduce(episodes: list[EvalEpisode], *, points: Callable[[str], np.ndarray], with_reference: bool = True) -> ReproductionReport
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/markers_eval/test_score.py
import pytest

from tools.markers_eval.score import Tally, judge_intro


@pytest.mark.parametrize(
    ("segment", "verdict"),
    [
        (None, "missed"),
        ((68.0, 86.0), "useful"),
        ((53.0, 91.0), "useful"),  # start 14.5 s early, end 4.75 s late: both inside the tolerances
        ((52.0, 86.0), "wrong"),  # start 15.5 s off
        ((68.0, 91.5), "wrong"),  # end 5.25 s off
    ],
)
def test_judge_intro_uses_the_spec_tolerances(segment, verdict):
    assert judge_intro(segment, (67.5, 86.25)) == verdict


def test_tally_counts_and_compares_with_the_spec():
    t = Tally()
    for v in ["useful"] * 91 + ["wrong"] * 13 + ["missed"] * 14:
        t.add(v)
    assert t.as_dict() == {"useful": 91, "wrong": 13, "missed": 14}
    assert t.at_least(91, 13)
    t.add("wrong")
    assert not t.at_least(91, 13)
```

```python
# tests/markers_eval/test_intros.py
"""The reproduction gate on synthetic seasons: port == reference, drift reported, tally from truth."""

from __future__ import annotations

import numpy as np

from media_preview_generator.markers.audio import POINT_S
from tools.markers_eval import fp3_reference
from tools.markers_eval.data import EvalEpisode
from tools.markers_eval.intros import reproduce


def _fps(seed: int, episodes: int = 3):
    rng = np.random.default_rng(seed)
    intro = rng.integers(0, 2**32, size=200, dtype=np.uint64).astype("<u4")
    out = {}
    for e in range(1, episodes + 1):
        body = rng.integers(0, 2**32, size=900, dtype=np.uint64).astype("<u4")
        body[100 + 10 * e : 300 + 10 * e] = intro
        out[f"/eval/Show/Season 01/Show - S01E{e:02d}.mkv"] = body
    return out


def _episodes(fps, *, stored_shift_s: float = 0.0):
    files = sorted(fps)
    ref = fp3_reference.analyse_points(fps, files)
    eps = []
    for f in files:
        seg = ref[f]["segment"]
        stored = (seg[0] + stored_shift_s, seg[1] + stored_shift_s, seg[2]) if seg else None
        e = int(f[-6:-4])
        truth = ((100 + 10 * e) * POINT_S, (299 + 10 * e) * POINT_S)
        eps.append(EvalEpisode("/eval/Show/Season 01", f, truth, None, stored))
    return eps


def test_identical_segments_pass_with_every_episode_useful():
    fps = _fps(1)
    report = reproduce(_episodes(fps), points=fps.__getitem__)
    assert report.port_vs_reference == [] and report.drift == []
    assert report.tally.as_dict() == {"useful": 3, "wrong": 0, "missed": 0}
    assert (report.seasons, report.episodes) == (1, 3)


def test_stored_segments_that_differ_are_reported_as_drift_not_as_a_port_failure():
    fps = _fps(2)
    report = reproduce(_episodes(fps, stored_shift_s=3.0), points=fps.__getitem__)
    assert report.port_vs_reference == []
    assert len(report.drift) == 3 and {d["file"] for d in report.drift} == set(fps)


def test_a_port_that_disagrees_with_the_reference_fails_the_gate(monkeypatch):
    fps = _fps(3)
    from tools.markers_eval import intros

    monkeypatch.setattr(intros, "season_intros", lambda points: {f: None for f in points})
    report = reproduce(_episodes(fps), points=fps.__getitem__)
    assert len(report.port_vs_reference) == 3
    assert report.passed is False


def test_passed_needs_the_spec_numbers(monkeypatch):
    fps = _fps(4)
    report = reproduce(_episodes(fps), points=fps.__getitem__)
    assert report.passed is False  # 3 useful < 91: a synthetic season can't pass the real-data gate
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers_eval -q`
Expected: FAIL — `ModuleNotFoundError: tools.markers_eval.score`.

- [ ] **Step 3: Implement**

```python
# tools/markers_eval/score.py
"""Scoring rules for the accuracy harness (spec §5.3: "useful" = end within 5 s and start within 15 s)."""

from __future__ import annotations

from dataclasses import dataclass

USEFUL_END_S = 5.0
USEFUL_START_S = 15.0


def judge_intro(segment: tuple[float, float] | None, truth: tuple[float, float]) -> str:
    """``useful``, ``wrong`` or ``missed`` for one detected intro against its truth (seconds)."""
    if segment is None:
        return "missed"
    useful = abs(segment[1] - truth[1]) <= USEFUL_END_S and abs(segment[0] - truth[0]) <= USEFUL_START_S
    return "useful" if useful else "wrong"


@dataclass
class Tally:
    """Useful / wrong / missed counts."""

    useful: int = 0
    wrong: int = 0
    missed: int = 0

    def add(self, verdict: str) -> None:
        """Count one verdict."""
        setattr(self, verdict, getattr(self, verdict) + 1)

    def as_dict(self) -> dict[str, int]:
        """The counts by name."""
        return {"useful": self.useful, "wrong": self.wrong, "missed": self.missed}

    def at_least(self, useful: int, wrong: int) -> bool:
        """Whether this is as good as a reference: as many useful or more, as many wrong or fewer."""
        return self.useful >= useful and self.wrong <= wrong
```

```python
# tools/markers_eval/data.py
"""Loaders for the local-only truth sets under docs/design/intro-credits/evidence (git-ignored: real library paths)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

EVIDENCE_ENV = "MARKERS_EVAL_EVIDENCE"


def evidence_dir() -> Path:
    """``$MARKERS_EVAL_EVIDENCE``, else this checkout's evidence folder."""
    env = os.environ.get(EVIDENCE_ENV)
    return Path(env) if env else Path(__file__).resolve().parents[2] / "docs/design/intro-credits/evidence"


@dataclass(frozen=True)
class EvalEpisode:
    """One episode of the 118-episode intro eval."""

    season: str
    file: str
    truth_intro: tuple[float, float] | None
    truth_credits: tuple[float, float] | None
    v3_segment: tuple[float, float, int] | None


def _pair(value: object) -> tuple[float, float] | None:
    return (float(value[0]), float(value[1])) if isinstance(value, list) and len(value) >= 2 else None


def load_v3_results(path: Path | None = None) -> list[EvalEpisode]:
    """``eval/eval_results_v3.json``: season, file, chapter truth and the v3 intro segment per episode."""
    rows = json.loads((path or evidence_dir() / "eval/eval_results_v3.json").read_text())
    out = []
    for r in rows:
        seg = (r.get("intro") or {}).get("segment")
        out.append(
            EvalEpisode(
                season=r["season"],
                file=r["file"],
                truth_intro=_pair((r.get("truth") or {}).get("intro")),
                truth_credits=_pair((r.get("truth") or {}).get("credits")),
                v3_segment=(float(seg[0]), float(seg[1]), int(seg[2])) if seg else None,
            )
        )
    return out


def by_season(episodes: list[EvalEpisode]) -> dict[str, list[EvalEpisode]]:
    """Episodes grouped by season folder, both sorted (the eval matched each season's files in path order)."""
    groups: dict[str, list[EvalEpisode]] = {}
    for e in episodes:
        groups.setdefault(e.season, []).append(e)
    return {s: sorted(groups[s], key=lambda e: e.file) for s in sorted(groups)}
```

```python
# tools/markers_eval/cache.py
"""Fingerprints of real episodes, cached on local disk (never next to the media) so harness re-runs are cheap."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np

from media_preview_generator.markers.audio.fingerprint import ALGORITHM, compute_fingerprint, window_s
from media_preview_generator.markers.probe import probe_media


class FingerprintCache:
    """``points(path)``: the app's own fingerprint of a file, computed once per file identity."""

    def __init__(self, root: Path, *, ffmpeg: str, ffprobe: str) -> None:
        """Create the cache.

        Args:
            root: Cache folder (created); must not be under /data*.
            ffmpeg: An ffmpeg with chromaprint.
            ffprobe: ffprobe for durations.
        """
        if str(root.resolve()).startswith("/data"):
            raise ValueError("the fingerprint cache must not live under /data*")
        root.mkdir(parents=True, exist_ok=True)
        self._root, self._ffmpeg, self._ffprobe = root, ffmpeg, ffprobe

    def points(self, path: str) -> np.ndarray:
        """The file's fingerprint (spec window and algorithm), from the cache when the file is unchanged."""
        st = os.stat(path)
        duration_ms = probe_media(path, ffprobe=self._ffprobe).duration_ms
        if not duration_ms:
            raise ValueError(f"no duration for {os.path.basename(path)}")
        key = f"{path}|{st.st_size}|{st.st_mtime_ns}|{window_s(duration_ms):.3f}|{ALGORITHM}"
        cached = self._root / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".npy")
        if cached.exists():
            return np.load(cached)
        points = compute_fingerprint(path, duration_ms, ffmpeg=self._ffmpeg)
        np.save(cached, points)
        return points
```

```python
# tools/markers_eval/intros.py
"""TV intro eval: the v3 matcher on the 118 episodes with studio-chapter truth (spec §5.3)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio.matcher import season_intros

from . import fp3_reference
from .data import EvalEpisode, by_season
from .score import Tally, judge_intro

SPEC_V3 = (91, 13, 14)
# Stored segments are rounded floats from another ffmpeg build; two points either way is the same answer.
DRIFT_TOLERANCE_S = 2 * POINT_S


@dataclass
class ReproductionReport:
    """What the gate found."""

    tally: Tally = field(default_factory=Tally)
    port_vs_reference: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)
    seasons: int = 0
    episodes: int = 0

    @property
    def passed(self) -> bool:
        """Port equals the reference everywhere and the tally is at least the spec's numbers."""
        return not self.port_vs_reference and self.tally.at_least(SPEC_V3[0], SPEC_V3[1])


def _as_tuple(segment) -> tuple | None:
    return tuple(segment) if segment else None


def _drifted(stored: tuple[float, float, int] | None, got: tuple | None) -> bool:
    if (stored is None) != (got is None):
        return True
    if stored is None:
        return False
    return (
        abs(stored[0] - got[0]) > DRIFT_TOLERANCE_S
        or abs(stored[1] - got[1]) > DRIFT_TOLERANCE_S
        or stored[2] != got[2]
    )


def reproduce(
    episodes: list[EvalEpisode], *, points: Callable[[str], np.ndarray], with_reference: bool = True
) -> ReproductionReport:
    """Run the port (and the reference) season by season on the eval's own file lists.

    Args:
        episodes: From ``load_v3_results``.
        points: Fingerprint of a file.
        with_reference: Also run the slow reference and compare exactly.

    Returns:
        The report.
    """
    report = ReproductionReport()
    for season, group in by_season(episodes).items():
        report.seasons += 1
        fps = {e.file: points(e.file) for e in group}
        port = {f: _as_tuple(seg) for f, seg in season_intros(fps).items()}
        if with_reference:
            reference = fp3_reference.analyse_points(fps, sorted(fps))
            for f in sorted(fps):
                if port[f] != _as_tuple(reference[f]["segment"]):
                    report.port_vs_reference.append({"season": season, "file": f, "port": port[f],
                                                     "reference": reference[f]["segment"]})  # fmt: skip
        for e in group:
            report.episodes += 1
            got = port[e.file]
            if _drifted(e.v3_segment, got):
                report.drift.append({"season": season, "file": e.file, "stored": e.v3_segment, "now": got})
            if e.truth_intro is not None:
                report.tally.add(judge_intro(got[:2] if got else None, e.truth_intro))
    return report
```

```python
# tools/markers_eval/__main__.py
"""python -m tools.markers_eval <command>: see tools/markers_eval/README.md."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from media_preview_generator.markers.audio.fingerprint import chromaprint_ffmpeg
from media_preview_generator.markers.probe import ffprobe_path_for

from .cache import FingerprintCache
from .data import load_v3_results
from .intros import SPEC_V3, reproduce


def _cache(args: argparse.Namespace) -> FingerprintCache:
    ffmpeg = chromaprint_ffmpeg(args.ffmpeg)
    if ffmpeg is None:
        sys.exit("No ffmpeg with chromaprint found (pass --ffmpeg)")
    root = Path(args.cache or os.environ.get("MARKERS_EVAL_CACHE") or Path.home() / ".cache/markers_eval")
    return FingerprintCache(root, ffmpeg=ffmpeg, ffprobe=ffprobe_path_for(ffmpeg))


def cmd_reproduce(args: argparse.Namespace) -> int:
    report = reproduce(load_v3_results(), points=_cache(args).points, with_reference=not args.no_reference)
    summary = {
        "seasons": report.seasons,
        "episodes": report.episodes,
        "tally": report.tally.as_dict(),
        "spec": dict(zip(("useful", "wrong", "missed"), SPEC_V3, strict=True)),
        "port_vs_reference": len(report.port_vs_reference),
        "drift": len(report.drift),
        "passed": report.passed,
    }
    print(json.dumps(summary, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps({**summary, "details": {
            "port_vs_reference": report.port_vs_reference, "drift": report.drift}}, indent=1, default=str))  # fmt: skip
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.markers_eval")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("reproduce", help="v3 matcher port vs reference and spec §5.3 on the 118 episodes")
    rep.add_argument("--ffmpeg")
    rep.add_argument("--cache")
    rep.add_argument("--json", help="write details (local-only: holds file paths)")
    rep.add_argument("--no-reference", action="store_true", help="skip the slow pure-Python reference")
    rep.set_defaults(func=cmd_reproduce)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

`tools/markers_eval/README.md`: purpose (spec §10.2, not CI), the data/resource rules paragraph above, and the
`reproduce` command from Step 5.

- [ ] **Step 4: Run the tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers_eval -q`
Expected: PASS.

- [ ] **Step 5: Run the gate on the real episodes (storage, read-only media)**

```bash
cd /home/data/workspace/plex_generate_vid_previews
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval reproduce --ffmpeg /usr/bin/ffmpeg \
  --json docs/design/intro-credits/evidence/eval/phase2_reproduce.json
```
Expected: `"port_vs_reference": 0`, tally useful ≥ 91 and wrong ≤ 13, `"passed": true`, exit 0 (first run
fingerprints ~124 files, a few minutes).

If `port_vs_reference > 0`: Task 2's port is wrong — stop, `superpowers:systematic-debugging` with the listed
season's cached fingerprints, fix Task 2 (new synthetic case reproducing it), re-run. If the port equals the reference
but the tally is below the spec: fingerprints differ from the eval's. Compare one episode's point count and first 20
values between `fingerprint_command` (`-ss 0 -t W -i file`) and the eval's order (`-ss 0 -i file -t W`, see
`evidence/detect/fp.py` `fingerprint`); if the eval's order reproduces the numbers, switch `fingerprint_command` to it
(Task 1 test updated) and add a spec §14 line. If neither does, stop and report to the owner with both tallies — don't
tune matcher parameters.

- [ ] **Step 6: Write the committed summary**

`docs/design/intro-credits/evidence/eval/phase2-harness.md`, section "Reproduction gate (Task 6)": date, commit,
ffmpeg version (`ffmpeg -version | head -1`), seasons/episodes, tally vs spec, port-vs-reference 0, number of drifted
segments and the show names they belong to (no paths). `phase2_reproduce.json` stays local (git-ignored).

- [ ] **Step 7: Commit**

`test(markers): accuracy harness skeleton and v3 matcher reproduction gate`.

---
## Task 7: Season step + season audio detector (+ previous-season hint source)

`[sequential]` (after Tasks 1, 2, 3, 6) `[high-risk]` — spec §5.3 (group = season folder on disk; one other episode is
enough; a season's first episode uses up to 4 episodes of the previous season "as a candidate that still needs a
second source"; "a new episode only fingerprints itself and compares with cached siblings; siblings still without an
intro are re-decided when it arrives"), §5.5 rules 6 and 8, §6.2 step 4, §6.4 items 4–5, §14 2026-09-13 ("A season's
first episode may use the previous season's fingerprints as a hint that needs a second source").

**ADDED 2026-09-14 (phase-1 lab scale run, finding F1):** the season step also checks chapter intros against their
season. Reservation Dogs S01E05/E06 (Disney+) carry "Intro" chapters of 126 s and 87 s that are story, while the
season's other "Intro" chapters are 3–11 s; chapters decide alone at High, so both were published wrong. Rule: when a
season folder has at least 2 *other* episodes with chapter intro candidates, an episode whose chapter intro lasts more
than `max(2 × median(others), median(others) + 30 s)` may not decide from chapters alone: it needs an agreeing
independent source (else Needs review, reason "Intro chapter is much longer than the rest of the season's"). On the
scale run's 247 chapter-decided intros this flags exactly 3: both Reservation Dogs episodes and Mr. Robot S04E01
(88 s vs 14 s), costing at most one correct marker. It is a season-level input, so it must be order-independent:
recompute it in the season step and let the Season follow-up job (Task 8) re-decide siblings when a chapter candidate
in the season appears or changes. Tests: the three flagged cells, a season with fewer than 2 others (no check), a
flagged episode with an agreeing online source (publishes), and order independence (any processing order gives the
same decisions).

**OWNER DECISION 2026-09-14 (supersedes R2 below; owner unsure, controller ruled):** season audio does **not** decide
alone at any setting in phase 2. It counts as one independent source for agreement only (High and Medium). Why: spec
§5.3 measured 13 wrong of 104 answered (12.5%) for the matcher alone, far above the single sources Medium trusts
(chapters, SkipDB exact/shifted). Task 15's harness must report, on the 118-episode set, what Medium would publish
and get wrong with season audio allowed alone; if its wrong rate is no worse than SkipDB's alone, the controller takes
those numbers to the owner to switch it on. The previous-season hint stays agreement-only. Spec §5.5 rule 6 keeps its
wording; Task 16 adds a §14 line.

**OWNER RULING R2 (recommended default implemented here):** a same-season audio match is a source that reads the
file itself, so at "Medium" it may decide alone (ledger ruling 2026-09-14 "local detectors may decide alone at
Medium"; `decide.py` already treats `Source.SEASON_AUDIO` that way). Spec §5.5 rule 6 names only chapters and SkipDB,
so Task 16 amends its wording. The previous-season hint never decides alone at any setting: it is a new
`Source.SEASON_AUDIO_PREVIOUS`, in the same independence group as season audio and agreement-only.

**OWNER RULING R3 (recommended default implemented here):** spec §6.4 item 5 puts the season decision in "the
dispatcher's completion callback". The dispatcher has no such hook, and a season's decision is per episode: the first
episode of a season to reach a worker fingerprints every episode of its folder that has no fingerprint (spec §6.2
step 4), so every other episode of that season decides on the checking thread from cached fingerprints
(`needs_worker` false, Task 3), without a worker slot. Episodes outside the job whose intro is still undecided and whose
answer the new fingerprints change are re-decided by a "Season" follow-up job (Task 8). Task 16 updates §6.4 item 5.

**Group rules (implemented exactly):**
- Episodes = video files (`plex_client.VIDEO_EXTENSIONS`) in the file's folder that are episodes by path
  (`ids_from_path(...).is_episode`) and not extras (`is_extra`), sorted by path, the file itself always included.
- A member whose store record has a different identity than the file on disk is skipped (its own run re-reads it); a
  member the store never saw gets a record (ffprobe duration, `season_key` = folder) and is fingerprinted.
- Same-season matching uses every member with a non-empty fingerprint, in path order; `others` = that count − 1.
- Only when the folder holds just this episode: previous season = the show folder's season folder numbered one lower
  (`Season NN`/`Series NN`/`Staffel NN`/`Saison NN`, any padding), its first 4 episode files by path, **cached
  fingerprints only** (spec: "use the previous season's cached fingerprints"); matched with this episode first, then
  those files in path order (the order `evidence/eval/few_siblings.py` measured).
- Candidate: `Candidate(INTRO, round(start_s*1000), round(end_s*1000), source, confidence=support/others,
  origin=f"{support}/{others}")`; `origin` becomes the evidence label the Inspector shows ("Audio 10/10").
- Signature = sha1 of `[SEASON_AUDIO_VERSION, [[path, size, mtime_ns, has_fingerprint] for every episode (and
  previous-season file when used)]]`; the detector re-runs when it changes (`due`).

**Files:**
- Create: `media_preview_generator/markers/audio/season.py`
- Modify: `media_preview_generator/markers/models.py` (`Source.SEASON_AUDIO_PREVIOUS` appended after `USER`, so the
  enum order tie-break of existing sources is unchanged), `media_preview_generator/markers/decide.py`
  (`_INDEPENDENCE_GROUP`, `_AGREEMENT_ONLY`), `media_preview_generator/markers/pipeline.py` (`_decision_order` ~417;
  `build_context` ~305; new `default_local_detectors`)
- Test: `tests/markers/audio/test_season.py`, `tests/markers/test_decide.py` (new class `TestSeasonAudioSources`),
  `tests/markers/test_pipeline.py` (`build_context` registration tests next to the existing
  `assert ctx.local_detectors == ()` test ~3763, which changes)

**Interfaces:**
- Consumes: Task 1 `ensure_fingerprint`, `points_of`, `chromaprint_ffmpeg`, `FingerprintError`, `WINDOW`,
  `MarkerStore.get_fingerprint/get_season_pair/set_season_pair/get_detector_run/set_detector_run`; Task 2
  `pair_runs`, `file_hits`, `intro_for`, `Run`, `IntroSegment`, `MATCHER_VERSION`; Task 3 `LocalDetectorSpec(stores,
  version, due, needs_worker)`, `DetectorUnavailableError`, `PipelineContext.request_followups`.
- Produces (Tasks 8, 12, 15):
```python
# markers/models.py
Source.SEASON_AUDIO_PREVIOUS = "season_audio_previous"
# markers/audio/season.py
SEASON_AUDIO_VERSION = MATCHER_VERSION
MAX_PREVIOUS_SEASON_FILES = 4
@dataclass(frozen=True) class SeasonGroup: folder: str; episodes: tuple[str, ...]
def season_group(canonical_path: str) -> SeasonGroup
def previous_season_files(canonical_path: str) -> tuple[str, ...]
def season_audio_due(rec: FileRecord, ctx: PipelineContext) -> bool
def season_audio_needs_worker(rec: FileRecord, ctx: PipelineContext) -> bool
def detect_season_audio(rec: FileRecord, *, ctx: PipelineContext, gpu: str | None = None, gpu_device_path: str | None = None,
                        phase_callback: Callable[[str], None] | None = None, cancel_check: Callable[[], bool] | None = None,
                        pause_check: Callable[[], bool] | None = None) -> list[Candidate]
def season_audio_spec(ffmpeg_path: str | None) -> LocalDetectorSpec | None
# markers/pipeline.py
def default_local_detectors(settings: GlobalMarkersSettings, config: Any) -> tuple[LocalDetectorSpec, ...]
```

- [ ] **Step 1: Write the failing decision-rule tests**

Append to `tests/markers/test_decide.py` (it already imports `Candidate`, `Source`, `MarkerType`, `DecisionContext`,
`DecisionStatus`, `decide`; add what's missing):

```python
class TestSeasonAudioSources:
    """Spec §5.3/§5.5: same-season audio reads the file (decides alone at Medium); the previous-season hint never does
    and is the same independent source as season audio."""

    DUR = 1_321_472
    ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
             "server_markers", "server_markers_imported")  # fmt: skip

    def _ctx(self, publish_when):
        return DecisionContext(self.DUR, False, publish_when, frozenset({MarkerType.INTRO}), self.ORDER)

    def test_same_season_audio_alone_decides_at_medium_only(self):
        c = [Candidate(MarkerType.INTRO, 126_000, 157_000, Source.SEASON_AUDIO, 1.0, "10/10")]
        assert decide(c, self._ctx("medium"), {})[MarkerType.INTRO].status is DecisionStatus.DECIDED
        assert decide(c, self._ctx("high"), {})[MarkerType.INTRO].status is DecisionStatus.NEEDS_REVIEW

    @pytest.mark.parametrize("publish_when", ["high", "medium"])
    def test_previous_season_hint_alone_never_decides(self, publish_when):
        c = [Candidate(MarkerType.INTRO, 126_000, 157_000, Source.SEASON_AUDIO_PREVIOUS, 1.0, "4/4")]
        assert decide(c, self._ctx(publish_when), {})[MarkerType.INTRO].status is DecisionStatus.NEEDS_REVIEW

    def test_hint_and_same_season_audio_are_one_source(self):
        c = [
            Candidate(MarkerType.INTRO, 126_000, 157_000, Source.SEASON_AUDIO_PREVIOUS, 1.0, "4/4"),
            Candidate(MarkerType.INTRO, 126_500, 157_500, Source.SEASON_AUDIO, 1.0, "1/1"),
        ]
        assert decide(c, self._ctx("high"), {})[MarkerType.INTRO].status is DecisionStatus.NEEDS_REVIEW

    def test_hint_confirmed_by_an_independent_source_decides_at_high(self):
        c = [
            Candidate(MarkerType.INTRO, 126_000, 157_000, Source.SEASON_AUDIO_PREVIOUS, 1.0, "4/4"),
            Candidate(MarkerType.INTRO, 127_000, 158_800, Source.SKIPDB, 0.9),
        ]
        d = decide(c, self._ctx("high"), {})[MarkerType.INTRO]
        assert d.status is DecisionStatus.DECIDED
        assert d.marker.decided_by == ("skipdb", "season_audio_previous")
        # The agreed end comes from the first agreeing source in the user's order (SkipDB), the start is the later one.
        assert (d.marker.start_ms, d.marker.end_ms) == (127_000, 158_800)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_decide.py -k SeasonAudio -q`
Expected: FAIL — `AttributeError: SEASON_AUDIO_PREVIOUS`.

- [ ] **Step 3: Add the source**

`models.py`, after `USER = "user"`:

```python
    # A season's only episode matched against the previous season's cached fingerprints (spec §5.3): a hint that
    # needs a second source. Last in the enum so the order tie-break of the other sources doesn't move.
    SEASON_AUDIO_PREVIOUS = "season_audio_previous"
```

`decide.py`:

```python
_INDEPENDENCE_GROUP = {
    Source.INTRODB: _INTRODB_GROUP,
    Source.THEINTRODB: _INTRODB_GROUP,
    Source.SERVER_MARKERS_IMPORTED: _INTRODB_GROUP,
    # The previous season's audio is the same method on the same show: never a second opinion for season audio.
    Source.SEASON_AUDIO_PREVIOUS: Source.SEASON_AUDIO.value,
}
# At "Medium" a lone source publishes only when it checks this file's cut itself (rule 6): IntroDB takes no duration,
# TheIntroDB answers the closest cut it has, markers already on servers never decide alone (rule 7), and the previous
# season's audio is a hint (spec §5.3).
_AGREEMENT_ONLY = SERVER_SOURCES | {Source.INTRODB, Source.THEINTRODB, Source.SEASON_AUDIO_PREVIOUS}
```

`pipeline.py` `_decision_order`:

```python
def _decision_order(settings: GlobalMarkersSettings) -> tuple[str, ...]:
    """Enabled sources in the user's order; importer-plugin copies ride on the server-markers switch and the
    previous-season hint on the season-audio switch, each ranked right after its switch."""
    riders = {
        Source.SERVER_MARKERS.value: Source.SERVER_MARKERS_IMPORTED.value,
        Source.SEASON_AUDIO.value: Source.SEASON_AUDIO_PREVIOUS.value,
    }
    order: list[str] = []
    for source_id in settings.ordered_enabled_sources():
        order.append(source_id)
        if source_id in riders:
            order.append(riders[source_id])
    return tuple(order)
```

Run the decide tests again: PASS. Run `tests/markers/test_decide.py` whole: PASS (no existing cell moves).

- [ ] **Step 4: Write the failing season tests**

```python
# tests/markers/audio/test_season.py
"""Season step: group listing, previous season, whole-season fingerprinting, inline matching, weekly releases."""

from __future__ import annotations

import os
import re
import zlib
from unittest.mock import patch

import numpy as np
import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.audio import POINT_S, fingerprint, season
from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import FileIdentity, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.probe import MediaProbe
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import ready_publisher
from tests.markers.test_pipeline import _ctx, _registry, _run

DUR = 1_321_472
N_POINTS = int(fingerprint.window_s(DUR) / POINT_S)
INTRO = np.random.default_rng(42).integers(0, 2**32, size=240, dtype=np.uint64).astype("<u4")
OFFSETS = {"S01E01": 300, "S01E02": 520, "S01E03": 710, "S01E04": 90, "S02E01": 400, "S02E02": 900, "S02E03": 150}
MEDIUM = {"sources": [{"id": "theintrodb", "enabled": False}], "detect": {"intro": True, "credits": False},
          "publish_when": "medium"}  # fmt: skip
HIGH = {**MEDIUM, "publish_when": "high"}


def fake_points(path: str) -> np.ndarray:
    key = re.search(r"S\d\dE\d\d", path).group(0)
    rng = np.random.default_rng(zlib.crc32(key.encode()))
    body = rng.integers(0, 2**32, size=N_POINTS, dtype=np.uint64).astype("<u4")
    body[OFFSETS[key] : OFFSETS[key] + 240] = INTRO
    return body


def planted_ms(path: str) -> tuple[int, int]:
    at = OFFSETS[re.search(r"S\d\dE\d\d", path).group(0)]
    return round(at * POINT_S * 1000), round((at + 239) * POINT_S * 1000)


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


@pytest.fixture
def show(tmp_path):
    root = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}"

    def make(season_no: int, episodes: int) -> list[str]:
        folder = root / f"Season {season_no:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for e in range(1, episodes + 1):
            p = folder / f"Show (2020) - S{season_no:02d}E{e:02d}.mkv"
            if not p.exists():  # adding an episode later must not touch the ones already there
                p.write_bytes(b"x" * (100 + e))
            paths.append(str(p))
        return paths

    return make


class _Audio:
    """Patches ffmpeg (fingerprints), ffprobe of other episodes and the chromaprint check."""

    def __init__(self, fail: set[str] | None = None):
        self.computed: list[str] = []
        self.fail = fail or set()

    def compute(self, path, duration_ms, *, ffmpeg, cancel_check=None):
        self.computed.append(path)
        if path in self.fail:
            raise fingerprint.FingerprintError("ffmpeg exited 1")
        return fake_points(path)

    def __enter__(self):
        self._patches = [
            patch.object(fingerprint, "compute_fingerprint", side_effect=self.compute),
            patch.object(season, "probe_media", return_value=MediaProbe(DUR, ())),
            patch.object(season, "chromaprint_ffmpeg", return_value="/usr/lib/jellyfin-ffmpeg/ffmpeg"),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _spec():
    with patch.object(season, "chromaprint_ffmpeg", return_value="/usr/lib/jellyfin-ffmpeg/ffmpeg"):
        return season.season_audio_spec("/usr/lib/jellyfin-ffmpeg/ffmpeg")


def _season_ctx(store, path, raw=MEDIUM):
    return _ctx(store, _registry(path, ServerType.PLEX), detectors=(_spec(),), settings_raw=raw)


def _evidence(store, path, source):
    rec = store.get_file(path)
    return [c for c in store.get_evidence(rec.id) if c.source is source]


class TestGroup:
    def test_episodes_of_the_folder_sorted_without_extras_or_other_files(self, show):
        e1, e2 = show(2, 2)
        folder = os.path.dirname(e1)
        for name in ("Show (2020) - S02E02-sample.mkv", "notes.txt", "Show - Making Of.mkv"):
            open(os.path.join(folder, name), "wb").close()
        os.mkdir(os.path.join(folder, "Extras"))
        assert season.season_group(e2) == season.SeasonGroup(folder, (e1, e2))

    def test_previous_season_is_the_first_four_episodes_of_the_season_numbered_one_lower(self, show):
        s1 = show(1, 6)
        (s2e1,) = show(2, 1)
        assert season.previous_season_files(s2e1) == tuple(s1[:4])
        assert season.previous_season_files(s1[0]) == ()

    def test_previous_season_folder_with_other_padding_is_found(self, tmp_path):
        prev = tmp_path / "media" / "tv" / "Show {tvdb-1}" / "Season 1"
        cur = tmp_path / "media" / "tv" / "Show {tvdb-1}" / "Season 02"
        prev.mkdir(parents=True), cur.mkdir(parents=True)
        (prev / "Show - S01E01.mkv").write_bytes(b"x")
        (cur / "Show - S02E01.mkv").write_bytes(b"x")
        assert season.previous_season_files(str(cur / "Show - S02E01.mkv")) == (str(prev / "Show - S01E01.mkv"),)

    def test_specials_have_no_previous_season(self, tmp_path):
        folder = tmp_path / "media" / "tv" / "Show {tvdb-1}" / "Specials"
        folder.mkdir(parents=True)
        (folder / "Show - S00E01.mkv").write_bytes(b"x")
        assert season.previous_season_files(str(folder / "Show - S00E01.mkv")) == ()


class TestSeasonAudio:
    def test_first_episode_fingerprints_the_season_and_finds_its_intro(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio() as audio:
            out, _ = _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        assert sorted(audio.computed) == [e1, e2, e3]
        (cand,) = _evidence(store, e1, Source.SEASON_AUDIO)
        start, end = planted_ms(e1)
        assert abs(cand.start_ms - start) <= 500 and abs(cand.end_ms - end) <= 500
        assert cand.origin == "2/2" and cand.confidence == 1.0
        assert out.outcome_key == FileOutcome.PUBLISHED.value  # Medium: same-season audio decides alone
        assert ctx.take_followups() == [e2, e3]
        assert all(store.get_fingerprint(store.get_file(p).id, "intro") is not None for p in (e1, e2, e3))

    def test_the_rest_of_the_season_matches_on_the_checking_thread(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio() as audio:
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            ctx.take_followups()
            audio.computed.clear()
            out, _ = _run(ctx, e2, {"plex-1": ready_publisher()})  # check stage
        assert out is not None and audio.computed == []
        (cand,) = _evidence(store, e2, Source.SEASON_AUDIO)
        assert cand.origin == "2/2"
        assert ctx.take_followups() == [e3]  # e1 already ran with this season's fingerprints

    def test_at_high_a_lone_audio_match_needs_review(self, store, show):
        e1, _, _ = show(1, 3)
        with _Audio():
            out, _ = _run(_season_ctx(store, e1, HIGH), e1, {"plex-1": ready_publisher()}, stage="process")
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value

    def test_a_current_answer_is_not_matched_again(self, store, show):
        e1, _, _ = show(1, 3)
        ctx = _season_ctx(store, e1, HIGH)
        with _Audio(), patch.object(season, "pair_runs", wraps=season.pair_runs) as runs:
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            first = runs.call_count
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        assert first == 2 and runs.call_count == 2

    def test_pairs_are_cached_the_same_way_round(self, store, show):
        e1, e2 = show(1, 2)
        ctx = _season_ctx(store, e1, HIGH)
        with _Audio():
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
            with patch.object(season, "pair_runs", side_effect=AssertionError("recomputed")):
                _run(ctx, e2, {"plex-1": ready_publisher()})
        a, b = store.get_file(e1), store.get_file(e2)
        assert store.get_season_pair(a.id, b.id, 3) is not None and store.get_season_pair(b.id, a.id, 3) is None


class TestWeeklyReleases:
    def _cache_previous(self, store, paths):
        for p in paths:
            st = os.stat(p)
            rec = store.upsert_file(FileIdentity(p, st.st_size, st.st_mtime_ns), duration_ms=DUR,
                                    season_key=os.path.dirname(p), is_movie=False)  # fmt: skip
            store.set_fingerprint(rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0,
                                  length_s=fingerprint.window_s(DUR), algorithm=1, points=fake_points(p).tobytes())  # fmt: skip

    def test_a_new_seasons_only_episode_gets_a_hint_from_the_cached_previous_season(self, store, show):
        s1 = show(1, 4)
        (s2e1,) = show(2, 1)
        self._cache_previous(store, s1)
        with _Audio() as audio:
            out, _ = _run(_season_ctx(store, s2e1), s2e1, {"plex-1": ready_publisher()}, stage="process")
        assert audio.computed == [s2e1]
        assert _evidence(store, s2e1, Source.SEASON_AUDIO) == []
        (hint,) = _evidence(store, s2e1, Source.SEASON_AUDIO_PREVIOUS)
        assert hint.origin == "4/4"
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value  # a hint needs a second source even at Medium

    def test_no_cached_previous_season_gives_no_hint_and_fingerprints_nothing_else(self, store, show):
        show(1, 4)
        (s2e1,) = show(2, 1)
        with _Audio() as audio:
            _run(_season_ctx(store, s2e1), s2e1, {"plex-1": ready_publisher()}, stage="process")
        assert audio.computed == [s2e1] and _evidence(store, s2e1, Source.SEASON_AUDIO_PREVIOUS) == []

    def test_the_second_episode_arriving_re_decides_the_first(self, store, show):
        self._cache_previous(store, show(1, 4))
        (s2e1,) = show(2, 1)
        ctx = _season_ctx(store, s2e1)
        with _Audio():
            _run(ctx, s2e1, {"plex-1": ready_publisher()}, stage="process")
            s2e2 = show(2, 2)[1]
            assert season.season_audio_due(store.get_file(s2e1), ctx) is True
            _run(ctx, s2e2, {"plex-1": ready_publisher()}, stage="process")
            assert ctx.take_followups() == [s2e1]
            out, _ = _run(ctx, s2e1, {"plex-1": ready_publisher()})  # the Season follow-up job's check stage
        (cand,) = _evidence(store, s2e1, Source.SEASON_AUDIO)
        assert cand.origin == "1/1" and _evidence(store, s2e1, Source.SEASON_AUDIO_PREVIOUS) == []
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        decided = store.get_decisions(store.get_file(s2e1).id)[MarkerType.INTRO]
        assert decided.status is DecisionStatus.DECIDED


class TestFailures:
    def test_the_episodes_own_fingerprint_failing_stores_nothing(self, store, show):
        e1, _, _ = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio(fail={e1}):
            out, _ = _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        rec = store.get_file(e1)
        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) is None
        assert store.get_detector_run(rec.id, Source.SEASON_AUDIO) is None

    def test_a_sibling_that_fails_is_left_out_and_makes_the_answer_due_again(self, store, show):
        e1, e2, e3 = show(1, 3)
        ctx = _season_ctx(store, e1)
        with _Audio(fail={e3}):
            _run(ctx, e1, {"plex-1": ready_publisher()}, stage="process")
        (cand,) = _evidence(store, e1, Source.SEASON_AUDIO)
        assert cand.origin == "1/1"
        assert season.season_audio_due(store.get_file(e1), ctx) is False
        assert season.season_audio_needs_worker(store.get_file(e1), ctx) is True  # e3 still has no fingerprint

    def test_a_sibling_changed_on_disk_is_not_read_or_rewritten(self, store, show):
        e1, e2, _ = show(1, 3)
        old = store.upsert_file(FileIdentity(e2, 1, 1), duration_ms=DUR, season_key=os.path.dirname(e2), is_movie=False)
        with _Audio() as audio:
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
        assert e2 not in audio.computed
        assert store.get_file(e2) == old

    def test_no_chromaprint_registers_no_detector(self, loguru_caplog):
        from media_preview_generator.markers.settings import load_global, validate_global

        settings = load_global(validate_global({}, None)[0])
        with patch.object(season, "chromaprint_ffmpeg", return_value=None):
            assert season.season_audio_spec("/usr/local/bin/ffmpeg") is None
            assert pipeline.default_local_detectors(settings, type("Cfg", (), {"ffmpeg_path": "/usr/local/bin/ffmpeg"})()) == ()
        assert "chromaprint" in loguru_caplog.text

    def test_season_audio_switched_off_registers_no_detector(self):
        from media_preview_generator.markers.settings import load_global, validate_global

        settings = load_global(validate_global({"sources": [{"id": "season_audio", "enabled": False}]}, None)[0])
        with patch.object(season, "chromaprint_ffmpeg", return_value="/ffmpeg"):
            assert pipeline.default_local_detectors(settings, type("Cfg", (), {"ffmpeg_path": "/ffmpeg"})()) == ()
```

(`loguru_caplog` comes from `tests/markers/conftest.py`, which applies to `tests/markers/audio/` too.)

- [ ] **Step 5: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/audio/test_season.py -q`
Expected: FAIL — `ImportError: cannot import name 'season'`.

- [ ] **Step 6: Implement `season.py`**

```python
# media_preview_generator/markers/audio/season.py
"""Season audio: the v3 matcher over a season folder's episodes (spec §5.3, §6.2 step 4, §6.4 items 4–5).

Fingerprinting needs a worker (CPU ffmpeg, at most two at once): the first episode of a season to get one fingerprints
every episode of its folder that has none, so the rest of the season matches from cached fingerprints on the checking
threads. An episode alone in its folder (a new season's first weekly release) matches against up to four cached
episodes of the previous season; that answer is a hint (``Source.SEASON_AUDIO_PREVIOUS``) that never decides alone.
Episodes whose intro is still undecided and whose answer the new fingerprints change are handed to the job as
follow-ups, which a "Season" job decides again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from ...plex_client import VIDEO_EXTENSIONS
from ..decide import DecisionStatus
from ..external_ids import ids_from_path, is_extra
from ..models import Candidate, FileIdentity, MarkerType, Source
from ..probe import ProbeError, probe_media
from .fingerprint import WINDOW, FingerprintError, chromaprint_ffmpeg, ensure_fingerprint, points_of
from .matcher import MATCHER_VERSION, IntroSegment, Run, file_hits, intro_for, pair_runs

if TYPE_CHECKING:
    from ..pipeline import LocalDetectorSpec, PipelineContext
    from ..store import FileRecord

SEASON_AUDIO_VERSION = MATCHER_VERSION
MAX_PREVIOUS_SEASON_FILES = 4
_SEASON_FOLDER_RE = re.compile(r"^(?:season|series|staffel|saison)\s*(\d{1,4})$", re.IGNORECASE)


@dataclass(frozen=True)
class SeasonGroup:
    """A season folder and its episode files (sorted, the asking file included)."""

    folder: str
    episodes: tuple[str, ...]


def _is_episode_file(path: str) -> bool:
    return (
        os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS
        and not is_extra(path)
        and ids_from_path(path).is_episode
        and os.path.isfile(path)
    )


def season_group(canonical_path: str) -> SeasonGroup:
    """The episodes sharing a file's season folder on disk (the group is server-agnostic, spec §5.3)."""
    folder = os.path.dirname(canonical_path)
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    episodes = {canonical_path} | {p for p in (os.path.join(folder, n) for n in names) if _is_episode_file(p)}
    return SeasonGroup(folder, tuple(sorted(episodes)))


def previous_season_files(canonical_path: str) -> tuple[str, ...]:
    """The first episodes (by path, at most 4) of the season folder numbered one lower than this file's."""
    folder = os.path.dirname(canonical_path)
    match = _SEASON_FOLDER_RE.match(os.path.basename(folder))
    if not match or int(match.group(1)) <= 1:
        return ()
    wanted, show = int(match.group(1)) - 1, os.path.dirname(folder)
    try:
        names = sorted(os.listdir(show))
    except OSError:
        return ()
    for name in names:
        other = _SEASON_FOLDER_RE.match(name)
        previous = os.path.join(show, name)
        if other and int(other.group(1)) == wanted and os.path.isdir(previous):
            files = sorted(p for p in (os.path.join(previous, n) for n in os.listdir(previous)) if _is_episode_file(p))
            return tuple(files[:MAX_PREVIOUS_SEASON_FILES])
    return ()


def _disk_identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _current_record(ctx: PipelineContext, path: str) -> FileRecord | None:
    rec = ctx.store.get_file(path)
    return rec if rec is not None and _disk_identity(path) == (rec.size, rec.mtime_ns) else None


def _cached_points(ctx: PipelineContext, rec: FileRecord | None) -> np.ndarray | None:
    stored = ctx.store.get_fingerprint(rec.id, WINDOW) if rec is not None else None
    return points_of(stored) if stored is not None else None


def _member_record(ctx: PipelineContext, path: str) -> FileRecord | None:
    """A season member's record: the store's when it matches the disk, a new one for a file never seen; None for a
    file that changed since it was read (its own run reads it again) or can't be probed."""
    identity = _disk_identity(path)
    rec = ctx.store.get_file(path)
    if identity is None or (rec is not None and identity != (rec.size, rec.mtime_ns)):
        return None
    if rec is not None and rec.duration_ms:
        return rec
    try:
        duration_ms = probe_media(path, ffprobe=ctx.ffprobe).duration_ms
    except ProbeError as exc:
        logger.debug("Season audio skips {}: {}", os.path.basename(path), exc)
        return None
    if not duration_ms:
        return None
    return ctx.store.upsert_file(
        FileIdentity(path, *identity),
        duration_ms=duration_ms,
        season_key=rec.season_key if rec else os.path.dirname(path),
        is_movie=rec.is_movie if rec else False,
    )


def _signature_paths(canonical_path: str, group: SeasonGroup) -> tuple[str, ...]:
    if len(group.episodes) == 1:
        return group.episodes + previous_season_files(canonical_path)
    return group.episodes


def _signature(ctx: PipelineContext, paths: tuple[str, ...]) -> str:
    items = []
    for path in paths:
        identity = _disk_identity(path) or (None, None)
        rec = _current_record(ctx, path)
        has_fingerprint = rec is not None and ctx.store.get_fingerprint(rec.id, WINDOW) is not None
        items.append([path, identity[0], identity[1], has_fingerprint])
    payload = json.dumps([SEASON_AUDIO_VERSION, items])
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()


def season_audio_due(rec: FileRecord, ctx: PipelineContext) -> bool:
    """Whether the season's files or their fingerprints changed since this episode's answer."""
    group = season_group(rec.canonical_path)
    current = _signature(ctx, _signature_paths(rec.canonical_path, group))
    return ctx.store.get_detector_run(rec.id, Source.SEASON_AUDIO) != current


def season_audio_needs_worker(rec: FileRecord, ctx: PipelineContext) -> bool:
    """Whether an episode of the folder (this one included) still needs ffmpeg or ffprobe; otherwise matching is
    cheap enough for a checking thread."""
    for path in season_group(rec.canonical_path).episodes:
        stored = ctx.store.get_file(path)
        if stored is None:
            return True
        if _disk_identity(path) != (stored.size, stored.mtime_ns):
            continue
        if ctx.store.get_fingerprint(stored.id, WINDOW) is None:
            return True
    return False


def _intro(
    ctx: PipelineContext,
    target: str,
    files: list[str],
    records: dict[str, FileRecord],
    points: dict[str, np.ndarray],
) -> IntroSegment | None:
    def runs_between(first: str, second: str) -> list[Run]:
        a, b = records[first], records[second]
        cached = ctx.store.get_season_pair(a.id, b.id, MATCHER_VERSION)
        if cached is not None:
            return [Run(*run) for run in cached]
        runs = pair_runs(points[first], points[second])
        ctx.store.set_season_pair(a.id, b.id, MATCHER_VERSION, [tuple(run) for run in runs])
        return runs

    return intro_for(file_hits(target, files, runs_between), len(files) - 1)


def _candidate(segment: IntroSegment, others: int, source: Source) -> Candidate:
    return Candidate(
        MarkerType.INTRO,
        int(round(segment.start_s * 1000)),
        int(round(segment.end_s * 1000)),
        source,
        confidence=segment.support / others,
        origin=f"{segment.support}/{others}",
    )


def _request_redecide(ctx: PipelineContext, rec: FileRecord, members: dict[str, FileRecord], signature: str) -> None:
    stale = []
    for path, member in members.items():
        if member.id == rec.id or os.path.dirname(path) != os.path.dirname(rec.canonical_path):
            continue
        if ctx.store.get_detector_run(member.id, Source.SEASON_AUDIO) == signature:
            continue
        intro = ctx.store.get_decisions(member.id).get(MarkerType.INTRO)
        if intro is not None and intro.status is DecisionStatus.DECIDED:
            continue
        stale.append(path)
    if stale:
        ctx.request_followups(stale)


def detect_season_audio(
    rec: FileRecord,
    *,
    ctx: PipelineContext,
    gpu: str | None = None,
    gpu_device_path: str | None = None,
    phase_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
) -> list[Candidate]:
    """Match this episode's opening against its season (or, alone, the previous season's cached episodes).

    Args:
        rec: The episode (its identity matches the disk: the pipeline just checked).
        ctx: The job's context.
        gpu: Unused (chromaprint is CPU only).
        gpu_device_path: Unused.
        phase_callback: Worker row step text.
        cancel_check: True once the job is cancelled.
        pause_check: Unused (a paused job doesn't block a worker).

    Returns:
        At most one intro candidate (season audio, or the previous-season hint).

    Raises:
        DetectorUnavailableError: No chromaprint ffmpeg, this episode couldn't be fingerprinted, or cancelled.
    """
    from ..pipeline import DetectorUnavailableError

    phase = phase_callback or (lambda _text: None)
    ffmpeg = chromaprint_ffmpeg(getattr(ctx.config, "ffmpeg_path", None))
    if ffmpeg is None:
        raise DetectorUnavailableError("no ffmpeg with chromaprint")
    group = season_group(rec.canonical_path)
    phase("Fingerprinting audio…")
    try:
        own = ensure_fingerprint(ctx.store, rec, ffmpeg=ffmpeg, cancel_check=cancel_check)
    except FingerprintError as exc:
        raise DetectorUnavailableError(str(exc)) from exc
    if own is None:
        raise DetectorUnavailableError("the file changed while it was fingerprinted")
    records: dict[str, FileRecord] = {rec.canonical_path: rec}
    points: dict[str, np.ndarray] = {rec.canonical_path: own}
    others = [p for p in group.episodes if p != rec.canonical_path]
    for n, path in enumerate(others, 1):
        if cancel_check and cancel_check():
            raise DetectorUnavailableError("cancelled")
        member = _member_record(ctx, path)
        if member is None:
            continue
        cached = _cached_points(ctx, member)
        if cached is None:
            phase(f"Fingerprinting season audio {n}/{len(others)}…")
            try:
                cached = ensure_fingerprint(ctx.store, member, ffmpeg=ffmpeg, cancel_check=cancel_check)
            except FingerprintError as exc:
                logger.info("Season audio leaves out {} this time: {}", os.path.basename(path), exc)
                continue
        if cached is not None:
            records[path], points[path] = member, cached

    phase("Matching season audio…")
    candidates: list[Candidate] = []
    audible = sorted(p for p, pts in points.items() if len(pts))
    if len(own) and len(audible) > 1:
        segment = _intro(ctx, rec.canonical_path, audible, records, points)
        if segment is not None:
            candidates.append(_candidate(segment, len(audible) - 1, Source.SEASON_AUDIO))
    elif len(own) and len(group.episodes) == 1:
        previous: dict[str, np.ndarray] = {}
        for path in previous_season_files(rec.canonical_path):
            member = _current_record(ctx, path)
            cached = _cached_points(ctx, member)
            if member is not None and cached is not None and len(cached):
                records[path], previous[path] = member, cached
        if previous:
            files = [rec.canonical_path, *sorted(previous)]
            segment = _intro(ctx, rec.canonical_path, files, records, {**points, **previous})
            if segment is not None:
                candidates.append(_candidate(segment, len(previous), Source.SEASON_AUDIO_PREVIOUS))

    signature = _signature(ctx, _signature_paths(rec.canonical_path, group))
    ctx.store.set_detector_run(rec.id, Source.SEASON_AUDIO, signature)
    _request_redecide(ctx, rec, records, signature)
    return candidates


def season_audio_spec(ffmpeg_path: str | None) -> LocalDetectorSpec | None:
    """The season audio detector, or None when no ffmpeg with chromaprint exists (source unavailable)."""
    from ..pipeline import LocalDetectorSpec

    if chromaprint_ffmpeg(ffmpeg_path) is None:
        return None
    return LocalDetectorSpec(
        source=Source.SEASON_AUDIO,
        types=frozenset({MarkerType.INTRO}),
        detect=detect_season_audio,
        stores=frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}),
        version=SEASON_AUDIO_VERSION,
        due=season_audio_due,
        needs_worker=season_audio_needs_worker,
    )
```

(`set_detector_run` is written before the pipeline stores the candidates. If the store write of the candidates then
fails, the next run's `_detector_due` still sees no evidence version and asks again, so no answer is lost.)

- [ ] **Step 7: Register the detector**

In `pipeline.py` add:

```python
def default_local_detectors(settings: GlobalMarkersSettings, config: Any) -> tuple[LocalDetectorSpec, ...]:
    """The local detectors a job uses: season audio when its source is on and an ffmpeg with chromaprint exists.

    Args:
        settings: Global detection settings.
        config: The job's ``Config`` (its ``ffmpeg_path``).

    Returns:
        The detector specs, in no particular order (the pipeline runs them at their source's place).
    """
    detectors: list[LocalDetectorSpec] = []
    if settings.source_enabled(Source.SEASON_AUDIO.value):
        from .audio.season import season_audio_spec

        spec = season_audio_spec(getattr(config, "ffmpeg_path", None))
        if spec is None:
            logger.warning(
                "Season audio matching is on, but no ffmpeg with chromaprint was found; TV intros come from the other "
                "sources only"
            )
        else:
            detectors.append(spec)
    return tuple(detectors)
```

and in `build_context` pass `local_detectors=default_local_detectors(settings, config)` to `PipelineContext`. Change
the existing `tests/markers/test_pipeline.py` test that asserts `ctx.local_detectors == ()` to patch
`media_preview_generator.markers.audio.season.chromaprint_ffmpeg` to return None (and keep its assertion), and add a
sibling test with it returning a path that asserts `[s.source for s in ctx.local_detectors] == [Source.SEASON_AUDIO]`.

- [ ] **Step 8: Run the tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers -q`
Expected: PASS.

- [ ] **Step 9: Real-data check (read-only, storage)**

With the harness cache from Task 6 already holding the fingerprints: a throwaway script in the scratchpad (not
committed) runs `check_item`/`process_item` through `docs/design/intro-credits/evidence/audit-phase1/harness.py`'s
`ctx_for` + `run` (fake servers, a temp `CONFIG_DIR`) with `detectors=(season_audio_spec("/usr/bin/ffmpeg"),)` over
the 124 episodes of `eval_results_v3.json`, patching `fingerprint.compute_fingerprint` to read the Task 6
`FingerprintCache` instead of running ffmpeg again. Pass when every episode's stored `season_audio` candidate equals
the Task 6 port segment (ms rounding) for the same file list — the eval used each season's first 8 files, so run it on
temp folders of symlinks to exactly those files (symlinks live in the scratchpad; the media is only read). Report any
difference in the task report.

- [ ] **Step 10: Mutation self-check (high-risk)**

One at a time, confirm a test fails, revert: `len(group.episodes) == 1` → `len(audible) == 1` (previous season used
when siblings are silent); drop `Source.SEASON_AUDIO_PREVIOUS` from `_AGREEMENT_ONLY`; `files = [rec, *sorted(previous)]`
→ `sorted([rec, *previous])`; skip the identity check in `_member_record`; `ctx.request_followups(stale)` for decided
siblings too; `season_audio_needs_worker` returning False for a never-seen sibling.

- [ ] **Step 11: Commit**

`feat(markers): season audio intro detection with previous-season hints`.

---
## Task 8: Season follow-up jobs + vendor-webhook season grouping

`[sequential]` (after Task 7) `[high-risk]` (job engine) — spec §5.3 ("siblings still without an intro are re-decided
when it arrives"), §6.2 step 1 ("an Intro & Credits job for the same files at NORMAL, grouped by season folder"),
§6.4 item 9; ledger parked item "per-episode vendor webhook follow-ups not season-grouped" (progress.md line 236;
`.superpowers/sdd/plan-phase1/audit-A-report.md` "LOW — Per-file vendor webhooks create one Intro & Credits job per
episode", including its fix note: the runner must re-read `job.config` after waiting).

Two changes:
1. **Season follow-up job.** After an Intro & Credits job finishes (not cancelled, not itself a Season job), the files
   the season detector asked about (`ctx.take_followups()`) that weren't items of this job are queued as one job:
   `source="season"`, the job's priority, capped at `MAX_RETRY_FILES` (500), named
   `Season: <show folder> · <season folder>` (or `Season: N seasons`). A Season job never queues another Season job,
   never retries a file missing from disk and never queues a verify job.
2. **Season grouping of webhook follow-ups.** Plex/Emby/Jellyfin `library.new` webhooks arrive one episode at a time.
   An episode whose season folder is already covered by a webhook follow-up that hasn't read its files yet joins that
   job (its `file_paths` and item-id hints) instead of creating another. Only episodes are grouped (a flat folder of
   movies isn't a season). The runner reads the follow-up's config again right before it lists the files, and marks it
   `files_sealed`, under the same lock the webhook side takes, so a file joins exactly one job.

A joined episode's own preview job may still be running when the joined job runs; markers don't need previews
(ledger ruling 2026-09-13 "a preview job PENDING with progress.retry_eta counts as finished"), so that's accepted.

**Files:**
- Modify: `media_preview_generator/markers/job_runner.py` (constants, `_queue_season_followups`, the config re-read +
  seal before `build_items`, the follow-up call after `_complete`, `sent_files`), `media_preview_generator/markers/triggers.py`
  (use `job_runner.FOLLOW_UP_LOCK` instead of `_follow_up_lock`; `_season_folders`, `_joinable_follow_ups`, `_join`;
  `submit_webhook_follow_up`)
- Test: `tests/markers/test_season_followups.py` (new); update assertions in `tests/markers/test_job_runner.py` and
  `tests/markers/test_triggers.py` only where a follow-up config now carries `files_sealed` (list them in the report)

**Interfaces:**
- Consumes: Task 3 `PipelineContext.take_followups() -> list[str]`; phase 1 `create_intro_credits_job`,
  `JobManager.update_job_config(job_id, config)`, `get_pending_jobs()`, `ids_from_path`.
- Produces:
```python
# markers/job_runner.py
SEASON_SOURCE = "season"
FILES_SEALED = "files_sealed"
FOLLOW_UP_LOCK: threading.Lock
def _queue_season_followups(job, paths: list[str]) -> None
# markers/triggers.py
def _season_folders(paths: Iterable[str], configs: list[ServerConfig]) -> set[str]
def _joinable_follow_ups(jm, configs: list[ServerConfig]) -> list[tuple[Job, set[str]]]
def _join(jm, job: Job, paths: list[str], hints: dict[str, dict[str, str]] | None) -> None
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/markers/test_season_followups.py
"""Season follow-up jobs after a job, and season grouping of one-episode webhook follow-ups."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers import job_runner, triggers
from tests.markers.test_job_runner import _item, env  # noqa: F401 - env is a fixture
from tests.markers.test_triggers import _server, settings  # noqa: F401 - settings is a fixture

SHOW = "/media/tv/Show (2020) {tvdb-1}"
S1, S2 = f"{SHOW}/Season 01", f"{SHOW}/Season 02"


def ep(season_folder: str, e: int) -> str:
    n = int(season_folder[-2:])
    return f"{season_folder}/Show (2020) - S{n:02d}E{e:02d}.mkv"


class TestSeasonFollowUpJob:
    def _run(self, env, items, followups):
        env.ctx.take_followups.return_value = followups
        with (
            patch.object(job_runner, "build_items", return_value=(items, [], {})),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job") as create,
        ):
            job_runner.run_intro_credits_job("j1")
        return create

    def test_other_episodes_of_the_season_get_one_season_job(self, env):
        create = self._run(env, [_item(ep(S1, 3))], [ep(S1, 2), ep(S1, 1), ep(S1, 3)])
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        assert kwargs["file_paths"] == [ep(S1, 1), ep(S1, 2)]  # the job's own episode is left out, sorted
        assert (kwargs["source"], kwargs["priority"]) == ("season", 3)
        assert kwargs["library_name"] == "Season: Show (2020) {tvdb-1} · Season 01"
        assert "force" not in kwargs and "follows_job_id" not in kwargs

    def test_several_seasons_are_named_by_count(self, env):
        create = self._run(env, [_item(ep(S1, 9))], [ep(S1, 1), ep(S2, 1)])
        assert create.call_args.kwargs["library_name"] == "Season: 2 seasons"

    def test_nothing_outside_the_job_queues_nothing(self, env):
        create = self._run(env, [_item(ep(S1, 1)), _item(ep(S1, 2))], [ep(S1, 2)])
        create.assert_not_called()

    def test_a_season_job_never_queues_another(self, env):
        env.job.config = {"file_paths": [ep(S1, 1)], "source": "season"}
        create = self._run(env, [_item(ep(S1, 1))], [ep(S1, 2)])
        create.assert_not_called()

    def test_a_cancelled_job_queues_nothing(self, env):
        env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
        create = self._run(env, [_item(ep(S1, 1))], [ep(S1, 2)])
        create.assert_not_called()

    def test_a_huge_season_backlog_is_capped(self, env):
        many = [f"{S1}/Show (2020) - S01E{e:03d}.mkv" for e in range(1, 603)]
        create = self._run(env, [_item(ep(S1, 1))], many)
        assert len(create.call_args.kwargs["file_paths"]) == job_runner.MAX_RETRY_FILES
        assert any("more episode" in c.args[1] for c in env.jm.add_log.call_args_list)

    @pytest.mark.parametrize(("source", "retried"), [("season", False), ("sonarr", True)])
    def test_a_season_job_doesnt_retry_a_file_missing_from_disk(self, env, monkeypatch, source, retried):
        path = ep(S1, 1)
        env.job.config = {"file_paths": [path], "source": source}
        captured = {}
        monkeypatch.setattr(job_runner, "set_file_result_callback", lambda fn, job_id: captured.setdefault("fn", fn))

        def submit(**kwargs):
            captured["fn"](path, "skipped_file_not_found", "File not found on disk", "worker")
            return env.tracker

        env.dispatcher.submit_items.side_effect = submit
        env.ctx.take_followups.return_value = []
        with (
            patch.object(job_runner, "build_items", return_value=([_item(path)], [], {path: path})),
            patch.object(job_runner, "_queue_retry") as retry,
        ):
            job_runner.run_intro_credits_job("j1")
        assert retry.called is retried


class TestFollowUpConfigIsReadWhenItsFilesAreListed:
    def test_episodes_that_joined_while_waiting_are_listed_and_the_job_is_sealed(self, env, monkeypatch):
        env.job.config = {"file_paths": [ep(S1, 1)], "follows_job_id": "p1", "source": "plex"}

        def joined_while_waiting(job_id, follows_job_id, cancel_check):
            env.job.config = {**env.job.config, "file_paths": [ep(S1, 1), ep(S1, 2)]}
            return True

        monkeypatch.setattr(job_runner, "_wait_for_preceding_job", joined_while_waiting)
        env.ctx.take_followups.return_value = []
        with patch.object(job_runner, "build_items", return_value=([_item(ep(S1, 1))], [], {})) as build:
            job_runner.run_intro_credits_job("j1")
        listed = build.call_args.args[0]
        assert listed["file_paths"] == [ep(S1, 1), ep(S1, 2)] and listed[job_runner.FILES_SEALED] is True
        env.jm.update_job_config.assert_called_once_with("j1", listed)

    def test_jobs_that_follow_no_preview_job_are_not_sealed(self, env):
        env.ctx.take_followups.return_value = []
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        env.jm.update_job_config.assert_not_called()


class TestWebhookSeasonGrouping:
    @pytest.fixture
    def jm(self, settings, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        settings["media_servers"] = [_server("jf-1", "jellyfin")]
        manager = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: manager)
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
        return manager

    def _existing(self, jm, paths, **extra):
        config = {"kind": JOB_KIND_INTRO_CREDITS, "file_paths": paths, "follows_job_id": "prev-0", "force": False}
        return jm.create_job(library_name="existing", kind=JOB_KIND_INTRO_CREDITS, config={**config, **extra})

    def _submit(self, paths, hints=None):
        return triggers.submit_webhook_follow_up(preview_job_id="prev-2", paths=paths, source="plex", item_id_hints=hints)

    def _new(self, jm, before):
        return [j for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS and j.id not in before]

    def test_a_sibling_episode_joins_the_waiting_follow_up_of_its_season(self, jm):
        waiting = self._existing(jm, [ep(S1, 1)])
        out = self._submit([ep(S1, 2)], hints={ep(S1, 2): {"jf-1": "abc"}})
        assert out == waiting.id and self._new(jm, {waiting.id}) == []
        config = jm.get_job(waiting.id).config
        assert config["file_paths"] == [ep(S1, 1), ep(S1, 2)]
        assert config["webhook_item_id_hints"] == {ep(S1, 2): {"jf-1": "abc"}}

    def test_another_season_gets_its_own_job(self, jm):
        waiting = self._existing(jm, [ep(S1, 1)])
        out = self._submit([ep(S2, 1)])
        (new,) = self._new(jm, {waiting.id})
        assert out == new.id and new.config["file_paths"] == [ep(S2, 1)]
        assert jm.get_job(waiting.id).config["file_paths"] == [ep(S1, 1)]

    @pytest.mark.parametrize(
        "extra", [{job_runner.FILES_SEALED: True}, {"retry_attempt": 1}, {"verify": True}, {"force": True}]
    )
    def test_sealed_retry_verify_and_forced_jobs_take_no_more_files(self, jm, extra):
        existing = self._existing(jm, [ep(S1, 1)], **extra)
        out = self._submit([ep(S1, 2)])
        (new,) = self._new(jm, {existing.id})
        assert out == new.id and new.config["file_paths"] == [ep(S1, 2)]

    def test_files_that_arent_episodes_are_not_grouped(self, jm):
        existing = self._existing(jm, ["/media/tv/a.mkv"])
        out = self._submit(["/media/tv/b.mkv"])
        (new,) = self._new(jm, {existing.id})
        assert out == new.id

    def test_a_batch_across_seasons_joins_where_it_can_and_queues_the_rest(self, jm):
        waiting = self._existing(jm, [ep(S1, 1)])
        out = self._submit([ep(S1, 2), ep(S2, 1)])
        (new,) = self._new(jm, {waiting.id})
        assert out == new.id and new.config["file_paths"] == [ep(S2, 1)]
        assert jm.get_job(waiting.id).config["file_paths"] == [ep(S1, 1), ep(S1, 2)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_season_followups.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'FILES_SEALED'`.

- [ ] **Step 3: Implement the runner side**

In `job_runner.py` next to the other constants:

```python
SEASON_SOURCE = "season"
# A webhook follow-up's config key once its runner has read its files: no more episodes join it after that.
FILES_SEALED = "files_sealed"
# Serialises webhook follow-ups joining a job (triggers.py) with a runner reading that job's files.
FOLLOW_UP_LOCK = threading.Lock()
# Job sources whose files no sender just reported: a file missing from disk won't appear by waiting.
_NO_RETRY_SOURCES = _USER_PICKED_SOURCES | {SEASON_SOURCE}
```

(`_USER_PICKED_SOURCES` is defined above; keep this line after it.)

```python
def _queue_season_followups(job, paths: list[str]) -> None:
    """Create the job that decides again the other episodes of this job's seasons (spec §5.3). Never raises.

    Args:
        job: The job that just finished.
        paths: Files the season detector asked about that weren't this job's own items.
    """
    jm = get_job_manager()
    try:
        from .triggers import create_intro_credits_job

        chosen = sorted(paths)[:MAX_RETRY_FILES]
        if len(paths) > len(chosen):
            jm.add_log(
                job.id, f"INFO - {len(paths) - len(chosen)} more episode(s) are decided again on their own next run"
            )
        folders = sorted({os.path.dirname(p) for p in chosen})
        if len(folders) == 1:
            name = f"Season: {os.path.basename(os.path.dirname(folders[0]))} · {os.path.basename(folders[0])}"
        else:
            name = f"Season: {len(folders)} seasons"
        followup = create_intro_credits_job(
            library_name=name, priority=job.priority, source=SEASON_SOURCE, file_paths=chosen
        )
        jm.add_log(
            job.id,
            f"INFO - {len(chosen)} other episode(s) of the same season are checked again with the new audio match "
            f"(job {followup.id[:8]})",
        )
    except Exception:
        logger.exception("Could not queue the season follow-up for Intro & Credits job {}", job.id)
```

In `run_intro_credits_job`:
- Right before `items, warnings, sender_paths = build_items(`:

```python
                if cfg.get("follows_job_id"):
                    # Episodes of the same season may have joined this follow-up while it waited (triggers.py); the
                    # files are read now, and nothing joins after this.
                    with FOLLOW_UP_LOCK:
                        latest = jm.get_job(job_id) or job
                        cfg = {**(latest.config or {}), FILES_SEALED: True}
                        jm.update_job_config(job_id, cfg)
```

- Keep the full item list before `_skip_finished_before_restart`: `listed = {item.canonical_path for item in items}`
  right after the empty-items check.
- Replace `sent_files = bool(cfg.get("file_paths")) and cfg.get("source") not in _USER_PICKED_SOURCES` with
  `... not in _NO_RETRY_SOURCES` and extend its comment: "a Season job's files were on disk when the season step saw
  them".
- After `_complete(jm, job_id, outcome, [...])` and before the retry/verify calls:

```python
                if cfg.get("source") != SEASON_SOURCE:
                    followups = [path for path in ctx.take_followups() if path not in listed]
                    if followups:
                        _queue_season_followups(job, followups)
```

- [ ] **Step 4: Implement the webhook side**

In `triggers.py`: delete `_follow_up_lock = threading.Lock()`; import `from .job_runner import FILES_SEALED,
FOLLOW_UP_LOCK, start_intro_credits_job_async` (extend the existing import) and `from .external_ids import
ids_from_path`; replace `with _follow_up_lock:` by `with FOLLOW_UP_LOCK:`. Add:

```python
def _season_folders(paths: Iterable[str], configs: list[ServerConfig]) -> set[str]:
    """Season folders of the episode files among ``paths`` (every local candidate of each sender path)."""
    return {
        os.path.dirname(candidate)
        for path in paths
        for candidate in _local_candidates(str(path), configs)
        if ids_from_path(candidate).is_episode
    }


def _joinable_follow_ups(jm, configs: list[ServerConfig]) -> list[tuple[Job, set[str]]]:
    """Webhook follow-ups whose runner hasn't read its files yet, with the season folders they already cover.

    Retries, verify jobs and forced re-detects keep their own file lists.
    """
    joinable = []
    for job in jm.get_pending_jobs():
        cfg = job.config or {}
        if job.kind != JOB_KIND_INTRO_CREDITS or not cfg.get("follows_job_id"):
            continue
        if cfg.get(FILES_SEALED) or cfg.get("force") or cfg.get("retry_attempt") or cfg.get("verify"):
            continue
        folders = _season_folders(cfg.get("file_paths") or [], configs)
        if folders:
            joinable.append((job, folders))
    return joinable


def _join(jm, job: Job, paths: list[str], hints: dict[str, dict[str, str]] | None) -> None:
    """Add episodes (and their item id hints) to a waiting follow-up's config."""
    cfg = dict(job.config or {})
    cfg["file_paths"] = list(dict.fromkeys([*(cfg.get("file_paths") or []), *paths]))
    joined_hints = dict(cfg.get("webhook_item_id_hints") or {})
    joined_hints.update({p: h for p, h in (hints or {}).items() if p in paths})
    cfg["webhook_item_id_hints"] = joined_hints
    jm.update_job_config(job.id, cfg)
```

In `submit_webhook_follow_up`, inside the lock, after `if not fresh: ... return None`:

```python
        rest = list(fresh)
        joined_id: str | None = None
        for waiting, folders in _joinable_follow_ups(jm, configs):
            mine = [p for p in rest if _season_folders([p], configs) & folders]
            if not mine:
                continue
            _join(jm, waiting, mine, item_id_hints)
            logger.info(
                "Intro & Credits for webhook job {}: {} episode(s) join follow-up {} of the same season",
                preview_job_id,
                len(mine),
                waiting.id[:8],
            )
            rest = [p for p in rest if p not in mine]
            joined_id = joined_id or waiting.id
        if not rest:
            return joined_id
```

and use `rest` instead of `fresh` in the rest of the function (the name, the hints filter, `file_paths=`). Update the
docstring: "An episode whose season folder a waiting follow-up already covers joins it; the id returned is then that
job's when nothing new was created."

- [ ] **Step 5: Run the task's tests and the job/trigger suites**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers/test_season_followups.py tests/markers/test_job_runner.py tests/markers/test_job_runner_real.py tests/markers/test_triggers.py tests/markers/test_webhook_follow_up_wiring.py -q`
Expected: PASS after adjusting only assertions that compared a follow-up's whole config (now with `files_sealed`).

- [ ] **Step 6: Mutation self-check (high-risk)**

One at a time, confirm a test fails, revert: seal outside `FOLLOW_UP_LOCK`; don't re-read `job.config` (use the
thread-start `cfg`); drop `if path not in listed`; drop the `SEASON_SOURCE` guard; `_season_folders` without the
`is_episode` filter; `_joinable_follow_ups` ignoring `FILES_SEALED`.

- [ ] **Step 7: Commit**

`feat(markers): season follow-up jobs and season-grouped webhook follow-ups`.

---
## Task 9: Plex version drift in read-back

`[sequential]` (after Task 3; parallel to Tasks 6–8) `[high-risk]` — ledger parked item "a newly added Plex version
that is never decided (disk not mapped into the container) leaves the existing version skipping, so our markers stay on
the item … reconcile's periodic read-back (or a part-count check) must re-run the item's Plex write" (progress.md line
184); `plex-item-publishing.md` (a type is shown only when every live version is decided and agrees); spec §6.3
("Multi-version items share one marker set"); `tests/markers/test_publisher_contract.py::
test_a_new_version_that_is_never_decided_takes_our_markers_off_at_the_next_publish` (whose first assertion documents
this limit as "up_to_date").

Today a normal run skips a file whose decision and item row are unchanged once `shows()` finds our markers. A version
added to the Plex item afterwards (never decided here) changes neither, so our markers keep showing on an item whose
new cut nobody checked — a precision failure. Fix: every write records the item's version files; the read-back
compares them and reports `Shown.VERSIONS_CHANGED`, which the pipeline treats like any other drift (write again, and
the Plex publisher takes off the types the new version hasn't agreed on). Task 11's reconcile sweep uses the same
signal without a job for the file.

**Files:**
- Modify: `media_preview_generator/markers/publishers/base.py` (`Shown`, `MarkerPublisher.last_item_files`, `shows`
  signature + docstring), `media_preview_generator/markers/publishers/plex_db.py` (`_version_files`, `write`, `shows`),
  `media_preview_generator/markers/publishers/jellyfin.py` (`shows` signature), `media_preview_generator/markers/store.py`
  (`item_versions` table, `ItemPublishStateRow.item_files`, `get_item_publish_state`, `set_item_publish_state`),
  `media_preview_generator/markers/pipeline.py` (`_shown_on_server`, `_publish_to`), `tests/markers/fakes.py`
  (`ready_publisher`, `FakePlexItems`)
- Test: `tests/markers/test_store.py` (item files), `tests/markers/test_publisher_contract.py` (new tests + the
  "known limit" line), `tests/markers/test_pipeline.py` (`TestReadBack`-style tests near the existing read-back ones)

**Interfaces:**
- Consumes: phase-1 publisher contract.
- Produces (Tasks 10, 11):
```python
class Shown(str, Enum): OURS; MISSING; REPLACED; VERSIONS_CHANGED = "versions_changed"
MarkerPublisher.last_item_files: tuple[str, ...] | None = None     # set by write; None = the server has no versions to track
MarkerPublisher.shows(self, item_id: str, ours: list[Marker], *, kept_types: frozenset[MarkerType] = frozenset(),
                      item_files: tuple[str, ...] | None = None) -> Shown | None
ItemPublishStateRow.item_files: tuple[str, ...] | None = None
MarkerStore.set_item_publish_state(self, server_id: str, item_id: str, markers: list[Marker] | None, status: str, *,
                                   kept_types: Iterable[MarkerType] | None = None,
                                   item_files: Iterable[str] | None = None) -> int
```

- [ ] **Step 1: Write the failing tests**

`tests/markers/test_store.py`, new class:

```python
class TestItemFiles:
    MARKER = Marker(MarkerType.INTRO, 1_000, 30_000, ("chapters",))

    def test_recorded_sorted_without_bumping_the_version_and_kept_after_a_failure(self, store):
        v1 = store.set_item_publish_state("plex-1", "7", [self.MARKER], "written", item_files=["/b.mkv", "/a.mkv"])
        row = store.get_item_publish_state("plex-1", "7")
        assert row.item_files == ("/a.mkv", "/b.mkv") and row.version == v1
        v2 = store.set_item_publish_state("plex-1", "7", [self.MARKER], "written", item_files=["/a.mkv"])
        assert v2 == v1 and store.get_item_publish_state("plex-1", "7").item_files == ("/a.mkv",)
        store.set_item_publish_state("plex-1", "7", None, "failed")
        assert store.get_item_publish_state("plex-1", "7").item_files == ("/a.mkv",)

    def test_rows_without_recorded_files_read_as_unknown(self, store):
        store.set_item_publish_state("jf-1", "abc", [self.MARKER], "written")
        assert store.get_item_publish_state("jf-1", "abc").item_files is None
```

(`test_store.py` already has a `store` fixture and imports `Marker`, `MarkerType`; add what's missing.)

`tests/markers/test_publisher_contract.py` (add `Shown` to the `publishers.base` import):

```python
@pytest.mark.parametrize("vendor", VENDORS)
def test_read_back_accepts_the_recorded_item_files(request, vendor):
    target = request.getfixturevalue(vendor)
    publisher = _publisher(target)
    ours = publisher.write(target.item_id, [INTRO, CREDITS], previous=[], duration_ms=DUR, canonical_path=target.path)
    files = publisher.last_item_files
    assert publisher.shows(target.item_id, ours, item_files=files) is Shown.OURS
    if vendor == "plex":
        assert files == (target.path,)
        assert publisher.shows(target.item_id, ours, item_files=("/other.mkv",)) is Shown.VERSIONS_CHANGED
        assert publisher.shows(target.item_id, ours, item_files=None) is Shown.OURS
    else:
        assert files is None  # Jellyfin item ids are per version: nothing to track


def test_a_version_added_later_takes_our_markers_off_on_the_next_normal_run(plex_item):
    item = plex_item(in_item=("1080p",))
    item.chapters[item.paths["1080p"]] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    assert _outcomes(item.run("1080p")) == ["published"]
    assert item.store.get_item_publish_state("plex-1", "7").item_files == (item.paths["1080p"],)
    item.add_part("2160p")  # its disk isn't mapped into the container: never decided
    out = item.run("1080p")
    assert _outcomes(out) == ["waiting"] and "intro, credits" in out.publisher_rows[0]["message"]
    assert item.served() == item.recorded() == [] and item.commits == 2
    assert item.store.get_item_publish_state("plex-1", "7").item_files == tuple(sorted(item.paths.values()))
    assert _outcomes(item.run("1080p")) == ["waiting"] and item.commits == 2
```

In `test_a_new_version_that_is_never_decided_takes_our_markers_off_at_the_next_publish`, the line
`assert _outcomes(item.run("1080p")) == ["up_to_date"]  # the known limit until something makes A publish` becomes
`assert _outcomes(item.run("1080p")) == ["waiting"]  # the read-back sees the new version`, and the `commits == 2`
checks after the trigger stay as they are (the removal now happens one run earlier; the triggered run writes nothing).

`tests/markers/test_pipeline.py`, next to the existing read-back tests:

```python
class TestReadBackVersions:
    def test_read_back_passes_the_item_files_recorded_at_the_last_write(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()

        def write(item_id, markers, **kwargs):
            plex.last_item_files = ("/plex/a.mkv", "/plex/b.mkv")
            return plex.succeed(item_id, markers, **kwargs)

        plex.write.side_effect = write
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        row = store.get_item_publish_state("plex-1", "item-plex-1")
        assert row.item_files == ("/plex/a.mkv", "/plex/b.mkv")
        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert plex.shows.call_args.args == ("item-plex-1", [INTRO_CH, CREDITS_CH])
        assert plex.shows.call_args.kwargs == {"kept_types": frozenset(), "item_files": ("/plex/a.mkv", "/plex/b.mkv")}
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value

    def test_changed_versions_publish_again(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        plex.shows.return_value = Shown.VERSIONS_CHANGED
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert plex.write.call_count == 2
        assert plex.write.call_args.kwargs["previous"] == [INTRO_CH, CREDITS_CH]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_store.py::TestItemFiles tests/markers/test_publisher_contract.py -k "item_files or version_added_later or never_decided" tests/markers/test_pipeline.py::TestReadBackVersions -q`
Expected: FAIL — `TypeError: set_item_publish_state() got an unexpected keyword argument 'item_files'`.

- [ ] **Step 3: Implement the contract and the store**

`base.py`:

```python
class Shown(str, Enum):
    """What a server item shows of the markers this app last left there (``MarkerPublisher.shows``)."""

    OURS = "ours"
    MISSING = "missing"  # some of ours are gone and nothing else of that type took their place
    REPLACED = "replaced"  # the server shows another marker of one of our types instead (its own detection)
    # The item's versions aren't the ones our last write agreed on (Plex: one marker set for every version, and a
    # version added since hasn't been decided the same way).
    VERSIONS_CHANGED = "versions_changed"
```

On `MarkerPublisher`, after `last_kept_types`:

```python
    # Set by every ``write`` that read the item: the item's version files that write computed the marker set for. The
    # caller records them and passes them back to ``shows``. None where items have no shared versions (Jellyfin).
    last_item_files: tuple[str, ...] | None = None
```

Add `item_files: tuple[str, ...] | None = None` to the abstract `shows` signature with this docstring line:
"item_files: The item's version files recorded at the last write (``last_item_files``); a server with one marker set
per item reports ``VERSIONS_CHANGED`` when its versions differ now. None: not recorded, not compared."

`jellyfin.py` `shows`: add the same keyword and "item_files: Ignored (see ``write``)." to its docstring.

`store.py`: add to `_SCHEMA` after `item_kept_types`:

```python
    # The version files a server item had when this app last wrote its markers (Plex: one set for every version).
    """CREATE TABLE IF NOT EXISTS item_versions (
        server_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        files_json TEXT NOT NULL,
        PRIMARY KEY (server_id, item_id))""",
```

`ItemPublishStateRow` gains `item_files: tuple[str, ...] | None = None` (after `kept_types`). In
`get_item_publish_state`, inside the lock after `kept = ...`:

```python
            files_row = self._conn.execute(
                "SELECT files_json FROM item_versions WHERE server_id=? AND item_id=?", (server_id, item_id)
            ).fetchone()
        item_files = tuple(json.loads(files_row["files_json"])) if files_row else None
```

and pass `item_files` to the row. In `set_item_publish_state` add the keyword (docstring: "item_files: The item's
version files the write computed the set for; None keeps the recorded ones. Recording them never bumps the version.")
and, as the first statement inside `with self._tx() as conn:`,

```python
            if item_files is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO item_versions (server_id, item_id, files_json) VALUES (?,?,?)",
                    (server_id, item_id, json.dumps(sorted(item_files))),
                )
```

- [ ] **Step 4: Implement the Plex side**

`plex_db.py`, next to `_is_optimized_copy`:

```python
def _version_files(parts: list[_Part]) -> tuple[str, ...]:
    """The item's versions as its sorted part files, Plex's optimized copies left out (they take no part in agreement)."""
    return tuple(sorted(p.file for p in parts if not _is_optimized_copy(p)))
```

In `write`: first line `self.last_item_files = None`; right after `if not parts: raise ItemNotFoundError(...)` add
`self.last_item_files = _version_files(parts)`. In `shows`, inside the `with self._database(...)` block after the
`rows = ...` query:

```python
                parts = self._item_parts(conn, rating_key) if item_files is not None else None
```

and before `if any(not served.get(mtype) for mtype in kept_types):`

```python
        if parts is not None and _version_files(parts) != tuple(item_files):
            return Shown.VERSIONS_CHANGED
```

Add `item_files` to the method signature and docstring ("item_files: … a different set now is VERSIONS_CHANGED,
whatever the rows show").

- [ ] **Step 5: Implement the pipeline and the fakes**

`pipeline.py`:

```python
def _shown_on_server(
    publisher: MarkerPublisher,
    cfg: ServerConfig,
    item_id: str,
    ours: list[Marker],
    kept_types: frozenset[MarkerType],
    item_files: tuple[str, ...] | None,
) -> Shown | None:
    """What the server shows of ``ours`` (and of the types it keeps as its own, and of the item's versions) now.

    Returns:
        None when it couldn't be read.
    """
    try:
        return publisher.shows(item_id, ours, kept_types=kept_types, item_files=item_files)
    except Exception as exc:
        # A transient read problem mustn't fail or rewrite a file whose records say it is up to date.
        logger.debug("Couldn't read back the markers on {} for item {}: {}", cfg.name, item_id, type(exc).__name__)
        return None
```

In `_publish_to`: `shown = _shown_on_server(publisher, cfg, item_id, list(item_row.markers), item_row.kept_types,
item_row.item_files)`; the log line's reason becomes

```python
            reason = {
                Shown.OURS: "set to restore this app's markers",
                Shown.VERSIONS_CHANGED: "the item's versions changed since last run",
            }.get(shown, f"markers {shown.value} since last run")
```

and `version = store.set_item_publish_state(cfg.id, item_id, ours, "written", kept_types=kept,
item_files=publisher.last_item_files)`.

`tests/markers/fakes.py`: in `ready_publisher` add `pub.last_item_files = None`. In `FakePlexItems.publisher`'s
`write`, set `pub.last_item_files = tuple(sorted(self.parts[item_id]))` before computing `desired`; its `shows` gains
`item_files=None` and starts with

```python
            if item_files is not None and tuple(sorted(self.parts[item_id])) != tuple(item_files):
                return Shown.VERSIONS_CHANGED
```

- [ ] **Step 6: Run the tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers -q`
Expected: PASS.

- [ ] **Step 7: Mutation self-check (high-risk)**

One at a time, confirm a test fails, revert: record `last_item_files` including optimized copies; compare unsorted;
skip the comparison when `rows` is empty; pipeline passes `item_files=None`; `set_item_publish_state` bumps the version
when only the files changed (must break `test_recorded_sorted_without_bumping_the_version…` and the multi-version
contract tests' commit counts).

- [ ] **Step 8: Commit**

`fix(markers): a Plex version added after publishing is noticed by the read-back`.

---
## Task 10: Emby server client + `EmbyMarkerPublisher` + install route

`[sequential]` (after Tasks 4, 9) `[high-risk]` — spec §3.3, §6.3 EmbyMarkerPublisher ("`POST
/MediaPreviewBridge/Markers/{internalId}` … Types: IntroStart, IntroEnd, CreditsStart … app installs via `POST
/Packages/Installed/{name}`. Until accepted: manual DLL install instructions"), §7.1 (Emby status: plugin, installed vs
required, Install/Update, which types it can show), §13 item 5; Task 4's HTTP contract; `plex-item-publishing.md`
publisher contract (`write` returns ours, `atomic_writes`, `previous=None`).

**OWNER DECISION 2026-09-14 (supersedes R1 below):** Emby **always** gets a decided credits marker, even when the
credits end before the file does (Emby's player then skips to the end, including any after-credits scene). Send the
credits start; don't clamp or drop. The Inspector and the Emby server row say plainly: "Emby skips to the end of the
file". Task 16 adds the rule to spec §6.3 and a §14 line; docs/guides.md tells Emby users.

**OWNER RULING R1 (recommended default implemented here):** Emby keeps a credits *start* only; its player treats it as
"credits run to the end". A decided credits marker that ends before the file does (a scene after the credits, spec
§5.5 rule 7's Rick and Morty case) would skip that scene on Emby. Precision over coverage: credits are sent to Emby only
when they end within 2 s of the file's end (`decide.EOF_CLAMP_MS`); otherwise the row says "Emby can't show credits that
end before the file does". Spec §6.3 is silent; Task 16 adds the rule.

**Publisher behaviour (implemented exactly):**
- `supported_types = {intro, credits}`; `name = "emby_bridge"`; `atomic_writes = False` (the plugin stores before
  Emby's chapters are confirmed).
- `project(markers, *, duration_ms)` drops credits that don't run to the end (unknown duration → dropped).
- An item whose MediaSources have durations more than 2 s apart (`SAME_CUT_MS`) → `PublishError("This Emby item groups
  versions of different lengths; one marker set can't fit them all")`: one chapter set per item.
- Unchanged set (`previous` == wanted) and the plugin holds this file's size (not stale) and chapters show ours →
  no request (mirrors Jellyfin).
- Nothing wanted → DELETE when `previous` is None or non-empty.
- POST with the file's size; `Stale` in the answer → DELETE + `PublishError` ("different file size"); then chapters are
  read back (`get_chapter_markers`) and must show ours, else DELETE (best effort) + `PublishError`.
- `shows()`: exactly one IntroStart and one IntroEnd make the intro; each CreditsStart is compared by its start (Emby has
  no end); `compare_shown(..., others_alongside=False)` (Emby's own detected markers replace ours on the same rows).

**Files:**
- Create: `media_preview_generator/markers/publishers/emby.py`
- Modify: `media_preview_generator/servers/_embyish.py` (move `get_bridge_info`, `get_bridge_markers_access` here from
  `jellyfin.py`, probe id as class attribute `_BRIDGE_ACCESS_PROBE_ID`), `media_preview_generator/servers/jellyfin.py`
  (delete the moved methods; `_BRIDGE_ACCESS_PROBE_ID = "ffffffffffffffffffffffffffffffff"` class attribute),
  `media_preview_generator/servers/emby.py` (Bridge client, catalog check, install),
  `media_preview_generator/markers/publishers/base.py` (`project(..., duration_ms=)`, `projection_note`),
  `media_preview_generator/markers/publishers/factory.py` (Emby branch, docstring),
  `media_preview_generator/markers/pipeline.py` (`_publish_to`: projection with duration + note),
  `media_preview_generator/markers/inspect.py` (`EMBY_NEEDS_PLUGIN_MESSAGE` removed; `_server_row` wanted for Emby;
  `_checked_as_if_on` fallback text), `media_preview_generator/web/routes/api_servers.py` (`install_jellyfin_plugin`:
  Emby allowed), `tests/markers/fakes.py` (`ready_publisher.project` accepts `duration_ms`, `projection_note`)
- Test: `tests/markers/test_emby_publisher.py`, `tests/test_servers_emby_bridge.py`, `tests/test_servers_emby_markers_vcr.py`
  (+ cassettes), `tests/markers/test_publisher_contract.py` (`emby` fixture in `VENDORS`; delete
  `test_emby_has_no_publisher_yet`), `tests/markers/test_publisher_factory.py`, `tests/markers/test_inspect.py`,
  the install-plugin route tests in `tests/test_routes*.py` that assert "Jellyfin-only" (find with
  `grep -rn "Jellyfin-only" tests`)

**Interfaces:**
- Consumes: Task 4 HTTP contract; Task 9 `shows(..., item_files=)`; phase 1 `EmbyApiClient.get_chapter_markers`,
  `get_media_source_durations`, `compare_shown`, `SAME_CUT_MS`.
- Produces (Tasks 11, 12, 13):
```python
# servers/_embyish.py (moved, behaviour unchanged for Jellyfin)
EmbyApiClient.get_bridge_info(self) -> dict[str, Any] | None          # {"installed", "version", "features"}
EmbyApiClient.get_bridge_markers_access(self) -> str | None           # "ok" | "unauthorized" | "forbidden" | None
# servers/emby.py
EmbyServer.PLUGIN_NAME = "Media Preview Bridge for Emby"
EmbyServer.get_emby_marker_state(self, item_id: str) -> dict[str, Any] | None
    # {"intro_start_ticks", "intro_end_ticks", "credits_start_ticks", "file_size", "stale"}; None = unknown
EmbyServer.put_emby_markers(self, item_id: str, *, intro_start_ticks: int | None, intro_end_ticks: int | None,
                            credits_start_ticks: int | None, file_size: int | None) -> requests.Response
EmbyServer.delete_emby_markers(self, item_id: str) -> requests.Response
EmbyServer.bridge_catalog_listed(self) -> bool | None
EmbyServer.install_plugin(self) -> dict[str, Any]                     # {"steps", "ok", "error", "manual"}
# markers/publishers/base.py
MarkerPublisher.project(self, markers: Iterable[Marker], *, duration_ms: int | None = None) -> list[Marker]
MarkerPublisher.projection_note(self, markers: Iterable[Marker], *, duration_ms: int | None) -> str   # "" by default
# markers/publishers/emby.py
CREDITS_BEFORE_END_NOTE = "Emby can't show credits that end before the file does"
def credits_run_to_end(marker: Marker, duration_ms: int | None) -> bool
class EmbyMarkerPublisher(MarkerPublisher)
```

- [ ] **Step 1: Write the failing client tests**

```python
# tests/test_servers_emby_bridge.py
"""EmbyServer's Media Preview Bridge calls: URLs, bodies and answer shapes (the plugin contract of Task 4)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from media_preview_generator.servers import EmbyServer, JellyfinServer, ServerConfig, ServerType


def _server(cls=EmbyServer, stype=ServerType.EMBY):
    return cls(ServerConfig(id="e1", type=stype, name="Emby", enabled=True, url="http://emby:8096",
                            auth={"method": "api_key", "api_key": "k"}, libraries=[]))  # fmt: skip


def _resp(status=200, body=None):
    r = MagicMock(status_code=status)
    r.json.return_value = body
    return r


class TestBridgeInfoShared:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ({"Ok": True, "Version": "1.0.0.0", "Features": ["markers"]}, {"installed": True, "version": "1.0.0.0", "features": ["markers"]}),
            ({"ok": True, "version": "10.11.1.0", "features": ["trickplay", "markers"]}, {"installed": True, "version": "10.11.1.0", "features": ["trickplay", "markers"]}),
        ],
    )  # fmt: skip
    def test_ping_is_read_in_emby_and_jellyfin_casing(self, body, expected):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, body)) as req:
            assert server.get_bridge_info() == expected
        assert req.call_args.args == ("GET", "/MediaPreviewBridge/Ping")

    def test_access_probe_uses_each_vendors_own_unused_id(self):
        emby, jellyfin = _server(), _server(JellyfinServer, ServerType.JELLYFIN)
        with patch.object(emby, "_request", return_value=_resp(200, {"Found": False, "Error": "item not found"})) as req:
            assert emby.get_bridge_markers_access() == "ok"
        assert req.call_args.args == ("GET", "/MediaPreviewBridge/Markers/999999999999")
        with patch.object(jellyfin, "_request", return_value=_resp(404, {"error": "item not found"})) as req:
            assert jellyfin.get_bridge_markers_access() == "ok"
        assert req.call_args.args == ("GET", "/MediaPreviewBridge/Markers/ffffffffffffffffffffffffffffffff")


class TestEmbyMarkers:
    def test_state_is_read_from_the_plugin_answer(self):
        body = {"Id": "42", "Found": True, "Error": None, "IntroStartTicks": 100_000_000, "IntroEndTicks": 400_000_000,
                "CreditsStartTicks": None, "FileSize": 1234, "Stale": False, "Stored": 0}  # fmt: skip
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, body)) as req:
            state = server.get_emby_marker_state("42")
        assert req.call_args.args == ("GET", "/MediaPreviewBridge/Markers/42")
        assert state == {"intro_start_ticks": 100_000_000, "intro_end_ticks": 400_000_000,
                         "credits_start_ticks": None, "file_size": 1234, "stale": False}  # fmt: skip

    @pytest.mark.parametrize(
        "answer", [_resp(200, {"Found": False, "Error": "item not found"}), _resp(404, None), _resp(500, None)]
    )
    def test_unknown_answers_read_as_none(self, answer):
        server = _server()
        with patch.object(server, "_request", return_value=answer):
            assert server.get_emby_marker_state("42") is None

    def test_transport_errors_read_as_none(self):
        server = _server()
        with patch.object(server, "_request", side_effect=requests.ConnectionError("down")):
            assert server.get_emby_marker_state("42") is None

    def test_put_sends_the_pascal_case_body(self):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, {})) as req:
            server.put_emby_markers("42", intro_start_ticks=1, intro_end_ticks=2, credits_start_ticks=None, file_size=9)
        assert req.call_args.args == ("POST", "/MediaPreviewBridge/Markers/42")
        assert req.call_args.kwargs["json_body"] == {
            "IntroStartTicks": 1, "IntroEndTicks": 2, "CreditsStartTicks": None, "FileSize": 9,
        }  # fmt: skip

    def test_ids_are_quoted_into_the_url(self):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, {})) as req:
            server.delete_emby_markers("../System/Restart")
        assert req.call_args.args == ("DELETE", "/MediaPreviewBridge/Markers/..%2FSystem%2FRestart")


class TestCatalogInstall:
    def test_not_in_the_catalog_says_install_by_hand(self):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, [{"Name": "TimeMarkEdit"}])) as req:
            result = server.install_plugin()
        assert req.call_count == 1 and req.call_args.args == ("GET", "/Packages")
        assert result["ok"] is False and result["manual"] is True and "by hand" in result["error"]

    def test_listed_package_is_installed_and_emby_restarted(self):
        server = _server()
        answers = [_resp(200, [{"Name": "Media Preview Bridge for Emby"}]), _resp(204, None), _resp(204, None)]
        with patch.object(server, "_request", side_effect=answers) as req:
            result = server.install_plugin()
        assert [c.args for c in req.call_args_list] == [
            ("GET", "/Packages"),
            ("POST", "/Packages/Installed/Media%20Preview%20Bridge%20for%20Emby"),
            ("POST", "/System/Restart"),
        ]
        assert result["ok"] is True and result["manual"] is False
```

- [ ] **Step 2: Write the failing publisher tests**

```python
# tests/markers/test_emby_publisher.py
"""EmbyMarkerPublisher: capability matrix, credits-to-the-end rule, write/confirm/cleanup, read-back."""

from __future__ import annotations

from unittest.mock import MagicMock, create_autospec

import pytest
import requests

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers.base import Capability, ItemNotFoundError, PublishError, Shown
from media_preview_generator.markers.publishers.emby import CREDITS_BEFORE_END_NOTE, EmbyMarkerPublisher
from media_preview_generator.markers.settings import load_server
from media_preview_generator.servers import EmbyServer
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import server_config

T = MarkerType
DUR = 1_321_472
INTRO = Marker(T.INTRO, 126_771, 157_068, ("chapters",))
CREDITS_TO_END = Marker(T.CREDITS, 1_295_324, DUR, ("chapters",))
CREDITS_EARLY = Marker(T.CREDITS, 1_250_000, 1_290_000, ("theintrodb", "server_markers"))


def _ok(**extra):
    return MagicMock(status_code=200, **{"json.return_value": {"Found": True, "Error": None, "Stale": False, **extra}})


class FakeEmby:
    """An autospec'd EmbyServer whose plugin stores what is posted and whose chapters show it."""

    def __init__(self, media_path):
        self.server = create_autospec(EmbyServer, instance=True)
        self.path = media_path
        self.markers = None
        self.size = None
        self.stale = False
        self.show_nothing = False
        s = self.server
        s.get_bridge_info.return_value = {"installed": True, "version": "1.0.0.0", "features": ["markers"]}
        s.get_bridge_markers_access.return_value = "ok"
        s.bridge_catalog_listed.return_value = False
        s.get_media_source_durations.return_value = [DUR]
        s.put_emby_markers.side_effect = self._put
        s.delete_emby_markers.side_effect = self._delete
        s.get_chapter_markers.side_effect = self._chapters
        s.get_emby_marker_state.side_effect = self._state

    def _put(self, item_id, *, intro_start_ticks, intro_end_ticks, credits_start_ticks, file_size):
        self.markers, self.size = (intro_start_ticks, intro_end_ticks, credits_start_ticks), file_size
        return _ok(Stale=self.stale, Stored=3)

    def _delete(self, item_id):
        self.markers, self.size = None, None
        return _ok(Stored=0)

    def _chapters(self, item_id):
        rows = [{"marker_type": "Chapter", "start_ms": 0, "name": "Chapter 1"}]
        if self.markers and not self.show_nothing and not self.stale:
            i_s, i_e, c_s = self.markers
            if i_s is not None:
                rows += [{"marker_type": "IntroStart", "start_ms": i_s // 10_000, "name": "Intro"},
                         {"marker_type": "IntroEnd", "start_ms": i_e // 10_000, "name": "Intro End"}]  # fmt: skip
            if c_s is not None:
                rows.append({"marker_type": "CreditsStart", "start_ms": c_s // 10_000, "name": "Credits"})
        return rows

    def _state(self, item_id):
        m = self.markers or (None, None, None)
        return {"intro_start_ticks": m[0], "intro_end_ticks": m[1], "credits_start_ticks": m[2],
                "file_size": self.size, "stale": self.stale}  # fmt: skip


@pytest.fixture
def emby(tmp_path):
    media = tmp_path / "S01E01.mkv"
    media.write_bytes(b"x" * 321)
    fake = FakeEmby(str(media))
    cfg = server_config("emby-1", ServerType.EMBY)
    fake.publisher = EmbyMarkerPublisher(fake.server, cfg, load_server(cfg.markers, "emby"))
    return fake


def _write(emby, markers, previous=(), **kw):
    return emby.publisher.write("42", list(markers), previous=None if previous is None else list(previous),
                                duration_ms=kw.get("duration_ms", DUR), canonical_path=emby.path)  # fmt: skip


class TestCapability:
    @pytest.mark.parametrize(
        ("setup", "state"),
        [
            (lambda s: setattr(s.get_bridge_info, "return_value", None), Capability.UNREACHABLE),
            (lambda s: setattr(s.get_bridge_info, "return_value", {"installed": False, "version": None, "features": []}), Capability.NEEDS_PLUGIN),
            (lambda s: setattr(s.get_bridge_info, "return_value", {"installed": True, "version": "0.9", "features": []}), Capability.PLUGIN_OUTDATED),
            (lambda s: setattr(s.get_bridge_markers_access, "return_value", "unauthorized"), Capability.MISCONFIGURED),
            (lambda s: setattr(s.get_bridge_markers_access, "return_value", "forbidden"), Capability.MISCONFIGURED),
            (lambda s: setattr(s.get_bridge_markers_access, "return_value", None), Capability.UNREACHABLE),
            (lambda s: None, Capability.READY),
        ],
    )  # fmt: skip
    def test_matrix(self, emby, setup, state):
        setup(emby.server)
        report = emby.publisher.capability()
        assert report.state is state
        if state is Capability.READY:
            assert report.details == {"plugin_version": "1.0.0.0", "can_show": ["intro", "credits"]}
        if state is Capability.NEEDS_PLUGIN:
            assert report.details == {"catalog_listed": False}

    def test_switched_off_is_disabled_without_asking_emby(self, tmp_path):
        server = create_autospec(EmbyServer, instance=True)
        cfg = server_config("emby-1", ServerType.EMBY, markers={"enabled": False, "library_ids": None})
        report = EmbyMarkerPublisher(server, cfg, load_server(cfg.markers, "emby")).capability()
        assert report.state is Capability.DISABLED and server.get_bridge_info.call_count == 0


class TestProjection:
    @pytest.mark.parametrize(
        ("marker", "duration", "kept"),
        [(CREDITS_TO_END, DUR, True), (Marker(T.CREDITS, 1_295_000, DUR - 2_000, ()), DUR, True),
         (Marker(T.CREDITS, 1_295_000, DUR - 2_001, ()), DUR, False), (CREDITS_EARLY, DUR, False),
         (CREDITS_TO_END, None, False), (INTRO, DUR, True), (Marker(T.RECAP, 0, 30_000, ()), DUR, False)],
    )  # fmt: skip
    def test_credits_only_when_they_run_to_the_end(self, emby, marker, duration, kept):
        assert (emby.publisher.project([marker], duration_ms=duration) == [marker]) is kept

    def test_note_names_credits_that_end_early(self, emby):
        assert emby.publisher.projection_note([INTRO, CREDITS_EARLY], duration_ms=DUR) == CREDITS_BEFORE_END_NOTE
        assert emby.publisher.projection_note([INTRO, CREDITS_TO_END], duration_ms=DUR) == ""


class TestWrite:
    def test_posts_ticks_with_the_file_size_and_returns_ours(self, emby):
        assert _write(emby, [INTRO, CREDITS_TO_END]) == [INTRO, CREDITS_TO_END]
        kwargs = emby.server.put_emby_markers.call_args.kwargs
        assert kwargs == {"intro_start_ticks": 1_267_710_000, "intro_end_ticks": 1_570_680_000,
                          "credits_start_ticks": 12_953_240_000, "file_size": 321}  # fmt: skip
        assert emby.server.put_emby_markers.call_args.args == ("42",)
        assert emby.publisher.last_write_changed is True

    def test_early_credits_are_not_sent(self, emby):
        assert _write(emby, [INTRO, CREDITS_EARLY]) == [INTRO]
        assert emby.server.put_emby_markers.call_args.kwargs["credits_start_ticks"] is None

    def test_unchanged_set_for_this_file_sends_nothing(self, emby):
        _write(emby, [INTRO, CREDITS_TO_END])
        emby.server.put_emby_markers.reset_mock()
        assert _write(emby, [INTRO, CREDITS_TO_END], previous=[INTRO, CREDITS_TO_END]) == [INTRO, CREDITS_TO_END]
        emby.server.put_emby_markers.assert_not_called()
        assert emby.publisher.last_write_changed is False

    def test_unchanged_set_for_a_replaced_file_is_posted_again(self, emby):
        _write(emby, [INTRO])
        emby.size = 1  # the plugin holds another file's size
        emby.server.put_emby_markers.reset_mock()
        _write(emby, [INTRO], previous=[INTRO])
        assert emby.server.put_emby_markers.call_args.kwargs["file_size"] == 321

    @pytest.mark.parametrize(("previous", "deleted"), [(None, True), ([INTRO], True), ([], False)])
    def test_nothing_wanted_deletes_only_what_may_be_ours(self, emby, previous, deleted):
        assert _write(emby, [], previous=previous) == []
        assert emby.server.delete_emby_markers.called is deleted
        assert emby.publisher.last_write_changed is deleted

    def test_stale_answer_is_removed_and_fails(self, emby):
        emby.stale = True
        with pytest.raises(PublishError, match="different file size"):
            _write(emby, [INTRO])
        emby.server.delete_emby_markers.assert_called_once_with("42")

    def test_chapters_not_showing_ours_are_removed_and_fail(self, emby):
        emby.show_nothing = True
        with pytest.raises(PublishError, match="chapters don't show"):
            _write(emby, [INTRO])
        emby.server.delete_emby_markers.assert_called_once_with("42")

    def test_item_grouping_versions_of_other_lengths_is_refused(self, emby):
        emby.server.get_media_source_durations.return_value = [DUR, DUR + 60_000]
        with pytest.raises(PublishError, match="versions of different lengths"):
            _write(emby, [INTRO])
        emby.server.put_emby_markers.assert_not_called()

    @pytest.mark.parametrize(
        ("answer", "error", "state"),
        [
            (MagicMock(status_code=200, **{"json.return_value": {"Found": False, "Error": "item not found"}}), ItemNotFoundError, None),
            (MagicMock(status_code=200, **{"json.return_value": {"Found": True, "Error": "invalid intro ticks"}}), PublishError, None),
            (MagicMock(status_code=404, **{"json.side_effect": ValueError}), PublishError, Capability.NEEDS_PLUGIN),
            (MagicMock(status_code=401), PublishError, None),
            (MagicMock(status_code=403), PublishError, None),
            (MagicMock(status_code=503), PublishError, Capability.UNREACHABLE),
        ],
    )  # fmt: skip
    def test_answers_map_to_errors(self, emby, answer, error, state):
        emby.server.put_emby_markers.side_effect = None
        emby.server.put_emby_markers.return_value = answer
        with pytest.raises(error) as caught:
            _write(emby, [INTRO])
        assert caught.value.state is state

    def test_transport_failure_is_unreachable(self, emby):
        emby.server.put_emby_markers.side_effect = requests.ConnectionError("down")
        with pytest.raises(PublishError) as caught:
            _write(emby, [INTRO])
        assert caught.value.state is Capability.UNREACHABLE


class TestShows:
    def test_ours_missing_and_replaced(self, emby):
        _write(emby, [INTRO, CREDITS_TO_END])
        assert emby.publisher.shows("42", [INTRO, CREDITS_TO_END]) is Shown.OURS
        emby.markers = (1_000_000_000, 1_300_000_000, 12_953_240_000)  # Emby's own intro detection
        assert emby.publisher.shows("42", [INTRO, CREDITS_TO_END]) is Shown.REPLACED
        emby.markers = None
        assert emby.publisher.shows("42", [INTRO]) is Shown.MISSING

    def test_unreadable_chapters_are_unknown(self, emby):
        emby.server.get_chapter_markers.side_effect = None
        emby.server.get_chapter_markers.return_value = None
        assert emby.publisher.shows("42", [INTRO]) is None
```

Add to `tests/markers/test_publisher_contract.py` (import `EmbyServer`; add `"emby"` to `VENDORS`; delete
`test_emby_has_no_publisher_yet`):

```python
@pytest.fixture
def emby(tmp_path):
    from tests.markers.test_emby_publisher import FakeEmby

    path = _media(tmp_path)
    fake = FakeEmby(path)
    cfg = _config("emby-1", ServerType.EMBY, str(tmp_path / "media"))

    def shown():
        return [(r["marker_type"], r["start_ms"]) for r in fake._chapters("x") if r["marker_type"] != "Chapter"]

    return SimpleNamespace(
        name="emby",
        server=fake.server,
        cfg=cfg,
        item_id="4242",
        path=path,
        shown=shown,
        both=[("IntroStart", INTRO.start_ms), ("IntroEnd", INTRO.end_ms), ("CreditsStart", CREDITS.start_ms)],
        # The plugin may still hold what a failed write stored, so an unknown previous clears it.
        after_unknown_clear="cleared",
        failure_leaves_markers=True,
        atomic_writes=False,
    )
```

`tests/markers/test_publisher_factory.py`: add `(ServerType.EMBY, EmbyMarkerPublisher)` to the factory matrix
parametrization.

`tests/markers/test_inspect.py` (new test next to the Emby row tests):

```python
def test_emby_plan_leaves_out_credits_that_end_before_the_file(store, factory):
    early = Marker(T.CREDITS, 1_250_000, 1_290_000, ("chapters",))  # a scene follows: ends 30 s before the file does
    _known_file(
        store,
        {T.INTRO: _decided(INTRO), T.CREDITS: _decided(early), T.RECAP: _none(T.RECAP), T.PREVIEW: _none(T.PREVIEW)},
    )
    registry = _registry(server_config("emby", ServerType.EMBY), server_config("jf", ServerType.JELLYFIN))
    registry.get("emby").get_chapter_markers.return_value = [
        {"marker_type": "IntroStart", "start_ms": 11_000},
        {"marker_type": "IntroEnd", "start_ms": 37_000},
    ]
    payload = inspect.item_payload(PATH, registry=registry, store=store)
    # Emby already shows everything it can show of this decision; Jellyfin still needs both markers.
    assert _row(payload, "emby")["plan"] == "up_to_date"
    assert _row(payload, "jf")["plan"] == "will_add"
```

Replace `test_emby_needs_its_plugin` (it relied on Emby having no publisher) with:

```python
def test_emby_without_the_plugin_needs_it_and_says_whether_the_catalog_has_it():
    cfg = server_config("emby", ServerType.EMBY, markers={"enabled": False, "library_ids": None})
    server = MagicMock(name="emby-client")
    server.get_bridge_info.return_value = {"installed": False, "version": None, "features": []}
    server.bridge_catalog_listed.return_value = False
    payload = inspect.server_status_payload(server, cfg)
    assert payload["capability"]["state"] == "needs_plugin"
    assert payload["capability"]["message"] == "Install the Media Preview Bridge for Emby plugin"
    assert payload["capability"]["details"] == {"catalog_listed": False}
    assert payload["can_show"] == ["intro", "credits"]
```

In the same file's `_PublisherFactory`, keep `return None` for Emby: those Inspector tests stand for "no usable
publisher" and still pass.

- [ ] **Step 3: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_servers_emby_bridge.py tests/markers/test_emby_publisher.py -q`
Expected: FAIL — `ModuleNotFoundError: media_preview_generator.markers.publishers.emby`.

- [ ] **Step 4: Move the shared Bridge calls and add the Emby client**

Cut `get_bridge_info` and `get_bridge_markers_access` from `JellyfinServer` into `EmbyApiClient` (after
`get_plugin_names`), unchanged except:

```python
    # A valid item id no library item has, per vendor: the admin-only Markers route answers it without touching an item.
    _BRIDGE_ACCESS_PROBE_ID = ""
```

on `EmbyApiClient`, `f"/MediaPreviewBridge/Markers/{self._BRIDGE_ACCESS_PROBE_ID}"` in the probe, a `200` answer →
`"ok"` (unchanged), and in `get_bridge_info` read `payload.get("ok", payload.get("Ok"))`,
`payload.get("version", payload.get("Version"))`, `payload.get("features", payload.get("Features"))`. On
`JellyfinServer`: `_BRIDGE_ACCESS_PROBE_ID = "ffffffffffffffffffffffffffffffff"` (and delete the module constant). The
existing Jellyfin tests must pass unchanged — they are the proof the move changed nothing.

In `servers/emby.py` (imports: `urllib.parse`, `requests`):

```python
    PLUGIN_NAME = "Media Preview Bridge for Emby"
    _BRIDGE_ACCESS_PROBE_ID = "999999999999"

    def _markers_path(self, item_id: str) -> str:
        return f"/MediaPreviewBridge/Markers/{urllib.parse.quote(str(item_id), safe='')}"

    def get_emby_marker_state(self, item_id: str) -> dict[str, Any] | None:
        """What the Bridge plugin stores for an item, shown or not.

        Returns:
            ``{"intro_start_ticks", "intro_end_ticks", "credits_start_ticks", "file_size", "stale"}`` (None values when
            nothing is stored), or None when unknown (unknown item, no plugin route, error).
        """
        try:
            resp = self._request("GET", self._markers_path(item_id))
            body = resp.json() if resp.status_code == 200 else None
        except (requests.RequestException, ValueError) as exc:
            logger.debug("Emby Bridge markers read failed on {} for {}: {}", self.name, item_id, type(exc).__name__)
            return None
        if not isinstance(body, dict) or body.get("Found") is not True or body.get("Error"):
            return None

        def ticks(key: str) -> int | None:
            value = body.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return {
            "intro_start_ticks": ticks("IntroStartTicks"),
            "intro_end_ticks": ticks("IntroEndTicks"),
            "credits_start_ticks": ticks("CreditsStartTicks"),
            "file_size": ticks("FileSize"),
            "stale": body.get("Stale") is True,
        }

    def put_emby_markers(
        self,
        item_id: str,
        *,
        intro_start_ticks: int | None,
        intro_end_ticks: int | None,
        credits_start_ticks: int | None,
        file_size: int | None,
    ) -> requests.Response:
        """Replace the plugin's markers for an item; it writes them into Emby's chapter rows straight away.

        Raises:
            requests.RequestException: Transport failure.
        """
        body = {
            "IntroStartTicks": intro_start_ticks,
            "IntroEndTicks": intro_end_ticks,
            "CreditsStartTicks": credits_start_ticks,
            "FileSize": file_size,
        }
        return self._request("POST", self._markers_path(item_id), json_body=body)

    def delete_emby_markers(self, item_id: str) -> requests.Response:
        """Remove the plugin's markers for an item (the file's own chapters stay).

        Raises:
            requests.RequestException: Transport failure.
        """
        return self._request("DELETE", self._markers_path(item_id))

    def bridge_catalog_listed(self) -> bool | None:
        """Whether Emby's plugin catalog offers the plugin (None when the catalog couldn't be read)."""
        try:
            resp = self._request("GET", "/Packages", timeout=20)
            body = resp.json() if resp.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return None
        if not isinstance(body, list):
            return None
        return any(isinstance(p, dict) and (p.get("Name") or p.get("name")) == self.PLUGIN_NAME for p in body)

    def install_plugin(self) -> dict[str, Any]:
        """Install the plugin from Emby's catalog and restart Emby; say so when it has to be installed by hand.

        Returns:
            ``{"steps": [{"step", "ok", "detail"}], "ok", "error", "manual"}`` (same shape as Jellyfin's).
        """
        result: dict[str, Any] = {"steps": [], "ok": False, "error": "", "manual": False}
        listed = self.bridge_catalog_listed()
        if not listed:
            result["manual"] = listed is False
            result["error"] = (
                "Media Preview Bridge for Emby isn't in the Emby plugin catalog yet; install it by hand (see the Intro "
                "& Credits guide)"
                if listed is False
                else "Couldn't read Emby's plugin catalog"
            )
            result["steps"].append({"step": "catalog", "ok": False, "detail": result["error"]})
            return result
        result["steps"].append({"step": "catalog", "ok": True, "detail": "listed"})
        for step, path in (
            ("queue_install", f"/Packages/Installed/{urllib.parse.quote(self.PLUGIN_NAME)}"),
            ("restart", "/System/Restart"),
        ):
            try:
                self._request("POST", path).raise_for_status()
            except requests.RequestException as exc:
                result["error"] = f"{step} failed: {type(exc).__name__}"
                result["steps"].append({"step": step, "ok": False, "detail": result["error"]})
                return result
            result["steps"].append({"step": step, "ok": True, "detail": ""})
        result["ok"] = True
        return result
```

(`MagicMock` answers in the tests have a callable `raise_for_status`; a 204 mock doesn't raise.)

- [ ] **Step 5: Implement the base additions and the publisher**

`base.py` `MarkerPublisher`:

```python
    def project(self, markers: Iterable[Marker], *, duration_ms: int | None = None) -> list[Marker]:
        """Keep supported types, ordered by start (``duration_ms`` matters only to servers with extra rules: Emby)."""
        return sorted((m for m in markers if m.type in self.supported_types), key=lambda m: (m.start_ms, m.type.value))

    def projection_note(self, markers: Iterable[Marker], *, duration_ms: int | None) -> str:
        """Why decided markers of a supported type aren't sent to this server ("" when all are)."""
        return ""
```

```python
# media_preview_generator/markers/publishers/emby.py
"""Emby publisher via the Media Preview Bridge for Emby plugin (spec §3.3, §6.3).

Emby has no marker write API. The plugin stores what we POST, writes it into the item's chapter rows as IntroStart /
IntroEnd / CreditsStart (keeping the file's own chapters) and writes it back after a FullRefresh deletes it. Emby keeps
no credits end, so its credits marker means "to the end of the file": credits that end earlier (a scene after them)
aren't sent. Every POST is confirmed by reading the item's chapters back.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import requests
from loguru import logger

from ..decide import EOF_CLAMP_MS
from ..models import Marker, MarkerType
from ..sources.server_markers import SAME_CUT_MS
from .base import Capability, CapabilityReport, ItemNotFoundError, MarkerPublisher, PublishError, Shown, compare_shown

if TYPE_CHECKING:
    from ...servers.base import ServerConfig
    from ...servers.emby import EmbyServer
    from ..settings import ServerMarkersSettings

TICKS_PER_MS = 10_000
MARKERS_FEATURE = "markers"
CREDITS_BEFORE_END_NOTE = "Emby can't show credits that end before the file does"


def credits_run_to_end(marker: Marker, duration_ms: int | None) -> bool:
    """Whether a credits marker ends within 2 s of the file's end (unknown duration: can't tell, so no)."""
    return duration_ms is not None and marker.end_ms >= duration_ms - EOF_CLAMP_MS


def _times(markers: Iterable[Marker]) -> list[tuple[MarkerType, int, int]]:
    return [(m.type, m.start_ms, m.end_ms) for m in markers]


def _file_size(path: str) -> int | None:
    try:
        return os.stat(path).st_size
    except OSError:
        return None


class EmbyMarkerPublisher(MarkerPublisher):
    """Pushes markers to the Emby Bridge plugin, which keeps them in Emby's chapter rows."""

    supported_types = frozenset({MarkerType.INTRO, MarkerType.CREDITS})
    name = "emby_bridge"
    # The plugin stores before its chapters are read back, so a PublishError can follow a POST that changed its store.
    atomic_writes = False

    def __init__(self, server: EmbyServer, config: ServerConfig, settings: ServerMarkersSettings) -> None:
        """Create the publisher.

        Args:
            server: Live ``EmbyServer`` client.
            config: That server's ``ServerConfig``.
            settings: That server's ``ServerMarkersSettings``.
        """
        self._server = server
        self._config = config
        self._settings = settings

    def project(self, markers: Iterable[Marker], *, duration_ms: int | None = None) -> list[Marker]:
        """Supported types by start, credits only when they run to the end of the file."""
        return [
            m
            for m in super().project(markers)
            if m.type is not MarkerType.CREDITS or credits_run_to_end(m, duration_ms)
        ]

    def projection_note(self, markers: Iterable[Marker], *, duration_ms: int | None) -> str:
        """The note for credits left out because they end before the file does."""
        credits = [m for m in markers if m.type is MarkerType.CREDITS]
        return CREDITS_BEFORE_END_NOTE if any(not credits_run_to_end(m, duration_ms) for m in credits) else ""

    def capability(self) -> CapabilityReport:
        """Check the switch, the plugin (with the markers feature) and administrator access."""
        if not self._settings.enabled:
            return CapabilityReport(Capability.DISABLED, "Intro & Credits is off for this server")
        info = self._server.get_bridge_info()
        if info is None:
            return CapabilityReport(Capability.UNREACHABLE, "Can't reach this Emby server")
        if not info.get("installed"):
            return CapabilityReport(
                Capability.NEEDS_PLUGIN,
                "Install the Media Preview Bridge for Emby plugin",
                {"catalog_listed": self._server.bridge_catalog_listed()},
            )
        version = info.get("version")
        if MARKERS_FEATURE not in (info.get("features") or []):
            return CapabilityReport(
                Capability.PLUGIN_OUTDATED,
                f"Update Media Preview Bridge for Emby (installed {version or 'unknown'}) to get markers support",
                {"plugin_version": version},
            )
        access = self._server.get_bridge_markers_access()
        if access == "unauthorized":
            return CapabilityReport(Capability.MISCONFIGURED, "Emby rejected this server's credentials; reconnect it")
        if access == "forbidden":
            return CapabilityReport(
                Capability.MISCONFIGURED,
                "Emby refused the Media Preview Bridge markers endpoint; this server's API key or user needs "
                "administrator rights",
            )
        if access != "ok":
            return CapabilityReport(
                Capability.UNREACHABLE, "Can't reach the Media Preview Bridge markers endpoint on this Emby server"
            )
        return CapabilityReport(
            Capability.READY,
            "Media Preview Bridge for Emby plugin",
            {"plugin_version": version, "can_show": ["intro", "credits"]},
        )

    def write(
        self,
        item_id: str,
        markers: list[Marker],
        *,
        previous: list[Marker] | None,
        duration_ms: int | None,
        canonical_path: str,
        own_previous: list[Marker] | None = None,
        kept_types: frozenset[MarkerType] = frozenset(),
    ) -> list[Marker]:
        """Replace the plugin's markers for the item and confirm Emby's chapters show them.

        ``own_previous`` and ``kept_types`` are ignored: an Emby item's chapters belong to it alone, and there is no
        "keep Emby's" setting.

        Returns:
            The markers that are ours on the item now (``project(markers, duration_ms=)``, or ``[]``).

        Raises:
            ItemNotFoundError: Emby doesn't know the item yet.
            PublishError: Not written, or stored but not shown (then removed again, best effort).
        """
        self.last_write_changed = False
        wanted = self.project(markers, duration_ms=duration_ms)
        if wanted:
            # Removing ours is always allowed; showing one set on an item holding several cuts isn't.
            self._check_one_cut(item_id, duration_ms)
        if wanted and previous is not None and _times(MarkerPublisher.project(self, previous)) == _times(wanted):
            if self._plugin_holds_this_file(item_id, canonical_path) and self.shows(item_id, wanted) is Shown.OURS:
                return wanted
        try:
            if not wanted:
                if previous is None or previous:
                    self._check(self._server.delete_emby_markers(item_id), item_id)
                    self.last_write_changed = True
                return []
            intro = next((m for m in wanted if m.type is MarkerType.INTRO), None)
            credits = next((m for m in wanted if m.type is MarkerType.CREDITS), None)
            body = self._check(
                self._server.put_emby_markers(
                    item_id,
                    intro_start_ticks=intro.start_ms * TICKS_PER_MS if intro else None,
                    intro_end_ticks=intro.end_ms * TICKS_PER_MS if intro else None,
                    credits_start_ticks=credits.start_ms * TICKS_PER_MS if credits else None,
                    file_size=_file_size(canonical_path),
                ),
                item_id,
            )
        except requests.RequestException as exc:
            raise PublishError(
                f"Can't reach Emby ({type(exc).__name__}); markers not written", state=Capability.UNREACHABLE
            ) from exc
        self.last_write_changed = True
        if body.get("Stale"):
            self._remove_unconfirmed(item_id)
            raise PublishError(
                "Emby sees a different file size for this item than the analysed file; markers stored but not shown"
            )
        shown = self.shows(item_id, wanted)
        if shown is not Shown.OURS:
            self._remove_unconfirmed(item_id)
            if shown is None:
                raise PublishError("Stored on Emby but couldn't confirm it is shown", state=Capability.UNREACHABLE)
            raise PublishError("Emby stored the markers but its chapters don't show them; check the Emby log")
        logger.info("Emby {}: stored {} marker(s) for item {}", self._config.name, len(wanted), item_id)
        return wanted

    def shows(
        self,
        item_id: str,
        ours: list[Marker],
        *,
        kept_types: frozenset[MarkerType] = frozenset(),
        item_files: tuple[str, ...] | None = None,
    ) -> Shown | None:
        """Read the item's chapter markers.

        Args:
            item_id: Emby item id.
            ours: What this app last left on the item.
            kept_types: Ignored (see ``write``).
            item_files: Ignored (versions of one Emby item are refused in ``write``).

        Returns:
            How Emby's markers compare with ``ours`` (credits compared by start: Emby has no end); None when the chapters
            couldn't be read.
        """
        rows = self._server.get_chapter_markers(item_id)
        if rows is None:
            return None
        starts: dict[str, list[int]] = {}
        for row in rows:
            starts.setdefault(row["marker_type"], []).append(row["start_ms"])
        served: dict[MarkerType, list[tuple[int, int]]] = {}
        if len(starts.get("IntroStart", [])) == 1 and len(starts.get("IntroEnd", [])) == 1:
            served[MarkerType.INTRO] = [(starts["IntroStart"][0], starts["IntroEnd"][0])]
        credits_end = next((m.end_ms for m in ours if m.type is MarkerType.CREDITS), None)
        served[MarkerType.CREDITS] = [(start, credits_end) for start in starts.get("CreditsStart", [])]
        return compare_shown(MarkerPublisher.project(self, ours), served, others_alongside=False)

    def _check_one_cut(self, item_id: str, duration_ms: int | None) -> None:
        durations = self._server.get_media_source_durations(item_id)
        if not durations or len(durations) < 2:
            return
        if duration_ms is None or not all(d is not None and abs(d - duration_ms) <= SAME_CUT_MS for d in durations):
            raise PublishError("This Emby item groups versions of different lengths; one marker set can't fit them all")

    def _plugin_holds_this_file(self, item_id: str, canonical_path: str) -> bool:
        size = _file_size(canonical_path)
        state = self._server.get_emby_marker_state(item_id)
        return size is not None and state is not None and not state["stale"] and state["file_size"] == size

    def _remove_unconfirmed(self, item_id: str) -> None:
        try:
            resp = self._server.delete_emby_markers(item_id)
        except requests.RequestException as exc:
            logger.warning("Emby {}: couldn't remove unconfirmed markers for item {} ({})", self._config.name, item_id,
                           type(exc).__name__)  # fmt: skip
            return
        if resp.status_code != 200:
            logger.warning("Emby {}: couldn't remove unconfirmed markers for item {} (HTTP {})", self._config.name,
                           item_id, resp.status_code)  # fmt: skip

    @staticmethod
    def _check(resp: requests.Response, item_id: str) -> dict[str, Any]:
        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError as exc:
                raise PublishError("Media Preview Bridge for Emby returned an unreadable answer") from exc
            if not isinstance(body, dict):
                raise PublishError("Media Preview Bridge for Emby returned an unreadable answer")
            if body.get("Found") is False:
                raise ItemNotFoundError(f"Emby doesn't know item {item_id} (yet)")
            if body.get("Error"):
                raise PublishError(f"Media Preview Bridge for Emby refused the markers: {body['Error']}")
            return body
        if resp.status_code == 404:
            raise PublishError(
                "Media Preview Bridge for Emby isn't installed or has no markers endpoint", state=Capability.NEEDS_PLUGIN
            )
        if resp.status_code == 401:
            raise PublishError("Emby rejected this server's credentials (HTTP 401); reconnect it")
        if resp.status_code == 403:
            raise PublishError(
                "Emby refused the markers write (HTTP 403); the server's API key or user needs administrator rights"
            )
        if resp.status_code in (502, 503, 504):
            raise PublishError(
                f"Emby is unavailable (HTTP {resp.status_code}); markers not written", state=Capability.UNREACHABLE
            )
        raise PublishError(f"Media Preview Bridge for Emby returned HTTP {resp.status_code}")
```

`factory.py`: after the Jellyfin branch

```python
    if config.type is ServerType.EMBY:
        from .emby import EmbyMarkerPublisher

        return EmbyMarkerPublisher(server, config, settings)
```

and the docstring's Returns line becomes "The publisher, or None for a server type without one.".

- [ ] **Step 6: Pipeline, Inspector, route, fakes**

`pipeline.py` `_publish_to`: replace `wanted = publisher.project(markers.values())` with

```python
    wanted = publisher.project(markers.values(), duration_ms=rec.duration_ms)
    # Decided markers this server can't show the way they were decided (Emby: credits ending before the file does).
    unshown = publisher.projection_note(markers.values(), duration_ms=rec.duration_ms)
```

and join `unshown` into the three messages a server row can end with: in `_up_to_date`
`message = with_kept_note(with_kept_note("", kept_note(kept_types, wanted)) or "Up to date", unshown)`; in the
"nothing to write" branch (`if not wanted and previous == [] and own_previous is None`, not needs review)
`message = with_kept_note("This server can't show the markers found for this file" if markers else "No markers found", unshown)`; before `written = _finish(...)`
`message = with_kept_note(message, unshown)`. Add a pipeline test in `tests/markers/test_pipeline.py`:

```python
def test_a_note_for_markers_the_server_cant_show_reaches_the_row(store, media):
    reg = _registry(media, ServerType.EMBY)
    emby = ready_publisher("emby_bridge")
    emby.projection_note.return_value = "Emby can't show credits that end before the file does"
    out, _ = _run(_ctx(store, reg), media, {"emby-1": emby}, probe=_probe(CHAPTERS_BOTH))
    row = out.publisher_rows[0]
    assert row["status"] == ServerStatus.WRITTEN.value
    assert row["message"] == "2 marker(s); Emby can't show credits that end before the file does"
    assert emby.projection_note.call_args.kwargs == {"duration_ms": DUR}
    # The first projection is the pipeline's; the fake's write projects again without a duration.
    assert emby.project.call_args_list[0].kwargs == {"duration_ms": DUR}
```

`tests/markers/fakes.py` `ready_publisher`:
`pub.project.side_effect = lambda ms, **kw: sorted((m for m in ms if m.type in pub.supported_types), key=lambda m: (m.start_ms, m.type.value))`
and `pub.projection_note.return_value = ""`.

`inspect.py`: delete `EMBY_NEEDS_PLUGIN_MESSAGE`; in `_checked_as_if_on` the `publisher is None` fallback message
becomes `"No marker publisher for this server type"`; in `_server_row` build `wanted` with the publisher rule:

```python
    wanted = sorted(
        (
            m
            for m in markers.values()
            if m.type.value in can_show
            and not (cfg.type is ServerType.EMBY and m.type is MarkerType.CREDITS
                     and not credits_run_to_end(m, rec.duration_ms if rec else None))
        ),
        key=lambda m: (m.start_ms, m.type.value),
    )  # fmt: skip
```

(import `credits_run_to_end` from `.publishers.emby`).

`api_servers.py` `install_jellyfin_plugin`: `if cfg.type not in (ServerType.JELLYFIN, ServerType.EMBY): return
jsonify({"ok": False, "error": "plugin install is for Jellyfin and Emby servers"}), 400`; the `hasattr` error text says
"this client doesn't support plugin install"; docstring mentions Emby's catalog-or-manual answer. Update the route tests
that asserted the Jellyfin-only wording, and add one that an Emby server gets `EmbyServer.install_plugin()`'s dict back
(patch `_instantiate_for_probe` as the Jellyfin route test does) and that `_forget_marker_capability(server_id)` still
runs.

- [ ] **Step 7: Run the tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers tests/test_servers_emby_bridge.py tests/test_servers_jellyfin*.py tests/test_servers_emby*.py tests/test_routes*.py -q`
Expected: PASS.

- [ ] **Step 8: Cassettes from the lab Emby (plugin from Task 4 installed)**

```python
# tests/test_servers_emby_markers_vcr.py
"""Cassette-backed contract tests for the Emby Bridge markers routes and Emby's chapter read (recorded on mlab-emby)."""

from __future__ import annotations

import os

import pytest

from media_preview_generator.servers import EmbyServer, Library, ServerConfig, ServerType

pytestmark = [pytest.mark.vcr]

SYNTH_E01 = "/media/synth-chapters/Synth Chapters (2021)/Season 01/Synth Chapters (2021) - S01E01.webm"


@pytest.fixture
def emby_lab():
    auth = {"method": "api_key", "api_key": os.environ.get("EMBY_TOKEN", "fake-token")}
    if os.environ.get("EMBY_USER_ID"):
        auth["user_id"] = os.environ["EMBY_USER_ID"]
    return EmbyServer(
        ServerConfig(
            id="emby-vcr-markers",
            type=ServerType.EMBY,
            name="Emby VCR",
            enabled=True,
            url=os.environ.get("EMBY_URL", "http://fake-emby.local:8096"),
            auth=auth,
            verify_ssl=False,
            libraries=[Library(id="1", name="Synth Chapters", remote_paths=("/media/synth-chapters",), enabled=True)],
        )
    )


class TestEmbyBridgeMarkersContract:
    def test_ping_access_store_show_delete(self, emby_lab):
        assert emby_lab.get_bridge_info()["features"] == ["markers"]
        assert emby_lab.get_bridge_markers_access() == "ok"
        item_id = emby_lab._uncached_resolve_remote_path_to_item_id(SYNTH_E01)
        assert item_id
        before = emby_lab.get_emby_marker_state(item_id)
        assert before is not None
        resp = emby_lab.put_emby_markers(item_id, intro_start_ticks=100_000_000, intro_end_ticks=400_000_000,
                                         credits_start_ticks=1_000_000_000, file_size=before["file_size"])  # fmt: skip
        assert resp.status_code == 200 and resp.json()["Stored"] == 3
        kinds = {(r["marker_type"], r["start_ms"]) for r in emby_lab.get_chapter_markers(item_id)}
        assert {("IntroStart", 10_000), ("IntroEnd", 40_000), ("CreditsStart", 100_000)} <= kinds
        assert any(r["marker_type"] == "Chapter" for r in emby_lab.get_chapter_markers(item_id))
        assert emby_lab.delete_emby_markers(item_id).status_code == 200
        assert emby_lab.get_emby_marker_state(item_id)["intro_start_ticks"] is None
        if before["intro_start_ticks"] is not None or before["credits_start_ticks"] is not None:
            emby_lab.put_emby_markers(item_id, intro_start_ticks=before["intro_start_ticks"],
                                      intro_end_ticks=before["intro_end_ticks"],
                                      credits_start_ticks=before["credits_start_ticks"], file_size=before["file_size"])  # fmt: skip

    def test_catalog_check(self, emby_lab):
        assert emby_lab.bridge_catalog_listed() in (True, False)
```

Record: `set -a; . docs/design/intro-credits/evidence/lab/env; set +a; EMBY_URL=http://127.0.0.1:18096
EMBY_USER_ID="$EMBY_UID" /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_servers_emby_markers_vcr.py
--record-mode=once`, then the token-leak grep from Task 5 on `tests/cassettes/test_servers_emby_markers_vcr/`
(`$EMBY_TOKEN`), then replay offline.

- [ ] **Step 9: Lab proof through the app**

On `mlab-app` rebuilt from this branch (`app.sh recreate` after the image build in Task 17's Step 1 command), turn on
Intro & Credits for `mlab-emby` (`PUT /api/servers/mlab-emby {"markers": {"enabled": true, "library_ids": null}}`),
run `POST /api/markers/jobs {"file_paths": ["/media/synth-chapters/Synth Chapters (2021)"]}`, and check: job rows for
`mlab-emby` are `markers_written`; `GET /Users/{uid}/Items/{id}?Fields=Chapters` on each synth episode shows
IntroStart/IntroEnd/CreditsStart at the chapter truth (`phase1_matrix.SYNTH_TRUTH`) plus the original chapters; a
second job is Up to date with zero POSTs (plugin store file mtimes unchanged). Record in `phase2-results.md` → "Task
10".

- [ ] **Step 10: Commit**

`feat(markers): Emby publisher through Media Preview Bridge for Emby`.

---
## Task 11: Reconcile sweep (bulk read-back, 12-hourly "Check servers" job, on-demand route)

`[sequential]` (after Tasks 9, 10) `[high-risk]` — spec §6.2 step 6 ("Reconcile (periodic + after jobs): read markers
back from each server; if they differ (Plex forced detection, Emby FullRefresh without heal), re-publish"), §14
2026-09-14 read-back lines (phase 1 already reads back before "Up to date" and queues a delayed verify job for replaced
files — the "after jobs" half), roadmap phase 2 bullet `reconcile.py`, ledger parked item progress.md line 184 (with
Task 9's `VERSIONS_CHANGED`).

**OWNER DECISION 2026-09-14 (supersedes R5 below):** no built-in 12-hour job. "Check servers" is something the user
schedules like any other job: in the existing schedule modal (Automation → Schedules), the Intro & Credits job type
gets a "Check servers" choice, run on the schedule the user sets (same schedule store and runner as preview and
Intro & Credits schedules). `POST /api/markers/reconcile` still queues it on demand, and the dashboard's Start job
modal offers it too. Nothing is scheduled by default. Drop `ScheduleManager.apply_markers_reconcile`, the
`__markers_reconcile` interval job and its test; test instead that a saved "Check servers" schedule queues exactly one
LOW job (none while one is queued or running) and that no schedule exists after a fresh start. Task 16 updates docs.

**OWNER RULING R5 (recommended default implemented here):** the periodic half is an ordinary LOW Intro & Credits job
named "Intro & Credits · Check servers", queued every 12 hours when any server has Intro & Credits on and no such job
is already queued or running. It shows on the dashboard like any job (gate, pause, cancel, logs). Its file listing reads
back every published server item in bulk and keeps only the files of drifted items; the pipeline then does the actual
re-publish for those files, with its normal rules (`keep_plex`, versions, consent). When nothing drifted it completes at
once with a log line. `POST /api/markers/reconcile` queues it on demand (the lab uses this; no UI button in phase 2).

**Files:**
- Create: `media_preview_generator/markers/reconcile.py`
- Modify: `media_preview_generator/markers/store.py` (`published_items`, `files_for_item`),
  `media_preview_generator/markers/publishers/base.py` (`ReadBackItem`, `shows_many`),
  `media_preview_generator/markers/publishers/plex_db.py` (`READ_BACK_CHUNK`, `shows_many`, `_shown_in`; `shows`
  delegates), `media_preview_generator/markers/job_runner.py` (`build_items` reconcile branch, empty-reconcile
  completion, no retries for reconcile), `media_preview_generator/markers/triggers.py` (`create_intro_credits_job(...,
  reconcile=False)`), `media_preview_generator/web/scheduler.py` (`ScheduleManager.apply_markers_reconcile`),
  `media_preview_generator/web/app.py` (call it after `schedule_manager.start()`),
  `media_preview_generator/web/routes/api_markers.py` (`POST /markers/reconcile`)
- Test: `tests/markers/test_reconcile.py`, `tests/markers/test_store.py` (published items), `tests/markers/test_plex_db_publisher.py`
  (`shows_many` equals `shows`), `tests/markers/test_api_markers.py` (route), `tests/test_scheduler*.py` (registration;
  use the file that tests `apply_quiet_hours`)

**Interfaces:**
- Consumes: Task 9 `Shown.VERSIONS_CHANGED`, `ItemPublishStateRow.item_files`; Task 10 Emby publisher; phase 1
  `publisher_for`, `markers_for_path`, `load_server`, `create_intro_credits_job`, `markers_enabled_anywhere`.
- Produces:
```python
# markers/publishers/base.py
ReadBackItem = tuple[str, list[Marker], frozenset[MarkerType], tuple[str, ...] | None]   # item id, ours, kept, files
MarkerPublisher.shows_many(self, items: list[ReadBackItem], *, cancel_check: Callable[[], bool] | None = None) -> dict[str, Shown | None]
# markers/store.py
MarkerStore.published_items(self, server_id: str) -> list[ItemPublishStateRow]     # status "written" with markers or kept types
MarkerStore.files_for_item(self, server_id: str, item_id: str) -> list[str]      # canonical paths, sorted
# markers/reconcile.py
RECONCILE_JOB_ID = "__markers_reconcile"; RECONCILE_INTERVAL_HOURS = 12; RECONCILE_SOURCE = "reconcile"
RECONCILE_JOB_NAME = "Intro & Credits · Check servers"
@dataclass(frozen=True) class Drift: server_id: str; item_id: str; shown: Shown; files: tuple[str, ...]
def find_drift(*, registry: Any, store: MarkerStore, cancel_check: Callable[[], bool] | None = None) -> tuple[list[Drift], list[str]]
def run_markers_reconcile() -> str | None
# markers/triggers.py
create_intro_credits_job(..., reconcile: bool = False) -> Job                     # config["reconcile"] = True
# web/scheduler.py
ScheduleManager.apply_markers_reconcile(self) -> None
# web/routes/api_markers.py
POST /api/markers/reconcile → 202 {"job_id"} | 200 {"job_id": null, "reason"}
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/markers/test_reconcile.py
"""Reconcile: which published items drifted, the files to run again, and queueing the job."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers import job_runner, reconcile
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType
from media_preview_generator.markers.publishers.base import Capability, CapabilityReport, Shown
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import FakeRegistry, ready_publisher, server_config

T = MarkerType
INTRO = Marker(T.INTRO, 10_000, 40_000, ("chapters",))


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _published(store, server_id, item_id, path, *, markers=(INTRO,), kept=None, files=None):
    rec = store.upsert_file(FileIdentity(path, 1, 1), duration_ms=120_000, season_key=None, is_movie=False)
    store.set_publish_state(rec.id, server_id, item_id=item_id, markers=list(markers), status="written")
    store.set_item_publish_state(server_id, item_id, list(markers), "written", kept_types=kept, item_files=files)


def _registry(*configs):
    return FakeRegistry({c.id: c for c in configs})


class TestFindDrift:
    def test_only_drifted_items_are_returned_with_their_files(self, store):
        cfg = server_config("plex-1", ServerType.PLEX)
        _published(store, "plex-1", "1", "/m/a.mkv", files=("/m/a.mkv",))
        _published(store, "plex-1", "2", "/m/b.mkv")
        _published(store, "plex-1", "3", "/m/c.mkv")
        pub = ready_publisher()
        pub.shows_many.return_value = {"1": Shown.OURS, "2": Shown.REPLACED, "3": Shown.VERSIONS_CHANGED}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, warnings = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert [(d.item_id, d.shown, d.files) for d in drifts] == [
            ("2", Shown.REPLACED, ("/m/b.mkv",)),
            ("3", Shown.VERSIONS_CHANGED, ("/m/c.mkv",)),
        ]
        assert warnings == []
        (items,), kwargs = pub.shows_many.call_args
        assert [(i[0], i[1], i[2], i[3]) for i in items] == [
            ("1", [INTRO], frozenset(), ("/m/a.mkv",)),
            ("2", [INTRO], frozenset(), None),
            ("3", [INTRO], frozenset(), None),
        ]
        assert "cancel_check" in kwargs

    def test_unreadable_items_are_counted_in_a_warning_not_rewritten(self, store):
        cfg = server_config("jf-1", ServerType.JELLYFIN)
        _published(store, "jf-1", "x", "/m/a.mkv")
        pub = ready_publisher("jellyfin_bridge")
        pub.shows_many.return_value = {"x": None}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, warnings = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert drifts == [] and warnings == ["Couldn't read what 1 item(s) show on JF-1"]

    @pytest.mark.parametrize(
        ("setting", "drift"), [("restore", True), ("keep_plex", False)], ids=["released", "still-kept"]
    )
    def test_kept_types_are_released_when_the_server_is_set_to_use_ours(self, store, setting, drift):
        markers = {"enabled": True, "library_ids": None,
                   "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": setting}}  # fmt: skip
        cfg = server_config("plex-1", ServerType.PLEX, markers=markers)
        _published(store, "plex-1", "1", "/m/a.mkv", kept={T.CREDITS})
        pub = ready_publisher()
        pub.shows_many.return_value = {"1": Shown.OURS}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, _ = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert bool(drifts) is drift

    @pytest.mark.parametrize(
        "cfg",
        [
            server_config("plex-1", ServerType.PLEX, markers={"enabled": False, "library_ids": None}),
            server_config("plex-1", ServerType.PLEX, enabled=False),
        ],
        ids=["markers-off", "server-off"],
    )
    def test_servers_with_intro_and_credits_off_are_not_read(self, store, cfg):
        _published(store, "plex-1", "1", "/m/a.mkv")
        with patch.object(reconcile, "publisher_for") as factory:
            assert reconcile.find_drift(registry=_registry(cfg), store=store) == ([], [])
        factory.assert_not_called()

    def test_a_server_that_cant_take_markers_is_skipped_with_its_reason(self, store):
        cfg = server_config("plex-1", ServerType.PLEX)
        _published(store, "plex-1", "1", "/m/a.mkv")
        pub = ready_publisher()
        pub.capability.return_value = CapabilityReport(Capability.NEEDS_LOCAL_DB, "Plex's database isn't local")
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, warnings = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert drifts == [] and warnings == ["Skipped PLEX-1: Plex's database isn't local"]
        pub.shows_many.assert_not_called()

    def test_default_shows_many_reads_each_item_and_stops_on_cancel(self):
        pub = ready_publisher()
        from media_preview_generator.markers.publishers.base import MarkerPublisher

        calls = iter([False, True])
        out = MarkerPublisher.shows_many(pub, [("1", [INTRO], frozenset(), None), ("2", [INTRO], frozenset(), None)],
                                         cancel_check=lambda: next(calls))  # fmt: skip
        assert out == {"1": Shown.OURS}
        assert pub.shows.call_args.kwargs == {"kept_types": frozenset(), "item_files": None}


class TestReconcileJob:
    def test_items_are_the_drifted_files_without_hints(self, store):
        drifts = [reconcile.Drift("plex-1", "2", Shown.REPLACED, ("/m/b.mkv", "/m/a.mkv")),
                  reconcile.Drift("jf-1", "x", Shown.MISSING, ("/m/a.mkv",))]  # fmt: skip
        with (
            patch.object(reconcile, "find_drift", return_value=(drifts, ["Couldn't read what 1 item(s) show on X"])),
            patch("media_preview_generator.markers.store.get_marker_store", return_value=store),
        ):
            items, warnings, sent = job_runner.build_items({"reconcile": True}, registry=MagicMock())
        assert [(i.canonical_path, i.item_id_by_server, i.server_id) for i in items] == [
            ("/m/a.mkv", {}, ""),
            ("/m/b.mkv", {}, ""),
        ]
        assert warnings == ["Couldn't read what 1 item(s) show on X"] and sent == {}

    def test_run_queues_one_low_job_when_markers_are_on_somewhere(self, monkeypatch):
        jm = MagicMock()
        jm.get_pending_jobs.return_value = []
        jm.get_running_jobs.return_value = []
        monkeypatch.setattr(reconcile, "get_job_manager", lambda: jm)
        with (
            patch("media_preview_generator.markers.triggers.markers_enabled_anywhere", return_value=True),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job", return_value=MagicMock(id="r1")) as create,
        ):
            assert reconcile.run_markers_reconcile() == "r1"
        assert create.call_args.kwargs == {
            "library_name": "Intro & Credits · Check servers", "priority": 3, "source": "reconcile", "reconcile": True,
        }  # fmt: skip

    def test_run_reuses_a_queued_or_running_reconcile_job(self, monkeypatch):
        jm = MagicMock()
        queued = MagicMock(id="r0", kind=JOB_KIND_INTRO_CREDITS, config={"reconcile": True})
        jm.get_pending_jobs.return_value = [queued]
        jm.get_running_jobs.return_value = []
        monkeypatch.setattr(reconcile, "get_job_manager", lambda: jm)
        with (
            patch("media_preview_generator.markers.triggers.markers_enabled_anywhere", return_value=True),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job") as create,
        ):
            assert reconcile.run_markers_reconcile() == "r0"
        create.assert_not_called()

    def test_run_does_nothing_when_markers_are_off_everywhere(self):
        with (
            patch("media_preview_generator.markers.triggers.markers_enabled_anywhere", return_value=False),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job") as create,
        ):
            assert reconcile.run_markers_reconcile() is None
        create.assert_not_called()
```

`tests/markers/test_store.py`:

```python
class TestPublishedItems:
    def test_written_items_with_markers_or_kept_types_and_their_files(self, store):
        m = Marker(MarkerType.INTRO, 1_000, 30_000, ("chapters",))
        a = store.upsert_file(FileIdentity("/m/b.mkv", 1, 1), duration_ms=1, season_key=None, is_movie=False)
        b = store.upsert_file(FileIdentity("/m/a.mkv", 1, 1), duration_ms=1, season_key=None, is_movie=False)
        for rec in (a, b):
            store.set_publish_state(rec.id, "plex-1", item_id="7", markers=[m], status="written")
        store.set_item_publish_state("plex-1", "7", [m], "written")
        store.set_item_publish_state("plex-1", "8", [], "written", kept_types={MarkerType.CREDITS})
        store.set_item_publish_state("plex-1", "9", [], "written")
        store.set_item_publish_state("plex-1", "10", [m], "failed")
        store.set_item_publish_state("jf-1", "11", [m], "written")
        assert [r.item_id for r in store.published_items("plex-1")] == ["7", "8"]
        assert store.files_for_item("plex-1", "7") == ["/m/a.mkv", "/m/b.mkv"]
        assert store.files_for_item("plex-1", "8") == []
```

`tests/markers/test_plex_db_publisher.py` (its `_make_db`, `_publisher`, `_write_one`, `INTRO`, `CREDITS_FINAL`; the
default part is `/data/tv/S01E01.mkv` on item 7; add `Shown` to the imports if missing):

```python
class TestReadBackMany:
    def test_shows_many_answers_what_shows_answers(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO, CREDITS_FINAL])
        files = ("/data/tv/S01E01.mkv",)
        items = [("7", [INTRO, CREDITS_FINAL], frozenset(), files), ("not-a-key", [INTRO], frozenset(), None)]
        assert pub.shows_many(items) == {"7": Shown.OURS, "not-a-key": None}
        assert pub.shows("7", [INTRO, CREDITS_FINAL], item_files=files) is Shown.OURS
        assert pub.shows_many([("7", [INTRO], frozenset(), ("/data/tv/other.mkv",))]) == {"7": Shown.VERSIONS_CHANGED}

    def test_one_database_connection_per_chunk(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        opened = []
        real = type(pub)._database

        def counting(self, **kwargs):
            opened.append(kwargs["read_only"])
            return real(self, **kwargs)

        monkeypatch.setattr(type(pub), "_database", counting)
        out = pub.shows_many([(str(n), [], frozenset(), None) for n in range(1, 1002)])
        assert opened == [True, True, True] and len(out) == 1001
```

`tests/markers/test_api_markers.py` (it has `client` and `api_headers` from `tests/markers/conftest.py`):

```python
class TestReconcileRoute:
    def test_queues_the_reconcile_job(self, client):
        from tests.markers.conftest import api_headers

        with patch("media_preview_generator.markers.reconcile.run_markers_reconcile", return_value="r1") as run:
            resp = client.post("/api/markers/reconcile", headers=api_headers())
        assert resp.status_code == 202 and resp.get_json() == {"job_id": "r1"}
        run.assert_called_once_with()

    def test_nothing_to_check_says_why(self, client):
        from tests.markers.conftest import api_headers

        with patch("media_preview_generator.markers.reconcile.run_markers_reconcile", return_value=None):
            resp = client.post("/api/markers/reconcile", headers=api_headers())
        assert resp.status_code == 200
        assert resp.get_json() == {"job_id": None, "reason": "Intro & Credits is off on every server"}

    def test_needs_auth(self, app):
        assert app.test_client().post("/api/markers/reconcile").status_code == 401
```

Scheduler test (in the file testing `apply_quiet_hours`, using its `ScheduleManager` fixture):

```python
def test_markers_reconcile_is_registered_once_every_12_hours(schedule_manager):
    schedule_manager.apply_markers_reconcile()
    schedule_manager.apply_markers_reconcile()
    jobs = [j for j in schedule_manager.scheduler.get_jobs() if j.id == "__markers_reconcile"]
    assert len(jobs) == 1
    assert jobs[0].trigger.interval.total_seconds() == 12 * 3600
    assert jobs[0].func_ref == "media_preview_generator.markers.reconcile:run_markers_reconcile"
    assert all(not s["id"].startswith("__") for s in schedule_manager.get_all_schedules())
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_reconcile.py tests/markers/test_store.py::TestPublishedItems -q`
Expected: FAIL — `ImportError: cannot import name 'reconcile'`.

- [ ] **Step 3: Implement store, base and Plex bulk read**

`store.py`:

```python
    def published_items(self, server_id: str) -> list[ItemPublishStateRow]:
        """Server items where this app's last write succeeded and something of ours (or kept) is there, by item id."""
        with self._lock:
            ids = [
                r["item_id"]
                for r in self._conn.execute(
                    "SELECT item_id FROM item_publish_state WHERE server_id=? AND status='written' ORDER BY item_id",
                    (server_id,),
                ).fetchall()
            ]
        rows = [row for item_id in ids if (row := self.get_item_publish_state(server_id, item_id)) is not None]
        return [row for row in rows if row.markers or row.kept_types]

    def files_for_item(self, server_id: str, item_id: str) -> list[str]:
        """Local files whose last publish to this server went to this item, sorted."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.canonical_path FROM publish_state p JOIN files f ON f.id = p.file_id "
                "WHERE p.server_id=? AND p.item_id=? ORDER BY f.canonical_path",
                (server_id, item_id),
            ).fetchall()
        return [r["canonical_path"] for r in rows]
```

(`ORDER BY item_id` is text order: "10" before "7". The test lists "7", "8" only because "10" is failed and "9" empty;
keep the test as written.)

`base.py`:

```python
ReadBackItem = tuple[str, list[Marker], frozenset[MarkerType], tuple[str, ...] | None]
```

and on `MarkerPublisher`:

```python
    def shows_many(
        self, items: list[ReadBackItem], *, cancel_check: Callable[[], bool] | None = None
    ) -> dict[str, Shown | None]:
        """``shows`` for many items (reconcile). Servers with a cheaper bulk read override it.

        Args:
            items: ``(item_id, ours, kept_types, item_files)`` per item.
            cancel_check: True once the job is cancelled; items not read by then are left out.

        Returns:
            What ``shows`` answers, per item id read.
        """
        out: dict[str, Shown | None] = {}
        for item_id, ours, kept_types, item_files in items:
            if cancel_check and cancel_check():
                break
            out[item_id] = self.shows(item_id, ours, kept_types=kept_types, item_files=item_files)
        return out
```

(import `Callable` from `collections.abc`).

`plex_db.py`: `READ_BACK_CHUNK = 500` next to `BUSY_TIMEOUT_S`. Move the body of `shows` that runs after the
connection opens into

```python
    def _shown_in(
        self,
        conn: sqlite3.Connection,
        tag_id: int,
        rating_key: int,
        ours: list[Marker],
        kept_types: frozenset[MarkerType],
        item_files: tuple[str, ...] | None,
    ) -> Shown:
        rows = conn.execute(
            "SELECT text, time_offset, end_time_offset, extra_data FROM taggings WHERE metadata_item_id=? AND tag_id=?",
            (rating_key, tag_id),
        ).fetchall()
        if item_files is not None and _version_files(self._item_parts(conn, rating_key)) != tuple(item_files):
            return Shown.VERSIONS_CHANGED
        served = {
            mtype: [_served_times(mtype, start, end, _row_is_final(extra)) for text, start, end, extra in rows if text == name]
            for mtype, name in _TYPE_TEXT.items()
        }
        if any(not served.get(mtype) for mtype in kept_types):
            return Shown.MISSING
        return compare_shown(self.project(ours), served, others_alongside=False)
```

then

```python
    def shows_many(
        self, items: list[ReadBackItem], *, cancel_check: Callable[[], bool] | None = None
    ) -> dict[str, Shown | None]:
        """Read many items' marker rows with one lock proof and one read-only connection per chunk.

        A chunk that can't be read (Plex's database busy past the deadline, not local, schema changed) answers None for
        its items; an id that isn't a Plex rating key answers None.
        """
        out: dict[str, Shown | None] = {}
        for first in range(0, len(items), READ_BACK_CHUNK):
            if cancel_check and cancel_check():
                break
            chunk = items[first : first + READ_BACK_CHUNK]
            try:
                deadline = time.monotonic() + BUSY_TIMEOUT_S
                local = self._local_checks(deadline=deadline)
                if not local.ready:
                    raise PublishError(local.message, state=local.state)
                with self._database(read_only=True, deadline=deadline) as conn:
                    self._check_schema(conn)
                    tag_id = self._marker_tag_id(conn)
                    for item_id, ours, kept_types, item_files in chunk:
                        try:
                            rating_key = _rating_key(item_id)
                        except ItemNotFoundError:
                            out[item_id] = None
                            continue
                        out[item_id] = self._shown_in(conn, tag_id, rating_key, ours, kept_types, item_files)
            except (PublishError, sqlite3.Error) as exc:
                logger.debug("Plex {}: couldn't read {} item(s) back: {}", self._config.name, len(chunk), type(exc).__name__)
                out.update({item_id: None for item_id, *_ in chunk if item_id not in out})
        return out

    def shows(
        self,
        item_id: str,
        ours: list[Marker],
        *,
        kept_types: frozenset[MarkerType] = frozenset(),
        item_files: tuple[str, ...] | None = None,
    ) -> Shown | None:
        """(docstring unchanged from Task 9)"""
        return self.shows_many([(item_id, list(ours), frozenset(kept_types), item_files)]).get(item_id)
```

- [ ] **Step 4: Implement `reconcile.py`, the job and the triggers flag**

```python
# media_preview_generator/markers/reconcile.py
"""Reconcile (spec §6.2 step 6): a periodic check that servers still show what this app published.

A normal job already reads a file's markers back before calling it up to date, and replaced files get a delayed verify
job. Reconcile covers every other file: a LOW Intro & Credits job reads back each published server item in bulk and
runs the pipeline only for the files of items that drifted (Plex's own forced detection, a Jellyfin rescan, an Emby
refresh the plugin couldn't heal, a Plex version added since), which re-publishes them under the normal rules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..job_kinds import JOB_KIND_INTRO_CREDITS
from ..web.jobs import PRIORITY_LOW, get_job_manager
from .pipeline import markers_for_path
from .publishers.base import Shown
from .publishers.factory import publisher_for
from .settings import load_server
from .store import MarkerStore

RECONCILE_JOB_ID = "__markers_reconcile"
RECONCILE_INTERVAL_HOURS = 12
RECONCILE_SOURCE = "reconcile"
RECONCILE_JOB_NAME = "Intro & Credits · Check servers"


@dataclass(frozen=True)
class Drift:
    """A published server item that no longer shows what this app left there."""

    server_id: str
    item_id: str
    shown: Shown
    files: tuple[str, ...]


def find_drift(
    *, registry: Any, store: MarkerStore, cancel_check: Callable[[], bool] | None = None
) -> tuple[list[Drift], list[str]]:
    """Read back every published item on every server with Intro & Credits on.

    Args:
        registry: The job's ``ServerRegistry``.
        store: The markers store.
        cancel_check: True once the job is cancelled.

    Returns:
        The drifted items with their local files, and job warnings (servers skipped, items that couldn't be read).
    """
    drifts: list[Drift] = []
    warnings: list[str] = []
    for cfg in registry.configs():
        if cancel_check and cancel_check():
            break
        settings = load_server(cfg.markers, cfg.type.value)
        if not cfg.enabled or not settings.enabled:
            continue
        rows = store.published_items(cfg.id)
        if not rows:
            continue
        server = registry.get(cfg.id)
        publisher = (
            publisher_for(server, cfg, sibling_markers=lambda path: markers_for_path(store, path))
            if server is not None
            else None
        )
        if publisher is None:
            warnings.append(f"Couldn't check {cfg.name}: no connection to it")
            continue
        try:
            report = publisher.capability()
        except Exception as exc:
            logger.warning("Reconcile couldn't check {}: {}", cfg.name, type(exc).__name__)
            warnings.append(f"Couldn't check {cfg.name}: {type(exc).__name__}")
            continue
        if not report.ready:
            warnings.append(f"Skipped {cfg.name}: {report.message}")
            continue
        answers = publisher.shows_many(
            [(r.item_id, list(r.markers), r.kept_types, r.item_files) for r in rows], cancel_check=cancel_check
        )
        # A type kept as Plex's own goes back to ours once the server is set to use ours (the pipeline writes it).
        release_kept = settings.on_plex_redetect != "keep_plex"
        unreadable = 0
        for row in rows:
            if row.item_id not in answers:
                continue  # cancelled before this item was read
            shown = answers[row.item_id]
            if shown is None:
                unreadable += 1
                continue
            if shown is Shown.OURS and row.kept_types and release_kept:
                shown = Shown.REPLACED
            if shown is Shown.OURS:
                continue
            files = tuple(store.files_for_item(cfg.id, row.item_id))
            if files:
                drifts.append(Drift(cfg.id, row.item_id, shown, files))
        if unreadable:
            warnings.append(f"Couldn't read what {unreadable} item(s) show on {cfg.name}")
    return drifts, warnings


def run_markers_reconcile() -> str | None:
    """Queue the reconcile job (the scheduler's 12-hourly call and ``POST /api/markers/reconcile``).

    Returns:
        The new or the already queued/running reconcile job's id; None when Intro & Credits is off everywhere.
    """
    from .triggers import create_intro_credits_job, markers_enabled_anywhere

    if not markers_enabled_anywhere():
        return None
    jm = get_job_manager()
    for job in [*jm.get_pending_jobs(), *jm.get_running_jobs()]:
        if job.kind == JOB_KIND_INTRO_CREDITS and (job.config or {}).get("reconcile"):
            return job.id
    job = create_intro_credits_job(
        library_name=RECONCILE_JOB_NAME, priority=PRIORITY_LOW, source=RECONCILE_SOURCE, reconcile=True
    )
    return job.id
```

(The pipeline import is safe: `reconcile` is imported lazily by `job_runner.build_items`, the scheduler and the route.)

`triggers.create_intro_credits_job`: add `reconcile: bool = False` (docstring: "reconcile: List the files of drifted
published items instead of libraries or paths (``reconcile.find_drift``).") and `if reconcile: config["reconcile"] =
True`.

`job_runner.build_items`, first thing in the function body:

```python
    if job_config.get("reconcile"):
        from .reconcile import find_drift
        from .store import get_marker_store

        drifts, warnings = find_drift(registry=registry, store=get_marker_store(), cancel_check=cancel_check)
        paths = sorted({path for drift in drifts for path in drift.files})
        if len(paths) > MAX_RETRY_FILES:
            warnings.append(f"{len(paths) - MAX_RETRY_FILES} more changed file(s) are checked on the next run")
            paths = paths[:MAX_RETRY_FILES]
        # No item id hints: a drifted item may be gone or merged, so each file's item is looked up again.
        return (
            [ProcessableItem(canonical_path=p, server_id="", item_id_by_server={}, title=os.path.basename(p)) for p in paths],
            warnings,
            {},
        )
```

In `run_intro_credits_job`: the `if not items:` branch becomes

```python
                if not items:
                    if cfg.get("reconcile"):
                        jm.add_log(job_id, "INFO - Every server checked still shows what this app published")
                        jm.complete_job(job_id, warning=" | ".join(warnings) or None)
                    else:
                        jm.complete_job(job_id, warning=" ".join(["No files to check.", *warnings]))
                    return
```

and `if waiting:` → `if waiting and not cfg.get("reconcile"):` (a drifted file whose item is gone waits for the next
reconcile, not a retry chain every 12 hours).

- [ ] **Step 5: Scheduler, app start, route**

`web/scheduler.py` `ScheduleManager`:

```python
    def apply_markers_reconcile(self) -> None:
        """Register the 12-hourly Intro & Credits reconcile (idempotent; the job it queues checks whether it's needed)."""
        from ..markers.reconcile import RECONCILE_INTERVAL_HOURS, RECONCILE_JOB_ID, run_markers_reconcile

        if not self.scheduler.running:
            self.start()
        self.scheduler.add_job(
            run_markers_reconcile,
            trigger=IntervalTrigger(hours=RECONCILE_INTERVAL_HOURS),
            id=RECONCILE_JOB_ID,
            replace_existing=True,
        )
```

`web/app.py`, right after `schedule_manager.start()`:

```python
    try:
        schedule_manager.apply_markers_reconcile()
    except Exception:
        from loguru import logger as _rc_logger

        _rc_logger.exception("Could not register the Intro & Credits reconcile")
```

`web/routes/api_markers.py`:

```python
@api.route("/markers/reconcile", methods=["POST"])
@api_token_required
def marker_reconcile():
    """Check now that servers still show the published markers (the job the scheduler queues every 12 hours).

    Returns:
        202 with ``{"job_id"}`` (a new LOW job, or the one already queued or running); 200 with ``{"job_id": null,
        "reason"}`` when Intro & Credits is off everywhere; 503 when the config directory isn't writable.
    """
    from ...markers.reconcile import run_markers_reconcile

    blocked = _config_unwritable_response()
    if blocked is not None:
        return blocked
    job_id = run_markers_reconcile()
    if job_id is None:
        return jsonify({"job_id": None, "reason": "Intro & Credits is off on every server"}), 200
    return jsonify({"job_id": job_id}), 202
```

If `POST /api/markers/jobs` is in the CSRF-exempt list (spec §14 2026-09-14 "`POST /api/markers/jobs` is CSRF-exempt"),
add this route next to it.

- [ ] **Step 6: Run the tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers tests/test_scheduler*.py -q`
Expected: PASS.

- [ ] **Step 7: Mutation self-check (high-risk)**

One at a time, confirm a test fails, revert: `published_items` including failed rows; `find_drift` dropping the
`release_kept` rule; `shows_many` chunk failure answering OURS instead of None; `build_items` keeping hints; the
reconcile job queueing retries; `run_markers_reconcile` not reusing a running job.

- [ ] **Step 8: Commit**

`feat(markers): 12-hourly reconcile job re-publishes markers servers dropped`.

---
## Task 12: Markers API — season payload, season publish, local source status, evidence labels

`[sequential]` (after Tasks 7, 10) — spec §7.4 ("Inspector → Season view: per-episode intro/credits, evidence chips,
per-server dots, 'Needs review', bulk publish"), §7.2 (sources with measured numbers; season audio unavailable → say
why), roadmap phase 2 ("Inspector Season view + `GET /api/markers/season`"), ledger parked item "evidence rows lack the
chapter title label (store EvidenceRow)" (progress.md line 200). Design artifact section "Inspector → Season view".

**OWNER RULING R4 (recommended default implemented here):** jobs already publish every decided marker, so the
mockup's "Publish 8 to 3 servers" button queues a normal (not forced) HIGH Intro & Credits job for the season folder:
decided episodes are sent to every server that doesn't show them yet, undecided ones are checked again. A second click
while that job is queued or running returns the same job.

Server dots come from `markers.db` only (no live server reads), so the Season view of a 26-episode season is one
cheap request; the per-episode Inspector tab stays the place for live "what the server shows now".

**Files:**
- Modify: `media_preview_generator/markers/store.py` (`EvidenceRow.label`), `media_preview_generator/markers/inspect.py`
  (`_decision_dict` extracted from `item_payload`; evidence `label`; new `season_payload`),
  `media_preview_generator/markers/triggers.py` (`submit_season_publish`), `media_preview_generator/markers/job_runner.py`
  (`_USER_PICKED_SOURCES` gains `"inspector_season"`), `media_preview_generator/web/routes/api_markers.py` (three routes)
- Test: `tests/markers/test_inspect.py` (season payload class; evidence label), `tests/markers/test_api_markers.py`
  (routes), `tests/markers/test_triggers.py` (season publish)

**Interfaces:**
- Consumes: Task 7 `season_group`, `Source.SEASON_AUDIO_PREVIOUS`; Task 1 `chromaprint_status`; phase 1 `_owners`, `allowed_matches`, `load_server`, `ids_from_path`, `_library_file`,
  `_without_secrets`, `_config_unwritable_response`.
- Produces (Tasks 13, 14):
```python
EvidenceRow.label: str = ""                                       # chapter title, "10/10" for season audio, else ""
def season_payload(canonical_path: str, *, registry: Any, store: MarkerStore) -> dict
def submit_season_publish(folder: str) -> str
# GET  /api/markers/season?path=<episode>  → 200 season_payload | 400 {"error"} | 500 {"error"}
# POST /api/markers/season/publish {"path": <episode>} → 202 {"job_id"} | 400 | 503
# GET  /api/markers/sources/local → 200 {"season_audio": {"available": bool, "ffmpeg": str | None, "message": str}}
```
`season_payload` shape (Task 14 renders exactly this):
```json
{
  "folder": "/media/tv/Rick and Morty (2013) {tvdb-275274}/Season 01",
  "show": "Rick and Morty (2013) {tvdb-275274}",
  "season": "Season 01",
  "servers": [{"server_id": "plex-1", "server_name": "Plex", "server_type": "plex", "markers_enabled": true}],
  "episodes": [{
    "path": ".../Rick and Morty (2013) - S01E01 - Pilot.mkv", "name": "Rick and Morty (2013) - S01E01 - Pilot.mkv",
    "episode": "E01", "known": true, "duration_ms": 1321472,
    "intro":   {"status": "decided", "reason": "...", "marker": {"type": "intro", "start_ms": 127000, "end_ms": 157000, "decided_by": ["season_audio", "theintrodb"], "locked": false}, "proposed": null},
    "credits": {"status": "needs_review", "reason": "...", "marker": null, "proposed": {"start_ms": 1296000, "end_ms": 1321472}},
    "evidence": [{"source": "season_audio", "label": "10/10"}, {"source": "theintrodb", "label": ""}],
    "servers": {"plex-1": {"state": "ok", "message": "2 marker(s)"}}
  }],
  "counts": {"episodes": 11, "ready": 8, "needs_review": 3}
}
```
Server `state`: `off` (server disabled, Intro & Credits off, or this episode's library not selected), `ok` (last
publish written with markers), `none` (written with nothing of ours, or never published), `waiting`, `failed`,
`skipped`. Episode `ready` = known, no enabled type in `needs_review`, at least one decided marker. `servers` lists the
enabled servers owning the asked episode's file, in registry order.

- [ ] **Step 1: Write the failing tests**

`tests/markers/test_inspect.py` — add `import os` and `from types import SimpleNamespace` to its imports, then change
the existing `test_known_file_decisions_and_evidence` expectation: the chapters row gains `"label": "Opening"` (the
title `_known_file` stores) and the theintrodb row gains `"label": ""`. Append:

```python
class TestSeasonPayload:
    @pytest.fixture
    def season(self, tmp_path, store):
        folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        paths = []
        for e in (1, 2, 3):
            p = folder / f"Show (2020) - S01E{e:02d}.mkv"
            p.write_bytes(b"x")
            paths.append(str(p))
        (folder / "Show (2020) - S01E01-sample.mkv").write_bytes(b"x")
        root = str(tmp_path / "media")
        reg = _registry(
            server_config("plex-1", ServerType.PLEX, root=root),
            server_config("jf-1", ServerType.JELLYFIN, root=root, markers={"enabled": False, "library_ids": None}),
        )
        return SimpleNamespace(folder=str(folder), paths=paths, reg=reg, store=store)

    @staticmethod
    def _decide(store, path, intro=None, *, credits_review=False, evidence=()):
        st = os.stat(path)
        rec = store.upsert_file(
            FileIdentity(path, st.st_size, st.st_mtime_ns),
            duration_ms=DURATION,
            season_key=os.path.dirname(path),
            is_movie=False,
        )
        for source in (Source.SEASON_AUDIO, Source.SKIPDB):
            store.replace_evidence(rec.id, source, [c for c in evidence if c.source is source])
        server = [c for c in evidence if c.source is Source.SERVER_MARKERS]
        store.replace_evidence(rec.id, Source.SERVER_MARKERS, server, origin="plex-1")
        credits = (
            TypeDecision(T.CREDITS, DecisionStatus.NEEDS_REVIEW, None, Marker(T.CREDITS, 1_296_000, DURATION, ("skipdb",)), "disagree")
            if credits_review
            else _none(T.CREDITS)
        )  # fmt: skip
        store.save_decisions(
            rec.id,
            {T.INTRO: _decided(intro) if intro else _none(T.INTRO), T.CREDITS: credits},
            settings_fingerprint="f",
        )
        return rec

    def test_lists_the_folders_episodes_with_decisions_chips_dots_and_counts(self, season):
        intro = Marker(T.INTRO, 127_000, 157_000, ("season_audio", "skipdb"))
        audio = Candidate(T.INTRO, 127_000, 157_000, Source.SEASON_AUDIO, 1.0, "2/2")
        skip = Candidate(T.INTRO, 128_000, 157_500, Source.SKIPDB, 0.9)
        on_plex = Candidate(T.INTRO, 76_000, 112_000, Source.SERVER_MARKERS, 1.0)
        e1 = self._decide(season.store, season.paths[0], intro, evidence=(audio, skip, on_plex))
        season.store.set_publish_state(e1.id, "plex-1", item_id="7", markers=[intro], status="written", message="1 marker(s)")
        e2 = self._decide(season.store, season.paths[1], intro, credits_review=True, evidence=(audio,))
        season.store.set_publish_state(
            e2.id, "plex-1", item_id="8", markers=None, status="waiting", message="Not in this server's library yet"
        )

        payload = inspect.season_payload(season.paths[1], registry=season.reg, store=season.store)

        assert (payload["folder"], payload["show"], payload["season"]) == (season.folder, "Show (2020) {tvdb-1}", "Season 01")
        assert payload["servers"] == [
            {"server_id": "plex-1", "server_name": "PLEX-1", "server_type": "plex", "markers_enabled": True},
            {"server_id": "jf-1", "server_name": "JF-1", "server_type": "jellyfin", "markers_enabled": False},
        ]
        eps = payload["episodes"]
        assert [e["episode"] for e in eps] == ["E01", "E02", "E03"]  # the -sample extra isn't listed
        assert eps[0]["intro"]["marker"] == {
            "type": "intro", "start_ms": 127_000, "end_ms": 157_000, "decided_by": ["season_audio", "skipdb"], "locked": False,
        }  # fmt: skip
        # Markers already on a server are the dots, not chips; only season audio carries its "2/2" label.
        assert eps[0]["evidence"] == [{"source": "season_audio", "label": "2/2"}, {"source": "skipdb", "label": ""}]
        assert eps[0]["servers"] == {"plex-1": {"state": "ok", "message": "1 marker(s)"}, "jf-1": {"state": "off", "message": ""}}
        assert eps[1]["credits"]["status"] == "needs_review"
        assert eps[1]["credits"]["proposed"] == {"start_ms": 1_296_000, "end_ms": DURATION}
        assert eps[1]["servers"]["plex-1"] == {"state": "waiting", "message": "Not in this server's library yet"}
        assert (eps[2]["known"], eps[2]["duration_ms"], eps[2]["intro"]["status"]) == (False, None, None)
        assert eps[2]["servers"]["plex-1"] == {"state": "none", "message": ""}
        assert payload["counts"] == {"episodes": 3, "ready": 1, "needs_review": 1}

    @pytest.mark.parametrize(
        ("status", "markers", "state"),
        [("written", [], "none"), ("failed", None, "failed"), ("skipped", None, "skipped")],
    )
    def test_dot_states(self, season, status, markers, state):
        rec = self._decide(season.store, season.paths[0])
        season.store.set_publish_state(rec.id, "plex-1", item_id="7", markers=markers, status=status, message="m")
        payload = inspect.season_payload(season.paths[0], registry=season.reg, store=season.store)
        assert payload["episodes"][0]["servers"]["plex-1"] == {"state": state, "message": "m"}

    def test_a_disabled_server_is_off_for_every_episode(self, season):
        season.reg.configs_by_id["plex-1"] = server_config("plex-1", ServerType.PLEX, root=season.folder, enabled=False)
        payload = inspect.season_payload(season.paths[0], registry=season.reg, store=season.store)
        assert {e["servers"]["plex-1"]["state"] for e in payload["episodes"]} == {"off"}
        assert payload["servers"][0]["markers_enabled"] is False
```

`tests/markers/test_api_markers.py` (its `client`, `servers`, `media` and `created` fixtures already exist; `media`
holds `tv/Show/S01E01.mkv` inside plex-1's TV library and `movies/Film/Film.mkv` inside jf-1's Movies library):

```python
# --------------------------------------------------------------------------- season view


@pytest.fixture
def season_calls(monkeypatch):
    from media_preview_generator.markers import inspect

    calls = []
    monkeypatch.setattr(inspect, "season_payload", lambda path, **kw: calls.append((path, kw)) or {"episodes": []})
    return calls


def test_season_payload_for_an_episode(client, servers, media, season_calls):
    episode = str(media / "tv" / "Show" / "S01E01.mkv")
    resp = client.get("/api/markers/season", query_string={"path": episode}, headers=_api_headers())
    assert (resp.status_code, resp.get_json()) == (200, {"episodes": []})
    ((path, kwargs),) = season_calls
    assert path == episode and set(kwargs) == {"registry", "store"}


@pytest.mark.parametrize(
    ("relative", "error"),
    [("../secret.mkv", "Path is not a file inside any server library"), ("movies/Film/Film.mkv", "Not a TV episode")],
)
def test_season_refuses_other_paths(client, servers, media, season_calls, relative, error):
    resp = client.get("/api/markers/season", query_string={"path": str(media / relative)}, headers=_api_headers())
    assert (resp.status_code, resp.get_json()) == (400, {"error": error})
    assert season_calls == []


def test_season_payload_crash_is_a_json_error_without_details(client, servers, media, monkeypatch):
    from media_preview_generator.markers import inspect

    monkeypatch.setattr(inspect, "season_payload", MagicMock(side_effect=RuntimeError(f"db at {PLEX_TOKEN}")))
    resp = _secret_free(
        client.get("/api/markers/season", query_string={"path": str(media / "tv" / "Show" / "S01E01.mkv")}, headers=_api_headers())
    )  # fmt: skip
    assert (resp.status_code, resp.get_json()) == (500, {"error": "Couldn't build the Season view for this file"})


def test_season_publish_queues_a_normal_high_job_for_the_folder(client, servers, media, created):
    body = {"path": str(media / "tv" / "Show" / "S01E01.mkv")}
    resp = client.post("/api/markers/season/publish", json=body, headers=_api_headers())
    assert (resp.status_code, resp.get_json()) == (202, {"job_id": "job-123"})
    assert created == [
        {
            "library_name": "Intro & Credits: tv · Show",
            "priority": 1,
            "source": "inspector_season",
            "file_paths": [str(media / "tv" / "Show")],
        }
    ]


def test_season_publish_refuses_a_movie(client, servers, media, created):
    resp = client.post("/api/markers/season/publish", json={"path": str(media / "movies" / "Film" / "Film.mkv")}, headers=_api_headers())
    assert (resp.status_code, resp.get_json(), created) == (400, {"error": "Not a TV episode"}, [])


@pytest.mark.parametrize(("found", "reason"), [("/usr/lib/jellyfin-ffmpeg/ffmpeg", ""), (None, "no chromaprint")])
def test_local_sources_status(client, servers, monkeypatch, found, reason):
    from media_preview_generator.markers.audio import fingerprint

    monkeypatch.setattr(fingerprint, "chromaprint_status", lambda configured: (found, reason))
    resp = client.get("/api/markers/sources/local", headers=_api_headers())
    assert resp.get_json() == {"season_audio": {"available": found is not None, "ffmpeg": found, "message": reason}}
```

Add `("get", "/api/markers/season?path=/x")`, `("post", "/api/markers/season/publish")` and
`("get", "/api/markers/sources/local")` to the parametrize list of `test_every_route_needs_authentication`.

`tests/markers/test_triggers.py`:

```python
class TestSeasonPublish:
    """Season view "Publish": one normal HIGH job per season folder while one is queued or running."""

    FOLDER = "/media/tv/Show/Season 01"

    @pytest.fixture
    def jm(self, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        jm = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        with patch.object(triggers, "start_intro_credits_job_async"):
            yield jm

    def test_creates_a_normal_high_priority_job_for_the_folder(self, jm):
        job_id = triggers.submit_season_publish(self.FOLDER)
        (job,) = [j for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS]
        assert (job.id, job.priority, job.library_name) == (job_id, 1, "Intro & Credits: Show · Season 01")
        assert job.config["file_paths"] == [self.FOLDER]
        assert job.config["source"] == "inspector_season"
        assert not job.config.get("force")

    def test_clicked_again_while_queued_returns_the_same_job(self, jm):
        first = triggers.submit_season_publish(self.FOLDER)
        assert triggers.submit_season_publish(self.FOLDER) == first
        assert triggers.submit_season_publish("/media/tv/Show/Season 02") != first

    def test_a_finished_job_is_not_reused(self, jm):
        first = triggers.submit_season_publish(self.FOLDER)
        jm.start_job(first)
        jm.complete_job(first)
        assert triggers.submit_season_publish(self.FOLDER) != first
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers/test_inspect.py -k Season tests/markers/test_api_markers.py -k Season tests/markers/test_triggers.py -k SeasonPublish -q`
Expected: FAIL — `AttributeError: module 'media_preview_generator.markers.inspect' has no attribute 'season_payload'`.

- [ ] **Step 3: Implement**

`store.py`: `EvidenceRow` gains `label: str = ""` as its last field (docstring: "``label`` is the candidate's own
title, e.g. a chapter name or season audio's "10/10""); `evidence_rows` passes `label=r["label"] or ""` (empty
lookups store NULL).

`inspect.py`: extract from `item_payload`

```python
def _decision_dict(decision: Any, marker: Marker | None) -> dict:
    """One type's stored decision in the Inspector's shape."""
    return {
        "status": decision.status.value if decision else None,
        "reason": decision.reason if decision else "",
        "marker": {**_marker_dict(marker), "decided_by": list(marker.decided_by), "locked": marker.locked}
        if marker
        else None,
        "proposed": (
            {"start_ms": decision.proposed_start_ms, "end_ms": decision.proposed_end_ms}
            if decision and decision.proposed_start_ms is not None
            else None
        ),
    }
```

(`item_payload`'s loop becomes `payload["decisions"][mtype.value] = _decision_dict(decisions.get(mtype),
markers.get(mtype))`; its evidence dicts gain `"label": r.label`.) Then:

```python
_SEASON_TYPES = (MarkerType.INTRO, MarkerType.CREDITS)
# Evidence chips name what decided an episode; markers already on servers are the dots, not chips.
_CHIP_SOURCES_EXCLUDED = frozenset({Source.SERVER_MARKERS, Source.SERVER_MARKERS_IMPORTED})
_DOT_STATES = {"written": "ok", "waiting": "waiting", "failed": "failed", "skipped": "skipped"}


def _episode_label(path: str) -> str | None:
    ids = ids_from_path(path)
    return f"E{ids.episode:02d}" if ids.is_episode and ids.episode is not None else None


def _dot(cfg: ServerConfig, matches: list[OwnershipMatch], rec: FileRecord | None, store: MarkerStore) -> dict:
    if not cfg.enabled or not allowed_matches(cfg, matches):
        return {"state": "off", "message": ""}
    row = store.get_publish_state(rec.id, cfg.id) if rec else None
    if row is None:
        return {"state": "none", "message": ""}
    state = _DOT_STATES.get(row.status, "none")
    if state == "ok" and not row.markers:
        state = "none"
    return {"state": state, "message": row.message}


def season_payload(canonical_path: str, *, registry: Any, store: MarkerStore) -> dict:
    """The Season view: every episode of a file's season folder, from markers.db only (no live server reads).

    Args:
        canonical_path: An episode's local path (already validated by the caller).
        registry: The ``ServerRegistry``.
        store: The markers store.

    Returns:
        ``folder``, ``show``, ``season``, ``servers`` (the asked file's owners with ``markers_enabled``), ``episodes``
        (sorted; each with ``path``, ``name``, ``episode`` "E01", ``known``, ``duration_ms``, ``intro`` and
        ``credits`` in ``item_payload``'s decision shape, ``evidence`` chips ``{source, label}`` and ``servers`` dots
        ``{server_id: {state, message}}`` where state is off/ok/none/waiting/failed/skipped) and ``counts``
        (``episodes``, ``ready``, ``needs_review``).
    """
    from .audio.season import season_group

    group = season_group(canonical_path)
    owners = list(_owners(canonical_path, registry))
    servers = [
        {
            "server_id": cfg.id,
            "server_name": cfg.name,
            "server_type": cfg.type.value,
            "markers_enabled": bool(cfg.enabled and allowed_matches(cfg, matches)),
        }
        for cfg, _server, matches in owners
    ]
    episodes, ready, review = [], 0, 0
    for path in group.episodes:
        rec = store.get_file(path)
        decisions = store.get_decisions(rec.id) if rec else {}
        markers = store.get_markers(rec.id) if rec else {}
        chips: dict[str, str] = {}
        for row in store.evidence_rows(rec.id) if rec else []:
            if row.type in _SEASON_TYPES and row.source not in _CHIP_SOURCES_EXCLUDED:
                chips.setdefault(row.source.value, row.label if row.source.value.startswith("season_audio") else "")
        matches_by_server = {cfg.id: matches for cfg, _server, matches in _owners(path, registry)}
        types = {mtype.value: _decision_dict(decisions.get(mtype), markers.get(mtype)) for mtype in _SEASON_TYPES}
        in_review = any(t["status"] == DecisionStatus.NEEDS_REVIEW.value for t in types.values())
        decided = any(t["marker"] for t in types.values())
        review += int(in_review)
        ready += int(rec is not None and decided and not in_review)
        episodes.append(
            {
                "path": path,
                "name": os.path.basename(path),
                "episode": _episode_label(path),
                "known": rec is not None,
                "duration_ms": rec.duration_ms if rec else None,
                **types,
                "evidence": [{"source": s, "label": label} for s, label in chips.items()],
                "servers": {
                    cfg.id: _dot(cfg, matches_by_server.get(cfg.id, []), rec, store) for cfg, _server, _m in owners
                },
            }
        )
    return {
        "folder": group.folder,
        "show": os.path.basename(os.path.dirname(group.folder)),
        "season": os.path.basename(group.folder),
        "servers": servers,
        "episodes": episodes,
        "counts": {"episodes": len(episodes), "ready": ready, "needs_review": review},
    }
```

(imports: `ids_from_path` from `.external_ids`, `Source` from `.models`, `DecisionStatus` from `.decide`, `Any`. The
evidence rows come back in insertion order, so chips follow the order the sources were stored.)

`triggers.py`:

```python
_SEASON_PUBLISH_SOURCE = "inspector_season"


def submit_season_publish(folder: str) -> str:
    """Queue the Season view's "Publish" for a season folder: a normal (not forced) HIGH job over its files.

    Decided episodes go to every server that doesn't show them yet; undecided ones are checked again. While this
    folder's job is still queued or running, that job is returned instead.

    Args:
        folder: The season folder's local path, already validated by the caller.

    Returns:
        The id of the new or the reused job.
    """
    jm = get_job_manager()
    with _redetect_lock:
        for job in [*jm.get_pending_jobs(), *jm.get_running_jobs()]:
            cfg = job.config or {}
            if (
                job.kind == JOB_KIND_INTRO_CREDITS
                and cfg.get("source") == _SEASON_PUBLISH_SOURCE
                and list(cfg.get("file_paths") or []) == [folder]
            ):
                return job.id
        job = create_intro_credits_job(
            library_name=f"Intro & Credits: {os.path.basename(os.path.dirname(folder))} · {os.path.basename(folder)}",
            priority=PRIORITY_HIGH,
            source=_SEASON_PUBLISH_SOURCE,
            file_paths=[folder],
        )
    return job.id
```

`job_runner.py`: `_USER_PICKED_SOURCES = frozenset({"manual", "inspector", "inspector_season"})`.

`api_markers.py`:

```python
@api.route("/markers/season", methods=["GET"])
@api_token_required
def marker_season():
    """Season view data for the season folder of one episode.

    Query: ``path`` (an episode file inside a server library).

    Returns:
        200 with ``markers.inspect.season_payload``; 400 when the path isn't a library file or isn't a TV episode; 500
        with a JSON error when the data can't be built.
    """
    from ...markers import inspect
    from ...markers.external_ids import ids_from_path
    from ...markers.store import get_marker_store

    registry = _registry()
    safe = _library_file(request.args.get("path"), registry)
    if safe is None:
        return jsonify({"error": "Path is not a file inside any server library"}), 400
    if not ids_from_path(safe).is_episode:
        return jsonify({"error": "Not a TV episode"}), 400
    try:
        payload = inspect.season_payload(safe, registry=registry, store=get_marker_store())
    except Exception as exc:
        logger.warning("Season view data failed: {}", type(exc).__name__)
        return jsonify({"error": "Couldn't build the Season view for this file"}), 500
    return jsonify(_without_secrets(payload, registry))


@api.route("/markers/season/publish", methods=["POST"])
@api_token_required
def marker_season_publish():
    """Queue the Season view's "Publish": a normal HIGH job over the episode's season folder.

    Body: ``{"path"}`` (an episode of the season).

    Returns:
        202 with ``{"job_id"}``; 400 when the path isn't a library episode; 503 when the config directory isn't writable.
    """
    from ...markers.external_ids import ids_from_path
    from ...markers.triggers import submit_season_publish

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "The request body must be a JSON object with a path"}), 400
    safe = _library_file(data.get("path"), _registry())
    if safe is None:
        return jsonify({"error": "Path is not a file inside any server library"}), 400
    if not ids_from_path(safe).is_episode:
        return jsonify({"error": "Not a TV episode"}), 400
    blocked = _config_unwritable_response()
    if blocked is not None:
        return blocked
    return jsonify({"job_id": submit_season_publish(os.path.dirname(safe))}), 202


@api.route("/markers/sources/local", methods=["GET"])
@api_token_required
def marker_local_sources():
    """Whether the local detectors can run in this container (season audio needs ffmpeg's chromaprint muxer).

    Returns:
        200 with ``{"season_audio": {"available", "ffmpeg", "message"}}``.
    """
    from ...markers.audio.fingerprint import chromaprint_status

    found, reason = chromaprint_status(None)
    return jsonify({"season_audio": {"available": found is not None, "ffmpeg": found, "message": reason}})
```

(`chromaprint_status(None)` looks at jellyfin-ffmpeg then PATH, the same order `config._resolve_ffmpeg_path` uses.)
If `POST /api/markers/item/redetect` is CSRF-exempt, add `/markers/season/publish` beside it.

- [ ] **Step 4: Run the tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov tests/markers -q`
Expected: PASS.

- [ ] **Step 5: Real-app smoke (storage lab)**

Rebuild and recreate `mlab-app` on this branch (phase 1 Task 19 Step 1 build command, then `./app.sh`), after Task 7's
lab run has left Rick and Morty S01 decided. Then:

```bash
cd /home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab
/home/data/.venv/bin/python - <<'PY'
import subprocess
import urllib.parse

import phase1_matrix as m

names = subprocess.run(["docker", "exec", "mlab-app", "ls", m.RICK_SEASON], capture_output=True, text=True, check=True)
first = sorted(n for n in names.stdout.splitlines() if n.endswith(".mkv"))[0]
season = m.app_ok("GET", "/api/markers/season?path=" + urllib.parse.quote(f"{m.RICK_SEASON}/{first}"))
print(season["counts"], [e["episode"] for e in season["episodes"]])
print({s["server_id"]: s["markers_enabled"] for s in season["servers"]})
print(m.app_ok("GET", "/api/markers/sources/local"))
PY
```

Expected: 11 episodes E01–E11; counts match the last Rick and Morty job's Files panel; every lab server listed;
`season_audio.available` true with `/usr/lib/jellyfin-ffmpeg/ffmpeg`. Paste the three printed lines into
`phase2-results.md` → "Task 12".

- [ ] **Step 6: Commit**

`feat(markers): Season view API, season publish and local source status`.

Backend milestone: after this commit run the whole-branch audit (execution model) on Tasks 1–12 and fix its HIGH and
MED findings before Tasks 13 and 14 start; Task 15 may keep running.

---
## Task 13: UI — season audio source live, Emby status and install, Inspector audio evidence

`[sequential]` (after Tasks 10, 12; may run beside Task 14, different files) — spec §7.2 ("Matching audio across a
season … 77% right, 12% missed, 11% wrong … CPU ≈2 s/episode"; unavailable → say why), §7.1 (Emby: plugin installed
vs catalog, Install/Update, "Can show"), §7.3 (Inspector evidence lanes), roadmap phase 2 (Settings season audio
"Coming soon" removed). Design artifact sections "Settings → Intro & Credits", "Servers → Edit → Intro & Credits",
"Inspector → Intro & Credits" (Season audio lane bar "0:00–0:34 · 11/12").

**Files:**
- Modify: `media_preview_generator/web/templates/settings.html` (season audio row ~601–615; JS after `loadMarkersUsage`
  ~1275; the load call ~965)
- Modify: `media_preview_generator/web/static/js/markers_server_tab.js` (`renderEmbyStatus` ~177, `installPlugin` ~224)
- Modify: `media_preview_generator/web/static/js/markers_inspector.js` (`SOURCES` ~26, `segmentLane` ~174,
  `renderWindow` ~248, `serverLines` ~303)
- Test: `tests/e2e/test_intro_credits_settings.py`, `tests/e2e/test_intro_credits_server_tab.py`,
  `tests/e2e/test_intro_credits_inspector.py`

**Interfaces:**
- Consumes: Task 12 `GET /api/markers/sources/local` → `{"season_audio": {"available", "ffmpeg", "message"}}`;
  evidence rows' `label`; Task 10 Emby capability states (`needs_plugin` with `details.catalog_listed`,
  `plugin_outdated` with `details.plugin_version`, `ready` with `details.plugin_version`), `POST
  /api/servers/<id>/install-plugin` answering `{"ok", "error", "manual", "steps"}`, and `CREDITS_BEFORE_END_NOTE`
  ("Emby can't show credits that end before the file does"); Task 7 `season_audio_previous` evidence source.
- Produces: nothing later tasks call. The guide anchor `docs/guides.md#emby-the-media-preview-bridge-for-emby-plugin`
  is written by Task 16 (heading "### Emby: the Media Preview Bridge for Emby plugin").

**Copy (from the design artifact and spec §5.3 numbers; any change → owner screenshot first):**
- Season audio tooltip: "Finds the theme tune a season's episodes share. On 118 test episodes: 91 right, 13 wrong, 14
  missed. On its own it publishes only at "Medium"; at "High" another source has to agree."
- Unavailable badge "Not available", reason line = the API's `message`.
- Emby rows: "How markers get here: Media Preview Bridge for Emby plugin"; "Plugin: Not installed [Install]" when the
  catalog lists it, "Plugin: Not installed · Install by hand" (link to the guide) when it doesn't; "Update needed
  [Update]"; "1.0.0.0 ✓"; "Can show: Intro · credits start (only credits that run to the end)".
- Inspector: the season audio bar reads "0:02–0:29 · 10/10"; the previous-season lane is named "Previous season audio";
  an Emby card whose decided credits end before the file does says "Emby can't show credits that end before the file
  does" instead of "Emby has no “credits end”".

- [ ] **Step 1: Write the failing e2e tests**

`tests/e2e/test_intro_credits_settings.py` — change the coming-soon parametrize to `["credits_text"]` only and keep
`test_coming_soon_sources_round_trip_their_stored_values` unchanged (season audio's stored `enabled: False` still
round-trips). Add `_mock_local_sources` and call it from `_open_settings` (new keyword `local=None`):

```python
def _mock_local_sources(page: Page, body: object, status: int = 200) -> None:
    def handler(route: Route) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/markers/sources/local", handler)


AVAILABLE = {"season_audio": {"available": True, "ffmpeg": "/usr/lib/jellyfin-ffmpeg/ffmpeg", "message": ""}}
```

In `_open_settings(page, app_url, markers, usage=None, usage_status=200, local=None)` add
`_mock_local_sources(page, local if local is not None else AVAILABLE)` before `page.goto`. Then in the class:

```python
    def test_season_audio_is_switchable_and_explains_its_numbers(self, authed_page: Page, app_url: str) -> None:
        captured = _open_settings(authed_page, app_url, _default_markers())
        row = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        switch = row.locator(".markers-source-enabled")
        expect(switch).to_be_enabled()
        expect(row.locator(".markers-source-soon-badge")).to_have_count(0)
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()
        # Bootstrap moves ``title`` into ``data-bs-original-title`` once the tooltip is initialised.
        tooltip = row.locator(".info-icon").evaluate("el => el.getAttribute('data-bs-original-title') || el.getAttribute('title')")
        assert tooltip == (
            "Finds the theme tune a season's episodes share. On 118 test episodes: 91 right, 13 wrong, 14 missed. "
            'On its own it publishes only at "Medium"; at "High" another source has to agree.'
        )

        switch.click()
        sent = _wait_for_post(authed_page, captured, lambda m: next(s for s in m["sources"] if s["id"] == "season_audio")["enabled"] is False)
        assert [s["id"] for s in sent["sources"]] == SOURCE_ORDER

    def test_season_audio_without_chromaprint_says_why(self, authed_page: Page, app_url: str) -> None:
        reason = "Needs an ffmpeg with the chromaprint muxer (the Docker image's jellyfin-ffmpeg has it)"
        _open_settings(authed_page, app_url, _default_markers(),
                       local={"season_audio": {"available": False, "ffmpeg": None, "message": reason}})  # fmt: skip
        row = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        expect(row.locator(".markers-source-unavailable")).to_have_text("Not available", timeout=5000)
        expect(row.locator(".markers-source-reason")).to_have_text(reason)
        # The stored choice stays visible and is still saved as it was; the job just can't run it here.
        expect(row.locator(".markers-source-enabled")).to_be_checked()

    def test_local_source_check_failing_leaves_the_row_as_is(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, _default_markers(), local={"error": "boom"})
        row = authed_page.locator("#markersSourceList .markers-source[data-id='season_audio']")
        expect(row.locator(".markers-source-enabled")).to_be_enabled()
        expect(row.locator(".markers-source-unavailable")).to_be_hidden()
```

`tests/e2e/test_intro_credits_server_tab.py` — replace `TestEmbyTab.test_emby_not_available_yet_and_save_sends_switch`
and extend `_mock_server_page` so the install answer can be set: `captured["install_answer"] = {"ok": True, "steps":
[]}` and `install_handler` fulfils `captured["install_answer"]`.

```python
@pytest.mark.e2e
class TestEmbyTab:
    def test_needs_plugin_listed_in_the_catalog_installs_and_rechecks(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        captured = _mock_server_page(
            authed_page, server,
            _status(server, "needs_plugin", "Install the Media Preview Bridge for Emby plugin", {"catalog_listed": True}),
        )  # fmt: skip
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("Media Preview Bridge for Emby plugin", timeout=5000)
        expect(block).to_contain_text("Not installed")
        expect(block).to_contain_text("Intro · credits start (only credits that run to the end)")
        authed_page.locator("#markersInstallPluginBtn").click()
        expect(block).to_contain_text("Emby is restarting", timeout=5000)
        assert captured["installs"] == [{"method": "POST", "url": f"{app_url}/api/servers/emby-1/install-plugin"}]

    def test_needs_plugin_not_in_the_catalog_links_the_manual_install_guide(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        _mock_server_page(
            authed_page, server,
            _status(server, "needs_plugin", "Install the Media Preview Bridge for Emby plugin", {"catalog_listed": False}),
        )  # fmt: skip
        _open_tab(authed_page, app_url, server)
        link = authed_page.locator("#markersStatusBlock a.markers-manual-install")
        expect(link).to_have_text("Install by hand", timeout=5000)
        expect(link).to_have_attribute("href", re.compile(r"docs/guides\.md#emby-the-media-preview-bridge-for-emby-plugin$"))
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_count(0)

    def test_install_that_needs_a_manual_install_shows_the_answer(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        captured = _mock_server_page(
            authed_page, server,
            _status(server, "needs_plugin", "Install the Media Preview Bridge for Emby plugin", {"catalog_listed": True}),
        )  # fmt: skip
        error = "Media Preview Bridge for Emby isn't in the Emby plugin catalog yet; install it by hand (see the Intro & Credits guide)"
        captured["install_answer"] = {"ok": False, "manual": True, "error": error, "steps": []}
        _open_tab(authed_page, app_url, server)
        authed_page.locator("#markersInstallPluginBtn").click()
        expect(authed_page.locator("#markersInstallResult")).to_have_text(error, timeout=5000)

    def test_outdated_offers_update(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        status = _status(
            server,
            "plugin_outdated",
            "Update Media Preview Bridge for Emby (installed 0.9.0.0) to get markers support",
            {"plugin_version": "0.9.0.0"},
        )
        _mock_server_page(authed_page, server, status)
        _open_tab(authed_page, app_url, server)
        expect(authed_page.locator("#markersStatusBlock")).to_contain_text("Update needed", timeout=5000)
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_text("Update")

    def test_ready_shows_the_version_without_a_warning(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        _mock_server_page(authed_page, server, _status(server, "ready", "Media Preview Bridge for Emby plugin", {"plugin_version": "1.0.0.0"}))
        _open_tab(authed_page, app_url, server)
        block = authed_page.locator("#markersStatusBlock")
        expect(block).to_contain_text("1.0.0.0 ✓", timeout=5000)
        expect(block.locator(".alert-warning")).to_have_count(0)
        expect(authed_page.locator("#markersInstallPluginBtn")).to_have_count(0)

    def test_emby_save_sends_switch_and_libraries(self, authed_page: Page, app_url: str) -> None:
        server = _vendor_server("emby", "emby-1")
        captured = _mock_server_page(authed_page, server, _status(server, "ready", "Media Preview Bridge for Emby plugin", {"plugin_version": "1.0.0.0"}))
        _open_tab(authed_page, app_url, server)
        expect(_lib_toggle(authed_page, "3")).not_to_be_checked(timeout=5000)  # Sports is unticked by default
        authed_page.locator("#markersLibraryList label[for='markersLib-3']").click()
        _flip_switch_on(authed_page)
        expect(authed_page.locator("#markersPlexConfirmModal")).to_be_hidden()
        body = _save_and_get_put(authed_page, captured)
        assert body["markers"] == {"enabled": True, "library_ids": ["1", "2", "3"]}
```

Keep `test_emby_unknown_state_shows_the_message` but change its last line to
`expect(block).not_to_contain_text("Not installed")`. Add `import re` to the file's imports.

`tests/e2e/test_intro_credits_inspector.py`:

```python
def _labelled(source: str, mtype: str, start: int, end: int, label: str) -> dict:
    return {**_evidence(source, mtype, start, end), "label": label}


def season_audio() -> dict:
    payload = south_park()
    payload["decisions"]["intro"] = _decision("decided", ("intro", 2_000, 29_000), decided_by=["season_audio", "theintrodb"])
    payload["evidence"] = [
        _labelled("season_audio", "intro", 2_000, 29_000, "10/10"),
        _labelled("season_audio_previous", "intro", 2_500, 29_500, "4/4"),
        _evidence("theintrodb", "intro", 1_000, 29_000),
    ]
    payload["decisions"]["credits"] = _decision("decided", ("credits", 1_250_000, 1_280_000))
    return payload
```

```python
    def test_season_audio_lanes_carry_their_match_count(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, season_audio())
        inspector.open_result()
        page = inspector.open_tab()
        expect(_lane(page, "opening", "Season audio").locator(".mk-bar")).to_have_text("0:02–0:29 · 10/10")
        expect(_lane(page, "opening", "Previous season audio").locator(".mk-bar")).to_have_text("0:02–0:29 · 4/4")
        expect(_lane(page, "opening", "TheIntroDB").locator(".mk-bar")).to_have_text("0:01–0:29")

    def test_emby_card_explains_credits_that_end_before_the_file(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, season_audio())
        inspector.open_result()
        page = inspector.open_tab()
        card = _server_card(page, "emby-1")
        expect(card).to_contain_text("Emby can't show credits that end before the file does")
        expect(card).not_to_contain_text("Emby has no “credits end”")
        expect(card).not_to_contain_text("Credits 20:50")
        expect(_server_card(page, "jf-1")).not_to_contain_text("Emby can't show")

    def test_emby_card_for_credits_to_the_end_keeps_the_no_end_note(self, authed_page: Page, app_url: str) -> None:
        inspector = _Inspector(authed_page, app_url, south_park())
        inspector.open_result()
        page = inspector.open_tab()
        expect(_server_card(page, "emby-1")).to_contain_text("Emby has no “credits end”")
```

(`laneRange` prints `clock(start)–clock(end)` with an en dash, seconds floored, so 2 500 ms reads "0:02".)

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest -m e2e -n 0 --no-cov tests/e2e/test_intro_credits_settings.py tests/e2e/test_intro_credits_server_tab.py tests/e2e/test_intro_credits_inspector.py -k "season_audio or local_source or Emby or emby" -q`
Expected: FAIL — season audio switch disabled; Emby tab shows "Not available yet"; season audio bar text lacks
" · 10/10".

- [ ] **Step 3: Settings row and local source check**

`settings.html`, season audio `<li>`: drop the `markers-source-soon` class and the "Coming soon" badge; new tooltip
(copy above); remove `disabled` from its switch; after the tooltip button add

```html
<span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle ms-1 markers-source-unavailable" hidden>Not available</span>
```

and under "CPU · about 2 s per episode" add `<div class="small text-warning-emphasis markers-source-reason" hidden></div>`.

JS after `loadMarkersUsage`:

```javascript
async function loadMarkersLocalSources() {
    const row = document.querySelector('#markersSourceList .markers-source[data-id="season_audio"]');
    if (!row) return;
    try {
        const response = await fetch('/api/markers/sources/local');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        const audio = data && data.season_audio;
        if (!audio || typeof audio.available !== 'boolean') return;
        // The switch keeps the stored choice: a job in another container (or after an image update) may have chromaprint.
        row.querySelector('.markers-source-unavailable').hidden = audio.available;
        const reason = row.querySelector('.markers-source-reason');
        reason.textContent = audio.available ? '' : String(audio.message || '');
        reason.hidden = audio.available;
    } catch (_error) {
        // Unknown: leave the row as it is.
    }
}
```

Call `loadMarkersLocalSources();` next to `loadMarkersUsage();` (~965).

- [ ] **Step 4: Emby status and install**

`markers_server_tab.js`:

```javascript
    const EMBY_MANUAL_GUIDE_URL = 'https://github.com/stevezau/media_preview_generator/blob/main/docs/guides.md#emby-the-media-preview-bridge-for-emby-plugin';
    const RESTARTING = { jellyfin: 'Jellyfin', emby: 'Emby' };

    function renderEmbyStatus(status) {
        const capability = status.capability || {};
        const details = capability.details || {};
        const rows = [kvRow('How markers get here', 'Media Preview Bridge for Emby plugin')];
        const pluginStates = ['ready', 'plugin_outdated', 'needs_plugin'];
        if (capability.state === 'ready') {
            rows.push(kvRow('Plugin', badge('ok', details.plugin_version ? `${details.plugin_version} ✓` : 'Installed ✓')));
        } else if (capability.state === 'plugin_outdated') {
            rows.push(kvRow('Plugin', badge('warn', 'Update needed') + installButton('Update')));
        } else if (capability.state === 'needs_plugin') {
            const action = details.catalog_listed === true
                ? installButton('Install')
                : ` · <a class="markers-manual-install" href="${EMBY_MANUAL_GUIDE_URL}" target="_blank" rel="noopener">Install by hand</a>`;
            rows.push(kvRow('Plugin', badge('bad', 'Not installed') + action));
        }
        rows.push(kvRow('Can show', 'Intro · credits start (only credits that run to the end)'
            + infoIcon('Emby has no "credits end": a Skip Credits button always skips to the end of the file, so credits followed by a scene are left out.')));
        return kvGrid(rows) + (pluginStates.includes(capability.state) ? '' : warningLine(capability));
    }
```

In `installPlugin`, the restarting line becomes
`` `${RESTARTING[vendorOf(server)] || 'The server'} is restarting — checking again in 20 s…` `` (the Jellyfin test's
"Jellyfin is restarting" still passes).

- [ ] **Step 5: Inspector audio lanes and the Emby credits note**

`markers_inspector.js`:

```javascript
    const SOURCES = [
        ['chapters', 'Chapters'],
        ['theintrodb', 'TheIntroDB'],
        ['introdb', 'IntroDB'],
        ['skipdb', 'SkipDB'],
        ['season_audio', 'Season audio'],
        ['season_audio_previous', 'Previous season audio'],
        ['credits_text', 'Credit text'],
        ['user', 'Your marker'],
    ];
    // Sources whose stored label is a match count ("10/10") worth showing on the bar.
    const COUNTED_SOURCES = ['season_audio', 'season_audio_previous'];
    const EMBY_CREDITS_BEFORE_END = 'Emby can\'t show credits that end before the file does';
```

`segmentLane(name, segments, win, payload, emptyText)`: the bar label becomes
`laneRange(seg, duration) + (COUNTED_SOURCES.indexOf(seg.source) !== -1 && seg.label ? ' · ' + seg.label : '')` (the
"now" lanes pass server segments without `source`, so they're unchanged).

`serverLines`: before `wanted` is used, split Emby's credits:

```javascript
        const emby = server.server_type === 'emby';
        const creditsBeforeEnd = emby && wanted.some(function (m) { return m.type === 'credits' && !toEnd(m, duration); });
        if (creditsBeforeEnd) wanted = wanted.filter(function (m) { return m.type !== 'credits'; });
```

(`const wanted` → `let wanted`), and the Emby note becomes

```javascript
        if (creditsBeforeEnd) lines.push(EMBY_CREDITS_BEFORE_END);
        else if (emby && wanted.some(function (m) { return m.type === 'credits'; })) lines.push('Emby has no “credits end”');
```

(`toEnd(seg, duration)` already exists at ~85 and applies `END_OF_FILE_MS`, the same 2 s allowance as
`credits_run_to_end`.)

- [ ] **Step 6: Run the e2e files and the unit suite**

Run: `/home/data/.venv/bin/python -m pytest -m e2e -n 8 --no-cov tests/e2e/test_intro_credits_settings.py tests/e2e/test_intro_credits_server_tab.py tests/e2e/test_intro_credits_inspector.py -q`
Expected: PASS (all, including the untouched Plex/Jellyfin cases).

- [ ] **Step 7: Owner screenshot checkpoint**

Take three screenshots with the same Playwright fixtures (Settings season audio row available + unavailable; Emby Edit
tab needs-plugin catalog + manual; Inspector with the season audio payload) into
`docs/design/intro-credits/evidence/screenshots/phase2/` and show them to the owner beside the design artifact. Do not
commit until the owner accepts the wording.

- [ ] **Step 8: Commit**

`feat(markers): season audio source live in Settings, Emby plugin status and install, audio match counts in the Inspector`.

---
## Task 14: UI — Inspector → Season view

`[sequential]` (after Task 12; may run beside Task 13 — Task 14 owns `bif_viewer.html`'s Intro & Credits pane and
Task 13 doesn't touch it; both edit `markers_inspector.js` in different functions, so rebase the later one) — spec
§7.4 ("Season view: per-episode intro/credits, evidence chips, per-server dots, 'Needs review', bulk publish"),
roadmap phase 2 ("Inspector Season view"), design artifact section "Inspector → Season view" (title "Rick and Morty ·
Season 1", badges "8 ready" / "3 need review", button "Publish 8 to 3 servers", columns Ep · Intro · Credits · Evidence
· Plex · Jellyfin · Emby, chips "Audio 10/10" / "TheIntroDB" / "🔒 Locked by you", "Review" / "Published", legend
"dots: green = server shows this marker, amber = waiting, grey = server not enabled").

**OWNER RULING R4 (Task 12):** "Publish N to M servers" queues a normal HIGH job for the season folder. N = episodes
ready, M = servers with Intro & Credits on for this season. Differences from the mockup this task makes, to show the
owner in Step 7: no "2 releases (NTb, Absinth)" subline (the payload has no release groups); every episode is listed
(no "Showing 7 of 11"); a red dot for "failed"; "Review" opens that episode in "This episode" instead of an editor
(editing is phase 4).

**Files:**
- Create: `media_preview_generator/web/static/js/markers_season.js`
- Modify: `media_preview_generator/web/templates/bif_viewer.html` (the `#inspector-tab-markers` pane ~160–170; result
  `dataset` ~324–331; `setMarkersItem` call in `loadPreviewFromResult` ~366; script tags ~186)
- Modify: `media_preview_generator/web/static/js/markers_inspector.js` (`loadMarkersInspector` ~444, `render` ~390)
- Modify: `media_preview_generator/web/static/css/pages/markers_inspector.css` (append)
- Test: `tests/e2e/test_intro_credits_season.py`

**Interfaces:**
- Consumes: Task 12 `GET /api/markers/season?path=` (shape in Task 12), `POST /api/markers/season/publish {"path"}` →
  202 `{"job_id"}`; `window.loadMarkersInspector(item)` (phase 1); app.js `apiPost(url, body)`, `showToast(title,
  message, level)`, `_initBootstrapTooltips(root)`.
- Produces: `window.markersSeason = { setItem(item), setPath(path) }` — `markers_inspector.js` calls `setItem` at the
  start of every `loadMarkersInspector(item)` and `setPath(payload.canonical_path)` in `render`. The Inspector item
  gains `type` (the search result's `type`, e.g. `"episode"`).

- [ ] **Step 1: Write the failing e2e tests**

```python
# tests/e2e/test_intro_credits_season.py
"""E2E: Preview Inspector → Intro & Credits → "Whole season".

Search, BIF and ``GET /api/markers/item`` are mocked as in ``test_intro_credits_inspector.py``; ``GET
/api/markers/season`` returns the ``markers.inspect.season_payload`` shape and ``POST /api/markers/season/publish`` is
captured, so each test pins how one season state renders and what the page sends.
"""

from __future__ import annotations

import copy
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json
from .test_intro_credits_inspector import _DURATION, _MEDIA_FILE, _Inspector, _result, south_park

_FOLDER = "/data/tv/South Park (1997)/Season 01"
_SERVERS = [
    {"server_id": "plex-1", "server_name": "Plex", "server_type": "plex", "markers_enabled": True},
    {"server_id": "jf-1", "server_name": "Jellyfin", "server_type": "jellyfin", "markers_enabled": True},
    {"server_id": "emby-1", "server_name": "Emby", "server_type": "emby", "markers_enabled": False},
]


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _type(status, start=None, end=None, *, locked=False, proposed=None):
    marker = None
    if status == "decided":
        marker = {"type": "x", "start_ms": start, "end_ms": end, "decided_by": ["season_audio"], "locked": locked}
    return {"status": status, "reason": "", "marker": marker, "proposed": proposed}


def _dots(plex, jf, message=""):
    return {
        "plex-1": {"state": plex, "message": message},
        "jf-1": {"state": jf, "message": message},
        "emby-1": {"state": "off", "message": ""},
    }


def _episode(n, intro, credits, evidence, dots, *, known=True):
    return {
        "path": f"{_FOLDER}/South Park S01E{n:02d}.mkv",
        "name": f"South Park S01E{n:02d}.mkv",
        "episode": f"E{n:02d}",
        "known": known,
        "duration_ms": _DURATION if known else None,
        "intro": intro,
        "credits": credits,
        "evidence": evidence,
        "servers": dots,
    }


def season() -> dict:
    audio = {"source": "season_audio", "label": "10/10"}
    tidb = {"source": "theintrodb", "label": ""}
    return {
        "folder": _FOLDER,
        "show": "South Park (1997) {tvdb-75897}",
        "season": "Season 01",
        "servers": copy.deepcopy(_SERVERS),
        "episodes": [
            _episode(1, _type("decided", 127_000, 157_000), _type("decided", 1_295_000, _DURATION), [audio, tidb], _dots("ok", "ok", "2 marker(s)")),
            _episode(2, _type("decided", 1_000, 30_000, locked=True), _type("decided", 1_246_000, _DURATION), [audio, {"source": "user", "label": ""}], _dots("ok", "waiting", "Not in this server's library yet")),
            _episode(3, _type("decided", 2_000, 29_000), _type("needs_review", proposed={"start_ms": 1_230_000, "end_ms": _DURATION}), [audio], _dots("ok", "failed", "HTTP 500 from the plugin")),
            _episode(4, _type(None), _type(None), [], _dots("none", "none"), known=False),
        ],
        "counts": {"episodes": 4, "ready": 2, "needs_review": 1},
    }  # fmt: skip


class _Season(_Inspector):
    def __init__(self, page: Page, app_url: str, payload: dict, *, results=None, season_status: int = 200) -> None:
        super().__init__(page, app_url, south_park(), results=results)
        self.season_payload = payload
        self.season_status = season_status
        self.season_requests: list[str] = []
        self.publish_bodies: list[dict] = []
        page.route("**/api/markers/season?**", self._season)
        page.route("**/api/markers/season/publish", self._publish)

    def _season(self, route: Route) -> None:
        self.season_requests.append(route.request.url)
        _fulfill_json(route, self.season_payload, status=self.season_status)

    def _publish(self, route: Route) -> None:
        self.publish_bodies.append(route.request.post_data_json or {})
        _fulfill_json(route, {"job_id": "5a5a5a5a-1111-4222-8333-444455556666"}, status=202)

    def whole_season(self) -> Page:
        self.page.locator("label[for='markersViewSeason']").click()
        expect(self.page.locator("#markersSeasonBody")).not_to_contain_text("Loading", timeout=3000)
        return self.page


def _row(page: Page, episode: str):
    return page.locator(f'#markersSeasonBody tr[data-episode="{episode}"]')


@pytest.mark.e2e
class TestSeasonView:
    def test_toggle_shows_for_an_episode_and_loads_the_season_only_when_asked(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        page = view.open_tab()
        expect(page.locator("#markersViewToggle")).to_be_visible()
        expect(page.locator("#markersViewEpisode")).to_be_checked()
        assert view.season_requests == []

        view.whole_season()
        (url,) = view.season_requests
        assert parse_qs(urlparse(url).query)["path"] == [_MEDIA_FILE]
        expect(page.locator("#markersEpisodeView")).to_be_hidden()

    def test_toggle_is_hidden_for_a_movie(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season(), results=[{**_result(), "type": "movie"}])
        view.open_result()
        page = view.open_tab()
        expect(page.locator("#markersViewToggle")).to_be_hidden()

    def test_header_counts_rows_chips_and_dots(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        body = page.locator("#markersSeasonBody")
        expect(body.locator(".mk-season-title")).to_have_text("South Park (1997) · Season 01")
        expect(body.locator(".mk-season-sub")).to_have_text("4 episodes")
        expect(body.locator(".mk-season-ready")).to_have_text("2 ready")
        expect(body.locator(".mk-season-review")).to_have_text("1 need review")
        expect(body.locator("#markersSeasonPublishBtn")).to_have_text("Publish 2 to 2 servers")
        expect(body.locator("thead th.mk-season-servers")).to_have_text("Plex · Jellyfin · Emby")

        expect(_row(page, "E01").locator("td").nth(1)).to_have_text("2:07 – 2:37")
        expect(_row(page, "E01").locator("td").nth(2)).to_have_text("21:35 →")
        expect(_row(page, "E01").locator(".mk-chip")).to_have_text(["Audio 10/10", "TheIntroDB"])
        expect(_row(page, "E01").locator(".mk-season-action")).to_have_text("Published")
        expect(_row(page, "E02").locator(".mk-chip")).to_have_text(["Audio 10/10", "Your marker", "🔒 Locked by you"])
        expect(_row(page, "E02").locator(".mk-season-action")).to_have_text("")
        expect(_row(page, "E03").locator("td").nth(2)).to_have_text("Needs review")
        expect(_row(page, "E03").locator(".mk-season-action button")).to_have_text("Review")
        expect(_row(page, "E04").locator("td").nth(1)).to_have_text("Not checked yet")

        dots = _row(page, "E02").locator(".mk-dot")
        expect(dots).to_have_count(3)
        expect(dots.nth(0)).to_have_class(re.compile(r"\bmk-dot-ok\b"))
        expect(dots.nth(1)).to_have_class(re.compile(r"\bmk-dot-waiting\b"))
        expect(dots.nth(1)).to_have_attribute("title", "Jellyfin: Not in this server's library yet")
        expect(dots.nth(2)).to_have_class(re.compile(r"\bmk-dot-off\b"))
        expect(_row(page, "E03").locator(".mk-dot").nth(1)).to_have_class(re.compile(r"\bmk-dot-failed\b"))
        expect(body.locator(".mk-season-legend")).to_have_text(
            "Dots: green = server shows this marker, amber = waiting, red = failed, grey = server not enabled or nothing sent yet"
        )

    def test_publish_sends_the_episode_path_and_links_the_job(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        page.locator("#markersSeasonPublishBtn").click()
        expect(page.locator("#toastBody")).to_contain_text("5a5a5a5a", timeout=3000)
        assert view.publish_bodies == [{"path": _MEDIA_FILE}]

    def test_publish_is_disabled_with_nothing_ready_or_no_server_on(self, authed_page: Page, app_url: str) -> None:
        payload = season()
        payload["counts"]["ready"] = 0
        view = _Season(authed_page, app_url, payload)
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        expect(page.locator("#markersSeasonPublishBtn")).to_be_disabled()
        expect(page.locator("#markersSeasonPublishBtn")).to_have_text("Publish 0 to 2 servers")

    def test_review_opens_that_episode(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        before = len(view.item_requests)
        _row(page, "E03").locator(".mk-season-action button").click()
        expect(page.locator("#markersViewEpisode")).to_be_checked()
        expect(page.locator("#markersEpisodeView")).to_be_visible()
        for _ in range(30):
            if len(view.item_requests) > before:
                break
            page.wait_for_timeout(100)
        assert parse_qs(urlparse(view.item_requests[before]).query)["path"] == [f"{_FOLDER}/South Park S01E03.mkv"]

    def test_a_season_error_is_shown(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, {"error": "Not a TV episode"}, season_status=400)
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        expect(page.locator("#markersSeasonBody .alert-warning")).to_have_text("Couldn't load this season: Not a TV episode")

    def test_a_second_toggle_uses_the_loaded_season(self, authed_page: Page, app_url: str) -> None:
        view = _Season(authed_page, app_url, season())
        view.open_result()
        view.open_tab()
        page = view.whole_season()
        page.locator("label[for='markersViewEpisode']").click()
        view.whole_season()
        assert len(view.season_requests) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest -m e2e -n 0 --no-cov tests/e2e/test_intro_credits_season.py -q`
Expected: FAIL — `#markersViewToggle` not found.

- [ ] **Step 3: Pane markup and the item type**

`bif_viewer.html`, replace the pane's inner `col-lg-10` content with:

```html
<div class="d-flex align-items-center gap-2 mb-2" id="markersViewToggle" hidden>
    <div class="btn-group btn-group-sm" role="group" aria-label="Show this episode or the whole season">
        <input type="radio" class="btn-check" name="markersView" id="markersViewEpisode" value="episode" autocomplete="off" checked>
        <label class="btn btn-outline-secondary" for="markersViewEpisode">This episode</label>
        <input type="radio" class="btn-check" name="markersView" id="markersViewSeason" value="season" autocomplete="off">
        <label class="btn btn-outline-secondary" for="markersViewSeason">Whole season</label>
    </div>
    <button type="button" class="info-icon" tabindex="0" data-bs-toggle="tooltip" data-bs-placement="top" title="Intros are found by comparing a season's episodes, so the whole season is the natural place to check and publish them."><i class="bi bi-info-circle"></i></button>
</div>
<div id="markersEpisodeView">
    <div class="d-flex justify-content-between align-items-center mb-2 flex-wrap gap-2">
        <div class="small text-muted text-break" id="markersInspectorPath"></div>
        <button class="btn btn-sm btn-outline-secondary" id="markersRedetectBtn" type="button" disabled title="Look this file up and detect it again, asking every source. Runs as a job on the Dashboard."><i class="bi bi-arrow-repeat me-1"></i>Re-detect</button>
    </div>
    <div id="markersInspectorBody"></div>
</div>
<div id="markersSeasonBody" hidden></div>
```

In the results loop add `a.dataset.resultType = r.type || '';` and in `loadPreviewFromResult` pass
`type: meta.resultType || ''` in the `setMarkersItem({...})` object. Add
`<script src="{{ url_for('static', filename='js/markers_season.js') }}"></script>` after the `markers_inspector.js`
tag.

`markers_inspector.js`: first line of `loadMarkersInspector(item)` body:
`if (window.markersSeason) window.markersSeason.setItem(item);`; in `render(payload, item)` after the path is set:
`if (window.markersSeason) window.markersSeason.setPath(payload.canonical_path || item.media_file || '');`.

- [ ] **Step 4: `markers_season.js`**

```javascript
// =========================================================================
// Preview Inspector → Intro & Credits → "Whole season".
//
// Renders GET /api/markers/season (markers.inspect.season_payload): every episode of the season folder with its
// decisions, evidence chips and one dot per server, and "Publish" (POST /api/markers/season/publish, a normal HIGH
// job for the folder). Loaded only when "Whole season" is picked, then kept per path. markers_inspector.js calls
// setItem(item) for every item and setPath(canonical path) once the item's data arrives. Text goes through
// textContent. Depends on app.js globals: apiPost, showToast, _initBootstrapTooltips.
// =========================================================================
(function () {
    'use strict';

    // A segment ending this close to the end of the file runs "to the end" (same allowance as the backend).
    const END_OF_FILE_MS = 2000;
    const CHIP_NAMES = {
        chapters: 'Chapters',
        theintrodb: 'TheIntroDB',
        introdb: 'IntroDB',
        skipdb: 'SkipDB',
        season_audio: 'Audio',
        season_audio_previous: 'Previous season',
        credits_text: 'Credit text',
        user: 'Your marker',
    };
    const DOT_WORDS = {
        ok: 'shows our markers',
        waiting: 'waiting',
        failed: 'failed',
        skipped: 'skipped',
        none: 'nothing sent yet',
        off: 'Intro & Credits is off',
    };
    const LEGEND = 'Dots: green = server shows this marker, amber = waiting, red = failed, grey = server not enabled or nothing sent yet';

    const cache = new Map();
    let item = null;
    let path = '';
    let seq = 0;

    const $ = function (id) { return document.getElementById(id); };

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function clock(ms) {
        const total = Math.max(0, Math.floor(ms / 1000));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = String(total % 60).padStart(2, '0');
        return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
    }

    function range(marker, duration) {
        const end = marker.end_ms === null || marker.end_ms === undefined ? duration : marker.end_ms;
        if (duration && end >= duration - END_OF_FILE_MS) return `${clock(marker.start_ms)} →`;
        return `${clock(marker.start_ms)} – ${clock(end)}`;
    }

    function typeCell(decision, duration) {
        const td = el('td', 'mk-season-time');
        const d = decision || {};
        if (d.status === 'decided' && d.marker) td.textContent = range(d.marker, duration);
        else if (d.status === 'needs_review') td.appendChild(el('span', 'badge text-bg-warning', 'Needs review'));
        else if (d.status === 'disabled') td.appendChild(el('span', 'text-muted', 'Off'));
        else if (d.status === 'no_evidence') td.appendChild(el('span', 'text-muted', '—'));
        else td.appendChild(el('span', 'text-muted', 'Not checked yet'));
        return td;
    }

    function chipsCell(episode) {
        const td = el('td');
        (episode.evidence || []).forEach(function (chip) {
            const name = CHIP_NAMES[chip.source] || chip.source;
            td.appendChild(el('span', 'badge mk-chip', chip.label ? `${name} ${chip.label}` : name));
        });
        const locked = ['intro', 'credits'].some(function (t) { return episode[t] && episode[t].marker && episode[t].marker.locked; });
        if (locked) td.appendChild(el('span', 'badge mk-chip mk-chip-locked', '🔒 Locked by you'));
        return td;
    }

    function dotsCell(episode, servers) {
        const td = el('td');
        const dots = el('div', 'mk-dots');
        servers.forEach(function (server) {
            const state = (episode.servers || {})[server.server_id] || { state: 'none', message: '' };
            const dot = el('span', 'mk-dot mk-dot-' + state.state);
            dot.title = `${server.server_name}: ${state.message || DOT_WORDS[state.state] || state.state}`;
            dots.appendChild(dot);
        });
        td.appendChild(dots);
        return td;
    }

    function actionCell(episode, servers) {
        const td = el('td', 'mk-season-action');
        const review = ['intro', 'credits'].some(function (t) { return episode[t] && episode[t].status === 'needs_review'; });
        if (review) {
            const button = el('button', 'btn btn-sm btn-outline-secondary py-0', 'Review');
            button.type = 'button';
            button.addEventListener('click', function () { openEpisode(episode.path); });
            td.appendChild(button);
            return td;
        }
        const on = servers.filter(function (s) { return s.markers_enabled; });
        const published = on.length && on.every(function (s) { return ((episode.servers || {})[s.server_id] || {}).state === 'ok'; });
        if (published) td.appendChild(el('span', 'text-muted', 'Published'));
        return td;
    }

    function render(payload) {
        const body = $('markersSeasonBody');
        const servers = payload.servers || [];
        const counts = payload.counts || {};
        const on = servers.filter(function (s) { return s.markers_enabled; }).length;

        const head = el('div', 'd-flex justify-content-between align-items-center flex-wrap gap-2 mb-2');
        const titles = el('div');
        const show = String(payload.show || '').replace(/\s*\{[a-z]+-[^}]*\}/gi, '').trim();
        titles.appendChild(el('div', 'mk-season-title', `${show} · ${payload.season || ''}`));
        titles.appendChild(el('div', 'mk-season-sub text-muted small', `${counts.episodes || 0} episodes`));
        const actions = el('div', 'd-flex align-items-center gap-2');
        actions.appendChild(el('span', 'badge text-bg-success mk-season-ready', `${counts.ready || 0} ready`));
        if (counts.needs_review) actions.appendChild(el('span', 'badge text-bg-warning mk-season-review', `${counts.needs_review} need review`));
        const publish = el('button', 'btn btn-sm btn-primary', `Publish ${counts.ready || 0} to ${on} server${on === 1 ? '' : 's'}`);
        publish.type = 'button';
        publish.id = 'markersSeasonPublishBtn';
        publish.disabled = !counts.ready || !on;
        publish.title = 'Runs Intro & Credits for this season as a job: decided episodes go to every server that doesn\'t show them yet, the rest are checked again.';
        publish.addEventListener('click', function () { publishSeason(publish); });
        actions.appendChild(publish);
        head.append(titles, actions);

        const card = el('div', 'card');
        const scroll = el('div', 'table-responsive');
        const table = el('table', 'table table-sm align-middle mb-0 mk-season-table');
        const headRow = el('tr');
        ['Ep', 'Intro', 'Credits', 'Evidence'].forEach(function (label) { headRow.appendChild(el('th', '', label)); });
        headRow.appendChild(el('th', 'mk-season-servers', servers.map(function (s) { return s.server_name; }).join(' · ')));
        headRow.appendChild(el('th'));
        const thead = el('thead');
        thead.appendChild(headRow);
        const tbody = el('tbody');
        (payload.episodes || []).forEach(function (episode) {
            const row = el('tr');
            row.dataset.episode = episode.episode || episode.name;
            row.appendChild(el('td', 'mk-season-time', episode.episode || episode.name));
            row.append(
                typeCell(episode.intro, episode.duration_ms),
                typeCell(episode.credits, episode.duration_ms),
                chipsCell(episode),
                dotsCell(episode, servers),
                actionCell(episode, servers),
            );
            tbody.appendChild(row);
        });
        table.append(thead, tbody);
        scroll.appendChild(table);
        card.appendChild(scroll);
        body.replaceChildren(head, card, el('div', 'mk-season-legend text-muted small mt-1', LEGEND));
        if (typeof window._initBootstrapTooltips === 'function') window._initBootstrapTooltips(body);
    }

    async function load() {
        const body = $('markersSeasonBody');
        const asked = path || (item && item.media_file) || '';
        if (!asked) return;
        const mine = ++seq;
        if (cache.has(asked)) {
            render(cache.get(asked));
            return;
        }
        body.replaceChildren(el('div', 'text-muted small py-3', 'Loading the season…'));
        try {
            const resp = await fetch('/api/markers/season?path=' + encodeURIComponent(asked));
            if (resp.status === 401) {
                window.location.href = '/login';
                return;
            }
            const data = await resp.json().catch(function () { return {}; });
            if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
            cache.set(asked, data);
            if (mine === seq) render(data);
        } catch (error) {
            if (mine === seq) body.replaceChildren(el('div', 'alert alert-warning py-2', `Couldn't load this season: ${error.message}`));
        }
    }

    async function publishSeason(button) {
        const asked = path || (item && item.media_file) || '';
        button.disabled = true;
        try {
            const result = await apiPost('/api/markers/season/publish', { path: asked });
            // The job changes what the season shows once it runs: load it fresh next time.
            cache.clear();
            showToast('Publish season', 'Queued — see the Dashboard', 'success');
            const jobId = result && result.job_id;
            const toastBody = $('toastBody');
            if (jobId && toastBody) {
                const link = el('a', 'ms-1', String(jobId).substring(0, 8));
                link.href = '/?job=' + encodeURIComponent(jobId);
                toastBody.append(' ', link);
            }
        } catch (error) {
            showToast('Publish season', `Couldn't queue it: ${error.message}`, 'danger');
        } finally {
            button.disabled = false;
        }
    }

    function showView(season) {
        $('markersViewEpisode').checked = !season;
        $('markersViewSeason').checked = season;
        $('markersEpisodeView').hidden = season;
        $('markersSeasonBody').hidden = !season;
        if (season) load();
    }

    function openEpisode(episodePath) {
        showView(false);
        window.loadMarkersInspector({ server_id: '', item_id: '', media_file: episodePath, type: 'episode' });
    }

    function setItem(next) {
        item = next;
        path = '';
        seq++;
        const toggle = $('markersViewToggle');
        if (!toggle) return;
        toggle.hidden = !(next && next.type === 'episode' && next.media_file);
        showView(false);
    }

    function setPath(next) {
        path = next || '';
    }

    function wire() {
        const season = $('markersViewSeason');
        const episode = $('markersViewEpisode');
        if (!season || !episode) return;
        season.addEventListener('change', function () { if (season.checked) showView(true); });
        episode.addEventListener('change', function () { if (episode.checked) showView(false); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
    else wire();

    window.markersSeason = { setItem: setItem, setPath: setPath };
})();
```

(`openEpisode` → `loadMarkersInspector` → `setItem` hides nothing for an episode and resets to "This episode"; the
Preview tab keeps the file the user searched for.)

`markers_inspector.css` (append):

```css
.mk-season-title {
    font-size: 1.1rem;
    font-weight: 650;
}

.mk-season-table td,
.mk-season-table th {
    white-space: nowrap;
}

.mk-season-time {
    font-family: var(--bs-font-monospace);
}

.mk-chip {
    margin-right: 4px;
    border: 1px solid var(--bs-border-color);
    background: transparent;
    color: var(--bs-body-color);
    font-weight: 500;
}

.mk-chip-locked {
    background: var(--bs-secondary-bg);
}

.mk-dots {
    display: flex;
    gap: 6px;
}

.mk-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--bs-secondary-bg);
    border: 1px solid var(--bs-border-color);
}

.mk-dot-ok {
    background: var(--bs-success);
    border-color: var(--bs-success);
}

.mk-dot-waiting {
    background: var(--bs-warning);
    border-color: var(--bs-warning);
}

.mk-dot-failed {
    background: var(--bs-danger);
    border-color: var(--bs-danger);
}
```

- [ ] **Step 5: Run the e2e files**

Run: `/home/data/.venv/bin/python -m pytest -m e2e -n 8 --no-cov tests/e2e/test_intro_credits_season.py tests/e2e/test_intro_credits_inspector.py -q`
Expected: PASS (the inspector file proves the wrapper div and `setItem`/`setPath` calls changed nothing there).

- [ ] **Step 6: Real app (storage lab)**

With `mlab-app` rebuilt on this branch: open `http://127.0.0.1:18080/bif-viewer` in Playwright (log in with
`MLAB_APP_TOKEN` read from `evidence/lab/env` inside the script, never echoed), search "Rick and Morty" on mlab-plex,
open S01E01, Intro & Credits → Whole season. Screenshot to
`docs/design/intro-credits/evidence/screenshots/phase2/season-view-rick-and-morty.png`; check the counts against Task 12
Step 5's printed counts.

- [ ] **Step 7: Owner screenshot checkpoint**

Show the owner the lab screenshot beside the design artifact's Season view, listing the four differences named at the
top of this task. Do not commit until the owner accepts them.

- [ ] **Step 8: Commit**

`feat(markers): Inspector Season view with per-server dots and season publish`.

---
## Task 15: Harness — full report (decisions vs Plex's own markers, online cases, credits chapter rules)

`[sequential]` (after Tasks 6, 7; may run beside Tasks 9–14, only `tools/` and `tests/markers_eval/`) — spec §10.2
("TV intros 118 … Credits: 80 files + the 205-movie chapter set … Online: 43 verified cases … results pasted in the
PR"), roadmap phase 2 (`tools/markers_eval/`: "a **Plex baseline** column read-only from the prod Plex DB for the same
files. Report useful/wrong/missed per source, per publish setting, and for Plex's own markers." Done when: "harness ≥
spec §5.3 … **our decided markers are at least as good as Plex's native markers on the same files (more useful, not
more wrong)**"), ledger L165 ("anime 'Ending' titles and files with two 'End Credits' chapters: report").

Same data and resource rules as Task 6: `nice -n 19`; media only read by ffprobe/ffmpeg; caches under
`$MARKERS_EVAL_CACHE`; truth files local-only; the prod Plex DB only through `sqlite3 "file:<db>?mode=ro"` over ssh;
committed summaries hold counts and show names, never paths.

**What "at least as good as Plex" is measured on.** The 118 intro episodes: truth = their intro chapters, so chapters
can't also be a source there. Our decision per episode runs the real `decide()` on season audio (the port) plus
Plex's own intro marker for the same file as `server_markers` (agreement only, spec §5.5 rule 7), at High and at
Medium. Online answers aren't recorded for these files, so this is a floor for what a real library gets. At High, a
season-audio answer only publishes when Plex's marker agrees, so High can't be "more useful" than Plex on this set by
construction; the gate is therefore **Medium beats Plex (useful ≥, wrong ≤) and High is not more wrong than Plex**,
and both rows go to the owner (plan gap G4).

**Files:**
- Create: `tools/markers_eval/online.py`, `tools/markers_eval/plex.py`, `tools/markers_eval/credits.py`,
  `tools/markers_eval/decisions.py`
- Modify: `tools/markers_eval/data.py` (`EvalEpisode.duration_s`), `tools/markers_eval/cache.py` (`ProbeCache`),
  `tools/markers_eval/__main__.py` (`plex-sql`, `report`), `tools/markers_eval/README.md`,
  `docs/design/intro-credits/evidence/eval/phase2-harness.md`
- Test: `tests/markers_eval/test_online.py`, `tests/markers_eval/test_plex.py`, `tests/markers_eval/test_credits.py`,
  `tests/markers_eval/test_decisions.py`

**Interfaces:**
- Consumes: Task 6 `EvalEpisode`, `load_v3_results`, `by_season`, `Tally`, `judge_intro`, `FingerprintCache.points`,
  `_cache(args)`; Task 2 `season_intros(fps) -> dict[str, IntroSegment | None]` (`IntroSegment.start_s`, `end_s`,
  `support`); Task 7 `season_group(path).episodes`; phase 1 `decide(candidates, ctx, locked)`,
  `DecisionContext(duration_ms, is_movie, publish_when, enabled_types, source_order)`, `DecisionStatus`,
  `theintrodb._candidates(body)`, `introdb._candidates(body)`, `skipdb._candidates(segments)`,
  `chapters.chapter_candidates(probe)`, `chapters.classify_chapter_title(title)`, `probe.probe_media(path, *,
  ffprobe)`, `MediaProbe`, `Chapter`.
- Produces (Tasks 17, 18 paste its output):
```python
# tools/markers_eval/online.py
SETTINGS: tuple[tuple[str, tuple[str, ...], str], ...]
def load_online(evidence: Path | None = None) -> tuple[list[dict], list[dict]]
def skipdb_segments(case: dict, dump: list[dict]) -> dict[str, dict]
def judge_online(mtype: MarkerType, marker: Marker, case: dict) -> str          # "useful" | "late" | "wrong"
def run_online(results: list[dict], dump: list[dict], *, order: tuple[str, ...], level: str) -> dict[str, Counter]
# tools/markers_eval/plex.py
@dataclass(frozen=True) class PlexMarker: type: str; start_ms: int; end_ms: int; final: bool
def export_sql(folders: list[str]) -> str
def parse_export(lines: Iterable[str]) -> dict[str, list[PlexMarker]]
def load_baseline(path: Path) -> dict[str, list[PlexMarker]]
# tools/markers_eval/cache.py
class ProbeCache:
    def __init__(self, root: Path, *, ffprobe: str) -> None
    def probe(self, path: str) -> MediaProbe
# tools/markers_eval/credits.py
EARLY_S = 10.0; LATE_S = 30.0
def judge_credits(start_s: float | None, truth_s: float) -> str                  # "useful" | "late" | "wrong" | "missed"
@dataclass class CreditsReport: tally: Counter; titles_missed: list[str]; several_credits: list[str]; ending_titles: list[str]
def title_coverage(movies: list[dict]) -> CreditsReport
def chapter_rules(files: list[dict], adjudicated: dict[str, dict], *, probe: Callable[[str], MediaProbe]) -> CreditsReport
# tools/markers_eval/decisions.py
@dataclass class DecisionRows: plex: Tally; audio: Tally; high: Tally; medium: Tally
    def gate(self) -> bool
def season_segments(episodes: list[EvalEpisode], *, points: Callable[[str], np.ndarray], full_folder: bool) -> dict[str, tuple[float, float, int, int] | None]
def compare_with_plex(episodes: list[EvalEpisode], segments: dict[str, tuple[float, float, int, int] | None], baseline: dict[str, list[PlexMarker]]) -> DecisionRows
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/markers_eval/test_online.py
from collections import Counter

from media_preview_generator.markers.models import Marker, MarkerType
from tools.markers_eval.online import judge_online, run_online, skipdb_segments

CASE = {"show": "Show", "imdb": "tt1", "season": 1, "episode": 2, "dur": 1320.0, "intro": [60.0, 90.0], "credits_start": 1290.0}


def _result(tidb=None, idb=None):
    return {"case": CASE, "tidb": tidb, "idb": idb}


def _dump_row(kind, start_ms, end_ms, duration_ms):
    return {"imdb_id": "tt1", "season": 1, "episode": 2, "segment_type": kind, "start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration_ms}


def test_skipdb_segments_pick_the_nearest_duration_and_label_the_match():
    dump = [_dump_row("intro", 61_000, 90_000, 1_300_000), _dump_row("intro", 60_500, 90_000, 1_319_000)]
    seg = skipdb_segments(CASE, dump)["intro"]
    assert (seg["start_ms"], seg["match"]) == (60_500, "exact")
    assert skipdb_segments(CASE, [_dump_row("intro", 1, 2, 1_000_000)])["intro"]["match"] == "out-of-range"


def test_judge_online_matches_the_audit_rules():
    assert judge_online(MarkerType.INTRO, Marker(MarkerType.INTRO, 59_000, 94_000, ("x",)), CASE) == "useful"
    assert judge_online(MarkerType.INTRO, Marker(MarkerType.INTRO, 59_000, 95_500, ("x",)), CASE) == "wrong"
    assert judge_online(MarkerType.CREDITS, Marker(MarkerType.CREDITS, 1_279_000, 1_320_000, ("x",)), CASE) == "wrong"
    assert judge_online(MarkerType.CREDITS, Marker(MarkerType.CREDITS, 1_321_000, 1_325_000, ("x",)), CASE) == "late"


def test_run_online_decides_through_the_real_rules():
    idb = {"intro": {"start_ms": 60_000, "end_ms": 90_000}, "recap": None, "outro": None}
    dump = [_dump_row("intro", 60_500, 91_000, 1_319_000)]
    high = run_online([_result(idb=idb)], dump, order=("chapters", "introdb", "skipdb", "server_markers"), level="high")
    assert high["intro"] == Counter(useful=1)
    assert high["credits"] == Counter(missed=1)
    alone = run_online([_result(idb=idb)], [], order=("chapters", "introdb", "skipdb", "server_markers"), level="medium")
    assert alone["intro"] == Counter(missed=1)  # IntroDB alone never decides (spec §5.5 rule 6)
```

```python
# tests/markers_eval/test_plex.py
from tools.markers_eval.plex import PlexMarker, export_sql, parse_export


def test_parse_export_keeps_markers_and_files_without_any():
    lines = [
        "/tv/Show/Season 01/Show - S01E01.mkv|intro|1609|32897|{\"pv:version\":\"5\"}|1319744|abc\n",
        "/tv/Show/Season 01/Show - S01E01.mkv|credits|1290000|1319744|{\"pv:final\":\"1\"}|1319744|abc\n",
        "/tv/Show/Season 01/Show - S01E02.mkv|||||1300000|def\n",
    ]
    out = parse_export(lines)
    assert out["/tv/Show/Season 01/Show - S01E01.mkv"] == [
        PlexMarker("intro", 1609, 32897, False),
        PlexMarker("credits", 1_290_000, 1_319_744, True),
    ]
    assert out["/tv/Show/Season 01/Show - S01E02.mkv"] == []


def test_export_sql_is_read_only_and_escapes_folder_names():
    sql = export_sql(["/tv/Bob's 100% Show/Season 01"])
    assert "LIKE '/tv/Bob''s 100\\% Show/Season 01/%' ESCAPE '\\'" in sql
    for word in ("INSERT", "UPDATE", "DELETE", "CREATE", "ATTACH", "PRAGMA"):
        assert word not in sql.upper()
```

```python
# tests/markers_eval/test_credits.py
from collections import Counter

from media_preview_generator.markers.probe import Chapter, MediaProbe
from tools.markers_eval.credits import chapter_rules, judge_credits, title_coverage


def test_judge_credits():
    assert [judge_credits(s, 100.0) for s in (None, 89.0, 95.0, 131.0)] == ["missed", "wrong", "useful", "late"]


def test_title_coverage_lists_unrecognised_and_ending_titles():
    movies = [
        {"file": "/m/A (2001)/A.mkv", "chapters": ["Opening", "End Credits"]},
        {"file": "/m/B (2002)/B.mkv", "chapters": ["Part 1", "Ending"]},
        {"file": "/m/C (2003)/C.mkv", "chapters": ["Part 1", "Rolling Titles"]},
    ]
    report = title_coverage(movies)
    assert report.tally == Counter(found=1, not_found=2)  # "Ending" isn't a credits title today (ledger L165)
    assert report.titles_missed == ["Ending", "Rolling Titles"]
    assert report.ending_titles == ["B (2002)"]


def test_chapter_rules_judges_against_adjudicated_truth_and_reports_two_credits_chapters():
    probes = {
        "/m/A (2001)/A.mkv": MediaProbe(6_000_000, (Chapter(0, 5_700_000, "Film"), Chapter(5_700_000, None, "End Credits"))),
        "/m/B (2002)/B.mkv": MediaProbe(6_000_000, (Chapter(0, 5_500_000, "Film"), Chapter(5_500_000, 5_800_000, "End Credits"), Chapter(5_800_000, None, "End Credits"))),
    }  # fmt: skip
    files = [
        {"file": "/m/A (2001)/A.mkv", "credits_start": 5690.0},
        {"file": "/m/B (2002)/B.mkv", "credits_start": 5500.0},
    ]
    adjudicated = {"B.mkv": {"truth": 5460.0, "reason": "names over footage"}}
    report = chapter_rules(files, adjudicated, probe=probes.__getitem__)
    assert report.tally == Counter(useful=1, late=1)
    assert report.several_credits == ["B (2002)"]
```

```python
# tests/markers_eval/test_decisions.py
import numpy as np

from tools.markers_eval.data import EvalEpisode
from tools.markers_eval.decisions import compare_with_plex, season_segments
from tools.markers_eval.plex import PlexMarker


def _ep(name, truth=(60.0, 90.0)):
    return EvalEpisode(season="/tv/S/Season 01", file=f"/tv/S/Season 01/{name}.mkv", truth_intro=truth,
                       truth_credits=None, v3_segment=None, duration_s=1320.0)  # fmt: skip


def test_rows_for_plex_audio_high_and_medium():
    e1, e2, e3 = _ep("e1"), _ep("e2"), _ep("e3")
    segments = {e1.file: (60.2, 89.8, 2, 2), e2.file: (60.0, 90.0, 2, 2), e3.file: None}
    baseline = {
        e1.file: [PlexMarker("intro", 60_000, 90_500, False)],  # agrees with audio
        e2.file: [PlexMarker("intro", 140_000, 170_000, False)],  # Plex wrong, disagrees
        e3.file: [PlexMarker("intro", 61_000, 90_000, False)],  # Plex alone
    }
    rows = compare_with_plex([e1, e2, e3], segments, baseline)
    assert rows.plex.as_dict() == {"useful": 2, "wrong": 1, "missed": 0}
    assert rows.audio.as_dict() == {"useful": 2, "wrong": 0, "missed": 1}
    assert rows.high.as_dict() == {"useful": 1, "wrong": 0, "missed": 2}  # e2 contradicted, e3 server markers alone
    assert rows.medium.as_dict() == {"useful": 1, "wrong": 0, "missed": 2}  # a contradicting server marker blocks Medium too
    assert rows.gate() is False  # Medium isn't more useful than Plex here


def test_same_season_audio_alone_decides_at_medium_and_passes_the_gate():
    e1, e2, e3, e4 = _ep("e1"), _ep("e2"), _ep("e3"), _ep("e4")
    segments = {e1.file: (60.2, 89.8, 3, 3), e2.file: (60.0, 90.0, 3, 3), e3.file: None, e4.file: (59.5, 90.2, 3, 3)}
    baseline = {
        e1.file: [PlexMarker("intro", 60_000, 90_500, False)],
        e2.file: [PlexMarker("intro", 140_000, 170_000, False)],
        e3.file: [PlexMarker("intro", 61_000, 90_000, False)],
        e4.file: [],
    }
    rows = compare_with_plex([e1, e2, e3, e4], segments, baseline)
    assert rows.plex.as_dict() == {"useful": 2, "wrong": 1, "missed": 1}
    assert rows.high.as_dict() == {"useful": 1, "wrong": 0, "missed": 3}
    assert rows.medium.as_dict() == {"useful": 2, "wrong": 0, "missed": 2}
    assert rows.gate() is True


def test_season_segments_eval_lists_and_full_folder(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("tools.markers_eval.decisions.season_intros", lambda fps: calls.append(sorted(fps)) or {f: None for f in fps})
    monkeypatch.setattr("tools.markers_eval.decisions.season_group", lambda path: type("G", (), {"episodes": ("/tv/S/Season 01/e1.mkv", "/tv/S/Season 01/e2.mkv", "/tv/S/Season 01/e9.mkv")})())
    eps = [_ep("e1"), _ep("e2")]
    season_segments(eps, points=lambda p: np.zeros(4, dtype=np.uint32), full_folder=False)
    season_segments(eps, points=lambda p: np.zeros(4, dtype=np.uint32), full_folder=True)
    assert calls == [["/tv/S/Season 01/e1.mkv", "/tv/S/Season 01/e2.mkv"],
                     ["/tv/S/Season 01/e1.mkv", "/tv/S/Season 01/e2.mkv", "/tv/S/Season 01/e9.mkv"]]  # fmt: skip
```

The expected rows were traced through the branch's `decide()`: e1's audio and Plex marker agree at both levels
("sources agree: season_audio, server_markers"); e2 is "sources disagree" at both; e3 is "sources don't agree yet"
(server markers never decide alone). e4 in the second test relies on Task 7's rule (same-season audio decides alone at
Medium). If `decide()` answers differently after Task 7, stop: either Task 7 changed more than R2 or the test is wrong
— find out which before changing a number.

- [ ] **Step 2: Run them to verify they fail**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers_eval -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.markers_eval.online'`.

- [ ] **Step 3: Implement `online.py`, `plex.py`, `ProbeCache`, `credits.py`**

```python
# tools/markers_eval/online.py
"""The 43 verified online cases through the real source parsers and decide() (ported from audit-phase1/online43_decide.py).

SkipDB is simulated from its ODbL dump by the nearest-duration row; ``match`` is exact (≤ 2 s), shifted (≤ 15 s) or
out-of-range by |dump duration − file duration|, as the read API labels it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide
from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.sources import introdb, skipdb, theintrodb

from .data import evidence_dir

SETTINGS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Default sources, High", ("chapters", "introdb", "skipdb", "server_markers"), "high"),
    ("TheIntroDB on, High", ("chapters", "theintrodb", "introdb", "skipdb", "server_markers"), "high"),
    ("TheIntroDB on, Medium", ("chapters", "theintrodb", "introdb", "skipdb", "server_markers"), "medium"),
)
_SKIPDB_KEYS = ("intro", "outro", "recap", "preview")


def load_online(evidence: Path | None = None) -> tuple[list[dict], list[dict]]:
    """``online/online_results.json`` (recorded TheIntroDB/IntroDB answers per case) and the SkipDB dump's segments."""
    root = evidence or evidence_dir()
    results = json.loads((root / "online/online_results.json").read_text())
    dump = json.loads((root / "online/skipdb-dump.json").read_text())["segments"]
    return results, dump


def skipdb_segments(case: dict, dump: list[dict]) -> dict[str, dict]:
    """The dump rows SkipDB's read API would return for this case, each with its ``match`` label."""
    rows = [x for x in dump if x["imdb_id"] == case["imdb"] and x.get("season") == case["season"] and x.get("episode") == case["episode"]]
    out: dict[str, dict] = {}
    for key in _SKIPDB_KEYS:
        found = [x for x in rows if x["segment_type"] == key and x.get("start_ms") is not None]
        if not found:
            continue
        best = min(found, key=lambda x: abs((x.get("duration_ms") or 0) - case["dur"] * 1000))
        diff = abs((best.get("duration_ms") or 0) - case["dur"] * 1000)
        out[key] = {**best, "match": "exact" if diff <= 2000 else "shifted" if diff <= 15000 else "out-of-range"}
    return out


def judge_online(mtype: MarkerType, marker: Marker, case: dict) -> str:
    """The phase-1 audit's verdict: a wrong intro skips story (>5 s outside the truth); credits >10 s early are wrong."""
    start, end = marker.start_ms / 1000, marker.end_ms / 1000
    if mtype is MarkerType.INTRO:
        truth_start, truth_end = case["intro"]
        wrong = end > truth_end + 5 or start < truth_start - 5 or end < truth_start or start > truth_end
        return "wrong" if wrong else "useful"
    truth = case["credits_start"]
    if start < truth - 10:
        return "wrong"
    return "late" if start > truth + 30 else "useful"


def run_online(results: list[dict], dump: list[dict], *, order: tuple[str, ...], level: str) -> dict[str, Counter]:
    """Counts per type (``intro``, ``credits``): useful / late / wrong / missed (not decided)."""
    counts = {"intro": Counter(), "credits": Counter()}
    for row in results:
        case = row["case"]
        candidates = []
        if "theintrodb" in order and isinstance(row.get("tidb"), dict):
            candidates += theintrodb._candidates(row["tidb"])
        if "introdb" in order and isinstance(row.get("idb"), dict):
            candidates += introdb._candidates(row["idb"])
        if "skipdb" in order:
            candidates += skipdb._candidates(skipdb_segments(case, dump))
        ctx = DecisionContext(int(case["dur"] * 1000), False, level, frozenset({MarkerType.INTRO, MarkerType.CREDITS}), order)
        decisions = decide(candidates, ctx, {})
        for mtype in (MarkerType.INTRO, MarkerType.CREDITS):
            d = decisions[mtype]
            truth = case["intro"] if mtype is MarkerType.INTRO else case["credits_start"]
            if truth is None:
                continue
            verdict = judge_online(mtype, d.marker, case) if d.status is DecisionStatus.DECIDED else "missed"
            counts[mtype.value][verdict] += 1
    return counts
```

(The audit script counted a decided marker without truth as wrong; the eval cases all carry both truths, so rows
without one are skipped instead of guessed.)

```python
# tools/markers_eval/plex.py
"""Plex's own markers for the eval files, exported read-only from the prod Plex database (the "Plex baseline").

Export (README has the full command): ``python -m tools.markers_eval plex-sql`` piped over ssh into
``sqlite3 -separator '|' 'file:<prod db>?mode=ro'`` on ``plex``, saved as ``evidence/lab/prod_plex_baseline.txt``. Rows: file|type|start_ms|end_ms|extra_data|duration_ms|hash; a file
without markers has empty marker fields.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MARKER_TAG_TYPE = 12


@dataclass(frozen=True)
class PlexMarker:
    """One of Plex's markers on a file (``final``: credits that run to the end, Plex's ``pv:final``)."""

    type: str
    start_ms: int
    end_ms: int
    final: bool


def _like(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
    return f"mp.file LIKE '{escaped}/%' ESCAPE '\\'"


def export_sql(folders: list[str]) -> str:
    """A read-only query for every part under the given season folders, with its markers when it has any."""
    where = " OR ".join(_like(f) for f in sorted(set(folders)))
    return (
        "SELECT mp.file, t.text, t.time_offset, t.end_time_offset, t.extra_data, mp.duration, mp.hash\n"
        "FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id\n"
        "LEFT JOIN taggings t ON t.metadata_item_id = mi.metadata_item_id\n"
        f"  AND t.tag_id IN (SELECT id FROM tags WHERE tag_type = {MARKER_TAG_TYPE} AND tag = '')\n"
        f"WHERE {where}\nORDER BY mp.file, t.time_offset;\n"
    )


def parse_export(lines: Iterable[str]) -> dict[str, list[PlexMarker]]:
    """Markers by file path (``[]`` for a file Plex has no markers for)."""
    out: dict[str, list[PlexMarker]] = {}
    for line in lines:
        parts = line.rstrip("\n").split("|")
        if len(parts) < 7:
            continue
        path, mtype, start, end, extra = parts[:5]
        markers = out.setdefault(path, [])
        if mtype and start and end:
            markers.append(PlexMarker(mtype, int(start), int(end), '"pv:final":"1"' in extra))
    return out


def load_baseline(path: Path) -> dict[str, list[PlexMarker]]:
    """``parse_export`` of a saved export file."""
    with path.open(encoding="utf-8") as fh:
        return parse_export(fh)
```

(`path` may itself contain `|` in theory; the prod library's paths don't — `parse_export` would drop such a row, and
`compare_with_plex` then counts that episode's Plex answer as missed, which only flatters Plex's baseline downwards;
note it in the README.)

`cache.py` — add:

```python
class ProbeCache:
    """``probe(path)``: ffprobe duration and chapters of a file, cached as JSON per file identity."""

    def __init__(self, root: Path, *, ffprobe: str) -> None:
        """Create the cache.

        Args:
            root: Cache folder (created); must not be under /data*.
            ffprobe: ffprobe binary.

        Raises:
            ValueError: ``root`` is under /data*.
        """
        if str(root.resolve()).startswith("/data"):
            raise ValueError("the probe cache must not live under /data*")
        self._root = root / "probes"
        self._root.mkdir(parents=True, exist_ok=True)
        self._ffprobe = ffprobe

    def probe(self, path: str) -> MediaProbe:
        """Probe (or read the cached probe of) one file."""
        st = os.stat(path)
        key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
        cached = self._root / f"{key}.json"
        if cached.exists():
            raw = json.loads(cached.read_text())
            return MediaProbe(raw["duration_ms"], tuple(Chapter(c["start_ms"], c["end_ms"], c["title"]) for c in raw["chapters"]))
        probe = probe_media(path, ffprobe=self._ffprobe)
        cached.write_text(json.dumps({"duration_ms": probe.duration_ms, "chapters": [dataclasses.asdict(c) for c in probe.chapters]}))
        return probe
```

(imports: `dataclasses`, `json`, `from media_preview_generator.markers.probe import Chapter, MediaProbe, probe_media`.)
Also give `FingerprintCache` a `root` property returning `self._root`.

```python
# tools/markers_eval/credits.py
"""Credits from chapters (spec §5.1 rules) against hand-checked truth, and the chapter titles those rules miss."""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from media_preview_generator.markers.models import MarkerType
from media_preview_generator.markers.probe import MediaProbe
from media_preview_generator.markers.sources.chapters import chapter_candidates, classify_chapter_title

EARLY_S = 10.0
LATE_S = 30.0
_ENDING_RE = re.compile(r"\bending\b", re.I)


@dataclass
class CreditsReport:
    """Counts plus the files worth a human look (named by their folder, never a path)."""

    tally: Counter = field(default_factory=Counter)
    titles_missed: list[str] = field(default_factory=list)
    several_credits: list[str] = field(default_factory=list)
    ending_titles: list[str] = field(default_factory=list)


def _name(path: str) -> str:
    return os.path.basename(os.path.dirname(path))


def judge_credits(start_s: float | None, truth_s: float) -> str:
    """``missed`` (no answer), ``wrong`` (>10 s early: skips story), ``late`` (>30 s late) or ``useful``."""
    if start_s is None:
        return "missed"
    if start_s < truth_s - EARLY_S:
        return "wrong"
    return "late" if start_s > truth_s + LATE_S else "useful"


def title_coverage(movies: list[dict]) -> CreditsReport:
    """The 205-movie chapter set: does the title classifier see a credits chapter in each file's titles?"""
    report = CreditsReport()
    for movie in movies:
        titles = list(movie.get("chapters") or [])
        found = any(classify_chapter_title(t) is MarkerType.CREDITS for t in titles)
        report.tally["found" if found else "not_found"] += 1
        if not found and titles:
            report.titles_missed.append(titles[-1])
        if any(_ENDING_RE.search(t) for t in titles):
            report.ending_titles.append(_name(movie["file"]))
    return report


def chapter_rules(files: list[dict], adjudicated: dict[str, dict], *, probe: Callable[[str], MediaProbe]) -> CreditsReport:
    """The 80 hand-checked files: the chapter source's credits start against the truth (adjudicated wins)."""
    report = CreditsReport()
    for entry in files:
        path = entry["file"]
        truth = (adjudicated.get(os.path.basename(path)) or {}).get("truth", entry["credits_start"])
        media = probe(path)
        credits = sorted((c for c in chapter_candidates(media) if c.type is MarkerType.CREDITS), key=lambda c: c.start_ms)
        if len(credits) > 1:
            report.several_credits.append(_name(path))
        start = credits[-1].start_ms / 1000 if credits else None
        report.tally[judge_credits(start, float(truth))] += 1
        if any(_ENDING_RE.search(c.title) for c in media.chapters):
            report.ending_titles.append(_name(path))
    return report
```

The spec's rule 3 takes the **last** credits chapter; `chapter_rules` does the same so the report measures what the
decision would publish. `adjudicated.json` is keyed by file basename (checked: its first key is a bare file name).

- [ ] **Step 4: Implement `decisions.py` and `EvalEpisode.duration_s`**

`data.py`: `EvalEpisode` gains `duration_s: float | None = None` as its last field; `load_v3_results` passes
`duration_s=(r.get("intro") or {}).get("duration")`.

```python
# tools/markers_eval/decisions.py
"""Our intro decisions on the 118 episodes against Plex's own markers for the same files (roadmap phase 2 gate)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from media_preview_generator.markers.audio.matcher import season_intros
from media_preview_generator.markers.audio.season import season_group
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide
from media_preview_generator.markers.models import Candidate, MarkerType, Source

from .data import EvalEpisode, by_season
from .plex import PlexMarker
from .score import Tally, judge_intro

ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
         "server_markers", "server_markers_imported")  # fmt: skip


@dataclass
class DecisionRows:
    """useful/wrong/missed for Plex's own intro markers, season audio alone, and our decisions at High and Medium."""

    plex: Tally = field(default_factory=Tally)
    audio: Tally = field(default_factory=Tally)
    high: Tally = field(default_factory=Tally)
    medium: Tally = field(default_factory=Tally)

    def gate(self) -> bool:
        """Medium at least as good as Plex (more or as useful, no more wrong) and High no more wrong than Plex."""
        return self.medium.at_least(self.plex.useful, self.plex.wrong) and self.high.wrong <= self.plex.wrong


def season_segments(
    episodes: list[EvalEpisode], *, points: Callable[[str], np.ndarray], full_folder: bool
) -> dict[str, tuple[float, float, int, int] | None]:
    """Season audio per eval episode: (start_s, end_s, support, others) or None.

    Args:
        episodes: The eval episodes.
        points: Fingerprint of a file.
        full_folder: Match against every episode in the season folder (what the app does) instead of the eval's own
            file lists (at most 8 per season, how spec §5.3 was measured).

    Returns:
        By eval file path.
    """
    out: dict[str, tuple[float, float, int, int] | None] = {}
    for _season, group in by_season(episodes).items():
        files = list(season_group(group[0].file).episodes) if full_folder else [e.file for e in group]
        segments = season_intros({f: points(f) for f in files})
        others = len(files) - 1
        for e in group:
            seg = segments.get(e.file)
            out[e.file] = (seg.start_s, seg.end_s, seg.support, others) if seg else None
    return out


def compare_with_plex(
    episodes: list[EvalEpisode],
    segments: dict[str, tuple[float, float, int, int] | None],
    baseline: dict[str, list[PlexMarker]],
) -> DecisionRows:
    """Judge Plex's intro, season audio alone, and ``decide()`` on both at High and Medium, per episode with a truth."""
    rows = DecisionRows()
    for e in episodes:
        if e.truth_intro is None or e.duration_s is None:
            continue
        plex = [m for m in baseline.get(e.file, []) if m.type == "intro"]
        rows.plex.add(judge_intro((plex[0].start_ms / 1000, plex[0].end_ms / 1000) if plex else None, e.truth_intro))
        seg = segments.get(e.file)
        rows.audio.add(judge_intro(seg[:2] if seg else None, e.truth_intro))
        candidates = [Candidate(MarkerType.INTRO, m.start_ms, m.end_ms, Source.SERVER_MARKERS, 1.0, "plex") for m in plex]
        if seg:
            # Same integers and confidence as markers/audio/season.py's _candidate, so the harness decides what the app would.
            candidates.append(Candidate(MarkerType.INTRO, int(round(seg[0] * 1000)), int(round(seg[1] * 1000)),
                                        Source.SEASON_AUDIO, seg[2] / seg[3], f"{seg[2]}/{seg[3]}"))  # fmt: skip
        for level, tally in (("high", rows.high), ("medium", rows.medium)):
            ctx = DecisionContext(round(e.duration_s * 1000), False, level, frozenset({MarkerType.INTRO}), ORDER)
            d = decide(candidates, ctx, {})[MarkerType.INTRO]
            decided = (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if d.status is DecisionStatus.DECIDED else None
            tally.add(judge_intro(decided, e.truth_intro))
    return rows
```

`IntroSegment` is Task 2's `NamedTuple(start_s, end_s, support)`. The Plex baseline path
keys are Plex's own paths; the eval files are storage paths (`/data_16tb*/...`) and Plex on `plex` sees the same
`/data_16tb*` mounts (the existing `prod_plex_truth.txt` rows prove it), so they compare as they are.

- [ ] **Step 5: Wire the commands**

`__main__.py` — add:

```python
def cmd_plex_sql(args: argparse.Namespace) -> int:
    folders = sorted({e.season for e in load_v3_results()})
    print(export_sql(folders), end="")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    evidence = evidence_dir()
    episodes = load_v3_results()
    fingerprints = _cache(args)
    segments = season_segments(episodes, points=fingerprints.points, full_folder=args.full_folder)
    rows = compare_with_plex(episodes, segments, load_baseline(Path(args.plex_baseline)))
    results, dump = load_online(evidence)
    online = {label: run_online(results, dump, order=order, level=level) for label, order, level in SETTINGS}
    probes = ProbeCache(fingerprints.root, ffprobe=ffprobe_path_for(chromaprint_ffmpeg(args.ffmpeg)))
    files = json.loads((evidence / "credits/movies40.json").read_text()) + json.loads((evidence / "credits/tv40.json").read_text())
    adjudicated = json.loads((evidence / "credits/adjudicated.json").read_text())
    credits = chapter_rules(files, adjudicated, probe=probes.probe)
    coverage = title_coverage(json.loads((evidence / "credits/movie_credit_truth.json").read_text()))
    summary = {
        "mode": "full folder" if args.full_folder else "eval lists",
        "intros": {name: getattr(rows, name).as_dict() for name in ("plex", "audio", "high", "medium")},
        "gate": rows.gate(),
        "online": {label: {t: dict(c) for t, c in counts.items()} for label, counts in online.items()},
        "credits_80": dict(credits.tally),
        "credits_205_titles": dict(coverage.tally),
        "several_credits_chapters": sorted(set(credits.several_credits)),
        "ending_titles": sorted(set(credits.ending_titles + coverage.ending_titles)),
        "titles_missed": sorted(set(coverage.titles_missed)),
    }
    print(json.dumps(summary, indent=2))
    return 0 if rows.gate() and rows.audio.at_least(*SPEC_V3[:2]) else 1
```

Parsers: `plex-sql` (no options); `report` with `--ffmpeg`, `--cache`, `--plex-baseline` (required), `--full-folder`
(flag). README: both
commands, the export line from `plex.py`'s docstring, the "what at least as good as Plex is measured on" paragraph.

- [ ] **Step 6: Run the unit tests**

Run: `/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers_eval -q`
Expected: PASS.

- [ ] **Step 7: Export the Plex baseline (prod, read-only)**

```bash
cd /home/data/workspace/plex_generate_vid_previews
/home/data/.venv/bin/python -m tools.markers_eval plex-sql > "$SCRATCH/plex_baseline.sql"
grep -Eic 'insert|update|delete|create|attach|pragma|vacuum|replace' "$SCRATCH/plex_baseline.sql"   # must print 0
PROD_PLEX_DB="/config/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
ssh -o BatchMode=yes plex "nice -n 19 sqlite3 -separator '|' 'file:${PROD_PLEX_DB}?mode=ro'" < "$SCRATCH/plex_baseline.sql" \
  > docs/design/intro-credits/evidence/lab/prod_plex_baseline.txt
wc -l docs/design/intro-credits/evidence/lab/prod_plex_baseline.txt
```

(`$SCRATCH` = the session scratchpad. The database path is the one the phase-1 scale run reads read-only
(`phase1_matrix.PROD_PLEX_DB`); the `?mode=ro` URI is what keeps it read-only — never drop it. Add
`docs/design/intro-credits/evidence/lab/prod_plex_baseline.txt` to `.gitignore` next to `lab/*_truth.txt`.)

- [ ] **Step 8: Run the report both ways (storage, read-only media)**

```bash
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval report --ffmpeg /usr/bin/ffmpeg \
  --plex-baseline docs/design/intro-credits/evidence/lab/prod_plex_baseline.txt > "$SCRATCH/report_lists.json"
nice -n 19 /home/data/.venv/bin/python -m tools.markers_eval report --ffmpeg /usr/bin/ffmpeg --full-folder \
  --plex-baseline docs/design/intro-credits/evidence/lab/prod_plex_baseline.txt > "$SCRATCH/report_folders.json"
```

Expected (eval lists): `intros.audio` equals Task 6's tally; exit 0 when the gate holds. Full folder fingerprints every
episode of those seasons (read-only, a few hundred files, run once). If full folder is worse than eval lists on
`audio` (more wrong or fewer useful), stop and report both to the owner before Task 17: the app matches whole folders
(spec §5.3), so the owner picks between accepting it and capping the group size (plan gap G5). If the gate fails,
report the rows to the owner; don't tune the matcher or the rules.

- [ ] **Step 9: Committed summary**

`evidence/eval/phase2-harness.md`, section "Full report (Task 15)": date, commit, both modes' four intro rows, the gate,
the three online settings' counts per type, credits 80-file counts, 205-set title coverage, and the show/movie folder
names in `several_credits_chapters` and `ending_titles` (the L165 report) — no paths, no JSON dumps.

- [ ] **Step 10: Commit**

`test(markers): harness full report — decisions vs Plex's own markers, online cases, credits chapter rules`.

---
## Task 16: Docs, spec amendments, Emby catalog text

`[sequential]` (after Tasks 1–15) — `.claude/rules/docs.md` (README, `docs/reference.md`, `docs/guides.md` on every
feature/API/config change), spec §0 working rule ("Update this spec (not just code) whenever a decision changes; add a
dated line to §14"), roadmap phase 2 ("Catalog submission text + manual install docs"), owner checkpoint 4 (Emby
catalog forum thread / developer id).

No code changes. Every number written here comes from `evidence/eval/phase2-harness.md` (Tasks 6, 15) or
`evidence/lab/phase2-results.md` (Tasks 4, 10–14) as committed — copy them, never round or re-derive.

**Files:**
- Modify: `docs/guides.md` (§ "Intro & Credits": intro paragraph ~549, sources table ~568–576, "### Emby" ~648,
  "### Checking the servers still show them" ~660, troubleshooting table ~712–720; new subsections below)
- Modify: `docs/reference.md` (~247 intro line, `sources[].id` row ~281, job `source` row ~316, endpoints table ~371)
- Modify: `README.md` (~86 Intro & Credits feature line)
- Modify: `emby-plugin/README.md` (Task 4's file: add "Catalog submission")
- Modify: `docs/design/intro-credits/spec.md` (§0 status, §3.3, §5.3, §5.5 rule 6, §6.2 steps 4 and 6, §6.3 Emby,
  §6.4 item 5, §7.4, §13 item 5, §14)
- Modify: `docs/design/intro-credits/plan-roadmap.md` (phase 2 bullets: mark superseded lines),
  `docs/design/intro-credits/evidence/README.md` (new files)

**Interfaces:** none (docs). The guide heading `### Emby: the Media Preview Bridge for Emby plugin` must produce the
anchor `emby-the-media-preview-bridge-for-emby-plugin` that Task 13's `EMBY_MANUAL_GUIDE_URL` links to.

- [ ] **Step 1: `docs/guides.md`**

1. Intro paragraph: replace "and (in a later update) matching the theme tune across a season or reading the on-screen
   credit" with "matching the theme tune across a season, and (in a later update) reading the on-screen credit".
2. Sources table, season audio row:
   `| Matching audio across a season | TV intros. Compares the first minutes of a season's episodes and finds the theme they share: on 118 test episodes 91 right, 13 wrong, 14 missed. On its own it publishes only at **Medium**; at **High** another source has to agree. Needs the Docker image's ffmpeg (Settings shows "Not available" and why on other builds). CPU, about 2 s per episode, at most 2 at once. |`
3. New subsection after "### Needs review":

   ```markdown
   ### Season audio and weekly releases

   Season audio needs at least one other episode of the same season on disk. When a job checks an episode, it
   fingerprints every episode in that season folder that isn't fingerprinted yet (once per file; a replaced file is
   fingerprinted again) and matches them. A season's first episode, with no other episode yet, is compared with up to
   4 episodes of the previous season: that answer never publishes on its own and shows in the Inspector as
   "Previous season audio".

   When a new episode arrives, episodes of the same season that already have a fingerprint may now be decidable. If
   they aren't in the job that brought the new episode, the job queues one **Season: <show> · <season>** job for them
   (at the same priority, at most 500 files). A Season job never queues another one.
   ```
4. Replace "### Emby" (the "Not supported yet" paragraph) with:

   ```markdown
   ### Emby: the Media Preview Bridge for Emby plugin

   Emby has no API for markers, so a small Emby plugin, **Media Preview Bridge for Emby**, stores them and writes them
   as Emby's own intro/credits chapter markers. It puts them back when Emby rebuilds an item's chapters (a metadata
   refresh or rescan), and stops serving them when the file is replaced by a different-size file until this app sends
   them again.

   The Edit tab shows whether the plugin is installed and its version. When Emby's plugin catalog lists the plugin,
   **Install** installs it and restarts Emby. Otherwise install it by hand: download `MediaPreviewBridge.Emby.dll` for
   your Emby version (4.9 or 4.10) from the release, copy it into Emby's `plugins` folder (`/config/plugins` in the
   official Emby container) and restart Emby. The tab shows **Installed ✓** once Emby is back.

   Emby shows **Intro** (start and end) and **Credits start**. Emby has no "credits end": its Skip Credits button always
   skips to the end of the file, so credits followed by a scene (a stinger or next-episode preview) are not sent to
   Emby — the Inspector's Emby card says "Emby can't show credits that end before the file does". The API key or user
   this app uses for Emby needs administrator rights.
   ```
5. "### Checking the servers still show them": append

   ```markdown
   Every 12 hours, when any server has Intro & Credits on, the app also queues a Low-priority **Intro & Credits · Check
   servers** job. It reads back every item this app has published, in bulk (Plex: one database read per 500 items),
   and only checks again the files whose markers are gone, different, or whose Plex item gained or lost a version —
   those go through the normal rules (Keep Plex's, versions, the server switch). With nothing to fix it finishes at once.
   ```
6. Troubleshooting table: replace the Emby "Not available yet" row with three rows —
   `| *(Emby)* Red "Not installed" badge + **Install**: "Install the Media Preview Bridge for Emby plugin" | The plugin isn't on this Emby server and Emby's catalog lists it | Click **Install** |`,
   `| *(Emby)* Red "Not installed" · **Install by hand** | Emby's plugin catalog doesn't list the plugin yet | Follow [the manual install](#emby-the-media-preview-bridge-for-emby-plugin) |`,
   `| *(Emby)* Amber "Update needed" + **Update**: "Update Media Preview Bridge for Emby (installed …) to get markers support" | An older plugin without markers support | Click **Update** or copy the newer DLL |`;
   and add `| *(Settings)* Season audio "Not available" with a reason | This build's ffmpeg has no chromaprint (e.g. the arm64 image or a custom ffmpeg) | Use the amd64 Docker image; other sources keep working |`.

- [ ] **Step 2: `docs/reference.md` and `README.md`**

- ~247: "Skip Intro / Skip Credits markers for Plex, Jellyfin and Emby."
- `sources[].id` row notes: "`credits_text` is "Coming soon" in this release (its value round-trips, detection
  doesn't run). `season_audio` runs where ffmpeg has chromaprint (see `GET /api/markers/sources/local`)."
- Job `source` row: add `season` (a Season follow-up job), `inspector_season` (Season view "Publish"), `reconcile`
  (the 12-hourly check).
- Endpoints table rows, each with a `####` section in the existing style (request, 2xx body, errors):
  `GET /api/markers/season?path=` (Task 12 shape; 400 "Path is not a file inside any server library" / "Not a TV
  episode"; 500), `POST /api/markers/season/publish` (`{"path"}` → 202 `{"job_id"}`; 400; 503),
  `GET /api/markers/sources/local` (`{"season_audio": {"available", "ffmpeg", "message"}}`),
  `POST /api/markers/reconcile` (202 `{"job_id"}` or 200 `{"job_id": null, "reason": "Intro & Credits is off on every
  server"}`; 503). Copy each JSON example from the route's test in `tests/markers/test_api_markers.py`.
- `POST /api/servers/{id}/install-plugin`: "Jellyfin and Emby" and Emby's `manual: true` answer.
- `README.md` ~86: "Intro & Credits: Skip Intro / Skip Credits markers for Plex, Jellyfin and Emby, from chapters,
  online databases and season audio matching."

- [ ] **Step 3: Emby catalog submission text**

Append to `emby-plugin/README.md`:

```markdown
## Catalog submission

**Name:** Media Preview Bridge for Emby
**Short description:** Adds Skip Intro and Skip Credits markers from Media Preview Generator and keeps them in place.
**Description:** Media Preview Generator detects intros and credits once per file (chapters, online intro databases,
season audio matching) and sends them to every server that has the file. This plugin gives Emby a small API
(`/MediaPreviewBridge/Markers/{Id}`, administrators only) that stores those markers per item and writes them as Emby's
own IntroStart / IntroEnd / CreditsStart chapter markers, keeping every other chapter. When Emby rebuilds an item's
chapters it writes them again; when the file is replaced by a different file it stops until the app sends new ones;
when the item is removed it forgets them. No data leaves the server.
**Targets:** Emby Server 4.9 and 4.10 (separate DLLs).
**Source / issues:** https://github.com/stevezau/media_preview_generator (folder `emby-plugin/`).
**Developer:** <owner's Emby forum name — owner checkpoint 4>
```

Ask the owner (checkpoint 4) for the forum thread / developer id and whether to submit now; do not post anything.

- [ ] **Step 4: Spec amendments**

Edit these places; every changed rule also gets a dated §14 line (Step 5):
- §0 Status: phase 2 built (season audio, Emby plugin + publisher, reconcile job, Season view, harness); next step
  the phase-2 lab matrix (plan-phase2 Task 17).
- §3.3 Emby: add "4.9.1.90 — same chapter behaviour, plugin build proven on `mlab-emby49`" with the Task 4 results
  line reference.
- §5.3: after the few-episodes list add the full-folder numbers from Task 15 ("app mode, whole season folder: U/W/M")
  and the note that the harness's eval-lists mode reproduces the table above; replace "A new episode only fingerprints
  itself …" with R3's wording: "A job fingerprints the season folder's missing episodes on its workers, matches cached
  fingerprints inline, and queues a Season job for same-season episodes outside the job whose inputs changed."
- §5.5 rule 6: "… only when that source checks the file itself — chapters, SkipDB `exact`/`shifted` matches, or
  same-season audio (it compares the file's own audio with its season; the previous-season hint never) — …" (R2).
- §6.2 step 4: the R3 sentence above. Step 6: "Reconcile: after jobs = the read-back before "Up to date" and the
  delayed verify job (phase 1); periodic = a LOW 'Intro & Credits · Check servers' job every 12 h that bulk-reads
  every published item and re-runs drifted files. Plex `keep_plex` keeps Plex's markers (§14 2026-09-14), not stored
  as evidence." (R5; supersedes the roadmap's "stores Plex's set as evidence").
- §6.3 EmbyMarkerPublisher: replace the prototype bullets with the Task 4 HTTP contract (GET/POST/DELETE
  `/MediaPreviewBridge/Markers/{Id}` with `IntroStartTicks`, `IntroEndTicks`, `CreditsStartTicks`, `FileSize`,
  `Stale`; `Ping` `Features: ["markers"]`), the FileSize stale guard, the heal/remove behaviour, and R1: "Credits are
  sent only when they run to the end of the file (end ≥ duration − 2 s); otherwise none, with the note."
- §6.4 item 5: replace "runs when a season's last episode in the job finishes, in the dispatcher's completion
  callback" with the R3 sentence and "(the engine has no completion hook; phase 2 Task 3)".
- §7.4: "Publish N to M servers = a normal HIGH Intro & Credits job for the season folder (R4); Review opens that
  episode; editing is phase 4."
- §13 item 5: current catalog status (submitted / waiting for the owner's forum id).

- [ ] **Step 5: §14 dated lines**

Add one line each (date = the day the owner ruled; "recommended default, owner to confirm" if still open):
R1 Emby credits end rule; R2 same-season audio at Medium; R3 inline matching + Season follow-up jobs; R4 Season
publish; R5 visible 12-hourly reconcile job; per-(file, source) forced refresh and versioned local-detector evidence
(Task 3, ledger L165/L204); Plex `VERSIONS_CHANGED` from recorded item files (Task 9, ledger L184); harness gate
definition (Task 15: Medium beats Plex, High no more wrong).

- [ ] **Step 6: Roadmap and evidence map**

`plan-roadmap.md` phase 2: tick nothing (that's Task 18); append "(superseded: spec §14 <date>)" to the "`keep_plex`
stores Plex's set as evidence" and "after each Intro & Credits job" clauses. `evidence/README.md`: rows for
`eval/phase2-harness.md`, `lab/phase2-results.md`, `lab/phase2_matrix.py`, `lab/synth_audio.sh` (Task 17 creates the
last two — add their rows in Task 17 if this task lands first).

- [ ] **Step 7: Checks**

```bash
cd /home/data/workspace/plex_generate_vid_previews
grep -n "Coming soon" docs/guides.md docs/reference.md | grep -i "season audio\|season_audio\|matching audio"   # no output
grep -n "Not available yet\|coming in the next phase" docs/guides.md docs/reference.md                        # no output
/home/data/.venv/bin/python - <<'PY'
import re
heading = "Emby: the Media Preview Bridge for Emby plugin"
slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
assert slug == "emby-the-media-preview-bridge-for-emby-plugin", slug
assert f"### {heading}" in open("docs/guides.md").read()
PY
```

Expected: the two greps print nothing; the script exits 0.

- [ ] **Step 8: Commit**

`docs(markers): phase 2 guides, reference, Emby plugin catalog text and spec amendments`.

---
## Task 17: Phase-2 lab matrix (`phase2_matrix.py`) — the phase-2 "done when"

`[sequential]` (after Tasks 1–16) `[high-risk]` — spec §10.3 (lab: write, serve, refresh/scan/forced detection/restart,
client skip button via Playwright), §12 phase 2 "Done when: harness ≥ the §5.3 numbers, Emby skip button in the lab",
roadmap phase 2 done-when, ledger carry-overs: L144 (PROOF P3 steps 2–4, P4), L263 (Jellyfin 12.0 movie alternates),
L274 (Plex keeps the final flag on rebuilt credits after its forced intro detection), L276 (row 12 Plex app, owner).

Milestone audit first: before Step 1, run the whole-branch audit (execution model: "before Task 17") and fix its HIGH
and MED findings.

**Lab rules:** lab servers on storage only; never the prod Plex. Media under `/data*` is only mounted read-only; every
file the matrix creates, copies, touches or deletes lives under `evidence/lab/synth/`. Tokens from `evidence/lab/env`,
scrubbed by `phase1_matrix.scrub` from everything written or printed. One heavy step at a time, `nice -n 19` for
encodes. Results JSON is git-ignored (`evidence/**/*.json`); `phase2-results.md` is the committed summary.

**Files:**
- Create: `docs/design/intro-credits/evidence/lab/synth_audio.sh`, `docs/design/intro-credits/evidence/lab/phase2_matrix.py`
- Modify: `docs/design/intro-credits/evidence/lab/up.sh` (mount `Synth Audio (2022)` and `Synth Movie (2023)` for every
  server and the app; mount `synth/_plexonly` into `mlab-plex` only), `docs/design/intro-credits/evidence/lab/synth_chapters.sh`
  (the two-version synth movie), `docs/design/intro-credits/evidence/lab/phase2-results.md`,
  `docs/design/intro-credits/evidence/README.md` (rows for the two new scripts)

**Interfaces:**
- Consumes: `phase1_matrix` helpers (unchanged, imported): `ENV`, `scrub`, `say`, `now_iso`, `http`, `app`, `app_ok`,
  `plex`, `jf`, `emby`, `sh`, `wait_until`, `configure`, `status`, `start_markers_job`, `wait_job`, `job_files`,
  `item_payload`, `plex_db`, `plex_parts`, `plex_served`, `plex_wait_idle`, `jf_items`, `jf_segments`,
  `refresh_all_servers`, `jobs_since`, `set_redetect`, `play_and_find_skip`, `server_entries`, `MARKERS_ON`,
  `SYNTH_SEASON`, `SYNTH_TRUTH`, `synth_path`, `RICK_SEASON`, `VENV_PYTHON`, `SHOTS`, `JELLYFINS`, `APP`, `EMBY`.
  App routes from Tasks 10–12 (`/api/markers/reconcile`, `/api/markers/season`, `/api/markers/season/publish`,
  `/api/markers/sources/local`, `/api/servers/<id>/install-plugin`).
- Produces: `results/p2-row-NN.json` per row and `phase2-results.md` (Task 18 pastes its table into the PR).

- [ ] **Step 1: Build the image and bring the lab up**

```bash
cd /home/data/workspace/plex_generate_vid_previews
VER=$(/home/data/.venv/bin/python -m setuptools_scm)
nice -n 19 docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION="$VER" -t media_preview_generator:intro-credits . \
  > "$SCRATCH/build-intro-credits-p2.log" 2>&1
tail -3 "$SCRATCH/build-intro-credits-p2.log"
docker run --rm --entrypoint /usr/lib/jellyfin-ffmpeg/ffmpeg media_preview_generator:intro-credits -hide_banner -muxers \
  | grep -c chromaprint   # 1
cd docs/design/intro-credits/evidence/lab
./synth_audio.sh && ./synth_chapters.sh
docker rm -f mlab-plex mlab-jellyfin mlab-jf12 mlab-emby mlab-emby49 && ./up.sh   # volumes (claim, keys) kept
./app.sh recreate
```

(`$SCRATCH` = the session scratchpad. The Plex claim lives in `mlab_plex_config`; if `/identity` shows it unclaimed,
ask the owner for a claim token and run `PLEX_CLAIM=claim-xxxx ./up.sh recreate`.)

- [ ] **Step 2: `synth_audio.sh`**

```bash
#!/bin/bash
# Synthetic VP9/Opus episodes for the season audio lab: no chapters, a shared 30 s theme at a different offset in
# every episode, everything else unique per episode. Season audio (not chapters, not online sources) must find the
# theme. VP9/Opus because Playwright's Chromium has no H.264.
#
#   ./synth_audio.sh          encode missing files
#   ./synth_audio.sh force    re-encode everything
#
# Output (git-ignored; up.sh and app.sh mount the show folder read-only at /media/synth-audio):
#   synth/Synth Audio (2022)/Season 01/Synth Audio (2022) - S01E0N.webm   N = 1..4, 300 s
#   synth/Synth Audio (2022)/Season 02/Synth Audio (2022) - S02E01.webm   300 s
#   synth/_staging/Synth Audio (2022) - S02E02.webm                        300 s (the weekly-release row copies it in)
set -euo pipefail

readonly HERE="$(cd "$(dirname "$0")" && pwd)"
readonly SHOW="Synth Audio (2022)"
readonly ROOT="${HERE}/synth/${SHOW}"
readonly STAGING_DIR="${HERE}/synth/_staging"
readonly WORK_DIR="${HERE}/synth/.work-audio"
readonly FONT="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
readonly LENGTH=300
readonly THEME=30
readonly FORCE="${1:-}"

# "season episode" -> theme start (s); every start is inside the first 35% of 300 s and the theme never reaches the end.
declare -A THEME_START=(["1 1"]=20 ["1 2"]=45 ["1 3"]=5 ["1 4"]=70 ["2 1"]=15 ["2 2"]=60)

command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; exit 1; }
[[ -f "$FONT" ]] || { echo "font not found: $FONT" >&2; exit 1; }
mkdir -p "${ROOT}/Season 01" "${ROOT}/Season 02" "$STAGING_DIR" "$WORK_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT

# The theme: a fixed four-note melody over a fifth, identical in every episode.
readonly THEME_EXPR="0.35*sin(2*PI*(330+110*floor(mod(t*2,4)))*t)+0.2*sin(2*PI*495*t)"

encode() {
    local season="$1" episode="$2" out="$3"
    if [[ -f "$out" && "$FORCE" != "force" ]]; then
        echo "exists: ${out#"$HERE"/}"
        return
    fi
    local start="${THEME_START["$season $episode"]}"
    local after=$((LENGTH - start - THEME))
    local seed=$((season * 100 + episode))
    local tmp="${WORK_DIR}/$(basename "$out")"
    local label="S$(printf %02d "$season")E$(printf %02d "$episode")"
    # Unique parts: pink and brown noise with per-episode seeds, so no two episodes share any audio but the theme.
    nice -n 19 ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i "testsrc2=s=640x360:r=24:d=${start}" \
        -f lavfi -i "smptebars=s=640x360:r=24:d=${THEME}" \
        -f lavfi -i "testsrc2=s=640x360:r=24:d=${after}" \
        -f lavfi -i "anoisesrc=color=pink:seed=${seed}:amplitude=0.25:d=${start}:r=48000" \
        -f lavfi -i "aevalsrc=${THEME_EXPR}:s=48000:d=${THEME}" \
        -f lavfi -i "anoisesrc=color=brown:seed=$((seed + 50)):amplitude=0.25:d=${after}:r=48000" \
        -filter_complex "[3:a]aformat=channel_layouts=stereo[a0];[4:a]aformat=channel_layouts=stereo[a1];[5:a]aformat=channel_layouts=stereo[a2];[0:v][a0][1:v][a1][2:v][a2]concat=n=3:v=1:a=1[cv][a];[cv]drawtext=fontfile=${FONT}:fontsize=24:fontcolor=yellow:box=1:boxcolor=black@0.6:x=20:y=h-50:text='${label} theme ${start}s-$((start + THEME))s %{pts\\:hms}'[v]" \
        -map "[v]" -map "[a]" -metadata title="${SHOW} ${label}" \
        -c:v libvpx-vp9 -b:v 200k -deadline realtime -cpu-used 8 -row-mt 1 -g 48 -c:a libopus -b:a 64k "$tmp"
    # Move into place only when complete, so a server scan never sees a half-written file.
    mv -f "$tmp" "$out"
    echo "wrote:  ${out#"$HERE"/}"
}

for e in 1 2 3 4; do
    encode 1 "$e" "${ROOT}/Season 01/${SHOW} - S01E0${e}.webm"
done
encode 2 1 "${ROOT}/Season 02/${SHOW} - S02E01.webm"
encode 2 2 "${STAGING_DIR}/${SHOW} - S02E02.webm"
```

Check the synth season is decidable before any row uses it (read-only on the lab files, runs in seconds):

```bash
cd /home/data/workspace/plex_generate_vid_previews
/home/data/.venv/bin/python - <<'PY'
from pathlib import Path
from media_preview_generator.markers.audio.matcher import season_intros
from media_preview_generator.markers.audio.fingerprint import compute_fingerprint
from media_preview_generator.markers.probe import probe_media
season = Path("docs/design/intro-credits/evidence/lab/synth/Synth Audio (2022)/Season 01")
files = sorted(str(p) for p in season.glob("*.webm"))
fps = {f: compute_fingerprint(f, probe_media(f, ffprobe="/usr/bin/ffprobe").duration_ms, ffmpeg="/usr/bin/ffmpeg") for f in files}
truth = {1: 20, 2: 45, 3: 5, 4: 70}
for f, seg in season_intros(fps).items():
    e = int(f[-6:-5])
    print(Path(f).name, seg, "ok" if seg and abs(seg.start_s - truth[e]) <= 2 and abs(seg.end_s - truth[e] - 30) <= 2 else "BAD")
PY
```

Expected: four lines ending `ok`. Any `BAD`: the synth audio, not the matcher, is the suspect — lengthen the theme
to 40 s or make the unique parts louder, re-encode with `force`, re-check; record the final parameters in the script's
header. Never change matcher constants for synthetic media.

- [ ] **Step 3: Lab mounts and the synth movie**

`up.sh`: append to `MV`
`-v "${HERE}/synth/Synth Audio (2022):/media/synth-audio/Synth Audio (2022):ro"` and
`-v "${HERE}/synth/Synth Movie (2023):/media/synth-movies/Synth Movie (2023):ro"`; add
`PLEXONLY=(-v "${HERE}/synth/_plexonly:/media/plexonly:ro")` after `MV` and use `"${MV[@]}" "${PLEXONLY[@]}"` on the
`mlab-plex` line only (the app must not see it: that folder is the "version the app can't read" of ledger L184).

`synth_chapters.sh`: add a movie with two versions (ledger L263):

```bash
readonly MOVIE_DIR="${HERE}/synth/Synth Movie (2023)"
mkdir -p "$MOVIE_DIR" "${HERE}/synth/_plexonly/Synth Chapters (2021)/Season 01"
# Two versions of one movie (JF 12.0 alternate versions): same chapters, different encode height.
for v in 1080p 720p; do
    encode "${MOVIE_DIR}/Synth Movie (2023) - ${v}.webm" 1 \
        "Opening|0|30|chapter" "Story|30|100|chapter" "End Credits|100|120|credits"
done
# A 116 s cut of S01E03 that only Plex will see (the version-drift row copies it into synth/_plexonly and removes it).
encode "${STAGING_DIR}/Synth Chapters (2021) - S01E03 - Plexonly.webm" 3 \
    "Chapter 1|0|25|chapter" "Intro|25|55|intro" "Chapter 2|55|100|chapter" "Credits|100|116|credits"
```

(`encode` takes a title/start/end/kind list; the episode number only labels frames. If the two encodes come out
byte-identical, add `-metadata comment=${v}` in a `v`-specific branch so the files differ in size.)

Then in each server create or refresh libraries for `/media/synth-audio` (TV) and `/media/synth-movies` (Movies), and
on `mlab-plex` add `/media/plexonly` as a second location of the Synth Chapters library. Record the section/library
ids in `phase2-results.md` → "Lab setup".

- [ ] **Step 4: `phase2_matrix.py` — framework and configure**

```python
#!/usr/bin/env python3
"""Phase 2 lab matrix for Intro & Credits (plan-phase2 Task 17), on top of phase1_matrix's helpers.

    ./phase2_matrix.py configure        phase 1 configure + Emby 4.10/4.9 + synth audio libraries, Intro & Credits on
    ./phase2_matrix.py run 1 2 ...      run rows in the given order

Each row writes results/p2-row-NN.json (git-ignored) with its result and evidence; credentials are scrubbed.
Rows change lab state; phase2-results.md lists the order used and how to reset the lab.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import phase1_matrix as p1
from phase1_matrix import ENV, app_ok, emby, http, now_iso, plex, say, scrub, sh, wait_until

LAB = Path(__file__).resolve().parent
RESULTS = LAB / "results"
EMBY49 = "http://127.0.0.1:18099"
AUDIO_ROOT = "/media/synth-audio"
AUDIO_SHOW = f"{AUDIO_ROOT}/Synth Audio (2022)"
AUDIO_S1 = f"{AUDIO_SHOW}/Season 01"
AUDIO_S2 = f"{AUDIO_SHOW}/Season 02"
AUDIO_HOST = LAB / "synth" / "Synth Audio (2022)"
STAGED_S2E02 = LAB / "synth" / "_staging" / "Synth Audio (2022) - S02E02.webm"
# Theme truth written by synth_audio.sh: (season, episode) -> (start_ms, end_ms).
AUDIO_TRUTH = {(1, 1): (20_000, 50_000), (1, 2): (45_000, 75_000), (1, 3): (5_000, 35_000), (1, 4): (70_000, 100_000),
               (2, 1): (15_000, 45_000), (2, 2): (60_000, 90_000)}  # fmt: skip
EMBY_SERVERS = ("mlab-emby", "mlab-emby49")
ALL_MARKER_SERVERS = ("mlab-plex", "mlab-jellyfin", "mlab-jf12", *EMBY_SERVERS)


def audio_path(season: int, episode: int) -> str:
    return f"{AUDIO_SHOW}/Season {season:02d}/Synth Audio (2022) - S{season:02d}E{episode:02d}.webm"


def write_result(row: int, title: str, result: str, evidence: dict, notes: list[str] | None = None) -> dict:
    RESULTS.mkdir(exist_ok=True)
    body = scrub({"row": row, "title": title, "result": result, "at": now_iso(), "notes": notes or [], **evidence})
    (RESULTS / f"p2-row-{row:02d}.json").write_text(json.dumps(body, indent=2, default=str) + "\n")
    say(f"p2 row {row}: {result} — {title}")
    for note in notes or []:
        say(f"  - {note}")
    return body


def emby49(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    sep = "&" if "?" in path else "?"
    return http(method, f"{EMBY49}/emby{path}{sep}api_key={ENV['EMBY49_TOKEN']}", body=body)


def emby_call(server_id: str):
    return emby if server_id == "mlab-emby" else emby49


def emby_chapters(server_id: str) -> dict[str, list[dict]]:
    """File path -> marker chapters (IntroStart/IntroEnd/CreditsStart, ms) for every episode and movie on an Emby."""
    uid = ENV["EMBY_UID"] if server_id == "mlab-emby" else ENV["EMBY49_UID"]
    _, data = emby_call(server_id)("GET", f"/Users/{uid}/Items?Recursive=true&IncludeItemTypes=Episode,Movie&Fields=Path,Chapters")
    out = {}
    for item in data["Items"]:
        marks = [
            {"type": c["MarkerType"], "ms": c["StartPositionTicks"] // p1.TICKS_PER_MS}
            for c in item.get("Chapters") or []
            if c.get("MarkerType") in ("IntroStart", "IntroEnd", "CreditsStart")
        ]
        out[item["Path"]] = sorted(marks, key=lambda m: (m["ms"], m["type"]))
    return out


def server_entries() -> list[dict]:
    return [
        *p1.server_entries(),
        {
            "id": "mlab-emby49",
            "type": "emby",
            "name": "Lab Emby 4.9",
            "url": "http://mlab-emby49:8096",
            "auth": {"method": "api_key", "api_key": ENV["EMBY49_TOKEN"], "user_id": ENV["EMBY49_UID"]},
        },
    ]


def set_publish_when(level: str) -> None:
    app_ok("POST", "/api/settings", {"markers": {"publish_when": level}})
    say(f"publish_when -> {app_ok('GET', '/api/settings')['markers']['publish_when']}")


def configure() -> None:
    p1.configure()
    for entry in server_entries():
        if entry["id"] == "mlab-emby49":
            status, _ = p1.app("GET", "/api/servers/mlab-emby49")
            app_ok("PUT" if status == 200 else "POST", "/api/servers/mlab-emby49" if status == 200 else "/api/servers", entry)
    for sid in ALL_MARKER_SERVERS:
        app_ok("POST", f"/api/servers/{sid}/refresh-libraries")
    for sid in EMBY_SERVERS:
        app_ok("PUT", f"/api/servers/{sid}", {"markers": {"enabled": True, "library_ids": None}})
    for sid in ALL_MARKER_SERVERS:
        cap = app_ok("GET", f"/api/markers/servers/{sid}/status")["capability"]
        say(f"{sid}: {cap['state']} — {cap['message']}")
    say("local sources:", app_ok("GET", "/api/markers/sources/local"))


class ChromaprintSampler(threading.Thread):
    """How many chromaprint ffmpeg processes run in mlab-app, every 0.5 s, until stopped (rows 2 and 18)."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak = 0
        self.argv: list[str] = []
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            out = sh("docker", "exec", "mlab-app", "ps", "-eo", "args", check=False)
            lines = [line for line in out.splitlines() if "-f chromaprint" in line and "ps -eo" not in line]
            self.peak = max(self.peak, len(lines))
            if lines and not self.argv:
                self.argv = lines[0].split()
            self.stop.wait(0.5)


ROWS: dict[int, Any] = {}


def row(number: int):
    def register(fn):
        ROWS[number] = fn
        return fn

    return register


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("configure", "run"):
        print(__doc__)
        return 2
    if argv[0] == "configure":
        configure()
        return 0
    failed = 0
    for number in [int(a) for a in argv[1:]]:
        failed += ROWS[number]()["result"] == "fail"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

(`GET /api/servers/<id>` answers 404 for an unknown id, `api_servers.py` ~532.)

- [ ] **Step 5: Rows 1–4 (season audio)**

```python
@row(2)
def row_02_season_audio_backfill_medium() -> dict:
    """Synth Audio S01 + S02E01 at Medium on every server: S01 decided by season audio at the theme; S02E01 needs review
    (previous-season hint only); at most two chromaprint ffmpeg at once, each with -threads 2."""
    set_publish_when("medium")
    sampler = ChromaprintSampler()
    sampler.start()
    job = p1.start_markers_job({"file_paths": [AUDIO_SHOW], "library_name": "Phase 2 row 2 season audio", "force": True})
    done = p1.wait_job(job["id"], timeout=1800)
    sampler.stop.set()
    sampler.join()
    notes, decided = [], {}
    for (season, episode), (start, end) in AUDIO_TRUTH.items():
        if (season, episode) == (2, 2):
            continue
        payload = p1.item_payload(audio_path(season, episode))
        intro = payload["decisions"]["intro"]
        decided[f"S{season:02d}E{episode:02d}"] = intro
        if season == 1:
            m = intro["marker"]
            ok = m and "season_audio" in m["decided_by"] and abs(m["end_ms"] - end) <= 5_000 and abs(m["start_ms"] - start) <= 15_000
            if not ok:
                notes.append(f"S01E{episode:02d} intro {intro}")
        elif intro["status"] != "needs_review" or not any(e["source"] == "season_audio_previous" for e in payload["evidence"]):
            notes.append(f"S02E01 should need review with a previous-season hint: {intro['status']}")
    served = {
        "mlab-plex": {p: p1.plex_served(part["item"]) for p, part in p1.plex_parts().items() if p.startswith(AUDIO_S1)},
        **{sid: {p: p1.jf_segments(sid, i["id"]) for p, i in p1.jf_items(sid).items() if p.startswith(AUDIO_S1)} for sid in p1.JELLYFINS},
        **{sid: {p: c for p, c in emby_chapters(sid).items() if p.startswith(AUDIO_S1)} for sid in EMBY_SERVERS},
    }  # fmt: skip
    for sid, files in served.items():
        if len(files) != 4 or not all(files.values()):
            notes.append(f"{sid} doesn't serve an intro on every S01 episode: {sorted(k for k, v in files.items() if not v)}")
    if sampler.peak > 2 or (sampler.argv and "-threads" not in sampler.argv):
        notes.append(f"chromaprint processes peak {sampler.peak}, argv {sampler.argv}")
    result = "pass" if done["status"] == "completed" and not notes else "fail"
    return write_result(2, "Season audio backfill at Medium on five servers", result,
                        {"job": done["progress"], "decided": decided, "served": served, "chromaprint_peak": sampler.peak}, notes)  # fmt: skip
```

```python
@row(4)
def row_04_weekly_release() -> dict:
    """A new S02E02 arrives by webhook: its job decides it from S02E01's cached fingerprint and queues one Season job
    that re-decides S02E01 (Medium), which then shows on every server."""
    set_publish_when("medium")
    target = AUDIO_HOST / "Season 02" / STAGED_S2E02.name
    if not target.exists():
        subprocess.run(["cp", str(STAGED_S2E02), str(target)], check=True)
    p1.refresh_all_servers()
    t0 = now_iso()
    status, body = http("POST", f"{p1.APP}/api/webhooks/sonarr", headers={"X-Auth-Token": ENV["MLAB_APP_TOKEN"]}, body={
        "eventType": "Download",
        "series": {"title": "Synth Audio", "path": AUDIO_SHOW},
        "episodes": [{"seasonNumber": 2, "episodeNumber": 2, "title": "Synth Audio 2x2"}],
        "episodeFile": {"path": audio_path(2, 2), "relativePath": f"Season 02/{STAGED_S2E02.name}"},
    })  # fmt: skip
    if status >= 300:
        return write_result(4, "Weekly release", "fail", {"webhook": [status, body]})
    for batch in app_ok("GET", "/api/webhooks/pending")["pending"]:
        app_ok("POST", f"/api/webhooks/pending/{urllib.parse.quote(batch['key'], safe='')}/fire-now")
    season_job = wait_until("the Season job", lambda: next((j for j in p1.jobs_since(t0) if j["library_name"].startswith("Season: ")), None), timeout=1800, every=5)
    p1.wait_job(season_job["id"], timeout=1800)
    jobs = [{k: j[k] for k in ("id", "library_name", "status", "priority")} for j in p1.jobs_since(t0)]
    notes = []
    if season_job["library_name"] != "Season: Synth Audio (2022) · Season 02":
        notes.append(f"Season job name {season_job['library_name']}")
    if [f["file"] for f in p1.job_files(season_job["id"])] != [audio_path(2, 1)]:
        notes.append("the Season job should hold only S02E01")
    for episode in (1, 2):
        intro = p1.item_payload(audio_path(2, episode))["decisions"]["intro"]
        start, end = AUDIO_TRUTH[(2, episode)]
        if not intro["marker"] or abs(intro["marker"]["end_ms"] - end) > 5_000:
            notes.append(f"S02E{episode:02d} intro {intro}")
    result = "pass" if not notes and sum(j["library_name"].startswith("Season: ") for j in jobs) == 1 else "fail"
    return write_result(4, "Weekly release: new episode + one Season job", result, {"jobs": jobs}, notes)
```

(Files API entries name the path `file`, as `p1.row_01_backfill` reads them.)

Rows 1 and 3, written in the same style:
1. **Capability** — `configure`, then `GET /api/markers/servers/<id>/status` for all five servers: `ready`; both Embys'
   `details.plugin_version` equal the built plugin version; `GET /api/markers/sources/local` →
   `season_audio.available` true with `/usr/lib/jellyfin-ffmpeg/ffmpeg`. Pass when all hold.
3. **High alone** — `set_publish_when("high")`, forced single-file job on S01E02: intro `needs_review` unless evidence
   has a `server_markers` row agreeing within 5 s (then `decided` with `decided_by` containing `server_markers` is
   also a pass). Record Plex's own intro rows for the four S01 episodes from `p1.plex_db` (tag_type 12 rows not ours:
   compare with `markers.db` via `item_payload(...)["servers"]` `published`) — the evidence for plan gap G3 (Plex's
   detection and season audio both listen to the audio). Set `publish_when` back to `high`.

- [ ] **Step 6: Rows 5–9 (real episodes and Emby)**

5. **Rick and Morty S01 at High** — normal (not forced) job on `RICK_SEASON`: count episodes whose intro
   `decided_by` contains `season_audio`; for the 11 episodes with `online/cases.json` truth, judge the decided intro
   with `tools.markers_eval.score.judge_intro` (import from the repo root). Pass: no `wrong`; record useful/missed next
   to phase-1 row 2's numbers.
6. **Emby write and serve** — on `mlab-emby`: for Synth Chapters S01E01–E03 and Synth Audio S01E01–E04,
   `emby_chapters` shows `IntroStart`/`IntroEnd` at the decided intro (±1 s) and `CreditsStart` at the decided credits
   start only where credits run to the file end (Synth Chapters episodes: 100 s of 120 s → sent); the original chapters
   are still listed. Copy `synth/_staging/Synth Chapters (2021) - S01E01 - Extended.webm` next to S01E01 (it's the
   phase-1 multi-version file: 10 s tail after credits), refresh Emby, run a job on it: its `CreditsStart` is absent
   and the Files panel row for `mlab-emby` says "Emby can't show credits that end before the file does". Remove the
   copy afterwards (lab folder) and refresh.
7. **Emby wipe matrix** (4.10) — on Synth Chapters S01E02, after each step read `emby_chapters`:
   `POST /Items/{id}/Refresh?MetadataRefreshMode=FullRefresh&ReplaceAllMetadata=true` (heal: markers back within
   30 s), `MetadataRefreshMode=Default`, `MetadataRefreshMode=ValidationOnly`, library scan
   (`POST /Library/Refresh`), `docker restart mlab-emby`. Pass: markers present after every step. Then replace
   S01E03 with a different-size file (`synth_chapters.sh` output of E03 re-encoded with `-b:v 250k` into the lab folder),
   refresh: the plugin's `GET /MediaPreviewBridge/Markers/{id}` shows `Stale: true` and Emby lists no marker chapters;
   the next normal job re-sends them (`markers_written`) and `Stale` is false.
8. **Emby web Skip Intro** — `lab/emby_client.py <item id> <SHOTS>/emby-skip-intro.png` on Synth Chapters S01E02 (seek
   22 s is inside its 17–47 s intro): prints `skip found: True`. This is the spec §12 phase-2 "done when".
9. **Emby 4.9** — rows 6 (first paragraph only) and 7 (FullRefresh step only) against `mlab-emby49`.

- [ ] **Step 7: Rows 10–11 (reconcile and version drift)**

```python
@row(10)
def row_10_reconcile_restores() -> dict:
    """Markers a server dropped come back from one reconcile job; a second reconcile finds nothing to do."""
    set_publish_when("high")
    p1.set_redetect("restore")
    ep = p1.synth_path(1)
    parts = p1.plex_parts()
    item = parts[ep]["item"]
    plex("PUT", f"/library/metadata/{item}/credits", force=1)
    p1.plex_wait_idle()
    jf_item = p1.jf_items("mlab-jellyfin")[ep]["id"]
    p1.jf("mlab-jellyfin", "DELETE", f"/MediaPreviewBridge/Markers/{jf_item}")
    emby_item = next(i["Id"] for i in emby("GET", f"/Users/{ENV['EMBY_UID']}/Items?Recursive=true&IncludeItemTypes=Episode&Fields=Path")[1]["Items"] if i["Path"] == ep)
    emby("DELETE", f"/MediaPreviewBridge/Markers/{emby_item}")
    before = {"plex": p1.plex_served(item), "jellyfin": p1.jf_segments("mlab-jellyfin", jf_item), "emby": emby_chapters("mlab-emby").get(ep)}
    t0 = now_iso()
    first = app_ok("POST", "/api/markers/reconcile")
    job = p1.wait_job(first["job_id"], timeout=1800)
    files = [f["file"] for f in p1.job_files(first["job_id"])]
    truth = p1.SYNTH_TRUTH[1]
    after = {"plex": p1.plex_served(item), "jellyfin": p1.jf_segments("mlab-jellyfin", jf_item), "emby": emby_chapters("mlab-emby").get(ep)}
    second = app_ok("POST", "/api/markers/reconcile")
    second_job = p1.wait_job(second["job_id"], timeout=600) if second.get("job_id") else None
    notes = []
    if ep not in files:
        notes.append("the reconcile job didn't re-run the dropped file")
    if not any(m["type"] == "intro" and abs(m["start"] - truth[0]) <= 1_000 for m in after["plex"]):
        notes.append(f"Plex after reconcile {after['plex']}")
    if not after["jellyfin"] or not after["emby"]:
        notes.append("Jellyfin or Emby markers not back")
    if second_job and p1.job_files(second["job_id"]):
        notes.append("the second reconcile re-ran files")
    result = "pass" if job["status"] == "completed" and not notes else "fail"
    return write_result(10, "Reconcile restores dropped markers on Plex, Jellyfin and Emby", result,
                        {"before": before, "after": after, "files": files, "jobs_since": [j["library_name"] for j in p1.jobs_since(t0)]}, notes)  # fmt: skip
```

(`DELETE /MediaPreviewBridge/Markers/{itemId}` is the phase-1 Jellyfin plugin route, `jellyfin-plugin/Api/MarkersController.cs`;
the Emby route is Task 4's.)

11. **Plex version drift (ledger L184)** — copy `synth/_staging/Synth Chapters (2021) - S01E03 - Plexonly.webm`
    (116 s, a different cut) into `synth/_plexonly/Synth Chapters (2021)/Season 01/`, Plex section refresh,
    `plex_wait_idle`; Plex's item for E03 now has two `Media`. A normal job on E03: row stays `markers_waiting` (versions don't agree) and our markers are still served
    (the known limit). `POST /api/markers/reconcile`: the job lists E03, and afterwards Plex serves none of our
    markers on the item and the row reason names the versions. Delete the plex-only copy (lab folder), refresh Plex,
    run a normal job: E03 is written again. Pass when all three states hold.

- [ ] **Step 8: Rows 12–20 (carry-overs, UI, regression, owner)**

12. **P3 steps 2–4** (task-8-review): on a Rick and Morty episode with Plex's own credits rows and no intro, publish
    an intro only (a single-file job with `detect.credits` off in settings for the run); `plex_served` shows both;
    `plex_db` shows the `[index]` order (`text`, then `time_offset`); force credits detection; served markers and
    `[index]` after. **P4**: on a synth episode with no `pv:intros` key, publish an intro, then publish its removal
    (turn intro detection off, forced job), run non-forced intro detection for its season, check whether Plex adds its
    own intro row. Record both answers in `phase2-results.md`; restore settings.
13. **L274 final flag** — `set_redetect("keep_plex")`; on Rick and Morty S01E01 run Plex's forced intro detection;
    after `plex_wait_idle`, Plex's credits rows keep `final` (`plex_served(...)[i]["final"]` true on the last credits
    row) and the next normal job's row doesn't call our own credits "Plex's" (`item_payload` → `servers[mlab-plex]`
    `plan_reason` doesn't name credits unless Plex's rows differ from ours). `set_redetect("restore")`.
14. **L263 JF 12.0 movie alternates** — refresh `mlab-jf12`'s Movies library holding `/media/synth-movies`; job on
    the movie folder; `jf_items("mlab-jf12")` lists both versions; `jf_segments` shows Outro at 100 s for both media
    source ids; `p1.play_and_find_skip` isn't needed (credits only). Pass when both alternates serve.
15. **Cassette replay** — `cd` repo root; `/home/data/.venv/bin/python -m pytest --no-cov --record-mode=none
    tests/test_servers_markers_vcr.py tests/test_servers_emby_markers_vcr.py -q` passes with the lab containers
    stopped (`docker stop mlab-plex mlab-jellyfin mlab-emby` for the run, then start them again).
16. **Season view in the real app** — Playwright on `http://127.0.0.1:18080/bif-viewer` (token from `ENV` via stdin as
    `p1` rows 13/14 do), search "Synth Audio" on mlab-plex, S01E01 → Intro & Credits → Whole season: 4 episodes, "4
    ready", the publish button reads "Publish 4 to 5 servers", every dot green; click Publish → a job named
    "Intro & Credits: Synth Audio (2022) · Season 01" appears and completes Up to date. Screenshot to `SHOTS`.
17. **Security** — without the token: `GET /api/markers/season?path=x`, `POST /api/markers/season/publish`,
    `GET /api/markers/sources/local`, `POST /api/markers/reconcile` → 401; with it:
    `GET /api/markers/season?path=/etc/passwd` → 400 and `path=/media/synth-audio/../../etc/passwd` → 400.
18. **Resources** — during a forced job on Rick and Morty S01 with a cleared fingerprint cache (new `markers.db`
    identity isn't needed: `force` re-reads evidence but fingerprints are cached, so first delete `fingerprints` rows
    for that season with `docker exec mlab-app sqlite3 /config/markers.db "DELETE FROM fingerprints WHERE file_id IN
    (SELECT id FROM files WHERE canonical_path LIKE '/media/tv/Rick and Morty (2013)/Season 01/%')"` run through
    `docker exec mlab-app python3 -c 'import sqlite3, sys; c = sqlite3.connect("/config/markers.db"); c.execute(sys.argv[1]); c.commit()' "<statement>"`
    — the image has no sqlite3 CLI; this is the lab app's own config volume): `ChromaprintSampler.peak` ≤ 2, `-threads 2` in argv, `p1.StatsSampler` peak CPU and RSS
    recorded next to phase-1 row 15.
19. **Phase-1 regression** — `./phase1_matrix.py run 1 2 3 4 5 6 7 8 9 10 13 14 16 17 18 19` on the same lab (row 11
    stays unit-only, row 12 is row 20 here). Pass when every row passes; row 5's forced credits detection still
    replaces ours until a job or reconcile runs (phase-1 expectation unchanged).
20. **Plex app (owner, ledger L276)** — ask the owner to open Synth Chapters S01E02 and Synth Audio S01E02 on the
    claimed lab Plex in any Plex app and confirm Skip Intro (0:17 and 0:45) and Skip Credits (1:40 on Synth
    Chapters). Record the answer; the row stays "needs owner" until then.

- [ ] **Step 9: Run the matrix**

```bash
cd /home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab
./phase2_matrix.py configure
./phase2_matrix.py run 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18
./phase1_matrix.py run 1 2 3 4 5 6 7 8 9 10 13 14 16 17 18 19
```

Any fail → `superpowers:systematic-debugging` on the owning task's code, fix there (with a unit test that fails
before the fix), rebuild the image, `app.sh recreate`, re-run the failed row and every row after it.

- [ ] **Step 10: `phase2-results.md`**

Section "Task 17 — phase-2 lab matrix (date, commit, image id)": one table row per matrix row (result, one-line
evidence: counts, times, served markers — no tokens, no `/data` paths), the Task 15 harness summary (both modes, the
gate), the lab reset used, and open items for the owner (row 20, rulings R1–R5 and gaps G3–G5 if still open).

- [ ] **Step 11: Commit**

Stage `synth_audio.sh`, `synth_chapters.sh`, `up.sh`, `phase2_matrix.py`, `phase2-results.md`, `evidence/README.md`
(never `env`, `results/`, `synth/`); Architecture Review; `test(markers): phase 2 lab matrix scripts and results`.

---
## Task 18: PR, image, close-out

`[sequential]` (after Task 17) — spec §11 (branch, `build-docker` label, side-by-side container on `plex` only after
the owner's OK, merge only when the owner says fully tested), §10.2 ("results pasted in the PR"), owner checkpoints 2,
3, 4, 5, ledger L132 (dual-ABI release dry run: owner's call), L199 (pre-existing `remote_prefix` bug: open an issue).

- [ ] **Step 1: Branch up to date and full suites**

```bash
cd /home/data/workspace/plex_generate_vid_previews
git fetch origin
git merge-base --is-ancestor origin/dev HEAD || git merge origin/dev   # resolve conflicts, keep both sides' intent
/home/data/.venv/bin/python -m pytest
/home/data/.venv/bin/python -m pytest -m e2e -n 8 --no-cov
/home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/markers_eval
```

Expected: all PASS; note the three counts and coverage. A merge commit goes through the Architecture Review agent like
any commit.

- [ ] **Step 2: PR body (REST — `gh pr edit` is broken here)**

Write the body to `$SCRATCH/pr241.md`: summary (what users get now: season audio intros, Emby, the 12-hourly check,
Season view), phase status (phase 1 and 2 done; 3–4 pending), how it works (link `spec.md`), screenshots (Tasks 13, 14,
17 rows 8 and 16), the lab table from `phase2-results.md` plus the phase-1 table, the harness table from
`evidence/eval/phase2-harness.md` (§10.2: both modes, the gate, online settings, credits 80 files, 205-set titles),
test counts from Step 1, known limitations (credits text phase 3; Emby credits only when they run to the end; season
audio needs the amd64 image; the Emby catalog listing pending; Plex's own detection still replaces ours until the next
job or check), how to try `pr-241`. Keep it a draft.

```bash
gh api -X PATCH repos/stevezau/media_preview_generator/pulls/241 -f body="$(cat "$SCRATCH/pr241.md")" \
  -f title="feat: Intro & Credits (skip markers) for Plex, Jellyfin and Emby"
```

End the body with the attribution lines from the session's system reminder.

- [ ] **Step 3: PR image**

```bash
gh api -X POST repos/stevezau/media_preview_generator/issues/241/labels -f "labels[]=build-docker"
gh run list --branch feat/markers-detection --limit 5
```

Wait for the Docker workflow; then:

```bash
docker pull ghcr.io/stevezau/media_preview_generator:pr-241
docker run --rm --entrypoint python3 ghcr.io/stevezau/media_preview_generator:pr-241 -c \
  "import media_preview_generator.markers.audio.season, media_preview_generator.markers.reconcile, numpy; print('ok')"
docker run --rm --entrypoint /usr/lib/jellyfin-ffmpeg/ffmpeg ghcr.io/stevezau/media_preview_generator:pr-241 \
  -hide_banner -muxers | grep -c chromaprint
```

Expected: `ok` and `1`. Record the digest. Then `MLAB_APP_IMAGE=ghcr.io/stevezau/media_preview_generator:pr-241
./app.sh recreate` and re-run `./phase2_matrix.py run 1 2 8` on it; paste the three results under "PR image check" in
`phase2-results.md`.

- [ ] **Step 4: Owner checkpoints (ask, don't assume)**

Ask the owner, one message, short:
1. Side-by-side `pr-241` on `plex` against one real show (checkpoint 2), now including season audio and, if the owner
   has an Emby server, the Emby plugin (manual DLL install). Only after yes: the phase-1 Task 20 Step 4 container
   recipe (own config dir `/config/plex-generate-previews-pr241`, port 127.0.0.1:18081, flags from
   `/home/data/scripts/containers/plex.sh` minus port/config mounts); Plex DB writes stay off until the owner enables
   them (checkpoint 5). Record what the owner saw in `phase2-results.md`.
2. Emby catalog: forum thread / developer id, and whether to submit the text in `emby-plugin/README.md` (checkpoint 4).
3. Plugin releases: a `workflow_dispatch` dry run of the Jellyfin dual-ABI and Emby release workflows on a scratch
   tag (ledger L132) — only on the owner's word "release".
4. Open the pre-existing `remote_prefix` issue (ledger L199)? Draft text: "`MediaServer.resolve_remote_path_to_item_id`
   ignores `remote_prefix` path-mapping rows (the UI saves `plex_prefix`), so hand-edited or legacy configs don't map
   paths for item lookups." Open it with `gh issue create` only after yes.
5. Row 20 (Plex app Skip Intro / Skip Credits on the lab Plex), if still open.
6. Any open gap from the plan's gap list the owner hasn't ruled on (R1–R5, G3–G5).

- [ ] **Step 5: Close-out**

After the owner's answers (or with them recorded as pending):
- spec §0 Status: phase 2 lab-proven, PR image digest, next step phase 3; §14 dated lines for anything the owner
  ruled in Step 4.
- `plan-roadmap.md`: tick the phase-2 bullets that shipped; leave any the owner deferred unticked with a reason.
- `.superpowers/sdd/plan-phase2/progress.md`: final line with counts, image digest, open owner items.
- Write `docs/design/intro-credits/plan-phase3.md` with `superpowers:writing-plans` against the code as it exists
  (roadmap phase 3 bullets; this plan's parked items marked "phase 3").
- Architecture Review; commit `docs(intro-credits): phase 2 close-out and phase 3 plan`; push.

Nothing merges into `dev`: the PR stays draft until the owner says the feature is fully tested.

---
## Parked items

Every "Parked" and "Accepted concerns" line of the phase-1 ledger (`.superpowers/sdd/plan-phase1/progress.md`, line
numbers as of commit 993f1c3), and the roadmap phase-2 clauses this plan doesn't build as written.

- L100 DecisionRow drops the proposed marker's `decided_by` — Not in phase 2 because only the phase-4 Adjust/Lock editor
  needs "who proposed this"; the Season view shows evidence chips from stored evidence instead.
- L117 path-derived "movie" confirmed against the server's kind — Not in phase 2 because phase 1 Task 11 built it
  (`server_kinds` table, `pipeline.py` `get_server_kind`/`set_server_kind`).
- L128 Jellyfin 10.11 re-serves the plugin's stored copy after a file replacement — Not in phase 2 because phase 1 fixed
  it (the plugin's stored file size guard and the re-POST on size mismatch, `publishers/jellyfin.py` `_file_size`).
  Emby gets the same guard in Task 4 (`Stale`) and Task 10.
- L132 dual-ABI plugin release workflow dry run — Not in phase 2 because releases happen only on the owner's word
  "release"; Task 18 Step 4 asks the owner, for the Jellyfin and the new Emby workflow together.
- L144 PROOF P3 steps 2–4, P4, `get_markers` VCR cassette from the claimed lab Plex — Task 5 (cassette) and Task 17
  row 12 (P3, P4).
- L148 Inspector "current" must include our own Jellyfin segments — Not in phase 2 because phase 1 built it
  (`read_server_markers(..., include_ours=True)` in `inspect.py`).
- L155 VCR cassettes for Jellyfin Bridge/MediaSegments and Emby chapters — Task 5 (Jellyfin) and Task 10 Step 8 (Emby).
- L165 anime "Ending" chapters not treated as credits, and two "End Credits" chapters picking the last — Task 15 reports
  both on the 80-file and 205-movie sets (`ending_titles`, `several_credits_chapters`); changing the chapter rules is
  Not in phase 2 because they are missed coverage or late-but-safe, and credits are phase 3's subject.
- L165 local detectors re-run while undecided — Task 3 (`LocalDetectorSpec.due` + versioned evidence) and Task 7
  (`season_audio_due` signature).
- L165 `respect_locks=False` only meaningful with phase-4 locks — Not in phase 2 because nothing in phase 2 creates a
  lock; the phase-4 editor does.
- L166 `ctx.priority` fixed at job start — Not in phase 2 because phase 1 made it a callable (`PipelineContext.priority:
  Callable[[], int]`).
- L184 a Plex version that's never decided leaves our markers on the item — Task 9 (`VERSIONS_CHANGED` from recorded
  item files) and Task 11 (reconcile read-back), lab-proven in Task 17 row 11.
- L199 `resolve_remote_path_to_item_id` ignores `remote_prefix` rows — Not in phase 2 because it is a pre-existing bug
  outside Intro & Credits; Task 18 Step 4 asks the owner to open the issue (draft text there).
- L200 evidence rows lack the chapter title label — Task 12 (`EvidenceRow.label`, `label` in both payloads).
- L200 Plex item lookup by server + item id resolves the first version only — Not in phase 2 because the Inspector
  search passes each version's `media_file`, which is looked up by path first; only a hand-typed item id hits it.
- L204 a forced run returns at the first pending local detector and skips later sources' refresh — Task 3 (refresh
  tracked per (file, source)).
- L226 MED `_focustrap` accepted risk (Edit dialog focus trap reached through a private Bootstrap field, with a
  regression test) — Not in phase 2 because Task 13 doesn't touch the confirmation dialog; the e2e test still guards it.
- L236 audit A LOW-1 per-episode vendor webhook follow-ups not season-grouped — Task 8 (`_season_folders`, join into a
  queued follow-up).
- L244 accepted F3: plugin uninstall 60 s race (LOW) — Not in phase 2 because it is unchanged by this plan and
  accepted by the owner; Emby install has no uninstall button.
- L244 accepted F3: Inspector rare rewrite case (LOW) — Not in phase 2 because it is accepted; Task 13 changes only the
  Emby note and evidence labels in `serverLines`/lanes.
- L263 Jellyfin 12.0 movie alternates not lab-tested — Task 17 row 14 (`Synth Movie (2023)` two versions).
- L265 accepted: verify jobs show retry labels — Not in phase 2 because the owner answered ("Checking again in N min",
  2026-09-14) and it shipped.
- L265 accepted: mixed `keep_plex` case waits for a decision change — Not in phase 2 because the per-type kept state
  (2073f3d, owner-approved 2026-09-14) superseded it.
- L265 accepted: +1 read per file per server per run — Not in phase 2 because it stays accepted; Task 11's bulk
  read-back (`shows_many`) makes the periodic check cheap, the per-run read stays.
- L265 accepted: Inspector reads at 1 s HTTP precision vs the job's exact rows — Not in phase 2 because the Inspector
  compares within `_SAME_TOLERANCE_MS` by design.
- L271 accepted: same-size in-place replacement not re-sent to Jellyfin — Not in phase 2 because the plugin's data is
  still valid for an identical-size file; Emby follows the same rule (Task 4 `IsStale`).
- L271 accepted: kept state is sticky — Not in phase 2 because it is the owner's ruling (released by "Use ours" or
  Plex dropping its rows).
- L271 accepted: our stray row beside Plex's of a kept type stays — Not in phase 2 because it is rare and accepted;
  Task 11's reconcile doesn't delete rows of a kept type either.
- L274 open lab check: Plex keeps the `final` flag on rebuilt credits after its forced intro detection — Task 17 row 13.
- L276 row 12 Plex app Skip Intro / Skip Credits (owner) — Task 17 row 20 and Task 18 Step 4.
- Roadmap phase 2 "`keep_plex` stores Plex's set as evidence instead" — Not in phase 2 because spec §14 (2026-09-14)
  replaced it: Plex's own markers are kept on the server and read as `server_markers` evidence like any server's;
  Task 16 marks the roadmap clause superseded.
- Roadmap phase 2 "reconcile … after each Intro & Credits job" — Not in phase 2 as a separate step because phase 1
  already reads back before "Up to date" and queues the delayed verify job; Task 11 adds only the periodic half (R5).
- Spec §13 item 8 (Jellyfin 12.0 catalog install of a dual-ABI manifest) and item 9 (TheIntroDB keyed limit) — Not in
  phase 2 because item 8 needs a published release (owner's word) and item 9 needs the owner's own TheIntroDB key.
