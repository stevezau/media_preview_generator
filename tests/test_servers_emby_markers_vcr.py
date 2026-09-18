"""Cassette-backed contract tests for the Emby Bridge markers routes and Emby's chapter read (recorded on mlab-emby).

Re-record against the storage lab's Emby 4.10 with the Media Preview Bridge for Emby plugin installed (see
tests/cassettes/README.md, "Markers (lab)"). The plugin catalog read (``bridge_catalog_listed``) isn't recorded: Emby's
``/Packages`` answer is the whole public catalog (1.4 MB); tests/test_servers_emby_bridge.py covers its shape.
"""

from __future__ import annotations

import os

import pytest

from media_preview_generator.servers import EmbyServer, Library, ServerConfig, ServerType
from media_preview_generator.servers import emby as emby_module

pytestmark = [pytest.mark.vcr]

SYNTH_SEASON = "/media/synth-chapters/Synth Chapters (2021)/Season 01"
# S01E02: one version on both lab Embys (Emby 4.10 groups S01E01 with its "- Extended" copy of another length).
SYNTH_E02 = f"{SYNTH_SEASON}/Synth Chapters (2021) - S01E02.webm"
SYNTH_E02_SIZE = 6_441_241  # bytes of that file (synth_chapters.sh output)
SYNTH_E01 = f"{SYNTH_SEASON}/Synth Chapters (2021) - S01E01.webm"
SYNTH_E01_EXTENDED = f"{SYNTH_SEASON}/Synth Chapters (2021) - S01E01 - Extended.webm"


def _emby_lab(*, per_user: bool) -> EmbyServer:
    # The per-user item route answers a single item (never collapsed by the cassette scrubber, unlike an ``Items``
    # list without paths); the recorded user id is scrubbed to FAKE_USER_ID, which replay then sends.
    auth = {"method": "api_key", "api_key": os.environ.get("EMBY_TOKEN", "fake-token")}
    if per_user:
        auth["user_id"] = os.environ.get("EMBY_USER_ID", "FAKE_USER_ID")
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


@pytest.fixture
def emby_lab():
    return _emby_lab(per_user=True)


class TestEmbyBridgeMarkersContract:
    def test_ping_access_store_show_delete(self, emby_lab):
        assert emby_lab.get_bridge_info() == {"installed": True, "version": "1.0.0.0", "features": ["markers"]}
        assert emby_lab.get_bridge_markers_access() == "ok"
        item_id = emby_lab._uncached_resolve_remote_path_to_item_id(SYNTH_E02)
        assert item_id
        before = emby_lab.get_emby_marker_state(item_id)
        assert before is not None and before["intro_start_ticks"] is None  # the lab Emby holds none of ours
        size = SYNTH_E02_SIZE
        resp = emby_lab.put_emby_markers(
            item_id,
            intro_start_ticks=100_000_000,
            intro_end_ticks=400_000_000,
            credits_start_ticks=1_000_000_000,
            file_size=size,
            replace_own=True,
        )
        body = resp.json()
        assert resp.status_code == 200 and body["Stored"] == 3 and body["Stale"] is False
        assert (body["IntroStartTicks"], body["IntroEndTicks"], body["CreditsStartTicks"], body["FileSize"]) == (
            100_000_000,
            400_000_000,
            1_000_000_000,
            size,
        )
        state = emby_lab.get_emby_marker_state(item_id)
        assert state == {
            "intro_start_ticks": 100_000_000,
            "intro_end_ticks": 400_000_000,
            "credits_start_ticks": 1_000_000_000,
            "file_size": size,
            "stale": False,
        }
        rows = emby_lab.get_chapter_markers(item_id)
        kinds = {(r["marker_type"], r["start_ms"], r["name"]) for r in rows}
        assert {
            ("IntroStart", 10_000, "Intro"),
            ("IntroEnd", 40_000, "Intro End"),
            ("CreditsStart", 100_000, "Credits"),
        } <= kinds
        assert any(r["marker_type"] == "Chapter" for r in rows)  # the file's own chapters stay
        deleted = emby_lab.delete_emby_markers(item_id)
        assert deleted.status_code == 200 and deleted.json()["Stored"] == 0
        after = emby_lab.get_emby_marker_state(item_id)
        # Emby leaves null fields out of the answer: nothing stored reads as None everywhere.
        assert after == {
            "intro_start_ticks": None,
            "intro_end_ticks": None,
            "credits_start_ticks": None,
            "file_size": None,
            "stale": False,
        }
        assert not [r for r in emby_lab.get_chapter_markers(item_id) if r["marker_type"] != "Chapter"]

    def test_unknown_item_is_not_found(self, emby_lab):
        assert emby_lab.get_emby_marker_state("999999999998") is None
        resp = emby_lab.put_emby_markers(
            "999999999998",
            intro_start_ticks=1,
            intro_end_ticks=2,
            credits_start_ticks=None,
            file_size=None,
            replace_own=False,
        )
        assert resp.status_code == 200 and resp.json() == {
            "Id": "999999999998",
            "Found": False,
            "Error": "item not found",
            "Stale": False,
            "Stored": 0,
        }


class TestEmbyPremiereRegistrationContract:
    """The Intro & Credits tab's Emby Premiere note reads ``GET /Registrations/dvr``: the lab Emby has no Premiere key."""

    def test_a_server_without_a_premiere_key_is_not_registered(self):
        emby_module.clear_catalog_cache()  # the answer is kept per server URL for an hour
        try:
            assert _emby_lab(per_user=False).intro_skip_registered() is False
        finally:
            emby_module.clear_catalog_cache()


class TestEmbyItemMissingContract:
    """Check servers' read-back of an item Emby deleted: the chapter read fails, then Emby confirms the id is gone."""

    UNKNOWN_ITEM = "999999999997"

    @pytest.mark.parametrize("per_user", [False, True], ids=["api-key", "per-user"])
    def test_an_id_emby_doesnt_have_is_missing(self, per_user):
        emby = _emby_lab(per_user=per_user)
        assert emby.get_chapter_markers(self.UNKNOWN_ITEM) is None
        assert emby.item_missing(self.UNKNOWN_ITEM) is True

    def test_an_item_emby_has_is_not_missing(self, emby_lab):
        item_id = emby_lab._uncached_resolve_remote_path_to_item_id(SYNTH_E02)
        assert item_id
        assert emby_lab.item_missing(item_id) is False


class TestEmbyVersionsContract:
    @pytest.mark.parametrize("per_user", [False, True], ids=["api-key", "per-user"])
    def test_a_grouped_item_lists_every_version_with_its_own_item(self, per_user):
        # Emby groups S01E01 with its Extended cut. With an API key the other version shows only because the read asks
        # for AlternateMediaSources; each source names the item that version is.
        emby = _emby_lab(per_user=per_user)
        episode = emby._uncached_resolve_remote_path_to_item_id(SYNTH_E01)
        extended = emby._uncached_resolve_remote_path_to_item_id(SYNTH_E01_EXTENDED)
        assert episode and extended and episode != extended
        for item_id in (episode, extended):
            answer = emby.get_chapters_and_versions(item_id)
            assert answer is not None
            chapters, versions = answer
            assert sorted(versions) == sorted([(SYNTH_E01, episode), (SYNTH_E01_EXTENDED, extended)])
            assert [r["marker_type"] for r in chapters if r["marker_type"] != "Chapter"] == []  # nothing of ours
            assert chapters  # each version's own chapter rows (the synth files carry some)


class TestUserIdScrub:
    """The recording user's id is recorded as FAKE_USER_ID; any other id is left as sent, so it can't match on replay."""

    @staticmethod
    def _scrubbed(uri):
        from vcr.request import Request

        from tests.conftest import _scrub_request_uri

        return _scrub_request_uri(Request("GET", uri, None, {})).uri

    def test_the_recording_users_id_becomes_fake(self, monkeypatch):
        monkeypatch.setenv("EMBY_USER_ID", "0123456789abcdef0123456789abcdef")
        assert (
            self._scrubbed("http://127.0.0.1:18096/Users/0123456789abcdef0123456789abcdef/Items/51?Fields=Chapters")
            == "http://fake-server/Users/FAKE_USER_ID/Items/51?Fields=Chapters"
        )

    def test_another_id_is_left_as_sent(self, monkeypatch):
        monkeypatch.delenv("EMBY_USER_ID", raising=False)
        monkeypatch.delenv("JELLYFIN_USER_ID", raising=False)
        assert (
            self._scrubbed("http://127.0.0.1:18096/Users/fedcba9876543210fedcba9876543210/Items/51")
            == "http://fake-server/Users/fedcba9876543210fedcba9876543210/Items/51"
        )


class TestItemsWithoutPathScrub:
    """An ``Items`` answer asked for other fields than ``Path`` is kept only when every version's file is synthetic."""

    @staticmethod
    def _kept_items(sources):
        import json

        from tests.conftest import _scrub_response_body

        body = json.dumps({"Items": [{"Id": "53", "MediaSources": sources}], "TotalRecordCount": 1})
        scrubbed = _scrub_response_body({"headers": {}, "body": {"string": body}})
        return json.loads(scrubbed["body"]["string"])["Items"]

    def test_synthetic_versions_are_kept(self):
        sources = [{"ItemId": "55", "Path": SYNTH_E01_EXTENDED}, {"ItemId": "53", "Path": SYNTH_E01}]
        assert self._kept_items(sources) == [{"Id": "53", "MediaSources": sources}]

    @pytest.mark.parametrize(
        "sources",
        [[{"ItemId": "53", "Path": SYNTH_E01}, {"ItemId": "9", "Path": "/data/tv/Real Show - S01E01.mkv"}], [], None],
        ids=["one-real-file", "no-sources", "no-sources-field"],
    )
    def test_anything_else_is_dropped(self, sources):
        assert self._kept_items(sources) == []
