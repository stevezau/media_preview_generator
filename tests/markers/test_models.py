"""Value-object behavior for the Intro & Credits models (later tasks build on these exact shapes)."""

import dataclasses

import pytest

from media_preview_generator.markers.models import (
    LOCAL_SOURCES,
    Candidate,
    FileIdentity,
    Marker,
    MarkerType,
    MediaIds,
    Source,
)


class TestMediaIds:
    def test_is_episode_true_when_kind_is_episode(self):
        assert MediaIds(kind="episode", season=1, episode=3).is_episode is True

    def test_is_episode_false_when_kind_is_movie_or_unknown(self):
        assert MediaIds(kind="movie").is_episode is False
        assert MediaIds().is_episode is False  # default kind is "unknown"


class TestLocalSources:
    def test_local_sources_are_exactly_chapters_season_audio_credits_text(self):
        assert LOCAL_SOURCES == {Source.CHAPTERS, Source.SEASON_AUDIO, Source.CREDITS_TEXT}

    def test_online_and_server_sources_are_not_local(self):
        assert Source.THEINTRODB not in LOCAL_SOURCES
        assert Source.INTRODB not in LOCAL_SOURCES
        assert Source.SKIPDB not in LOCAL_SOURCES
        assert Source.SERVER_MARKERS not in LOCAL_SOURCES
        assert Source.USER not in LOCAL_SOURCES


class TestValueObjectsAreFrozen:
    def test_candidate_is_immutable(self):
        cand = Candidate(MarkerType.INTRO, 0, 1000, Source.CHAPTERS)
        with pytest.raises(dataclasses.FrozenInstanceError):
            cand.start_ms = 500

    def test_candidate_defaults(self):
        cand = Candidate(MarkerType.INTRO, 0, 1000, Source.CHAPTERS)
        assert cand.confidence == 1.0 and cand.origin == ""

    def test_marker_defaults_unlocked(self):
        marker = Marker(MarkerType.CREDITS, 1_000, 2_000, ("chapters",))
        assert marker.locked is False

    def test_file_identity_holds_path_size_mtime(self):
        fid = FileIdentity("/data/show/s01e01.mkv", 123, 456)
        assert (fid.canonical_path, fid.size, fid.mtime_ns) == ("/data/show/s01e01.mkv", 123, 456)
