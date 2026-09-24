"""Setup Health's two fixes for Plex's own intro/credits settings.

* ``POST /api/servers/<id>/plex-library-markers``: "Turn on" — one library's own "Intro markers" / "Credits markers"
  setting back on. While one is off, Plex hides every skip marker of that type in the library, ours included.
* ``POST /api/servers/<id>/plex-marker-detection``: "Set server-wide to Never" — Plex's server-wide detection prefs,
  which stop its own detection without hiding anything.

Plex's connection is mocked below ``PlexServer``, so each test drives the route through the real write and asserts
the exact request Plex would get.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

INTRO = "enableIntroMarkerGeneration"
CREDITS = "enableCreditsMarkerGeneration"
LIBRARY_URL = "/api/servers/plex-1/plex-library-markers"
NEVER_URL = "/api/servers/plex-1/plex-marker-detection"


def _plex(library_ids: list[str] | None = None, enabled: bool = True) -> dict:
    return {
        "id": "plex-1",
        "type": "plex",
        "name": "Plex",
        "enabled": True,
        "url": "http://127.0.0.1:9",
        "auth": {"method": "token", "token": "plex-token"},
        "libraries": [
            {"id": "1", "name": "Movies", "kind": "movie", "remote_paths": ["/data/movies"]},
            {"id": "2", "name": "TV Shows", "kind": "episode", "remote_paths": ["/data/tv"]},
        ],
        "output": {"plex_config_folder": "/plex"},
        "markers": {
            "enabled": enabled,
            "library_ids": library_ids,
            "plex": {"db_write_confirmed_at": "2026-09-01T00:00:00+00:00", "on_plex_redetect": "restore"},
        },
    }


JELLYFIN = {
    "id": "plex-1",
    "type": "jellyfin",
    "name": "Jellyfin",
    "enabled": True,
    "url": "http://127.0.0.1:9",
    "auth": {"method": "api_key", "api_key": "k"},
    "libraries": [{"id": "2", "name": "TV Shows", "remote_paths": []}],
    "markers": {"enabled": True, "library_ids": None},
}


@pytest.fixture
def seed(app):
    from media_preview_generator.web.settings_manager import get_settings_manager

    def _seed(*servers: dict) -> None:
        get_settings_manager().set("media_servers", list(servers))

    return _seed


@pytest.fixture
def conn():
    """The Plex connection every ``PlexServer`` the route builds gets: a movie library 1 and a TV library 2."""
    connection = MagicMock()
    connection.library.sections.return_value = [
        MagicMock(key="1", type="movie", title="Movies"),
        MagicMock(key="2", type="show", title="TV Shows"),
    ]
    with patch("media_preview_generator.servers.plex.PlexServer._connect", return_value=connection):
        yield connection


@pytest.mark.parametrize(
    ("url", "body"), [(LIBRARY_URL, {"library_id": "2", "prefs": [INTRO]}), (NEVER_URL, {"types": ["intro"]})]
)
def test_needs_authentication(app, seed, conn, url, body):
    seed(_plex())

    resp = app.test_client().post(url, json=body)

    assert resp.status_code == 401
    conn.query.assert_not_called()


class TestTurnOn:
    @pytest.mark.parametrize(
        ("library_id", "prefs", "query"),
        [
            ("2", [INTRO, CREDITS], f"{INTRO}=1&{CREDITS}=1"),
            ("2", [INTRO], f"{INTRO}=1"),
            ("1", [CREDITS], f"{CREDITS}=1"),
        ],
    )
    def test_writes_exactly_the_listed_prefs_of_that_library(self, client, seed, conn, library_id, prefs, query):
        seed(_plex())

        resp = client.post(LIBRARY_URL, json={"library_id": library_id, "prefs": prefs})

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "library_id": library_id, "prefs": prefs}
        conn.query.assert_called_once_with(f"/library/sections/{library_id}/prefs?{query}", method=conn._session.put)

    @pytest.mark.parametrize(
        "body",
        [
            {"library_id": "2", "prefs": ["GenerateIntroMarkerBehavior"]},
            {"library_id": "2", "prefs": ["enableBIFGeneration"]},
            {"library_id": "2", "prefs": [INTRO, "GenerateCreditsMarkerBehavior"]},
            {"library_id": "2", "prefs": []},
            {"library_id": "2", "prefs": INTRO},
            {"library_id": "2"},
            {"library_id": 2, "prefs": [INTRO]},
            {"prefs": [INTRO]},
        ],
        ids=[
            "server-intro-pref",
            "bif-pref",
            "one-allowed-one-not",
            "no-prefs",
            "prefs-not-a-list",
            "prefs-missing",
            "library-not-a-string",
            "library-missing",
        ],
    )
    def test_rejects_a_pref_outside_the_two_or_a_malformed_body(self, client, seed, conn, body):
        seed(_plex())

        resp = client.post(LIBRARY_URL, json=body)

        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False
        conn.query.assert_not_called()

    @pytest.mark.parametrize(
        ("server", "library_id"),
        [
            (_plex(), "7"),
            (_plex(library_ids=["1"]), "2"),
            (_plex(enabled=False), "2"),
        ],
        ids=["no-such-library", "outside-the-markers-selection", "intro-and-credits-off"],
    )
    def test_rejects_a_library_the_row_could_not_list(self, client, seed, conn, server, library_id):
        seed(server)

        resp = client.post(LIBRARY_URL, json={"library_id": library_id, "prefs": [CREDITS]})

        assert resp.status_code == 400
        assert "isn't one Intro & Credits goes to" in resp.get_json()["error"]
        conn.query.assert_not_called()

    @pytest.mark.parametrize(
        ("server", "status", "error"),
        [
            (JELLYFIN, 400, "Plex's marker settings are Plex settings"),
            ({**_plex(), "enabled": False}, 409, "is disabled"),
        ],
        ids=["not-plex", "disabled-server"],
    )
    def test_rejects_a_server_that_isnt_plex_or_is_disabled(self, client, seed, conn, server, status, error):
        seed(server)

        resp = client.post(LIBRARY_URL, json={"library_id": "2", "prefs": [CREDITS]})

        assert resp.status_code == status
        assert error in resp.get_json()["error"]
        conn.query.assert_not_called()

    def test_unknown_server_is_404(self, client, seed, conn):
        seed(_plex())

        resp = client.post("/api/servers/nope/plex-library-markers", json={"library_id": "2", "prefs": [CREDITS]})

        assert resp.status_code == 404
        conn.query.assert_not_called()

    def test_plexs_refusal_is_reported_not_raised(self, client, seed, conn):
        seed(_plex())
        conn.query.side_effect = RuntimeError("(400) bad_request")

        resp = client.post(LIBRARY_URL, json={"library_id": "2", "prefs": [CREDITS]})

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": False, "error": "(400) bad_request"}

    def test_intro_on_a_movie_library_is_refused_by_plex_side_check(self, client, seed, conn):
        seed(_plex())

        resp = client.post(LIBRARY_URL, json={"library_id": "1", "prefs": [INTRO]})

        assert resp.get_json() == {"ok": False, "error": "library 1 has no intro detection: only TV libraries do"}
        conn.query.assert_not_called()


class TestServerWideNever:
    @pytest.mark.parametrize(
        ("types", "query"),
        [
            (["intro", "credits"], "GenerateIntroMarkerBehavior=never&GenerateCreditsMarkerBehavior=never"),
            (["credits"], "GenerateCreditsMarkerBehavior=never"),
            (["intro"], "GenerateIntroMarkerBehavior=never"),
        ],
        ids=["both", "credits-only", "intro-only"],
    )
    def test_puts_exactly_the_types_in_scope_to_never(self, client, seed, conn, types, query):
        seed(_plex())

        resp = client.post(NEVER_URL, json={"types": types})

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "types": types}
        conn.query.assert_called_once_with(f"/:/prefs?{query}", method=conn._session.put)

    @pytest.mark.parametrize(
        "body",
        [{"types": []}, {"types": "intro"}, {"types": ["recap"]}, {"types": ["intro", 1]}, {}, {"library_id": "2"}],
        ids=["no-types", "not-a-list", "unknown-type", "not-text", "empty", "the-turn-on-body"],
    )
    def test_rejects_a_malformed_body(self, client, seed, conn, body):
        seed(_plex())

        resp = client.post(NEVER_URL, json=body)

        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False
        conn.query.assert_not_called()

    @pytest.mark.parametrize(
        ("server", "status", "error"),
        [
            (JELLYFIN, 400, "Plex's marker settings are Plex settings"),
            ({**_plex(), "enabled": False}, 409, "is disabled"),
            (_plex(enabled=False), 400, "Intro & Credits is off for this server"),
        ],
        ids=["not-plex", "disabled-server", "intro-and-credits-off"],
    )
    def test_rejects_a_server_the_row_could_not_show_for(self, client, seed, conn, server, status, error):
        seed(server)

        resp = client.post(NEVER_URL, json={"types": ["intro", "credits"]})

        assert resp.status_code == status
        assert error in resp.get_json()["error"]
        conn.query.assert_not_called()

    def test_unknown_server_is_404(self, client, seed, conn):
        seed(_plex())

        resp = client.post("/api/servers/nope/plex-marker-detection", json={"types": ["intro"]})

        assert resp.status_code == 404
        conn.query.assert_not_called()

    def test_plexs_refusal_is_reported_not_raised(self, client, seed, conn):
        seed(_plex())
        conn.query.side_effect = RuntimeError("(401) unauthorized")

        resp = client.post(NEVER_URL, json={"types": ["credits"]})

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": False, "error": "(401) unauthorized"}
