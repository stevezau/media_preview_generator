"""Pipeline matrix: owners, identity, kind, evidence order + early stop, decisions, publish fan-out, outcomes."""

from __future__ import annotations

import copy
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome
from media_preview_generator.markers import pipeline
from media_preview_generator.markers.audio.fingerprint import ChromaprintState
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
    Shown,
)
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, ServerType
from tests.markers.fakes import FakeClient, FakePlexItems, FakeRegistry, ready_publisher, server_config
from tests.markers.test_external_ids import EXTRA_SUFFIXES, EXTRAS_FOLDERS

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
        now=now or (lambda: datetime(2026, 9, 13, tzinfo=UTC)),
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
        # A server type the factory builds no publisher for (none given here): the owner counts, its row says why.
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
        factory_calls = []

        def factory(server, cfg, **kw):
            factory_calls.append(cfg.id)
            return ready_publisher("jellyfin_bridge")

        with (
            patch.object(pipeline, "probe_media", return_value=_probe()),
            patch.object(pipeline, "publisher_for", side_effect=factory),
        ):
            out = check_item(_item(media), ctx=_ctx(store, reg))
        assert list(_rows(out)) == ["jellyfin-1"]
        assert factory_calls == ["jellyfin-1"]
        reg.get("jellyfin-1").get_media_segments.assert_called_once_with("item-jellyfin-1")
        reg.get("plex-1").get_markers.assert_not_called()
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
        _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        reg.get("plex-1").get_markers.assert_called_once_with("item-plex-1")
        reg.get("jellyfin-1").get_media_segments.assert_called_once_with("item-jellyfin-1")

    @pytest.mark.parametrize(
        ("stype", "prepare", "message"),
        [
            (ServerType.PLEX, lambda server: None, "Plex database not found"),
            (ServerType.JELLYFIN, lambda server: setattr(server.get_bridge_info, "return_value", None), "reach"),
            (
                ServerType.EMBY,
                lambda server: setattr(server.get_bridge_info, "return_value", {"installed": False, "features": []}),
                "Install the Media Preview Bridge for Emby plugin",
            ),
        ],
        ids=["plex-no-db", "jellyfin-unreachable", "emby-no-plugin"],
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
        server.put_emby_markers.assert_not_called()


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


EXTRA_SHAPES = [
    *(f"Toy Story (1995) {{tmdb-862}}/Toy Story (1995)-{suffix}.mkv" for suffix in EXTRA_SUFFIXES),
    *(f"Toy Story (1995) {{tmdb-862}}/{folder}/Making Of.mkv" for folder in EXTRAS_FOLDERS),
    "Rick and Morty (2013) {tvdb-275274}/Season 01/Extras/Rick and Morty (2013) - S01E01 - Animatic.mkv",
]


class TestExtras:
    """Trailers and other extras are never checked: no server has them as items, so they would wait and retry forever."""

    def _extra(self, tmp_path, shape):
        path = tmp_path / "media" / "movies" / shape
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 100)
        return str(path)

    @pytest.mark.parametrize("stage", ["check", "process"])
    @pytest.mark.parametrize("shape", EXTRA_SHAPES)
    def test_an_extra_is_skipped_before_any_probe_owner_lookup_or_publish(self, store, tmp_path, shape, stage):
        path = self._extra(tmp_path, shape)
        reg = _registry(path, ServerType.PLEX, ServerType.JELLYFIN)
        for sid in ("plex-1", "jellyfin-1"):
            reg.get(sid).resolve_remote_path_to_item_id.return_value = None  # like a real server: not an item
        clients = _clients()
        pubs = {"plex-1": ready_publisher(), "jellyfin-1": ready_publisher("jellyfin_bridge")}
        with patch.object(pipeline, "owning_servers") as owners:
            out, probe = _run(_ctx(store, reg, clients=clients), path, pubs, probe=_probe(CHAPTERS_BOTH), stage=stage)
        assert (out.outcome_key, out.message) == (FileOutcome.SKIPPED.value, "Extras aren't checked for markers")
        assert not out.publisher_rows
        owners.assert_not_called()
        probe.assert_not_called()
        assert [c.calls for c in clients.values()] == [[], [], []]
        for sid, pub in pubs.items():
            pub.capability.assert_not_called()
            pub.write.assert_not_called()
            reg.get(sid).resolve_remote_path_to_item_id.assert_not_called()
        assert store.get_file(path) is None

    def test_the_episode_next_to_extras_is_still_published(self, store, media):
        season = os.path.dirname(media)
        trailer = os.path.join(season, "Rick and Morty (2013) - S01E01 - Pilot-trailer.mkv")
        featurette = os.path.join(season, "Featurettes", "Rick and Morty (2013) - S01E01 - Pilot.mkv")
        os.makedirs(os.path.dirname(featurette))
        for extra in (trailer, featurette):
            with open(extra, "wb") as f:
                f.write(b"x" * 100)
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        ctx = _ctx(store, reg)
        outcomes = {}
        with (
            patch.object(pipeline, "probe_media", return_value=_probe(CHAPTERS_BOTH)) as probe,
            patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: plex),
        ):
            for path in (trailer, media, featurette):
                outcomes[path] = check_item(_item(path), ctx=ctx).outcome_key
        assert outcomes == {
            trailer: FileOutcome.SKIPPED.value,
            media: FileOutcome.PUBLISHED.value,
            featurette: FileOutcome.SKIPPED.value,
        }
        assert [c.args[0] for c in probe.call_args_list] == [media]
        assert plex.write.call_args_list[0].args == ("item-plex-1", [INTRO_CH, CREDITS_CH])
        assert plex.write.call_args_list[0].kwargs["canonical_path"] == media
        assert plex.write.call_count == 1


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

    def test_changed_file_with_identical_markers_is_offered_again_to_every_server(self, store, media):
        # A changed identity clears every server's basis, so each publisher decides whether its server needs them
        # again (the Jellyfin plugin must store the new file's size: TestJellyfinReadBackVerify in the contract tests).
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
        rows = _rows(out)
        # Neither write changed its server here; both are still checked again later: servers rescan a replaced file.
        assert (rows["plex-1"]["status"], rows["jellyfin-1"]["status"]) == (
            ServerStatus.UP_TO_DATE.value,
            ServerStatus.UP_TO_DATE.value,
        )
        assert rows["plex-1"]["verify_later"] is True and rows["jellyfin-1"]["verify_later"] is True
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value

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
        assert server.get_external_ids.call_count == 1  # the kind and the ids in one answer
        _run(
            _ctx(store, reg, clients=clients, force=True),
            ambiguous,
            {"jellyfin-1": ready_publisher("jellyfin_bridge")},
            probe=_probe(CHAPTERS_OPENING),
        )
        assert server.get_external_ids.call_count == 2  # the kind came from the store; the ids had to be fetched
        assert [call["ids"] for call in clients["theintrodb"].calls] == [expected, expected]

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
        raw = {"sources": [{"id": sid, "enabled": False} for sid in ("theintrodb", "introdb", "skipdb")]}
        _run(_ctx(store, reg, settings_raw=raw), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
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
    def test_chapters_decide_both_and_the_other_sources_are_still_asked(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        ctx = _ctx(store, reg)
        out, _ = _run(ctx, media, {"plex-1": plex, "jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert [len(c.calls) for c in ctx.clients.values()] == [1, 1, 1]
        reg.get("plex-1").get_markers.assert_called_once_with("item-plex-1")
        reg.get("jellyfin-1").get_media_segments.assert_called_once_with("item-jellyfin-1")
        for pub, sid in ((plex, "plex-1"), (jf, "jellyfin-1")):
            args, kwargs = pub.write.call_args
            assert args == (f"item-{sid}", [INTRO_CH, CREDITS_CH])
            assert kwargs == {
                "previous": [],
                "own_previous": None,
                "duration_ms": DUR,
                "canonical_path": media,
                "kept_types": frozenset(),
            }
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
            # A type the sources don't agree on outranks the one written: only the user settles it.
            status = ServerStatus.WRITTEN
            outcome = FileOutcome.NEEDS_REVIEW if needs_review else FileOutcome.PUBLISHED
            assert plex.write.call_args.args == ("item-plex-1", expected)
        else:
            plex.write.assert_not_called()
            status = ServerStatus.NEEDS_REVIEW if needs_review else ServerStatus.NONE
            outcome = FileOutcome.NEEDS_REVIEW if needs_review else FileOutcome.NO_MARKERS
        assert [r["status"] for r in out.publisher_rows] == [status.value]
        assert out.outcome_key == outcome.value
        # Chapters alone never end the search (a normal-priority run asks every source); only nothing to detect does.
        assert len(clients["theintrodb"].calls) == (0 if intro == credits == "disabled" else 1)
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
        clients = _clients(skipdb=LookupResult("ok", (Candidate(T.INTRO, 127_894, 156_824, Source.SKIPDB),)))
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        medium = {**INTRO_ONLY, "publish_when": "medium"}
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=medium), media, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert [len(c.calls) for c in clients.values()] == [1, 1, 1]
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 127_894, 156_824, ("skipdb",))]

    def test_theintrodb_alone_never_publishes_even_at_medium(self, store, media):
        # TheIntroDB answers the closest cut it has, whatever this file's duration (audit B S2: 81 s of cold open).
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        out, _ = _run(
            _ctx(store, reg, clients=clients, settings_raw={**INTRO_ONLY, "publish_when": "medium"}),
            media,
            {"plex-1": plex},
        )
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        plex.write.assert_not_called()

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
        clients = _clients(skipdb=LookupResult("ok", (Candidate(T.INTRO, 127_000, 157_000, Source.SKIPDB),)))
        raw = {
            "sources": [{"id": "skipdb", "enabled": True}, {"id": "theintrodb", "enabled": True}],
            "detect": {"intro": True, "credits": False},
            "publish_when": "medium",
        }
        plex = ready_publisher()
        _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, {"plex-1": plex})
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 127_000, 157_000, ("skipdb",))]
        assert [len(c.calls) for c in clients.values()] == [0, 0, 1]
        # The server is still read before the first publish: its own marker could shorten the decided skip (rule 7).
        reg.get("plex-1").get_markers.assert_called_once_with("item-plex-1")

    def test_a_server_read_after_everything_is_decided_shortens_agreed_credits(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex_server = reg.get("plex-1")
        plex_server.get_markers.return_value = [
            {"type": "credits", "start_ms": 1_255_500, "end_ms": DUR, "final": True}
        ]
        plex_server.get_part_durations.return_value = [DUR]
        clients = _clients(
            introdb=LookupResult("ok", (Candidate(T.CREDITS, 1_239_000, None, Source.INTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.CREDITS, 1_241_000, None, Source.SKIPDB),)),
        )
        plex = ready_publisher()
        _run(_ctx(store, reg, clients=clients, settings_raw=CREDITS_DEFAULTS), media, {"plex-1": plex})
        shortened = Marker(T.CREDITS, 1_255_500, DUR, ("introdb", "skipdb", "server_markers"))
        assert plex.write.call_args.args == ("item-plex-1", [shortened])
        decision = store.get_decisions(store.get_file(media).id)[T.CREDITS]
        assert decision.reason == "sources agree: introdb, skipdb; start shortened to the server's own marker (plex-1)"
        plex_server.get_markers.assert_called_once_with("item-plex-1")

    def test_chapters_alone_keep_the_search_open_so_agreeing_sources_veto_them_on_the_first_run(self, store, media):
        # Audit B S1: the generic "Intro" chapter is the cold open (0-95 s); the theme sits in an unnamed chapter
        # (95-126 s) that IntroDB and SkipDB agree on. A normal run must not stop at the chapter.
        chapters = (
            Chapter(0, 95_000, "Intro"),
            Chapter(95_000, 126_000, "Chapter 2"),
            Chapter(126_000, 1_300_000, "Chapter 3"),
            Chapter(1_300_000, None, "Chapter 4"),
        )
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        clients = _clients(
            introdb=LookupResult("ok", (Candidate(T.INTRO, 95_500, 126_000, Source.INTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.INTRO, 96_000, 125_400, Source.SKIPDB),)),
        )
        raw = {
            "sources": [
                {"id": "chapters", "enabled": True},
                {"id": "introdb", "enabled": True},
                {"id": "skipdb", "enabled": True},
                {"id": "theintrodb", "enabled": False},
            ],
            "detect": {"intro": True, "credits": False},
        }
        for force in (False, False, True, False):
            ctx = _ctx(store, reg, clients=clients, settings_raw=raw, force=force)
            out, _ = _run(ctx, media, {"jellyfin-1": jf}, probe=_probe(chapters, duration=1_420_000))
            decision = store.get_decisions(store.get_file(media).id)[T.INTRO]
            assert (decision.status, decision.reason) == (
                DecisionStatus.NEEDS_REVIEW,
                "chapters contradicted by agreeing sources: introdb, skipdb",
            )
            assert (decision.proposed_start_ms, decision.proposed_end_ms) == (0, 95_000)
            assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        jf.write.assert_not_called()  # never published, so nothing to take back
        # The first run asked both; the forced run asked again; normal runs reuse the stored answers.
        assert (len(clients["introdb"].calls), len(clients["skipdb"].calls)) == (2, 2)

    @pytest.mark.parametrize(
        ("priority", "chapters", "force", "theintrodb_asked"),
        [
            (3, CHAPTERS_BOTH, False, False),  # Low: TheIntroDB's daily budget isn't spent only to confirm chapters
            (2, CHAPTERS_BOTH, False, True),  # Normal (webhook follow-ups) may spend it
            (1, CHAPTERS_BOTH, False, True),  # High (Inspector re-detect) may spend it
            (3, CHAPTERS_BOTH[:3], False, True),  # credits still open: TheIntroDB might decide them
            (3, CHAPTERS_BOTH, True, True),  # a forced re-detect asks every source
        ],
        ids=["low", "normal", "high", "low-credits-open", "low-forced"],
    )
    def test_theintrodb_is_skipped_at_low_priority_when_only_confirming_chapters(
        self, store, media, priority, chapters, force, theintrodb_asked
    ):
        reg = _registry(media, ServerType.PLEX)
        clients = _clients()
        ctx = _ctx(store, reg, clients=clients, force=force)
        ctx.priority = lambda: priority
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(chapters))
        assert len(clients["theintrodb"].calls) == (1 if theintrodb_asked else 0)
        assert [c["priority"] for c in clients["theintrodb"].calls] == ([priority] if theintrodb_asked else [])
        # The free sources and the servers are still asked: two of them agreeing could still veto the chapters.
        assert (len(clients["introdb"].calls), len(clients["skipdb"].calls)) == (1, 1)
        reg.get("plex-1").get_markers.assert_called_once_with("item-plex-1")

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
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
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


# TheIntroDB's daily reserve, spent by other jobs, refuses this file's lookup exactly like a real
# SourceLimiter.acquire() would (spec finding 4: this used to vanish into a DEBUG line with no trace on the file
# or the job).
TIDB_BUDGET_EXHAUSTED = LookupResult("unavailable", detail="TheIntroDB budget_exhausted")
SKIPDB_BUDGET_EXHAUSTED = LookupResult("unavailable", detail="SkipDB budget_exhausted")
INTRODB_BUDGET_EXHAUSTED = LookupResult("unavailable", detail="IntroDB budget_exhausted")


class TestBudgetExhaustedFileNote:
    def test_note_appears_when_the_result_could_still_change(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        clients = _clients(theintrodb=TIDB_BUDGET_EXHAUSTED)
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": ready_publisher()})
        assert out.message.endswith("; TheIntroDB not checked (daily limit reached)")

    def test_note_omitted_once_other_sources_already_decided_it(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        raw = {
            "sources": [
                {"id": "theintrodb", "enabled": True},
                {"id": "introdb", "enabled": True},
                {"id": "skipdb", "enabled": True},
            ],
            "detect": {"intro": True, "credits": False},
        }
        clients = _clients(
            theintrodb=TIDB_BUDGET_EXHAUSTED,
            introdb=LookupResult("ok", (Candidate(T.INTRO, 128_000, 157_000, Source.INTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.INTRO, 129_000, 157_800, Source.SKIPDB),)),
        )
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, {"plex-1": ready_publisher()})
        assert "not checked" not in out.message
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_note_omitted_when_nothing_ran_out(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        out, _ = _run(_ctx(store, reg, settings_raw=INTRO_ONLY), media, {"plex-1": ready_publisher()})
        assert "not checked" not in out.message

    def test_no_answer_is_stored_so_the_next_run_asks_theintrodb_again(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        client = FakeClient(TIDB_BUDGET_EXHAUSTED)
        clients = _clients(theintrodb=TIDB_BUDGET_EXHAUSTED)
        clients["theintrodb"] = client
        _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": ready_publisher()})
        assert store.evidence_fetched_at(store.get_file(media).id, Source.THEINTRODB) is None
        assert len(client.calls) == 1
        client.result = LookupResult("ok", (TIDB_INTRO,))
        _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": ready_publisher()})
        assert len(client.calls) == 2  # nothing stored last time, so a normal run asks again (no retry needed)
        assert store.evidence_fetched_at(store.get_file(media).id, Source.THEINTRODB) is not None


class TestBudgetExhaustedJobWarning:
    def test_no_warning_when_nothing_ran_out(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        ctx = _ctx(store, reg, settings_raw=INTRO_ONLY)  # default clients answer no_data, never budget_exhausted
        _run(ctx, media, {"plex-1": ready_publisher()})
        assert pipeline.budget_exhausted_warnings(ctx) == []

    def test_singular_wording_for_one_file(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        clients = _clients(theintrodb=TIDB_BUDGET_EXHAUSTED)
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY)
        _run(ctx, media, {"plex-1": ready_publisher()})
        assert pipeline.budget_exhausted_warnings(ctx) == [
            "TheIntroDB's daily lookup limit was reached: 1 file was checked without it. "
            "It resets at 00:00 UTC; run the library again after that "
            "(or add a TheIntroDB API key for a higher limit)."
        ]

    def test_counts_every_file_this_job_across_calls(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        other1 = os.path.join(os.path.dirname(media), "Rick and Morty (2013) - S01E02 - Lawnmower Dog.mkv")
        other2 = os.path.join(os.path.dirname(media), "Rick and Morty (2013) - S01E03 - Anatomy Park.mkv")
        for p in (other1, other2):
            open(p, "wb").write(b"y" * 10)
        clients = _clients(theintrodb=TIDB_BUDGET_EXHAUSTED)
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY)
        for p in (media, other1, other2):
            _run(ctx, p, {"plex-1": ready_publisher()})
        assert pipeline.budget_exhausted_warnings(ctx) == [
            "TheIntroDB's daily lookup limit was reached: 3 files were checked without it. "
            "It resets at 00:00 UTC; run the library again after that "
            "(or add a TheIntroDB API key for a higher limit)."
        ]

    def test_a_file_asked_again_on_its_worker_counts_once(self, store, media):
        # The checking thread asks TheIntroDB, then season audio needs a worker; the worker stage asks again.
        reg = _registry(media, ServerType.PLEX)
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), MagicMock(return_value=[]))
        client = FakeClient(TIDB_BUDGET_EXHAUSTED)
        ctx = _ctx(store, reg, clients={"theintrodb": client}, detectors=(spec,), settings_raw=INTRO_ONLY)
        assert _run(ctx, media, {"plex-1": ready_publisher()})[0] is None
        _run(ctx, media, {"plex-1": ready_publisher()}, stage="process")
        assert len(client.calls) == 2
        assert pipeline.budget_exhausted_warnings(ctx)[0].startswith(
            "TheIntroDB's daily lookup limit was reached: 1 file was checked without it."
        )

    def test_a_file_whose_worker_stage_got_an_answer_is_not_counted(self, store, media):
        # The budget reset (00:00 UTC) between the checking thread and the worker: the file was checked with it.
        reg = _registry(media, ServerType.PLEX)
        spec = LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), MagicMock(return_value=[]))
        client = FakeClient(TIDB_BUDGET_EXHAUSTED)
        ctx = _ctx(store, reg, clients={"theintrodb": client}, detectors=(spec,), settings_raw=INTRO_ONLY)
        assert _run(ctx, media, {"plex-1": ready_publisher()})[0] is None
        client.result = LookupResult("ok", (TIDB_INTRO,))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, stage="process")
        assert "not checked" not in out.message
        assert pipeline.budget_exhausted_warnings(ctx) == []

    def test_a_file_detected_again_after_it_changed_counts_once(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        client = FakeClient(TIDB_BUDGET_EXHAUSTED)
        ctx = _ctx(store, reg, clients={"theintrodb": client}, settings_raw=INTRO_ONLY)
        probes = []

        def probe(path, *, ffprobe):
            probes.append(path)
            if len(probes) == 1:
                os.utime(path, ns=(7, 7))  # replaced while its chapters were read: detected again from scratch
            return _probe()

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe_effect=probe)
        assert len(probes) == 2 and len(client.calls) == 2
        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        assert pipeline.budget_exhausted_warnings(ctx)[0].startswith(
            "TheIntroDB's daily lookup limit was reached: 1 file was checked without it."
        )

    def test_names_each_source_that_ran_out_and_only_theintrodb_gets_the_key_hint(self, store, media):
        # All three online sources in the same job: only TheIntroDB has a key concept, so IntroDB and SkipDB (which
        # take the identical no-hint branch) are both covered rather than assuming one stands in for the other.
        reg = _registry(media, ServerType.PLEX)
        raw = {
            "sources": [
                {"id": "theintrodb", "enabled": True},
                {"id": "skipdb", "enabled": True},
                {"id": "introdb", "enabled": True},
            ],
            "detect": {"intro": True, "credits": False},
        }
        clients = _clients(
            theintrodb=TIDB_BUDGET_EXHAUSTED, skipdb=SKIPDB_BUDGET_EXHAUSTED, introdb=INTRODB_BUDGET_EXHAUSTED
        )
        ctx = _ctx(store, reg, clients=clients, settings_raw=raw)
        _run(ctx, media, {"plex-1": ready_publisher()})
        warnings = pipeline.budget_exhausted_warnings(ctx)
        assert len(warnings) == 3
        # Sorted by name: IntroDB, SkipDB, TheIntroDB.
        assert warnings[0].startswith("IntroDB's daily lookup limit was reached: 1 file")
        assert "API key" not in warnings[0]
        assert warnings[1].startswith("SkipDB's daily lookup limit was reached: 1 file")
        assert "API key" not in warnings[1]
        assert warnings[2].startswith("TheIntroDB's daily lookup limit was reached: 1 file")
        assert "add a TheIntroDB API key for a higher limit" in warnings[2]

    @pytest.mark.parametrize(
        "detail",
        [
            "TheIntroDB rejected the API key (HTTP 401)",
            "TheIntroDB rejected the API key (HTTP 403)",
            "TheIntroDB requires an API key (HTTP 401)",
            "TheIntroDB API key contains invalid characters",
        ],
    )
    def test_a_refused_key_gives_one_job_warning_for_every_file(self, store, media, detail):
        reg = _registry(media, ServerType.PLEX)
        others = [os.path.join(os.path.dirname(media), f"Rick and Morty (2013) - S01E0{n}.mkv") for n in (2, 3)]
        for p in others:
            open(p, "wb").write(b"y" * 10)
        ctx = _ctx(store, reg, clients=_clients(theintrodb=LookupResult("unavailable", detail=detail)))
        for p in (media, *others):
            _run(ctx, p, {"plex-1": ready_publisher()})
        assert pipeline.budget_exhausted_warnings(ctx) == [
            f"{detail}: 3 files were checked without it. Check the TheIntroDB API key in Settings → Intro & Credits."
        ]

    @pytest.mark.parametrize(
        "detail",
        ["TheIntroDB HTTP 500", "TheIntroDB network error: ConnectTimeout", "TheIntroDB rate_limited"],
    )
    def test_other_unavailable_answers_give_no_job_warning(self, store, media, detail):
        reg = _registry(media, ServerType.PLEX)
        ctx = _ctx(store, reg, clients=_clients(theintrodb=LookupResult("unavailable", detail=detail)))
        _run(ctx, media, {"plex-1": ready_publisher()})
        assert pipeline.budget_exhausted_warnings(ctx) == []

    def test_a_refused_key_and_a_used_up_budget_each_get_their_line(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        clients = _clients(
            theintrodb=LookupResult("unavailable", detail="TheIntroDB rejected the API key (HTTP 401)"),
            skipdb=SKIPDB_BUDGET_EXHAUSTED,
        )
        ctx = _ctx(store, reg, clients=clients)
        _run(ctx, media, {"plex-1": ready_publisher()})
        warnings = pipeline.budget_exhausted_warnings(ctx)
        assert [w.split(":")[0] for w in warnings] == [
            "SkipDB's daily lookup limit was reached",
            "TheIntroDB rejected the API key (HTTP 401)",
        ]

    def test_warns_once_per_job_per_source_not_per_file(self, store, media, loguru_caplog):
        reg = _registry(media, ServerType.PLEX)
        other = os.path.join(os.path.dirname(media), "Rick and Morty (2013) - S01E02 - Lawnmower Dog.mkv")
        open(other, "wb").write(b"y" * 10)
        clients = _clients(theintrodb=TIDB_BUDGET_EXHAUSTED)
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY)
        for p in (media, other):
            _run(ctx, p, {"plex-1": ready_publisher()})
        budget_lines = [r for r in loguru_caplog.records if "daily lookup budget ran out" in r.getMessage()]
        assert len(budget_lines) == 1
        assert budget_lines[0].levelname == "WARNING"
        assert "TheIntroDB" in budget_lines[0].getMessage()


def _ticks(ms):
    return ms * 10_000


_VENDOR_READ = {
    ServerType.PLEX: "get_markers",
    ServerType.JELLYFIN: "get_media_segments",
    ServerType.EMBY: "get_chapter_markers",
}


def _vendor_read(reg, sid):
    """The vendor client call that reads a server's markers."""
    return getattr(reg.get(sid), _VENDOR_READ[reg.get_config(sid).type])


def _serve_intro(reg, sid, start_ms, end_ms):
    """Make a server serve one intro marker of its own through its vendor client."""
    server = reg.get(sid)
    stype = reg.get_config(sid).type
    if stype is ServerType.PLEX:
        server.get_markers.return_value = [{"type": "intro", "start_ms": start_ms, "end_ms": end_ms, "final": False}]
    elif stype is ServerType.JELLYFIN:
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": _ticks(start_ms), "EndTicks": _ticks(end_ms)}
        ]
    else:
        server.get_chapter_markers.return_value = [
            {"marker_type": "IntroStart", "start_ms": start_ms, "name": ""},
            {"marker_type": "IntroEnd", "start_ms": end_ms, "name": ""},
        ]


class TestServerMarkers:
    def test_server_markers_confirm_online_source_and_are_read_once_before_publishing(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        _serve_intro(reg, "plex-1", 127_000, 158_000)
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        plex = ready_publisher()
        out, _ = _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"plex-1": plex})
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        reg.get("plex-1").get_markers.assert_called_once_with("item-plex-1")
        _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY, force=True), media, {"plex-1": plex})
        assert reg.get("plex-1").get_markers.call_count == 1  # never re-read a server we've published to
        assert plex.write.call_args.args[1] == [Marker(T.INTRO, 127_894, 156_824, ("theintrodb", "server_markers"))]

    def test_server_markers_read_from_owner_without_markers_enabled(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN, ServerType.EMBY)
        reg.configs_by_id["plex-1"].markers.update({"enabled": False})
        _run(_ctx(store, reg), media, {"jellyfin-1": ready_publisher("jellyfin_bridge")})
        for sid in ("plex-1", "jellyfin-1", "emby-1"):
            _vendor_read(reg, sid).assert_called_once_with(f"item-{sid}")
        stored = {r.origin for r in store.evidence_rows(store.get_file(media).id) if r.source is Source.SERVER_MARKERS}
        assert stored == {"plex-1", "jellyfin-1", "emby-1"}

    def test_server_markers_alone_never_publish(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        _serve_intro(reg, "plex-1", 127_000, 158_000)
        plex = ready_publisher()
        raw = {**INTRO_ONLY, "publish_when": "medium"}
        out, _ = _run(_ctx(store, reg, settings_raw=raw), media, {"plex-1": plex})
        plex.write.assert_not_called()
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value  # a second opinion with nothing to confirm

    def test_after_a_file_change_only_servers_we_never_published_to_are_read_again(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge")
        jf.capability.return_value = CapabilityReport(Capability.NEEDS_PLUGIN, "Install the plugin")
        for sid in ("plex-1", "jellyfin-1"):
            _serve_intro(reg, sid, 127_000, 158_000)
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY)
        _run(ctx, media, {"plex-1": plex, "jellyfin-1": jf})
        assert [_vendor_read(reg, sid).call_count for sid in ("plex-1", "jellyfin-1")] == [1, 1]
        os.utime(media, ns=(8, 8))
        _run(ctx, media, {"plex-1": plex, "jellyfin-1": jf})
        assert [_vendor_read(reg, sid).call_count for sid in ("plex-1", "jellyfin-1")] == [1, 2]

    def test_force_reads_again_servers_we_never_published_to(self, store, media):
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["jellyfin-1"].markers["enabled"] = False
        _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        # once per server; the stored answers are reused
        assert [_vendor_read(reg, sid).call_count for sid in ("plex-1", "jellyfin-1")] == [1, 1]
        _run(_ctx(store, reg, force=True), media, {"plex-1": ready_publisher()})
        assert [_vendor_read(reg, sid).call_count for sid in ("plex-1", "jellyfin-1")] == [2, 2]

    def test_server_markers_after_an_empty_publish_can_be_read(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        st = os.stat(media)
        rec = store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        store.set_publish_state(rec.id, "plex-1", item_id="item-plex-1", markers=[], status="written")
        _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        reg.get("plex-1").get_markers.assert_called_once_with("item-plex-1")

    @pytest.mark.parametrize(
        "setup",
        [
            lambda server: setattr(server.get_markers, "return_value", None),
            lambda server: setattr(server.get_markers, "side_effect", RuntimeError("HTTP 500")),
        ],
        ids=["unreadable", "exception"],
    )
    def test_unreadable_server_markers_are_retried_next_run(self, store, media, setup):
        reg = _registry(media, ServerType.PLEX)
        server = reg.get("plex-1")
        setup(server)
        out, _ = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        server.get_markers.side_effect, server.get_markers.return_value = None, []
        _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        assert server.get_markers.call_count == 2

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
            _serve_intro(reg, sid, 126_771, 157_068)
        reg.get("jellyfin-1").get_bridge_markers.return_value = [
            {"type": "Intro", "startTicks": _ticks(126_771), "endTicks": _ticks(157_068)}
        ]
        st = os.stat(version_b)
        rec_b = store.upsert_file(
            FileIdentity(version_b, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        for sid in configs:
            store.set_publish_state(rec_b.id, sid, item_id="shared-item", markers=[INTRO_CH], status="written")
            store.set_item_publish_state(sid, "shared-item", [INTRO_CH], "written")
        pubs = {
            "plex-1": ready_publisher(),
            "jellyfin-1": ready_publisher("jellyfin_bridge", ("intro", "credits", "recap")),
        }
        out, _ = _run(
            _ctx(store, reg, settings_raw={"detect": {"recap": True}}), version_a, pubs, probe=_probe(CHAPTERS_BOTH)
        )
        reg.get("plex-1").get_markers.assert_not_called()
        reg.get("emby-1").get_chapter_markers.assert_not_called()
        reg.get("jellyfin-1").get_media_segments.assert_called_once_with("shared-item")
        reg.get("jellyfin-1").get_bridge_markers.assert_called_once_with("shared-item")  # ours are left out
        stored = [r for r in store.evidence_rows(store.get_file(version_a).id) if r.source is Source.SERVER_MARKERS]
        assert [(r.origin, r.type) for r in stored] == [("jellyfin-1", None)]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    @pytest.mark.parametrize(
        ("serves_intro", "later", "published_first", "read_again"),
        [
            (False, timedelta(hours=12), False, False),
            (False, timedelta(days=1), False, False),
            (False, timedelta(days=1, seconds=1), False, True),
            (True, timedelta(days=30), False, False),
            (False, timedelta(days=2), True, False),
        ],
        ids=["empty-12h", "empty-exactly-a-day", "empty-after-a-day", "markers-30d", "empty-but-we-published"],
    )
    def test_empty_server_answers_are_read_again_after_a_day(
        self, tmp_path, media, serves_intro, later, published_first, read_again
    ):
        # Plex may detect the intro overnight after the webhook job read nothing.
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX)
        if serves_intro:
            _serve_intro(reg, "plex-1", 127_000, 158_000)
        raw = {"detect": {"recap": True}} if published_first else INTRO_ONLY
        probe = _probe(CHAPTERS_BOTH if published_first else ())
        _run(
            _ctx(store, reg, settings_raw=raw, now=lambda: clock["t"]),
            media,
            {"plex-1": ready_publisher()},
            probe=probe,
        )
        clock["t"] += later
        _run(_ctx(store, reg, settings_raw=raw, now=lambda: clock["t"]), media, {"plex-1": ready_publisher()})
        server = reg.get("plex-1")
        assert server.get_markers.call_count == (2 if read_again else 1)
        server.get_markers.assert_called_with("item-plex-1")
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
        server = reg.get("plex-1")
        server.resolve_remote_path_to_item_id.return_value = "42"

        def plex_markers(item_id):
            st = os.stat(version_b)  # version B's check commits to the same Plex item while A's HTTP read is out
            rec_b = store.upsert_file(
                FileIdentity(version_b, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=True
            )
            store.set_publish_state(rec_b.id, "plex-1", item_id="42", markers=[CREDITS_CH], status="written")
            store.set_item_publish_state("plex-1", "42", [CREDITS_CH], "written")
            return [{"type": "credits", "start_ms": 1_295_000, "end_ms": DUR, "final": True}]

        server.get_markers.side_effect = plex_markers
        _run(_ctx(store, reg), version_a, {"plex-1": ready_publisher()})
        server.get_markers.assert_called_once_with("42")
        assert store.evidence_fetched_at(store.get_file(version_a).id, Source.SERVER_MARKERS, "plex-1") is None

    def test_server_without_the_item_is_not_read(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = None
        _run(_ctx(store, reg), media, {"plex-1": ready_publisher()})
        reg.get("plex-1").get_markers.assert_not_called()

    def test_jellyfin_is_not_read_again_after_we_published_there(self, store, media):
        reg = _registry(media, ServerType.JELLYFIN)
        _serve_intro(reg, "jellyfin-1", 127_000, 158_000)
        jf = ready_publisher("jellyfin_bridge")
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, {"jellyfin-1": jf})
        assert jf.write.call_count == 1
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY, force=True)
        out, _ = _run(ctx, media, {"jellyfin-1": jf})
        reg.get("jellyfin-1").get_media_segments.assert_called_once_with("item-jellyfin-1")
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value


# Audit B S10, the owner's Rick and Morty S01 Blu-rays: the markers Plex serves (its own detection, prod), IntroDB's
# outro and SkipDB's duration-matched outro (recorded answers), and the credits Plex must be left with. Each file's
# post-credits scene starts where Plex's non-final credits end.
RM_S01 = {
    "S01E06": (
        1_288_928,
        [
            {"type": "credits", "start_ms": 1_192_035, "end_ms": 1_218_035, "final": False},
            {"type": "credits", "start_ms": 1_264_035, "end_ms": 1_288_928, "final": True},
        ],
        (1_191_000, 1_288_000),
        (1_282_000, 1_289_000),
        (1_191_000, 1_218_035),
    ),
    "S01E07": (
        1_321_570,
        [{"type": "credits", "start_ms": 1_239_413, "end_ms": 1_267_413, "final": False}],
        (1_239_000, 1_321_000),
        (1_314_000, 1_321_000),
        (1_239_000, 1_267_413),
    ),
    "S01E08": (
        1_335_752,
        [{"type": "credits", "start_ms": 1_250_176, "end_ms": 1_286_176, "final": False}],
        (1_249_000, 1_335_000),
        (1_329_000, 1_336_000),
        (1_249_000, 1_286_176),
    ),
}
CREDITS_DEFAULTS = {"detect": {"intro": False, "credits": True}}
S03E05_BLURAY_MS = 1_444_574
IDB_S03E05_INTRO = LookupResult("ok", (Candidate(T.INTRO, 24_046, 114_105, Source.INTRODB),))
INTRO_DEFAULTS = {"detect": {"intro": True, "credits": False}}


class TestServerMarkersFromVendors:
    """Markers already on servers, read through the vendor clients (no reader patched), all the way to decide()."""

    @pytest.mark.parametrize("episode", list(RM_S01))
    def test_plex_credits_shorten_crowd_credits_that_would_skip_the_post_credits_scene(self, store, media, episode):
        duration, plex_rows, outro, skip_outro, published = RM_S01[episode]
        reg = _registry(media, ServerType.PLEX)
        plex_server = reg.get("plex-1")
        plex_server.get_markers.return_value = plex_rows
        plex_server.get_part_durations.return_value = [duration]
        clients = _clients(
            introdb=LookupResult("ok", (Candidate(T.CREDITS, *outro, Source.INTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.CREDITS, *skip_outro, Source.SKIPDB),)),
        )
        plex = ready_publisher()
        out, _ = _run(
            _ctx(store, reg, clients=clients, settings_raw=CREDITS_DEFAULTS),
            media,
            {"plex-1": plex},
            probe=_probe(duration=duration),
        )
        assert plex.write.call_args.args == (
            "item-plex-1",
            [Marker(T.CREDITS, *published, ("introdb", "server_markers"))],
        )
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        stored = [c for c in store.get_evidence(store.get_file(media).id) if c.source is Source.SERVER_MARKERS]
        # Plex's final credits run to the end of the file.
        assert [(c.start_ms, c.end_ms) for c in stored] == [
            (r["start_ms"], None if r["final"] else r["end_ms"]) for r in plex_rows
        ]
        plex_server.get_markers.assert_called_once_with("item-plex-1")
        plex_server.get_part_durations.assert_called_once_with("item-plex-1")

    @pytest.mark.parametrize(
        ("stype", "durations", "ours", "shortened"),
        [
            (ServerType.PLEX, [DUR], False, True),
            (ServerType.PLEX, [DUR, DUR - 60_000], False, False),  # another cut of the item: not evidence
            (ServerType.JELLYFIN, None, False, True),
            (ServerType.JELLYFIN, None, True, False),  # the segment is the one our Bridge plugin serves
        ],
        ids=["plex-own", "plex-other-cut", "jellyfin-own", "jellyfin-ours"],
    )
    def test_the_servers_own_credits_shorten_an_early_end_credits_chapter(
        self, store, media, stype, durations, ours, shortened
    ):
        # Lab scale run, Avatar (2009) / Innerspace (1987): "End Credits" chapters start on the last story shots, and
        # the server's own credits start later, on the roll.
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        server = reg.get(sid)
        if stype is ServerType.PLEX:
            server.get_markers.return_value = [{"type": "credits", "start_ms": 1_255_500, "end_ms": DUR, "final": True}]
            server.get_part_durations.return_value = durations
        else:
            server.get_media_segments.return_value = [
                {"Type": "Outro", "StartTicks": _ticks(1_255_500), "EndTicks": _ticks(DUR)}
            ]
            if ours:
                server.get_bridge_markers.return_value = [
                    {"type": "Outro", "startTicks": _ticks(1_255_500), "endTicks": _ticks(DUR)}
                ]
        pub = ready_publisher() if stype is ServerType.PLEX else ready_publisher("jellyfin_bridge")
        chapters = (Chapter(0, 1_239_000, "Chapter 1"), Chapter(1_239_000, None, "End Credits"))
        _run(_ctx(store, reg, settings_raw=CREDITS_DEFAULTS), media, {sid: pub}, probe=_probe(chapters))
        decision = store.get_decisions(store.get_file(media).id)[T.CREDITS]
        if shortened:
            expected = Marker(T.CREDITS, 1_255_500, DUR, ("chapters", "server_markers"))
            assert decision.reason == f"chapters; start shortened to the server's own marker ({sid})"
        else:
            expected = Marker(T.CREDITS, 1_239_000, DUR, ("chapters",))
            assert decision.reason == "chapters"
        assert pub.write.call_args.args == (f"item-{sid}", [expected])

    @pytest.mark.parametrize(
        ("setup", "marker"),
        [
            # Plex's own credits start 16.5 s after the chapter: the start moves
            (
                "plex-shortens",
                Marker(T.CREDITS, 1_255_500, DUR, ("chapters", "server_markers")),
            ),
            # Plex's own credits and an importer plugin's copy on Jellyfin both agree with the chapter: two groups give
            # the chapter an earlier end, still credited to chapters and server markers only
            (
                "plex-and-imported-copy",
                Marker(T.CREDITS, 1_239_000, 1_290_000, ("chapters", "server_markers", "server_markers_imported")),
            ),
        ],
    )
    def test_chapters_backed_only_by_server_markers_keep_the_search_open(self, tmp_path, media, setup, marker):
        # Server markers never decide alone, so chapters + server markers still count as chapters alone: the free
        # sources are asked again when their "no data" answer is due, TheIntroDB isn't spent on it at Low.
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        if setup == "plex-shortens":
            reg = _registry(media, ServerType.PLEX)
            plex_rows = [{"type": "credits", "start_ms": 1_255_500, "end_ms": DUR, "final": True}]
        else:
            reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
            reg.configs_by_id["jellyfin-1"].markers["enabled"] = False
            jellyfin = reg.get("jellyfin-1")
            jellyfin.get_plugin_names.return_value = ["IntroDB"]
            jellyfin.get_media_segments.return_value = [
                {"Type": "Outro", "StartTicks": _ticks(1_240_000), "EndTicks": _ticks(1_290_000)}
            ]
            plex_rows = [{"type": "credits", "start_ms": 1_241_000, "end_ms": 1_300_000, "final": False}]
        reg.get("plex-1").get_markers.return_value = plex_rows
        reg.get("plex-1").get_part_durations.return_value = [DUR]
        clients = _clients()
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": False, "credits": True}}
        chapters = (Chapter(0, 1_239_000, "Chapter 1"), Chapter(1_239_000, None, "End Credits"))
        for _ in range(2):
            ctx = _ctx(store, reg, clients=clients, settings_raw=raw, now=lambda: clock["t"])
            ctx.priority = lambda: 3
            _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(chapters))
            assert store.get_markers(store.get_file(media).id)[T.CREDITS] == marker
            clock["t"] += pipeline.NO_DATA_RETRY + timedelta(seconds=1)
        assert (len(clients["introdb"].calls), len(clients["skipdb"].calls)) == (2, 2)
        assert clients["theintrodb"].calls == []
        store.close()

    @pytest.mark.parametrize(
        ("recheck", "later", "first_answer", "read_again"),
        [
            (False, timedelta(days=2), "empty", False),
            (True, timedelta(days=2), "empty", True),
            (True, timedelta(hours=12), "empty", False),
            (True, timedelta(days=1), "empty", False),
            (True, timedelta(days=2), "unusable", True),
            (True, timedelta(hours=12), "unusable", False),
            (True, timedelta(days=2), "has-markers", False),
        ],
        ids=[
            "normal-job", "check-servers", "check-servers-12h", "check-servers-exactly-a-day", "check-servers-unusable",
            "check-servers-unusable-12h", "check-servers-markers-stored",
        ],
    )  # fmt: skip
    def test_check_servers_asks_a_decided_files_server_again_once_its_empty_answer_is_a_day_old(
        self, tmp_path, media, recheck, later, first_answer, read_again
    ):
        # A webhook import is checked before Plex's own credits detection runs. Check servers reads Plex again a day
        # later, and Plex's credits then shorten the decided start (rule 7); a normal run never asks again.
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["plex-1"].markers["enabled"] = False  # Plex only lends evidence
        plex_server = reg.get("plex-1")
        plex_server.get_part_durations.return_value = [DUR]
        plex_credits = [{"type": "credits", "start_ms": 1_255_500, "end_ms": DUR, "final": True}]
        plex_server.get_markers.return_value = {"empty": [], "unusable": None, "has-markers": plex_credits}[
            first_answer
        ]
        jellyfin = ready_publisher("jellyfin_bridge")
        clients = _clients(
            introdb=LookupResult("ok", (Candidate(T.CREDITS, 1_239_000, None, Source.INTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.CREDITS, 1_241_000, None, Source.SKIPDB),)),
        )
        ctx = _ctx(store, reg, clients=clients, settings_raw=CREDITS_DEFAULTS, now=lambda: clock["t"])
        _run(ctx, media, {"jellyfin-1": jellyfin})
        first = store.get_markers(store.get_file(media).id)[T.CREDITS]
        clock["t"] += later
        plex_server.get_markers.return_value = plex_credits  # Plex detected its credits since
        ctx = _ctx(store, reg, clients=clients, settings_raw=CREDITS_DEFAULTS, now=lambda: clock["t"])
        ctx.recheck_empty_server_markers = recheck
        _run(ctx, media, {"jellyfin-1": jellyfin})
        assert plex_server.get_markers.call_count == (2 if read_again else 1)
        credits = store.get_markers(store.get_file(media).id)[T.CREDITS]
        if read_again:
            assert first.start_ms < 1_255_500 and credits.start_ms == 1_255_500
            assert "server_markers" in credits.decided_by
            assert jellyfin.write.call_args.args[1] == [credits]  # published through the pipeline
        else:
            assert credits == first
        store.close()

    @pytest.mark.parametrize("recheck", [False, True], ids=["normal-job", "check-servers"])
    @pytest.mark.parametrize(("first_answer", "read_again"), [("unusable", True), ("empty", False)])
    def test_with_a_type_still_undecided_check_servers_reads_a_server_like_a_normal_job(
        self, tmp_path, media, recheck, first_answer, read_again
    ):
        # No source knows the credits, so they stay undecided. An unusable answer is read again on every run; an
        # empty one only once it is a day old, whatever the job.
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["plex-1"].markers["enabled"] = False  # Plex only lends evidence
        plex_server = reg.get("plex-1")
        plex_server.get_part_durations.return_value = [DUR]
        plex_server.get_markers.return_value = {"empty": [], "unusable": None}[first_answer]
        jellyfin = ready_publisher("jellyfin_bridge")
        ctx = _ctx(store, reg, clients=_clients(), settings_raw=CREDITS_DEFAULTS, now=lambda: clock["t"])
        _run(ctx, media, {"jellyfin-1": jellyfin})
        assert store.get_markers(store.get_file(media).id).get(T.CREDITS) is None
        clock["t"] += timedelta(hours=12)
        ctx = _ctx(store, reg, clients=_clients(), settings_raw=CREDITS_DEFAULTS, now=lambda: clock["t"])
        ctx.recheck_empty_server_markers = recheck
        _run(ctx, media, {"jellyfin-1": jellyfin})
        assert plex_server.get_markers.call_count == (2 if read_again else 1)
        # Only Check servers counts a re-read that failed; a normal job's reads never move its backoff.
        rereads = store._conn.execute(
            "SELECT rereads FROM server_marker_rereads WHERE file_id=? AND server_id='plex-1'",
            (store.get_file(media).id,),
        ).fetchone()
        assert (rereads[0] if rereads else None) == (1 if recheck and first_answer == "unusable" else None)
        store.close()

    @pytest.mark.parametrize(
        ("recheck", "read_again"), [(False, True), (True, False)], ids=["normal-job", "check-servers"]
    )
    def test_with_a_type_still_undecided_an_empty_answer_read_again_once_waits_for_the_backoff_on_check_servers(
        self, tmp_path, media, recheck, read_again
    ):
        # One re-read already counted, so Check servers' next step is 2 days; the answer is 36 h old. A normal job's
        # empty-answer retry (1 day) reads it.
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["plex-1"].markers["enabled"] = False
        plex_server = reg.get("plex-1")
        plex_server.get_part_durations.return_value = [DUR]
        plex_server.get_markers.return_value = []
        jellyfin = ready_publisher("jellyfin_bridge")
        _run(_ctx(store, reg, clients=_clients(), settings_raw=CREDITS_DEFAULTS, now=lambda: clock["t"]), media,
             {"jellyfin-1": jellyfin})  # fmt: skip
        rec = store.get_file(media)
        store.replace_evidence(
            rec.id, Source.SERVER_MARKERS, [], origin="plex-1", version=pipeline.READER_VERSION
        )  # read again
        clock["t"] += timedelta(hours=36)
        ctx = _ctx(store, reg, clients=_clients(), settings_raw=CREDITS_DEFAULTS, now=lambda: clock["t"])
        ctx.recheck_empty_server_markers = recheck
        _run(ctx, media, {"jellyfin-1": jellyfin})
        assert plex_server.get_markers.call_count == (2 if read_again else 1)
        store.close()

    @pytest.mark.parametrize("cause", ["read-always-fails", "other-cut-on-the-item"])
    def test_check_servers_stops_asking_a_server_whose_read_never_works_after_five_re_reads(
        self, tmp_path, media, cause
    ):
        # Sixty daily Check servers runs: each takes the file only when its backoff is due, and the pipeline's failed
        # re-read counts. Then Plex serves usable markers and a forced run reads them: the count starts again.
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["plex-1"].markers["enabled"] = False  # Plex only lends evidence
        plex_server = reg.get("plex-1")
        plex_credits = [{"type": "credits", "start_ms": 1_255_500, "end_ms": DUR, "final": True}]
        if cause == "read-always-fails":
            plex_server.get_part_durations.return_value = [DUR]
            plex_server.get_markers.return_value = None
        else:
            plex_server.get_part_durations.return_value = [DUR, DUR - 60_000]
            plex_server.get_markers.return_value = plex_credits
        jellyfin = ready_publisher("jellyfin_bridge")
        clients = _clients(
            introdb=LookupResult("ok", (Candidate(T.CREDITS, 1_239_000, None, Source.INTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.CREDITS, 1_241_000, None, Source.SKIPDB),)),
        )

        def run(**kwargs):
            ctx = _ctx(store, reg, clients=clients, settings_raw=CREDITS_DEFAULTS, now=lambda: clock["t"], **kwargs)
            ctx.recheck_empty_server_markers = not kwargs
            _run(ctx, media, {"jellyfin-1": jellyfin})

        run(force=True)  # the webhook run: credits decided, Plex's answer unusable
        rec = store.get_file(media)
        [answer] = [r for r in store.evidence_rows(rec.id) if r.origin == "plex-1"]
        assert answer.detail == pipeline.UNUSABLE_SERVER_MARKERS_DETAIL
        reads = plex_server.get_markers.call_count
        passes = []
        for day in range(1, 61):
            clock["t"] = datetime(2026, 9, 13, tzinfo=UTC) + timedelta(days=day, minutes=day)
            if store.take_server_rechecks(
                ["plex-1", "jellyfin-1"], now=clock["t"], after=pipeline.RECHECK_AFTER, limit=10
            ):
                passes.append(day)
                run()
        assert passes == [1, 3, 7, 15, 31]
        assert plex_server.get_markers.call_count == reads + 5
        assert [r for r in store.evidence_rows(rec.id) if r.origin == "plex-1"] == [answer]  # the answer is kept
        plex_server.get_part_durations.return_value = [DUR]
        plex_server.get_markers.return_value = plex_credits
        run(force=True)
        assert (
            store._conn.execute(
                "SELECT COUNT(*) FROM server_marker_rereads WHERE file_id=? AND server_id='plex-1' AND rereads > 0",
                (rec.id,),
            ).fetchone()[0]
            == 0
        )
        store.close()

    def test_a_pipeline_context_doesnt_ask_servers_again_unless_the_job_checks_servers(self):
        assert (
            PipelineContext(
                registry=None, config=None, settings=None, store=None, priority=lambda: 3, ffprobe="ffprobe"
            ).recheck_empty_server_markers
            is False
        )

    @pytest.mark.parametrize("case", ["plex-cant-publish", "other-cut-item", "published-server"])
    def test_with_everything_decided_a_server_is_read_only_the_first_time(self, tmp_path, media, case):
        # Credits decided by IntroDB + SkipDB before the servers come up. Runs two days apart, so the empty-answer
        # retry (1 day) would be due every time: a decided file reads each server once, whatever it answered.
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        types = (ServerType.PLEX,) if case != "other-cut-item" else (ServerType.PLEX, ServerType.JELLYFIN)
        reg = _registry(media, *types)
        plex_server = reg.get("plex-1")
        plex = ready_publisher()
        publishers = {"plex-1": plex}
        if case == "plex-cant-publish":
            plex.capability.return_value = CapabilityReport(Capability.NEEDS_LOCAL_DB, "Plex's database isn't local")
        elif case == "other-cut-item":
            # Plex only lends evidence here; its item has a second version 60 s shorter, so its markers aren't used.
            reg.configs_by_id["plex-1"].markers["enabled"] = False
            plex_server.get_markers.return_value = [
                {"type": "credits", "start_ms": 1_255_500, "end_ms": DUR, "final": True}
            ]
            plex_server.get_part_durations.return_value = [DUR, DUR - 60_000]
            publishers = {"jellyfin-1": ready_publisher("jellyfin_bridge")}
        clients = _clients(
            introdb=LookupResult("ok", (Candidate(T.CREDITS, 1_239_000, None, Source.INTRODB),)),
            skipdb=LookupResult("ok", (Candidate(T.CREDITS, 1_241_000, None, Source.SKIPDB),)),
        )
        reads = []
        for _ in range(4):
            before = plex_server.get_markers.call_count
            ctx = _ctx(store, reg, clients=clients, settings_raw=CREDITS_DEFAULTS, now=lambda: clock["t"])
            _run(ctx, media, publishers)
            reads.append(plex_server.get_markers.call_count - before)
            decision = store.get_decisions(store.get_file(media).id)[T.CREDITS]
            assert decision.status is DecisionStatus.DECIDED
            clock["t"] += timedelta(days=2)
        assert reads == [1, 0, 0, 0]
        if case == "published-server":
            plex.write.assert_called_once()
        else:
            plex.write.assert_not_called()
        store.close()

    @pytest.mark.parametrize(
        ("stype", "durations", "expected"),
        [
            (ServerType.PLEX, [S03E05_BLURAY_MS], DecisionStatus.DECIDED),
            (ServerType.PLEX, [S03E05_BLURAY_MS, S03E05_BLURAY_MS - 1_500], DecisionStatus.DECIDED),
            (ServerType.PLEX, [S03E05_BLURAY_MS, S03E05_BLURAY_MS - 60_000], DecisionStatus.NEEDS_REVIEW),
            (ServerType.PLEX, None, DecisionStatus.NEEDS_REVIEW),
            # Each Emby version is its own item with its own markers (spec §3.3): the versions Emby lists don't matter.
            (ServerType.EMBY, [S03E05_BLURAY_MS], DecisionStatus.DECIDED),
            (ServerType.EMBY, [S03E05_BLURAY_MS - 60_000, S03E05_BLURAY_MS], DecisionStatus.DECIDED),
            (ServerType.EMBY, None, DecisionStatus.DECIDED),
        ],
        ids=[
            "plex-one-version",
            "plex-same-cut",
            "plex-other-cut",
            "plex-unreadable",
            "emby-one",
            "emby-other-cut-listed",
            "emby-versions-unreadable",
        ],
    )
    def test_item_wide_markers_are_evidence_only_when_every_version_is_this_cut(
        self, store, media, stype, durations, expected
    ):
        # Audit B S13: Plex's intro was detected on the WEB version; this file is the Blu-ray with a longer cold open.
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        server = reg.get(sid)
        if stype is ServerType.PLEX:
            server.get_markers.return_value = [{"type": "intro", "start_ms": 24_500, "end_ms": 113_900, "final": False}]
            server.get_part_durations.return_value = durations
            durations_call = server.get_part_durations
        else:
            server.get_chapter_markers.return_value = [
                {"marker_type": "IntroStart", "start_ms": 24_500, "name": ""},
                {"marker_type": "IntroEnd", "start_ms": 113_900, "name": ""},
            ]
            server.get_media_source_durations.return_value = durations
            durations_call = server.get_media_source_durations
        clients = _clients(introdb=IDB_S03E05_INTRO)
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_DEFAULTS)
        _run(ctx, media, {sid: ready_publisher()}, probe=_probe(duration=S03E05_BLURAY_MS))
        rec = store.get_file(media)
        decision = store.get_decisions(rec.id)[T.INTRO]
        assert decision.status is expected
        stored = [c for c in store.get_evidence(rec.id) if c.source is Source.SERVER_MARKERS]
        if expected is DecisionStatus.DECIDED:
            assert store.get_markers(rec.id)[T.INTRO] == Marker(T.INTRO, 24_500, 114_105, ("introdb", "server_markers"))
            assert stored == [Candidate(T.INTRO, 24_500, 113_900, Source.SERVER_MARKERS, origin=sid)]
        else:
            assert stored == []  # nothing stored, so the next run reads the server again
        if stype is ServerType.PLEX:
            durations_call.assert_called_once_with(f"item-{sid}")
        else:
            durations_call.assert_not_called()

    def test_an_emby_item_listing_another_cut_is_read_once_not_on_every_run(self, store, media):
        # Under user-id auth Emby lists every version with the item. Its markers used to be stored as "unusable" and
        # read again on every run that still needed evidence; now they are the item's own answer, read once.
        reg = _registry(media, ServerType.EMBY)
        server = reg.get("emby-1")
        server.get_chapter_markers.return_value = [
            {"marker_type": "IntroStart", "start_ms": 24_500, "name": ""},
            {"marker_type": "IntroEnd", "start_ms": 113_900, "name": ""},
        ]
        server.get_media_source_durations.return_value = [S03E05_BLURAY_MS - 60_000, S03E05_BLURAY_MS]
        ctx = _ctx(store, reg, settings_raw=INTRO_DEFAULTS)  # nothing else answers: the intro stays undecided
        for _ in range(3):
            pubs = {"emby-1": ready_publisher("emby_bridge")}
            out, _ = _run(ctx, media, pubs, probe=_probe(duration=S03E05_BLURAY_MS))
            assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        assert server.get_chapter_markers.call_count == 1
        rec = store.get_file(media)
        assert [c for c in store.get_evidence(rec.id) if c.source is Source.SERVER_MARKERS] == [
            Candidate(T.INTRO, 24_500, 113_900, Source.SERVER_MARKERS, origin="emby-1")
        ]

    @pytest.mark.parametrize(
        ("stype", "plugins", "source", "expected"),
        [
            (ServerType.JELLYFIN, ["Media Preview Bridge", "TheIntroDB"], Source.SERVER_MARKERS_IMPORTED, "review"),
            (ServerType.JELLYFIN, ["IntroDB"], Source.SERVER_MARKERS_IMPORTED, "review"),
            (ServerType.JELLYFIN, ["Intro Skipper"], Source.SERVER_MARKERS, "decided"),
            (ServerType.JELLYFIN, [], Source.SERVER_MARKERS, "decided"),
            (ServerType.JELLYFIN, None, None, "review"),
            (ServerType.JELLYFIN, ConnectionError("plugin list timed out"), None, "review"),
            (ServerType.EMBY, ["TheIntroDB"], Source.SERVER_MARKERS_IMPORTED, "review"),
            (ServerType.EMBY, ["Trakt"], Source.SERVER_MARKERS, "decided"),
            (ServerType.EMBY, [], Source.SERVER_MARKERS, "decided"),
            (ServerType.EMBY, None, None, "review"),
            (ServerType.EMBY, ConnectionError("plugin list timed out"), None, "review"),
        ],
        ids=[
            "jf-theintrodb",
            "jf-introdb",
            "jf-intro-skipper",
            "jf-none",
            "jf-unreadable",
            "jf-plugin-list-raises",
            "emby-theintrodb",
            "emby-other",
            "emby-none",
            "emby-unreadable",
            "emby-plugin-list-raises",
        ],
    )
    def test_markers_an_importer_plugin_wrote_count_with_the_crowd_source(
        self, store, media, stype, plugins, source, expected
    ):
        # Audit B S3: an IntroDB answer and its copy on a server with an importer plugin are one source, not two.
        reg = _registry(media, ServerType.PLEX, stype)
        reg.configs_by_id[f"{stype.value}-1"].markers["enabled"] = False
        sid = f"{stype.value}-1"
        server = reg.get(sid)
        if stype is ServerType.JELLYFIN:
            server.get_media_segments.return_value = [
                {"Type": "Intro", "StartTicks": _ticks(24_046), "EndTicks": _ticks(114_105)}
            ]
        else:
            server.get_chapter_markers.return_value = [
                {"marker_type": "IntroStart", "start_ms": 24_046, "name": ""},
                {"marker_type": "IntroEnd", "start_ms": 114_105, "name": ""},
            ]
        if isinstance(plugins, Exception):
            server.get_plugin_names.side_effect = plugins
        else:
            server.get_plugin_names.return_value = plugins
        plex = ready_publisher()
        clients = _clients(introdb=IDB_S03E05_INTRO)
        out, _ = _run(
            _ctx(store, reg, clients=clients, settings_raw=INTRO_DEFAULTS),
            media,
            {"plex-1": plex},
            probe=_probe(duration=S03E05_BLURAY_MS),
        )
        rec = store.get_file(media)
        rows = [r for r in store.evidence_rows(rec.id) if r.origin == sid]
        if source is None:
            # The plugin list couldn't be read: the markers don't count, and the server is read again like an empty one.
            assert [(r.source, r.type, r.detail) for r in rows] == [
                (Source.SERVER_MARKERS, None, "Couldn't read this server's plugins, so its markers aren't used")
            ]
        else:
            imported = source is Source.SERVER_MARKERS_IMPORTED
            detail = (
                f"Markers on this server look imported from {plugins[-1]}; not a second opinion for that database"
                if imported
                else ""
            )
            assert [(r.source, r.type, r.start_ms, r.end_ms, r.detail) for r in rows] == [
                (source, T.INTRO, 24_046, 114_105, detail)
            ]
            assert store.evidence_version(rec.id, source, sid) == pipeline.READER_VERSION
        if expected == "decided":
            assert plex.write.call_args.args[1] == [Marker(T.INTRO, 24_046, 114_105, ("introdb", "server_markers"))]
        else:
            plex.write.assert_not_called()
            assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        server.get_plugin_names.assert_called_once_with()
        reg.get("plex-1").get_plugin_names.assert_not_called()  # Plex detects its own markers

    @pytest.mark.parametrize(
        ("plugins", "published"),
        [
            (["SkipDB"], None),
            (["TheIntroDB"], Marker(T.INTRO, 24_046, 114_105, ("skipdb", "server_markers_imported"))),
            (["AniSkip"], Marker(T.INTRO, 24_046, 114_105, ("skipdb", "server_markers_imported"))),
            # importers of two databases: which one wrote the markers can't be told, so they count as IntroDB's copy
            (["SkipDB", "TheIntroDB"], Marker(T.INTRO, 24_046, 114_105, ("skipdb", "server_markers_imported"))),
        ],
        ids=["skipdb-importer", "theintrodb-importer", "aniskip-importer", "two-databases"],
    )
    def test_a_skipdb_importers_copy_is_skipdb_again(self, store, media, plugins, published):
        # Ruling 2026-09-16 (rule 8): an imported copy belongs to the group of the database it was imported from.
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["jellyfin-1"].markers["enabled"] = False
        server = reg.get("jellyfin-1")
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": _ticks(24_046), "EndTicks": _ticks(114_105)}
        ]
        server.get_plugin_names.return_value = plugins
        plex = ready_publisher()
        skipdb = LookupResult("ok", (Candidate(T.INTRO, 24_046, 114_105, Source.SKIPDB),))
        out, _ = _run(
            _ctx(store, reg, clients=_clients(skipdb=skipdb), settings_raw=INTRO_DEFAULTS),
            media,
            {"plex-1": plex},
            probe=_probe(duration=S03E05_BLURAY_MS),
        )
        rec = store.get_file(media)
        copies = [c for c in store.get_evidence(rec.id) if c.source is Source.SERVER_MARKERS_IMPORTED]
        assert [c.origin for c in copies] == ["jellyfin-1"]
        if published is None:
            plex.write.assert_not_called()
            assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
            assert store.get_decisions(rec.id)[T.INTRO].reason == "sources don't agree yet"
        else:
            assert plex.write.call_args.args[1] == [published]

    def test_a_row_naming_one_of_two_importers_is_read_again_and_its_database_becomes_unknown(self, store, media):
        # Reader version 1 stored only the first importer plugin: ["SkipDB", "TheIntroDB"] read as a SkipDB copy, which
        # let IntroDB and that copy agree. Version 2 reads the server again; both names make the database unknown, so
        # the copy counts with IntroDB/TheIntroDB again and IntroDB needs another source.
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["jellyfin-1"].markers["enabled"] = False
        server = reg.get("jellyfin-1")
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": _ticks(24_046), "EndTicks": _ticks(114_105)}
        ]
        server.get_plugin_names.return_value = ["SkipDB", "TheIntroDB"]
        st = os.stat(media)
        rec = store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns),
            duration_ms=S03E05_BLURAY_MS,
            season_key=None,
            is_movie=False,
        )
        old = Candidate(T.INTRO, 24_046, 114_105, Source.SERVER_MARKERS, origin="jellyfin-1")
        store.replace_evidence(
            rec.id,
            Source.SERVER_MARKERS_IMPORTED,
            [old],
            origin="jellyfin-1",
            detail="Markers on this server look imported from SkipDB; not used as a second opinion",
            version=1,
            also_replaces=(Source.SERVER_MARKERS,),
        )
        assert [c.copied_from for c in store.get_evidence(rec.id)] == ["skipdb"]
        ctx = _ctx(store, reg, clients=_clients(introdb=IDB_S03E05_INTRO), settings_raw=INTRO_DEFAULTS)
        assert pipeline.READER_VERSION == 2
        assert pipeline._server_markers_due(ctx, rec, "jellyfin-1", first_read_only=False)
        plex = ready_publisher()

        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(duration=S03E05_BLURAY_MS))

        server.get_media_segments.assert_called()
        rows = [r for r in store.evidence_rows(rec.id) if r.origin == "jellyfin-1"]
        assert [(r.source, r.detail) for r in rows] == [
            (
                Source.SERVER_MARKERS_IMPORTED,
                "Markers on this server look imported from SkipDB, TheIntroDB; not a second opinion for that database",
            )
        ]
        assert store.evidence_version(rec.id, Source.SERVER_MARKERS_IMPORTED, "jellyfin-1") == 2
        copies = [c for c in store.get_evidence(rec.id) if c.source is Source.SERVER_MARKERS_IMPORTED]
        assert [(c.start_ms, c.end_ms, c.copied_from) for c in copies] == [(24_046, 114_105, "")]
        plex.write.assert_not_called()
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        assert store.get_decisions(rec.id)[T.INTRO].reason == "sources don't agree yet"

    def test_markers_stored_before_the_plugin_check_are_dropped_when_the_plugins_cant_be_read(self, store, media):
        # A store from before this check holds the copy as independent server markers; they must not keep counting.
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["jellyfin-1"].markers["enabled"] = False
        server = reg.get("jellyfin-1")
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": _ticks(24_046), "EndTicks": _ticks(114_105)}
        ]
        server.get_plugin_names.return_value = None
        st = os.stat(media)
        rec = store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns),
            duration_ms=S03E05_BLURAY_MS,
            season_key=None,
            is_movie=False,
        )
        old = Candidate(T.INTRO, 24_046, 114_105, Source.SERVER_MARKERS, origin="jellyfin-1")
        store.replace_evidence(rec.id, Source.SERVER_MARKERS, [old], origin="jellyfin-1")  # no reader version yet
        plex = ready_publisher()
        clients = _clients(introdb=IDB_S03E05_INTRO)
        out, _ = _run(
            _ctx(store, reg, clients=clients, settings_raw=INTRO_DEFAULTS),
            media,
            {"plex-1": plex},
            probe=_probe(duration=S03E05_BLURAY_MS),
        )
        assert [c for c in store.get_evidence(rec.id) if c.origin == "jellyfin-1"] == []
        plex.write.assert_not_called()
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value

    @pytest.mark.parametrize("server_markers_on", [True, False], ids=["server-markers-on", "server-markers-off"])
    def test_an_imported_copy_still_confirms_a_source_outside_the_crowd(self, store, media, server_markers_on):
        # A copy of crowd data is still a second opinion for SkipDB (a different group); it follows the server-markers
        # switch like any marker already on a server.
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["jellyfin-1"].markers["enabled"] = False
        server = reg.get("jellyfin-1")
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": _ticks(24_046), "EndTicks": _ticks(114_105)}
        ]
        server.get_plugin_names.return_value = ["TheIntroDB"]
        clients = _clients(skipdb=LookupResult("ok", (Candidate(T.INTRO, 25_000, 113_000, Source.SKIPDB),)))
        order = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers")
        sources = [{"id": sid, "enabled": server_markers_on if sid == "server_markers" else True} for sid in order]
        raw = {"sources": sources, **INTRO_DEFAULTS}
        plex = ready_publisher()
        out, _ = _run(
            _ctx(store, reg, clients=clients, settings_raw=raw),
            media,
            {"plex-1": plex},
            probe=_probe(duration=S03E05_BLURAY_MS),
        )
        if server_markers_on:
            assert plex.write.call_args.args[1] == [
                Marker(T.INTRO, 25_000, 113_000, ("skipdb", "server_markers_imported"))
            ]
        else:
            plex.write.assert_not_called()
            assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value

    def test_a_server_with_no_markers_is_not_asked_for_its_plugins(self, store, media):
        reg = _registry(media, ServerType.JELLYFIN)
        _run(_ctx(store, reg), media, {"jellyfin-1": ready_publisher("jellyfin_bridge")})
        reg.get("jellyfin-1").get_media_segments.assert_called_once_with("item-jellyfin-1")
        reg.get("jellyfin-1").get_plugin_names.assert_not_called()

    def test_plugins_are_asked_once_per_server_per_job(self, store, media):
        other = os.path.join(os.path.dirname(media), "Rick and Morty (2013) - S01E02 - Lawnmower Dog.mkv")
        open(other, "wb").write(b"y" * 10)
        reg = _registry(media, ServerType.JELLYFIN)
        server = reg.get("jellyfin-1")
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": _ticks(24_046), "EndTicks": _ticks(114_105)}
        ]
        server.get_plugin_names.return_value = ["TheIntroDB"]
        pubs = {"jellyfin-1": ready_publisher("jellyfin_bridge")}
        ctx = _ctx(store, reg, settings_raw=INTRO_DEFAULTS)
        for path in (media, other):
            _run(ctx, path, pubs, probe=_probe(duration=S03E05_BLURAY_MS))
        assert server.get_plugin_names.call_count == 1
        third = os.path.join(os.path.dirname(media), "Rick and Morty (2013) - S01E03 - Anatomy Park.mkv")
        open(third, "wb").write(b"z" * 10)
        _run(_ctx(store, reg, settings_raw=INTRO_DEFAULTS), third, pubs, probe=_probe(duration=S03E05_BLURAY_MS))
        assert server.get_plugin_names.call_count == 2  # the next job asks again

    def test_plugins_are_fetched_once_when_many_checks_start_together(self, store, media):
        reg = _registry(media, ServerType.JELLYFIN)
        server = reg.get("jellyfin-1")
        ctx = _ctx(store, reg)
        started, release = threading.Barrier(4), threading.Event()

        def slow_plugins():
            release.wait(timeout=5)
            return ["TheIntroDB"]

        server.get_plugin_names.side_effect = slow_plugins
        owner = pipeline._Owning(server, reg.get_config("jellyfin-1"), ())
        answers = []

        def ask():
            started.wait(timeout=5)
            answers.append(pipeline._importer_plugin(ctx, owner))

        threads = [threading.Thread(target=ask) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.05)
        release.set()
        for t in threads:
            t.join(timeout=5)
        assert answers == [(True, "TheIntroDB")] * 4
        assert server.get_plugin_names.call_count == 1

    def test_an_importer_plugin_installed_later_replaces_the_earlier_answer(self, tmp_path, media):
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        reg = _registry(media, ServerType.JELLYFIN)
        server = reg.get("jellyfin-1")
        pubs = {"jellyfin-1": ready_publisher("jellyfin_bridge")}
        now = lambda: clock["t"]  # noqa: E731
        _run(_ctx(store, reg, settings_raw=INTRO_DEFAULTS, now=now), media, pubs)
        clock["t"] += timedelta(days=2)
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": _ticks(24_046), "EndTicks": _ticks(114_105)}
        ]
        server.get_plugin_names.return_value = ["TheIntroDB"]
        _run(_ctx(store, reg, settings_raw=INTRO_DEFAULTS, now=now), media, pubs)
        rows = [(r.source, r.type) for r in store.evidence_rows(store.get_file(media).id) if r.origin == "jellyfin-1"]
        assert rows == [(Source.SERVER_MARKERS_IMPORTED, T.INTRO)]
        store.close()


class TestRulesVersions:
    """Evidence made by older chapter rules, parsers or server readers is derived again on the next normal run."""

    MUSHOKU_S01E06 = (
        Chapter(0, 274_700, "Intro"),
        Chapter(274_700, 363_900, "OP"),
        Chapter(363_900, 1_300_000, "Part A"),
        Chapter(1_300_000, None, "ED"),
    )

    @pytest.mark.parametrize("stored_version", [None, pipeline.CHAPTER_RULES_VERSION - 1], ids=["unversioned", "older"])
    def test_chapters_from_older_rules_are_read_again(self, store, media, stored_version):
        # Audit B S12: scanned before the cold-open rule, the generic "Intro" chapter (the cold open) was kept.
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        raw = {"sources": [{"id": "chapters", "enabled": True}], "detect": {"intro": True, "credits": False}}
        probe = _probe(self.MUSHOKU_S01E06, duration=1_420_000)
        _run(_ctx(store, reg, settings_raw=raw), media, {"jellyfin-1": jf}, probe=probe)
        rec = store.get_file(media)
        old_rule = Candidate(T.INTRO, 0, 274_700, Source.CHAPTERS, origin="Intro")
        store.replace_evidence(rec.id, Source.CHAPTERS, [old_rule], version=stored_version)
        out, probe_mock = _run(_ctx(store, reg, settings_raw=raw), media, {"jellyfin-1": jf}, probe=probe)
        assert probe_mock.call_count == 1
        assert store.evidence_version(rec.id, Source.CHAPTERS) == pipeline.CHAPTER_RULES_VERSION
        assert store.get_markers(rec.id)[T.INTRO] == Marker(T.INTRO, 274_700, 363_900, ("chapters",))
        # With the current version stored nothing is probed again.
        _, probe_mock = _run(_ctx(store, reg, settings_raw=raw), media, {"jellyfin-1": jf}, probe=probe)
        assert probe_mock.call_count == 0

    @pytest.mark.parametrize("source", [Source.THEINTRODB, Source.INTRODB, Source.SKIPDB], ids=lambda s: s.value)
    def test_online_answers_from_an_older_parser_are_asked_again(self, store, media, source):
        reg = _registry(media, ServerType.PLEX)
        answer = LookupResult("ok", (Candidate(T.INTRO, 127_894, 156_824, source),))
        clients = _clients(**{source.value: answer})
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": True, "credits": False}}
        pubs = {"plex-1": ready_publisher()}
        _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, pubs)
        _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, pubs)
        assert len(clients[source.value].calls) == 1  # an ok answer with the current parser is never asked again
        rec = store.get_file(media)
        store.replace_evidence(rec.id, source, list(answer.candidates), version=0)
        _run(_ctx(store, reg, clients=clients, settings_raw=raw), media, pubs)
        assert len(clients[source.value].calls) == 2
        assert store.evidence_version(rec.id, source) == pipeline.PARSER_VERSIONS[source]

    @pytest.mark.parametrize("priority", [2, 3], ids=["normal", "low"])
    @pytest.mark.parametrize("stale", [Source.THEINTRODB, Source.SKIPDB], ids=lambda s: s.value)
    def test_an_older_answer_is_asked_again_even_when_it_helps_decide(self, store, media, stale, priority):
        # Stored answers that already agree would otherwise end the search before the stale one is reached.
        reg = _registry(media, ServerType.PLEX)
        clients = _clients(
            theintrodb=LookupResult("ok", (TIDB_INTRO,)),
            skipdb=LookupResult("ok", (Candidate(T.INTRO, 129_000, 157_800, Source.SKIPDB),)),
        )
        pubs = {"plex-1": ready_publisher()}
        _run(_ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY), media, pubs)
        rec = store.get_file(media)
        assert store.get_decisions(rec.id)[T.INTRO].status is DecisionStatus.DECIDED
        before = {sid: len(c.calls) for sid, c in clients.items()}
        store.replace_evidence(rec.id, stale, list(clients[stale.value].result.candidates), version=0)
        ctx = _ctx(store, reg, clients=clients, settings_raw=INTRO_ONLY)
        ctx.priority = lambda: priority
        _run(ctx, media, pubs)
        after = {sid: len(c.calls) - before[sid] for sid, c in clients.items()}
        assert after == {
            "theintrodb": int(stale is Source.THEINTRODB),
            "introdb": 0,
            "skipdb": int(stale is Source.SKIPDB),
        }
        assert store.evidence_version(rec.id, stale) == pipeline.PARSER_VERSIONS[stale]

    def test_server_markers_from_an_older_reader_are_read_again(self, store, media):
        reg = _registry(media, ServerType.JELLYFIN)
        server = reg.get("jellyfin-1")
        server.get_media_segments.return_value = [
            {"Type": "Intro", "StartTicks": _ticks(24_046), "EndTicks": _ticks(114_105)}
        ]
        server.get_bridge_markers.return_value = None  # can't tell ours apart: not evidence, nothing published yet
        pubs = {"jellyfin-1": ready_publisher("jellyfin_bridge")}
        pubs["jellyfin-1"].capability.return_value = CapabilityReport(Capability.NEEDS_PLUGIN, "Install the plugin")
        _run(_ctx(store, reg, settings_raw=INTRO_DEFAULTS), media, pubs)
        server.get_bridge_markers.return_value = []
        _run(_ctx(store, reg, settings_raw=INTRO_DEFAULTS), media, pubs)
        _run(_ctx(store, reg, settings_raw=INTRO_DEFAULTS), media, pubs)
        assert server.get_media_segments.call_count == 2  # stored the second time, then reused
        rec = store.get_file(media)
        stored = [c for c in store.get_evidence(rec.id) if c.origin == "jellyfin-1"]
        store.replace_evidence(rec.id, Source.SERVER_MARKERS, stored, origin="jellyfin-1", version=0)
        _run(_ctx(store, reg, settings_raw=INTRO_DEFAULTS), media, pubs)
        assert server.get_media_segments.call_count == 3
        assert store.evidence_version(rec.id, Source.SERVER_MARKERS, "jellyfin-1") == pipeline.READER_VERSION


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
        # A server still waiting or failed shows on the file; a skipped one leaves it published.
        expected_outcome = {
            ServerStatus.SKIPPED: FileOutcome.PUBLISHED,
            ServerStatus.WAITING: FileOutcome.WAITING,
            ServerStatus.FAILED: FileOutcome.FAILED,
        }
        assert out.outcome_key == expected_outcome[status].value
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
        # Only chapters are on, so only publishing could need the server's item.
        off = ("theintrodb", "introdb", "skipdb", "server_markers")
        raw = {"sources": [{"id": sid, "enabled": False} for sid in off]}
        out, _ = _run(_ctx(store, reg, settings_raw=raw), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
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
        assert kwargs == {
            "previous": None,
            "own_previous": None,
            "duration_ms": DUR,
            "canonical_path": media,
            "kept_types": frozenset(),
        }
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
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
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
        clock = {"t": datetime(2026, 9, 13, tzinfo=UTC)}
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
            reports.append(pipeline.cached_capability(ctx, cfg, plex))

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

    @pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.JELLYFIN, ServerType.EMBY])
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

        pub = ready_publisher(
            {ServerType.PLEX: "plex_db", ServerType.JELLYFIN: "jellyfin_bridge"}.get(stype, "emby_bridge")
        )
        ctx = _ctx(store, reg, live_config=live_config)
        out, _ = _run(ctx, media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        row = out.publisher_rows[0]
        assert (row["status"], row["message"]) == (status.value, message)
        assert pub.write.call_count == (1 if change == "unchanged" else 0)
        assert (sid in ctx._capabilities) is (change == "unchanged")

    @pytest.mark.parametrize(
        ("report", "cached"),
        [
            (CapabilityReport(Capability.DISABLED, "Intro & Credits is off for this server"), False),
            (CapabilityReport(Capability.NEEDS_CONFIRMATION, "Confirm the Plex database write to turn this on"), False),
            # Not from the saved settings: a server problem is still reused for the job's TTL.
            (CapabilityReport(Capability.UNREACHABLE, "Can't reach this Jellyfin server"), True),
        ],
        ids=["off", "confirmation-cleared", "unreachable"],
    )
    def test_an_answer_from_the_saved_settings_is_not_reused_for_the_next_file(self, store, media, report, cached):
        # Plex's capability reads the saved settings live: off for file 1, on again a minute later for file 2.
        other = _second_episode(media)
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.capability.side_effect = [report, CapabilityReport(Capability.READY, "ok", {"plex_pass": True})]
        ctx = _ctx(store, reg)  # the 300 s job cache
        first, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        second, _ = _run(ctx, other, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))

        assert (first.publisher_rows[0]["status"], first.publisher_rows[0]["message"]) == (
            ServerStatus.SKIPPED.value,
            report.message,
        )
        expected = (
            (ServerStatus.SKIPPED.value, report.message) if cached else (ServerStatus.WRITTEN.value, "2 marker(s)")
        )
        assert (second.publisher_rows[0]["status"], second.publisher_rows[0]["message"]) == expected
        assert plex.capability.call_count == (1 if cached else 2)
        assert [c.kwargs["canonical_path"] for c in plex.write.call_args_list] == ([] if cached else [other])

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
            patch.object(pipeline, "chromaprint_state", return_value=ChromaprintState.ABSENT),
            patch("media_preview_generator.markers.audio.season.chromaprint_ffmpeg", return_value=None),
        ):
            ctx = pipeline.build_context(registry=MagicMock(), config=MagicMock(ffmpeg_path=None), priority=3)
        assert ctx.live_config("plex-1") == cfg
        assert ctx.live_config("gone") is None


class TestPlexPassUnknown:
    """A READY Plex whose Plex Pass couldn't be read isn't written: Plex serves nothing without a Pass (audit C LOW).

    The file waits with a retry code, and the answer is reused only for a few seconds (pre-lab LOW-2, read-back
    review LOW-1): a Plex restart during a webhook follow-up must not leave the file for the next scheduled run, and
    a backfill while Plex's HTTP is down must not run the whole capability check for every file.
    """

    @pytest.mark.parametrize(
        ("stype", "details", "status", "message", "reason_code"),
        [
            (
                ServerType.PLEX,
                {"plex_pass": None},
                ServerStatus.WAITING,
                "Can't reach Plex to confirm Plex Pass",
                "plex_pass_unknown",
            ),
            (ServerType.PLEX, {"plex_pass": True}, ServerStatus.WRITTEN, "2 marker(s)", None),
            (ServerType.JELLYFIN, {"plugin_version": "10.11.1.0"}, ServerStatus.WRITTEN, "2 marker(s)", None),
            (ServerType.EMBY, {"plugin_version": "1.0.0.0"}, ServerStatus.WRITTEN, "2 marker(s)", None),
        ],
        ids=["plex-unknown", "plex-pass", "jellyfin", "emby"],
    )
    def test_unknown_pass_is_not_ready_for_writes(self, store, media, stype, details, status, message, reason_code):
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        pub = ready_publisher(
            {ServerType.PLEX: "plex_db", ServerType.JELLYFIN: "jellyfin_bridge"}.get(stype, "emby_bridge")
        )
        pub.capability.return_value = CapabilityReport(Capability.READY, "ready", details)
        ctx = _ctx(store, reg)
        out, _ = _run(ctx, media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        row = out.publisher_rows[0]
        assert (row["status"], row["message"], row.get("reason_code")) == (status.value, message, reason_code)
        assert pub.write.call_count == (0 if status is ServerStatus.WAITING else 1)
        assert sid in ctx._capabilities
        assert _state(store, media, sid).status == ("waiting" if status is ServerStatus.WAITING else "written")

    def test_files_a_few_seconds_apart_share_one_check(self, store, media):
        other = _second_episode(media)
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.capability.return_value = CapabilityReport(Capability.READY, "", {"plex_pass": None})
        ctx = _ctx(store, reg)
        outs = [_run(ctx, path, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))[0] for path in (media, other)]
        assert [(o.publisher_rows[0]["status"], o.publisher_rows[0].get("reason_code")) for o in outs] == [
            (ServerStatus.WAITING.value, "plex_pass_unknown")
        ] * 2
        assert plex.capability.call_count == 1
        plex.write.assert_not_called()

    def test_the_next_file_checks_plex_again_and_writes_once_it_answers(self, store, media, monkeypatch):
        monkeypatch.setattr(pipeline, "PLEX_PASS_UNKNOWN_TTL_S", 0.0)  # the next file comes after the short reuse
        other = _second_episode(media)
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        plex.capability.side_effect = [
            CapabilityReport(Capability.READY, "", {"plex_pass": None}),
            CapabilityReport(Capability.READY, "", {"plex_pass": True}),
        ]
        ctx = _ctx(store, reg)  # the 300 s job cache
        first, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        second, _ = _run(ctx, other, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert first.outcome_key == FileOutcome.WAITING.value
        assert (second.publisher_rows[0]["status"], second.publisher_rows[0]["message"]) == (
            ServerStatus.WRITTEN.value,
            "2 marker(s)",
        )
        assert [c.kwargs["canonical_path"] for c in plex.write.call_args_list] == [other]


class TestReadBackVerify:
    """Before an unchanged file is "Up to date" on a server, the server is asked whether it still shows ours."""

    @staticmethod
    def _published_then_checked(store, media, stype, shows, *, redetect="restore"):
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        if stype is ServerType.PLEX:
            reg.configs_by_id[sid].markers["plex"]["on_plex_redetect"] = redetect
        pub = ready_publisher("plex_db" if stype is ServerType.PLEX else "jellyfin_bridge")
        _run(_ctx(store, reg), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        if isinstance(shows, Exception):
            pub.shows.side_effect = shows
        else:
            pub.shows.return_value = shows
        pub.write.side_effect = lambda item_id, markers, **kw: (setattr(pub, "last_write_changed", True), markers)[1]
        out, _ = _run(_ctx(store, reg), media, {sid: pub})
        return pub, out.publisher_rows[0], out

    @pytest.mark.parametrize("stype", [ServerType.PLEX, ServerType.JELLYFIN])
    @pytest.mark.parametrize(
        ("shows", "redetect", "status", "message", "writes"),
        [
            (Shown.OURS, "restore", ServerStatus.UP_TO_DATE, "Up to date", 1),
            (None, "restore", ServerStatus.UP_TO_DATE, "Up to date", 1),  # couldn't be read
            (RuntimeError("HTTP 500"), "restore", ServerStatus.UP_TO_DATE, "Up to date", 1),
            (Shown.MISSING, "restore", ServerStatus.WRITTEN, "2 marker(s)", 2),
            (Shown.MISSING, "keep_plex", ServerStatus.WRITTEN, "2 marker(s)", 2),
            (Shown.REPLACED, "restore", ServerStatus.WRITTEN, "2 marker(s)", 2),
            # Whether Plex's own markers are kept is the publisher's call in its write (see the kept tests below).
            (Shown.REPLACED, "keep_plex", ServerStatus.WRITTEN, "2 marker(s)", 2),
            (Shown.VERSIONS_CHANGED, "restore", ServerStatus.WRITTEN, "2 marker(s)", 2),
        ],
        ids=[
            "ours",
            "unreadable",
            "read-raises",
            "missing",
            "missing-keep",
            "replaced",
            "replaced-keep",
            "versions-changed",
        ],
    )
    def test_matrix(self, store, media, stype, shows, redetect, status, message, writes):
        pub, row, out = self._published_then_checked(store, media, stype, shows, redetect=redetect)
        files = ("/plex/item-7.mkv",) if stype is ServerType.PLEX else None  # Jellyfin item ids are per version
        assert (row["status"], row["message"]) == (status.value, message)
        assert pub.write.call_count == writes
        assert [(c.args, c.kwargs) for c in pub.shows.call_args_list] == [
            ((f"item-{stype.value}-1", [INTRO_CH, CREDITS_CH]), {"kept_types": frozenset(), "item_files": files})
        ]
        # A read that failed is still "Up to date", but the job says it couldn't check (read-back review LOW-2).
        assert row.get("read_back_failed", False) is (shows is None or isinstance(shows, Exception))
        assert "verify_later" not in row
        if writes == 2:
            assert pub.write.call_args.kwargs["previous"] == [INTRO_CH, CREDITS_CH]
            assert out.outcome_key == FileOutcome.PUBLISHED.value

    @staticmethod
    def _keeping(pub, kept, shown, *, changed):
        def write(item_id, markers, **kwargs):
            pub.last_write_changed, pub.last_kept_types = changed, frozenset(kept)
            return [m for m in markers if m.type in shown]

        pub.write.side_effect = write

    @pytest.mark.parametrize("redetect", ["keep_plex", "restore"])
    def test_types_the_publisher_keeps_are_recorded_reported_and_passed_back(self, store, media, redetect):
        reg = _registry(media, ServerType.PLEX)
        reg.configs_by_id["plex-1"].markers["plex"]["on_plex_redetect"] = redetect
        pub = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": pub}, probe=_probe(CHAPTERS_BOTH))
        pub.shows.return_value = Shown.REPLACED
        self._keeping(pub, {T.CREDITS}, {T.INTRO}, changed=False)

        first, _ = _run(_ctx(store, reg), media, {"plex-1": pub})

        assert pub.write.call_args.kwargs["kept_types"] == frozenset()
        assert (first.publisher_rows[0]["status"], first.publisher_rows[0]["message"]) == (
            ServerStatus.UP_TO_DATE.value,
            "Keeping Plex's credits",
        )
        item = store.get_item_publish_state("plex-1", "item-plex-1")
        assert (item.markers, item.kept_types) == ((INTRO_CH,), {T.CREDITS})

        pub.shows.return_value = Shown.OURS
        second, _ = _run(_ctx(store, reg), media, {"plex-1": pub})

        assert pub.shows.call_args.args == ("item-plex-1", [INTRO_CH])
        assert pub.shows.call_args.kwargs == {"kept_types": {T.CREDITS}, "item_files": ("/plex/item-7.mkv",)}
        if redetect == "keep_plex":
            assert pub.write.call_count == 2
            assert (second.publisher_rows[0]["status"], second.publisher_rows[0]["message"]) == (
                ServerStatus.UP_TO_DATE.value,
                "Keeping Plex's credits",
            )
        else:
            # Switched to restore: the kept credits go through the publisher again, which puts ours back.
            assert pub.write.call_count == 3
            assert pub.write.call_args.kwargs["kept_types"] == {T.CREDITS}

    @pytest.mark.parametrize(
        ("kept", "shown", "changed", "status", "message"),
        [
            ({T.CREDITS}, {T.INTRO}, True, ServerStatus.WRITTEN, "1 marker(s); keeping Plex's credits"),
            (
                {T.CREDITS},
                set(),
                True,
                ServerStatus.WAITING,
                "Waiting for this item's other versions to agree on: intro; keeping Plex's credits",
            ),
            ({T.INTRO, T.CREDITS}, set(), False, ServerStatus.UP_TO_DATE, "Keeping Plex's intro and credits"),
            (
                {T.INTRO, T.CREDITS},
                set(),
                True,
                ServerStatus.WRITTEN,
                "Cleared our markers from this server; keeping Plex's intro and credits",
            ),
            (set(), {T.INTRO, T.CREDITS}, True, ServerStatus.WRITTEN, "2 marker(s)"),
        ],
        ids=["written", "waiting", "all-kept", "cleared", "none-kept"],
    )
    def test_file_row_names_the_kept_types(self, store, media, kept, shown, changed, status, message):
        reg = _registry(media, ServerType.PLEX)
        pub = ready_publisher()
        self._keeping(pub, kept, shown, changed=changed)
        out, _ = _run(_ctx(store, reg), media, {"plex-1": pub}, probe=_probe(CHAPTERS_BOTH))
        assert (out.publisher_rows[0]["status"], out.publisher_rows[0]["message"]) == (status.value, message)
        assert store.get_item_publish_state("plex-1", "item-plex-1").kept_types == kept

    @pytest.mark.parametrize(
        ("stype", "setting", "writes"),
        [
            (ServerType.PLEX, "keep_plex", 2),
            (ServerType.PLEX, "restore", 3),
            (ServerType.EMBY, "keep_emby", 2),
            (ServerType.EMBY, "restore", 3),
        ],
    )
    def test_each_vendors_keep_setting_holds_or_releases_kept_types(self, store, media, stype, setting, writes):
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        vendor = "Plex" if stype is ServerType.PLEX else "Emby"
        pub = ready_publisher("plex_db" if stype is ServerType.PLEX else "emby_bridge")
        _run(_ctx(store, reg), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        pub.shows.return_value = Shown.REPLACED
        self._keeping(pub, {T.CREDITS}, {T.INTRO}, changed=False)
        first, _ = _run(_ctx(store, reg), media, {sid: pub})
        assert first.publisher_rows[0]["message"] == f"Keeping {vendor}'s credits"

        block = "plex" if stype is ServerType.PLEX else "emby"
        key = "on_plex_redetect" if stype is ServerType.PLEX else "on_emby_redetect"
        reg.configs_by_id[sid].markers.setdefault(block, {})[key] = setting
        pub.shows.return_value = Shown.OURS
        second, _ = _run(_ctx(store, reg), media, {sid: pub})

        assert pub.write.call_count == writes
        if writes == 3:  # released: the kept credits go through the publisher again
            assert pub.write.call_args.kwargs["kept_types"] == {T.CREDITS}
        else:
            assert second.publisher_rows[0]["message"] == f"Keeping {vendor}'s credits"

    @pytest.mark.parametrize("publisher", ["plex_db", "emby_bridge"])
    def test_nothing_decided_on_an_item_of_kept_types_only_goes_through_the_write(self, store, media, publisher):
        # Emby's plugin still stores ours for a kept type; a Plex record left holding one is drift on every Check servers
        # run (audit MED-1). The real Plex publisher sends nothing to Plex's DB then (test_publisher_contract).
        stype = ServerType.PLEX if publisher == "plex_db" else ServerType.EMBY
        sid = f"{stype.value}-1"
        reg = _registry(media, stype)
        pub = ready_publisher(publisher)
        self._keeping(pub, {T.INTRO, T.CREDITS}, set(), changed=True)
        _run(_ctx(store, reg), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        assert store.get_item_publish_state(sid, f"item-{sid}").kept_types == {T.INTRO, T.CREDITS}
        pub.write.side_effect = pub.succeed
        pub.last_kept_types = frozenset()
        off = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": False, "credits": False}}

        _run(_ctx(store, reg, settings_raw=off), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))

        assert pub.write.call_count == 2
        call = pub.write.call_args
        assert call.args == (f"item-{sid}", []) and call.kwargs["previous"] == []
        assert call.kwargs["kept_types"] == {T.INTRO, T.CREDITS}
        assert store.get_item_publish_state(sid, f"item-{sid}").kept_types == frozenset()

    @pytest.mark.parametrize(
        ("kept", "shown", "changed", "status", "message"),
        [
            (set(), {T.INTRO, T.CREDITS}, True, ServerStatus.WRITTEN, "2 marker(s); Emby skips to the end of the file"),
            (set(), {T.INTRO, T.CREDITS}, False, ServerStatus.UP_TO_DATE, "Up to date; Emby skips to the end of the file"),
            ({T.INTRO}, {T.CREDITS}, True, ServerStatus.WRITTEN, "1 marker(s); keeping Emby's intro; Emby skips to the end of the file"),
        ],
        ids=["written", "unchanged", "kept"],
    )  # fmt: skip
    def test_a_note_for_markers_the_server_shows_differently_reaches_the_row(
        self, store, media, kept, shown, changed, status, message
    ):
        reg = _registry(media, ServerType.EMBY)
        emby = ready_publisher("emby_bridge")
        emby.projection_note.side_effect = lambda ms, **kw: (
            "Emby skips to the end of the file" if any(m.type is T.CREDITS for m in ms) else ""
        )
        self._keeping(emby, kept, shown, changed=changed)
        out, _ = _run(_ctx(store, reg), media, {"emby-1": emby}, probe=_probe(CHAPTERS_BOTH))
        row = out.publisher_rows[0]
        assert (row["status"], row["message"]) == (status.value, message)
        call = emby.projection_note.call_args
        assert call.args == ([m for m in (INTRO_CH, CREDITS_CH) if m.type in shown],)
        assert call.kwargs == {"duration_ms": DUR}

    def test_a_kept_type_is_kept_after_a_failed_write(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        pub = ready_publisher()
        self._keeping(pub, {T.CREDITS}, {T.INTRO}, changed=True)
        _run(_ctx(store, reg), media, {"plex-1": pub}, probe=_probe(CHAPTERS_BOTH))
        pub.write.side_effect = PublishError("Plex is busy", state=Capability.UNREACHABLE)
        os.utime(media, ns=(4, 4))
        _run(_ctx(store, reg), media, {"plex-1": pub}, probe=_probe(CHAPTERS_BOTH))
        assert store.get_item_publish_state("plex-1", "item-plex-1").kept_types == {T.CREDITS}
        self._keeping(pub, {T.CREDITS}, {T.INTRO}, changed=False)
        _run(_ctx(store, reg), media, {"plex-1": pub}, probe=_probe(CHAPTERS_BOTH))
        assert pub.write.call_args.kwargs["kept_types"] == {T.CREDITS}

    def test_nothing_of_ours_on_the_item_is_not_read_back(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        off = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"intro": False, "credits": False}}
        cleared, _ = _run(_ctx(store, reg, settings_raw=off), media, {"plex-1": plex})
        assert cleared.publisher_rows[0]["message"] == "Cleared our markers from this server"
        again, _ = _run(_ctx(store, reg, settings_raw=off), media, {"plex-1": plex})
        assert again.publisher_rows[0]["status"] == ServerStatus.UP_TO_DATE.value
        plex.shows.assert_not_called()  # the item record holds nothing of ours to look for
        assert plex.write.call_count == 2  # nor is it written to record the item's versions
        assert "read_back_failed" not in again.publisher_rows[0]

    @pytest.mark.parametrize(
        ("history", "verify_later"),
        [("new", False), ("unchanged", False), ("replaced", True), ("replaced-no-op", True)],
    )
    def test_a_replaced_files_rows_ask_for_a_later_check(self, store, media, history, verify_later):
        reg = _registry(media, ServerType.JELLYFIN)
        jf = ready_publisher("jellyfin_bridge")
        if history != "new":
            _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        if history.startswith("replaced"):
            os.utime(media, ns=(7, 7))
        if history == "replaced":
            jf.write.side_effect = lambda item_id, markers, **kw: (setattr(jf, "last_write_changed", True), markers)[1]
        out, _ = _run(_ctx(store, reg), media, {"jellyfin-1": jf}, probe=_probe(CHAPTERS_BOTH))
        row = out.publisher_rows[0]
        assert row.get("verify_later", False) is verify_later
        expected = ServerStatus.UP_TO_DATE if history in ("unchanged", "replaced-no-op") else ServerStatus.WRITTEN
        assert row["status"] == expected.value

    def test_a_replaced_file_that_waits_asks_for_no_later_check(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        os.utime(media, ns=(7, 7))
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = None
        out, _ = _run(_ctx(store, reg), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        assert out.publisher_rows[0]["status"] == ServerStatus.WAITING.value
        assert "verify_later" not in out.publisher_rows[0]


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

    @pytest.mark.parametrize(
        ("stype", "kept_only", "drift"),
        [
            (ServerType.PLEX, False, True),
            (ServerType.PLEX, True, False),  # none of ours on the item for a version added since to disagree with
            # Jellyfin and Emby item ids are per version: they record no versions, and the recording write is Plex's
            # only, so a record without files is never drift there. A kept-only row would be the same cell.
            (ServerType.JELLYFIN, False, False),
            (ServerType.EMBY, False, False),
        ],
        ids=["plex", "plex-kept-only", "jellyfin", "emby"],
    )
    def test_a_plex_record_without_item_files_is_written_once_to_record_them(
        self, store, media, stype, kept_only, drift
    ):
        reg = _registry(media, stype)
        sid = f"{stype.value}-1"
        pub = ready_publisher(
            {ServerType.PLEX: "plex_db", ServerType.EMBY: "emby_bridge"}.get(stype, "jellyfin_bridge")
        )
        pub.last_item_files = None  # published before item versions were recorded
        if kept_only:
            reg.configs_by_id[sid].markers["plex"]["on_plex_redetect"] = "keep_plex"
            TestReadBackVerify._keeping(pub, {T.INTRO, T.CREDITS}, set(), changed=True)
        _run(_ctx(store, reg), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))
        row = store.get_item_publish_state(sid, f"item-{sid}")
        assert row.item_files is None and bool(row.markers) is not kept_only
        if stype is ServerType.PLEX:
            pub.last_item_files = ("/plex/a.mkv",)
        outs = [_run(_ctx(store, reg), media, {sid: pub}, probe=_probe(CHAPTERS_BOTH))[0] for _ in range(2)]
        assert [o.outcome_key for o in outs] == [FileOutcome.UP_TO_DATE.value] * 2
        assert pub.write.call_count == (2 if drift else 1)
        if drift:
            assert pub.write.call_args.kwargs["previous"] == [INTRO_CH, CREDITS_CH]
            assert pub.shows.call_count == 1 and pub.shows.call_args.kwargs["item_files"] == ("/plex/a.mkv",)
        else:
            assert pub.shows.call_count == 2


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

        def plex_markers(item_id):
            cancelled.set()
            return []

        reg.get("plex-1").get_markers.side_effect = plex_markers
        plex = ready_publisher()
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
        # The intros in review outrank the Plex row on each file.
        assert out_a.publisher_rows[0]["status"] == ServerStatus.WAITING.value
        assert out_a.outcome_key == FileOutcome.NEEDS_REVIEW.value
        out_b = self._check(store, reg, items, b, CHAPTERS_CREDITS_ONLY, clients=review)
        assert out_b.publisher_rows[0]["status"] == ServerStatus.WRITTEN.value
        assert out_b.outcome_key == FileOutcome.NEEDS_REVIEW.value
        out_a = self._check(store, reg, items, a, clients=review)
        # B's publish already shows A's credits.
        assert out_a.publisher_rows[0]["status"] == ServerStatus.UP_TO_DATE.value
        assert out_a.outcome_key == FileOutcome.NEEDS_REVIEW.value
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
        items.shown.pop("42")  # removed and added again: Plex rebuilt the item without our markers
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
            "kept_types": frozenset(),
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
        plex_server, jellyfin_server = reg.get("plex-1"), reg.get("jellyfin-1")
        for _ in range(2):
            ctx = _ctx(store, reg, clients=clients)
            ctx.priority = lambda: 3  # a Low backfill keeps TheIntroDB's budget when only chapters decide
            _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))
        # "No data" answers and empty servers are stored, so the second normal run asks nothing again.
        assert [len(c.calls) for c in clients.values()] == [0, 1, 1]
        assert (plex_server.get_markers.call_count, jellyfin_server.get_media_segments.call_count) == (1, 1)
        ctx = _ctx(store, reg, clients=clients, force=True)
        ctx.priority = lambda: 3
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))
        assert [len(c.calls) for c in clients.values()] == [1, 2, 2]
        # Plex now shows our markers, so only the server we never published to is read again.
        assert (plex_server.get_markers.call_count, jellyfin_server.get_media_segments.call_count) == (1, 2)
        jellyfin_server.get_media_segments.assert_called_with("item-jellyfin-1")
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
        # SkipDB answers only a duration match, so its lone answer may publish at "Medium".

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
                return LookupResult("ok", (Candidate(T.INTRO, 128_000, 157_000, Source.SKIPDB),))
            if duration_ms == old_duration:
                return LookupResult("ok", (Candidate(T.INTRO, 128_000, 157_000, Source.SKIPDB),))
            return LookupResult("ok", (Candidate(T.INTRO, 186_000, 216_000, Source.SKIPDB),))

        clients["skipdb"].lookup = lookup

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
        new_intro = Marker(T.INTRO, 186_000, 216_000, ("skipdb",))
        rec = store.get_file(media)
        assert (rec.size, rec.mtime_ns, rec.duration_ms) == (200, 9, new_duration)
        assert [(c.start_ms, c.end_ms) for c in store.get_evidence(rec.id) if c.source is Source.SKIPDB] == [
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


class TestRealPlexPublisher:
    def test_a_job_writes_without_asking_plex_for_its_own_detection_settings(self, store, media, tmp_path, monkeypatch):
        # The real factory and PlexMarkerPublisher against a Plex 1.43 database file: Plex's detection settings are
        # for the Edit dialog, and each job's capability check would otherwise ask Plex for them too.
        from media_preview_generator.markers.publishers import plex_db
        from tests.markers.test_plex_db_publisher import PLEX_VERSION, _make_db, _served

        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((media, None),))
        monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)  # Plex has it open
        monkeypatch.setattr(plex_db, "filesystem_type", lambda _path, **_kw: "ext4")
        reg = _registry(media, ServerType.PLEX)
        reg.configs_by_id["plex-1"].output = {"plex_config_folder": str(folder)}
        server = reg.get("plex-1")
        server.resolve_remote_path_to_item_id.return_value = "7"
        server.get_server_status.return_value = {"plex_pass": True, "version": PLEX_VERSION}
        with patch.object(pipeline, "probe_media", return_value=_probe(CHAPTERS_BOTH)):
            out = check_item(_item(media), ctx=_ctx(store, reg))
        assert _rows(out)["plex-1"]["status"] == ServerStatus.WRITTEN.value
        assert [row[0] for row in _served(str(db))] == ["intro", "credits"]
        server.get_server_status.assert_called_with()  # the capability check did reach Plex
        server.get_marker_detection_prefs.assert_not_called()


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


@pytest.mark.parametrize("change", ["replaced", "removed"])
def test_markers_for_path_of_a_version_changed_on_disk_since_it_was_decided_is_unknown(store, media, change):
    # Its stored decisions describe the old file, so they must not help another version's publish agree.
    reg = _registry(media, ServerType.PLEX)
    _run(_ctx(store, reg), media, {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))
    assert pipeline.markers_for_path(store, media) == {T.INTRO: INTRO_CH, T.CREDITS: CREDITS_CH}
    if change == "replaced":
        with open(media, "wb") as f:
            f.write(b"z" * 300)
    else:
        os.remove(media)
    assert pipeline.markers_for_path(store, media) is None


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

    @pytest.mark.parametrize("theintrodb", [True, False])
    @pytest.mark.parametrize("introdb", [True, False])
    @pytest.mark.parametrize("skipdb", [True, False])
    def test_build_clients_matrix(self, theintrodb, introdb, skipdb):
        from media_preview_generator.markers.sources.skipdb import SkipDbClient

        wanted = {"theintrodb": theintrodb, "introdb": introdb, "skipdb": skipdb}
        raw = {"sources": [{"id": sid, "enabled": on} for sid, on in wanted.items()]}
        clients = pipeline.build_clients(load_global(validate_global(raw, None)[0]))
        assert set(clients) == {sid for sid, on in wanted.items() if on}
        assert not skipdb or isinstance(clients["skipdb"], SkipDbClient)

    def test_build_context_uses_live_settings_store_and_ffprobe(self, store):
        settings = load_global({})
        config = MagicMock(ffmpeg_path="/usr/lib/jellyfin-ffmpeg/ffmpeg")
        registry = MagicMock()
        with (
            patch.object(pipeline, "get_global_settings", return_value=settings),
            patch.object(pipeline, "get_marker_store", return_value=store),
            patch.object(pipeline, "ffprobe_path_for", return_value="/x/ffprobe") as ffprobe_for,
            patch.object(pipeline, "build_clients", return_value={"introdb": "client"}) as build,
            patch.object(pipeline, "chromaprint_state", return_value=ChromaprintState.ABSENT),
            patch("media_preview_generator.markers.audio.season.chromaprint_ffmpeg", return_value=None),
        ):
            ctx = pipeline.build_context(registry=registry, config=config, priority=3, force=True)
        ffprobe_for.assert_called_once_with("/usr/lib/jellyfin-ffmpeg/ffmpeg")
        build.assert_called_once_with(settings)
        assert (ctx.registry, ctx.config, ctx.settings, ctx.store) == (registry, config, settings, store)
        assert (ctx.priority(), ctx.force, ctx.ffprobe, ctx.clients) == (3, True, "/x/ffprobe", {"introdb": "client"})
        assert ctx.local_detectors == ()
        assert ctx.recheck_empty_server_markers is False
        with (
            patch.object(pipeline, "get_global_settings", return_value=settings),
            patch.object(pipeline, "get_marker_store", return_value=store),
            patch.object(pipeline, "build_clients", return_value={}),
            patch.object(pipeline, "chromaprint_state", return_value=ChromaprintState.ABSENT),
            patch("media_preview_generator.markers.audio.season.chromaprint_ffmpeg", return_value=None),
        ):
            ctx = pipeline.build_context(
                registry=registry, config=config, priority=3, recheck_empty_server_markers=True
            )
        assert ctx.recheck_empty_server_markers is True
        live = {"priority": 3}
        with (
            patch.object(pipeline, "get_global_settings", return_value=settings),
            patch.object(pipeline, "get_marker_store", return_value=store),
            patch.object(pipeline, "build_clients", return_value={}),
            patch.object(pipeline, "chromaprint_state", return_value=ChromaprintState.ABSENT),
            patch("media_preview_generator.markers.audio.season.chromaprint_ffmpeg", return_value=None),
        ):
            ctx = pipeline.build_context(registry=registry, config=config, priority=lambda: live["priority"])
        live["priority"] = 1
        assert ctx.priority() == 1

    @pytest.mark.parametrize(
        ("state", "found", "warning"),
        [
            (ChromaprintState.AVAILABLE, "/usr/lib/jellyfin-ffmpeg/ffmpeg", None),
            (ChromaprintState.ABSENT, None, "no ffmpeg with chromaprint was found"),
            (ChromaprintState.UNKNOWN, None, "ffmpeg didn't answer the check for chromaprint"),
        ],
        ids=["available", "absent", "unknown"],
    )
    def test_build_context_records_the_chromaprint_state_and_registers_season_audio_only_when_available(
        self, store, loguru_caplog, state, found, warning
    ):
        settings = load_global({})
        config = MagicMock(ffmpeg_path="/usr/local/bin/ffmpeg")
        with (
            patch.object(pipeline, "get_global_settings", return_value=settings),
            patch.object(pipeline, "get_marker_store", return_value=store),
            patch.object(pipeline, "build_clients", return_value={}),
            patch.object(pipeline, "chromaprint_state", return_value=state) as checked,
            patch("media_preview_generator.markers.audio.season.chromaprint_ffmpeg", return_value=found) as looked_up,
        ):
            ctx = pipeline.build_context(registry=MagicMock(), config=config, priority=3)
        checked.assert_called_once_with("/usr/local/bin/ffmpeg")
        assert all(call.args == ("/usr/local/bin/ffmpeg",) for call in looked_up.call_args_list)
        assert ctx.chromaprint is state
        assert [s.source for s in ctx.local_detectors] == ([Source.SEASON_AUDIO] if found else [])
        messages = {"no ffmpeg with chromaprint was found", "ffmpeg didn't answer the check for chromaprint"}
        assert {m for m in messages if m in loguru_caplog.text} == ({warning} if warning else set())

    def test_decision_order_ranks_each_rider_right_after_its_switch(self):
        raw = {"sources": [{"id": "server_markers"}, {"id": "season_audio"}, {"id": "chapters"}]}
        order = pipeline._decision_order(load_global(validate_global(raw, None)[0]))
        assert order[:5] == ("server_markers", "server_markers_imported", "season_audio", "season_audio_previous",
                             "chapters")  # fmt: skip
        off = {"sources": [{"id": "season_audio", "enabled": False}]}
        assert "season_audio_previous" not in pipeline._decision_order(load_global(validate_global(off, None)[0]))


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
        # Every status alone and every pair for two owners, sources agreeing (NEEDS_REVIEW rows need needs_review).
        ({W}, False, FileOutcome.PUBLISHED),
        ({U}, False, FileOutcome.UP_TO_DATE),
        ({S}, False, FileOutcome.SKIPPED),
        ({A}, False, FileOutcome.WAITING),
        ({F}, False, FileOutcome.FAILED),
        ({N}, False, FileOutcome.NO_MARKERS),
        (set(), False, FileOutcome.NO_MARKERS),
        ({W, U}, False, FileOutcome.PUBLISHED),
        ({W, S}, False, FileOutcome.PUBLISHED),
        ({W, A}, False, FileOutcome.WAITING),  # Jellyfin written, Jellyfin 12.0 hasn't indexed the file (lab row 8 A)
        ({W, F}, False, FileOutcome.FAILED),  # a broken write isn't hidden behind the server that took it
        ({W, N}, False, FileOutcome.PUBLISHED),
        ({U, S}, False, FileOutcome.UP_TO_DATE),
        ({U, A}, False, FileOutcome.WAITING),  # Jellyfins up to date, Plex waiting for the versions (lab row 8 B1)
        ({U, F}, False, FileOutcome.FAILED),
        ({U, N}, False, FileOutcome.UP_TO_DATE),
        ({S, A}, False, FileOutcome.WAITING),
        ({S, F}, False, FileOutcome.FAILED),
        ({S, N}, False, FileOutcome.NO_MARKERS),
        ({A, F}, False, FileOutcome.FAILED),
        ({A, N}, False, FileOutcome.WAITING),
        ({F, N}, False, FileOutcome.FAILED),
        # Three owners: one waiting or failed server still decides the file.
        ({W, U, A}, False, FileOutcome.WAITING),
        ({W, A, F}, False, FileOutcome.FAILED),
        ({U, S, N}, False, FileOutcome.UP_TO_DATE),
        # A marker type the sources don't agree on: every status alone and with a needs-review row.
        ({R}, True, FileOutcome.NEEDS_REVIEW),
        ({R, W}, True, FileOutcome.NEEDS_REVIEW),
        ({R, U}, True, FileOutcome.NEEDS_REVIEW),
        ({R, S}, True, FileOutcome.NEEDS_REVIEW),
        ({R, A}, True, FileOutcome.NEEDS_REVIEW),
        ({R, F}, True, FileOutcome.FAILED),
        ({R, N}, True, FileOutcome.NEEDS_REVIEW),
        ({W}, True, FileOutcome.NEEDS_REVIEW),  # intro written, credits need review
        ({U}, True, FileOutcome.NEEDS_REVIEW),
        ({S}, True, FileOutcome.NEEDS_REVIEW),
        ({A}, True, FileOutcome.NEEDS_REVIEW),
        ({F}, True, FileOutcome.FAILED),
        ({N}, True, FileOutcome.NEEDS_REVIEW),
        (set(), True, FileOutcome.NEEDS_REVIEW),
        ({W, A}, True, FileOutcome.NEEDS_REVIEW),
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
