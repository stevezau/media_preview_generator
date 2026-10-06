"""Design tokens in style.css keep AA contrast on the surfaces they are drawn on, in both themes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "media_preview_generator" / "web"
CSS = WEB / "static" / "css"
STYLE = (CSS / "style.css").read_text()

# Surfaces as painted (cards and inset tiles are translucent white over the page in dark; measured in the browser).
DARK = {"page": "#1a1a2e", "card": "#252538", "inset": "#2e2e40", "panel": "#242439"}
LIGHT = {"page": "#e4e7eb", "card": "#fbfcfd", "inset": "#e8ebef", "navbar": "#f4f6f8"}


def _block(selector: str) -> dict[str, str]:
    """Return the custom properties declared in the first rule that starts with ``selector``."""
    start = STYLE.index(selector + " {")
    body = STYLE[start : STYLE.index("\n}", start)]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", body))


def _soft_fill(tokens_css: str, name: str, surface: str) -> str:
    """The pill colour: a ``--<name>-soft`` rgba tint painted over ``surface``."""
    match = re.search(rf"--{name}-soft:\s*rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", tokens_css)
    assert match, name
    red, green, blue, alpha = int(match[1]), int(match[2]), int(match[3]), float(match[4])
    base = [int(surface[i : i + 2], 16) for i in (1, 3, 5)]
    mixed = [round(c * alpha + b * (1 - alpha)) for c, b in zip((red, green, blue), base, strict=True)]
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def _luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.fixture(scope="module")
def dark() -> dict[str, str]:
    tokens = _block(":root")
    tokens.update(_block('html[data-bs-theme="dark"]'))
    return tokens


@pytest.fixture(scope="module")
def light(dark: dict[str, str]) -> dict[str, str]:
    tokens = dict(dark)
    tokens.update(_block('html[data-bs-theme="light"]'))
    return tokens


class TestDarkTokens:
    @pytest.mark.parametrize("surface", ["page", "card", "inset", "panel"])
    @pytest.mark.parametrize("token", ["--muted", "--faint", "--ok", "--bad", "--run", "--accent-text"])
    def test_text_tokens_reach_aa_when_drawn_on_any_dark_surface(self, dark, token, surface) -> None:
        assert contrast(dark[token], DARK[surface]) >= 4.5, f"{token} on {surface}"

    @pytest.mark.parametrize("name", ["ok", "bad", "run", "warn"])
    @pytest.mark.parametrize("surface", ["page", "card"])
    def test_status_text_reaches_aa_on_its_own_tinted_pill_when_dark(self, dark, name, surface) -> None:
        pill = _soft_fill(STYLE[: STYLE.index('html[data-bs-theme="dark"] {')], name, DARK[surface])
        assert contrast(dark[f"--{name}"], pill) >= 4.5, f"{name} on its pill over {surface}"

    @pytest.mark.parametrize("surface", ["page", "card", "inset", "panel"])
    def test_body_text_is_comfortable_and_not_harsh_when_on_dark_surfaces(self, dark, surface) -> None:
        ratio = contrast(dark["--bs-body-color"], DARK[surface])
        assert 9.5 <= ratio <= 15, ratio

    def test_headings_stay_below_pure_white_when_dark(self, dark) -> None:
        assert dark["--bs-emphasis-color"].lower() != "#ffffff"
        assert contrast(dark["--bs-emphasis-color"], DARK["page"]) <= 15

    def test_faint_is_weaker_than_muted_when_dark(self, dark) -> None:
        assert contrast(dark["--faint"], DARK["card"]) < contrast(dark["--muted"], DARK["card"])

    def test_button_ink_reaches_aa_on_the_amber_button_fill(self, dark) -> None:
        assert contrast(dark["--accent-ink"], dark["--accent-fill"]) >= 4.5

    def test_modal_dropdown_and_offcanvas_surface_is_lighter_than_the_card(self, dark) -> None:
        assert _luminance(dark["--panel-2"]) > _luminance(DARK["page"])
        assert dark["--panel-2"].lower() == DARK["panel"]


class TestLightTokens:
    @pytest.mark.parametrize("surface", ["page", "card", "inset", "navbar"])
    @pytest.mark.parametrize(
        "token",
        ["--bs-body-color", "--bs-secondary-color", "--muted", "--faint", "--ok", "--bad", "--run", "--warn"]
        + ["--accent-text", "--plex-orange-text", "--bs-link-color"],
    )
    def test_text_tokens_reach_aa_when_drawn_on_any_light_surface(self, light, token, surface) -> None:
        assert contrast(light[token], LIGHT[surface]) >= 4.5, f"{token} on {surface}"

    def test_loudness_type_colour_reaches_aa_on_the_light_card(self, light) -> None:
        assert contrast(light["--t-loud"], LIGHT["card"]) >= 4.5

    def test_navbar_brand_uses_the_dark_amber_text_token_when_light(self) -> None:
        assert re.search(r'html\[data-bs-theme="light"\] \.navbar-brand \{\s*color: var\(--plex-orange-text\)', STYLE)

    def test_card_edge_is_visible_against_the_light_card(self, light) -> None:
        assert contrast(light["--bs-border-color"], LIGHT["card"]) >= 1.4
        assert contrast(light["--surface-border"], LIGHT["card"]) >= 1.4

    def test_button_ink_reaches_aa_on_the_light_amber_fill(self, light) -> None:
        assert contrast(light["--accent-ink"], light["--accent-fill"]) >= 4.5


class TestOneAccentForControls:
    def test_checked_controls_use_the_single_accent_fill_when_in_the_global_layer(self) -> None:
        assert re.search(r"\.form-check-input:checked \{ background-color: var\(--accent-fill\)", STYLE)

    @pytest.mark.parametrize("name", ["queue.css", "job_dialogs.css"])
    def test_dashboard_stylesheets_do_not_recolour_checked_controls(self, name) -> None:
        css = (CSS / name).read_text()
        assert ":checked" not in css
        assert "accent-color" not in css


class TestPageShell:
    def test_navbar_main_and_banner_share_the_one_page_shell_when_rendered(self) -> None:
        base = (WEB / "templates" / "base.html").read_text()
        assert base.count("page-shell") == 3
        assert 'class="page-shell"' in base

    def test_page_shell_uses_the_gutter_and_width_tokens(self) -> None:
        match = re.search(r"\.page-shell \{([^}]*)\}", STYLE)
        assert match
        assert "var(--page-gutter)" in match.group(1)
        assert "var(--page-max)" in match.group(1)


class TestOneAccentAndOneGutter:
    def test_no_page_stylesheet_recolours_switches_checkboxes_or_radios_when_css_is_read(self) -> None:
        offenders = [
            f"{path.relative_to(CSS)}: {line.strip()[:80]}"
            for path in CSS.rglob("*.css")
            if path.name != "style.css"
            for line in path.read_text().splitlines()
            if ".form-check-input:checked" in line
        ]
        assert not offenders, offenders

    @pytest.mark.parametrize(
        "template",
        ["servers.html", "webhook_activity.html", "automation.html", "settings.html", "logs.html", "inspector.html"],
    )
    def test_page_template_adds_no_container_of_its_own_when_it_extends_the_shell(self, template: str) -> None:
        source = (WEB / "templates" / template).read_text()
        assert not re.search(r'class="[^"]*\bcontainer(-fluid|-xxl|-xl|-lg)?\b', source), template
