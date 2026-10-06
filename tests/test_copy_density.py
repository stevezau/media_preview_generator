"""Visible helper lines under a setting label state purpose only; the rest lives in the label's ⓘ."""

from __future__ import annotations

import html
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "media_preview_generator" / "web"
MAX_VISIBLE_HINT = 110

TEMPLATE_BLOCK = re.compile(r"<template\b.*?</template>", re.S)
SR_HINT = re.compile(r'<div class="sr-hint[^"]*"[^>]*>(.*?)</div>', re.S)
TAGS = re.compile(r"<[^>]+>")


def _visible_hints(path: Path) -> list[str]:
    text = TEMPLATE_BLOCK.sub("", path.read_text(encoding="utf-8"))
    return [" ".join(html.unescape(TAGS.sub("", m)).split()) for m in SR_HINT.findall(text)]


def test_settings_sr_hints_stay_short_when_detail_moves_to_info_icon() -> None:
    hints = _visible_hints(WEB / "templates" / "settings.html")

    assert hints, "settings.html: no .sr-hint found, update the scope regex"
    too_long = [(len(h), h) for h in hints if len(h) > MAX_VISIBLE_HINT]
    assert not too_long, f"move the explanation into the label's ⓘ template: {too_long}"


def test_webhook_secret_detail_lives_in_info_template_when_hint_is_short() -> None:
    text = (WEB / "templates" / "_automation_triggers.html").read_text(encoding="utf-8")

    detail = re.search(r'<template id="infoWebhookSecretTpl">(.*?)</template>', text, re.S)

    assert detail, "Webhook Secret ⓘ detail template is missing"
    body = " ".join(html.unescape(TAGS.sub("", detail.group(1))).split())
    assert "Leave empty to use the main API token instead." in body
    assert "re-registers any Plex Direct webhook" in body
