"""Round-trip of markers settings through the settings and servers APIs."""

import pytest

from media_preview_generator.markers.settings import SECRET_MASK


def test_settings_get_masks_theintrodb_key(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set(
        "markers",
        {
            "detect": {"intro": True, "credits": True, "recap": False},
            "publish_when": "high",
            "respect_locks": True,
            "sources": [{"id": "theintrodb", "enabled": True, "api_key": "secret-abc"}],
        },
    )
    body = client.get("/api/settings").get_json()
    tidb = next(s for s in body["markers"]["sources"] if s["id"] == "theintrodb")
    assert tidb["api_key"] == SECRET_MASK
    assert "secret-abc" not in client.get("/api/settings").get_data(as_text=True)


def test_settings_post_masked_key_keeps_stored_key(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("markers", {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "k1"}]})
    resp = client.post(
        "/api/settings",
        json={
            "markers": {
                "publish_when": "medium",
                "sources": [{"id": "theintrodb", "enabled": True, "api_key": SECRET_MASK}],
            }
        },
    )
    assert resp.status_code == 200
    stored = get_settings_manager().get("markers")
    assert stored["publish_when"] == "medium"
    assert next(s for s in stored["sources"] if s["id"] == "theintrodb")["api_key"] == "k1"


def test_settings_post_without_sources_keeps_stored_key(client):
    """A save that never mentions ``sources`` at all must not drop the stored TheIntroDB key."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("markers", {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "k1"}]})
    resp = client.post("/api/settings", json={"markers": {"publish_when": "medium"}})
    assert resp.status_code == 200
    stored = get_settings_manager().get("markers")
    assert stored["publish_when"] == "medium"
    assert next(s for s in stored["sources"] if s["id"] == "theintrodb")["api_key"] == "k1"


def test_settings_post_invalid_markers_is_400(client):
    resp = client.post("/api/settings", json={"markers": {"publish_when": "sometimes"}})
    assert resp.status_code == 400
    assert "publish_when" in resp.get_json()["error"]


def _add_server(client, server_type, **extra):
    from media_preview_generator.web.settings_manager import get_settings_manager

    entry = {
        "id": f"{server_type}-1",
        "type": server_type,
        "name": server_type,
        "enabled": True,
        "url": "http://x:1",
        "auth": {},
        "libraries": [],
        "path_mappings": [],
        "exclude_paths": [],
        "output": {"plex_config_folder": "/tmp"} if server_type == "plex" else {},
        **extra,
    }
    get_settings_manager().set("media_servers", [entry])
    return entry["id"]


@pytest.mark.parametrize("server_type", ["plex", "emby", "jellyfin"])
def test_server_save_without_markers_key_carries_block_forward(client, server_type):
    from media_preview_generator.web.settings_manager import get_settings_manager

    block = {"enabled": False, "library_ids": ["7"]}
    if server_type == "plex":
        block["plex"] = {"db_write_confirmed_at": None, "on_plex_redetect": "keep_plex"}
    sid = _add_server(client, server_type, markers=block)
    resp = client.put(f"/api/servers/{sid}", json={"name": "renamed"})
    assert resp.status_code == 200, resp.get_json()
    assert get_settings_manager().get("media_servers")[0]["markers"] == block
    assert resp.get_json()["markers"] == block


def test_plex_enable_without_confirmation_is_400(client):
    sid = _add_server(client, "plex")
    resp = client.put(f"/api/servers/{sid}", json={"markers": {"enabled": True}})
    assert resp.status_code == 400
    assert "Confirm" in resp.get_json()["error"]


def test_jellyfin_enable_saves(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    sid = _add_server(client, "jellyfin")
    resp = client.put(f"/api/servers/{sid}", json={"markers": {"enabled": True, "library_ids": None}})
    assert resp.status_code == 200
    assert get_settings_manager().get("media_servers")[0]["markers"] == {"enabled": True, "library_ids": None}


def test_plex_enabled_confirmed_carries_forward_without_markers_key(client):
    """LOW regression: the carry-forward path must also work for an enabled, confirmed Plex block
    (the existing carry-forward test only covered a disabled block)."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    block = {
        "enabled": True,
        "library_ids": ["7"],
        "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"},
    }
    sid = _add_server(client, "plex", markers=block)
    resp = client.put(f"/api/servers/{sid}", json={"name": "renamed"})
    assert resp.status_code == 200, resp.get_json()
    assert get_settings_manager().get("media_servers")[0]["markers"] == block
    assert resp.get_json()["markers"] == block


def test_partial_plex_disable_keeps_confirmation_and_library_choice(client):
    """MED regression: PUT {"markers": {"enabled": false}} must not wipe db_write_confirmed_at,
    library_ids, or on_plex_redetect just because the client only meant to flip `enabled`.
    Re-enabling afterwards must not re-require confirmation, since it was never actually lost.
    """
    from media_preview_generator.web.settings_manager import get_settings_manager

    confirmed_block = {
        "enabled": True,
        "library_ids": ["7"],
        "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "keep_plex"},
    }
    sid = _add_server(client, "plex", markers=confirmed_block)

    resp = client.put(f"/api/servers/{sid}", json={"markers": {"enabled": False}})
    assert resp.status_code == 200, resp.get_json()
    stored = get_settings_manager().get("media_servers")[0]["markers"]
    assert stored["enabled"] is False
    assert stored["library_ids"] == ["7"]
    assert stored["plex"] == {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "keep_plex"}

    resp = client.put(f"/api/servers/{sid}", json={"markers": {"enabled": True}})
    assert resp.status_code == 200, resp.get_json()
    assert get_settings_manager().get("media_servers")[0]["markers"]["enabled"] is True


def test_partial_plex_update_explicit_null_library_ids_clears_selection(client):
    """An explicit `library_ids: null` in the posted markers block still wins over the stored
    selection — "all libraries" is a real choice, not an omission."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    confirmed_block = {
        "enabled": True,
        "library_ids": ["7"],
        "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"},
    }
    sid = _add_server(client, "plex", markers=confirmed_block)

    resp = client.put(f"/api/servers/{sid}", json={"markers": {"library_ids": None}})
    assert resp.status_code == 200, resp.get_json()
    stored = get_settings_manager().get("media_servers")[0]["markers"]
    assert stored["library_ids"] is None
    assert stored["plex"]["db_write_confirmed_at"] == "2026-09-13T00:00:00+00:00"


def test_partial_jellyfin_update_keeps_library_ids(client):
    """A partial PUT that only mentions `enabled` must keep the stored `library_ids` (non-Plex
    servers have no `plex` sub-block to worry about, but the same merge applies)."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    sid = _add_server(client, "jellyfin", markers={"enabled": True, "library_ids": ["3", "4"]})

    resp = client.put(f"/api/servers/{sid}", json={"markers": {"enabled": False}})
    assert resp.status_code == 200, resp.get_json()
    assert get_settings_manager().get("media_servers")[0]["markers"] == {"enabled": False, "library_ids": ["3", "4"]}


def test_partial_plex_update_non_dict_plex_is_400(client):
    """MED regression: a non-dict posted `plex` (e.g. a plain string) must 400 through
    validate_server's own "markers.plex must be an object" check, not crash the merge helper with
    a TypeError trying to spread a non-mapping."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    confirmed_block = {
        "enabled": True,
        "library_ids": ["7"],
        "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"},
    }
    sid = _add_server(client, "plex", markers=confirmed_block)

    resp = client.put(f"/api/servers/{sid}", json={"markers": {"plex": "x"}})
    assert resp.status_code == 400
    assert "markers.plex" in resp.get_json()["error"]
    assert get_settings_manager().get("media_servers")[0]["markers"] == confirmed_block


def test_partial_plex_update_explicit_null_plex_clears_confirmation(client):
    """MED regression: an explicit `"plex": null` must win over the stored `plex` dict (clearing
    the confirmation), not be silently ignored by the merge helper."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    confirmed_block = {
        "enabled": True,
        "library_ids": ["7"],
        "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"},
    }
    sid = _add_server(client, "plex", markers=confirmed_block)

    resp = client.put(f"/api/servers/{sid}", json={"markers": {"enabled": False, "plex": None}})
    assert resp.status_code == 200, resp.get_json()
    stored = get_settings_manager().get("media_servers")[0]["markers"]
    assert stored["plex"]["db_write_confirmed_at"] is None


def test_partial_plex_clear_confirmation_alone_while_enabled_is_400(client):
    """LOW: clearing db_write_confirmed_at without also disabling is refused (safe — it would
    otherwise leave the server enabled with no confirmation on record) — and nothing is saved."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    confirmed_block = {
        "enabled": True,
        "library_ids": ["7"],
        "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"},
    }
    sid = _add_server(client, "plex", markers=confirmed_block)

    resp = client.put(f"/api/servers/{sid}", json={"markers": {"plex": {"db_write_confirmed_at": None}}})
    assert resp.status_code == 400
    assert "Confirm" in resp.get_json()["error"]
    assert get_settings_manager().get("media_servers")[0]["markers"] == confirmed_block


def test_partial_plex_clear_confirmation_with_disable_succeeds(client):
    """LOW: the same confirmation-clearing edit succeeds once paired with `enabled: false`."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    confirmed_block = {
        "enabled": True,
        "library_ids": ["7"],
        "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"},
    }
    sid = _add_server(client, "plex", markers=confirmed_block)

    resp = client.put(
        f"/api/servers/{sid}",
        json={"markers": {"enabled": False, "plex": {"db_write_confirmed_at": None}}},
    )
    assert resp.status_code == 200, resp.get_json()
    stored = get_settings_manager().get("media_servers")[0]["markers"]
    assert stored["enabled"] is False
    assert stored["plex"]["db_write_confirmed_at"] is None
