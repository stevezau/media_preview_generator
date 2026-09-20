"""The Plex marker agent itself: its guards, its refusals, and the app writing Plex through it.

The agent is a container the user runs beside Plex (``plex-marker-agent/``); it is not part of the web app, so it
lives outside the package and is imported from its folder.
"""

from __future__ import annotations

import sqlite3
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers import plex_db, plex_remote
from media_preview_generator.markers.publishers.base import Capability, PublishError, Shown
from media_preview_generator.markers.publishers.plex_db import PlexMarkerPublisher
from media_preview_generator.markers.publishers.plex_remote import RemotePlexDb
from media_preview_generator.markers.settings import ServerMarkersSettings
from media_preview_generator.servers.base import ServerConfig, ServerType
from tests.markers.test_plex_db_publisher import _make_db, _mountinfo

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "plex-marker-agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import plex_marker_agent  # noqa: E402

T = MarkerType
TOKEN = "lab-shared-key-0123456789"
DUR = 1_320_000
INTRO = Marker(T.INTRO, 11_000, 37_000, ("chapters",))
LOCKED_INTRO = Marker(T.INTRO, 20_000, 44_000, ("user",), locked=True)
CREDITS = Marker(T.CREDITS, 1_299_000, DUR, ("chapters",))
AUTH = {"Authorization": f"Bearer {TOKEN}", plex_remote.PROTOCOL_HEADER: str(plex_remote.AGENT_PROTOCOL)}


@pytest.fixture(autouse=True)
def plex_holds_the_database(monkeypatch):
    """Stand in for Plex having its database open through this very path (the agent's lock proof)."""
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)


@pytest.fixture
def plex_config(tmp_path):
    """A Plex config folder with a 1.43 database in it, as the agent's container would see it."""
    folder = tmp_path / "Plex Media Server"
    _make_db(folder, journal_mode="wal")
    return folder


@pytest.fixture
def agent(plex_config, tmp_path):
    """The agent, pointed at that folder, with everything on a local disk."""
    app = plex_marker_agent.create_app(config_dir=str(plex_config), token=TOKEN, mountinfo_path=_mountinfo(tmp_path))
    app.config["TESTING"] = True
    return app.test_client()


def _db_file(plex_config: Path) -> Path:
    return Path(plex_db.plex_db_path(str(plex_config)))


def _served(plex_config: Path, item: int = 7) -> list[tuple]:
    """What Plex would serve for the item: its marker rows, in stored times."""
    conn = sqlite3.connect(_db_file(plex_config))
    try:
        return conn.execute(
            "SELECT t.text, t.time_offset, t.end_time_offset FROM taggings t JOIN tags g ON g.id = t.tag_id "
            "WHERE t.metadata_item_id=? AND g.tag_type=12 AND g.tag='' ORDER BY t.text, t.time_offset",
            (item,),
        ).fetchall()
    finally:
        conn.close()


class TestItStartsOnlyWhenItIsSetUp:
    def test_no_shared_key_refuses_to_start(self, plex_config):
        with pytest.raises(SystemExit, match="AGENT_TOKEN"):
            plex_marker_agent.create_app(config_dir=str(plex_config), token="")

    def test_no_plex_config_folder_refuses_to_start(self):
        with pytest.raises(SystemExit, match="PLEX_CONFIG_DIR"):
            plex_marker_agent.create_app(config_dir="", token=TOKEN)

    def test_it_reads_both_from_the_environment(self, plex_config, monkeypatch):
        monkeypatch.setenv("PLEX_CONFIG_DIR", str(plex_config))
        monkeypatch.setenv("AGENT_TOKEN", TOKEN)
        assert plex_marker_agent.create_app() is not None


class TestTheGuards:
    """Nothing reaches the database without the key and a protocol both sides know."""

    ENDPOINTS = [
        ("post", "/v1/checks/file", {}),
        ("post", "/v1/checks/db", {}),
        ("post", "/v1/item/read", {"rating_key": 7}),
        ("post", "/v1/item/write", {"write": {}}),
        ("post", "/v1/items/shown", {"items": []}),
        ("post", "/v1/item/exists", {"rating_key": 7}),
        ("get", "/v1/ping", None),
    ]

    @pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer wrong-key"}, {"Authorization": TOKEN}, {"X-Auth-Token": TOKEN}],
        ids=["no-key", "wrong-key", "no-bearer-prefix", "another-header"],
    )
    def test_every_endpoint_needs_the_shared_key(self, agent, method, path, body, headers):
        send = getattr(agent, method)
        response = send(path, json=body, headers={**headers, plex_remote.PROTOCOL_HEADER: "1"})
        assert response.status_code == 401
        assert response.get_json()["error"] == {"kind": "auth"}

    @pytest.mark.parametrize(
        "offered",
        ["kéy", "ÿ" * 8, "key—dash", "\U0001f511"],
        ids=["latin-1-letter", "high-latin-1-bytes", "em-dash", "astral"],
    )
    def test_a_non_ascii_key_is_refused_rather_than_crashing(self, agent, offered):
        """werkzeug decodes headers latin-1 and ``compare_digest`` raises TypeError on a str above U+007F.

        Our own key is ASCII-validated before it is stored (``markers/settings.py``), so only a third party
        sends one of these -- and a 500 on an auth path tells that third party the endpoint exists.
        """
        response = agent.post(
            "/v1/checks/file",
            json={},
            headers={"Authorization": f"Bearer {offered}", plex_remote.PROTOCOL_HEADER: "1"},
        )
        assert response.status_code == 401
        assert response.get_json()["error"] == {"kind": "auth"}

    @pytest.mark.parametrize("token", ["kéy", "—", "k\U0001f511"], ids=["accent", "em-dash", "astral"])
    def test_an_agent_whose_own_key_is_non_ascii_refuses_to_start(self, tmp_path, token):
        """The other half of the pair. The app stores printable ASCII only (``markers/settings.py``), so such a
        key could never match one it sends: every request would 401 and look like a typo on the app's side."""
        with pytest.raises(SystemExit, match="ASCII"):
            plex_marker_agent.create_app(config_dir=str(tmp_path), token=token)

    def test_a_wrong_key_says_nothing_about_this_plex(self, agent):
        body = agent.post("/v1/checks/file", json={}, headers={"Authorization": "Bearer nope"}).get_data(as_text=True)
        assert "Plex Media Server" not in body and "db_path" not in body

    @pytest.mark.parametrize("asked", [None, "2", "0", "one", ""], ids=["none", "newer", "older", "text", "empty"])
    def test_a_protocol_this_build_doesnt_speak_is_refused_with_both_versions(self, agent, asked):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if asked is not None:
            headers[plex_remote.PROTOCOL_HEADER] = asked
        response = agent.post("/v1/item/read", json={"rating_key": 7}, headers=headers)
        assert response.status_code == 409
        message = response.get_json()["error"]["message"]
        assert plex_marker_agent.AGENT_VERSION in message and "protocol 1" in message

    @pytest.mark.parametrize("asked", [None, "99"], ids=["no-protocol-header", "another-protocol"])
    def test_the_ping_still_answers_a_caller_whose_protocol_it_doesnt_speak(self, agent, asked):
        # The README's curl command sends the key and nothing else: when the two sides disagree, "which version are
        # you?" is the question the operator needs answered.
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if asked is not None:
            headers[plex_remote.PROTOCOL_HEADER] = asked
        response = agent.get("/v1/ping", headers=headers)
        assert response.status_code == 200
        assert response.get_json()["agent"]["version"] == plex_marker_agent.AGENT_VERSION

    def test_the_ping_still_needs_the_key(self, agent):
        assert agent.get("/v1/ping", headers={"Authorization": "Bearer nope"}).status_code == 401

    def test_the_health_check_needs_nothing_and_says_nothing(self, agent):
        payload = agent.get("/v1/health").get_json()
        assert payload["ok"] is True
        assert payload["agent"] == {"version": plex_marker_agent.AGENT_VERSION, "protocols": [1]}
        assert "db" not in str(payload)

    def test_every_answer_carries_the_agents_version_so_skew_is_seen_on_the_first_call(self, agent):
        for response in (agent.get("/v1/ping", headers=AUTH), agent.post("/v1/checks/file", json={}, headers=AUTH)):
            assert response.get_json()["agent"] == {"version": plex_marker_agent.AGENT_VERSION, "protocols": [1]}

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/v1/item/read", {"rating_key": "7"}),
            ("/v1/item/read", {"rating_key": True}),
            ("/v1/item/exists", {}),
            ("/v1/item/write", {"write": {"rating_key": 7}}),
            ("/v1/items/shown", {"items": "all"}),
            ("/v1/items/shown", {"items": [{"rating_key": 7}]}),
            ("/v1/items/shown", {"items": [], "timeout_s": "soon"}),
        ],
    )
    def test_a_body_that_isnt_what_it_should_be_is_refused(self, agent, path, body):
        assert agent.post(path, json=body, headers=AUTH).status_code == 400

    def test_a_body_that_isnt_json_is_refused(self, agent):
        assert agent.post("/v1/item/read", data="[]", content_type="application/json", headers=AUTH).status_code == 400

    def test_a_huge_body_is_refused_before_it_is_parsed(self, agent):
        big = {"items": [{"rating_key": 7, "ours": [], "item_files": ["x" * 4096] * 1000}]}
        assert agent.post("/v1/items/shown", json=big, headers=AUTH).status_code in (400, 413)

    def test_more_items_than_one_read_back_may_carry_is_refused(self, agent):
        items = [{"rating_key": i, "ours": [], "kept_types": [], "item_files": None} for i in range(201)]
        assert agent.post("/v1/items/shown", json={"items": items}, headers=AUTH).status_code == 400


class TestItOwnsThePath:
    """The caller says what to write, never where."""

    def test_the_database_path_comes_from_the_container_not_the_request(self, agent, plex_config, tmp_path):
        elsewhere = tmp_path / "somebody-elses"
        _make_db(elsewhere, journal_mode="wal")
        body = {
            "rating_key": 7,
            "db_path": str(_db_file(elsewhere)),
            "config_dir": str(elsewhere),
            "plex_config_folder": str(elsewhere),
        }
        assert agent.post("/v1/item/read", json=body, headers=AUTH).status_code == 200
        # Proven by writing: the row lands in the agent's own database, never in the one the body named.
        write = _write_body(rating_key=7, wanted=[INTRO])
        assert agent.post("/v1/item/write", json={**write, **body}, headers=AUTH).status_code == 200
        assert _served(plex_config) == [("intro", 11_000, 37_000)]
        assert _served(elsewhere) == []

    def test_the_ping_says_whether_the_database_is_even_there(self, agent, plex_config):
        assert agent.get("/v1/ping", headers=AUTH).get_json()["result"]["db_present"] is True
        _db_file(plex_config).unlink()
        assert agent.get("/v1/ping", headers=AUTH).get_json()["result"]["db_present"] is False


def _write_body(*, rating_key: int, wanted: list[Marker], prior=(), keep_plex=False, kept_types=()) -> dict:
    from media_preview_generator.markers.publishers.plex_db import WriteRequest, _Part

    request = WriteRequest(
        rating_key=rating_key,
        parts=[_Part(1, 1, "/data/tv/S01E01.mkv", None, None)],
        wanted=list(wanted),
        prior=list(prior),
        duration_ms=DUR,
        own_prior=[],
        calling_part_ids=(1,),
        kept_types=frozenset(kept_types),
        keep_plex=keep_plex,
    )
    return {"deadline_s": 5.0, "write": plex_remote.write_request_to_json(request)}


class TestItRefusesWhatTheInProcessWriterRefuses:
    """The same guards, because it is the same code — never a second implementation."""

    def test_a_database_on_a_network_share_is_refused_here_too(self, plex_config, tmp_path):
        app = plex_marker_agent.create_app(
            config_dir=str(plex_config), token=TOKEN, mountinfo_path=_mountinfo(tmp_path, fs="nfs4")
        )
        report = app.test_client().post("/v1/checks/file", json={}, headers=AUTH).get_json()["result"]["report"]
        assert report["state"] == Capability.NEEDS_LOCAL_DB.value
        assert "network share" in report["message"]

    def test_plex_not_holding_the_database_is_refused_here_too(self, agent, monkeypatch):
        monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: False)
        report = agent.post("/v1/checks/file", json={}, headers=AUTH).get_json()["result"]["report"]
        assert report["state"] == Capability.UNREACHABLE.value

    def test_a_schema_it_doesnt_know_stops_the_write(self, agent, plex_config):
        conn = sqlite3.connect(_db_file(plex_config))
        conn.execute("CREATE TRIGGER t AFTER INSERT ON taggings BEGIN SELECT 1; END")
        conn.commit()
        conn.close()
        response = agent.post("/v1/item/write", json=_write_body(rating_key=7, wanted=[INTRO]), headers=AUTH)
        assert response.status_code == 409
        error = response.get_json()["error"]
        assert error["state"] == Capability.UNSUPPORTED_SCHEMA.value and "triggers" in error["message"]
        assert _served(plex_config) == []

    def test_a_plex_that_never_made_its_marker_tag_row_is_refused_and_none_is_created(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder, tag_row=None, journal_mode="wal")
        app = plex_marker_agent.create_app(config_dir=str(folder), token=TOKEN, mountinfo_path=_mountinfo(tmp_path))
        response = app.test_client().post(
            "/v1/item/write", json=_write_body(rating_key=7, wanted=[INTRO]), headers=AUTH
        )
        assert response.status_code == 409
        assert response.get_json()["error"]["state"] == Capability.NEEDS_PLEX_DETECTION_ONCE.value
        conn = sqlite3.connect(Path(plex_db.plex_db_path(str(folder))))
        try:
            assert conn.execute("SELECT COUNT(*) FROM tags WHERE tag_type=12").fetchone()[0] == 0
        finally:
            conn.close()

    def test_an_item_this_plex_doesnt_have_is_an_item_not_found(self, agent):
        response = agent.post("/v1/item/read", json={"rating_key": 999}, headers=AUTH)
        assert response.status_code == 200 and response.get_json()["result"]["item"]["exists"] is False


class _FlaskResponse:
    """A ``requests``-shaped view of a Flask test response."""

    def __init__(self, response) -> None:
        self.status_code = response.status_code
        self._response = response

    def json(self):
        return self._response.get_json()


class ClientSession:
    """A ``requests.Session`` that calls the agent in-process, as the app's HTTP client would over the LAN."""

    def __init__(self, client) -> None:
        self._client = client
        self.stopped = False

    def post(self, url, json=None, headers=None, timeout=None):
        if self.stopped:
            raise __import__("requests").ConnectionError("connection refused")
        return _FlaskResponse(self._client.post(urlsplit(url).path, json=json, headers=headers))


def _publisher_through(session, *, keep_plex=False, identity=None) -> PlexMarkerPublisher:
    """The app's Plex publisher with no access to the database at all — only the agent."""
    config = ServerConfig(
        id="plex-1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://plex:32400",
        auth={},
        # Deliberately a folder that doesn't exist here: this app cannot see Plex's database.
        output={"plex_config_folder": "/nowhere/plex"},
        path_mappings=[],
        server_identity=identity,
    )
    settings = ServerMarkersSettings(
        True,
        None,
        "2026-09-13T00:00:00+00:00",
        "keep_plex" if keep_plex else "restore",
        agent_enabled=True,
        agent_url="http://plex-host.lan:9494",
        agent_token=TOKEN,
    )
    server = MagicMock()
    server.get_server_status.return_value = {"plex_pass": True, "version": "1.43.4"}
    server.get_marker_detection_prefs.return_value = {"intro": "never", "credits": "never"}
    return PlexMarkerPublisher(
        server, config, settings, db=RemotePlexDb(settings.agent_url, settings.agent_token, session=session)
    )


class TestTheAppWritesPlexThroughTheAgent:
    """The whole path, both halves: nothing here opens the database except the agent."""

    def test_markers_are_written_read_back_and_cleaned_up(self, agent, plex_config):
        session = ClientSession(agent)
        publisher = _publisher_through(session)

        ours = publisher.write(
            "7", [INTRO, CREDITS], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
        )

        assert ours == [INTRO, CREDITS]
        assert publisher.last_write_changed is True
        assert _served(plex_config) == [("credits", 1_297_000, DUR), ("intro", 11_000, 37_000)]
        assert publisher.shows_many([("7", ours, frozenset(), publisher.last_item_files)]) == {"7": Shown.OURS}

        # Nothing decided any more: the rows this app left go, and nothing else is touched.
        assert publisher.write("7", [], previous=ours, duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv") == []
        assert _served(plex_config) == []

    def test_the_second_run_changes_nothing(self, agent, plex_config):
        publisher = _publisher_through(ClientSession(agent))
        publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        again = _publisher_through(ClientSession(agent))
        again.write("7", [INTRO], previous=[INTRO], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert again.last_write_changed is False
        assert _served(plex_config) == [("intro", 11_000, 37_000)]

    def test_a_locked_marker_overrides_keep_plexs(self, agent, plex_config):
        # Plex's own intro row is there and the server is set to keep Plex's; a marker the user locked still wins.
        conn = sqlite3.connect(_db_file(plex_config))
        conn.execute(
            "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, "
            "created_at, extra_data) VALUES (7, 563, 0, 'intro', 990, 29306, '', 1, '{\"pv:version\":\"5\"}')"
        )
        conn.commit()
        conn.close()

        publisher = _publisher_through(ClientSession(agent), keep_plex=True)
        ours = publisher.write(
            "7", [LOCKED_INTRO], previous=None, duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
        )

        assert ours == [LOCKED_INTRO]
        assert _served(plex_config) == [("intro", 20_000, 44_000)]
        assert publisher.last_replaced_own_types == frozenset({T.INTRO})
        assert publisher.last_kept_types == frozenset()

    def test_an_unlocked_marker_still_leaves_plexs_own_rows_alone(self, agent, plex_config):
        conn = sqlite3.connect(_db_file(plex_config))
        conn.execute(
            "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, "
            "created_at, extra_data) VALUES (7, 563, 0, 'intro', 990, 29306, '', 1, '{\"pv:version\":\"5\"}')"
        )
        conn.commit()
        conn.close()

        publisher = _publisher_through(ClientSession(agent), keep_plex=True)
        publisher.write("7", [INTRO], previous=None, duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")

        assert _served(plex_config) == [("intro", 990, 29306)]
        assert publisher.last_kept_types == frozenset({T.INTRO})

    def test_the_capability_is_ready_and_shows_the_agents_own_path(self, agent, plex_config):
        report = _publisher_through(ClientSession(agent)).capability()
        assert report.state is Capability.READY
        assert report.details["db_path"] == str(_db_file(plex_config))
        assert report.details["agent"]["state"] == "connected"
        assert report.details["agent"]["version"] == plex_marker_agent.AGENT_VERSION

    def test_an_agent_that_stops_mid_publish_fails_the_write_and_leaves_plex_untouched(self, agent, plex_config):
        session = ClientSession(agent)
        publisher = _publisher_through(session)
        publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        session.stopped = True

        with pytest.raises(PublishError) as ei:
            publisher.write("7", [CREDITS], previous=[INTRO], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")

        assert ei.value.state is Capability.AGENT_UNAVAILABLE
        assert "Can't reach the Plex marker agent" in str(ei.value)
        assert _served(plex_config) == [("intro", 11_000, 37_000)]  # exactly what the first run left

    def test_a_stopped_agent_makes_the_server_not_ready_rather_than_crashing(self, agent):
        session = ClientSession(agent)
        session.stopped = True
        report = _publisher_through(session).capability()
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert report.details["agent"]["state"] == "unreachable"

    def test_a_wrong_key_writes_nothing_and_says_which_side_to_fix(self, agent, plex_config):
        publisher = _publisher_through(ClientSession(agent))
        publisher._db = RemotePlexDb("http://plex-host.lan:9494", "not-the-key", session=ClientSession(agent))
        with pytest.raises(PublishError) as ei:
            publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert "refused this app's key" in str(ei.value)
        assert _served(plex_config) == []

    def test_an_item_missing_from_plex_is_reported_through_the_agent(self, agent):
        publisher = _publisher_through(ClientSession(agent))
        assert publisher.item_missing("7") is False
        assert publisher.item_missing("999") is True


class TestTheImageStaysInStepWithTheApp:
    def test_every_pinned_dependency_is_the_app_s_own(self):
        # The agent runs the app's code; a different Flask or requests here would be a second, untested environment.
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        app_deps = set(pyproject["project"]["dependencies"])
        lines = [
            line.strip()
            for line in (AGENT_DIR / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert lines, "the agent image must pin its dependencies"
        assert set(lines) <= app_deps

    def test_the_version_the_agent_reports_is_one_this_app_accepts(self):
        assert not plex_remote.agent_too_old(plex_marker_agent.AGENT_VERSION)
        assert plex_remote.AGENT_PROTOCOL in plex_marker_agent.PROTOCOLS


class TestWhichPlexTheAgentServes:
    """The agent names its Plex from Preferences.xml, so the app can refuse one set up beside another server."""

    def _preferences(self, plex_config: Path, identifier: str) -> None:
        (plex_config / "Preferences.xml").write_text(
            f'<?xml version="1.0" encoding="utf-8"?>\n<Preferences MachineIdentifier="uuid-form" '
            f'ProcessedMachineIdentifier="{identifier}" TranscoderTempDirectory="/transcode"/>\n'
        )

    def test_the_file_checks_carry_it(self, agent, plex_config):
        self._preferences(plex_config, "plex-aaa")
        result = agent.post("/v1/checks/file", json={}, headers=AUTH).get_json()["result"]
        assert result["machine_identifier"] == "plex-aaa"

    def test_no_preferences_file_is_unknown_not_a_mismatch(self, agent):
        assert agent.post("/v1/checks/file", json={}, headers=AUTH).get_json()["result"]["machine_identifier"] == ""

    def test_the_app_writes_through_an_agent_beside_the_right_plex(self, agent, plex_config):
        self._preferences(plex_config, "plex-aaa")
        publisher = _publisher_through(ClientSession(agent), identity="plex-aaa")
        publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert _served(plex_config) == [("intro", 11_000, 37_000)]

    def test_the_app_refuses_an_agent_beside_another_plex_before_it_writes(self, agent, plex_config):
        self._preferences(plex_config, "plex-bbb")
        publisher = _publisher_through(ClientSession(agent), identity="plex-aaa")
        with pytest.raises(PublishError) as ei:
            publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert "different Plex server" in str(ei.value)
        assert _served(plex_config) == []
        assert publisher.capability().state is Capability.AGENT_UNAVAILABLE
