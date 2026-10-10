# Library Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Tools → Library health page that counts, per server and library, files still needing previews, loudness, intro and credits markers, lists them, starts existing jobs for them, and can ask Plex to show previews it isn't showing.

**Architecture:** A new read-only package `media_preview_generator/library_health/` with one counter per server type, a sqlite result store in CONFIG_DIR, and a single-flight background runner (Check now + nightly 03:00). A small API blueprint module and a page (template/JS/CSS) read the store and start existing job endpoints.

**Tech Stack:** Python 3.11+, Flask blueprint `api`, sqlite3, APScheduler 3 (existing `BackgroundScheduler`), loguru, vanilla JS + Bootstrap 5 (existing), pytest.

**Spec:** `docs/design/library-health/spec.md` — read §0–§1 before any task.

## Global Constraints

- Counting never writes to Plex, Emby, Jellyfin, markers.db, or media folders. The only write is Task 5's Plex re-read (`PUT /library/metadata/{id}/analyze`).
- Plex counts never read the media share (no `os.stat` of source files, no folder listing of media paths).
- Never use Plex `includeMarkers=1` on section listings.
- markers.db is opened only as `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`; never via `MarkerStore`.
- Plex DB access goes through `plex_database(settings, path_provider=lambda: PlexMarkerPublisher.db_path_for(cfg), label=cfg.name)`; only a `LocalPlexDb` result is used (a `RemotePlexDb` = agent mode = "unavailable"). Read with `db._database(read_only=True, deadline=time.monotonic() + 60)`.
- Logging via `from loguru import logger`; never log tokens. Type hints + Google docstrings on public functions. ruff line length 120.
- Copy rules (UI text): plain words, no internal names ("Plex's database", not "LocalPlexDb"). Feature names: "Previews", "Loudness", "Intro", "Credits". States: "to do", "not checked" (intro/credits), "nothing found", "not showing in Plex", "Off for this library", "Not used for films", "Plex only".
- Shared box: never run pytest with more than 8 workers (`-n 8` max). E2E: `-n 0` or `-n 8`.
- File-path jobs only when todo ≤ 1,000 (`PATH_JOB_LIMIT = 1000`), else library jobs.
- Nightly at 03:00 server-local time; no new settings.json keys.

## Review Focus

1. **A library whose Plex section id isn't in settings or was deleted** — skip silently (only libraries present in `cfg.libraries` are counted); test in Task 3.
2. **Plex parts whose path maps outside every path mapping** (`path_to_canonical_local` returns the input unchanged) — still counted, exclusion and markers.db lookups use the returned path; test in Task 3.
3. **Multi-version / multi-part items** — Plex counts parts; intro/credits are per metadata item but counted per part (each part's file shows the item's markers); the re-read de-duplicates item ids. Test in Tasks 3 and 5.
4. **Check started while a fix runs, or fix while a check runs** — check returns the running fix's state with 409 from the route; test in Tasks 5 and 6.
5. **Process restart or crash mid-check** — store keeps the last complete per-server rows; a half-written server never shows (transaction). Test in Task 2.

---

### Task 1: Models and scope

**Files:**
- Create: `media_preview_generator/library_health/__init__.py` (empty docstring module)
- Create: `media_preview_generator/library_health/models.py`
- Create: `media_preview_generator/library_health/scope.py`
- Test: `tests/library_health/__init__.py`, `tests/library_health/test_scope.py`

**Interfaces — Produces:**

```python
# models.py
class Feature(str, Enum):
    PREVIEWS = "previews"; LOUDNESS = "loudness"; INTRO = "intro"; CREDITS = "credits"

class CellState(str, Enum):
    COUNTED = "counted"; OFF = "off"; NOT_APPLICABLE = "not_applicable"; UNAVAILABLE = "unavailable"; ERROR = "error"

@dataclass
class Cell:
    state: CellState
    total: int = 0
    done: int = 0
    nothing_found: int = 0
    not_showing: int = 0          # Plex previews only
    reason: str = ""              # shown for OFF / NOT_APPLICABLE / UNAVAILABLE / ERROR
    @property
    def todo(self) -> int: return max(self.total - self.done - self.nothing_found, 0)
    def to_dict(self) -> dict: ...  # includes "todo"

@dataclass(frozen=True)
class TodoFile:
    feature: Feature
    path: str                     # canonical local path
    title: str
    item_id: str = ""             # server item id (Plex metadata item id for re-read)
    not_showing: bool = False     # Plex previews: BIF exists but Plex flag missing

@dataclass
class LibraryResult:
    library_id: str
    name: str
    kind: str | None
    total: int
    cells: dict[Feature, Cell]
    def to_dict(self) -> dict: ...

@dataclass
class ServerResult:
    server_id: str
    name: str
    type: str                     # "plex" | "emby" | "jellyfin"
    libraries: list[LibraryResult]
    todo: list[TodoFile]          # todo AND not_showing entries; not serialised by to_dict
    access: str = ""              # e.g. "Reads Plex's database directly" / "No access to Plex's database"
    error: str = ""               # whole-server failure message
    checked_at: float = 0.0
    duration_s: float = 0.0
    def to_dict(self) -> dict: ...

@dataclass
class CheckProgress:
    kind: str                     # "check" | "reread"
    started_at: float
    reason: str                   # "manual" | "nightly" | "after_reread"
    server_name: str = ""
    step: str = ""                # plain words, e.g. "Checking markers"
    done: int = 0
    total: int = 0
    cancelled: bool = False
    def to_dict(self) -> dict: ...
```

```python
# scope.py
@dataclass(frozen=True)
class LibraryScope:
    library_id: str
    name: str
    kind: str | None
    is_movie_library: bool
    features: dict[Feature, Cell | None]   # None = count it; a Cell = fixed state (OFF / NOT_APPLICABLE / UNAVAILABLE)

def library_scopes(cfg: ServerConfig) -> list[LibraryScope]: ...
def excluded(cfg: ServerConfig) -> Callable[[str], bool]: ...  # wraps config.paths.is_path_excluded(path, cfg.exclude_paths)
```

Rules for `library_scopes` (in order, per `lib in cfg.libraries`):
- Previews: `lib.enabled` False → `Cell(OFF, reason="Previews are off for this library")`.
- Loudness: non-Plex → `Cell(NOT_APPLICABLE, reason="Plex only")`. Plex: `s = load_server_loudness(cfg)`; `not s.enabled or not library_chosen(s, library_id=lib.id, kind=lib.kind)` → `Cell(OFF, reason="Loudness is off for this library")`.
- Markers: `m = load_server(cfg.markers, cfg.type.value)`; `not m.enabled or not library_allowed(m, library_id=lib.id, library_name=lib.name, kind=lib.kind)` → OFF for both INTRO and CREDITS ("Intro & credits are off for this library").
- Intro on a movie library (`lib.kind == "movie"`) → `Cell(NOT_APPLICABLE, reason="Films don't get intro markers")` (overrides OFF).
- `is_movie_library = lib.kind == "movie"`.

- [ ] **Step 1: Write failing tests** in `tests/library_health/test_scope.py` building `ServerConfig` objects directly (see `tests/conftest.py` helpers for ServerConfig construction; import `ServerConfig, ServerType, Library` from `media_preview_generator.servers.base`):
  - `test_previews_off_when_library_disabled` — `Library(id="1", name="Movies", enabled=False, kind="movie")` → previews cell OFF.
  - `test_loudness_not_applicable_on_jellyfin` — type JELLYFIN → loudness NOT_APPLICABLE, reason "Plex only".
  - `test_loudness_off_when_disabled_plex` — Plex, `loudness={"enabled": False, "library_ids": None}` → OFF.
  - `test_loudness_counted_when_library_chosen` — Plex, `loudness={"enabled": True, "library_ids": ["2"]}`, library id "2" kind "show" → loudness None.
  - `test_markers_default_skips_sports` — Plex, `markers={"enabled": True, "library_ids": None}`, library named "Sports" kind "show" → intro/credits OFF; "TV Shows" → None.
  - `test_intro_not_applicable_on_movie_library` — markers enabled, kind "movie" → intro NOT_APPLICABLE, credits None.
  - `test_excluded_uses_server_rules` — `exclude_paths=[{"value": "/data/skip", "type": "path"}]` → `excluded(cfg)("/data/skip/a.mkv")` True, `("/data/keep/a.mkv")` False.
- [ ] **Step 2:** Run `pytest --no-cov -n 0 tests/library_health/test_scope.py -v` → FAIL (module missing).
- [ ] **Step 3:** Implement `models.py` and `scope.py` exactly per the interfaces above.
- [ ] **Step 4:** Run the tests → PASS. Run `ruff check media_preview_generator/library_health tests/library_health && ruff format media_preview_generator/library_health tests/library_health`.
- [ ] **Step 5:** Stage (do not commit; the owner approves commits): `git add media_preview_generator/library_health tests/library_health`.

---

### Task 2: markers.db reader and result store

**Files:**
- Create: `media_preview_generator/library_health/markers_db.py`
- Create: `media_preview_generator/library_health/store.py`
- Test: `tests/library_health/test_markers_db.py`, `tests/library_health/test_store.py`

**Interfaces — Consumes:** Task 1 models. **Produces:**

```python
# markers_db.py
def nothing_found(markers_db_path: str) -> dict[str, set[Feature]]:
    """canonical_path -> {Feature.INTRO and/or Feature.CREDITS} whose decision status is no_evidence/needs_review.
    Returns {} when the file is missing or unreadable (logged at debug)."""
```
SQL: `SELECT f.canonical_path, d.type FROM files f JOIN decisions d ON d.file_id = f.id WHERE d.type IN ('intro','credits') AND d.status IN ('no_evidence','needs_review')`. markers.db path: `os.path.join(get_config_dir(), "markers.db")` — confirm against `markers/store.py` / where the app constructs `MarkerStore` (grep `MarkerStore(`) and use the same path helper.

```python
# store.py
class HealthStore:
    def __init__(self, db_path: str) -> None: ...      # creates tables; WAL; check_same_thread=False + a threading.Lock
    def replace_server(self, result: ServerResult) -> None: ...   # one transaction: delete this server's rows, insert new
    def remove_missing_servers(self, keep_ids: set[str]) -> None: ...  # drop servers no longer configured
    def load(self) -> list[ServerResult]: ...           # todo lists NOT loaded (empty list)
    def list_todo(self, server_id: str, library_id: str, feature: Feature, *, q: str = "",
                  offset: int = 0, limit: int = 100, not_showing: bool = False) -> tuple[int, list[TodoFile]]: ...
    def reread_items(self, server_id: str) -> list[tuple[str, str]]: ...   # distinct (item_id, library_id) with not_showing
def default_store() -> HealthStore: ...                 # CONFIG_DIR/library_health.db, cached module singleton
```
Tables: `servers(server_id PK, json TEXT, checked_at REAL)`; `todo(server_id, library_id, feature, path, title, item_id, not_showing INTEGER)` with index `(server_id, library_id, feature, path)`. `servers.json` holds `ServerResult.to_dict()` (libraries+cells). `list_todo` filters `path LIKE ? ESCAPE '\'` (case-insensitive via `LOWER`) when `q`, orders by path, clamps `limit` to 1..500.

- [ ] **Step 1: Failing tests:**
  - markers_db: build a temp markers.db with the real schema by instantiating `MarkerStore(tmp/"markers.db")` and inserting via its own API if simple, otherwise raw SQL matching `markers/store.py` CREATE TABLE statements for `files` and `decisions`; rows: path A intro no_evidence, path A credits decided, path B credits needs_review → `{A: {INTRO}, B: {CREDITS}}`. `test_missing_file_returns_empty`.
  - store: `test_replace_server_roundtrip` (load returns cells with todo); `test_replace_is_atomic_on_error` — monkeypatch the insert to raise after the delete, assert previous rows still load (use a transaction `with conn:`); `test_list_todo_filter_and_paging` (250 rows, q filter, offset/limit, limit clamp 9999→500); `test_reread_items_distinct` (two parts same item id → one entry); `test_remove_missing_servers`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS; ruff.
- [ ] **Step 5:** Stage.

---

### Task 3: Plex counters

**Files:**
- Create: `media_preview_generator/library_health/plex.py`
- Test: `tests/library_health/test_plex.py`, fixture builder `tests/library_health/plex_fixture.py`

**Interfaces — Consumes:** Tasks 1–2. **Produces:**

```python
def count_plex(cfg: ServerConfig, server: MediaServer, *, nothing_found: dict[str, set[Feature]],
               cancel_check: Callable[[], bool], progress: Callable[[str, int, int], None]) -> ServerResult: ...
```
Branch: `db = plex_database(load_server(cfg.markers, "plex"), path_provider=lambda: PlexMarkerPublisher.db_path_for(cfg), label=cfg.name or "")`. If `isinstance(db, LocalPlexDb)` and `cfg.output.get("plex_config_folder")` is set → `_count_from_db`, `access="Reads Plex's database directly"`; else `_count_from_api`, `access="No access to Plex's database"`, loudness/intro/credits cells `UNAVAILABLE` with reason "Needs access to Plex's database" (unless scope already made them OFF/NOT_APPLICABLE).

`_count_from_db` — one read-only connection, three queries, then close before stats:

```sql
-- parts (sections = the counted library ids)
SELECT mi.library_section_id, mi.id, mi.metadata_type, mp.id, mp.file, mp.hash, mp.extra_data,
       mi.title, parent.title AS season, grand.title AS show, mi."index", parent."index"
FROM metadata_items mi
JOIN media_items mdi ON mdi.metadata_item_id = mi.id AND mdi.deleted_at IS NULL
JOIN media_parts mp ON mp.media_item_id = mdi.id AND mp.deleted_at IS NULL
LEFT JOIN metadata_items parent ON parent.id = mi.parent_id
LEFT JOIN metadata_items grand ON grand.id = parent.parent_id
WHERE mi.library_section_id IN (...) AND mi.metadata_type IN (1, 4) AND mi.deleted_at IS NULL
  AND COALESCE(mdi.proxy_type, 0) = 0
-- audio stream count per part (same section filter, joined like the parts query)
SELECT ms.media_part_id, COUNT(*) FROM media_streams ms JOIN media_parts mp ON mp.id = ms.media_part_id
JOIN media_items mdi ON mdi.id = mp.media_item_id JOIN metadata_items mi ON mi.id = mdi.metadata_item_id
WHERE ms.stream_type_id = 2 AND mi.library_section_id IN (...) GROUP BY ms.media_part_id
-- only the audio streams that may carry loudness (pre-filter keeps memory small: ~4k rows on sflix, not ~300k)
SELECT ms.media_part_id, ms.extra_data FROM media_streams ms JOIN media_parts mp ON mp.id = ms.media_part_id
JOIN media_items mdi ON mdi.id = mp.media_item_id JOIN metadata_items mi ON mi.id = mdi.metadata_item_id
WHERE ms.stream_type_id = 2 AND ms.extra_data LIKE '%ln:%' AND mi.library_section_id IN (...)
-- markers
SELECT tg.metadata_item_id, tg.text FROM taggings tg JOIN tags t ON t.id = tg.tag_id
WHERE t.tag_type = 12 AND tg.text IN ('intro', 'credits')
```
Loudness per part: no audio rows → part excluded from loudness total; done iff the number of its pre-filtered rows passing `has_analysis(extra_data)` (catch `PublishError` → not passing) equals its audio stream count. Library section id must equal `Library.id` (string compare `str(section_id)`).

Per part: `path = path_to_canonical_local(mp.file, cfg.path_mappings)`; skip when `excluded(cfg)(path)`. Title: episode → `f"{show} – S{season_index:02d}E{index:02d} – {title}"`, movie → `title`. Previews: `bif = PlexBundleAdapter.bundle_bif_path(folder, hash)` when hash; done iff `os.stat(bif).st_size > 0` (OSError → todo); `not_showing` iff done and `'"mi:indexes":"sd"'` not in extra_data — parse with `json.loads` and check `data.get("mi:indexes") == "sd"` (fall back to the substring check on JSON errors). BIF stats run after the connection closes, in a `ThreadPoolExecutor(8)` (local disk), checking `cancel_check()` every 2,000 parts and calling `progress("Checking previews", n, total)`. Intro/credits: done iff the item has the tag; else `nothing_found.get(path)` contains the feature → nothing_found; else todo.

`_count_from_api` — for each previews-counted library: `server._connect().query(f"/library/sections/{lib.id}/all?type={1 if movie else 4}")` (find the existing helper on `PlexServer` that returns parsed XML; plexapi `query` returns an ElementTree element). Per `Video/Media/Part`: total += 1, done if `Part.get("indexes") == "sd"`, todo entry with `path_to_canonical_local(Part.get("file"))`.

Errors: any exception inside one library → that library's counted cells become `Cell(ERROR, reason=str(exc)[:200])`, logged with `logger.warning("Library health: could not count {} on {}: {}", lib.name, cfg.name, exc)`. A `PublishError` from `_database` (locked/misconfigured/schema) → every counted cell ERROR with its message.

- [ ] **Step 1:** Write `plex_fixture.py`: `build_plex_db(path, rows)` creating the minimal tables/columns used above (`metadata_items(id, library_section_id, metadata_type, parent_id, title, "index", deleted_at)`, `media_items(id, metadata_item_id, deleted_at, proxy_type)`, `media_parts(id, media_item_id, file, hash, extra_data, deleted_at)`, `media_streams(id, media_part_id, stream_type_id, extra_data)`, `tags(id, tag, tag_type)`, `taggings(id, metadata_item_id, tag_id, text)`), and `write_bif(plex_folder, hash, size=10)`. Because `LocalPlexDb._database` runs `_check_schema`, tests patch `count_plex` to use a fake db: inject via a module-level seam `_open_db(cfg) -> LocalPlexDb | None` that tests monkeypatch to return an object whose `_database(read_only=True, deadline=...)` yields `sqlite3.connect(fixture_path)`.
- [ ] **Step 2: Failing tests** (`tests/library_health/test_plex.py`):
  - `test_counts_previews_loudness_markers` — movie section "1": 3 parts: A (BIF + flag + full loudness + credits tag), B (BIF no flag, no loudness), C (no hash). Expect previews total 3 done 2 not_showing 1 todo 1; loudness total 3 done 1; intro NOT_APPLICABLE; credits done 1 todo 2. For full loudness use extra_data produced by the app's own encoder: build the `ln:*` dict with `loudness.analyze.ln_fields` + `VERSION_FIELD: ANALYSIS_VERSION` and `encode_extra_data` (same helpers `loudness/plex_db.py` imports), so the test follows the real rule.
  - `test_incomplete_native_loudness_is_todo` — `ln:loudness` only → todo, no exception.
  - `test_part_without_audio_excluded_from_loudness_total`.
  - `test_nothing_found_from_markers_db` — episode without intro tag, `nothing_found={path: {INTRO}}` → intro nothing_found 1, todo 0.
  - `test_excluded_paths_left_out`.
  - `test_library_not_in_settings_ignored` (Review Focus 1) — a part in section "99" not in cfg.libraries → not counted.
  - `test_unmapped_path_still_counted` (Review Focus 2).
  - `test_multi_part_item_counts_each_part_markers_shared` (Review Focus 3).
  - `test_agent_mode_uses_api_and_marks_unavailable` — monkeypatch `_open_db` to return None and stub `server._connect().query` to return an XML element with two Parts (one `indexes="sd"`).
  - `test_db_error_marks_cells_error` — `_database` raises `PublishError("locked")` → cells ERROR, reason contains "locked".
  - `test_cancel_stops_early` — cancel_check returns True → raises `CheckCancelled` (define in models.py: `class CheckCancelled(Exception)`).
- [ ] **Step 3:** Run → FAIL. **Step 4:** Implement. **Step 5:** Run → PASS; ruff; stage.

---

### Task 4: Emby and Jellyfin counters

**Files:**
- Create: `media_preview_generator/library_health/embyish.py`
- Test: `tests/library_health/test_embyish.py`

**Interfaces — Produces:**

```python
def count_embyish(cfg: ServerConfig, server: MediaServer, *, nothing_found: dict[str, set[Feature]],
                  cancel_check: Callable[[], bool], progress: Callable[[str, int, int], None]) -> ServerResult: ...
```

Item listing: own paged loop with `server._request("GET", "/Items", params={...})`, same paging as `servers/_embyish.py:list_items` (`StartIndex`, `Limit` = that module's `_LIST_ITEMS_PAGE_SIZE`, stop when a page is short or `StartIndex >= TotalRecordCount`), `ParentId=lib.id`, `IncludeItemTypes="Movie,Episode"`, `Recursive="true"`, `Fields="Path,Chapters"` (Emby) or `"Path,Trickplay"` (Jellyfin). Path → `path_to_canonical_local(item["Path"], cfg.path_mappings)`; skip items without Path or excluded. Title: `f"{SeriesName} – S{ParentIndexNumber:02d}E{IndexNumber:02d} – {Name}"` for episodes when present, else `Name`.

- Jellyfin previews done iff `item.get("Trickplay")` is a non-empty dict. Jellyfin markers (when not OFF/N/A): `server.get_media_segments(item_id)` — use the existing reader shown at `servers/jellyfin.py:~600` (find its public name; it returns `[{Type, StartTicks, EndTicks}]` or None). `None` (error) → count as todo and remember an error count; types `Intro` → intro, `Outro` → credits. Run with `ThreadPoolExecutor(4)`; progress("Checking intro & credits", n, total); `cancel_check()` every 50 items.
- Emby markers: from `item["Chapters"]` `MarkerType` (`IntroStart` → intro, `CreditsStart` → credits).
- Emby previews: `EmbyBifAdapter.sidecar_path(path, width=int(cfg.output.get("width") or 320), frame_interval=resolve_frame_interval(cfg.output))` (import `resolve_frame_interval` from where `processing/multi_server.py` imports it). Group items by parent folder; for each folder `os.scandir` once (single thread) into a name set; done iff sidecar name in set and the entry's size > 0 (`entry.stat().st_size`, only for matching names). OSError on a folder → its items are todo. progress("Checking previews", folders_done, folders_total); cancel every folder.
- Nothing found / todo as in Task 3. Loudness cell is NOT_APPLICABLE from scope.
- Errors per library → ERROR cells (as Task 3). If more than 10% of Jellyfin segment reads failed, mark intro/credits ERROR "Jellyfin didn't answer for N files".

- [ ] **Step 1: Failing tests** with a stub server object exposing `_request(method, path, params=None)` returning objects with `.status_code` and `.json()`, and `get_media_segments(item_id)`:
  - `test_jellyfin_previews_from_trickplay_field` (2 items, one Trickplay) → done 1 todo 1.
  - `test_jellyfin_segments_intro_outro` + `test_jellyfin_segment_errors_threshold`.
  - `test_emby_markers_from_chapters`.
  - `test_emby_previews_from_sidecar_files` — tmp media dir with `Film (2000).mkv` and `Film (2000)-320-10.bif` (non-empty) plus a second film without sidecar; zero-byte sidecar counts todo.
  - `test_paging_reads_all_pages` — 3 pages via StartIndex.
  - `test_unreadable_folder_counts_todo`.
- [ ] **Step 2–5:** FAIL → implement → PASS → ruff → stage.

---

### Task 5: Runner (check, Plex re-read, cancel, nightly)

**Files:**
- Create: `media_preview_generator/library_health/runner.py`
- Test: `tests/library_health/test_runner.py`

**Interfaces — Produces:**

```python
class HealthRunner:
    def __init__(self, store: HealthStore, *, registry_factory: Callable[[], ServerRegistry | None],
                 markers_db_path: Callable[[], str], sleep: Callable[[float], None] = time.sleep) -> None: ...
    def start_check(self, reason: str = "manual") -> tuple[bool, CheckProgress]: ...   # (started, progress)
    def start_reread(self, server_id: str) -> tuple[bool, CheckProgress | str]: ...    # False + message when busy/invalid
    def cancel(self) -> bool: ...
    def progress(self) -> CheckProgress | None: ...
    def last_error(self) -> str: ...
    def wait(self, timeout: float | None = None) -> None: ...   # tests
def get_runner() -> HealthRunner: ...      # module singleton wired to default_store(), settings_manager registry, markers.db path
def nightly_check() -> None: ...           # APScheduler entry: get_runner().start_check("nightly")
REREAD_INTERVAL_S = 0.5
```

- Single flight: one `threading.Lock` guards `_current: CheckProgress | None`; work runs in a daemon thread named `library-health`. `start_*` while busy returns `(False, current)` / `(False, "A check is already running")`.
- Check: build registry via `registry_factory()`; None → `last_error = "Could not read your media servers"`, stop. `nothing_found(markers_db_path())` once. For each enabled server config (`registry` configs; find the accessor that yields `ServerConfig` + live server, e.g. `registry.configs()`/`registry.get(id)` — read `servers/registry.py`), call `count_plex` / `count_embyish` (`ServerType.PLEX` / EMBY / JELLYFIN); on success `store.replace_server(result)`; on exception store nothing for that server and keep `result.error` in an in-memory `_server_errors[server_id]` that `GET` returns. After all servers `store.remove_missing_servers({configured ids})`. `CheckCancelled` → stop, keep stored results.
- Re-read: requires the server to be Plex and present; items = `store.reread_items(server_id)`; empty → message "Nothing to re-read". For each distinct item id: `server.refresh_preview_metadata(canonical_path="", item_id=item_id)` is NOT used (it may trigger a scan when id missing) — call the analyze PUT directly: `plex = server._connect(); plex.query(f"/library/metadata/{item_id}/analyze", method=plex._session.put)`, wrapped in try/except (log + count failures), `sleep(REREAD_INTERVAL_S)` between calls, `cancel` checked each item, progress `("Asking Plex to re-read", i, n)`. When done (not cancelled) run a check limited to that server (`_check_servers([server_id], reason="after_reread")`) in the same thread so `_current` stays set until finished.
- Every thread body is wrapped in `try/except Exception` that logs `logger.exception` and sets `last_error`; `_current` is always cleared in `finally`.

- [ ] **Step 1: Failing tests** (fake registry with two servers, monkeypatched `count_plex`/`count_embyish`):
  - `test_single_flight` — second `start_check` while first blocked on an Event returns `(False, progress)`.
  - `test_failed_server_keeps_previous_rows` — server 2 raises → store still has old server-2 rows, server 1 replaced.
  - `test_cancel_stops_and_clears_progress`.
  - `test_reread_dedups_paces_and_rechecks` — store has 3 not_showing rows for 2 item ids → 2 PUTs, `sleep` called with 0.5 between, then `count_plex` called once for that server.
  - `test_reread_rejected_while_check_runs` (Review Focus 4).
  - `test_reread_rejects_non_plex_server`.
  - `test_registry_failure_sets_last_error`.
- [ ] **Step 2–5:** FAIL → implement → PASS → ruff → stage.

---

### Task 6: API routes and nightly schedule

**Files:**
- Create: `media_preview_generator/web/routes/api_library_health.py`
- Modify: `media_preview_generator/web/routes/__init__.py` (add the module to the import list at :18-41)
- Modify: `media_preview_generator/web/scheduler.py` (register the internal nightly job next to `_QUIET_HOURS_RECHECK` at :1217-1223)
- Test: `tests/test_api_library_health.py`, extend the scheduler tests file that covers `__qh_recheck` (grep `__qh_recheck` in tests/)

**Interfaces — Consumes:** Task 5 `get_runner()`, Task 2 `default_store()`.

Routes (all `@api_token_required`, blueprint `api`):
- `GET /api/library-health` → `{"servers": [s.to_dict() | {"error": runner_error_for(s.server_id)}], "running": progress|None, "last_error": str, "next_nightly_at": iso|None}`. `next_nightly_at` from the scheduler job's `next_run_time` (None if the scheduler isn't running).
- `POST /api/library-health/check` → 202 `{"started": bool, "progress": ...}`.
- `POST /api/library-health/cancel` → 200 `{"cancelled": bool}`.
- `GET /api/library-health/files` — params validated: `server_id`, `library_id` required strings; `feature` in Feature values else 400; `q` ≤ 200 chars; `offset` ≥ 0; `limit` 1..500 (default 100); `not_showing` "1"/"0" → `{"total", "files": [{"path","title"}]}`.
- `POST /api/library-health/plex-reread` JSON `{"server_id"}` → 202 started / 409 `{"error": message}`.

Scheduler: in the same place `_QUIET_HOURS_RECHECK` is added, add
`self._scheduler.add_job(nightly_check, CronTrigger(hour=3, minute=0), id="__library_health_nightly", replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=3600)` (import inside the method to avoid import cycles). Make sure user-schedule listing code skips ids starting with `__` (check how `__qh_recheck` is hidden; follow the same rule).

- [ ] **Step 1: Failing tests** using the `client`/`auth_headers` fixtures from `tests/conftest.py`, with `get_runner` monkeypatched to a fake: auth required (401 without headers) for all 5 routes; GET shape; files 400 on bad feature; limit clamp; reread 409 passthrough; check 202. Scheduler test: after `start()`, a job with id `__library_health_nightly` exists with a CronTrigger at 03:00 and isn't returned by the user-schedule list API.
- [ ] **Step 2–5:** FAIL → implement → PASS → ruff → stage.

---

### Task 7: Page, nav, JS, CSS

**Files:**
- Modify: `media_preview_generator/web/routes/pages.py` (add `@main.route("/library-health")` `library_health()` with `@login_required`, rendering `library_health.html`, next to `inspector()` at :125-132)
- Modify: `media_preview_generator/web/templates/base.html` (Tools: add `menu_item(url_for('main.library_health'), 'clipboard2-pulse', 'Library health', "What's done and what's left in each library", active=request.endpoint == 'main.library_health')` right after Inspector at :154; add `'main.library_health'` to the aria-current tuple at :147-148)
- Create: `media_preview_generator/web/templates/library_health.html`
- Create: `media_preview_generator/web/static/js/library_health.js`
- Create: `media_preview_generator/web/static/css/pages/library_health.css`
- Modify: `tests/test_navbar_aria_current.py`, `tests/e2e/test_navbar_menus.py` (TOOLS spec :62-65 gains the item)
- Test: `tests/test_routes.py` (page renders 200 when authed, redirects when not), `tests/e2e/test_library_health_page.py`

Page structure (match the mockup; use the app's existing tokens in `style.css`: `--t-previews`, `--t-loud`, `--t-intro`, `--ok`, `--warn`, `--bad`, `--run`, `--line`, `--inset`, `--radius-card`; Bootstrap Icons `bi-*`; info icons as `<button class="info-icon" data-bs-toggle="tooltip" title="...">` like `servers.js:1949`, initialised with `new bootstrap.Tooltip(el)` after each render):
- Header: title "Library health", subtitle "What's done and what's left in each library. Checking only reads; it never changes your servers or files.", right side: "Check now" button (disabled + spinner text "Checking…" while running; becomes "Cancel" link next to progress), "Last checked <relative time> · took N s" from the newest `checked_at`, "Checks every night at 3:00 AM" (from `next_nightly_at`; hidden when null).
- Progress strip while running: "<server_name>: <step> · <done> of <total>" + progress bar; poll `GET /api/library-health` every 2 s while `running`, else every 60 s; stop polling when the tab is hidden (`visibilitychange`).
- Attention strip (only when any Plex server has `not_showing > 0`): "N previews are made but Plex isn't showing them" + per-library counts + "Review & fix" → Bootstrap modal: text from spec §1.8, counts per library, buttons Cancel / Start → `POST /api/library-health/plex-reread`. One strip per Plex server when several.
- Legend: has it / checked, nothing found / still to do.
- One card per server: logo letter + name + "N files · M libraries" + access chip + error chip; table (inside `.table-responsive`) with columns Library | Previews | Loudness | Intro | Credits. Cell rendering per state (spec §1.2): COUNTED → bold todo as a button-link ("8,517 to do" / intro+credits "69,277 not checked"), or "✓ All made" / "✓ All checked" when todo 0; stacked bar (done solid in feature color, nothing_found hatched); note line "1,544 of 10,061 done · 15%" or "46,383 have an intro · 2,101 nothing found"; Plex previews add a warning note "390 not showing in Plex" (clickable → file list with `not_showing=1`). OFF → muted chip with reason + "Change in Servers" link to `/servers`. NOT_APPLICABLE → faint text with reason. UNAVAILABLE → faint text + reason. ERROR → red text with reason. Empty state (no results yet): card "Not checked yet" + "Check now".
- File list modal (Bootstrap `modal-lg`): title "<Library> · <Feature>", count chip, filter input (debounced 300 ms) → `GET /api/library-health/files`, 100 per page with "Show more", each row: file name + parent folder (muted), link "Open in Inspector" → `/inspector?bif=<encodeURIComponent(path)>` (the Inspector reads `bif` on load, `inspector.js:3396`). Footer button "Start <feature> job for these N files" (or "for <Library>" when N > 1000) → confirm text in the modal footer ("Start a loudness job for 436 files in Sports?" + Start/Back) → POST to the existing endpoint per spec §1.7 (`/api/jobs/manual` `{file_paths, server_id}`; `/api/jobs` `{libraries:[{server_id, library_id}]}`; `/api/loudness/jobs` / `/api/markers/jobs` `{file_paths}` or `{libraries}`). On 2xx show a success toast "Job started — see the Dashboard" (use the existing toast helper in `app.js`; grep `showToast`), on error show the API's `error`. Paths list for ≤1,000 comes from paging `files` with limit 500 until `total` is reached.
- Escape every server-provided string with the global `escapeHtml` (`app.js:231`). All numbers via `toLocaleString()`.

- [ ] **Step 1: Failing tests:** route test (200 authed, contains "Library health"; 302/401 unauthed); nav tests updated to expect the new item and aria-current on `/library-health`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement page, JS, CSS, nav.
- [ ] **Step 4: E2E** `tests/e2e/test_library_health_page.py` with `authed_page`: monkeypatch nothing server-side; seed `library_health.db` in the e2e app's CONFIG_DIR (see how e2e conftest exposes it) with one Plex server result (counted cells, OFF cell, N/A cell, not_showing 3) and 3 todo rows; assert: card renders, "3 not showing in Plex" visible, clicking a todo number opens the file list with 3 rows, Inspector links have `?bif=`, "Review & fix" opens the dialog. Run `pytest -m e2e -n 0 --no-cov tests/e2e/test_library_health_page.py tests/e2e/test_navbar_menus.py`.
- [ ] **Step 5:** Run unit tests → PASS; ruff; stage.

---

### Task 8: Docs

**Files:**
- Modify: `docs/guides.md` (Tools list at :74 gains "Library health"; new section "Library health" after the Inspector section: what each column means, "to do" vs "not checked" vs "nothing found", the nightly 3:00 AM check, why Emby previews and Jellyfin markers can take longer, the "not showing in Plex" fix)
- Regenerate: `docs/llms-full.txt` via `python scripts/generate_llms_full.py`

- [ ] **Step 1:** Edit docs in plain words (follow `.claude/rules/docs.md`; keep it lean).
- [ ] **Step 2:** `python scripts/generate_llms_full.py && python scripts/generate_llms_full.py --check` → exit 0.
- [ ] **Step 3:** `pytest --no-cov -n 0 tests/test_docs_site.py` → PASS. Stage.

---

### Task 9: Verification (controller)

- [ ] Full suite once: `pytest -n 8` (verifier agent) → PASS, coverage ≥ floor.
- [ ] `mypy` the new package the way CI does (grep the CI workflow for the mypy command) → clean.
- [ ] Architecture Review agent on the diff (concurrency + Plex writes) → no HIGH.
- [ ] Lab proof: build image, run a throwaway `mlab-health-<date>` app container wired to mlab-plex/mlab-emby/mlab-jellyfin (copy `mlab_app_config` settings into a new volume), open the page via Playwright, screenshot, run Check now, confirm numbers against `docs/design/library-health/probes/` lab outputs, and run the re-read on the lab's remaining unflagged item (1378).
- [ ] sflix read-only cross-check: run `count_plex` from the built image in a throwaway container on plex with the same mounts as the live app container (`/plex`, `/config` copy of settings.json, media mounts) and only the read-only counter invoked, and compare to the probe numbers (movies 10,061 / TV 117,761 / sports 1,377; not_showing 390 / 6,600 / 24).
