"""What the pipeline relies on from every real publisher built by ``publisher_for`` (plex-item-publish-design.md).

``write`` returns what is ours on the server item afterwards; ``previous`` is what this app last left on that item (None
when unknown); ``atomic_writes`` says whether a failed write can have left anything behind. ``MagicMock`` publishers in
``test_pipeline.py`` never exercise that, so this runs the real Plex (temp database with Plex 1.43's schema) and
Jellyfin (autospec'd server) publishers.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.publishers.base import Capability, CapabilityReport, PublishError
from media_preview_generator.markers.publishers.factory import publisher_for
from media_preview_generator.markers.publishers.jellyfin import TICKS_PER_MS
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.jellyfin import JellyfinServer
from media_preview_generator.servers.plex import PlexServer
from tests.markers.fakes import FakeRegistry
from tests.markers.test_pipeline import CHAPTERS_BOTH, DUR, _ctx
from tests.markers.test_plex_db_publisher import PLEX_VERSION, _make_db, _rows

T = MarkerType
INTRO = Marker(T.INTRO, 126_771, 157_068, ("chapters",))
CREDITS = Marker(T.CREDITS, 1_295_324, DUR, ("chapters",))


def _config(sid: str, stype: ServerType, root: str, **kwargs) -> ServerConfig:
    markers = {"enabled": True, "library_ids": None}
    if stype is ServerType.PLEX:
        markers["plex"] = {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"}
    return ServerConfig(
        id=sid,
        type=stype,
        name=sid.upper(),
        enabled=True,
        url=f"http://{sid}",
        auth={},
        libraries=[Library("1", "TV Shows", (root,))],
        markers=markers,
        **kwargs,
    )


def _media(tmp_path) -> str:
    folder = tmp_path / "media" / "Show (2020) {tvdb-1}" / "Season 01"
    folder.mkdir(parents=True)
    path = folder / "Show (2020) - S01E01.mkv"
    path.write_bytes(b"x" * 100)
    return str(path)


def _resp(status: int):
    return MagicMock(status_code=status, **{"json.return_value": {}})


@pytest.fixture
def plex(tmp_path, monkeypatch):
    # Plex holding its database and a local filesystem are proven by Task 8's own tests; here they are given.
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)
    monkeypatch.setattr(plex_db, "filesystem_type", lambda *_a, **_kw: "ext4")
    path = _media(tmp_path)
    folder = tmp_path / "Plex Media Server"
    db = _make_db(folder, parts=((path, None),))
    cfg = _config("plex-1", ServerType.PLEX, str(tmp_path / "media"), output={"plex_config_folder": str(folder)})
    server = create_autospec(PlexServer, instance=True)

    def shown():
        return [
            (text, start, end)
            for text, start, end in _rows(
                db, "SELECT text, time_offset, end_time_offset FROM taggings WHERE metadata_item_id=7 ORDER BY 2"
            )
        ]

    return SimpleNamespace(
        name="plex",
        server=server,
        cfg=cfg,
        item_id="7",
        path=path,
        shown=shown,
        # Plex stores credits 2 s before they are served (spec §3.1).
        both=[("intro", INTRO.start_ms, INTRO.end_ms), ("credits", CREDITS.start_ms - 2_000, DUR)],
        # Nothing on the item is provably ours after a failed write, so nothing is removed.
        after_unknown_clear="kept",
        # One transaction: a write that raises leaves nothing behind.
        failure_leaves_markers=False,
        atomic_writes=True,
    )


@pytest.fixture
def jellyfin(tmp_path):
    path = _media(tmp_path)
    cfg = _config("jellyfin-1", ServerType.JELLYFIN, str(tmp_path / "media"))
    server = create_autospec(JellyfinServer, instance=True)
    stored: list[dict] = []

    def put(item_id, segments, *, file_size=None):
        stored[:] = segments
        return _resp(200)

    def delete(item_id):
        stored.clear()
        return _resp(204)

    server.put_bridge_markers.side_effect = put
    server.delete_bridge_markers.side_effect = delete
    server.get_media_segments.side_effect = lambda item_id: [
        {"Id": f"s{i}", "ItemId": item_id, "Type": s["type"], "StartTicks": s["startTicks"], "EndTicks": s["endTicks"]}
        for i, s in enumerate(stored)
    ]
    server.get_bridge_marker_state.return_value = {"segments": [], "fileSize": None, "stale": False}
    server.get_bridge_markers.side_effect = lambda item_id: list(stored)

    def shown():
        return [(s["type"], s["startTicks"] // TICKS_PER_MS, s["endTicks"] // TICKS_PER_MS) for s in stored]

    return SimpleNamespace(
        name="jellyfin",
        server=server,
        cfg=cfg,
        item_id="abc",
        path=path,
        shown=shown,
        both=[("Intro", INTRO.start_ms, INTRO.end_ms), ("Outro", CREDITS.start_ms, DUR)],
        # The plugin may still hold what a failed write stored, so an unknown previous clears it.
        after_unknown_clear="cleared",
        # Stored before the confirmation failed, and the best-effort DELETE failed too.
        failure_leaves_markers=True,
        atomic_writes=False,
    )


VENDORS = ["plex", "jellyfin"]


def _publisher(target):
    return publisher_for(target.server, target.cfg, sibling_markers=lambda _path: None)


def _served(markers):
    return [(m.type, m.start_ms, m.end_ms) for m in markers]


@pytest.mark.parametrize("vendor", VENDORS)
def test_atomic_writes_is_declared(request, vendor):
    target = request.getfixturevalue(vendor)
    assert _publisher(target).atomic_writes is target.atomic_writes


@pytest.mark.parametrize("vendor", VENDORS)
def test_markers_with_unknown_previous_are_written(request, vendor):
    target = request.getfixturevalue(vendor)
    ours = _publisher(target).write(
        target.item_id, [INTRO, CREDITS], previous=None, duration_ms=DUR, canonical_path=target.path
    )
    assert _served(ours) == _served([INTRO, CREDITS])
    assert target.shown() == target.both


@pytest.mark.parametrize("vendor", VENDORS)
def test_nothing_to_show_with_unknown_previous_is_accepted(request, vendor):
    target = request.getfixturevalue(vendor)
    publisher = _publisher(target)
    assert publisher.write(target.item_id, [], previous=None, duration_ms=DUR, canonical_path=target.path) == []
    assert target.shown() == []
    publisher.write(target.item_id, [INTRO, CREDITS], previous=[], duration_ms=DUR, canonical_path=target.path)
    assert publisher.write(target.item_id, [], previous=None, duration_ms=DUR, canonical_path=target.path) == []
    assert target.shown() == (target.both if target.after_unknown_clear == "kept" else [])


@pytest.mark.parametrize("vendor", VENDORS)
def test_known_previous_removes_what_we_published(request, vendor):
    target = request.getfixturevalue(vendor)
    publisher = _publisher(target)
    publisher.write(target.item_id, [INTRO, CREDITS], previous=[], duration_ms=DUR, canonical_path=target.path)
    ours = publisher.write(target.item_id, [], previous=[INTRO, CREDITS], duration_ms=DUR, canonical_path=target.path)
    assert ours == [] and target.shown() == []


def test_emby_has_no_publisher_yet(tmp_path):
    cfg = _config("emby-1", ServerType.EMBY, str(tmp_path / "media"))
    assert publisher_for(MagicMock(), cfg, sibling_markers=lambda _path: None) is None


@pytest.mark.parametrize("vendor", VENDORS)
def test_pipeline_after_a_failed_write_uses_the_real_publisher(request, tmp_path, vendor):
    # The first write fails (Jellyfin: after the plugin stored it); the next run has nothing to show. Jellyfin gets
    # previous=None and clears the plugin store; Plex's atomic failure left nothing, so there's nothing to write.
    target = request.getfixturevalue(vendor)
    target.server.resolve_remote_path_to_item_id.return_value = target.item_id
    reg = FakeRegistry({target.cfg.id: target.cfg}, servers_by_id={target.cfg.id: target.server})
    store = MarkerStore(str(tmp_path / "markers.db"))
    no_online = {"sources": [{"id": s, "enabled": s == "chapters"} for s in ("theintrodb", "introdb", "skipdb")]}
    item = ProcessableItem(canonical_path=target.path, server_id=target.cfg.id)
    real_write = type(_publisher(target)).write
    calls = []

    def failing_once(self, *args, **kwargs):
        calls.append(kwargs["previous"])
        if len(calls) == 1:
            if target.failure_leaves_markers:
                real_write(self, *args, **kwargs)
            raise PublishError("Stored on Jellyfin but couldn't confirm it is shown")
        return real_write(self, *args, **kwargs)

    with (
        patch.object(pipeline, "probe_media", return_value=MediaProbe(DUR, CHAPTERS_BOTH)),
        patch.object(
            type(_publisher(target)),
            "capability",
            return_value=CapabilityReport(Capability.READY, "ok"),
        ),
        patch.object(type(_publisher(target)), "write", failing_once),
    ):
        first = pipeline.check_item(item, ctx=_ctx(store, reg, settings_raw=no_online))
        off = {**no_online, "detect": {"intro": False, "credits": False}}
        second = pipeline.check_item(item, ctx=_ctx(store, reg, settings_raw=off))
    store.close()
    assert first.outcome_key == FileOutcome.FAILED.value
    target.server.resolve_remote_path_to_item_id.assert_called_with(target.path)
    if target.atomic_writes:
        # A failed Plex write changed nothing, so the item row's (empty) record stays exact: nothing to clear.
        assert calls == [[]]
        assert second.publisher_rows[0]["status"] == "markers_none"
    else:
        assert calls == [[], None]
        assert second.publisher_rows[0]["status"] == "markers_written"
    assert target.shown() == []


@pytest.mark.parametrize("vendor", VENDORS)
def test_own_previous_is_accepted(request, vendor):
    # The pipeline passes what the calling file last published on another item (a merge or split since); a publisher
    # either removes its own copy of those (Plex's part keys) or ignores it.
    target = request.getfixturevalue(vendor)
    ours = _publisher(target).write(
        target.item_id,
        [INTRO, CREDITS],
        previous=[],
        duration_ms=DUR,
        canonical_path=target.path,
        own_previous=[INTRO],
    )
    assert _served(ours) == _served([INTRO, CREDITS])
    assert target.shown() == target.both


# --- Multi-version Plex items: the real pipeline and PlexMarkerPublisher on one item (plex-item-publish-design.md) ---

NO_ONLINE = {"sources": [{"id": s, "enabled": s == "chapters"} for s in ("theintrodb", "introdb", "skipdb")]}


def chapters(intro: tuple[int, int] | None = None, credits: int | None = None) -> tuple[Chapter, ...]:
    """Chapters with an optional "Intro" (start, end) and a "Credits" chapter from ``credits`` to the end."""
    out, t = [], 0
    if intro:
        out += [Chapter(0, intro[0], "Chapter 1"), Chapter(intro[0], intro[1], "Intro")]
        t = intro[1]
    if credits is None:
        return (*out, Chapter(t, None, "Chapter 2"))
    return (*out, Chapter(t, credits, "Chapter 2"), Chapter(credits, None, "Credits"))


class PlexItem:
    """Versions of one episode as parts of Plex item 7 in a temp database, published through the real pipeline.

    Each version's chapters and duration are set per path; ``commits`` counts writes that changed Plex's database.
    """

    def __init__(self, tmp_path, monkeypatch, *, versions=("1080p", "2160p"), in_item=None):
        folder = tmp_path / "media" / "Show (2020) {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        self.paths = {}
        for version in versions:
            path = folder / f"Show (2020) - S01E01 - {version}.mkv"
            path.write_bytes(b"x" * 100)
            self.paths[version] = str(path)
        plex_folder = tmp_path / "Plex Media Server"
        in_item = versions if in_item is None else in_item
        self.db = _make_db(plex_folder, parts=tuple((self.paths[v], None) for v in in_item))
        self.cfg = _config(
            "plex-1", ServerType.PLEX, str(tmp_path / "media"), output={"plex_config_folder": str(plex_folder)}
        )
        server = create_autospec(PlexServer, instance=True)
        self.item_ids = dict.fromkeys(self.paths.values(), "7")
        server.resolve_remote_path_to_item_id.side_effect = lambda path: self.item_ids.get(path)
        server.get_external_ids.return_value = None
        server.get_server_status.return_value = {"plex_pass": True, "version": PLEX_VERSION}
        server.get_marker_detection_prefs.return_value = {"intro": "never", "credits": "never"}
        server.get_markers.return_value = []
        self.server = server
        self.registry = FakeRegistry({"plex-1": self.cfg}, servers_by_id={"plex-1": server})
        self.store = MarkerStore(str(tmp_path / "markers.db"))
        self.chapters: dict[str, tuple[Chapter, ...]] = {}
        self.durations: dict[str, int] = {}
        self.commits = 0
        real_write_item = plex_db.PlexMarkerPublisher._write_item
        commit_guard = threading.Lock()

        def counting_write_item(publisher, *args, **kwargs):
            changed = real_write_item(publisher, *args, **kwargs)
            with commit_guard:
                self.commits += int(bool(changed))
            return changed

        # Patched once for the whole test: runs on several threads must not patch and unpatch over each other.
        monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)
        monkeypatch.setattr(plex_db, "filesystem_type", lambda *_a, **_kw: "ext4")
        monkeypatch.setattr(plex_db.PlexMarkerPublisher, "_write_item", counting_write_item)
        monkeypatch.setattr(
            pipeline,
            "probe_media",
            lambda path, *, ffprobe: MediaProbe(self.durations.get(path, DUR), self.chapters.get(path, ())),
        )

    def run(self, version: str, *, force: bool = False):
        item = ProcessableItem(canonical_path=self.paths[version], server_id="plex-1")
        return pipeline.check_item(item, ctx=_ctx(self.store, self.registry, settings_raw=NO_ONLINE, force=force))

    def touch(self, version: str, ns: int) -> None:
        """A replaced file: same path, new identity."""
        os.utime(self.paths[version], ns=(ns, ns))

    def _sql(self, *statements: tuple) -> None:
        conn = sqlite3.connect(self.db)
        try:
            for statement in statements:
                conn.execute(*statement)
            conn.commit()
        finally:
            conn.close()

    def add_part(self, version: str, item: int = 7) -> None:
        (n,) = _rows(self.db, "SELECT COALESCE(MAX(id), 0) + 1 FROM media_items")[0]
        self._sql(
            ("INSERT INTO media_items (id, metadata_item_id) VALUES (?, ?)", (n, item)),
            ("INSERT INTO media_parts (id, media_item_id, file) VALUES (?, ?, ?)", (n, n, self.paths[version])),
        )

    def delete_part(self, version: str) -> None:
        """What Plex does when a version's file is deleted and the library is scanned."""
        os.remove(self.paths[version])
        self._sql(
            ("UPDATE media_parts SET deleted_at=1 WHERE file=?", (self.paths[version],)),
            (
                "UPDATE media_items SET deleted_at=1 WHERE id IN (SELECT media_item_id FROM media_parts WHERE file=?)",
                (self.paths[version],),
            ),
        )

    def move_part(self, version: str, item: int) -> None:
        """A merge or split: the version's media item now belongs to ``item`` (created if needed)."""
        self._sql(
            ("INSERT OR IGNORE INTO metadata_items (id, metadata_type, title) VALUES (?, 4, 'Ep')", (item,)),
            (
                "UPDATE media_items SET metadata_item_id=? WHERE id IN "
                "(SELECT media_item_id FROM media_parts WHERE file=?)",
                (item, self.paths[version]),
            ),
        )
        self.item_ids[self.paths[version]] = str(item)

    def served(self, item: int = 7) -> list[tuple[str, int, int]]:
        publisher = publisher_for(self.server, self.cfg, sibling_markers=lambda _path: None)
        return [(m.type.value, m.start_ms, m.end_ms) for m in publisher.read(str(item))]

    def recorded(self, item: int = 7) -> list[tuple[str, int, int]] | None:
        row = self.store.get_item_publish_state("plex-1", str(item))
        return None if row is None else [(m.type.value, m.start_ms, m.end_ms) for m in row.markers]

    def part_types(self) -> dict[str, list[str]]:
        """The ``pv:`` marker keys on each version's part."""
        rows = _rows(self.db, "SELECT file, extra_data FROM media_parts ORDER BY id")
        return {
            os.path.basename(file): sorted(k for k in json.loads(extra or "{}") if k in ("pv:intros", "pv:credits"))
            for file, extra in rows
        }


@pytest.fixture
def plex_item(tmp_path, monkeypatch):
    items = []

    def make(**kwargs) -> PlexItem:
        item = PlexItem(tmp_path, monkeypatch, **kwargs)
        items.append(item)
        return item

    yield make
    for item in items:
        item.store.close()


INTRO_X = (126_771, 157_068)
CREDITS_AT = 1_295_324
SHOWN_INTRO = ("intro", *INTRO_X)
SHOWN_CREDITS = ("credits", CREDITS_AT, DUR)


def _outcomes(*outs) -> list[str]:
    return [o.outcome_key.removeprefix("markers_") for o in outs]


def test_single_version_publishes_once_then_is_up_to_date(plex_item):
    item = plex_item(versions=("1080p",))
    item.chapters[item.paths["1080p"]] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    assert _outcomes(item.run("1080p"), item.run("1080p")) == ["published", "up_to_date"]
    assert item.served() == item.recorded() == [SHOWN_INTRO, SHOWN_CREDITS] and item.commits == 1

    item.touch("1080p", 5)  # replaced by a file with the same chapters: published again, nothing changes on Plex
    assert _outcomes(item.run("1080p"), item.run("1080p")) == ["published", "up_to_date"]
    assert item.commits == 1

    item.chapters[item.paths["1080p"]] = chapters(intro=INTRO_X)
    item.touch("1080p", 6)
    assert _outcomes(item.run("1080p"), item.run("1080p")) == ["published", "up_to_date"]
    assert item.served() == item.recorded() == [SHOWN_INTRO] and item.commits == 2


def test_a_version_added_later_takes_off_the_credits_it_disagrees_with(plex_item):
    item = plex_item(in_item=("1080p",))
    a, b = item.paths["1080p"], item.paths["2160p"]
    item.chapters[a] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    assert _outcomes(item.run("1080p")) == ["published"]
    item.add_part("2160p")
    item.chapters[b] = chapters(intro=INTRO_X, credits=CREDITS_AT - 19_000)
    assert _outcomes(item.run("2160p")) == ["waiting"]
    assert item.served() == item.recorded() == [SHOWN_INTRO] and item.commits == 2
    assert _outcomes(item.run("1080p"), item.run("2160p"), item.run("1080p")) == ["waiting"] * 3
    assert item.served() == item.recorded() == [SHOWN_INTRO] and item.commits == 2
    assert item.part_types() == {os.path.basename(a): ["pv:intros"], os.path.basename(b): ["pv:intros"]}


def test_partial_agreement_then_agreeing_credits_with_different_times(plex_item):
    item = plex_item()
    a, b = item.paths["1080p"], item.paths["2160p"]
    item.chapters[a] = chapters(intro=(11_000, 37_000), credits=CREDITS_AT)
    item.chapters[b] = chapters(intro=(11_000, 37_000), credits=CREDITS_AT - 19_000)
    assert _outcomes(item.run("1080p"), item.run("2160p"), item.run("1080p")) == ["waiting"] * 3
    assert item.served() == item.recorded() == [("intro", 11_000, 37_000)] and item.commits == 1

    # Both re-encoded without an intro chapter; the credits now agree 576 ms apart.
    item.chapters[a] = chapters(credits=CREDITS_AT)
    item.chapters[b] = chapters(credits=CREDITS_AT + 576)
    item.touch("1080p", 11)
    item.touch("2160p", 12)
    assert _outcomes(item.run("1080p")) == ["waiting"]  # B not decided again yet: our intro comes off
    assert item.served() == item.recorded() == [] and item.commits == 2
    assert _outcomes(item.run("2160p"), item.run("1080p")) == ["published", "published"]
    shown = item.served()
    assert shown == item.recorded() and [t for t, *_ in shown] == ["credits"] and item.commits == 3
    assert _outcomes(item.run("2160p"), item.run("1080p"), item.run("2160p")) == ["up_to_date"] * 3
    assert item.served() == shown and item.commits == 3


@pytest.mark.parametrize("trigger", ["forced", "file-changed"])
def test_a_new_version_that_is_never_decided_takes_our_markers_off_at_the_next_publish(plex_item, trigger):
    item = plex_item(in_item=("1080p",))
    item.chapters[item.paths["1080p"]] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    item.run("1080p")
    item.add_part("2160p")  # its disk isn't mapped into the container: never decided
    assert _outcomes(item.run("1080p")) == ["up_to_date"]  # the known limit until something makes A publish
    if trigger == "file-changed":
        item.touch("1080p", 9)
    out = item.run("1080p", force=trigger == "forced")
    assert _outcomes(out) == ["waiting"] and "intro, credits" in out.publisher_rows[0]["message"]
    assert item.served() == item.recorded() == [] and item.commits == 2
    assert _outcomes(item.run("1080p", force=trigger == "forced"), item.run("1080p")) == ["waiting", "waiting"]
    assert item.commits == 2
    assert item.part_types() == {os.path.basename(p): [] for p in item.paths.values()}


def test_a_deleted_version_stops_blocking_the_types_it_disagreed_on(plex_item):
    item = plex_item()
    a, b = item.paths["1080p"], item.paths["2160p"]
    item.chapters[a] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    item.chapters[b] = chapters(intro=INTRO_X, credits=CREDITS_AT - 19_000)
    assert _outcomes(item.run("1080p"), item.run("2160p"), item.run("1080p")) == ["waiting"] * 3
    item.delete_part("2160p")
    assert _outcomes(item.run("1080p"), item.run("1080p")) == ["published", "up_to_date"]
    assert item.served() == item.recorded() == [SHOWN_INTRO, SHOWN_CREDITS] and item.commits == 2


def test_a_transient_failure_keeps_the_item_row_so_the_next_run_removes_the_credits(plex_item):
    item = plex_item(versions=("1080p",))
    path = item.paths["1080p"]
    item.chapters[path] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    item.run("1080p")
    item.chapters[path] = chapters(intro=INTRO_X)
    item.touch("1080p", 3)
    real_database = plex_db.PlexMarkerPublisher._database

    def busy(publisher, *, read_only, deadline):
        if not read_only:
            raise PublishError("Plex is busy", state=Capability.UNREACHABLE)
        return real_database(publisher, read_only=read_only, deadline=deadline)

    with patch.object(plex_db.PlexMarkerPublisher, "_database", busy):
        assert _outcomes(item.run("1080p")) == ["failed"]
    assert item.store.get_item_publish_state("plex-1", "7").status == "failed"
    assert item.served() == item.recorded() == [SHOWN_INTRO, SHOWN_CREDITS]
    assert _outcomes(item.run("1080p"), item.run("1080p")) == ["published", "up_to_date"]
    assert item.served() == item.recorded() == [SHOWN_INTRO] and item.commits == 2


def test_agreeing_versions_with_different_runtimes_commit_once(plex_item):
    # Credits that run to the end differ by the runtimes (800 ms here); each version must not rewrite the other's.
    item = plex_item()
    a, b = item.paths["1080p"], item.paths["2160p"]
    item.durations[b] = DUR + 800
    item.chapters[a] = chapters(credits=CREDITS_AT)
    item.chapters[b] = chapters(credits=CREDITS_AT)
    assert _outcomes(item.run("1080p"), item.run("2160p")) == ["waiting", "published"]
    assert _outcomes(item.run("1080p")) == ["published"]
    outs = [item.run(v) for _ in range(3) for v in ("2160p", "1080p")]
    assert _outcomes(*outs) == ["up_to_date"] * 6
    assert item.commits == 1 and item.served() == item.recorded()


def test_a_merge_takes_the_moved_versions_credits_off_its_part(plex_item):
    item = plex_item(in_item=("1080p",))
    a, b = item.paths["1080p"], item.paths["2160p"]
    item.move_part("1080p", 7)
    item.add_part("2160p", item=8)
    item.move_part("2160p", 8)
    item.chapters[a] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    item.chapters[b] = chapters(intro=INTRO_X, credits=CREDITS_AT - 19_000)
    assert _outcomes(item.run("1080p"), item.run("2160p")) == ["published", "published"]

    item.move_part("2160p", 7)  # the user merges item 8 into 7
    item._sql(("DELETE FROM taggings WHERE metadata_item_id=8",), ("DELETE FROM metadata_items WHERE id=8",))
    # A's decision and item 7's row are unchanged, so A can't tell a part joined (the known limit); B's run can.
    assert _outcomes(item.run("1080p"), item.run("2160p"), item.run("1080p")) == ["up_to_date", "waiting", "waiting"]
    assert item.served() == item.recorded() == [SHOWN_INTRO]
    assert item.part_types() == {os.path.basename(a): ["pv:intros"], os.path.basename(b): ["pv:intros"]}
    commits = item.commits
    assert _outcomes(item.run("1080p"), item.run("2160p")) == ["waiting", "waiting"] and item.commits == commits


def test_a_split_takes_the_credits_that_went_to_review_off_the_moved_part(plex_item):
    item = plex_item()
    a, b = item.paths["1080p"], item.paths["2160p"]
    item.chapters[a] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    item.chapters[b] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    assert _outcomes(item.run("1080p"), item.run("2160p"), item.run("1080p"))[-1] in ("published", "up_to_date")
    assert item.served() == [SHOWN_INTRO, SHOWN_CREDITS]

    item.move_part("2160p", 8)  # the user splits B off into its own item
    item.chapters[b] = chapters(intro=INTRO_X)  # and B's credits chapter is gone in a new cut
    item.touch("2160p", 31)
    assert _outcomes(item.run("2160p")) == ["published"]
    assert item.served(8) == item.recorded(8) == [SHOWN_INTRO]
    assert _outcomes(item.run("1080p")) == ["up_to_date"]
    assert item.served(7) == item.recorded(7) == [SHOWN_INTRO, SHOWN_CREDITS]
    assert item.part_types() == {os.path.basename(a): ["pv:credits", "pv:intros"], os.path.basename(b): ["pv:intros"]}


def test_concurrent_versions_leave_the_item_row_matching_what_plex_serves(plex_item, monkeypatch):
    # B's new cut adds credits and B's publish commits them while A's run, whose new cut has no markers at all, holds
    # the item row it read before that commit. A must not record the item as empty while Plex serves B's credits.
    item = plex_item()
    a, b = item.paths["1080p"], item.paths["2160p"]
    item.chapters[a] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    item.chapters[b] = chapters(intro=INTRO_X)
    assert _outcomes(item.run("1080p"), item.run("2160p"), item.run("1080p")) == ["waiting", "published", "waiting"]
    assert item.served() == item.recorded() == [SHOWN_INTRO]

    item.chapters[b] = chapters(intro=INTRO_X, credits=CREDITS_AT)
    item.touch("2160p", 21)
    item.chapters[a] = chapters()
    item.touch("1080p", 22)

    b_decided_what_to_show = threading.Event()
    a_reached_the_item = threading.Event()
    b_done = threading.Event()
    real_desired = plex_db.PlexMarkerPublisher._desired
    real_previous = pipeline._previous_on_item

    def desired(publisher, *args, **kwargs):
        out = real_desired(publisher, *args, **kwargs)
        if threading.current_thread().name == "B":
            b_decided_what_to_show.set()  # B has read A's old decision and is about to write
            assert a_reached_the_item.wait(10)
        return out

    def previous(item_row, publisher):
        out = real_previous(item_row, publisher)
        if threading.current_thread().name == "A":
            a_reached_the_item.set()
            b_done.wait(10)  # without the item lock: A holds this row while B commits
        return out

    class Arrival:
        """The item lock, noticing A queueing behind B."""

        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "A":
                a_reached_the_item.set()
            self._lock.acquire()

        def __exit__(self, *exc):
            self._lock.release()

    monkeypatch.setattr(plex_db.PlexMarkerPublisher, "_desired", desired)
    monkeypatch.setattr(pipeline, "_previous_on_item", previous)
    monkeypatch.setattr(pipeline, "_ITEM_LOCKS", pipeline._KeyedLocks(lock_factory=Arrival))
    results = {}

    def run_b():
        results["B"] = item.run("2160p")
        b_done.set()

    def run_a():
        b_decided_what_to_show.wait(10)
        results["A"] = item.run("1080p")

    threads = [threading.Thread(target=run_b, name="B"), threading.Thread(target=run_a, name="A")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert set(results) == {"A", "B"}
    assert item.served() == item.recorded() == []
    for _ in range(2):
        item.run("2160p")
        item.run("1080p")
        assert item.served() == item.recorded() == []
