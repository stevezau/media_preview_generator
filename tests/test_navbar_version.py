"""The running version in every page's navbar: its label, the update dot, and that rendering never asks GitHub.

``navbar_version_info`` reads the cached update check (any age) and falls back to the installed build.
GitHub lookups are patched to raise, so a test fails if the navbar ever waits on the network.
"""

from __future__ import annotations

import re
import time

import pytest

from media_preview_generator.web.routes import api_system
from media_preview_generator.web.settings_manager import get_settings_manager

RELEASES = "https://github.com/stevezau/media_preview_generator/releases/latest"


def _no_github(*_args, **_kwargs):
    raise AssertionError("the navbar must not call GitHub")


@pytest.fixture(autouse=True)
def _isolated_version_cache(monkeypatch):
    monkeypatch.setattr("media_preview_generator.version_check.get_latest_github_release", _no_github)
    monkeypatch.setattr("media_preview_generator.version_check.get_branch_head_sha", _no_github)
    for var in ("GIT_BRANCH", "GIT_SHA"):
        monkeypatch.delenv(var, raising=False)
    api_system._version_cache["result"] = None
    api_system._version_cache["fetched_at"] = 0.0
    yield
    api_system._version_cache["result"] = None
    api_system._version_cache["fetched_at"] = 0.0


def _cache(result: dict, fetched_at: float | None = None) -> None:
    api_system._version_cache["result"] = result
    api_system._version_cache["fetched_at"] = time.monotonic() if fetched_at is None else fetched_at


class TestNavbarVersionInfo:
    @pytest.mark.parametrize(
        "branch,sha,label",
        [
            ("unknown", "unknown", "local build"),
            ("pr-12", "abc1234def", "PR-12"),
            ("3.4.1", "abc1234def", "v3.4.1"),
            ("v3.4.1", "abc1234def", "v3.4.1"),
            ("dev", "a9c8177d2e41", "dev@a9c8177"),
        ],
    )
    def test_before_any_check_the_label_comes_from_the_build(self, monkeypatch, branch, sha, label):
        monkeypatch.setenv("GIT_BRANCH", branch)
        monkeypatch.setenv("GIT_SHA", sha)

        assert api_system.navbar_version_info() == {"label": label, "update_available": False, "latest_version": None}

    def test_a_source_install_shows_the_package_version(self, monkeypatch):
        monkeypatch.setattr("media_preview_generator.version_check.get_current_version", lambda: "3.9.0")

        assert api_system.navbar_version_info()["label"] == "v3.9.0"

    def test_a_cached_update_shows_the_new_version(self):
        _cache(
            {"current_version": "3.4.0", "latest_version": "3.4.1", "update_available": True, "install_type": "docker"}
        )

        assert api_system.navbar_version_info() == {
            "label": "v3.4.0",
            "update_available": True,
            "latest_version": "3.4.1",
        }

    def test_a_check_older_than_the_hour_is_still_used_not_refetched(self):
        _cache(
            {
                "current_version": "dev@a9c8177",
                "latest_version": "dev@896c569",
                "update_available": True,
                "install_type": "dev_docker",
            },
            fetched_at=time.monotonic() - 10 * api_system._VERSION_CACHE_TTL,
        )

        info = api_system.navbar_version_info()

        assert info["label"] == "dev@a9c8177"
        assert info["update_available"] is True


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    # The startup pre-warm would race these tests for the cache.
    monkeypatch.setattr("media_preview_generator.web.app._prewarm_caches", lambda: None)
    from media_preview_generator.web.auth import get_auth_token
    from media_preview_generator.web.routes import clear_gpu_cache
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    clear_gpu_cache()
    from media_preview_generator.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    get_settings_manager().set("setup_complete", True)
    test_client = app.test_client()
    test_client.post("/login", data={"token": get_auth_token()}, follow_redirects=False)
    return test_client


def _version_link(html: str) -> str:
    match = re.search(r'<a class="navbar-version"[^>]*>.*?</a>', html, re.S)
    assert match, "navbar version link missing"
    return match.group(0)


class TestNavbarVersionRendering:
    @pytest.mark.parametrize("path", ["/", "/settings", "/automation", "/servers", "/logs", "/inspector"])
    def test_every_page_shows_the_version_linking_to_release_notes(self, client, monkeypatch, path):
        monkeypatch.setenv("GIT_BRANCH", "dev")
        monkeypatch.setenv("GIT_SHA", "a9c8177d2e41")

        response = client.get(path)

        assert response.status_code == 200
        link = _version_link(response.get_data(as_text=True))
        assert f'href="{RELEASES}"' in link
        assert 'target="_blank" rel="noopener noreferrer"' in link
        assert '<span id="navVersionText">dev@a9c8177</span>' in link
        assert 'title="Release notes"' in link
        assert 'aria-label="Version dev@a9c8177. Release notes, opens in a new tab"' in link
        assert "navVersionDot" not in link

    def test_an_available_update_adds_the_dot_and_says_which_version(self, client):
        _cache(
            {"current_version": "3.4.0", "latest_version": "3.4.1", "update_available": True, "install_type": "docker"}
        )

        link = _version_link(client.get("/").get_data(as_text=True))

        assert '<span id="navVersionText">v3.4.0</span>' in link
        assert 'id="navVersionDot"' in link
        assert 'title="Version 3.4.1 is available"' in link
        assert 'aria-label="Version v3.4.0. Version 3.4.1 is available, opens in a new tab"' in link

    @pytest.mark.parametrize("install_type,label", [("local_docker", "local build"), ("pr_build", "PR-12")])
    def test_builds_that_never_flag_updates_show_no_dot(self, client, install_type, label):
        _cache(
            {
                "current_version": label,
                "latest_version": "3.4.1",
                "update_available": False,
                "install_type": install_type,
            }
        )

        link = _version_link(client.get("/").get_data(as_text=True))

        assert f'<span id="navVersionText">{label}</span>' in link
        assert "navVersionDot" not in link
