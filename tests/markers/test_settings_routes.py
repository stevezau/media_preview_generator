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


STORED_GLOBAL = {
    "detect": {"intro": True, "credits": False, "recap": True},
    "publish_when": "high",
    "respect_locks": False,
    "sources": [
        {"id": "skipdb", "enabled": False},
        {"id": "chapters", "enabled": True},
        {"id": "theintrodb", "enabled": True, "api_key": "k1"},
        {"id": "introdb", "enabled": True},
        {"id": "season_audio", "enabled": False},
        {"id": "credits_text", "enabled": True},
        {"id": "server_markers", "enabled": True},
    ],
}


def _post_markers(client, posted, stored=STORED_GLOBAL):
    import copy

    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("markers", copy.deepcopy(stored))
    resp = client.post("/api/settings", json={"markers": posted})
    assert resp.status_code == 200, resp.get_json()
    return get_settings_manager().get("markers")


def test_settings_post_partial_markers_block_keeps_everything_it_doesnt_name(client):
    # A partial save must not reset detection, locks and the source order (which would also re-decide every file).
    assert _post_markers(client, {"publish_when": "medium"}) == {**STORED_GLOBAL, "publish_when": "medium"}


def test_settings_post_partial_detect_keeps_the_other_types(client):
    stored = _post_markers(client, {"detect": {"credits": True}})
    assert stored["detect"] == {"intro": True, "credits": True, "recap": True}
    assert stored["sources"] == STORED_GLOBAL["sources"] and stored["respect_locks"] is False


def test_settings_post_some_sources_updates_them_in_place(client):
    stored = _post_markers(
        client, {"sources": [{"id": "season_audio", "enabled": True}, {"id": "theintrodb", "enabled": False}]}
    )
    expected = [dict(s) for s in STORED_GLOBAL["sources"]]
    expected[4]["enabled"] = True
    expected[2]["enabled"] = False  # the stored key stays
    assert stored["sources"] == expected


def test_settings_post_every_source_sets_their_order(client):
    order = ["chapters", "introdb", "skipdb", "theintrodb", "season_audio", "credits_text", "server_markers"]
    # skipdb and season_audio (stored off) are sent without "enabled": reordering alone doesn't switch them on.
    posted = [{"id": sid} if sid in ("skipdb", "season_audio") else {"id": sid, "enabled": True} for sid in order]
    stored = _post_markers(client, {"sources": posted})
    assert [(s["id"], s["enabled"]) for s in stored["sources"]] == [
        ("chapters", True),
        ("introdb", True),
        ("skipdb", False),
        ("theintrodb", True),
        ("season_audio", False),
        ("credits_text", True),
        ("server_markers", True),
    ]
    assert next(s for s in stored["sources"] if s["id"] == "theintrodb")["api_key"] == "k1"


@pytest.mark.parametrize(
    "stored",
    [None, "garbage", {"publish_when": "high", "detect": {"credits": False}, "sources": [{"id": "bogus"}]}],
    ids=["missing", "non-object", "hand-edited-invalid"],
)
def test_settings_post_partial_block_over_nothing_usable_stored_fills_in_defaults(client, stored):
    # An invalid stored block reads as the defaults (load_global), so the save merges over those and heals it.
    from media_preview_generator.markers.settings import DEFAULT_GLOBAL_MARKERS

    assert _post_markers(client, {"publish_when": "medium"}, stored=stored) == {
        **DEFAULT_GLOBAL_MARKERS,
        "publish_when": "medium",
    }


@pytest.mark.parametrize(
    "posted",
    [
        {"sources": [{"id": "skipdb", "enabled": True}, "chapters"]},
        {"sources": [{"id": "nope", "enabled": True}]},
        {"sources": {"id": "skipdb"}},
        {"detect": ["intro"]},
    ],
    ids=["non-object-entry", "unknown-id", "sources-not-a-list", "detect-not-an-object"],
)
def test_settings_post_malformed_partial_block_is_still_400(client, posted):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("markers", STORED_GLOBAL)
    resp = client.post("/api/settings", json={"markers": posted})
    assert resp.status_code == 400
    assert get_settings_manager().get("markers") == STORED_GLOBAL


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


@pytest.mark.parametrize(
    ("server_type", "stored"),
    [
        ("plex", {"enabled": False, "library_ids": None, "plex": {"on_plex_redetect": "Restore"}}),
        ("plex", {"enabled": True, "library_ids": None}),  # hand-enabled without the DB-write confirmation
        ("jellyfin", {"enabled": True, "library_ids": "all"}),
    ],
    ids=["plex-bad-redetect", "plex-unconfirmed", "jellyfin-bad-library-ids"],
)
def test_server_save_without_markers_key_keeps_a_stored_block_that_no_longer_validates(client, server_type, stored):
    # A client that doesn't send markers (a script; the Edit dialog always sends them) must not be blocked from
    # URL/auth/library edits by a hand-edited block.
    from media_preview_generator.web.settings_manager import get_settings_manager

    sid = _add_server(client, server_type, markers=stored)
    resp = client.put(f"/api/servers/{sid}", json={"name": "renamed"})
    assert resp.status_code == 200, resp.get_json()
    saved = get_settings_manager().get("media_servers")[0]
    assert saved["name"] == "renamed"
    assert saved["markers"] == stored


@pytest.mark.parametrize(("server_type", "stored"), [("plex", True), ("jellyfin", "on"), ("emby", None)])
def test_server_save_without_markers_key_replaces_a_missing_or_non_object_block_with_defaults(
    client, server_type, stored
):
    from media_preview_generator.markers.settings import default_server_markers
    from media_preview_generator.web.settings_manager import get_settings_manager

    sid = _add_server(client, server_type, markers=stored)
    resp = client.put(f"/api/servers/{sid}", json={"name": "renamed"})
    assert resp.status_code == 200, resp.get_json()
    assert get_settings_manager().get("media_servers")[0]["markers"] == default_server_markers(server_type)


def test_emby_block_is_merged_and_validated(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    stored = {"enabled": False, "library_ids": ["7"], "emby": {"on_emby_redetect": "keep_emby"}}
    sid = _add_server(client, "emby", markers=stored)
    resp = client.put(f"/api/servers/{sid}", json={"markers": {"enabled": True}})
    assert resp.status_code == 200, resp.get_json()
    saved = get_settings_manager().get("media_servers")[0]["markers"]
    assert saved == {"enabled": True, "library_ids": ["7"], "emby": {"on_emby_redetect": "keep_emby"}}
    resp = client.put(f"/api/servers/{sid}", json={"markers": {"emby": {"on_emby_redetect": "restore"}}})
    assert resp.status_code == 200, resp.get_json()
    assert get_settings_manager().get("media_servers")[0]["markers"]["emby"] == {"on_emby_redetect": "restore"}
    resp = client.put(f"/api/servers/{sid}", json={"markers": {"emby": {"on_emby_redetect": "sometimes"}}})
    assert resp.status_code == 400 and "on_emby_redetect" in resp.get_json()["error"]


def test_server_save_with_markers_still_validates_the_merged_block(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    stored = {"enabled": False, "library_ids": None, "plex": {"on_plex_redetect": "Restore"}}
    sid = _add_server(client, "plex", markers=stored)
    resp = client.put(f"/api/servers/{sid}", json={"markers": {"library_ids": ["1"]}})
    assert resp.status_code == 400
    assert "on_plex_redetect" in resp.get_json()["error"]
    assert get_settings_manager().get("media_servers")[0]["markers"] == stored


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


@pytest.mark.parametrize(
    "posted_sources",
    [
        [
            {"id": sid, "enabled": True, **({"api_key": ""} if sid == "theintrodb" else {})}
            for sid in ("skipdb", "chapters", "theintrodb", "introdb", "season_audio", "credits_text", "server_markers")
        ],
        [{"id": "theintrodb", "api_key": ""}],
    ],
    ids=["full-list", "partial-list"],
)
def test_settings_post_empty_api_key_clears_the_stored_key(client, posted_sources):
    # "Clear key" in Settings sends "": the merge must not fill the stored key back in.
    stored = _post_markers(client, {"sources": posted_sources})
    assert next(s for s in stored["sources"] if s["id"] == "theintrodb")["api_key"] == ""
    tidb = next(s for s in client.get("/api/settings").get_json()["markers"]["sources"] if s["id"] == "theintrodb")
    assert tidb["api_key"] == ""


def test_settings_post_a_partial_list_naming_a_source_twice_is_400(client):
    # Merged by id, the second entry would silently win; the same duplicate in a full list is already a 400.
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("markers", STORED_GLOBAL)
    posted = {"sources": [{"id": "skipdb", "enabled": True}, {"id": "skipdb", "enabled": False}]}
    resp = client.post("/api/settings", json={"markers": posted})
    assert resp.status_code == 400
    assert get_settings_manager().get("markers") == STORED_GLOBAL


_CONFIRMED_PLEX = {
    "enabled": True,
    "library_ids": ["7"],
    "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "keep_plex"},
}


def test_server_save_with_markers_null_resets_the_block_to_defaults(client):
    # An explicit null is "no block": Intro & Credits off and the confirmation dropped, like a server never set up.
    from media_preview_generator.markers.settings import default_server_markers
    from media_preview_generator.web.settings_manager import get_settings_manager

    sid = _add_server(client, "plex", markers=_CONFIRMED_PLEX)
    resp = client.put(f"/api/servers/{sid}", json={"markers": None})
    assert resp.status_code == 200, resp.get_json()
    assert get_settings_manager().get("media_servers")[0]["markers"] == default_server_markers("plex")


def test_server_save_with_a_non_object_markers_is_400_and_keeps_the_block(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    sid = _add_server(client, "plex", markers=_CONFIRMED_PLEX)
    resp = client.put(f"/api/servers/{sid}", json={"markers": "on"})
    assert (resp.status_code, resp.get_json()) == (400, {"error": "markers must be an object"})
    assert get_settings_manager().get("media_servers")[0]["markers"] == _CONFIRMED_PLEX


@pytest.mark.parametrize(
    ("server_type", "markers", "status"),
    [
        ("plex", {"enabled": True}, 400),
        ("plex", None, 201),
        ("jellyfin", {"enabled": True, "library_ids": None}, 201),
        ("emby", {"enabled": True, "library_ids": ["3"], "emby": {"on_emby_redetect": "keep_emby"}}, 201),
    ],
    ids=["plex-unconfirmed", "plex-no-block", "jellyfin-on", "emby-keep"],
)
def test_create_server_validates_the_markers_block(client, server_type, markers, status):
    from media_preview_generator.markers.settings import default_server_markers
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("media_servers", [])
    body = {"type": server_type, "name": server_type, "url": "http://x:1", "enabled": False}
    if server_type == "plex":
        body["output"] = {"plex_config_folder": "/tmp"}
    if markers is not None:
        body["markers"] = markers
    resp = client.post("/api/servers", json=body)
    assert resp.status_code == status, resp.get_json()
    saved = get_settings_manager().get("media_servers")
    if status == 400:
        assert "Confirm" in resp.get_json()["error"] and saved == []
        return
    expected = default_server_markers(server_type) if markers is None else markers
    assert saved[0]["markers"] == expected
