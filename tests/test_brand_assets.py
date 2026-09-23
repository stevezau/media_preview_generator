"""One mark everywhere: the docs site, the README, the Unraid template and the app's own UI.

The logo is option A from docs/design/logo/ (spec §9), plus a favicon cut of it drawn on a 2-unit
grid so it lands on whole pixels at 16 and 32 px. These fail when one copy of the mark is updated
and another isn't, or a rendered PNG no longer has the size its <link> declares.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_IMG = REPO_ROOT / "docs" / "assets" / "img"
APP_IMG = REPO_ROOT / "media_preview_generator" / "web" / "static" / "images"
BASE_HTML = REPO_ROOT / "media_preview_generator" / "web" / "templates" / "base.html"
LOGO_DESIGN = REPO_ROOT / "docs" / "design" / "logo"
# The preview-frame path that makes option A option A.
LOGO_A_FRAME = "M25 8H51A5 5 0 0 1 56 13V27A5 5 0 0 1 51 32H42L38 36L34 32H25A5 5 0 0 1 20 27V13A5 5 0 0 1 25 8Z"


def test_every_copy_of_the_logo_is_logo_a() -> None:
    logo = (SITE_IMG / "logo.svg").read_bytes()
    assert LOGO_A_FRAME.encode() in logo
    assert (REPO_ROOT / "docs" / "images" / "icon.svg").read_bytes() == logo
    assert (APP_IMG / "icon.svg").read_bytes() == logo


def test_site_and_app_share_one_favicon() -> None:
    favicon = (SITE_IMG / "favicon.svg").read_bytes()
    assert b'viewBox="0 0 32 32"' in favicon
    assert (APP_IMG / "favicon.svg").read_bytes() == favicon


@pytest.mark.parametrize(
    ("path", "size"),
    [
        (SITE_IMG / "favicon-32.png", 32),
        (SITE_IMG / "apple-touch-icon.png", 180),
        (REPO_ROOT / "docs" / "images" / "icon.png", 512),
        (APP_IMG / "favicon-32.png", 32),
        (APP_IMG / "icon.png", 512),
    ],
    ids=["site-favicon-32", "apple-touch-180", "docs-icon-512", "app-favicon-32", "app-icon-512"],
)
def test_rendered_pngs_have_their_declared_size(path: Path, size: int) -> None:
    with Image.open(path) as image:
        assert image.size == (size, size)
        assert image.mode == "RGBA"


def test_app_pages_use_the_favicon_and_show_the_mark() -> None:
    base = BASE_HTML.read_text(encoding="utf-8")
    assert "filename='images/favicon.svg'" in base
    assert "filename='images/favicon-32.png'" in base
    assert 'class="navbar-brand-mark' in base


# The brand mark replaces the film-strip glyph only where it stands in for the product's own
# identity (the navbar brand link, the offcanvas/mobile menu header) -- not in unrelated feature
# icons elsewhere in the page (e.g. the Preview Inspector menu item), which keep their own icon.
_NAVBAR_BRAND_RE = re.compile(r'<a class="navbar-brand".*?</a>', re.DOTALL)
_OFFCANVAS_TITLE_RE = re.compile(r'<h5 class="offcanvas-title".*?</h5>', re.DOTALL)


def test_brand_elements_use_the_mark_and_not_the_old_film_icon() -> None:
    base = BASE_HTML.read_text(encoding="utf-8")
    navbar_brand = _NAVBAR_BRAND_RE.search(base)
    offcanvas_title = _OFFCANVAS_TITLE_RE.search(base)
    assert navbar_brand, "navbar-brand markup not found in base.html"
    assert offcanvas_title, "offcanvas-title markup not found in base.html"

    assert 'class="navbar-brand-mark' in navbar_brand.group()
    assert "bi-film" not in navbar_brand.group()

    assert 'class="offcanvas-brand-mark' in offcanvas_title.group()
    assert "bi-film" not in offcanvas_title.group()


def test_unchosen_logo_options_are_gone() -> None:
    assert not (LOGO_DESIGN / "logo-b.svg").exists()
    assert not (LOGO_DESIGN / "logo-c.svg").exists()
    assert not (LOGO_DESIGN / "logo-a.svg").exists()  # moved to docs/assets/img/logo.svg
