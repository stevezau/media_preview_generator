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

        # Pin the actual millisecond values the lab backfill published for the synth
        # chapters (phase1-results.md: intro 10-40s, credits 100-120s) so a units
        # regression (e.g. ms -> ticks) fails loudly instead of just satisfying the
        # shape checks above.
        by_type = {m["type"]: m for m in markers}
        assert by_type.keys() == {"intro", "credits"}
        assert by_type["intro"]["start_ms"] == 10_000
        assert by_type["intro"]["end_ms"] == 40_000
        assert by_type["credits"]["start_ms"] == 100_000
        assert by_type["credits"]["end_ms"] == 120_000

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
