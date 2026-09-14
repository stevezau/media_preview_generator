"""Pipeline matrix: owners, identity, kind, evidence order + early stop, decisions, publish fan-out, outcomes."""

from __future__ import annotations

import copy
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome
from media_preview_generator.markers import pipeline
from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, MediaIds, Source
from media_preview_generator.markers.outcomes import OUTCOME_KEYS, FileOutcome, ServerStatus, file_outcome
from media_preview_generator.markers.pipeline import LocalDetectorSpec, PipelineContext, check_item, process_item
from media_preview_generator.markers.probe import Chapter, MediaProbe, ProbeError
from media_preview_generator.markers.publishers.base import (
    Capability,
    CapabilityReport,
    ItemNotFoundError,
    PublishError,
)
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, ServerType
from tests.markers.fakes import FakeClient, FakePlexItems, FakeRegistry, ready_publisher, server_config

T = MarkerType
DUR = 1_321_472
NO_DATA = LookupResult("no_data")


@pytest.fixture
def media(tmp_path):
    folder = tmp_path / "media" / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
    folder.mkdir(parents=True)
    f = folder / "Rick and Morty (2013) - S01E01 - Pilot.mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


@pytest.fixture
def ambiguous(tmp_path):
    """A show folder with only a tmdb id and a file without SxxEyy: the path alone reads as a movie."""
    folder = tmp_path / "media" / "tv" / "Some Show (2020) {tmdb-1234}"
    folder.mkdir(parents=True)
    f = folder / "Some Show - Pilot.mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _probe(chapters=(), duration=DUR):
    return MediaProbe(duration, tuple(chapters))


CHAPTERS_BOTH = (
    Chapter(0, 126_771, "Chapter 1"),
    Chapter(126_771, 157_068, "Intro"),
    Chapter(157_068, 1_295_324, "Chapter 2"),
    Chapter(1_295_324, None, "Credits"),
)
INTRO_CH = Marker(T.INTRO, 126_771, 157_068, ("chapters",))
CREDITS_CH = Marker(T.CREDITS, 1_295_324, DUR, ("chapters",))
# "Opening" at the very start plus end credits: an intro for an episode, never for a movie.
CHAPTERS_OPENING = (
    Chapter(0, 30_000, "Opening"),
    Chapter(30_000, 1_000_000, "Part A"),
    Chapter(1_000_000, None, "End Credits"),
)
OPENING_CH = Marker(T.INTRO, 0, 30_000, ("chapters",))
END_CREDITS_CH = Marker(T.CREDITS, 1_000_000, DUR, ("chapters",))
TIDB_INTRO = Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB)
TIDB_CREDITS = Candidate(T.CREDITS, 1_296_000, 1_320_000, Source.THEINTRODB)
INTRO_ONLY = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": True, "credits": False}}


def _clients(theintrodb=NO_DATA, introdb=NO_DATA, skipdb=NO_DATA):
    return {"theintrodb": FakeClient(theintrodb), "introdb": FakeClient(introdb), "skipdb": FakeClient(skipdb)}


def _ctx(
    store,
    registry,
    *,
    settings_raw=None,
    clients=None,
    detectors=(),
    force=False,
    now=None,
    ttl=300.0,
    live_config=None,
):
    raw = settings_raw or {"sources": [{"id": "theintrodb", "enabled": True}]}
    settings = load_global(validate_global(raw, None)[0])
    return PipelineContext(
        registry=registry,
        config=MagicMock(),
        settings=settings,
        store=store,
        priority=lambda: 2,
        ffprobe="ffprobe",
        force=force,
        clients=clients if clients is not None else _clients(),
        local_detectors=detectors,
        now=now or (lambda: datetime(2026, 9, 13, tzinfo=timezone.utc)),
        capability_ttl_s=ttl,
        # The registry's configs stand in for the saved settings; TestConsentBeforeEachWrite uses the real ones.
        live_config=live_config or registry.get_config,
    )


def _media_root(path):
    """The tmp "media" folder every test file lives in: each server's one library covers it."""
    return path[: path.index("/media/") + len("/media")]


def _registry(media, *server_types):
    root = _media_root(media)
    configs = {f"{t.value}-1": server_config(f"{t.value}-1", t, root=root) for t in server_types}
    return FakeRegistry(configs)


def _item(path, hints=None):
    return ProcessableItem(canonical_path=path, server_id="plex-1", item_id_by_server=hints or {}, title="R&M S01E01")


def _run(ctx, media, publishers, probe=None, stage="check", probe_effect=None, hints=None, **kwargs):
    probe_kwargs = {"side_effect": probe_effect} if probe_effect else {"return_value": probe or _probe()}
    with (
        patch.object(pipeline, "probe_media", **probe_kwargs) as probe_mock,
        patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: publishers.get(cfg.id)),
    ):
        fn = check_item if stage == "check" else process_item
        out = fn(_item(media, hints), ctx=ctx, **kwargs)
    return out, probe_mock


def _state(store, path, sid):
    return store.get_publish_state(store.get_file(path).id, sid)


def _rows(out):
    return {r["server_id"]: r for r in out.publisher_rows}


class TestOwners:
    # The ownership matrix (library, server, markers switch, Plex confirmation, sports, selection, exclusions) lives in
    # test_marker_ownership.py; these rows check how the pipeline uses the rule.
    @pytest.mark.parametrize(
        "mutate",
        [lambda reg, media: reg.configs_by_id["plex-1"].markers.update({"enabled": False})],
        ids=["markers-off"],
    )
    def test_no_marker_owner_cells(self, store, media, mutate):
        reg = _registry(media, ServerType.PLEX)
        mutate(reg, media)
        ctx = _ctx(store, reg)
        out, probe = _run(ctx, media, {"plex-1": ready_publisher()})
        assert out.outcome_key == FileOutcome.NO_OWNERS.value
        assert out.publisher_rows == []
        probe.assert_not_called()
        assert store.get_file(media) is None  # nothing detected for a file without an enabled owner
        assert all(c.calls == [] for c in ctx.clients.values())

    def test_owner_without_a_publisher_is_skipped_not_failed(self, store, media):
        # Emby has no publisher until phase 2: the owner counts, its row says why nothing was written.
        reg = _registry(media, ServerType.EMBY)
        out, _ = _run(_ctx(store, reg), media, {}, probe=_probe(CHAPTERS_BOTH))
        assert [r["status"] for r in out.publisher_rows] == [ServerStatus.SKIPPED.value]
        assert out.outcome_key == FileOutcome.SKIPPED.value
        assert _state(store, media, "emby-1").status == "skipped"

    def test_many_owners_each_get_a_row_in_registry_order(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN, ServerType.EMBY)
        plex = ready_publisher()
        jf = ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex, "jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert [(r["server_id"], r["server_type"], r["status"]) for r in out.publisher_rows] == [
            ("plex-1", "plex", ServerStatus.WRITTEN.value),
            ("jellyfin-1", "jellyfin", ServerStatus.WRITTEN.value),
            ("emby-1", "emby", ServerStatus.SKIPPED.value),
        ]
        for pub, sid in ((plex, "plex-1"), (jf, "jellyfin-1")):
            assert pub.write.call_args.args == (f"item-{sid}", [INTRO_CH, CREDITS_CH])
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_owner_excluded_on_one_server_is_neither_read_nor_published_there(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["plex-1"].exclude_paths.append({"value": r"Rick and Morty", "type": "regex"})
        reader = MagicMock(return_value=[])
        factory_calls = []

        def factory(server, cfg, **kw):
            factory_calls.append(cfg.id)
            return ready_publisher("jellyfin_bridge")

        with (
            patch.object(pipeline, "probe_media", return_value=_probe()),
            patch.object(pipeline, "publisher_for", side_effect=factory),
            patch.object(pipeline, "read_server_markers", reader),
        ):
            out = check_item(_item(media), ctx=_ctx(store, reg))
        assert list(_rows(out)) == ["jellyfin-1"]
        assert factory_calls == ["jellyfin-1"]
        assert [c.args[1].id for c in reader.call_args_list] == ["jellyfin-1"]
        reg.get("plex-1").resolve_remote_path_to_item_id.assert_not_called()

    def test_server_whose_client_could_not_be_built_is_not_an_owner(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.get("plex-1")
        reg.servers_by_id["plex-1"] = None
        reg.get = lambda sid: reg.servers_by_id.get(sid) if sid == "plex-1" else FakeRegistry.get(reg, sid)
        jf = ready_publisher("jellyfin_bridge")
        out, _ = _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert [r["server_id"] for r in out.publisher_rows] == ["jellyfin-1"]

    def test_owner_with_markers_off_gets_no_row_while_the_other_publishes(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["plex-1"].markers["enabled"] = False
        jf = ready_publisher("jellyfin_bridge")
        out, _ = _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert [r["server_id"] for r in out.publisher_rows] == ["jellyfin-1"]
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert store.get_publish_state(store.get_file(media).id, "plex-1") is None

    @pytest.mark.parametrize(
        ("libraries", "library_ids", "owner"),
        [(lambda root: [Library("1", "Media", (root,)), Library("2", "TV", (root + "/tv",))], ["2"], True)],
        ids=["overlapping-inner-ticked"],
    )
    def test_markers_ownership_uses_every_matching_library(self, store, media, libraries, library_ids, owner):
        reg = _registry(media, ServerType.PLEX)
        cfg = reg.configs_by_id["plex-1"]
        cfg.libraries = libraries(_media_root(media))
        cfg.markers["library_ids"] = library_ids
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        if owner:
            assert [r["server_id"] for r in out.publisher_rows] == ["plex-1"]  # one row however many libraries match
            assert plex.write.call_args.args == ("item-plex-1", [INTRO_CH, CREDITS_CH])
        else:
            assert out.outcome_key == FileOutcome.NO_OWNERS.value
            plex.write.assert_not_called()

    def test_server_markers_are_read_from_a_library_with_previews_off(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        jf_cfg = reg.configs_by_id["jellyfin-1"]
        jf_cfg.libraries = [Library("7", "TV", (_media_root(media),), enabled=False)]
        jf_cfg.markers["enabled"] = False
        reader = MagicMock(return_value=[])
        with patch.object(pipeline, "read_server_markers", reader):
            _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        assert [(c.args[1].id, c.args[2]) for c in reader.call_args_list] == [
            ("plex-1", "item-plex-1"),
            ("jellyfin-1", "item-jellyfin-1"),
        ]

    @pytest.mark.parametrize(
        ("stype", "prepare", "message"),
        [
            (ServerType.PLEX, lambda server: None, "Plex database not found"),
            (ServerType.JELLYFIN, lambda server: setattr(server.get_bridge_info, "return_value", None), "reach"),
            (ServerType.EMBY, lambda server: None, "Not supported"),
        ],
        ids=["plex-no-db", "jellyfin-unreachable", "emby-no-publisher"],
    )
    def test_real_publisher_factory_per_vendor(self, store, media, stype, prepare, message):
        reg = _registry(media, stype)
        server = reg.get(f"{stype.value}-1")
        prepare(server)
        with patch.object(pipeline, "probe_media", return_value=_probe(CHAPTERS_BOTH)):
            out = check_item(_item(media), ctx=_ctx(store, reg))
        row = out.publisher_rows[0]
        assert row["status"] == ServerStatus.SKIPPED.value and message in row["message"]
        assert out.outcome_key == FileOutcome.SKIPPED.value
        assert _state(store, media, f"{stype.value}-1").status == "skipped"
        server.put_bridge_markers.assert_not_called()


class TestItemIdLookupScope:
    """Item ids are looked up in the libraries that hold the file, whatever their preview opt-in (audit C MED-1)."""

    @pytest.mark.parametrize("previews", [False, True], ids=["previews-off", "previews-on"])
    @pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.JELLYFIN, ServerType.EMBY])
    def test_lookup_is_scoped_to_the_matching_libraries(self, store, media, stype, previews):
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        root = _media_root(media)
        reg.configs_by_id[sid].libraries = [
            Library("1", "Movies", ("/elsewhere/movies",), enabled=True),
            Library("2", "TV Shows", (root,), enabled=previews),
            Library("3", "TV (4K)", (root,), enabled=previews),
        ]
        publishers = {} if stype is ServerType.EMBY else {sid: ready_publisher()}
        # No chapters: the evidence search reaches the server's own markers, so Emby (no publisher) is asked too.
        _run(_ctx(store, reg), media, publishers, probe=_probe())
        calls = reg.get(sid).resolve_remote_path_to_item_id.call_args_list
        assert [(c.args, c.kwargs) for c in calls] == [((media,), {"library_ids": ["2", "3"]})]

    def test_plex_markers_only_library_resolves_at_the_plex_boundary(self, store, media):
        from media_preview_generator.servers.plex import PlexServer

        reg = _registry(media, ServerType.PLEX)
        cfg = reg.configs_by_id["plex-1"]
        cfg.libraries = [
            Library("1", "Movies", ("/elsewhere/movies",), enabled=True),
            Library("2", "TV Shows", (_media_root(media),), enabled=False),  # Plex makes its own thumbnails here
        ]
        movies, tv = MagicMock(key=1, METADATA_TYPE="movie"), MagicMock(key=2, METADATA_TYPE="episode")
        episode = MagicMock(ratingKey=42)
        episode.media = [MagicMock(parts=[MagicMock(file=media)])]
        plex = MagicMock()
        plex.library.sections.return_value = [movies, tv]
        plex.fetchItems.side_effect = lambda ekey: [episode] if ekey.startswith("/library/sections/2/") else []
        server = PlexServer(cfg)
        server._plex = plex
        reg.servers_by_id["plex-1"] = server
        publisher = ready_publisher()
        out, _ = _run(_ctx(store, reg), media, {"plex-1": publisher}, probe=_probe(CHAPTERS_BOTH))
        assert _rows(out)["plex-1"]["status"] == ServerStatus.WRITTEN.value
        assert publisher.write.call_args.args == ("42", [INTRO_CH, CREDITS_CH])


class TestIdentityAndProbe:
    def test_missing_file(self, store, tmp_path):
        path = str(tmp_path / "media" / "gone.mkv")
        reg = _registry(path, ServerType.PLEX)
        plex = ready_publisher()
        out, probe = _run(_ctx(store, reg), path, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.FILE_NOT_FOUND.value
        probe.assert_not_called()
        plex.write.assert_not_called()
        assert store.get_file(path) is None

    def test_directory_at_the_path_is_not_a_file(self, store, tmp_path):
        path = tmp_path / "media" / "folder.mkv"
        path.mkdir(parents=True)
        reg = _registry(str(path), ServerType.PLEX)
        out, probe = _run(_ctx(store, reg), str(path), {"plex-1": ready_publisher()})
        assert out.outcome_key == FileOutcome.FILE_NOT_FOUND.value
        probe.assert_not_called()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_unreadable_folder_fails_instead_of_not_found(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        folder = os.path.dirname(media)
        os.chmod(folder, 0)
        try:
            out, probe = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        finally:
            os.chmod(folder, 0o755)
        assert out.outcome_key == FileOutcome.FAILED.value and "PermissionError" in out.message
        probe.assert_not_called()

    def test_probe_error_fails_item(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        with (
            patch.object(pipeline, "probe_media", side_effect=ProbeError("bad file")),
            patch.object(pipeline, "publisher_for", return_value=plex),
        ):
            out = check_item(_item(media), ctx=_ctx(store, reg))
        assert out.outcome_key == "failed" and "bad file" in out.message
        plex.write.assert_not_called()
        assert store.get_file(media) is None

    def test_probe_uses_the_context_ffprobe(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        ctx = _ctx(store, reg)
        ctx.ffprobe = "/usr/lib/jellyfin-ffmpeg/ffprobe"
        _, probe = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))
        probe.assert_called_once_with(media, ffprobe="/usr/lib/jellyfin-ffmpeg/ffprobe")

    def test_unknown_duration_fails_without_publishing_and_is_probed_again(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH, duration=None))
        assert out.outcome_key == FileOutcome.FAILED.value and "duration" in out.message
        plex.write.assert_not_called()
        out, probe = _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert probe.call_count == 1 and out.outcome_key == FileOutcome.PUBLISHED.value

    def test_unchanged_file_is_not_probed_twice_and_changed_file_is(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        pubs = {"plex-1": ready_publisher()}
        ctx = _ctx(store, reg)
        _, probe1 = _run(ctx, media, pubs, probe=_probe(CHAPTERS_BOTH))
        _, probe2 = _run(ctx, media, pubs, probe=_probe(CHAPTERS_BOTH))
        assert probe1.call_count == 1 and probe2.call_count == 0
        os.utime(media, ns=(1, 2))
        _, probe3 = _run(ctx, media, pubs, probe=_probe(CHAPTERS_BOTH))
        assert probe3.call_count == 1

    def test_force_probes_an_unchanged_file_again(self, store, media):
        # Chapter names are re-read on re-detect, so a newer chapter-name rule reaches already analysed files.
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH[:3]))
        assert plex.write.call_args.args[1] == [INTRO_CH]
        _, probe = _run(_ctx(store, reg, force=True), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert probe.call_count == 1
        assert plex.write.call_args.args[1] == [INTRO_CH, CREDITS_CH]

    def test_changed_file_republishes_with_previous_markers(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(5, 6))
        new_chapters = (
            Chapter(0, 10_000, "Chapter 1"),
            Chapter(10_000, 40_000, "Intro"),
            Chapter(40_000, None, "Chapter 2"),
        )
        _run(ctx, media, {"plex-1": plex}, probe=_probe(new_chapters))
        args, kwargs = plex.write.call_args
        assert args[1] == [Marker(T.INTRO, 10_000, 40_000, ("chapters",))]
        assert [m.type for m in kwargs["previous"]] == [T.INTRO, T.CREDITS]

    def test_changed_file_with_identical_markers_is_written_again_to_every_server(self, store, media):
        # Jellyfin drops segments when a file is replaced, so the per-server hash must not skip the re-publish.
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge")
        pubs = {"plex-1": plex, "jellyfin-1": jf}
        ctx = _ctx(store, reg)
        _run(ctx, media, pubs, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(5, 6))
        out, _ = _run(ctx, media, pubs, probe=_probe(CHAPTERS_BOTH))
        for pub, sid in ((plex, "plex-1"), (jf, "jellyfin-1")):
            assert pub.write.call_count == 2
            args, kwargs = pub.write.call_args
            assert args == (f"item-{sid}", [INTRO_CH, CREDITS_CH])
            assert kwargs["previous"] == [INTRO_CH, CREDITS_CH]
        assert {r["status"] for r in out.publisher_rows} == {ServerStatus.WRITTEN.value}

    def test_file_changed_during_analysis_is_detected_again_before_publishing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        new_chapters = (Chapter(0, 10_000, "Chapter 1"), Chapter(10_000, 40_000, "Intro"), Chapter(40_000, None, "B"))
        probes = []

        def probe(path, *, ffprobe):
            probes.append(path)
            if len(probes) == 1:
                os.utime(path, ns=(7, 7))  # e.g. Sonarr replaced the file while chapters were read
                return _probe(CHAPTERS_BOTH)
            return _probe(new_chapters)

        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe_effect=probe)
        assert len(probes) == 2
        plex.write.assert_called_once()
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 10_000, 40_000, ("chapters",))]
        assert store.get_file(media).mtime_ns == 7
        assert _state(store, media, "plex-1").markers == (Marker(T.INTRO, 10_000, 40_000, ("chapters",)),)
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_file_changed_between_two_servers_restarts_before_the_next_write(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge")
        order = []

        def plex_write(*args, **kwargs):
            order.append("plex")
            if len(order) == 1:
                os.utime(media, ns=(9, 9))
            return args[1]

        plex.write.side_effect = plex_write
        jf.write.side_effect = lambda *a, **k: order.append("jellyfin") or a[1]
        out, probe = _run(_ctx(store, reg), media, {"plex-1": plex, "jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert order == ["plex", "plex", "jellyfin"]  # jellyfin never received markers for the replaced file
        assert probe.call_count == 2
        assert {r["status"] for r in out.publisher_rows} == {ServerStatus.WRITTEN.value}
        assert store.get_file(media).mtime_ns == 9

    def test_file_that_keeps_changing_fails_without_publishing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        stamps = iter(range(10, 100))

        def probe(path, *, ffprobe):
            os.utime(path, ns=(next(stamps), next(stamps)))
            return _probe(CHAPTERS_BOTH)

        out, probe_mock = _run(_ctx(store, reg), media, {"plex-1": plex}, probe_effect=probe)
        assert out.outcome_key == FileOutcome.FAILED.value and "changing" in out.message
        assert probe_mock.call_count == pipeline.MAX_ATTEMPTS
        plex.write.assert_not_called()

    def test_file_removed_before_publishing_is_not_found(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()

        def probe(path, *, ffprobe):
            os.remove(path)
            return _probe(CHAPTERS_BOTH)

        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe_effect=probe)
        assert out.outcome_key == FileOutcome.FILE_NOT_FOUND.value
        plex.write.assert_not_called()


EPISODE_IDS = {"kind": "episode", "tmdb": None, "imdb": "tt7654321", "tvdb": "999", "season": 1, "episode": 3}
MOVIE_IDS = {"kind": "movie", "tmdb": "9999", "imdb": "tt0114709", "tvdb": None, "season": None, "episode": None}
UNKNOWN_IDS = {"kind": "unknown", "tmdb": None, "imdb": None, "tvdb": None, "season": None, "episode": None}


class TestKind:
    """A path that reads as a movie is confirmed by the owning server; intros and recaps exist for episodes only."""

    def _run_kind(self, store, path, server_ids, *, chapters=CHAPTERS_OPENING, types=("jellyfin",), clients=None):
        reg = _registry(path, *(ServerType(name) for name in types))
        pubs = {sid: ready_publisher(sid, ("intro", "credits", "recap")) for sid in reg.configs_by_id}
        for sid, answer in server_ids.items():
            if answer == "no-item":
                reg.get(sid).resolve_remote_path_to_item_id.return_value = None
            else:
                reg.get(sid).get_external_ids.return_value = answer
        ctx = _ctx(
            store,
            reg,
            clients=clients,
            settings_raw={"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"recap": True}},
        )
        out, _ = _run(ctx, path, pubs, probe=_probe(chapters))
        return out, reg, pubs, ctx

    def test_path_movie_that_the_server_calls_an_episode_gets_intros(self, store, ambiguous):
        out, reg, pubs, _ = self._run_kind(store, ambiguous, {"jellyfin-1": EPISODE_IDS})
        reg.get("jellyfin-1").get_external_ids.assert_called_once_with("item-jellyfin-1")
        assert pubs["jellyfin-1"].write.call_args.args[1] == [OPENING_CH, END_CREDITS_CH]
        rec = store.get_file(ambiguous)
        assert rec.is_movie is False and rec.season_key == os.path.dirname(ambiguous)

    def test_server_episode_ids_replace_the_path_movie_ids(self, store, ambiguous):
        clients = _clients()
        self._run_kind(store, ambiguous, {"jellyfin-1": EPISODE_IDS}, chapters=(), clients=clients)
        assert clients["theintrodb"].calls[0]["ids"] == MediaIds("episode", None, "tt7654321", "999", 1, 3)

    def test_server_confirmed_movie_detects_credits_only_even_with_an_opening_chapter(self, store, ambiguous):
        out, _, pubs, _ = self._run_kind(store, ambiguous, {"jellyfin-1": MOVIE_IDS})
        assert pubs["jellyfin-1"].write.call_args.args[1] == [END_CREDITS_CH]
        rec = store.get_file(ambiguous)
        assert rec.is_movie is True and rec.season_key is None
        assert store.get_decisions(rec.id)[T.INTRO].status is DecisionStatus.DISABLED
        assert store.get_decisions(rec.id)[T.RECAP].status is DecisionStatus.DISABLED

    def test_server_confirmed_movie_keeps_path_ids_first(self, store, ambiguous):
        clients = _clients()
        self._run_kind(store, ambiguous, {"jellyfin-1": MOVIE_IDS}, chapters=(), clients=clients)
        assert clients["theintrodb"].calls[0]["ids"] == MediaIds("movie", "1234", "tt0114709", None, None, None)

    @pytest.mark.parametrize("answer", [None, "no-item"], ids=["server-cant-answer", "not-in-server-yet"])
    def test_unconfirmed_path_movie_keeps_the_movie_kind_and_skips_online_lookups(self, store, ambiguous, answer):
        clients = _clients()
        out, reg, pubs, _ = self._run_kind(store, ambiguous, {"jellyfin-1": answer}, clients=clients)
        if answer is None:
            assert pubs["jellyfin-1"].write.call_args.args[1] == [END_CREDITS_CH]
        else:
            reg.get("jellyfin-1").get_external_ids.assert_not_called()
        assert store.get_file(ambiguous).is_movie is True
        assert all(c.calls == [] for c in clients.values())

    def test_unconfirmed_movie_is_looked_up_once_a_server_confirms_it(self, store, ambiguous):
        clients = _clients()
        self._run_kind(store, ambiguous, {"jellyfin-1": None}, chapters=(), clients=clients)
        assert clients["theintrodb"].calls == []
        self._run_kind(store, ambiguous, {"jellyfin-1": EPISODE_IDS}, chapters=(), clients=clients)
        assert clients["theintrodb"].calls[0]["ids"].is_episode
        assert store.get_file(ambiguous).is_movie is False

    def test_second_server_answers_when_the_first_cant(self, store, ambiguous):
        out, reg, pubs, _ = self._run_kind(
            store, ambiguous, {"plex-1": None, "jellyfin-1": EPISODE_IDS}, types=("plex", "jellyfin")
        )
        reg.get("plex-1").get_external_ids.assert_called_once_with("item-plex-1")
        assert pubs["jellyfin-1"].write.call_args.args[1] == [OPENING_CH, END_CREDITS_CH]

    def test_server_unknown_kind_wins_over_path_movie(self, store, ambiguous):
        clients = _clients()
        out, _, pubs, _ = self._run_kind(store, ambiguous, {"jellyfin-1": UNKNOWN_IDS}, clients=clients)
        assert pubs["jellyfin-1"].write.call_args.args[1] == [END_CREDITS_CH]
        assert store.get_file(ambiguous).is_movie is False
        assert all(c.calls == [] for c in clients.values())  # no ids to ask with

    def test_server_unknown_kind_is_never_looked_up(self, store, ambiguous):
        clients = _clients()
        out, _, _, _ = self._run_kind(store, ambiguous, {"jellyfin-1": UNKNOWN_IDS}, chapters=(), clients=clients)
        assert all(c.calls == [] for c in clients.values())
        assert out.outcome_key == FileOutcome.NO_MARKERS.value

    def test_server_exception_counts_as_no_answer(self, store, ambiguous):
        reg = _registry(ambiguous, ServerType.JELLYFIN)
        reg.get("jellyfin-1").get_external_ids.side_effect = RuntimeError("HTTP 500")
        jf = ready_publisher("jellyfin_bridge")
        out, _ = _run(_ctx(store, reg), ambiguous, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_OPENING))
        assert jf.write.call_args.args[1] == [END_CREDITS_CH]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_hint_item_id_confirms_the_kind_without_a_path_lookup(self, store, ambiguous):
        reg = _registry(ambiguous, ServerType.JELLYFIN)
        reg.get("jellyfin-1").get_external_ids.return_value = EPISODE_IDS
        jf = ready_publisher("jellyfin_bridge")
        _run(
            _ctx(store, reg), ambiguous, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_OPENING), hints={"jellyfin-1": "abc"}
        )
        reg.get("jellyfin-1").get_external_ids.assert_called_once_with("abc")
        reg.get("jellyfin-1").resolve_remote_path_to_item_id.assert_not_called()
        assert jf.write.call_args.args[0] == "abc"

    def test_server_kind_is_asked_once_while_the_file_is_unchanged(self, store, ambiguous):
        reg = _registry(ambiguous, ServerType.JELLYFIN)
        server = reg.get("jellyfin-1")
        server.get_external_ids.return_value = EPISODE_IDS
        jf = ready_publisher("jellyfin_bridge")
        _run(_ctx(store, reg), ambiguous, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_OPENING))
        out, _ = _run(_ctx(store, reg), ambiguous, {"jellyfin-1": jf})
        assert server.get_external_ids.call_count == 1
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value  # still an episode: the intro stays
        assert store.get_server_kind(store.get_file(ambiguous).id) == "episode"
        os.utime(ambiguous, ns=(3, 3))
        server.get_external_ids.return_value = MOVIE_IDS
        _run(_ctx(store, reg), ambiguous, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_OPENING))
        assert server.get_external_ids.call_count == 2
        assert jf.write.call_args.args[1] == [END_CREDITS_CH]

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            (EPISODE_IDS, MediaIds("episode", None, "tt7654321", "999", 1, 3)),
            # A cached movie keeps the path's tmdb id first and still gets the server's imdb id.
            (MOVIE_IDS, MediaIds("movie", "1234", "tt0114709", None, None, None)),
        ],
        ids=["episode", "movie"],
    )
    def test_cached_server_kind_still_completes_ids_when_a_lookup_needs_them(self, store, ambiguous, answer, expected):
        reg = _registry(ambiguous, ServerType.JELLYFIN)
        server = reg.get("jellyfin-1")
        server.get_external_ids.return_value = answer
        clients = _clients()
        _run(
            _ctx(store, reg, clients=clients),
            ambiguous,
            {"jellyfin-1": ready_publisher("jellyfin_bridge")},
            probe=_probe(CHAPTERS_OPENING),
        )
        assert clients["theintrodb"].calls == []
        _run(
            _ctx(store, reg, clients=clients, force=True),
            ambiguous,
            {"jellyfin-1": ready_publisher("jellyfin_bridge")},
            probe=_probe(CHAPTERS_OPENING),
        )
        assert server.get_external_ids.call_count == 2  # the kind came from the store; the ids had to be fetched
        assert clients["theintrodb"].calls[0]["ids"] == expected

    def test_cached_unknown_kind_is_never_looked_up(self, store, ambiguous):
        reg = _registry(ambiguous, ServerType.JELLYFIN)
        reg.get("jellyfin-1").get_external_ids.return_value = UNKNOWN_IDS
        clients = _clients()
        for _ in range(2):
            _run(_ctx(store, reg, clients=clients), ambiguous, {"jellyfin-1": ready_publisher("jellyfin_bridge")})
        assert store.get_server_kind(store.get_file(ambiguous).id) == "unknown"
        assert all(c.calls == [] for c in clients.values())

    @pytest.mark.parametrize("cached", ["unknown", "movie"])
    def test_cached_server_kind_is_used_without_asking(self, store, ambiguous, cached):
        reg = _registry(ambiguous, ServerType.JELLYFIN)
        server = reg.get("jellyfin-1")
        server.get_external_ids.return_value = UNKNOWN_IDS if cached == "unknown" else MOVIE_IDS
        clients = _clients()
        jf = ready_publisher("jellyfin_bridge")
        _run(_ctx(store, reg, clients=clients), ambiguous, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_OPENING))
        calls_after_first = [len(c.calls) for c in clients.values()]
        out, _ = _run(_ctx(store, reg, clients=clients), ambiguous, {"jellyfin-1": jf})
        assert server.get_external_ids.call_count == 1
        assert store.get_file(ambiguous).is_movie is (cached == "movie")
        assert [len(c.calls) for c in clients.values()] == calls_after_first
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value

    def test_path_episode_is_not_confirmed_when_nothing_needs_ids(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        reg.get("plex-1").get_external_ids.return_value = MOVIE_IDS
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        reg.get("plex-1").get_external_ids.assert_not_called()
        assert plex.write.call_args.args[1] == [INTRO_CH, CREDITS_CH]

    def test_path_episode_ignores_server_ids_of_another_kind(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        reg.get("plex-1").get_external_ids.return_value = MOVIE_IDS
        clients = _clients()
        _run(_ctx(store, reg, clients=clients), media, {"plex-1": ready_publisher()})
        assert clients["introdb"].calls[0]["ids"] == MediaIds("episode", None, None, "275274", 1, 1)

    def test_path_episode_with_an_imdb_id_never_asks_the_server_for_ids(self, store, tmp_path):
        folder = tmp_path / "media" / "tv" / "Show (2020) {imdb-tt1234567}" / "Season 01"
        folder.mkdir(parents=True)
        path = str(folder / "Show - S01E02.mkv")
        open(path, "wb").close()
        reg = _registry(path, ServerType.PLEX)
        reg.get("plex-1").get_external_ids.return_value = EPISODE_IDS
        clients = _clients()
        _run(_ctx(store, reg, clients=clients), path, {"plex-1": ready_publisher()})
        reg.get("plex-1").get_external_ids.assert_not_called()
        assert clients["introdb"].calls[0]["ids"] == MediaIds("episode", None, "tt1234567", None, 1, 2)

    def test_path_without_kind_asks_the_server(self, store, tmp_path):
        folder = tmp_path / "media" / "tv" / "Anime Show"
        folder.mkdir(parents=True)
        path = str(folder / "Anime Show - 001.mkv")
        open(path, "wb").close()
        out, _, pubs, _ = self._run_kind(store, path, {"jellyfin-1": EPISODE_IDS})
        assert pubs["jellyfin-1"].write.call_args.args[1] == [OPENING_CH, END_CREDITS_CH]

    def test_movie_detects_credits_only(self, store, tmp_path):
        folder = tmp_path / "media" / "movies" / "Toy Story (1995) {tmdb-862}"
        folder.mkdir(parents=True)
        path = folder / "Toy Story (1995) {imdb-tt0114709}.mkv"
        path.write_bytes(b"x")
        reg = _registry(str(path), ServerType.PLEX)
        chapters = (
            Chapter(0, 60_000, "Intro"),
            Chapter(60_000, 4_629_000, "Movie"),
            Chapter(4_629_000, None, "End Credits"),
        )
        plex = ready_publisher()
        with (
            patch.object(pipeline, "probe_media", return_value=MediaProbe(4_866_050, chapters)),
            patch.object(pipeline, "publisher_for", return_value=plex),
        ):
            check_item(_item(str(path)), ctx=_ctx(store, reg))
        assert plex.write.call_args.args[1] == [Marker(T.CREDITS, 4_629_000, 4_866_050, ("chapters",))]

    def test_path_without_ids_uses_server_metadata(self, store, tmp_path):
        folder = tmp_path / "media" / "tv" / "Some Show" / "Season 02"
        folder.mkdir(parents=True)
        path = folder / "Some Show - S02E05.mkv"
        path.write_bytes(b"x")
        reg = _registry(str(path), ServerType.PLEX)
        reg.get("plex-1").get_external_ids.return_value = {
            "kind": "episode",
            "tmdb": "1",
            "imdb": "tt1",
            "tvdb": None,
            "season": 2,
            "episode": 5,
        }
        client = FakeClient(NO_DATA)
        clients = {"theintrodb": client, "introdb": FakeClient(NO_DATA), "skipdb": FakeClient(NO_DATA)}
        with (
            patch.object(pipeline, "probe_media", return_value=_probe()),
            patch.object(pipeline, "publisher_for", return_value=ready_publisher()),
        ):
            check_item(_item(str(path)), ctx=_ctx(store, reg, clients=clients))
        reg.get("plex-1").get_external_ids.assert_called_once_with("item-plex-1")
        assert client.calls[0]["ids"] == MediaIds("episode", "1", "tt1", None, 2, 5)


DECISION_CELLS = ("decided", "review", "none", "disabled")


class TestEvidenceAndDecisions:
    def test_chapters_decide_both_and_skip_online_lookups(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        ctx = _ctx(store, reg)
        with patch.object(pipeline, "read_server_markers") as reader:
            out, _ = _run(ctx, media, {"plex-1": plex, "jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert all(c.calls == [] for c in ctx.clients.values())
        reader.assert_not_called()
        for pub, sid in ((plex, "plex-1"), (jf, "jellyfin-1")):
            args, kwargs = pub.write.call_args
            assert args == (f"item-{sid}", [INTRO_CH, CREDITS_CH])
            assert kwargs == {"previous": [], "own_previous": None, "duration_ms": DUR, "canonical_path": media}
        assert {r["server_id"]: r["status"] for r in out.publisher_rows} == {
            "plex-1": ServerStatus.WRITTEN.value,
            "jellyfin-1": ServerStatus.WRITTEN.value,
        }
        row = _rows(out)["plex-1"]
        assert row["canonical_path"] == media and row["adapter_name"] == "plex_db" and row["server_name"] == "PLEX-1"
        assert _state(store, media, "plex-1").markers == (INTRO_CH, CREDITS_CH)
        assert _state(store, media, "plex-1").status == "written"
        assert _state(store, media, "plex-1").item_id == "item-plex-1"
        assert "intro" in out.message and "credits" in out.message

    def test_second_run_is_up_to_date_without_writes(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        plex.write.reset_mock()
        out, _ = _run(ctx, media, {"plex-1": plex})
        plex.write.assert_not_called()
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        assert out.publisher_rows[0]["status"] == ServerStatus.UP_TO_DATE.value

    @pytest.mark.parametrize("intro", DECISION_CELLS)
    @pytest.mark.parametrize("credits", DECISION_CELLS)
    def test_per_type_decision_matrix(self, store, media, intro, credits):
        chapters = [Chapter(0, 126_771, "Chapter 1"), Chapter(157_068, 1_295_324, "Chapter 2")]
        online = []
        if intro == "decided":
            chapters.append(Chapter(126_771, 157_068, "Intro"))
        if credits == "decided":
            chapters.append(Chapter(1_295_324, None, "Credits"))
        if intro == "review":
            online.append(TIDB_INTRO)
        if credits == "review":
            online.append(TIDB_CREDITS)
        chapters.sort(key=lambda c: c.start_ms)
        clients = _clients(theintrodb=LookupResult("ok", tuple(online)) if online else NO_DATA)
        raw = {
            "sources": [{"id": "theintrodb", "enabled": True}],
            "detect": {"intro": intro != "disabled", "credits": credits != "disabled"},
        }
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        out, _ = _run(
            _ctx(store, reg, clients=clients, settings_raw=raw), media, {"plex-1": plex}, probe=_probe(chapters)
        )

        expected = [m for m, cell in ((INTRO_CH, intro), (CREDITS_CH, credits)) if cell == "decided"]
        needs_review = "review" in (intro, credits)
        if expected:
            status, outcome = ServerStatus.WRITTEN, FileOutcome.PUBLISHED
            assert plex.write.call_args.args == ("item-plex-1", expected)
        else:
            plex.write.assert_not_called()
            status = ServerStatus.NEEDS_REVIEW if needs_review else ServerStatus.NONE
            outcome = FileOutcome.NEEDS_REVIEW if needs_review else FileOutcome.NO_MARKERS
        assert [r["status"] for r in out.publisher_rows] == [status.value]
        assert out.outcome_key == outcome.value
        still_open = any(cell in ("review", "none") for cell in (intro, credits))
        assert len(clients["theintrodb"].calls) == (1 if still_open else 0)
        statuses = {t: d.status for t, d in store.get_decisions(store.get_file(media).id).items()}
        by_cell = {
            "decided": DecisionStatus.DECIDED,
            "review": DecisionStatus.NEEDS_REVIEW,
            "none": DecisionStatus.NO_EVIDENCE,
            "disabled": DecisionStatus.DISABLED,
        }
        assert statuses[T.INTRO] is by_cell[intro] and statuses[T.CREDITS] is by_cell[credits]

    def test_recap_is_detected_for_episodes_when_turned_on(self, store, media):
        chapters = (Chapter(0, 40_000, "Previously"), *CHAPTERS_BOTH[1:])
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge", ("intro", "credits", "recap"))
        _run(
            _ctx(store, reg, settings_raw={"detect": {"recap": True}}),
            media,
            {"jellyfin-1": jf},
            probe=_probe(chapters),
        )
        assert jf.write.call_args.args[1] == [Marker(T.RECAP, 0, 40_000, ("chapters",)), INTRO_CH, CREDITS_CH]

    def test_needs_review_removes_a_marker_published_before(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg, clients=_clients(theintrodb=LookupResult("ok", (TIDB_INTRO,))), settings_raw=INTRO_ONLY)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH[:3]))
        os.utime(media, ns=(3, 3))
        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe())
        assert plex.write.call_args.args == ("item-plex-1", [])
        assert plex.write.call_args.kwargs["previous"] == [INTRO_CH]
        assert out.publisher_rows[0]["status"] == ServerStatus.WRITTEN.value
        assert _state(store, media, "plex-1").markers == ()
        out, _ = _run(ctx, media, {"plex-1": plex})
        assert plex.write.call_count == 2
        assert out.publisher_rows[0]["status"] == ServerStatus.NEEDS_REVIEW.value
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value

    def test_settings_change_applies_to_stored_evidence_without_new_lookups(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        medium = {**INTRO_ONLY, "publish_when": "medium"}
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=medium), media, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert len(clients["theintrodb"].calls) == 1
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 127_894, 156_824, ("theintrodb",))]

    def test_online_order_early_stop_and_dependent_sources(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        intro_tidb = LookupResult("ok", (TIDB_INTRO,))
        intro_idb = LookupResult("ok", (Candidate(T.INTRO, 128_000, 160_000, Source.INTRODB),))
        intro_skip = LookupResult(
            "ok",
            (
                Candidate(T.INTRO, 129_000, 157_800, Source.SKIPDB),
                Candidate(T.CREDITS, 1_296_000, 1_320_000, Source.SKIPDB),
            ),
        )
        clients = _clients(theintrodb=intro_tidb, introdb=intro_idb, skipdb=intro_skip)
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY)
        plex = ready_publisher()
        out, _ = _run(ctx, media, {"plex-1": plex})
        # IntroDB and TheIntroDB are one source, so SkipDB is still asked; it agrees and the search stops there.
        assert [len(c.calls) for c in clients.values()] == [1, 1, 1]
        call = clients["theintrodb"].calls[0]
        assert call["ids"] == MediaIds("episode", None, None, "275274", 1, 1)
        assert call["duration_ms"] == DUR and call["priority"] == 2
        # The agreed end is TheIntroDB's (first in source order); the start is the latest of the agreeing sources.
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 129_000, 156_824, ("theintrodb", "introdb", "skipdb"))]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_stops_querying_once_everything_is_decided(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        raw = {**INTRO_ONLY, "publish_when": "medium"}
        with patch.object(pipeline, "read_server_markers") as reader:
            _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, {"plex-1": ready_publisher()})
        assert [len(c.calls) for c in clients.values()] == [1, 0, 0]
        reader.assert_not_called()

    def test_single_source_at_high_needs_review_and_writes_nothing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        plex.write.assert_not_called()
        assert out.publisher_rows[0]["status"] == ServerStatus.NEEDS_REVIEW.value
        assert "needs review" in out.message

    def test_disabled_source_is_not_queried_and_its_stored_evidence_is_ignored(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        st = os.stat(media)
        rec = store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns),
            duration_ms=DUR,
            season_key=os.path.dirname(media),
            is_movie=False,
        )
        store.replace_evidence(rec.id, Source.SKIPDB, [Candidate(T.INTRO, 127_000, 157_000, Source.SKIPDB)])
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        raw = {
            "sources": [{"id": "theintrodb", "enabled": True}, {"id": "skipdb", "enabled": False}],
            "detect": {"intro": True, "credits": False},
        }
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, {"plex-1": plex})
        assert clients["skipdb"].calls == []
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value  # skipdb's stored agreement doesn't count

    def test_locks_count_even_when_respect_locks_is_off(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        st = os.stat(media)
        rec = store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        locked = Marker(T.INTRO, 5_000, 30_000, ("user",), locked=True)
        store.lock_marker(rec.id, locked)
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        out, _ = _run(
            _ctx(store, reg, clients=clients, settings_raw={**INTRO_ONLY, "respect_locks": False}),
            media,
            {"plex-1": plex},
        )
        assert all(c.calls == [] for c in clients.values())
        assert store.get_decisions(rec.id)[T.INTRO].status is DecisionStatus.DECIDED
        assert plex.write.call_args.args[1] == [locked]
        assert "needs review" not in out.message

    def test_locked_marker_beats_chapters_and_survives_a_file_change(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        locked = Marker(T.INTRO, 5_000, 30_000, ("user",), locked=True)
        store.lock_marker(store.get_file(media).id, locked)
        out, _ = _run(ctx, media, {"plex-1": plex})
        assert plex.write.call_args.args[1] == [locked, CREDITS_CH]
        os.utime(media, ns=(4, 4))
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert plex.write.call_count == 3
        assert plex.write.call_args.args[1] == [locked, CREDITS_CH]


class TestOnlineLookups:
    @pytest.mark.parametrize(
        ("first", "later", "force", "queried_again"),
        [
            (LookupResult("unavailable", detail="blocked"), timedelta(0), False, True),
            (LookupResult("not_applicable"), timedelta(0), False, True),
            (NO_DATA, timedelta(days=1), False, False),
            (NO_DATA, timedelta(days=14), False, False),
            (NO_DATA, timedelta(days=14, seconds=1), False, True),
            (NO_DATA, timedelta(days=1), True, True),
            (LookupResult("ok", (TIDB_INTRO,)), timedelta(days=30), False, False),
            (LookupResult("ok", (TIDB_INTRO,)), timedelta(0), True, True),
        ],
        ids=[
            "unavailable",
            "not-applicable",
            "no-data-1d",
            "no-data-14d",
            "no-data-after-14d",
            "no-data-force",
            "ok-30d",
            "ok-force",
        ],
    )
    def test_lookup_caching_matrix(self, tmp_path, media, first, later, force, queried_again):
        clock = {"t": datetime(2026, 9, 13, tzinfo=timezone.utc)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX)
        client = FakeClient(first)
        clients = {"theintrodb": client, "introdb": FakeClient(NO_DATA), "skipdb": FakeClient(NO_DATA)}
        now = lambda: clock["t"]  # noqa: E731
        _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY, now=now), media, {"plex-1": ready_publisher()})
        stored = store.evidence_fetched_at(store.get_file(media).id, Source.THEINTRODB)
        assert (stored is not None) == (first.status in ("ok", "no_data"))
        clock["t"] += later
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY, now=now, force=force)
        _run(ctx, media, {"plex-1": ready_publisher()})
        assert len(client.calls) == (2 if queried_again else 1)
        store.close()

    def test_unavailable_answer_keeps_earlier_evidence(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        client = FakeClient(LookupResult("ok", (TIDB_INTRO,)))
        clients = _clients(skipdb=LookupResult("ok", (Candidate(T.INTRO, 129_000, 157_800, Source.SKIPDB),)))
        clients["theintrodb"] = client
        plex = ready_publisher()
        _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": plex})
        client.result = LookupResult("unavailable", detail="TheIntroDB HTTP 503")
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY, force=True), media, {"plex-1": plex})
        assert len(client.calls) == 2
        assert Source.THEINTRODB in {c.source for c in store.get_evidence(store.get_file(media).id)}
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value

    @pytest.mark.parametrize(
        "broken",
        [LookupResult("unavailable", detail="TheIntroDB HTTP 503"), LookupResult("mystery"), RuntimeError("bug")],
        ids=["unavailable", "unknown-status", "exception"],
    )
    def test_a_failing_source_is_not_stored_and_later_sources_still_decide(self, store, media, broken):
        reg = _registry(media, ServerType.PLEX)
        clients = _clients(
            introdb=LookupResult("ok", (Candidate(T.INTRO, 128_000, 157_000, Source.INTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.INTRO, 129_000, 157_800, Source.SKIPDB),)),
        )
        if isinstance(broken, Exception):
            clients["theintrodb"].lookup = MagicMock(side_effect=broken)
        else:
            clients["theintrodb"].result = broken
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": plex})
        assert store.evidence_fetched_at(store.get_file(media).id, Source.THEINTRODB) is None
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_enabled_source_without_a_client_is_skipped(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        clients = _clients()
        del clients["theintrodb"]
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": ready_publisher()})
        assert len(clients["introdb"].calls) == 1 and out.outcome_key == FileOutcome.NO_MARKERS.value

    def test_ids_are_fetched_from_a_server_at_most_once_per_file(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        assert reg.get("plex-1").get_external_ids.call_count == 1
        assert reg.get("jellyfin-1").get_external_ids.call_count == 1  # plex had nothing, so jellyfin was asked
        reg.get("plex-1").resolve_remote_path_to_item_id.assert_called_once_with(media, library_ids=["1"])

    def test_lookups_use_the_job_priority_at_the_time_of_the_lookup(self, store, media):
        other = os.path.join(os.path.dirname(media), "Rick and Morty (2013) - S01E02 - Lawnmower Dog.mkv")
        open(other, "wb").write(b"y" * 10)
        reg = _registry(media, ServerType.PLEX)
        live = {"priority": 3}
        clients = _clients()
        ctx = _ctx(store, reg, clients=clients)
        ctx.priority = lambda: live["priority"]
        _run(ctx, media, {"plex-1": ready_publisher()})
        live["priority"] = 2  # the user raised the backfill to Normal
        _run(ctx, other, {"plex-1": ready_publisher()})
        assert [c["priority"] for c in clients["theintrodb"].calls] == [3, 2]

    def test_cancel_during_a_lookup_stops_before_the_next_source_and_publishing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        cancelled = threading.Event()
        clients = _clients()

        def lookup(ids, *, duration_ms, priority, cancel_check=None):
            cancelled.set()
            return LookupResult("unavailable", detail="TheIntroDB cancelled")

        clients["theintrodb"].lookup = lookup
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg, clients=clients), media, {"plex-1": plex}, cancel_check=cancelled.is_set)
        assert out.outcome_key == FileOutcome.FAILED.value and "cancel" in out.message
        assert clients["introdb"].calls == [] and clients["skipdb"].calls == []
        plex.write.assert_not_called()


class TestServerMarkers:
    def test_server_markers_confirm_online_source_and_are_read_once_before_publishing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex_server = reg.get("plex-1")
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        reader = MagicMock(return_value=[Candidate(T.INTRO, 127_000, 158_000, Source.SERVER_MARKERS, origin="plex-1")])
        plex = ready_publisher()
        with patch.object(pipeline, "read_server_markers", reader):
            out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": plex})
            assert out.outcome_key == FileOutcome.PUBLISHED.value
            reader.assert_called_once_with(plex_server, reg.get_config("plex-1"), "item-plex-1")
            _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY, force=True), media, {"plex-1": plex})
            assert reader.call_count == 1  # never re-read a server we've published to
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 127_894, 156_824, ("theintrodb", "server_markers"))]

    def test_server_markers_read_from_owner_without_markers_enabled(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN, ServerType.EMBY)
        reg.configs_by_id["plex-1"].markers.update({"enabled": False})
        reader = MagicMock(return_value=[])
        with patch.object(pipeline, "read_server_markers", reader):
            _run(_ctx(store, reg), media, {"jellyfin-1": ready_publisher("jellyfin_bridge")})
        assert [(c.args[1].id, c.args[2]) for c in reader.call_args_list] == [
            ("plex-1", "item-plex-1"),
            ("jellyfin-1", "item-jellyfin-1"),
            ("emby-1", "item-emby-1"),
        ]
        stored = {r.origin for r in store.evidence_rows(store.get_file(media).id) if r.source is Source.SERVER_MARKERS}
        assert stored == {"plex-1", "jellyfin-1", "emby-1"}

    def test_server_markers_alone_never_publish(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        reader = MagicMock(return_value=[Candidate(T.INTRO, 127_000, 158_000, Source.SERVER_MARKERS, origin="plex-1")])
        plex = ready_publisher()
        raw = {**INTRO_ONLY, "publish_when": "medium"}
        with patch.object(pipeline, "read_server_markers", reader):
            out, _ = _run(_ctx(store, reg, settings_raw=raw), media, {"plex-1": plex})
        plex.write.assert_not_called()
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value  # a second opinion with nothing to confirm

    def test_after_a_file_change_only_servers_we_never_published_to_are_read_again(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge")
        jf.capability.return_value = CapabilityReport(Capability.NEEDS_PLUGIN, "Install the plugin")
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        reader = MagicMock(return_value=[Candidate(T.INTRO, 127_000, 158_000, Source.SERVER_MARKERS, origin="x")])
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY)
        with patch.object(pipeline, "read_server_markers", reader):
            _run(ctx, media, {"plex-1": plex, "jellyfin-1": jf})
            assert [c.args[1].id for c in reader.call_args_list] == ["plex-1", "jellyfin-1"]
            os.utime(media, ns=(8, 8))
            reader.reset_mock()
            _run(ctx, media, {"plex-1": plex, "jellyfin-1": jf})
        assert [c.args[1].id for c in reader.call_args_list] == ["jellyfin-1"]

    def test_force_reads_again_servers_we_never_published_to(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["jellyfin-1"].markers["enabled"] = False
        reader = MagicMock(return_value=[])
        with patch.object(pipeline, "read_server_markers", reader):
            _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
            _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
            assert reader.call_count == 2  # once per server; the stored answers are reused
            _run(_ctx(store, reg, force=True), media, {"plex-1": ready_publisher()})
        assert reader.call_count == 4

    def test_server_markers_after_an_empty_publish_can_be_read(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        st = os.stat(media)
        rec = store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        store.set_publish_state(rec.id, "plex-1", item_id="item-plex-1", markers=[], status="written")
        reader = MagicMock(return_value=[])
        with patch.object(pipeline, "read_server_markers", reader):
            _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        reader.assert_called_once_with(reg.get("plex-1"), reg.get_config("plex-1"), "item-plex-1")

    @pytest.mark.parametrize(
        "setup",
        [
            lambda reader, server: setattr(reader, "return_value", None),
            lambda reader, server: setattr(reader, "side_effect", RuntimeError("HTTP 500")),
        ],
        ids=["unreadable", "exception"],
    )
    def test_unreadable_server_markers_are_retried_next_run(self, store, media, setup):
        reg = _registry(media, ServerType.PLEX)
        reader = MagicMock()
        setup(reader, reg.get("plex-1"))
        with patch.object(pipeline, "read_server_markers", reader):
            out, _ = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
            assert out.outcome_key == FileOutcome.NO_MARKERS.value
            reader.side_effect, reader.return_value = None, []
            _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
            _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        assert reader.call_count == 2

    def test_versions_sharing_an_item_never_read_our_own_plex_or_emby_markers_back(self, store, tmp_path):
        # Plex (and Emby) can't tell our rows from their own and show one set per item, so once version B is
        # published, version A must not count those markers as a second source. Jellyfin's reader skips ours itself.
        folder = tmp_path / "media" / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
        folder.mkdir(parents=True)
        version_a = str(folder / "Rick and Morty (2013) - S01E01 - Pilot - 1080p.mkv")
        version_b = str(folder / "Rick and Morty (2013) - S01E01 - Pilot - 2160p.mkv")
        for path in (version_a, version_b):
            open(path, "wb").write(b"x" * 10)
        types = (ServerType.PLEX, ServerType.JELLYFIN, ServerType.EMBY)
        reg = _registry(version_a, *types)
        configs = reg.configs_by_id
        for sid in configs:
            reg.get(sid).resolve_remote_path_to_item_id.return_value = "shared-item"
        st = os.stat(version_b)
        rec_b = store.upsert_file(
            FileIdentity(version_b, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        for sid in configs:
            store.set_publish_state(rec_b.id, sid, item_id="shared-item", markers=[INTRO_CH], status="written")
            store.set_item_publish_state(sid, "shared-item", [INTRO_CH], "written")
        reader = MagicMock(return_value=[])
        pubs = {
            "plex-1": ready_publisher(),
            "jellyfin-1": ready_publisher("jellyfin_bridge", ("intro", "credits", "recap")),
        }
        with patch.object(pipeline, "read_server_markers", reader):
            out, _ = _run(
                _ctx(store, reg, settings_raw={"detect": {"recap": True}}), version_a, pubs, probe=_probe(CHAPTERS_BOTH)
            )
        assert [(c.args[1].id, c.args[2]) for c in reader.call_args_list] == [("jellyfin-1", "shared-item")]
        assert reader.call_args.kwargs == {}  # include_ours stays at its default
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    @pytest.mark.parametrize(
        ("answer", "later", "published_first", "read_again"),
        [
            ([], timedelta(hours=12), False, False),
            ([], timedelta(days=1), False, False),
            ([], timedelta(days=1, seconds=1), False, True),
            (
                [Candidate(T.INTRO, 127_000, 158_000, Source.SERVER_MARKERS, origin="plex-1")],
                timedelta(days=30),
                False,
                False,
            ),
            ([], timedelta(days=2), True, False),
        ],
        ids=["empty-12h", "empty-exactly-a-day", "empty-after-a-day", "markers-30d", "empty-but-we-published"],
    )
    def test_empty_server_answers_are_read_again_after_a_day(
        self, tmp_path, media, answer, later, published_first, read_again
    ):
        # Plex may detect the intro overnight after the webhook job read nothing.
        clock = {"t": datetime(2026, 9, 13, tzinfo=timezone.utc)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX)
        reader = MagicMock(return_value=answer)
        raw = {"detect": {"recap": True}} if published_first else INTRO_ONLY
        probe = _probe(CHAPTERS_BOTH if published_first else ())
        with patch.object(pipeline, "read_server_markers", reader):
            _run(
                _ctx(store, reg, settings_raw=raw, now=lambda: clock["t"]),
                media,
                {"plex-1": ready_publisher()},
                probe=probe,
            )
            clock["t"] += later
            _run(_ctx(store, reg, settings_raw=raw, now=lambda: clock["t"]), media, {"plex-1": ready_publisher()})
        assert reader.call_count == (2 if read_again else 1)
        assert reader.call_args == ((reg.get("plex-1"), reg.get_config("plex-1"), "item-plex-1"),)
        store.close()

    def test_a_sibling_publish_landing_during_the_read_discards_the_answer(self, store, tmp_path):
        folder = tmp_path / "media" / "movies" / "Toy Story (1995) {tmdb-862}"
        folder.mkdir(parents=True)
        version_a, version_b = (
            str(folder / "Toy Story (1995) - 1080p.mkv"),
            str(folder / "Toy Story (1995) - 2160p.mkv"),
        )
        for path in (version_a, version_b):
            open(path, "wb").write(b"x" * 10)
        reg = _registry(version_a, ServerType.PLEX)
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = "42"

        def reader(server, cfg, item_id):
            st = os.stat(version_b)  # version B's check commits to the same Plex item while A's HTTP read is out
            rec_b = store.upsert_file(
                FileIdentity(version_b, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=True
            )
            store.set_publish_state(rec_b.id, "plex-1", item_id="42", markers=[CREDITS_CH], status="written")
            store.set_item_publish_state("plex-1", "42", [CREDITS_CH], "written")
            return [Candidate(T.CREDITS, 1_295_000, None, Source.SERVER_MARKERS, origin="plex-1")]

        with patch.object(pipeline, "read_server_markers", side_effect=reader):
            _run(_ctx(store, reg), version_a, {"plex-1": ready_publisher()})
        assert store.evidence_fetched_at(store.get_file(version_a).id, Source.SERVER_MARKERS, "plex-1") is None

    def test_server_without_the_item_is_not_read(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = None
        reader = MagicMock(return_value=[])
        with patch.object(pipeline, "read_server_markers", reader):
            _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        reader.assert_not_called()

    def test_jellyfin_is_not_read_again_after_we_published_there(self, store, media):
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        reader = MagicMock(return_value=[Candidate(T.INTRO, 127_000, 158_000, Source.SERVER_MARKERS, origin="x")])
        with patch.object(pipeline, "read_server_markers", reader):
            _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"jellyfin-1": jf})
            assert jf.write.call_count == 1
            ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY, force=True)
            out, _ = _run(ctx, media, {"jellyfin-1": jf})
        reader.assert_called_once_with(reg.get("jellyfin-1"), reg.get_config("jellyfin-1"), "item-jellyfin-1")
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value


class TestPublishFanOut:
    def _decided(self, store, media, reg, pubs):
        return _run(_ctx(store, reg), media, pubs, probe=_probe(CHAPTERS_BOTH))[0]

    @pytest.mark.parametrize(
        ("setup", "status", "reason_code"),
        [
            (
                lambda pub, server: setattr(
                    pub.capability, "return_value", CapabilityReport(Capability.NEEDS_PLUGIN, "Install the plugin")
                ),
                ServerStatus.SKIPPED,
                None,
            ),
            (
                lambda pub, server: setattr(server.resolve_remote_path_to_item_id, "return_value", None),
                ServerStatus.WAITING,
                "not_in_library",
            ),
            (
                lambda pub, server: setattr(pub.write, "side_effect", ItemNotFoundError("not indexed")),
                ServerStatus.WAITING,
                "not_in_library",
            ),
            (lambda pub, server: setattr(pub.write, "side_effect", PublishError("boom")), ServerStatus.FAILED, None),
            (
                lambda pub, server: setattr(
                    pub.write, "side_effect", PublishError("locked", state=Capability.NEEDS_LOCAL_DB)
                ),
                ServerStatus.FAILED,
                None,
            ),
            (lambda pub, server: setattr(pub.write, "side_effect", RuntimeError("bug")), ServerStatus.FAILED, None),
            (
                lambda pub, server: setattr(pub.capability, "side_effect", RuntimeError("bug")),
                ServerStatus.FAILED,
                None,
            ),
        ],
        ids=[
            "capability",
            "no-item-id",
            "item-not-found",
            "publish-error",
            "publish-error-with-state",
            "unexpected",
            "capability-raises",
        ],
    )
    def test_one_server_problem_does_not_block_the_other(self, store, media, setup, status, reason_code):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge")
        setup(plex, reg.get("plex-1"))
        out = self._decided(store, media, reg, {"plex-1": plex, "jellyfin-1": jf})
        rows = _rows(out)
        assert rows["plex-1"]["status"] == status.value and rows["plex-1"]["message"]
        assert rows["jellyfin-1"]["status"] == ServerStatus.WRITTEN.value
        # Only "the server hasn't indexed the file yet" carries a code: the job retries those files later.
        assert rows["plex-1"].get("reason_code", "absent") == (reason_code or "absent")
        assert "reason_code" not in rows["jellyfin-1"]
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        state = _state(store, media, "plex-1")
        expected_state = {
            ServerStatus.SKIPPED: "skipped",
            ServerStatus.WAITING: "waiting",
            ServerStatus.FAILED: "failed",
        }
        assert state.status == expected_state[status] and state.markers == () and state.message

    def test_waiting_for_other_versions_carries_no_reason_code(self, store, media):
        # The job retries "not in the library yet" files from the code; versions that don't agree aren't retried.
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.write.side_effect = lambda item_id, markers, **kwargs: [INTRO_CH]  # the item's other versions disagree
        out = self._decided(store, media, reg, {"plex-1": plex})
        row = out.publisher_rows[0]
        assert row["status"] == ServerStatus.WAITING.value
        assert row["message"] == "Waiting for this item's other versions to agree on: credits"
        assert "reason_code" not in row

    @pytest.mark.parametrize("state", [c for c in Capability if c is not Capability.READY], ids=lambda c: c.value)
    def test_every_capability_state_other_than_ready_skips_with_its_message(self, store, media, state):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.capability.return_value = CapabilityReport(state, f"because {state.value}")
        out = self._decided(store, media, reg, {"plex-1": plex})
        plex.write.assert_not_called()
        assert out.publisher_rows[0]["status"] == ServerStatus.SKIPPED.value
        assert out.publisher_rows[0]["message"] == f"because {state.value}"
        assert out.outcome_key == FileOutcome.SKIPPED.value
        reg.get("plex-1").resolve_remote_path_to_item_id.assert_not_called()

    def test_publish_error_with_a_capability_state_checks_the_server_again(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.write.side_effect = PublishError("Plex's database is on a network share", state=Capability.NEEDS_LOCAL_DB)
        ctx = _ctx(store, reg)
        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert out.outcome_key == FileOutcome.FAILED.value
        plex.capability.return_value = CapabilityReport(Capability.NEEDS_LOCAL_DB, "network share")
        out, _ = _run(ctx, media, {"plex-1": plex})
        assert plex.capability.call_count == 2 and plex.write.call_count == 1
        assert out.publisher_rows[0]["status"] == ServerStatus.SKIPPED.value

    def test_publish_error_without_a_state_keeps_the_cached_capability_and_retries(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.write.side_effect = PublishError("boom")
        ctx = _ctx(store, reg)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        plex.write.side_effect = plex.succeed
        out, _ = _run(ctx, media, {"plex-1": plex})
        assert plex.capability.call_count == 1 and plex.write.call_count == 2
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_capability_exception_is_not_cached(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.capability.side_effect = [RuntimeError("HTTP 500"), CapabilityReport(Capability.READY, "ok")]
        ctx = _ctx(store, reg)
        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert "RuntimeError" in out.publisher_rows[0]["message"]
        out, _ = _run(ctx, media, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_nothing_found_is_markers_none_and_writes_nothing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe())
        plex.write.assert_not_called()
        assert [r["status"] for r in out.publisher_rows] == [ServerStatus.NONE.value]
        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        assert store.get_publish_state(store.get_file(media).id, "plex-1") is None

    def test_decided_types_the_server_cant_show_are_markers_none(self, store, media):
        chapters = (Chapter(0, 40_000, "Previously"), Chapter(40_000, None, "Chapter 1"))
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        out, _ = _run(
            _ctx(store, reg, settings_raw={"detect": {"recap": True}}), media, {"plex-1": plex}, probe=_probe(chapters)
        )
        plex.write.assert_not_called()
        assert out.publisher_rows[0]["status"] == ServerStatus.NONE.value
        assert "can't show" in out.publisher_rows[0]["message"]

    def test_item_not_in_server_yet_is_markers_waiting(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = None
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        plex.write.assert_not_called()
        assert [r["status"] for r in out.publisher_rows] == [ServerStatus.WAITING.value]
        assert out.outcome_key == FileOutcome.WAITING.value
        assert _state(store, media, "plex-1").status == "waiting"

    def test_item_id_lookup_exception_is_markers_waiting(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        reg.get("plex-1").resolve_remote_path_to_item_id.side_effect = RuntimeError("HTTP 500")
        out, _ = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))
        assert out.outcome_key == FileOutcome.WAITING.value

    def test_hint_item_id_is_used_without_lookup(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        with (
            patch.object(pipeline, "probe_media", return_value=_probe(CHAPTERS_BOTH)),
            patch.object(pipeline, "publisher_for", return_value=plex),
        ):
            check_item(_item(media, hints={"plex-1": "4242"}), ctx=_ctx(store, reg))
        assert plex.write.call_args.args[0] == "4242"
        reg.get("plex-1").resolve_remote_path_to_item_id.assert_not_called()

    def test_new_item_id_is_published_again(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH), hints={"plex-1": "1"})
        out, _ = _run(ctx, media, {"plex-1": plex}, hints={"plex-1": "2"})
        assert plex.write.call_args.args == ("2", [INTRO_CH, CREDITS_CH])
        assert plex.write.call_args.kwargs["previous"] == []  # nothing of ours was ever left on item 2
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    @pytest.mark.parametrize(
        "later",
        [
            lambda pub, server: setattr(
                pub.capability, "return_value", CapabilityReport(Capability.UNREACHABLE, "down")
            ),
            lambda pub, server: setattr(server.resolve_remote_path_to_item_id, "return_value", None),
            lambda pub, server: setattr(pub.capability, "side_effect", RuntimeError("HTTP 500")),
        ],
        ids=["skipped", "waiting", "capability-raises"],
    )
    def test_rows_that_dont_know_the_item_keep_the_published_item_id(self, store, media, later):
        # Another version of the same Plex item must keep seeing that our markers are still on it.
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(6, 6))
        later(plex, reg.get("plex-1"))
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        state = _state(store, media, "plex-1")
        assert state.status in ("skipped", "waiting", "failed") and state.item_id == "item-plex-1"
        assert store.published_to_item("plex-1", "item-plex-1") is True

    def test_a_failed_write_is_sent_again_even_when_the_set_matches_the_last_publish(self, store, media):
        # A write that raised may still have landed (a timeout after Jellyfin stored it), so only a clean "written"
        # row says what the server shows.
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        jf.write.side_effect = PublishError("Can't reach Jellyfin (ReadTimeout)", state=Capability.UNREACHABLE)
        _run(_ctx(store, reg, settings_raw={"detect": {"credits": False}}), media, {"jellyfin-1": jf})
        assert jf.write.call_args.args[1] == [INTRO_CH]
        jf.write.side_effect = jf.succeed
        out, _ = _run(_ctx(store, reg), media, {"jellyfin-1": jf})
        assert jf.write.call_count == 3
        assert jf.write.call_args.args[1] == [INTRO_CH, CREDITS_CH]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    @pytest.mark.parametrize(
        ("settings_raw", "clients"),
        [
            ({"detect": {"intro": False, "credits": False}}, None),
            (INTRO_ONLY, {"theintrodb": FakeClient(LookupResult("ok", (TIDB_INTRO,)))}),
        ],
        ids=["nothing-to-show", "needs-review"],
    )
    def test_after_a_failed_publish_an_empty_set_is_still_written_with_unknown_previous(
        self, store, media, settings_raw, clients
    ):
        # A write that raised may have left markers on the server (the plugin stores before its check fails, or a
        # POST times out after Jellyfin received it), so "nothing to publish" still clears the server.
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        jf.write.side_effect = PublishError("Media Preview Bridge returned HTTP 500")
        _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert _state(store, media, "jellyfin-1").status == "failed"
        assert _state(store, media, "jellyfin-1").markers == ()
        jf.write.side_effect = jf.succeed
        os.utime(media, ns=(4, 4))
        ctx = _ctx(store, reg, settings_raw=settings_raw, clients={**_clients(), **(clients or {})})
        out, _ = _run(ctx, media, {"jellyfin-1": jf}, probe=_probe())
        args, kwargs = jf.write.call_args
        assert args == ("item-jellyfin-1", [])
        assert kwargs == {"previous": None, "own_previous": None, "duration_ms": DUR, "canonical_path": media}
        assert out.publisher_rows[0]["status"] == ServerStatus.WRITTEN.value
        assert _state(store, media, "jellyfin-1").status == "written"

    @pytest.mark.parametrize(
        "error", [PublishError("Plex's database is busy"), RuntimeError("bug")], ids=["publish-error", "unexpected"]
    )
    def test_a_write_that_raises_marks_the_item_failed_and_keeps_its_markers(self, store, media, error):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        written = store.get_item_publish_state("plex-1", "item-plex-1")
        os.utime(media, ns=(4, 4))
        plex.write.side_effect = error
        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        row = store.get_item_publish_state("plex-1", "item-plex-1")
        assert out.outcome_key == FileOutcome.FAILED.value
        assert (row.status, row.markers) == ("failed", written.markers) and row.version > written.version
        assert store.get_publish_basis(store.get_file(media).id, "plex-1") is None

    @pytest.mark.parametrize(
        ("stype", "name", "expected_previous"),
        [
            # One transaction: after a failure the item row still says exactly what is ours there.
            (ServerType.PLEX, "plex_db", [INTRO_CH, CREDITS_CH]),
            # The plugin may have stored before the error: what it holds is unknown.
            (ServerType.JELLYFIN, "jellyfin_bridge", None),
        ],
        ids=["atomic", "not-atomic"],
    )
    def test_after_a_failed_publish_previous_follows_atomic_writes(self, store, media, stype, name, expected_previous):
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        pub = ready_publisher(name)
        _run(_ctx(store, reg), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(4, 4))
        pub.write.side_effect = PublishError("boom")
        _run(_ctx(store, reg), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH[:3]))
        item_row = store.get_item_publish_state(sid, f"item-{sid}")
        assert (item_row.status, item_row.markers) == ("failed", (INTRO_CH, CREDITS_CH))
        pub.write.side_effect = pub.succeed
        _run(_ctx(store, reg), media, {sid: pub})
        assert pub.write.call_args.args[1] == [INTRO_CH]
        assert pub.write.call_args.kwargs["previous"] == expected_previous

    @pytest.mark.parametrize("between", ["skipped", "waiting"])
    def test_unknown_previous_survives_a_skipped_or_waiting_run(self, store, media, between):
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        jf.write.side_effect = PublishError("Media Preview Bridge returned HTTP 500")
        _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        jf.write.side_effect = jf.succeed
        server = reg.get("jellyfin-1")
        if between == "skipped":
            jf.capability.return_value = CapabilityReport(Capability.UNREACHABLE, "Can't reach this Jellyfin server")
        else:
            server.resolve_remote_path_to_item_id.return_value = None
        _run(_ctx(store, reg), media, {"jellyfin-1": jf})
        assert _state(store, media, "jellyfin-1").status == between
        jf.capability.return_value = CapabilityReport(Capability.READY, "ok")
        server.resolve_remote_path_to_item_id.return_value = "item-jellyfin-1"
        _run(_ctx(store, reg, settings_raw={"detect": {"intro": False, "credits": False}}), media, {"jellyfin-1": jf})
        assert jf.write.call_count == 2
        assert jf.write.call_args.args == ("item-jellyfin-1", [])
        assert jf.write.call_args.kwargs["previous"] is None

    def test_nothing_on_record_for_an_item_never_reached_is_not_cleared(self, store, media):
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        jf.capability.return_value = CapabilityReport(Capability.NEEDS_PLUGIN, "Install the plugin")
        _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert _state(store, media, "jellyfin-1").item_id is None
        jf.capability.return_value = CapabilityReport(Capability.READY, "ok")
        out, _ = _run(
            _ctx(store, reg, settings_raw={"detect": {"intro": False, "credits": False}}), media, {"jellyfin-1": jf}
        )
        jf.write.assert_not_called()
        assert out.outcome_key == FileOutcome.NO_MARKERS.value

    def test_cancel_during_one_servers_write_stops_before_the_next(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        cancelled = threading.Event()
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge")
        plex.write.side_effect = lambda *a, **k: cancelled.set() or a[1]
        out, _ = _run(
            _ctx(store, reg),
            media,
            {"plex-1": plex, "jellyfin-1": jf},
            probe=_probe(CHAPTERS_BOTH),
            cancel_check=cancelled.is_set,
        )
        assert out.message == "cancelled by user"
        jf.write.assert_not_called()
        assert _state(store, media, "plex-1").status == "written"

    def test_unchanged_decisions_are_not_saved_again(self, tmp_path, media):
        clock = {"t": datetime(2026, 9, 13, tzinfo=timezone.utc)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()

        def decided_at():
            return {t: d.decided_at for t, d in store.get_decisions(store.get_file(media).id).items()}

        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        first = decided_at()
        clock["t"] += timedelta(hours=1)
        _run(_ctx(store, reg), media, {"plex-1": plex})
        assert decided_at() == first
        clock["t"] += timedelta(hours=1)
        _run(_ctx(store, reg, settings_raw={"publish_when": "medium"}), media, {"plex-1": plex})
        assert all(at == clock["t"].isoformat() for at in decided_at().values())  # settings changed: saved again
        store.close()

    @pytest.mark.parametrize(
        ("state", "rechecked"),
        [
            (None, False),
            (Capability.READY, False),
            (Capability.UNREACHABLE, True),
            (Capability.NEEDS_LOCAL_DB, True),
            (Capability.MISCONFIGURED, True),
            (Capability.UNSUPPORTED_SCHEMA, True),
            (Capability.NEEDS_PLUGIN, True),
        ],
        ids=lambda v: v.value if isinstance(v, Capability) else str(v),
    )
    def test_only_capability_level_failures_recheck_the_server_for_the_next_file(
        self, store, media, tmp_path, state, rechecked
    ):
        # Per-item failures (Jellyfin's write check: provider off for a library, stale file size) come back without a
        # state and say nothing about the server.
        other = os.path.join(os.path.dirname(media), "Rick and Morty (2013) - S01E02 - Lawnmower Dog.mkv")
        open(other, "wb").write(b"y" * 10)
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        jf.write.side_effect = [PublishError("write check failed", state=state), [INTRO_CH, CREDITS_CH]]
        ctx = _ctx(store, reg)
        _run(ctx, media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        out, _ = _run(ctx, other, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert jf.capability.call_count == (2 if rechecked else 1)
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_all_servers_failed_is_failed(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.write.side_effect = PublishError("boom")
        out = self._decided(store, media, reg, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.FAILED.value

    def test_waiting_for_an_unindexed_item_keeps_the_last_published_markers(self, store, media):
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(2, 2))
        jf.write.side_effect = ItemNotFoundError("Jellyfin doesn't know item item-jellyfin-1 (yet)")
        _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH[:3]))
        assert _state(store, media, "jellyfin-1").markers == (INTRO_CH, CREDITS_CH)

    def test_markers_hash_is_kept_per_server(self, store, media):
        chapters = (Chapter(0, 40_000, "Previously"), *CHAPTERS_BOTH[1:])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge", ("intro", "credits", "recap"))
        pubs = {"plex-1": plex, "jellyfin-1": jf}
        _run(_ctx(store, reg), media, pubs, probe=_probe(chapters))
        out, _ = _run(_ctx(store, reg, settings_raw={"detect": {"recap": True}}), media, pubs)
        assert plex.write.call_count == 1 and jf.write.call_count == 2
        assert jf.write.call_args.args[1] == [Marker(T.RECAP, 0, 40_000, ("chapters",)), INTRO_CH, CREDITS_CH]
        assert {r["server_id"]: r["status"] for r in out.publisher_rows} == {
            "plex-1": ServerStatus.UP_TO_DATE.value,
            "jellyfin-1": ServerStatus.WRITTEN.value,
        }

    def test_up_to_date_leaves_publish_state_untouched(self, tmp_path, media):
        clock = {"t": datetime(2026, 9, 13, tzinfo=timezone.utc)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        before = _state(store, media, "plex-1")
        clock["t"] += timedelta(hours=1)
        _run(_ctx(store, reg), media, {"plex-1": plex})
        assert _state(store, media, "plex-1") == before
        store.close()

    def test_capability_is_cached_per_context(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(9, 9))
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert plex.capability.call_count == 1

    def test_capability_is_fetched_again_after_the_ttl(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg, ttl=0.0)
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(9, 9))
        _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert plex.capability.call_count == 2

    def test_capability_is_fetched_once_when_many_checks_start_together(self, store, media):
        # 32 checking threads hit an empty cache at job start (and at every TTL expiry); Plex's check reads prefs
        # over HTTP and opens its DB, so it must run once per server, not once per thread.
        reg = _registry(media, ServerType.PLEX)
        ctx = _ctx(store, reg)
        cfg = reg.get_config("plex-1")
        plex = ready_publisher()

        def slow_capability():
            time.sleep(0.05)
            return CapabilityReport(Capability.READY, "ok")

        plex.capability.side_effect = slow_capability
        barrier = threading.Barrier(16)
        reports = []

        def check():
            barrier.wait()
            reports.append(pipeline._capability(ctx, cfg, plex))

        threads = [threading.Thread(target=check) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert plex.capability.call_count == 1
        assert len(reports) == 16 and all(r.ready for r in reports)

    def test_publisher_gets_a_sibling_lookup_backed_by_the_store(self, store, media):
        # Without this lookup Plex can never see that the versions of an item agree, so nothing is shown on them.
        reg = _registry(media, ServerType.PLEX)
        with (
            patch.object(pipeline, "probe_media", return_value=_probe(CHAPTERS_BOTH)),
            patch.object(pipeline, "publisher_for", return_value=ready_publisher()) as factory,
        ):
            check_item(_item(media), ctx=_ctx(store, reg))
        assert factory.call_args.args == (reg.get("plex-1"), reg.get_config("plex-1"))
        lookup = factory.call_args.kwargs["sibling_markers"]
        assert lookup(media) == {T.INTRO: INTRO_CH, T.CREDITS: CREDITS_CH}
        assert lookup("/elsewhere/never-decided.mkv") is None

    def test_locked_marker_is_published_even_when_detection_is_off(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        raw = {"detect": {"intro": False, "credits": False}}
        st = os.stat(media)
        rec = store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        store.lock_marker(rec.id, Marker(T.INTRO, 5_000, 30_000, ("user",), locked=True))
        ctx = _ctx(store, reg, settings_raw=raw)
        out, _ = _run(ctx, media, {"plex-1": plex})
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 5_000, 30_000, ("user",), locked=True)]
        assert all(c.calls == [] for c in ctx.clients.values())
        assert "intro" in out.message


@pytest.fixture
def saved_settings(tmp_path):
    """The real settings manager on a temp config folder."""
    from media_preview_generator.web import settings_manager as sm_mod

    sm_mod.reset_settings_manager()
    yield sm_mod.get_settings_manager(str(tmp_path / "config"))
    sm_mod.reset_settings_manager()


def _second_episode(media):
    other = os.path.join(os.path.dirname(media), "Rick and Morty (2013) - S01E02 - Lawnmower Dog.mkv")
    with open(other, "wb") as f:
        f.write(b"y" * 100)
    return other


class TestConsentBeforeEachWrite:
    """Turning Intro & Credits off (or the server, or a library) stops a running job's writes (audit C MED-2)."""

    @pytest.mark.parametrize(
        ("flip", "message"),
        [
            (lambda entry: entry.update(enabled=False), "This server is turned off on the Servers page"),
            (lambda entry: entry["markers"].update(enabled=False), "Intro & Credits is off for this server"),
        ],
        ids=["server-off", "markers-off"],
    )
    def test_turning_it_off_after_the_first_file_stops_the_second_write(
        self, store, media, saved_settings, flip, message
    ):
        from media_preview_generator.servers.registry import server_config_to_dict

        other = _second_episode(media)
        reg = _registry(media, ServerType.PLEX)
        saved_settings.set("media_servers", [server_config_to_dict(reg.configs_by_id["plex-1"])])
        plex = ready_publisher()
        ctx = _ctx(store, reg, live_config=pipeline.live_server_config)

        first, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        entries = saved_settings.get("media_servers")
        flip(entries[0])
        saved_settings.set("media_servers", entries)
        second, _ = _run(ctx, other, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))

        assert first.publisher_rows[0]["status"] == ServerStatus.WRITTEN.value
        assert (second.publisher_rows[0]["status"], second.publisher_rows[0]["message"]) == (
            ServerStatus.SKIPPED.value,
            message,
        )
        assert [c.kwargs["canonical_path"] for c in plex.write.call_args_list] == [media]
        assert _state(store, other, "plex-1").status == "skipped"
        assert "plex-1" not in ctx._capabilities  # checked again once it's back on

    # Emby has no publisher yet: its row is "Not supported" before any write could be attempted.
    @pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.JELLYFIN])
    @pytest.mark.parametrize(
        ("change", "status", "message"),
        [
            ("removed", ServerStatus.SKIPPED, "This server was removed"),
            ("server-off", ServerStatus.SKIPPED, "This server is turned off on the Servers page"),
            ("markers-off", ServerStatus.SKIPPED, "Intro & Credits is off for this server"),
            (
                "library-deselected",
                ServerStatus.SKIPPED,
                "This library isn't selected for Intro & Credits on this server",
            ),
            ("library-removed", ServerStatus.SKIPPED, "This library isn't selected for Intro & Credits on this server"),
            ("path-excluded", ServerStatus.SKIPPED, "This file is excluded on this server"),
            ("unreadable", ServerStatus.FAILED, "Couldn't read this server's saved settings (RuntimeError)"),
            ("unchanged", ServerStatus.WRITTEN, "2 marker(s)"),
        ],
    )
    def test_saved_settings_matrix(self, store, media, stype, change, status, message):
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        live = copy.deepcopy(reg.configs_by_id[sid])
        if change == "server-off":
            live.enabled = False
        elif change == "markers-off":
            live.markers["enabled"] = False
        elif change == "library-deselected":
            live.markers["library_ids"] = ["9"]
        elif change == "library-removed":
            live.libraries = [Library("1", "TV Shows", ("/somewhere/else",))]
        elif change == "path-excluded":
            live.exclude_paths = [{"value": "Rick and Morty", "type": "regex"}]

        def live_config(server_id):
            assert server_id == sid
            if change == "unreadable":
                raise RuntimeError("settings.json unreadable")
            return None if change == "removed" else live

        pub = ready_publisher("plex_db" if stype is ServerType.PLEX else "jellyfin_bridge")
        ctx = _ctx(store, reg, live_config=live_config)
        out, _ = _run(ctx, media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        row = out.publisher_rows[0]
        assert (row["status"], row["message"]) == (status.value, message)
        assert pub.write.call_count == (1 if change == "unchanged" else 0)
        assert (sid in ctx._capabilities) is (change == "unchanged")

    def test_plex_confirmation_cleared_loads_as_off(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        live = copy.deepcopy(reg.configs_by_id["plex-1"])
        live.markers["plex"]["db_write_confirmed_at"] = None
        plex = ready_publisher()
        out, _ = _run(
            _ctx(store, reg, live_config=lambda _sid: live), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH)
        )
        assert (out.publisher_rows[0]["status"], out.publisher_rows[0]["message"]) == (
            ServerStatus.SKIPPED.value,
            "Intro & Credits is off for this server",
        )
        plex.write.assert_not_called()

    def test_back_on_checks_the_server_again_and_writes(self, store, media):
        other = _second_episode(media)
        reg = _registry(media, ServerType.JELLYFIN)
        live = copy.deepcopy(reg.configs_by_id["jellyfin-1"])
        live.markers["enabled"] = False
        jf = ready_publisher("jellyfin_bridge")
        ctx = _ctx(store, reg, live_config=lambda _sid: live)
        _run(ctx, media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        live.markers["enabled"] = True
        out, _ = _run(ctx, other, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert out.publisher_rows[0]["status"] == ServerStatus.WRITTEN.value
        assert jf.capability.call_count == 2
        assert [c.kwargs["canonical_path"] for c in jf.write.call_args_list] == [other]

    def test_the_publisher_reads_the_saved_settings_on_every_check(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        live = copy.deepcopy(reg.configs_by_id["plex-1"])
        factory_kwargs = []

        def factory(server, cfg, **kwargs):
            factory_kwargs.append(kwargs)
            return ready_publisher()

        with (
            patch.object(pipeline, "probe_media", return_value=_probe(CHAPTERS_BOTH)),
            patch.object(pipeline, "publisher_for", side_effect=factory),
        ):
            check_item(_item(media), ctx=_ctx(store, reg, live_config=lambda _sid: live))
        provider = factory_kwargs[0]["settings_provider"]
        assert (provider().enabled, provider().db_write_confirmed_at) == (True, "2026-09-13T00:00:00+00:00")
        live.markers["enabled"] = False
        assert provider().enabled is False
        live.markers["enabled"], live.enabled = True, False
        assert provider().enabled is False

    def test_build_context_reads_the_saved_settings(self, store, saved_settings):
        from media_preview_generator.servers.registry import server_config_to_dict

        cfg = server_config("plex-1", ServerType.PLEX)
        saved_settings.set("media_servers", [{"id": "other", "type": "jellyfin"}, server_config_to_dict(cfg)])
        with (
            patch.object(pipeline, "get_global_settings", return_value=load_global({})),
            patch.object(pipeline, "get_marker_store", return_value=store),
            patch.object(pipeline, "build_clients", return_value={}),
        ):
            ctx = pipeline.build_context(registry=MagicMock(), config=MagicMock(ffmpeg_path=None), priority=3)
        assert ctx.live_config("plex-1") == cfg
        assert ctx.live_config("gone") is None


class TestPlexPassUnknown:
    """A READY Plex whose Plex Pass couldn't be read isn't written: Plex serves nothing without a Pass (audit C LOW)."""

    # Emby has no publisher yet, so it never reaches the capability check.
    @pytest.mark.parametrize(
        ("stype", "details", "status", "message"),
        [
            (ServerType.PLEX, {"plex_pass": None}, ServerStatus.SKIPPED, "Can't reach Plex to confirm Plex Pass"),
            (ServerType.PLEX, {"plex_pass": True}, ServerStatus.WRITTEN, "2 marker(s)"),
            (ServerType.JELLYFIN, {"plugin_version": "10.11.1.0"}, ServerStatus.WRITTEN, "2 marker(s)"),
        ],
        ids=["plex-unknown", "plex-pass", "jellyfin"],
    )
    def test_unknown_pass_is_not_ready_for_writes(self, store, media, stype, details, status, message):
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        pub = ready_publisher("plex_db" if stype is ServerType.PLEX else "jellyfin_bridge")
        pub.capability.return_value = CapabilityReport(Capability.READY, "ready", details)
        out, _ = _run(_ctx(store, reg), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        assert (out.publisher_rows[0]["status"], out.publisher_rows[0]["message"]) == (status.value, message)
        assert pub.write.call_count == (0 if status is ServerStatus.SKIPPED else 1)


class TestStages:
    def test_check_defers_to_worker_when_a_local_detector_can_help(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[])
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), detector)
        plex = ready_publisher()
        ctx = _ctx(store, reg, detectors=(spec,))
        out, _ = _run(ctx, media, {"plex-1": plex})
        assert out is None
        detector.assert_not_called()
        plex.write.assert_not_called()

    def test_check_does_not_defer_when_the_detector_types_are_decided(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), MagicMock(return_value=[]))
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"credits": False}}
        out, _ = _run(
            _ctx(store, reg, detectors=(spec,), settings_raw=raw),
            media,
            {"plex-1": ready_publisher()},
            probe=_probe(CHAPTERS_BOTH),
        )
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_detector_for_a_disabled_source_is_ignored(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[])
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), detector)
        raw = {"sources": [{"id": "season_audio", "enabled": False}]}
        out, _ = _run(_ctx(store, reg, detectors=(spec,), settings_raw=raw), media, {"plex-1": ready_publisher()})
        assert out is not None and out.outcome_key == FileOutcome.NO_MARKERS.value

    def test_process_forwards_worker_gpu_and_callbacks_to_detectors_and_lookups(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detected = [Candidate(T.INTRO, 126_000, 158_000, Source.SEASON_AUDIO)]
        detector = MagicMock(return_value=detected)
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), detector)
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        ctx = _ctx(store, reg, detectors=(spec,), clients=clients, settings_raw=INTRO_ONLY)
        plex = ready_publisher()
        cancel = MagicMock(return_value=False)
        pause = MagicMock(return_value=False)
        phase = MagicMock()
        with (
            patch.object(pipeline, "probe_media", return_value=_probe()),
            patch.object(pipeline, "publisher_for", return_value=plex),
        ):
            out = process_item(
                _item(media),
                ctx=ctx,
                gpu="NVIDIA",
                gpu_device_path="cuda:0",
                cancel_check=cancel,
                pause_check=pause,
                phase_callback=phase,
            )
        (rec,), kwargs = detector.call_args
        assert rec.canonical_path == media and rec.duration_ms == DUR
        assert kwargs["ctx"] is ctx and kwargs["gpu"] == "NVIDIA" and kwargs["gpu_device_path"] == "cuda:0"
        assert kwargs["cancel_check"] is cancel and kwargs["pause_check"] is pause and kwargs["phase_callback"] is phase
        assert clients["theintrodb"].calls[0]["cancel_check"] is cancel
        assert store.get_evidence(rec.id) == [TIDB_INTRO, *detected] or set(store.get_evidence(rec.id)) == {
            TIDB_INTRO,
            *detected,
        }
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert any("Looking up" in c.args[0] for c in phase.call_args_list)

    def test_process_never_returns_none(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), MagicMock(return_value=[]))
        out, _ = _run(_ctx(store, reg, detectors=(spec,)), media, {"plex-1": ready_publisher()}, stage="process")
        assert out is not None

    def test_detector_errors_reach_the_worker_for_its_cpu_fallback(self, store, media):
        from media_preview_generator.processing.generator import CodecNotSupportedError

        reg = _registry(media, ServerType.PLEX)
        spec = LocalDetectorSpec(
            Source.SEASON_AUDIO, frozenset({T.INTRO}), MagicMock(side_effect=CodecNotSupportedError("gpu"))
        )
        with pytest.raises(CodecNotSupportedError):
            _run(_ctx(store, reg, detectors=(spec,)), media, {"plex-1": ready_publisher()}, stage="process")

    def test_forced_evidence_is_refreshed_once_across_check_and_worker(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), MagicMock(return_value=[]))
        clients = _clients()
        ctx = _ctx(store, reg, detectors=(spec,), clients=clients, force=True)
        pubs = {"plex-1": ready_publisher()}
        assert _run(ctx, media, pubs)[0] is None
        out, probe = _run(ctx, media, pubs, stage="process")
        assert probe.call_count == 0
        assert [len(c.calls) for c in clients.values()] == [1, 1, 1]
        assert out.outcome_key == FileOutcome.NO_MARKERS.value

    def test_cancel_before_work(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        with patch.object(pipeline, "probe_media") as probe:
            out = check_item(_item(media), ctx=_ctx(store, reg), cancel_check=lambda: True)
        assert out.outcome_key == "failed" and "cancel" in out.message
        probe.assert_not_called()

    def test_cancel_after_reading_chapters_publishes_nothing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        cancelled = threading.Event()
        plex = ready_publisher()

        def probe(path, *, ffprobe):
            cancelled.set()
            return _probe(CHAPTERS_BOTH)

        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe_effect=probe, cancel_check=cancelled.is_set)
        assert out.outcome_key == FileOutcome.FAILED.value and "cancel" in out.message
        plex.write.assert_not_called()

    def test_pause_does_not_hold_the_worker(self, store, media):
        # A paused Intro & Credits job gives its workers back to previews: an item in flight finishes its cheap steps
        # instead of blocking; only long-running detectors (which receive pause_check) wait.
        reg = _registry(media, ServerType.PLEX)
        pause = MagicMock(return_value=True)
        out, _ = _run(
            _ctx(store, reg),
            media,
            {"plex-1": ready_publisher()},
            stage="process",
            probe=_probe(CHAPTERS_BOTH),
            pause_check=pause,
        )
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        pause.assert_not_called()

    def test_cancel_while_reading_chapters_stops_before_asking_servers(self, store, ambiguous):
        reg = _registry(ambiguous, ServerType.JELLYFIN)
        cancelled = threading.Event()

        def probe(path, *, ffprobe):
            cancelled.set()
            return _probe(CHAPTERS_OPENING)

        pubs = {"jellyfin-1": ready_publisher("jellyfin_bridge")}
        out, _ = _run(_ctx(store, reg), ambiguous, pubs, probe_effect=probe, cancel_check=cancelled.is_set)
        assert out.message == "cancelled by user"
        reg.get("jellyfin-1").resolve_remote_path_to_item_id.assert_not_called()
        assert store.get_file(ambiguous) is None

    def test_cancel_during_the_last_source_publishes_nothing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        cancelled = threading.Event()

        def reader(server, cfg, item_id):
            cancelled.set()
            return []

        plex = ready_publisher()
        with patch.object(pipeline, "read_server_markers", side_effect=reader):
            out, _ = _run(
                _ctx(store, reg),
                media,
                {"plex-1": plex},
                probe=_probe(CHAPTERS_BOTH[:3]),
                cancel_check=cancelled.is_set,
            )
        assert out.message == "cancelled by user"
        plex.write.assert_not_called()

    def test_file_changed_during_analysis_with_nothing_to_write_is_detected_again(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        stamps = iter([(7, 7)])

        def probe(path, *, ffprobe):
            for ns in stamps:
                os.utime(path, ns=ns)
            return _probe()

        out, probe_mock = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()}, probe_effect=probe)
        assert probe_mock.call_count == 2
        assert store.get_file(media).mtime_ns == 7
        assert out.outcome_key == FileOutcome.NO_MARKERS.value

    def test_forced_check_that_fails_after_refreshing_does_not_refresh_again_on_the_worker(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        clients = _clients()
        ctx = _ctx(store, reg, clients=clients, force=True)
        with (
            patch.object(pipeline, "probe_media", return_value=_probe()) as probe,
            patch.object(pipeline, "publisher_for", side_effect=[RuntimeError("bad config"), ready_publisher()]),
        ):
            with pytest.raises(RuntimeError):
                check_item(_item(media), ctx=ctx)
            out = process_item(_item(media), ctx=ctx)
        assert probe.call_count == 1
        assert [len(c.calls) for c in clients.values()] == [1, 1, 1]
        assert out.outcome_key == FileOutcome.NO_MARKERS.value

    def test_detector_for_a_type_the_file_cant_have_does_not_defer(self, store, tmp_path):
        folder = tmp_path / "media" / "movies" / "Toy Story (1995) {tmdb-862}"
        folder.mkdir(parents=True)
        path = str(folder / "Toy Story (1995).mkv")
        open(path, "wb").close()
        reg = _registry(path, ServerType.PLEX)
        detector = MagicMock(return_value=[])
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), detector)
        out, _ = _run(_ctx(store, reg, detectors=(spec,)), path, {"plex-1": ready_publisher()})
        assert out is not None and out.outcome_key == FileOutcome.NO_MARKERS.value
        detector.assert_not_called()


CHAPTERS_EARLY_CREDITS = (
    Chapter(0, 126_771, "Chapter 1"),
    Chapter(126_771, 157_068, "Intro"),
    Chapter(157_068, 1_276_324, "Chapter 2"),
    Chapter(1_276_324, None, "Credits"),
)
CHAPTERS_CREDITS_ONLY = (Chapter(0, 1_295_324, "Chapter 1"), Chapter(1_295_324, None, "Credits"))
SERVED_INTRO = ("intro", 126_771, 157_068)
SERVED_CREDITS = ("credits", 1_295_324, DUR)


class TestPlexItems:
    """Plex serves one marker set per item: previous, outcomes and up-to-date checks follow the item, not the file."""

    @pytest.fixture
    def versions(self, tmp_path):
        folder = tmp_path / "media" / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
        folder.mkdir(parents=True)
        paths = []
        for tag in ("1080p", "2160p"):
            path = folder / f"Rick and Morty (2013) - S01E01 - Pilot - {tag}.mkv"
            path.write_bytes(b"x" * 100)
            paths.append(str(path))
        return paths

    @staticmethod
    def _plex(path, parts):
        reg = _registry(path, ServerType.PLEX)
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = "42"
        return reg, FakePlexItems({"42": parts})

    @staticmethod
    def _check(store, reg, items, path, chapters=(), **ctx_kwargs):
        with (
            patch.object(pipeline, "probe_media", return_value=_probe(chapters)),
            patch.object(
                pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: items.publisher(kw["sibling_markers"])
            ),
        ):
            return check_item(_item(path), ctx=_ctx(store, reg, **ctx_kwargs))

    def test_a_single_version_item_is_written_once(self, store, media):
        reg, items = self._plex(media, [media])
        first = self._check(store, reg, items, media, CHAPTERS_BOTH)
        second = self._check(store, reg, items, media)
        assert [c["previous"] for c in items.calls] == [[]]
        assert (first.outcome_key, second.outcome_key) == (FileOutcome.PUBLISHED.value, FileOutcome.UP_TO_DATE.value)
        assert items.served("42") == [SERVED_INTRO, SERVED_CREDITS]
        assert store.get_item_publish_state("plex-1", "42").markers == (INTRO_CH, CREDITS_CH)

    def test_a_version_added_later_takes_off_the_markers_it_disagrees_with(self, store, versions):
        a, b = versions
        reg, items = self._plex(a, [a])
        assert self._check(store, reg, items, a, CHAPTERS_BOTH).outcome_key == FileOutcome.PUBLISHED.value
        assert items.served("42") == [SERVED_INTRO, SERVED_CREDITS]

        items.parts["42"] = [a, b]  # the user adds a 2160p version whose credits start 19 s earlier
        out_b = self._check(store, reg, items, b, CHAPTERS_EARLY_CREDITS)
        assert items.calls[-1]["path"] == b and items.calls[-1]["previous"] == [INTRO_CH, CREDITS_CH]
        assert items.served("42") == [SERVED_INTRO]  # A's credits no longer fit both versions
        assert out_b.outcome_key == FileOutcome.WAITING.value
        assert "credits" in out_b.publisher_rows[0]["message"]
        assert _state(store, b, "plex-1").markers == (INTRO_CH,)
        assert store.get_item_publish_state("plex-1", "42").markers == (INTRO_CH,)

        out_a = self._check(store, reg, items, a)  # A's own decision didn't change, but the item did
        assert len(items.calls) == 3 and items.calls[-1]["path"] == a and items.calls[-1]["previous"] == [INTRO_CH]
        assert out_a.outcome_key == FileOutcome.WAITING.value
        assert _state(store, a, "plex-1").markers == (INTRO_CH,)

        version = store.get_item_publish_state("plex-1", "42").version
        again_a, again_b = self._check(store, reg, items, a), self._check(store, reg, items, b)
        # A waiting version looks at the item on every run (another version may have gone); nothing changed here.
        assert [c["path"] for c in items.calls[3:]] == [a, b]
        assert again_a.outcome_key == again_b.outcome_key == FileOutcome.WAITING.value
        assert items.served("42") == [SERVED_INTRO]
        assert store.get_item_publish_state("plex-1", "42").version == version

    def test_partial_agreement_follows_every_versions_decision(self, store, versions):
        a, b = versions
        reg, items = self._plex(a, [a, b])
        first_a = self._check(store, reg, items, a, CHAPTERS_BOTH)
        assert items.served("42") == [] and first_a.outcome_key == FileOutcome.WAITING.value  # B not decided yet
        self._check(store, reg, items, b, CHAPTERS_EARLY_CREDITS)
        self._check(store, reg, items, a)
        assert items.served("42") == [SERVED_INTRO]

        # Later both intros go to review (one online source, no chapter) and the credits agree.
        os.utime(a, ns=(5, 5))
        os.utime(b, ns=(6, 6))
        review = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        out_a = self._check(store, reg, items, a, CHAPTERS_CREDITS_ONLY, clients=review)
        assert items.calls[-1]["previous"] == [INTRO_CH]
        assert items.served("42") == []  # our intro is gone; B still disagrees on credits
        assert out_a.outcome_key == FileOutcome.WAITING.value
        out_b = self._check(store, reg, items, b, CHAPTERS_CREDITS_ONLY, clients=review)
        assert out_b.outcome_key == FileOutcome.PUBLISHED.value
        out_a = self._check(store, reg, items, a, clients=review)
        assert out_a.outcome_key == FileOutcome.PUBLISHED.value
        assert items.served("42") == [SERVED_CREDITS]
        assert store.get_item_publish_state("plex-1", "42").markers == (CREDITS_CH,)

    def test_a_version_with_nothing_to_show_takes_off_what_another_version_left(self, store, versions):
        a, b = versions
        reg, items = self._plex(a, [a])
        self._check(store, reg, items, a, CHAPTERS_BOTH)
        items.parts["42"] = [a, b]
        out_b = self._check(store, reg, items, b)  # no chapters and no online answers: nothing decided for B
        assert items.calls[-1]["markers"] == [] and items.calls[-1]["previous"] == [INTRO_CH, CREDITS_CH]
        assert items.served("42") == []
        assert out_b.outcome_key == FileOutcome.PUBLISHED.value  # the write removed our markers
        assert store.published_to_item("plex-1", "42") is False

    def test_an_item_that_comes_back_is_written_again_not_reported_from_an_old_basis(self, store, media):
        reg, items = self._plex(media, [media])
        server = reg.get("plex-1")
        self._check(store, reg, items, media, CHAPTERS_BOTH)
        server.resolve_remote_path_to_item_id.return_value = None
        missing = self._check(store, reg, items, media)
        assert missing.outcome_key == FileOutcome.WAITING.value
        server.resolve_remote_path_to_item_id.return_value = "42"
        back = self._check(store, reg, items, media)
        assert len(items.calls) == 2 and back.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_waiting_version_gets_its_markers_once_the_disagreeing_version_is_gone(self, store, versions):
        a, b = versions
        reg, items = self._plex(a, [a, b])
        self._check(store, reg, items, a, CHAPTERS_BOTH)
        self._check(store, reg, items, b, CHAPTERS_EARLY_CREDITS)
        assert self._check(store, reg, items, a).outcome_key == FileOutcome.WAITING.value
        assert items.served("42") == [SERVED_INTRO]

        items.parts["42"] = [a]  # the user deletes the 2160p version; A's decision and the item row are unchanged
        out = self._check(store, reg, items, a)
        assert items.calls[-1]["path"] == a and items.calls[-1]["previous"] == [INTRO_CH]
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert items.served("42") == [SERVED_INTRO, SERVED_CREDITS]
        calls = len(items.calls)
        assert self._check(store, reg, items, a).outcome_key == FileOutcome.UP_TO_DATE.value
        assert len(items.calls) == calls

    def test_a_forced_run_takes_off_markers_a_new_undecided_version_does_not_share(self, store, versions):
        a, b = versions
        reg, items = self._plex(a, [a])
        self._check(store, reg, items, a, CHAPTERS_BOTH)
        items.parts["42"] = [a, b]  # added, never decided (its disk isn't mapped into the container)
        assert self._check(store, reg, items, a).outcome_key == FileOutcome.UP_TO_DATE.value  # the known limit
        assert len(items.calls) == 1

        forced = self._check(store, reg, items, a, CHAPTERS_BOTH, force=True)
        assert items.calls[-1]["previous"] == [INTRO_CH, CREDITS_CH]
        assert items.served("42") == []
        assert forced.outcome_key == FileOutcome.WAITING.value
        assert "intro, credits" in forced.publisher_rows[0]["message"]
        assert store.get_item_publish_state("plex-1", "42").markers == ()

        again = self._check(store, reg, items, a, CHAPTERS_BOTH, force=True)
        assert len(items.calls) == 3 and items.calls[-1]["previous"] == []
        assert again.outcome_key == FileOutcome.WAITING.value and items.served("42") == []

    def test_own_previous_follows_the_file_to_its_new_item_after_a_merge_or_split(self, store, media):
        reg, items = self._plex(media, [media])
        items.parts["43"] = [media]
        server = reg.get("plex-1")
        self._check(store, reg, items, media, CHAPTERS_BOTH)
        server.resolve_remote_path_to_item_id.return_value = "43"
        out = self._check(store, reg, items, media, CHAPTERS_BOTH)
        moved = items.calls[-1]
        assert (moved["item_id"], moved["previous"], moved["own_previous"]) == ("43", [], [INTRO_CH, CREDITS_CH])
        assert out.outcome_key == FileOutcome.PUBLISHED.value and _state(store, media, "plex-1").item_id == "43"
        self._check(store, reg, items, media, CHAPTERS_BOTH, force=True)
        assert items.calls[-1]["own_previous"] is None  # published on the new item now

    def test_a_moved_file_with_nothing_to_show_still_writes_to_take_its_copy_off(self, store, media):
        reg, items = self._plex(media, [media])
        items.parts["43"] = [media]
        self._check(store, reg, items, media, CHAPTERS_BOTH)
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = "43"
        os.utime(media, ns=(4, 4))  # replaced by a cut without chapters: nothing decided, and item 43 has no row
        out = self._check(store, reg, items, media)
        last = items.calls[-1]
        assert (last["item_id"], last["markers"], last["previous"]) == ("43", [], [])
        assert last["own_previous"] == [INTRO_CH, CREDITS_CH]
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert _state(store, media, "plex-1").item_id == "43"

    @pytest.mark.parametrize(
        "error",
        [PublishError("Plex's database is busy"), ItemNotFoundError("Plex item 43 not found"), RuntimeError("bug")],
        ids=["publish-error", "item-not-found", "unexpected"],
    )
    def test_a_move_whose_write_fails_offers_the_old_markers_again(self, store, media, error):
        reg, items = self._plex(media, [media])
        items.parts["43"] = [media]
        self._check(store, reg, items, media, CHAPTERS_BOTH)
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = "43"
        items.fail_next = error
        self._check(store, reg, items, media, CHAPTERS_BOTH)
        # The part still carries what was published on item 42, so the row keeps pointing there.
        assert _state(store, media, "plex-1").item_id == "42"
        self._check(store, reg, items, media, CHAPTERS_BOTH)
        assert items.calls[-1]["own_previous"] == [INTRO_CH, CREDITS_CH]
        assert _state(store, media, "plex-1").item_id == "43"

    def test_own_previous_is_none_when_the_file_left_nothing_on_its_old_item(self, store, versions):
        a, b = versions
        reg, items = self._plex(a, [a, b])  # B never decided: A's publish leaves nothing on item 42
        items.parts["43"] = [a]
        assert self._check(store, reg, items, a, CHAPTERS_BOTH).outcome_key == FileOutcome.WAITING.value
        assert _state(store, a, "plex-1").markers == ()
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = "43"
        self._check(store, reg, items, a, CHAPTERS_BOTH)
        assert items.calls[-1]["item_id"] == "43" and items.calls[-1]["own_previous"] is None

    def test_a_transient_plex_failure_keeps_the_item_row_for_the_next_removal(self, store, media):
        reg, items = self._plex(media, [media])
        self._check(store, reg, items, media, CHAPTERS_BOTH)
        os.utime(media, ns=(3, 3))
        items.fail_next = PublishError("Plex's database is busy", state=Capability.UNREACHABLE)
        failed = self._check(store, reg, items, media, CHAPTERS_BOTH[:3])
        assert failed.outcome_key == FileOutcome.FAILED.value
        row = store.get_item_publish_state("plex-1", "42")
        assert (row.status, row.markers) == ("failed", (INTRO_CH, CREDITS_CH))
        assert items.served("42") == [SERVED_INTRO, SERVED_CREDITS]
        out = self._check(store, reg, items, media)
        assert items.calls[-1]["previous"] == [INTRO_CH, CREDITS_CH]
        assert items.served("42") == [SERVED_INTRO]  # the credits we left are removed after all
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert store.get_item_publish_state("plex-1", "42").markers == (INTRO_CH,)


NO_INTRO_CHAPTER = (Chapter(0, 200_000, "Chapter 1"), Chapter(200_000, None, "Chapter 2"))
LATE_INTRO_CHAPTER = (
    Chapter(0, 150_000, "Chapter 1"),
    Chapter(150_000, 180_000, "Intro"),
    Chapter(180_000, None, "Chapter 2"),
)


class TestForce:
    """A forced re-detect gathers every source, and every run decides from everything stored."""

    def test_forced_and_normal_runs_agree_so_markers_never_flip(self, store, media):
        # Two agreeing online sources published 61–90 s; a newer chapter rule finds an "Intro" chapter at 150–180 s.
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        clients = _clients(
            theintrodb=LookupResult("ok", (Candidate(T.INTRO, 60_000, 90_000, Source.THEINTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.INTRO, 61_000, 91_000, Source.SKIPDB),)),
        )
        runs = []
        for force, chapters in (
            (False, NO_INTRO_CHAPTER),
            (True, LATE_INTRO_CHAPTER),
            (False, LATE_INTRO_CHAPTER),
            (True, LATE_INTRO_CHAPTER),
            (False, LATE_INTRO_CHAPTER),
        ):
            ctx = _ctx(store, reg, settings_raw=INTRO_ONLY, clients=clients, force=force)
            _run(ctx, media, {"jellyfin-1": jf}, probe=_probe(chapters))
            rec = store.get_file(media)
            decision = store.get_decisions(rec.id)[T.INTRO]
            runs.append(
                (
                    (decision.status, decision.reason, decision.proposed_start_ms),
                    _state(store, media, "jellyfin-1").markers,
                )
            )
        first_shown = runs[0][1]
        assert [(m.start_ms, m.end_ms) for m in first_shown] == [(61_000, 90_000)]
        assert runs[1][0][0] is DecisionStatus.NEEDS_REVIEW and runs[1][1] == ()  # two agreeing sources contradict it
        assert runs[2:] == [runs[1]] * 3  # later forced and normal runs keep the same decision
        assert jf.write.call_count == 2  # the publish, then its removal; never again
        assert [len(c.calls) for c in clients.values()] == [3, 3, 3]  # run 1 and both forced runs ask every source

    @pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.JELLYFIN], ids=lambda t: t.value)
    def test_forced_run_writes_again_and_reports_up_to_date_when_nothing_changed(self, store, media, stype):
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        pub = ready_publisher("plex_db" if stype is ServerType.PLEX else "jellyfin_bridge")
        _run(_ctx(store, reg), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        _run(_ctx(store, reg), media, {sid: pub})
        assert pub.write.call_count == 1
        before = _state(store, media, sid)
        out, _ = _run(_ctx(store, reg, force=True), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        assert pub.write.call_count == 2
        assert pub.write.call_args.args == (f"item-{sid}", [INTRO_CH, CREDITS_CH])
        assert pub.write.call_args.kwargs == {
            "previous": [INTRO_CH, CREDITS_CH],
            "own_previous": None,
            "duration_ms": DUR,
            "canonical_path": media,
        }
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        assert out.publisher_rows[0]["status"] == ServerStatus.UP_TO_DATE.value
        assert _state(store, media, sid) == before

    def test_forced_run_that_changes_the_item_is_published(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        plex.write.side_effect = lambda item_id, markers, **kw: [INTRO_CH]  # the server now takes only the intro
        out, _ = _run(_ctx(store, reg, force=True), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert out.outcome_key == FileOutcome.WAITING.value
        plex.write.side_effect = plex.succeed
        out, _ = _run(_ctx(store, reg, force=True), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert _state(store, media, "plex-1").markers == (INTRO_CH, CREDITS_CH)

    def test_forced_run_asks_every_source_even_when_chapters_decide(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["jellyfin-1"].markers["enabled"] = False
        clients = _clients()
        reader = MagicMock(return_value=[])
        with patch.object(pipeline, "read_server_markers", reader):
            _run(_ctx(store, reg, clients=clients), media, {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))
            assert [len(c.calls) for c in clients.values()] == [0, 0, 0] and reader.call_count == 0
            out, _ = _run(
                _ctx(store, reg, clients=clients, force=True),
                media,
                {"plex-1": ready_publisher()},
                probe=_probe(CHAPTERS_BOTH),
            )
        assert [len(c.calls) for c in clients.values()] == [1, 1, 1]
        reader.assert_called_once_with(reg.get("jellyfin-1"), reg.get_config("jellyfin-1"), "item-jellyfin-1")
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value

    def test_forced_run_decides_from_the_answers_it_just_refreshed(self, store, media):
        # SkipDB withdrew its answer since the first run: the forced run must not publish from the stale agreement.
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        clients = _clients(
            theintrodb=LookupResult("ok", (Candidate(T.INTRO, 60_000, 90_000, Source.THEINTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.INTRO, 61_000, 91_000, Source.SKIPDB),)),
        )
        _run(_ctx(store, reg, settings_raw=INTRO_ONLY, clients=clients), media, {"jellyfin-1": jf})
        assert [(m.start_ms, m.end_ms) for m in _state(store, media, "jellyfin-1").markers] == [(61_000, 90_000)]
        clients["skipdb"].result = NO_DATA
        out, _ = _run(_ctx(store, reg, settings_raw=INTRO_ONLY, clients=clients, force=True), media, {"jellyfin-1": jf})
        assert store.get_decisions(store.get_file(media).id)[T.INTRO].status is DecisionStatus.NEEDS_REVIEW
        assert jf.write.call_args.args == ("item-jellyfin-1", [])
        assert out.publisher_rows[0]["status"] == ServerStatus.WRITTEN.value

    def test_forced_run_with_moved_chapter_times_saves_and_publishes_them(self, store, media):
        # Same status and reason ("chapters"), different times: the stored marker must still be replaced.
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        moved = (
            Chapter(0, 130_000, "Chapter 1"),
            Chapter(130_000, 160_000, "Intro"),
            Chapter(160_000, 1_295_324, "Chapter 2"),
            Chapter(1_295_324, None, "Credits"),
        )
        _run(_ctx(store, reg, force=True), media, {"plex-1": plex}, probe=_probe(moved))
        moved_intro = Marker(T.INTRO, 130_000, 160_000, ("chapters",))
        assert store.get_markers(store.get_file(media).id)[T.INTRO] == moved_intro
        assert plex.write.call_args.args == ("item-plex-1", [moved_intro, CREDITS_CH])

    def test_forced_run_hands_detector_sources_to_the_worker_even_when_decided(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[])
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), detector)
        clients = _clients()
        ctx = _ctx(store, reg, detectors=(spec,), clients=clients, force=True)
        pubs = {"plex-1": ready_publisher()}
        assert _run(ctx, media, pubs, probe=_probe(CHAPTERS_BOTH))[0] is None
        out, probe = _run(ctx, media, pubs, stage="process")
        detector.assert_called_once()
        assert detector.call_args.args[0].canonical_path == media
        assert probe.call_count == 0 and [len(c.calls) for c in clients.values()] == [1, 1, 1]
        assert out.outcome_key == FileOutcome.PUBLISHED.value


class TestConcurrentJobs:
    def test_two_jobs_on_one_file_never_interleave(self, store, media):
        # Job L waits on a lookup for the old cut while Sonarr replaces the file and webhook job N starts on it.
        # N must queue behind L on the file, so L's answer for the old cut can never overwrite N's for the new one.
        old_duration, new_duration = 1_321_000, 1_380_000
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        raw = {**INTRO_ONLY, "publish_when": "medium"}
        entered = []
        n_queued = threading.Event()

        class SpyLock:
            def __init__(self):
                self._lock = threading.Lock()

            def __enter__(self):
                entered.append(threading.current_thread().name)
                if len(entered) == 2:
                    n_queued.set()
                self._lock.acquire()
                return self

            def __exit__(self, *exc):
                self._lock.release()

        results = {}
        clients = _clients()
        ctx_n = _ctx(store, reg, settings_raw=raw, clients=clients)

        def lookup(ids, *, duration_ms, priority, cancel_check=None):
            if duration_ms == old_duration and "N" not in results:
                with open(media, "wb") as f:
                    f.write(b"y" * 200)
                os.utime(media, ns=(9, 9))
                job_n = threading.Thread(target=lambda: results.update(N=check_item(_item(media), ctx=ctx_n)), name="N")
                results["N"] = None
                results["thread"] = job_n
                job_n.start()
                results["queued"] = n_queued.wait(timeout=10)
                return LookupResult("ok", (Candidate(T.INTRO, 128_000, 157_000, Source.THEINTRODB),))
            if duration_ms == old_duration:
                return LookupResult("ok", (Candidate(T.INTRO, 128_000, 157_000, Source.THEINTRODB),))
            return LookupResult("ok", (Candidate(T.INTRO, 186_000, 216_000, Source.THEINTRODB),))

        clients["theintrodb"].lookup = lookup

        def probe(path, *, ffprobe):
            return MediaProbe(new_duration if os.path.getsize(path) == 200 else old_duration, ())

        with (
            patch.object(pipeline, "_PATH_LOCKS", pipeline._KeyedLocks(lock_factory=SpyLock)),
            patch.object(pipeline, "probe_media", side_effect=probe),
            patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: plex),
        ):
            out_l = check_item(_item(media), ctx=_ctx(store, reg, settings_raw=raw, clients=clients))
            results["thread"].join(timeout=10)
            later, _ = _run(_ctx(store, reg, settings_raw=raw, clients=clients), media, {"plex-1": plex})
        assert results["queued"] is True
        new_intro = Marker(T.INTRO, 186_000, 216_000, ("theintrodb",))
        rec = store.get_file(media)
        assert (rec.size, rec.mtime_ns, rec.duration_ms) == (200, 9, new_duration)
        assert [(c.start_ms, c.end_ms) for c in store.get_evidence(rec.id) if c.source is Source.THEINTRODB] == [
            (186_000, 216_000)
        ]
        assert _state(store, media, "plex-1").markers == (new_intro,)
        assert plex.write.call_args.args[1] == [new_intro]
        assert {out_l.outcome_key, results["N"].outcome_key} <= {
            FileOutcome.PUBLISHED.value,
            FileOutcome.UP_TO_DATE.value,
        }
        assert later.outcome_key == FileOutcome.UP_TO_DATE.value

    @pytest.mark.parametrize(
        ("chapters", "write_effect", "runs_before", "status", "rows_written"),
        [
            (CHAPTERS_BOTH, None, 0, ServerStatus.WRITTEN, ["item", "basis", "file"]),
            (
                CHAPTERS_BOTH,
                lambda item_id, markers, **kw: [INTRO_CH],
                0,
                ServerStatus.WAITING,
                ["item", "basis", "file"],
            ),
            (CHAPTERS_BOTH, None, 1, ServerStatus.UP_TO_DATE, []),
            ((), None, 0, ServerStatus.NONE, []),
            (CHAPTERS_BOTH, ItemNotFoundError("gone"), 0, ServerStatus.WAITING, ["clear basis", "file"]),
            (CHAPTERS_BOTH, PublishError("busy"), 0, ServerStatus.FAILED, ["item", "clear basis", "file"]),
            (CHAPTERS_BOTH, RuntimeError("bug"), 0, ServerStatus.FAILED, ["item", "clear basis", "file"]),
        ],
        ids=["written", "waiting", "up-to-date", "nothing-to-show", "item-not-found", "publish-error", "unexpected"],
    )
    def test_the_item_lock_covers_the_item_row_read_the_write_and_the_rows_written_after(
        self, store, media, chapters, write_effect, runs_before, status, rows_written
    ):
        # Versions of one Plex item publish on different threads, each holding only its own path's lock: one version
        # must never read the item row before another's write and record the item after it.
        reg = _registry(media, ServerType.PLEX)
        settings = {"sources": [{"id": "server_markers", "enabled": False}]}
        plex = ready_publisher()
        for _ in range(runs_before):
            _run(_ctx(store, reg, settings_raw=settings), media, {"plex-1": plex}, probe=_probe(chapters))
        effect = plex.succeed if write_effect is None else write_effect

        class OwnedLock:
            def __init__(self):
                self._lock, self.owner = threading.Lock(), None

            def __enter__(self):
                self._lock.acquire()
                self.owner = threading.get_ident()

            def __exit__(self, *exc):
                self.owner = None
                self._lock.release()

        locks = pipeline._KeyedLocks(lock_factory=OwnedLock)
        seen = []

        def spied(label, fn):
            def call(*args, **kwargs):
                entry = locks._locks.get(("plex-1", "item-plex-1"))
                seen.append((label, entry is not None and entry[0].owner == threading.get_ident()))
                return fn(*args, **kwargs)

            return call

        labels = {
            "get_item_publish_state": "read item",
            "set_item_publish_state": "item",
            "set_publish_basis": "basis",
            "clear_publish_basis": "clear basis",
            "set_publish_state": "file",
        }
        for name, label in labels.items():
            setattr(store, name, spied(label, getattr(store, name)))

        def write(*args, **kwargs):
            if isinstance(effect, Exception):
                raise effect
            return effect(*args, **kwargs)

        plex.write.side_effect = spied("write", write)
        with patch.object(pipeline, "_ITEM_LOCKS", locks):
            out, _ = _run(_ctx(store, reg, settings_raw=settings), media, {"plex-1": plex}, probe=_probe(chapters))
        assert out.publisher_rows[0]["status"] == status.value
        assert [label for label, _ in seen if label not in ("read item", "write")] == rows_written
        assert seen[0] == ("read item", True)
        assert all(held for _, held in seen), seen
        assert locks._locks == {}

    def test_a_keyed_lock_stays_while_a_waiter_is_queued_so_a_later_arrival_waits_too(self):
        # Holder H, waiter W queued behind it, then T arrives while W holds the lock. Dropping the entry when H leaves
        # would give T a fresh lock and let it in next to W.
        arrived = {name: threading.Event() for name in "HWT"}
        inside = {name: threading.Event() for name in "HWT"}
        leave = {name: threading.Event() for name in "HW"}

        class ArrivalLock:
            def __init__(self):
                self._lock = threading.Lock()

            def __enter__(self):
                arrived[threading.current_thread().name].set()
                self._lock.acquire()

            def __exit__(self, *exc):
                self._lock.release()

        locks = pipeline._KeyedLocks(lock_factory=ArrivalLock)
        key = ("plex-1", "7")

        def run():
            name = threading.current_thread().name
            with locks.hold(key):
                inside[name].set()
                if name in leave:
                    assert leave[name].wait(10)

        threads = {name: threading.Thread(target=run, name=name) for name in "HWT"}
        threads["H"].start()
        assert inside["H"].wait(10)
        threads["W"].start()
        assert arrived["W"].wait(10)
        assert not inside["W"].is_set() and locks._locks[key][1] == 2

        leave["H"].set()
        assert inside["W"].wait(10)
        threads["H"].join(10)
        assert key in locks._locks and locks._locks[key][1] == 1  # W still holds the entry H's release must not drop

        threads["T"].start()
        assert arrived["T"].wait(10)
        assert not inside["T"].wait(0.2)  # queued behind W, on the same lock
        leave["W"].set()
        assert inside["T"].wait(10)
        for thread in threads.values():
            thread.join(10)
        assert locks._locks == {}

    def test_path_locks_are_released_and_forgotten(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        _run(_ctx(store, reg), media, {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))
        assert pipeline._PATH_LOCKS._locks == {}


def test_markers_for_path(store, media):
    assert pipeline.markers_for_path(store, media) is None
    st = os.stat(media)
    rec = store.upsert_file(
        FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
    )
    assert pipeline.markers_for_path(store, media) is None  # known file, never decided
    reg = _registry(media, ServerType.PLEX)
    _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
    assert pipeline.markers_for_path(store, media) == {}  # decided: nothing to show
    store.lock_marker(rec.id, Marker(T.INTRO, 5_000, 30_000, ("user",), locked=True))
    assert pipeline.markers_for_path(store, media) == {T.INTRO: Marker(T.INTRO, 5_000, 30_000, ("user",), locked=True)}


class TestHandlersAndContext:
    def test_kind_handlers_bind_the_context(self, store, media):
        ctx = _ctx(store, _registry(media, ServerType.PLEX))
        handlers = pipeline.kind_handlers(ctx)
        item = _item(media)
        cancel = MagicMock()
        with (
            patch.object(pipeline, "check_item", return_value=None) as check,
            patch.object(pipeline, "process_item", return_value=ItemOutcome("markers_none")) as process,
        ):
            handlers.check_fn(item, cancel_check=cancel)
            handlers.process_fn(item, gpu="NVIDIA", gpu_device_path="cuda:0", pause_check=cancel)
        check.assert_called_once_with(item, ctx=ctx, cancel_check=cancel)
        process.assert_called_once_with(item, ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0", pause_check=cancel)
        assert handlers.outcome_keys == OUTCOME_KEYS
        assert handlers.check_label == "Looking up markers…" and handlers.check_worker_label == "Intro & Credits"
        assert handlers.check_share == 0.25

    def test_build_clients_for_enabled_online_sources_only(self):
        from media_preview_generator.markers.sources.introdb import IntroDbClient
        from media_preview_generator.markers.sources.theintrodb import TheIntroDbClient

        raw = {
            "sources": [{"id": "theintrodb", "enabled": True, "api_key": "k-123"}, {"id": "skipdb", "enabled": False}]
        }
        clients = pipeline.build_clients(load_global(validate_global(raw, None)[0]))
        assert set(clients) == {"theintrodb", "introdb"}
        assert isinstance(clients["theintrodb"], TheIntroDbClient) and isinstance(clients["introdb"], IntroDbClient)
        assert clients["theintrodb"]._api_key == "k-123"

    def test_build_context_uses_live_settings_store_and_ffprobe(self, store):
        settings = load_global({})
        config = MagicMock(ffmpeg_path="/usr/lib/jellyfin-ffmpeg/ffmpeg")
        registry = MagicMock()
        with (
            patch.object(pipeline, "get_global_settings", return_value=settings),
            patch.object(pipeline, "get_marker_store", return_value=store),
            patch.object(pipeline, "ffprobe_path_for", return_value="/x/ffprobe") as ffprobe_for,
            patch.object(pipeline, "build_clients", return_value={"introdb": "client"}) as build,
        ):
            ctx = pipeline.build_context(registry=registry, config=config, priority=3, force=True)
        ffprobe_for.assert_called_once_with("/usr/lib/jellyfin-ffmpeg/ffmpeg")
        build.assert_called_once_with(settings)
        assert (ctx.registry, ctx.config, ctx.settings, ctx.store) == (registry, config, settings, store)
        assert (ctx.priority(), ctx.force, ctx.ffprobe, ctx.clients) == (3, True, "/x/ffprobe", {"introdb": "client"})
        assert ctx.local_detectors == ()
        live = {"priority": 3}
        with (
            patch.object(pipeline, "get_global_settings", return_value=settings),
            patch.object(pipeline, "get_marker_store", return_value=store),
            patch.object(pipeline, "build_clients", return_value={}),
        ):
            ctx = pipeline.build_context(registry=registry, config=config, priority=lambda: live["priority"])
        live["priority"] = 1
        assert ctx.priority() == 1


W, U, R, S, A, F, N = (
    ServerStatus.WRITTEN,
    ServerStatus.UP_TO_DATE,
    ServerStatus.NEEDS_REVIEW,
    ServerStatus.SKIPPED,
    ServerStatus.WAITING,
    ServerStatus.FAILED,
    ServerStatus.NONE,
)


@pytest.mark.parametrize(
    ("statuses", "needs_review", "expected"),
    [
        # Every pair of per-server statuses for two owners (NEEDS_REVIEW rows only exist when needs_review is True).
        ({W}, False, FileOutcome.PUBLISHED),
        ({W, U}, False, FileOutcome.PUBLISHED),
        ({W, S}, False, FileOutcome.PUBLISHED),
        ({W, A}, False, FileOutcome.PUBLISHED),
        ({W, F}, False, FileOutcome.PUBLISHED),
        ({W, N}, False, FileOutcome.PUBLISHED),
        ({U}, False, FileOutcome.UP_TO_DATE),
        ({U, S}, False, FileOutcome.UP_TO_DATE),
        ({U, A}, False, FileOutcome.UP_TO_DATE),
        ({U, F}, False, FileOutcome.FAILED),
        ({U, N}, False, FileOutcome.UP_TO_DATE),
        ({S}, False, FileOutcome.SKIPPED),
        ({S, A}, False, FileOutcome.WAITING),
        ({S, F}, False, FileOutcome.FAILED),
        ({S, N}, False, FileOutcome.NO_MARKERS),
        ({A}, False, FileOutcome.WAITING),
        ({A, F}, False, FileOutcome.FAILED),
        ({A, N}, False, FileOutcome.WAITING),
        ({F}, False, FileOutcome.FAILED),
        ({F, N}, False, FileOutcome.FAILED),
        ({N}, False, FileOutcome.NO_MARKERS),
        (set(), False, FileOutcome.NO_MARKERS),
        ({R}, True, FileOutcome.NEEDS_REVIEW),
        ({R, W}, True, FileOutcome.PUBLISHED),
        ({R, U}, True, FileOutcome.UP_TO_DATE),
        ({R, S}, True, FileOutcome.NEEDS_REVIEW),
        ({R, A}, True, FileOutcome.WAITING),
        ({R, F}, True, FileOutcome.FAILED),
        ({R, N}, True, FileOutcome.NEEDS_REVIEW),
        ({S}, True, FileOutcome.NEEDS_REVIEW),
        ({N}, True, FileOutcome.NEEDS_REVIEW),
        ({A}, True, FileOutcome.WAITING),
    ],
)
def test_file_outcome_precedence(statuses, needs_review, expected):
    assert file_outcome({s.value for s in statuses}, needs_review=needs_review) is expected


def test_outcome_keys_are_every_file_outcome_in_order():
    assert OUTCOME_KEYS == tuple(o.value for o in FileOutcome)
    assert OUTCOME_KEYS == (
        "markers_published",
        "markers_up_to_date",
        "markers_waiting",
        "markers_needs_review",
        "markers_skipped",
        "markers_none",
        "markers_no_owners",
        "skipped_file_not_found",
        "failed",
    )
