"""The setup file checker resolves draft mappings without reading media contents."""

import pytest

from tests import test_routes as route_fixtures

_reset_singletons = route_fixtures._reset_singletons
app = route_fixtures.app
client = route_fixtures.client
authed_client = route_fixtures.authed_client

ENDPOINT = "/api/setup/preview-file-path"


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    """Constrain checks to one temporary media mount."""
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr("media_preview_generator.web.routes.api_settings.MEDIA_ROOT", str(root))
    return root


@pytest.mark.parametrize("prefix_key", ["remote_prefix", "plex_prefix"])
@pytest.mark.parametrize(
    "source", ["/server/Movie.mkv", "/imports/Movie.mkv", r"Z:\Movies\Movie.mkv", r"\\nas\Movies\Movie.mkv"]
)
def test_draft_mapping_reads_metadata_only(authed_client, media_root, prefix_key, source):
    movie = media_root / "Movie.mkv"
    movie.write_bytes(b"file contents must not be returned")
    prefix = source.replace("\\", "/").rsplit("/", 1)[0]
    response = authed_client.post(
        ENDPOINT,
        json={"path": source, "path_mappings": [{prefix_key: prefix, "local_prefix": str(media_root)}]},
    )
    assert response.status_code == 200
    assert response.json == {
        "input_path": source.replace("\\", "/"),
        "local_path": str(movie),
        "mapping_applied": True,
        "exists": True,
        "is_file": True,
        "readable": True,
    }
    assert movie.read_bytes() == b"file contents must not be returned"
    assert list(media_root.iterdir()) == [movie]


def test_webhook_alias_and_segment_boundary(authed_client, media_root):
    (media_root / "video.mkv").touch()
    mappings = [{"remote_prefix": "/server", "local_prefix": str(media_root), "webhook_prefixes": ["/imports"]}]
    assert authed_client.post(ENDPOINT, json={"path": "/imports/video.mkv", "path_mappings": mappings}).json["readable"]
    assert (
        authed_client.post(ENDPOINT, json={"path": "/imports-extra/video.mkv", "path_mappings": mappings}).status_code
        == 400
    )


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_file_outcomes_are_distinct(authed_client, media_root, monkeypatch, kind):
    candidate = media_root / "item"
    if kind == "directory":
        candidate.mkdir()
    elif kind == "unreadable":
        candidate.touch()
        monkeypatch.setattr("media_preview_generator.web.routes.api_settings.os.access", lambda *_: False)
    response = authed_client.post(ENDPOINT, json={"path": str(candidate)})
    assert response.status_code == 200
    assert response.json["exists"] is (kind != "missing")
    assert response.json["is_file"] is (kind == "unreadable")
    assert response.json["readable"] is False


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"path": 12},
        {"path": ""},
        {"path": "/a\x00b"},
        {"path": "relative.mkv"},
        {"path": "/media/../outside"},
        {"path": "/a", "path_mappings": {}},
        {"path": "/a", "path_mappings": [None]},
        {"path": "/a", "path_mappings": [{"remote_prefix": 3}]},
        {"path": "/a", "path_mappings": [{"remote_prefix": "/a", "local_prefix": "relative"}]},
        {"path": "/a", "path_mappings": [{"remote_prefix": "/a", "local_prefix": "/b", "webhook_prefixes": "bad"}]},
        {"path": "/a", "path_mappings": [{"remote_prefix": "/a", "local_prefix": "/b", "webhook_prefixes": [None]}]},
        {"path": "/" + "a" * 4096},
        {"path": "/a", "path_mappings": [{}] * 101},
    ],
)
def test_rejects_malformed_requests(authed_client, media_root, payload):
    assert authed_client.post(ENDPOINT, json=payload).status_code == 400


def test_symlink_escape_and_prefix_collision_rejected(authed_client, media_root, tmp_path):
    outside = tmp_path / "outside.mkv"
    outside.write_text("secret")
    (media_root / "escape.mkv").symlink_to(outside)
    for path in [str(media_root / "escape.mkv"), str(media_root) + "-other/file.mkv"]:
        response = authed_client.post(ENDPOINT, json={"path": path})
        assert response.status_code == 400
        assert "secret" not in response.get_data(as_text=True)
        assert "local_path" not in response.json


def test_auth_required_after_setup_and_setup_state_private(client, media_root):
    assert client.post(ENDPOINT, json={"path": str(media_root)}).status_code == 401
    assert client.get("/api/setup/state").status_code == 401


def test_available_during_initial_setup(client, media_root):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("setup_complete", False)
    assert client.post(ENDPOINT, json={"path": str(media_root)}).status_code == 200


def test_default_root_still_requires_absolute_path(authed_client, media_root, monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.routes.api_settings.MEDIA_ROOT", "/")
    assert authed_client.post(ENDPOINT, json={"path": str(media_root)}).status_code == 200
    assert authed_client.post(ENDPOINT, json={"path": "relative"}).status_code == 400


def test_request_body_limit(authed_client):
    assert (
        authed_client.post(
            ENDPOINT, data='{"path":"' + "a" * (1024 * 1024) + '"}', content_type="application/json"
        ).status_code
        == 413
    )


@pytest.mark.parametrize("prefix", ["relative", "Z:Movies"])
def test_relative_server_path_cannot_be_made_absolute_by_mapping(authed_client, media_root, prefix):
    response = authed_client.post(
        ENDPOINT,
        json={
            "path": f"{prefix}/file.mkv",
            "path_mappings": [{"remote_prefix": prefix, "local_prefix": str(media_root)}],
        },
    )
    assert response.status_code == 400
    assert "absolute" in response.json["error"]
