"""Servers Add/Edit modal redesign: every inventoried control must survive.

``tests/data/server_modal_ids.txt`` is the committed 146-row inventory of the modal. Each line names a control (or a literal that has to
stay in the shipped code), the vendors it applies to and where it lives. These checks run against the template
sources and the shipped JS/Python, so a removed or renamed id fails here before an e2e run would notice. The
per-vendor visibility half of the contract is in ``tests/e2e/test_server_modal_redesign.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "media_preview_generator"
WEB = PACKAGE / "web"
DATA_FILE = Path(__file__).resolve().parent / "data" / "server_modal_ids.txt"

TEMPLATES = ("servers.html", "_add_server_modal.html", "_server_connection_form.html")
SCRIPTS = (
    "servers.js",
    "markers_server_tab.js",
    "loudness_server_tab.js",
    "plex_webhook_panel.js",
    "plex-auth.js",
    "folder_picker.js",
)
INVENTORY_ROWS = 146
MAX_HINT_CHARS = 95


def _entries() -> list[tuple[int, str, str, str, str]]:
    entries = []
    for line in DATA_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        row, vendors, tab, mode, selector = line.split("\t")
        entries.append((int(row), vendors, tab, mode, selector))
    return entries


ENTRIES = _entries()
ELEMENT_ENTRIES = [e for e in ENTRIES if e[3] in {"visible", "attached"}]
CODE_ENTRIES = [e for e in ENTRIES if e[3] == "code"]


@pytest.fixture(scope="module")
def template_source() -> str:
    return "\n".join((WEB / "templates" / name).read_text(encoding="utf-8") for name in TEMPLATES)


@pytest.fixture(scope="module")
def script_source() -> str:
    return "\n".join((WEB / "static" / "js" / name).read_text(encoding="utf-8") for name in SCRIPTS)


@pytest.fixture(scope="module")
def python_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.rglob("*.py"))


def _tokens(selector: str) -> list[str]:
    """The ids, classes, names and attribute values a selector depends on."""
    return re.findall(r"#([\w-]+)|\.([\w-]+)|name=\"([\w-]+)\"|data-type=\"([\w-]+)\"|for=\"([\w-]+)\"", selector)


def _bare_names(selector: str) -> list[str]:
    return [next(part for part in match if part) for match in _tokens(selector)]


class TestInventoryList:
    def test_list_covers_every_inventory_row_when_compared_to_the_inventory_count(self) -> None:
        assert {e[0] for e in ENTRIES} == set(range(1, INVENTORY_ROWS + 1))

    def test_list_rows_use_known_vendors_and_modes_when_parsed(self) -> None:
        for row, vendors, _tab, mode, selector in ENTRIES:
            assert vendors and set(vendors) <= set("PEJ"), (row, vendors)
            assert mode in {"visible", "attached", "code"}, (row, mode)
            assert selector, row


class TestElementsSurvive:
    @pytest.mark.parametrize(
        ("row", "selector"),
        sorted({(e[0], e[4]) for e in ELEMENT_ENTRIES}),
        ids=lambda value: str(value),
    )
    def test_control_is_in_the_templates_or_built_by_the_scripts_when_inventoried(
        self, row: int, selector: str, template_source: str, script_source: str
    ) -> None:
        names = _bare_names(selector)
        assert names, f"row {row}: no id, class or name found in {selector!r}"
        for name in names:
            in_template = name in template_source
            in_script = name in script_source
            assert in_template or in_script, f"row {row}: {name!r} (from {selector!r}) is in no template or script"

    @pytest.mark.parametrize("name", ["editReauthJfQc", "editReauthEmbyPw", "auth-quick", "auth-pw", "auth-key"])
    def test_radio_ids_stay_attributes_of_inputs_when_templates_are_read(self, name: str, template_source: str) -> None:
        assert re.search(rf'<input[^>]*\bid="{re.escape(name)}"', template_source), name

    def test_static_ids_are_unique_within_the_edit_modal_when_templates_are_read(self, template_source: str) -> None:
        """A duplicated id would make the shared handlers bind to the wrong element."""
        ids = re.findall(r'\bid="([\w-]+)"', template_source)
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        assert not duplicated, duplicated


class TestCodeSurvives:
    @pytest.mark.parametrize(("row", "token"), sorted({(e[0], e[4]) for e in CODE_ENTRIES}), ids=lambda v: str(v))
    def test_token_is_still_shipped_when_inventoried(
        self, row: int, token: str, template_source: str, script_source: str, python_source: str
    ) -> None:
        shipped = template_source + "\n" + script_source + "\n" + python_source
        assert token in shipped, f"row {row}: {token!r} no longer appears in the templates, scripts or Python"


class TestSharedPartials:
    def test_connection_form_partial_is_still_included_by_the_add_modal_and_the_wizard(self) -> None:
        assert "_server_connection_form.html" in (WEB / "templates" / "_add_server_modal.html").read_text("utf-8")
        assert "_server_connection_form.html" in (WEB / "templates" / "setup.html").read_text("utf-8")

    def test_edit_modal_tab_panes_keep_their_ids_when_templates_are_read(self, template_source: str) -> None:
        for tab in ("general", "health", "processing", "libraries", "paths", "excludes", "automation"):
            assert f'id="edit-tab-{tab}"' in template_source
            assert f'data-bs-target="#edit-tab-{tab}"' in template_source

    def test_open_edit_modal_keeps_its_tab_names_when_scripts_are_read(self, script_source: str) -> None:
        for key in ("general", "health", "processing", "markers", "loudness"):
            assert re.search(rf"\b{key}:\s*'edit-tab-", script_source), key

    def test_every_vendor_still_has_a_type_picker_button_when_templates_are_read(self, template_source: str) -> None:
        for vendor in ("plex", "emby", "jellyfin"):
            assert f'data-type="{vendor}"' in template_source

    def test_visible_hint_lines_stay_short_when_templates_are_read(self, template_source: str) -> None:
        """Copy density: a visible one-line hint states purpose only (<= ~90 characters of its own text)."""
        visible = re.sub(r"<template.*?</template>", "", template_source, flags=re.S)
        pattern = r'<(div|p|span) class="sm-hint[^"]*"[^>]*>(.*?)</\1>'
        hints = []
        for match in re.finditer(pattern, visible, flags=re.S):
            body = re.sub(r"<button.*?</button>", "", match.group(2), flags=re.S)
            text = re.sub(r"<[^>]+>|\{[{%].*?[}%]\}", "", body)
            hints.append(" ".join(text.split()))
        assert len(hints) > 20, "the hint pattern no longer matches the templates"
        offenders = [text[:60] for text in hints if len(text) > MAX_HINT_CHARS]
        assert not offenders, offenders
