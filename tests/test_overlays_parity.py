"""Function parity for the overlays redesign: every id, class, data attribute and handler the overlays inventory
lists as kept / restyled / moved still exists in the templates or scripts.

The inventory is committed as ``tests/data/overlay_ids.txt`` (section, row, status, token), so the test needs nothing
outside ``tests/`` and the shipped sources. Rows tagged "new" are optional in the spec and are not checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = Path(__file__).resolve().parent / "data" / "overlay_ids.txt"
WEB = ROOT / "media_preview_generator/web"

# Inventory tokens that name something the redesign deliberately replaced; each maps to what superseded it.
SUPERSEDED = {
    ".sched-group-label": "numbered section headers (.ov-sec-h) carry the group labels",
    "#jobCheckServersNote": None,
}
SUPERSEDED = {token: why for token, why in SUPERSEDED.items() if why}
TOKEN = re.compile(r"`([^`]+)`")
ID_LIKE = re.compile(r"^#[A-Za-z][\w-]*$")
CLASS_LIKE = re.compile(r"^\.[A-Za-z][\w-]*$")
DATA_LIKE = re.compile(r"^data-[a-z-]+$")
FUNC_LIKE = re.compile(r"^_?[a-z][A-Za-z0-9]*$")
NAME_LIKE = re.compile(r"^name=([A-Za-z]\w*)$")


def _corpus() -> str:
    parts = [p.read_text(encoding="utf-8") for p in sorted((WEB / "templates").glob("*.html"))]
    parts += [p.read_text(encoding="utf-8") for p in sorted((WEB / "static/js").glob("*.js"))]
    return "\n".join(parts)


def _entries() -> list[tuple[str, int, str, str]]:
    """(section, row, status, token) for every line of the committed inventory list."""
    entries = []
    for line in DATA_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        section, row, status, token = line.split("\t")
        entries.append((section, int(row), status, token))
    return entries


def _inventory_tokens() -> list[tuple[str, str]]:
    """(section, token) for every checkable token of a row tagged kept / restyled / moved."""
    return [
        (section, token)
        for section, _row, status, token in _entries()
        if status in {"kept", "restyled", "moved"} and token != "-"
    ]


def _present(token: str, corpus: str) -> bool | None:
    """True / False when the token can be checked, None when it is prose or a pattern."""
    if ID_LIKE.match(token):
        name = token[1:]
        # Ids shared by two dialogs are built from a prefix: id="{{ filter_prefix }}ScanFilters".
        shared = re.sub(r"^(job|schedule)", "", name)
        return (
            f'id="{name}"' in corpus
            or f"id='{name}'" in corpus
            or f"getElementById('{name}')" in corpus
            or f'id="{{{{ filter_prefix }}}}{shared}"' in corpus
        )
    if CLASS_LIKE.match(token):
        return token[1:] in corpus
    if DATA_LIKE.match(token):
        return token in corpus or f"dataset.{token[5:]}" in corpus
    match = NAME_LIKE.match(token)
    if match:
        return f'name="{match.group(1)}"' in corpus
    if FUNC_LIKE.match(token):
        return re.search(rf"\b{re.escape(token)}\b", corpus) is not None
    return None


_TOKENS = sorted({tok for _, tok in _inventory_tokens() if tok not in SUPERSEDED})


def test_inventory_lists_the_expected_volume_of_controls() -> None:
    rows = len({row for _, row, _, _ in _entries()})
    assert rows >= 125, f"the overlays inventory parsed to {rows} rows"
    assert len(_TOKENS) >= 100


@pytest.mark.parametrize("token", _TOKENS)
def test_inventory_token_exists_when_row_is_kept_restyled_or_moved(token: str) -> None:
    present = _present(token, _corpus())
    if present is None:
        pytest.skip("prose or a pattern, not an identifier")
    assert present, f"{token} is in the overlays inventory but not in the templates or scripts"
