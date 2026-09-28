"""Support link wiring: navbar coffee cup, Star button, and FUNDING.yml.

The Ko-fi handle is duplicated across two files (base.html and
.github/FUNDING.yml). These tests pin them together so renaming the
account in one place fails loudly instead of shipping a dead link.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest

from media_preview_generator.web.settings_manager import get_settings_manager

SPONSOR_URL = "https://ko-fi.com/stevezau"
STAR_URL = "https://github.com/stevezau/media_preview_generator"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    from media_preview_generator.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False  # CSRF has its own tests: tests/test_csrf.py
    return app


@pytest.fixture
def authenticated_client(flask_app):
    """A test client with a valid session cookie."""
    from media_preview_generator.web.auth import get_auth_token

    token = get_auth_token()
    sm = get_settings_manager()
    sm.set("setup_complete", True)

    client = flask_app.test_client()
    client.post("/login", data={"token": token}, follow_redirects=False)
    return client


@dataclass
class _Anchor:
    attrs: dict[str, str]
    icon_classes: list[set[str]] = field(default_factory=list)
    text: str = ""

    def has_icon(self, *required: str) -> bool:
        return any(set(required) <= classes for classes in self.icon_classes)


class _AnchorCollector(HTMLParser):
    """Collects each <a> with its attributes, descendant <i> classes, and text.

    The icon classes matter as much as the href: every rule in the sponsor CSS
    is keyed on ``.sponsor-cup``, so a link that renders without that class is
    a silently broken feature, not a cosmetic nit.
    """

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[_Anchor] = []
        self._open: _Anchor | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        if tag == "a":
            self._open = _Anchor(attrs=attr_map)
            self.anchors.append(self._open)
        elif tag == "i" and self._open is not None:
            self._open.icon_classes.append(set(attr_map.get("class", "").split()))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open is not None and data.strip():
            self._open.text = f"{self._open.text} {data.strip()}".strip()


def _anchors_to(html: str, href: str) -> list[_Anchor]:
    parser = _AnchorCollector()
    parser.feed(html)
    return [a for a in parser.anchors if a.attrs.get("href") == href]


def _sponsor_anchors(html: str) -> list[_Anchor]:
    return _anchors_to(html, SPONSOR_URL)


@pytest.fixture
def dashboard_html(authenticated_client) -> str:
    response = authenticated_client.get("/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


class TestSponsorLinksInNavbar:
    def test_renders_only_the_navbar_cup(self, dashboard_html):
        """One entry point: the always-visible coffee cup. The Help menu no longer repeats it."""
        anchors = _sponsor_anchors(dashboard_html)

        assert [a.attrs.get("id", "") for a in anchors] == ["sponsorLinkBtn"]

    def test_help_menu_has_no_coffee_item_but_keeps_the_repo_link(self, dashboard_html):
        """The Help dropdown is for help; the cup beside it covers Ko-fi."""
        start = dashboard_html.index('aria-labelledby="helpMenuBtn"')
        help_menu = dashboard_html[start : dashboard_html.index("</ul>", start)]

        assert SPONSOR_URL not in help_menu
        assert "Buy me a coffee" not in help_menu
        assert [a.text for a in _anchors_to(help_menu, STAR_URL)] == ["GitHub repository"]

    def test_no_link_still_points_at_github_sponsors(self, dashboard_html):
        """Ko-fi replaced GitHub Sponsors as the one place to chip in."""
        assert "github.com/sponsors" not in dashboard_html

    @pytest.mark.parametrize("attr,expected", [("target", "_blank"), ("rel", "noopener noreferrer")])
    def test_every_sponsor_link_opens_safely_in_a_new_tab(self, dashboard_html, attr, expected):
        """rel=noopener guards against reverse-tabnabbing on target=_blank."""
        anchors = _sponsor_anchors(dashboard_html)
        assert anchors, "no sponsor links rendered at all"

        for anchor in anchors:
            assert anchor.attrs.get(attr) == expected, (
                f"sponsor link {anchor.attrs.get('id') or anchor.attrs} has {attr}={anchor.attrs.get(attr)!r}"
            )

    def test_every_sponsor_link_carries_the_styled_cup_icon(self, dashboard_html):
        """All sponsor CSS keys on .sponsor-cup — losing the class kills the styling."""
        anchors = _sponsor_anchors(dashboard_html)
        assert anchors, "no sponsor links rendered at all"

        for anchor in anchors:
            assert anchor.has_icon("bi", "bi-cup-hot-fill", "sponsor-cup"), (
                f"sponsor link {anchor.attrs.get('id') or anchor.attrs} is missing its "
                f"bi-cup-hot-fill/sponsor-cup icon; got {anchor.icon_classes}"
            )

    def test_every_sponsor_link_is_labelled_in_text_not_just_an_icon(self, dashboard_html):
        """The phone menu's row needs a visible label."""
        for anchor in _sponsor_anchors(dashboard_html):
            assert anchor.text == "Buy me a coffee", (
                f"sponsor link {anchor.attrs.get('id') or anchor.attrs} has text {anchor.text!r}"
            )

    def test_navbar_cup_is_labelled_for_screen_readers(self, dashboard_html):
        """The desktop cup is icon-only, so it needs an accessible name."""
        cup = next(a for a in _sponsor_anchors(dashboard_html) if a.attrs.get("id") == "sponsorLinkBtn")

        assert cup.attrs.get("aria-label") == "Buy me a coffee on Ko-fi"
        assert cup.attrs.get("title") == "Buy me a coffee on Ko-fi"

    def test_cup_renders_on_every_page_not_just_the_dashboard(self, authenticated_client):
        """It lives in base.html, so a page that overrides blocks must keep it."""
        response = authenticated_client.get("/settings")
        assert response.status_code == 200

        assert len(_sponsor_anchors(response.get_data(as_text=True))) == 1


class TestStarButton:
    def test_star_opens_the_repo_in_a_new_tab_with_its_name(self, dashboard_html):
        """There is no URL that stars a repo, so Star opens the repo page where GitHub's button is."""
        star = next(a for a in _anchors_to(dashboard_html, STAR_URL) if a.attrs.get("id") == "navStarBtn")

        assert star.attrs.get("aria-label") == "Star Media Preview Generator on GitHub"
        assert star.attrs.get("title") == "Star Media Preview Generator on GitHub"
        assert star.attrs.get("target") == "_blank"
        assert star.attrs.get("rel") == "noopener noreferrer"
        assert star.has_icon("bi", "bi-star-fill", "navbar-star")
        # Desktop word, then the phone menu's longer label (CSS shows one of them).
        assert star.text == "Star Star on GitHub"


class TestFundingConfig:
    def test_funding_yml_targets_the_same_account_as_the_ui(self):
        """GitHub reads FUNDING.yml from the default branch to render the repo's Sponsor button."""
        funding = REPO_ROOT / ".github" / "FUNDING.yml"
        assert funding.is_file(), "FUNDING.yml is missing — the repo Sponsor button won't render"

        handle = SPONSOR_URL.rsplit("/", 1)[-1]
        # Match active directives only — a raw substring search would also pass on a
        # fully commented-out file, which renders no button at all.
        directives = [
            line.strip()
            for line in funding.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert f"ko_fi: {handle}" in directives, (
            f"FUNDING.yml must actively declare '{handle}' to match the in-app Ko-fi links; got {directives}"
        )
