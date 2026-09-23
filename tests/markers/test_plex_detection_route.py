"""``POST /api/servers/<id>/plex-marker-detection``: Setup Health's per-library "Turn off" for Plex's own detection.

Plex's connection is mocked below ``PlexServer``, so each test drives the route through the real write and asserts
the exact request Plex would get: one library's ``/prefs``, only the prefs asked for, never the server-wide ones.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

INTRO = "enableIntroMarkerGeneration"
CREDITS = "enableCreditsMarkerGeneration"
URL = "/api/servers/plex-1/plex-marker-detection"


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


def test_needs_authentication(app, seed, conn):
    seed(_plex())

    resp = app.test_client().post(URL, json={"library_id": "2", "prefs": [INTRO]})

    assert resp.status_code == 401
    conn.query.assert_not_called()


@pytest.mark.parametrize(
    ("library_id", "prefs", "query"),
    [
        ("2", [INTRO, CREDITS], f"{INTRO}=0&{CREDITS}=0"),
        ("2", [INTRO], f"{INTRO}=0"),
        ("1", [CREDITS], f"{CREDITS}=0"),
    ],
)
def test_writes_exactly_the_listed_prefs_of_that_library(client, seed, conn, library_id, prefs, query):
    seed(_plex())

    resp = client.post(URL, json={"library_id": library_id, "prefs": prefs})

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
def test_rejects_a_pref_outside_the_two_or_a_malformed_body(client, seed, conn, body):
    seed(_plex())

    resp = client.post(URL, json=body)

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
def test_rejects_a_library_the_row_could_not_list(client, seed, conn, server, library_id):
    seed(server)

    resp = client.post(URL, json={"library_id": library_id, "prefs": [CREDITS]})

    assert resp.status_code == 400
    assert "isn't one Intro & Credits goes to" in resp.get_json()["error"]
    conn.query.assert_not_called()


def test_rejects_a_server_that_isnt_plex(client, seed, conn):
    seed(
        {
            "id": "plex-1",
            "type": "jellyfin",
            "name": "Jellyfin",
            "enabled": True,
            "url": "http://127.0.0.1:9",
            "auth": {"method": "api_key", "api_key": "k"},
            "libraries": [{"id": "2", "name": "TV Shows", "remote_paths": []}],
            "markers": {"enabled": True, "library_ids": None},
        }
    )

    resp = client.post(URL, json={"library_id": "2", "prefs": [CREDITS]})

    assert resp.status_code == 400
    conn.query.assert_not_called()


def test_unknown_server_is_404(client, seed, conn):
    seed(_plex())

    resp = client.post("/api/servers/nope/plex-marker-detection", json={"library_id": "2", "prefs": [CREDITS]})

    assert resp.status_code == 404
    conn.query.assert_not_called()


def test_plexs_refusal_is_reported_not_raised(client, seed, conn):
    seed(_plex())
    conn.query.side_effect = RuntimeError("(400) bad_request")

    resp = client.post(URL, json={"library_id": "2", "prefs": [CREDITS]})

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": False, "error": "(400) bad_request"}


def test_intro_on_a_movie_library_is_refused_by_plex_side_check(client, seed, conn):
    seed(_plex())

    resp = client.post(URL, json={"library_id": "1", "prefs": [INTRO]})

    assert resp.get_json() == {"ok": False, "error": "library 1 has no intro detection: only TV libraries do"}
    conn.query.assert_not_called()
