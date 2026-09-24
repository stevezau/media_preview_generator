"""Files missing from disk: marked in markers.db (never deleted), and only while their disk root is mounted."""

import errno
import os
import shutil
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers import job_runner, missing, pipeline
from media_preview_generator.markers.audio import fingerprint as fpmod
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.locks import FILE_RUN_LOCKS
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.pipeline import PipelineContext, check_item
from media_preview_generator.markers.probe import ProbeError
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from tests.markers.fakes import FakeRegistry

INTRO = Marker(MarkerType.INTRO, 10_000, 40_000, ("chapters",))
LOCK = Marker(MarkerType.CREDITS, 1_400_000, 1_500_000, ("user",), locked=True)
MISSING_4 = "4 files are missing from disk; they're hidden from Needs review until they come back"


def _config(library_root: str, *, mapping: tuple[str, str] | None = None, enabled: bool = True) -> ServerConfig:
    return ServerConfig(
        id="plex-1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=enabled,
        url="http://plex",
        auth={},
        libraries=[Library("1", "TV Shows", (library_root,))],
        path_mappings=[{"remote_prefix": mapping[0], "local_prefix": mapping[1]}] if mapping else [],
        markers={
            "enabled": True,
            "library_ids": None,
            "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"},
        },
    )


def _episode(folder, name: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * 10)
    return str(path)


def _known(store: MarkerStore, path: str, status: DecisionStatus = DecisionStatus.NEEDS_REVIEW):
    """A file markers.db knows, with its identity as it is on disk and an intro in ``status``."""
    st = os.stat(path)
    rec = store.upsert_file(
        FileIdentity(path, st.st_size, st.st_mtime_ns),
        duration_ms=1_500_000,
        season_key=os.path.dirname(path),
        is_movie=False,
    )
    shown = (
        {"marker": INTRO, "proposed": None} if status is DecisionStatus.DECIDED else {"marker": None, "proposed": INTRO}
    )
    store.save_decisions(rec.id, {MarkerType.INTRO: TypeDecision(MarkerType.INTRO, status, reason="x", **shown)},
                         settings_fingerprint="s")  # fmt: skip
    return store.get_file(path)


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "config" / "markers.db"))
    yield s
    s.close()


@pytest.fixture
def library(tmp_path, store):
    """A TV library mapped from Plex's /data/tv to <tmp>/mnt/tv: one show kept, one Sonarr deleted whole."""
    mount = tmp_path / "mnt"
    tv = mount / "tv"
    kept = _episode(tv / "Kept Show" / "Season 01", "Kept Show - S01E01.mkv")
    deleted = [
        _episode(tv / "Deleted Show" / f"Season 0{s}", f"Deleted Show - S0{s}E0{e}.mkv") for s in (1, 2) for e in (1, 2)
    ]
    for path in [kept, *deleted]:
        _known(store, path)
    store.lock_marker(store.get_file(deleted[0]).id, LOCK)
    configs = [_config("/data/tv", mapping=("/data", str(mount)))]
    return SimpleNamespace(mount=mount, tv=tv, kept=kept, deleted=deleted, configs=configs)


def _delete_series(library) -> None:
    shutil.rmtree(library.tv / "Deleted Show")


def _marked(store: MarkerStore, paths) -> list[bool]:
    return [store.get_file(path).missing_since is not None for path in paths]


def _ctx(store, configs) -> PipelineContext:
    return PipelineContext(
        registry=FakeRegistry({"plex-1": configs[0]}),
        config=MagicMock(),
        settings=load_global(validate_global({"sources": [{"id": "theintrodb", "enabled": True}]}, None)[0]),
        store=store,
        priority=lambda: 2,
        ffprobe="ffprobe",
    )


class TestDiskRoots:
    def test_a_file_lies_under_its_library_folder_and_the_mapping_folder_it_came_from(self, library):
        assert missing.disk_roots(library.kept, library.configs) == (str(library.mount), str(library.tv))

    def test_a_library_without_a_mapping_is_its_own_root(self, tmp_path):
        path = str(tmp_path / "tv" / "Show" / "Season 01" / "e1.mkv")
        assert missing.disk_roots(path, [_config(str(tmp_path / "tv"))]) == (str(tmp_path / "tv"),)

    @pytest.mark.parametrize(
        "configs",
        [
            lambda tmp: [_config(str(tmp / "movies"))],  # another library
            lambda tmp: [_config(str(tmp / "tv"), enabled=False)],  # a server turned off
            lambda tmp: [_config("/")],  # "/" holds entries whatever is mounted
            lambda tmp: [],
        ],
        ids=["other-library", "server-off", "bare-root", "no-servers"],
    )
    def test_no_root_for_a_file_no_enabled_library_holds(self, tmp_path, configs):
        assert missing.disk_roots(str(tmp_path / "tv" / "Show" / "e1.mkv"), configs(tmp_path)) == ()


class TestSweep:
    def test_a_series_deleted_whole_is_marked_and_hidden_from_needs_review_with_nothing_deleted(
        self, store, library, loguru_caplog
    ):
        _delete_series(library)
        before = {path: store.get_file(path) for path in library.deleted}

        assert missing.sweep_missing_files(store, library.configs) == 4

        assert _marked(store, [library.kept, *library.deleted]) == [False, True, True, True, True]
        assert store.files_in_review() == [library.kept]
        assert MISSING_4 in loguru_caplog.text
        for path, rec in before.items():  # the rows, their decisions and the user's lock all stay
            assert store.get_file(path).id == rec.id and store.get_decisions(rec.id)
        assert store.get_locked(before[library.deleted[0]].id) == {MarkerType.CREDITS: LOCK}
        assert os.path.exists(library.kept)  # only markers.db changes: the media are never touched

    def test_a_file_replaced_by_an_upgrade_marks_the_old_name_only(self, store, library):
        old = library.kept
        new = old.replace("S01E01.mkv", "S01E01 - Bluray-1080p.mkv")
        os.rename(old, new)
        _known(store, new, DecisionStatus.DECIDED)

        assert missing.sweep_missing_files(store, library.configs) == 1

        assert _marked(store, [old, new]) == [True, False]

    @pytest.mark.parametrize(
        "disk",
        ["library-unmounted", "library-mount-empty", "mapping-root-missing", "nested-mount-gone", "no-root"],
    )
    def test_a_disk_that_isnt_there_marks_nothing(self, store, library, tmp_path, disk):
        if disk == "library-unmounted":
            shutil.rmtree(library.tv)  # the mount point itself is gone
        elif disk == "library-mount-empty":
            shutil.rmtree(library.tv)
            library.tv.mkdir()  # an unmounted share leaves its (empty) mount point
        elif disk == "mapping-root-missing":
            shutil.rmtree(library.mount)
        elif disk == "nested-mount-gone":
            # "Deleted Show" was its own mount inside the library: unmounted, its mount point stays empty.
            _delete_series(library)
            (library.tv / "Deleted Show").mkdir()
        else:
            library.configs = [_config(str(tmp_path / "movies"))]
            _delete_series(library)

        assert missing.sweep_missing_files(store, library.configs) == 0
        assert not any(_marked(store, [library.kept, *library.deleted]))

    @pytest.mark.parametrize("unreadable", ["file", "library-root", "season-parent"])
    def test_a_stale_network_handle_marks_nothing(self, store, library, monkeypatch, unreadable):
        _delete_series(library)
        stale = {
            "file": library.deleted[0],
            "library-root": str(library.tv),
            "season-parent": str(library.tv / "Deleted Show" / "Season 01"),
        }[unreadable]
        real_lstat, real_scandir = os.lstat, os.scandir

        def lstat(path, *args, **kwargs):
            if os.fspath(path) == stale:
                raise OSError(errno.ESTALE, "Stale file handle")
            return real_lstat(path, *args, **kwargs)

        def scandir(path="."):
            if os.fspath(path) == stale:
                raise OSError(errno.ESTALE, "Stale file handle")
            return real_scandir(path)

        monkeypatch.setattr(os, "lstat", lstat)
        monkeypatch.setattr(os, "scandir", scandir)
        missing.sweep_missing_files(store, library.configs)
        assert _marked(store, [library.deleted[0]]) == [False]

    @pytest.mark.parametrize("dangling", ["file", "series-folder"])
    def test_a_symlink_whose_remote_target_dropped_is_not_missing(self, store, tmp_path, dangling):
        # Reviewer's repro: a Real-Debrid/zurg-style library of symlinks into an rclone mount; the rclone mount drops.
        remote = tmp_path / "rclone" / "__all__"
        lib = tmp_path / "mnt" / "tv"
        (lib / "Other Show").mkdir(parents=True)
        if dangling == "file":
            target = _episode(remote / "torrentA", "file.mkv")
            season = lib / "Show" / "Season 01"
            season.mkdir(parents=True)
            link = season / "Show - S01E01.mkv"
            os.symlink(target, link)
        else:
            _episode(remote / "Show" / "Season 01", "Show - S01E01.mkv")
            os.symlink(remote / "Show", lib / "Show")
            link = lib / "Show" / "Season 01" / "Show - S01E01.mkv"
        rec = _known(store, str(link))
        store.lock_marker(rec.id, LOCK)
        configs = [_config("/data/tv", mapping=("/data", str(tmp_path / "mnt")))]
        os.rename(remote, tmp_path / "rclone" / "gone")  # every symlink into it dangles
        assert not os.path.exists(link)

        assert missing.sweep_missing_files(store, configs) == 0
        assert missing.mark_if_missing(store, store.get_file(str(link)), configs) is False
        assert _marked(store, [str(link)]) == [False]
        assert store.get_locked(rec.id) == {MarkerType.CREDITS: LOCK}

    def test_a_file_a_job_is_running_is_skipped(self, store, library):
        _delete_series(library)
        with FILE_RUN_LOCKS.hold(library.deleted[0]):
            assert missing.sweep_missing_files(store, library.configs) == 3
        assert _marked(store, library.deleted) == [False, True, True, True]

    def test_a_file_that_is_back_is_cleared_and_listed_again(self, store, library):
        _delete_series(library)
        missing.sweep_missing_files(store, library.configs)
        _episode(library.tv / "Deleted Show" / "Season 01", os.path.basename(library.deleted[0]))  # restored

        assert missing.sweep_missing_files(store, library.configs) == 0

        assert _marked(store, library.deleted) == [False, True, True, True]
        assert store.files_in_review() == sorted([library.kept, library.deleted[0]])

    def test_a_symlink_back_at_a_missing_files_path_clears_it(self, store, library, tmp_path):
        # Whatever is at the path, a dangling link included, isn't missing (a remote mount behind it may come back).
        _delete_series(library)
        missing.sweep_missing_files(store, library.configs)
        season = library.tv / "Deleted Show" / "Season 01"
        season.mkdir(parents=True)
        os.symlink(tmp_path / "rclone" / "gone.mkv", library.deleted[0])

        missing.sweep_missing_files(store, library.configs)

        assert _marked(store, library.deleted) == [False, True, True, True]

    def test_each_sweep_goes_on_from_where_the_last_stopped(self, store, library):
        _delete_series(library)
        assert missing.sweep_missing_files(store, library.configs, limit=2) == 1  # the kept file, then one deleted
        assert missing.sweep_missing_files(store, library.configs, limit=2) == 2
        assert missing.sweep_missing_files(store, library.configs, limit=2) == 1  # wraps round to the lowest id
        assert all(_marked(store, library.deleted))

    def test_a_sweep_out_of_time_checks_no_further_file(self, store, library, monkeypatch):
        _delete_series(library)
        clock = iter([0.0, 0.0, 30.0, 61.0])
        monkeypatch.setattr(missing, "_monotonic", lambda: next(clock))
        assert missing.sweep_missing_files(store, library.configs) == 1  # the kept file and one deleted fit in 60 s
        assert _marked(store, library.deleted) == [True, False, False, False]


class TestAJobFindsTheFileMissing:
    def _run(self, store, configs, path):
        ctx = _ctx(store, configs)
        return ctx, check_item(ProcessableItem(canonical_path=path, server_id=""), ctx=ctx)

    def test_it_marks_the_file_and_counts_it_for_the_jobs_log_line(self, store, library):
        _delete_series(library)
        ctx, out = self._run(store, library.configs, library.deleted[0])
        assert out.outcome_key == FileOutcome.FILE_NOT_FOUND.value
        assert _marked(store, library.deleted) == [True, False, False, False]
        assert (ctx.take_missing(), ctx.take_missing()) == (1, 0)

    def test_a_dangling_symlink_is_not_found_but_not_marked(self, store, library, tmp_path):
        target = _episode(tmp_path / "rclone", "file.mkv")
        link = library.tv / "Kept Show" / "Season 01" / "Kept Show - S01E02.mkv"
        os.symlink(target, link)
        _known(store, str(link))
        os.remove(target)
        ctx, out = self._run(store, library.configs, str(link))
        assert out.outcome_key == FileOutcome.FILE_NOT_FOUND.value
        assert _marked(store, [str(link)]) == [False] and ctx.take_missing() == 0

    def test_a_check_that_doesnt_answer_in_time_marks_nothing(self, store, library, monkeypatch):
        _delete_series(library)
        release = threading.Event()
        real_scandir = os.scandir

        def stalled(path="."):  # a hard-mounted share that stalls: no error, no answer
            if os.fspath(path) == str(library.tv):
                release.wait(5)
            return real_scandir(path)

        monkeypatch.setattr(missing, "CHECK_TIMEOUT_S", 0.2)
        monkeypatch.setattr(os, "scandir", stalled)
        try:
            assert missing.mark_if_missing(store, store.get_file(library.deleted[0]), library.configs) is False
        finally:
            release.set()
        assert _marked(store, [library.deleted[0]]) == [False]

    def test_an_in_place_upgrade_gap_is_marked_then_cleared_when_the_file_lands_with_its_locks(
        self, store, library, monkeypatch
    ):
        # Reviewer's repro: Sonarr or Tdarr remove the old file and move the new one in under the same name.
        path = library.deleted[0]
        rec = store.get_file(path)
        os.remove(path)  # the gap
        ctx, out = self._run(store, library.configs, path)
        assert out.outcome_key == FileOutcome.FILE_NOT_FOUND.value and _marked(store, [path]) == [True]
        assert library.deleted[0] not in store.files_in_review()
        with open(path, "wb") as fh:
            fh.write(b"y" * 20)  # the upgrade lands
        monkeypatch.setattr(pipeline, "probe_media", MagicMock(side_effect=ProbeError("still being written")))

        self._run(store, library.configs, path)  # its run stops at the probe, but saw the file on disk

        assert _marked(store, [path]) == [False]
        assert store.get_locked(rec.id) == {MarkerType.CREDITS: LOCK}
        assert path in store.files_in_review()

    def test_the_same_identity_stored_again_clears_the_mark(self, store, library):
        rec = store.get_file(library.kept)
        assert store.mark_missing(rec)
        again = store.upsert_file(FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns), duration_ms=None,
                                  season_key=rec.season_key, is_movie=False)  # fmt: skip
        assert again.missing_since is None and store.get_decisions(rec.id)

    def test_a_new_identity_stored_for_the_path_clears_the_mark(self, store, library):
        path = library.deleted[0]
        rec = store.get_file(path)
        assert store.mark_missing(rec)
        with open(path, "wb") as fh:
            fh.write(b"y" * 20)
        st = os.stat(path)
        new = store.upsert_file(FileIdentity(path, st.st_size, st.st_mtime_ns), duration_ms=1, season_key=None,
                                is_movie=False)  # fmt: skip
        assert new.id == rec.id and new.missing_since is None
        assert store.get_locked(new.id) == {MarkerType.CREDITS: LOCK}


class TestDecideAgain:
    def test_the_list_skips_and_marks_a_series_deleted_since(self, store, library, loguru_caplog):
        _delete_series(library)
        items = job_runner._items_to_decide_again(store, library.configs)
        assert [item.canonical_path for item in items] == [library.kept]
        assert all(_marked(store, library.deleted))
        assert MISSING_4 in loguru_caplog.text

    def test_without_the_servers_configs_nothing_is_marked(self, store, library):
        _delete_series(library)
        assert len(job_runner._items_to_decide_again(store)) == 5
        assert not any(_marked(store, library.deleted))

    def test_a_file_stored_again_while_it_was_checked_is_not_marked(self, store, library, monkeypatch):
        # Reviewer's repro: the row is read before the (up to 60 s) check; an upgrade landing meanwhile wins.
        path = library.deleted[0]
        os.remove(path)
        real = missing._missing

        def checked_then_file_lands(p, configs, folders):
            gone = real(p, configs, folders)
            with open(path, "wb") as fh:
                fh.write(b"y" * 20)
            st = os.stat(path)
            store.upsert_file(FileIdentity(path, st.st_size, st.st_mtime_ns), duration_ms=1, season_key=None,
                              is_movie=False)  # fmt: skip
            return gone

        monkeypatch.setattr(missing, "_missing", checked_then_file_lands)
        assert missing.mark_missing_files(store, [path], library.configs) == 0
        assert _marked(store, [path]) == [False]


class TestStore:
    def test_marking_keeps_every_row_tied_to_the_file(self, store, library):
        rec = store.get_file(library.deleted[0])
        other = store.get_file(library.deleted[1])
        store.replace_evidence(rec.id, Source.CHAPTERS, [], version=1)
        store.set_publish_state(rec.id, "plex-1", item_id="7", markers=[INTRO], status="written", message="1")
        store.set_fingerprint(rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0,
                              length_s=105.0, algorithm=1, points=b"")  # fmt: skip
        store.set_season_pair(rec.id, other.id, 4, [], identity_a=(rec.size, rec.mtime_ns),
                              identity_b=(other.size, other.mtime_ns))  # fmt: skip
        conn = sqlite3.connect(store.db_path)

        def rows_of_the_file():
            counts = {}
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
                    counts[(table, fk[3])] = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {fk[3]}=?", (rec.id,)
                    ).fetchone()[0]
            return counts

        try:
            before = rows_of_the_file()
            assert store.mark_missing(rec) is True
            assert rows_of_the_file() == before
            assert conn.execute("SELECT COUNT(*) FROM files WHERE id=?", (rec.id,)).fetchone()[0] == 1
        finally:
            conn.close()
        assert store.mark_missing(store.get_file(library.deleted[0])) is False  # already marked

    def test_a_row_a_new_file_took_over_is_not_marked(self, store, library):
        rec = store.get_file(library.kept)
        with open(library.kept, "ab") as fh:
            fh.write(b"more")  # a new file came to the same path, and its run stored it
        _known(store, library.kept)
        assert store.mark_missing(rec) is False
        assert store.get_file(library.kept).missing_since is None

    def test_clear_missing(self, store, library):
        rec = store.get_file(library.kept)
        assert store.clear_missing(rec.id) is False
        store.mark_missing(rec)
        assert store.clear_missing(rec.id) is True and store.get_file(library.kept).missing_since is None

    def test_what_lists_files_to_work_on_skips_marked_files(self, store, library):
        rec = store.get_file(library.deleted[0])
        store.set_publish_state(rec.id, "plex-1", item_id="7", markers=None, status="waiting",
                                message="Waiting for this item's other versions to agree on: intro")  # fmt: skip
        store.set_publish_state(rec.id, "plex-2", item_id="8", markers=None, status="failed", message="x")
        season = os.path.dirname(library.deleted[0])
        assert library.deleted[0] in store.files_in_review()
        assert store.files_waiting_for_other_versions() == [library.deleted[0]]
        assert store.files_with_undelivered_locks(["plex-2"]) == [(library.deleted[0], "plex-2")]
        assert [r.canonical_path for r in store.files_in_season(season)] == library.deleted[:2]

        store.mark_missing(rec)

        assert library.deleted[0] not in store.files_in_review()
        assert store.files_waiting_for_other_versions() == []
        assert store.files_with_undelivered_locks(["plex-2"]) == []
        assert [r.canonical_path for r in store.files_in_season(season)] == library.deleted[1:2]


class TestBackgroundSweep:
    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        monkeypatch.setattr(fpmod, "_sweep_started_at", None)
        monkeypatch.setattr(fpmod, "_stuck_warned_at", None)
        yield
        assert fpmod._SWEEP_LOCK.acquire(timeout=5)
        fpmod._SWEEP_LOCK.release()

    def test_missing_files_are_marked_on_the_fingerprint_sweeps_thread_before_it(self, store, library):
        _delete_series(library)
        order, done = [], threading.Event()
        real = missing.sweep_missing_files

        def marking(*args, **kwargs):
            order.append(("missing", threading.current_thread().name))
            return real(*args, **kwargs)

        def fingerprints(swept_store):
            order.append(("fingerprints", threading.current_thread().name))
            done.set()
            return 0

        with (
            patch.object(fpmod, "sweep_missing_files", side_effect=marking),
            patch.object(fpmod, "sweep_fingerprint_cache", side_effect=fingerprints),
        ):
            assert fpmod.start_fingerprint_sweep(store, configs=library.configs) is True
            assert done.wait(5)
        assert order == [("missing", "fingerprint-sweep"), ("fingerprints", "fingerprint-sweep")]
        assert all(_marked(store, library.deleted))

    def test_without_configs_only_the_fingerprints_are_swept(self, store):
        done = threading.Event()
        with (
            patch.object(fpmod, "sweep_missing_files") as marking,
            patch.object(fpmod, "sweep_fingerprint_cache", side_effect=lambda s: done.set() or 0),
        ):
            assert fpmod.start_fingerprint_sweep(store) is True
            assert done.wait(5)
        marking.assert_not_called()

    def test_a_missing_file_sweep_that_raises_still_sweeps_the_fingerprints(self, store, library, loguru_caplog):
        done = threading.Event()
        with (
            patch.object(fpmod, "sweep_missing_files", side_effect=sqlite3.OperationalError("database is locked")),
            patch.object(fpmod, "sweep_fingerprint_cache", side_effect=lambda s: done.set() or 0),
        ):
            assert fpmod.start_fingerprint_sweep(store, configs=library.configs) is True
            assert done.wait(5)
        assert "Couldn't check markers.db for files missing from disk" in loguru_caplog.text
