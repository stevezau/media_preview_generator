"""Builders shared by the pipeline test modules: a pipeline context, a registry of fake servers, a probe, one run."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.decide import FileLimits
from media_preview_generator.markers.models import Candidate, Marker, MarkerType, Source
from media_preview_generator.markers.pipeline import PipelineContext, check_item, process_item
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.processing.types import ProcessableItem
from tests.markers.fakes import FakeClient, FakeRegistry, server_config

T = MarkerType
DUR = 1_321_472
# What every write for ``media`` carries as the file's limits.
EPISODE_LIMITS = FileLimits(DUR)
# A file replaced by another cut (another length): nothing of the file it replaced carries over.
NEW_CUT = DUR + 2_000
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
    return ProcessableItem(
        canonical_path=path, server_id="plex-1", item_id_by_server=hints or {}, title=os.path.basename(path)
    )


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


EPISODE_IDS = {"kind": "episode", "tmdb": None, "imdb": "tt7654321", "tvdb": "999", "season": 1, "episode": 3}
MOVIE_IDS = {"kind": "movie", "tmdb": "9999", "imdb": "tt0114709", "tvdb": None, "season": None, "episode": None}
UNKNOWN_IDS = {"kind": "unknown", "tmdb": None, "imdb": None, "tvdb": None, "season": None, "episode": None}
