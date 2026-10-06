"""The navbar marks the current page with aria-current, not only with the `.active` class."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from media_preview_generator.web.settings_manager import get_settings_manager


@pytest.fixture
def authenticated_client(tmp_path, monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.auth import get_auth_token

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    get_settings_manager().set("setup_complete", True)
    client = app.test_client()
    client.post("/login", data={"token": get_auth_token()}, follow_redirects=False)
    return client


class _CurrentLinks(HTMLParser):
    """Collects (id, href) of every <a> carrying aria-current="page"."""

    def __init__(self) -> None:
        super().__init__()
        self.current: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: (value or "") for key, value in attrs}
        if tag == "a" and attr_map.get("aria-current") == "page":
            self.current.append((attr_map.get("id", ""), attr_map.get("href", "")))


def _current_links(html: str) -> list[tuple[str, str]]:
    parser = _CurrentLinks()
    parser.feed(html)
    return parser.current


class TestNavbarAriaCurrent:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/", [("", "/")]),
            ("/servers", [("", "/servers")]),
            ("/automation", [("navAutomationDropdown", "/automation")]),
            ("/settings", [("navSettingsDropdown", "/settings")]),
            ("/logs", [("navToolsDropdown", "/inspector"), ("", "/logs")]),
            ("/webhook-activity", [("navToolsDropdown", "/inspector"), ("", "/webhook-activity")]),
            ("/inspector", [("navToolsDropdown", "/inspector"), ("", "/inspector")]),
        ],
    )
    def test_marks_only_current_page_when_page_is_visited(self, authenticated_client, path, expected):
        html = authenticated_client.get(path).get_data(as_text=True)

        assert _current_links(html) == expected
