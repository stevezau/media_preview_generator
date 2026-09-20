"""The Inspector editor's write API: save + lock + publish now, and unlock.

`POST /api/markers/item/markers` and `DELETE /api/markers/item/markers` (plan ruling P-R4). The publish itself is
covered in test_publish_now.py; here the contract is the request shape, the refusals, what reaches `publish_now`, and
the per-server answer the editor renders.
"""

from __future__ import annotations

import os

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType
from media_preview_generator.markers.outcomes import ServerStatus
from media_preview_generator.markers.publishers.emby import CREDITS_BEFORE_END_NOTE
from media_preview_generator.markers.store import LOCKED_BY_USER, UNLOCKED_PENDING, get_marker_store
from media_preview_generator.web.routes.api_markers import _EDITOR_RESULTS
from tests.markers.conftest import api_headers as _api_headers

T = MarkerType
DUR = 1_320_000
PLEX_TOKEN = "plex-token-SECRET-1234"


@pytest.fixture
def media(tmp_path):
    root = tmp_path.resolve() / "media"
    (root / "tv" / "Show").mkdir(parents=True)
    (root / "tv" / "Show" / "S01E01.mkv").write_bytes(b"x" * 100)
    (tmp_path / "outside.mkv").write_bytes(b"x")
    return root


@pytest.fixture
def episode(media):
    return str(media / "tv" / "Show" / "S01E01.mkv")


def _server(sid, stype, media, *, markers=True, extra_markers=None):
    block = {"enabled": markers, "library_ids": None, **(extra_markers or {})}
    if stype == "plex":
        block.setdefault("plex", {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"})
    return {
        "id": sid,
        "type": stype,
        "name": sid.upper(),
        "enabled": True,
        "url": "http://127.0.0.1:9",
        "auth": {"token": PLEX_TOKEN} if stype == "plex" else {"api_key": "key"},
        "libraries": [{"id": "1", "name": "TV Shows", "remote_paths": [str(media / "tv")]}],
        "markers": block,
    }


@pytest.fixture
def servers(app, media):
    from media_preview_generator.web.settings_manager import get_settings_manager

    entries = [
        _server("plex-1", "plex", media),
        _server("jf-1", "jellyfin", media),
        _server("emby-1", "emby", media),
    ]
    get_settings_manager().set("media_servers", entries)
    return entries


@pytest.fixture
def only_plex_and_emby(servers):
    """The two servers that can't show a recap or a preview, so those types have no owner that shows them."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    entries = [e for e in servers if e["id"] != "jf-1"]
    get_settings_manager().set("media_servers", entries)
    return entries


@pytest.fixture
def known(app, episode):
    st = os.stat(episode)
    store = get_marker_store()
    return store.upsert_file(
        FileIdentity(episode, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
    )


@pytest.fixture
def published(monkeypatch):
    """Captures every ``publish_now`` call and answers with the rows a test asks for."""

    class Recorder:
        def __init__(self):
            self.calls = []
            self.rows = []

        def __call__(self, path, **kwargs):
            self.calls.append({"path": path, **kwargs})
            return self.rows

    recorder = Recorder()
    monkeypatch.setattr(pipeline, "publish_now", recorder)
    return recorder


def _row(server_id, server_type, status, message=""):
    return {
        "server_id": server_id,
        "server_name": server_id.upper(),
        "server_type": server_type,
        "adapter_name": "markers",
        "status": status,
        "message": message,
        "canonical_path": "",
        "frame_source": "",
        "output_paths": [],
    }


def _save(client, episode, markers, **extra):
    return client.post(
        "/api/markers/item/markers", headers=_api_headers(), json={"path": episode, "markers": markers, **extra}
    )


class TestSaveRefusals:
    def test_a_body_that_is_not_an_object_is_refused(self, client, servers):
        resp = client.post("/api/markers/item/markers", headers=_api_headers(), json=[1])
        assert resp.status_code == 400

    def test_a_request_with_neither_path_nor_item_is_refused(self, client, servers):
        resp = client.post("/api/markers/item/markers", headers=_api_headers(), json={"markers": []})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Give path, or server_id and item_id"

    def test_a_path_outside_every_server_library_is_refused(self, client, servers, tmp_path):
        resp = _save(client, str(tmp_path / "outside.mkv"), [{"type": "intro", "start_ms": 0, "end_ms": 5_000}])
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Path is not a file inside any server library"

    def test_an_unknown_server_id_is_a_404(self, client, servers):
        resp = client.post(
            "/api/markers/item/markers",
            headers=_api_headers(),
            json={"server_id": "nope", "item_id": "1", "markers": []},
        )
        assert resp.status_code == 404

    def test_an_item_id_that_is_not_shaped_like_that_servers_ids_is_refused(self, client, servers):
        resp = client.post(
            "/api/markers/item/markers",
            headers=_api_headers(),
            json={"server_id": "plex-1", "item_id": "abc", "markers": []},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "item_id isn't an item id this server uses"

    def test_a_file_that_was_never_analysed_is_refused_with_a_reason_the_ui_can_act_on(self, client, servers, episode):
        resp = _save(client, episode, [{"type": "intro", "start_ms": 0, "end_ms": 5_000}])
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == "not_analysed"

    @pytest.mark.parametrize(
        ("markers", "expected"),
        [
            ([], "markers must be a non-empty list of {type, start_ms, end_ms}"),
            ("intro", "markers must be a non-empty list of {type, start_ms, end_ms}"),
            ([{"start_ms": 0, "end_ms": 5}], "type must be one of intro, credits, recap, preview"),
            ([{"type": "chapter", "start_ms": 0, "end_ms": 5}], "type must be one of intro, credits, recap, preview"),
            (
                [{"type": "intro", "start_ms": 0, "end_ms": 5}, {"type": "intro", "start_ms": 1, "end_ms": 6}],
                "markers has intro twice",
            ),
            (
                [{"type": "intro", "start_ms": "0", "end_ms": 5}],
                "intro: start_ms must be a whole number of milliseconds",
            ),
            (
                [{"type": "intro", "start_ms": True, "end_ms": 5}],
                "intro: start_ms must be a whole number of milliseconds",
            ),
        ],
    )
    def test_a_markers_list_it_cannot_save_from_is_refused(self, client, servers, known, episode, markers, expected):
        resp = _save(client, episode, markers)
        assert resp.status_code == 400
        assert resp.get_json()["error"] == expected

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            (-1, 5_000, "inside the file"),
            (DUR, DUR, "inside the file"),
            (0, DUR + 2_001, "inside the file"),
            (30_000, 30_000, "end after it starts"),
            (30_000, 29_999, "end after it starts"),
        ],
    )
    def test_the_two_bounds_a_user_marker_keeps(self, client, servers, known, episode, start, end, expected):
        """Ruling P-R2: inside the file, and ending after it starts -- those two, and no others."""
        resp = _save(client, episode, [{"type": "intro", "start_ms": start, "end_ms": end}])
        assert resp.status_code == 400
        assert expected in resp.get_json()["error"]

    def test_a_file_that_changed_on_disk_is_refused_before_anything_is_saved(self, client, servers, known, episode):
        """Its stored duration describes the old file, so the bounds would be checked against the wrong length."""
        with open(episode, "wb") as fh:
            fh.write(b"y" * 300)
        resp = _save(client, episode, [{"type": "intro", "start_ms": 0, "end_ms": 5_000}])
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == "file_changed"
        assert get_marker_store().get_markers(known.id) == {}

    @pytest.mark.parametrize(
        "entry",
        [
            {"type": "recap", "start_ms": 1_000, "end_ms": 20_000},
            {"type": "preview", "start_ms": 1_300_000, "end_ms": 1_310_000},
        ],
        ids=["recap", "preview"],
    )
    def test_a_type_no_enabled_owner_can_show_is_refused(self, client, only_plex_and_emby, known, episode, entry):
        resp = _save(client, episode, [entry])
        assert resp.status_code == 400
        assert entry["type"] in resp.get_json()["error"]
        assert get_marker_store().get_markers(known.id) == {}  # nothing was saved before the refusal

    def test_the_same_type_is_accepted_once_a_jellyfin_that_shows_it_is_on(
        self, client, servers, known, episode, published
    ):
        """The refusal is about the owners, not the type: Jellyfin shows recaps and previews."""
        resp = _save(client, episode, [{"type": "preview", "start_ms": 1_300_000, "end_ms": 1_310_000}])
        assert resp.status_code == 200

    def test_no_server_with_intro_and_credits_on_is_refused(self, app, client, media, known, episode):
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("media_servers", [_server("plex-1", "plex", media, markers=False)])
        resp = _save(client, episode, [{"type": "intro", "start_ms": 0, "end_ms": 5_000}])
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == "no_marker_owner"


class TestSaveAccepts:
    @pytest.mark.parametrize(
        ("markers", "kind"),
        [
            ([{"type": "intro", "start_ms": 0, "end_ms": 2_000}], "a 2 s intro (under the 3 s minimum)"),
            ([{"type": "intro", "start_ms": 900_000, "end_ms": 950_000}], "an intro past the first 35 %"),
            ([{"type": "credits", "start_ms": 10_000, "end_ms": 60_000}], "credits in the first minute"),
            ([{"type": "intro", "start_ms": 0, "end_ms": 400_000}], "an intro over the 300 s cap"),
        ],
    )
    def test_the_bounds_that_only_catch_a_wrong_source_do_not_apply_to_the_user(
        self, client, servers, known, episode, published, markers, kind
    ):
        resp = _save(client, episode, markers)
        assert resp.status_code == 200, kind
        assert resp.get_json()["markers"][markers[0]["type"]]["locked"] is True

    @pytest.mark.parametrize("end", [None, "missing"])
    def test_no_end_means_the_marker_runs_to_the_end_of_the_file(self, client, servers, known, episode, published, end):
        entry = {"type": "credits", "start_ms": 1_290_000}
        if end is None:
            entry["end_ms"] = None
        resp = _save(client, episode, [entry])
        assert resp.status_code == 200
        assert resp.get_json()["markers"]["credits"]["end_ms"] == DUR

    def test_an_end_a_couple_of_seconds_past_the_file_is_clamped_like_any_candidate(
        self, client, servers, known, episode, published
    ):
        resp = _save(client, episode, [{"type": "credits", "start_ms": 1_290_000, "end_ms": DUR + 2_000}])
        assert resp.status_code == 200
        assert resp.get_json()["markers"]["credits"]["end_ms"] == DUR


class TestSaveStoresAndPublishes:
    def test_the_save_is_stored_and_locked_before_any_server_is_asked(
        self, client, servers, known, episode, monkeypatch
    ):
        """P-R1: the request that publishes must not be able to lose the edit."""
        seen = {}

        def capture(path, **kwargs):
            store = get_marker_store()
            seen["locked"] = store.get_locked(known.id)
            seen["reason"] = store.get_decisions(known.id)[T.INTRO].reason
            return []

        monkeypatch.setattr(pipeline, "publish_now", capture)
        resp = _save(client, episode, [{"type": "intro", "start_ms": 5_000, "end_ms": 35_000}])
        assert resp.status_code == 200
        assert seen["locked"] == {T.INTRO: Marker(T.INTRO, 5_000, 35_000, ("user",), locked=True)}
        assert seen["reason"] == LOCKED_BY_USER

    def test_adjusting_is_locking(self, client, servers, known, episode, published):
        """P-R3: there is no adjusted-but-unlocked state."""
        _save(client, episode, [{"type": "intro", "start_ms": 5_000, "end_ms": 35_000}])
        store = get_marker_store()
        assert store.get_markers(known.id)[T.INTRO].locked is True
        assert store.get_decisions(known.id)[T.INTRO].status is DecisionStatus.DECIDED

    def test_the_publish_runs_on_a_registry_whose_servers_cannot_hold_the_request(
        self, client, servers, known, episode, published
    ):
        _save(client, episode, [{"type": "intro", "start_ms": 5_000, "end_ms": 35_000}])
        [call] = published.calls
        assert call["path"] == episode
        timeouts = {cfg.id: cfg.timeout for cfg in call["registry"].configs()}
        assert timeouts == {sid: pipeline.PUBLISH_NOW_SERVER_TIMEOUT_S for sid in ("plex-1", "jf-1", "emby-1")}

    def test_a_save_that_publishes_nothing_still_answers_200_with_the_stored_marker(
        self, client, servers, known, episode, published
    ):
        published.rows = [_row("plex-1", "plex", "failed", "Can't reach this server")]
        resp = _save(client, episode, [{"type": "intro", "start_ms": 5_000, "end_ms": 35_000}])
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["markers"]["intro"] == {
            "type": "intro",
            "start_ms": 5_000,
            "end_ms": 35_000,
            "locked": True,
            "locked_at": body["markers"]["intro"]["locked_at"],
        }
        assert body["markers"]["intro"]["locked_at"]
        assert body["servers"][0]["result"] == "failed"

    def test_the_editors_words_cover_every_publish_outcome_there_is(self):
        """A new ServerStatus with no entry would leak the job's own `markers_*` wording into the editor."""
        assert set(_EDITOR_RESULTS) == {s.value for s in ServerStatus}

    @pytest.mark.parametrize("status", list(ServerStatus), ids=lambda s: s.value)
    def test_every_publish_outcome_reaches_the_editor_in_its_own_words(
        self, client, servers, known, episode, published, status
    ):
        published.rows = [_row("plex-1", "plex", status.value, "why")]
        resp = _save(client, episode, [{"type": "intro", "start_ms": 5_000, "end_ms": 35_000}])
        assert resp.get_json()["servers"][0] == {
            "server_id": "plex-1",
            "server_name": "PLEX-1",
            "server_type": "plex",
            "result": _EDITOR_RESULTS[status.value],
            "message": "why",
            "can_show": ["intro", "credits"],
            "cant_show": [],
            "notes": [],
        }
        assert not _EDITOR_RESULTS[status.value].startswith("markers_")

    @pytest.mark.parametrize("mtype", ["recap", "preview"])
    @pytest.mark.parametrize(
        ("server_id", "server_type", "shows_it"),
        [("plex-1", "plex", False), ("emby-1", "emby", False), ("jf-1", "jellyfin", True)],
    )
    def test_a_type_a_server_cannot_show_is_named_per_server(
        self, client, servers, known, episode, published, server_id, server_type, shows_it, mtype
    ):
        """D8, first shape: recap and preview are type-level -- Plex and Emby simply don't have them."""
        published.rows = [_row(server_id, server_type, "markers_written", "1 marker(s)")]
        resp = _save(client, episode, [{"type": mtype, "start_ms": 1_000, "end_ms": 20_000}])
        assert resp.get_json()["servers"][0]["cant_show"] == ([] if shows_it else [mtype])

    def test_an_edited_credits_end_is_accepted_on_emby_with_the_note_saying_what_emby_does(
        self, client, servers, known, episode, published
    ):
        """D8, second shape: not a refusal -- published start-only, and the editor says so per field."""
        published.rows = [_row("emby-1", "emby", "markers_written", "1 marker(s)")]
        resp = _save(client, episode, [{"type": "credits", "start_ms": 1_200_000, "end_ms": 1_250_000}])
        row = resp.get_json()["servers"][0]
        assert row["result"] == "written"
        assert row["cant_show"] == []
        assert row["notes"] == [{"type": "credits", "field": "end", "note": CREDITS_BEFORE_END_NOTE}]

    def test_credits_that_run_to_the_end_carry_no_emby_note(self, client, servers, known, episode, published):
        published.rows = [_row("emby-1", "emby", "markers_written", "1 marker(s)")]
        resp = _save(client, episode, [{"type": "credits", "start_ms": 1_290_000, "end_ms": None}])
        assert resp.get_json()["servers"][0]["notes"] == []

    def test_only_emby_carries_the_credits_end_note(self, client, servers, known, episode, published):
        published.rows = [
            _row("plex-1", "plex", "markers_written", "1 marker(s)"),
            _row("jf-1", "jellyfin", "markers_written", "1 marker(s)"),
        ]
        resp = _save(client, episode, [{"type": "credits", "start_ms": 1_200_000, "end_ms": 1_250_000}])
        assert [r["notes"] for r in resp.get_json()["servers"]] == [[], []]

    def test_a_failure_message_never_carries_a_servers_credentials(self, client, servers, known, episode, published):
        published.rows = [_row("plex-1", "plex", "failed", f"401 for http://x/?X-Plex-Token={PLEX_TOKEN}")]
        resp = _save(client, episode, [{"type": "intro", "start_ms": 5_000, "end_ms": 35_000}])
        assert PLEX_TOKEN not in resp.get_data(as_text=True)


class TestUnlock:
    def _locked(self, client, episode, published):
        return _save(client, episode, [{"type": "intro", "start_ms": 5_000, "end_ms": 35_000}])

    def test_unlock_drops_the_lock_and_publishes_nothing(self, client, servers, known, episode, published):
        self._locked(client, episode, published)
        published.calls.clear()
        resp = client.delete(
            "/api/markers/item/markers", headers=_api_headers(), json={"path": episode, "types": ["intro"]}
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["unlocked"] == ["intro"]
        assert body["markers"] == {}
        assert body["decisions"]["intro"] == {"status": "needs_review", "reason": UNLOCKED_PENDING}
        assert published.calls == []  # the next run re-decides and re-publishes; this request contacts nobody
        assert get_marker_store().get_locked(known.id) == {}

    def test_unlocking_a_type_that_was_not_locked_changes_nothing(self, client, servers, known, episode, published):
        self._locked(client, episode, published)
        resp = client.delete(
            "/api/markers/item/markers", headers=_api_headers(), json={"path": episode, "types": ["credits"]}
        )
        assert resp.status_code == 200
        assert resp.get_json()["unlocked"] == []
        assert set(get_marker_store().get_locked(known.id)) == {T.INTRO}

    def test_unlocking_one_type_leaves_the_others_locked(self, client, servers, known, episode, published):
        _save(
            client,
            episode,
            [
                {"type": "intro", "start_ms": 5_000, "end_ms": 35_000},
                {"type": "credits", "start_ms": 1_290_000, "end_ms": None},
            ],
        )
        resp = client.delete(
            "/api/markers/item/markers", headers=_api_headers(), json={"path": episode, "types": ["intro"]}
        )
        assert resp.get_json()["markers"]["credits"]["locked"] is True
        assert set(get_marker_store().get_locked(known.id)) == {T.CREDITS}

    @pytest.mark.parametrize("types", [None, [], "intro", ["chapter"], [1]])
    def test_a_types_list_it_cannot_read_is_refused(self, client, servers, known, episode, types):
        body = {"path": episode} if types is None else {"path": episode, "types": types}
        resp = client.delete("/api/markers/item/markers", headers=_api_headers(), json=body)
        assert resp.status_code == 400
        assert "types must be a non-empty list" in resp.get_json()["error"]

    def test_unlocking_a_file_that_was_never_analysed_is_refused(self, client, servers, episode):
        resp = client.delete(
            "/api/markers/item/markers", headers=_api_headers(), json={"path": episode, "types": ["intro"]}
        )
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == "not_analysed"

    def test_a_path_outside_every_server_library_is_refused(self, client, servers, tmp_path):
        resp = client.delete(
            "/api/markers/item/markers",
            headers=_api_headers(),
            json={"path": str(tmp_path / "outside.mkv"), "types": ["intro"]},
        )
        assert resp.status_code == 400
