"""Value-object behavior for the Intro & Credits models (later tasks build on these exact shapes)."""

from media_preview_generator.markers.models import (
    SERVER_SOURCES,
    MediaIds,
    Source,
)


class TestMediaIds:
    def test_is_episode_true_when_kind_is_episode(self):
        assert MediaIds(kind="episode", season=1, episode=3).is_episode is True

    def test_is_episode_false_when_kind_is_movie_or_unknown(self):
        assert MediaIds(kind="movie").is_episode is False
        assert MediaIds().is_episode is False  # default kind is "unknown"


class TestServerSources:
    def test_server_sources_are_a_servers_own_markers_and_an_importer_plugins_copy(self):
        assert SERVER_SOURCES == {Source.SERVER_MARKERS, Source.SERVER_MARKERS_IMPORTED}

    def test_the_imported_source_round_trips_by_value(self):
        assert Source("server_markers_imported") is Source.SERVER_MARKERS_IMPORTED
