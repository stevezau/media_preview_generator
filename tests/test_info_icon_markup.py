"""Static checks on how ⓘ info icons are authored (the rendered rule is tests/e2e/test_info_icons.py).

app.js's ``_applyInfoIconAffordance`` owns "Click for more." and the pointer class, so authors write only the short
hover sentence and attach a detail; these checks keep the markup from drifting back to hand-written hints, bare
icons, walls of hover text, or a detail that points at nothing.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "media_preview_generator" / "web"
TEMPLATES = sorted((WEB / "templates").glob("*.html"))
SCRIPTS = sorted((WEB / "static" / "js").glob("*.js"))

ICON_TAG = re.compile(r'<(?:button|i|span|a)\b[^>]*class="[^"]*\binfo-icon\b[^"]*"[^>]*>', re.S)
TITLE = re.compile(r'\btitle="([^"]*)"')
# The longest hover allowed: about two lines of a wide tooltip. The longest today is the credits window text in
# Settings › Intro & Credits › Advanced (170 characters).
MAX_HINT = 170


def _icons(path: Path) -> list[tuple[int, str]]:
    text = path.read_text()
    return [(text.count("\n", 0, m.start()) + 1, m.group(0)) for m in ICON_TAG.finditer(text)]


@pytest.mark.parametrize("path", TEMPLATES + SCRIPTS, ids=lambda p: p.name)
def test_no_icon_writes_its_own_click_hint(path: Path) -> None:
    for line, tag in _icons(path):
        title = TITLE.search(tag)
        assert not (title and re.search(r"click for (details|more)", title.group(1), re.I)), (
            f"{path.name}:{line}: the helper adds 'Click for more.'; write only the hover sentence"
        )


@pytest.mark.parametrize("path", TEMPLATES + SCRIPTS, ids=lambda p: p.name)
def test_no_bare_info_circle_tooltips(path: Path) -> None:
    bare = re.findall(r"<i\b[^>]*\bbi-info-circle\b[^>]*\bdata-bs-toggle=", path.read_text(), re.S)
    assert not bare, f'{path.name}: use a <button class="info-icon"> so the ⓘ rule applies: {bare}'


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_hover_text_is_short(path: Path) -> None:
    for line, tag in _icons(path):
        title = TITLE.search(tag)
        hint = re.sub(r"\s+", " ", html.unescape(title.group(1))) if title else ""
        assert len(hint) <= MAX_HINT, (
            f"{path.name}:{line}: {len(hint)} characters of hover; move the rest into a detail <template>: {hint}"
        )


def test_every_detail_template_an_icon_names_exists() -> None:
    defined = {m for path in TEMPLATES for m in re.findall(r'<template id="([^"]+)"', path.read_text())}
    named = {
        (path.name, m)
        for path in TEMPLATES + SCRIPTS
        for m in re.findall(r'data-explain-template="([A-Za-z][\w-]*)"', path.read_text())
    }
    assert named, "no ⓘ names a detail template at all"
    missing = sorted((name, tpl) for name, tpl in named if tpl not in defined)
    assert not missing, f"ⓘs name detail templates that don't exist: {missing}"
