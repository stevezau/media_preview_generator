"""Every relative link in docs/ lands on a file that exists, inside docs/, at a heading that exists.

Jekyll publishes a link to a missing page or a renamed heading without complaint; the reader finds
out by clicking. Heading ids are kramdown's (GFM parser), which is what the site serves. Links also
have to stay inside docs/: a relative ../CONTRIBUTING.md works on github.com and 404s on the site,
so those use the absolute GitHub URL.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.generate_llms_full import published_sources

# Session fixture reused from test_docs_site.py (one Jekyll build per run). A test parameter named
# `site` requests it; the per-line noqa silences the "redefinition" warning.
from tests.test_docs_site import site  # noqa: F401

DOCS = Path(__file__).resolve().parent.parent / "docs"

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# A reference-style definition, `[label]: target` (comparison.md cites its sources this way).
LINK_DEFINITION = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:\s*(\S+)", re.MULTILINE)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
EXPLICIT_ID = re.compile(r"\{#([A-Za-z0-9_-]+)\}\s*$")
HTML_ID = re.compile(r"""\sid=["']([A-Za-z0-9_-]+)["']""")
HTML_TAG = re.compile(r"<[^>]+>")
SERVED_HEADING_ID = re.compile(r'<h[1-6][^>]*\sid="([^"]+)"')
SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "{{", "{%")


def _slugify(text: str) -> str:
    """The id kramdown's GFM parser gives a heading: what GitHub Pages and our Jekyll build serve.

    Checked against Shortlist's deployed pages (its tests/unit/test_docs_links.py): underscores are
    kept, leading non-letters are dropped, and runs are not collapsed ("A / B" -> "a--b"). The spaces
    before a trailing tag stay too: "Connection  <a id=...></a>" is served as #connection--.
    """
    text = HTML_TAG.sub("", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^[^a-zA-Z]+", "", text)
    text = re.sub(r"[^a-zA-Z0-9 _-]", "", text)
    return text.replace(" ", "-").lower()


def _anchors(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8", errors="replace"))  # "# comment" in code isn't a heading
    found = set(HTML_ID.findall(text))
    seen: dict[str, int] = {}
    for raw in HEADING.findall(text):
        if explicit := EXPLICIT_ID.search(raw):
            found.add(explicit.group(1))
            continue
        slug = _slugify(raw)
        count = seen.get(slug, 0)
        found.add(slug if count == 0 else f"{slug}-{count}")  # kramdown numbers repeats: x, x-1, x-2
        seen[slug] = count + 1
    return found


def _pages() -> list[Path]:
    # The published pages plus the docs hub, which is only read on github.com.
    return sorted([*published_sources(DOCS), DOCS / "README.md"])


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(DOCS).as_posix())
def test_every_relative_link_resolves(page: Path) -> None:
    broken: list[str] = []
    text = FENCE.sub("", page.read_text(encoding="utf-8", errors="replace"))
    for target in LINK.findall(text) + LINK_DEFINITION.findall(text):
        if target.startswith(SKIP_PREFIXES):
            continue
        url, _, fragment = target.partition("#")
        if not url:
            if fragment and fragment not in _anchors(page):
                broken.append(f"#{fragment}: this page has no such heading")
            continue
        if url.startswith("/"):
            continue  # a site path; Jekyll's permalinks decide it
        resolved = (page.parent / url.split("?", 1)[0]).resolve()
        if DOCS.resolve() not in resolved.parents:
            broken.append(f"{url}: leaves docs/, so it 404s on the site (use the absolute GitHub URL)")
        elif not resolved.exists():
            broken.append(f"{url}: no such file")
        elif fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
            broken.append(f"{url}#{fragment}: {resolved.name} has no such heading")
    assert not broken, f"{page.relative_to(DOCS)} links nowhere:\n  " + "\n  ".join(broken)


def test_heading_ids_match_what_the_site_serves(site: Path) -> None:  # noqa: F811
    # The link check is only as good as _slugify: an id it predicts that kramdown never emits lets a
    # dead #anchor through. Compare both ways against the ids the real build gave every heading.
    wrong: list[str] = []
    for page in published_sources(DOCS):
        relative = page.relative_to(DOCS).with_suffix("").as_posix()
        html = (site / ("" if relative == "index" else relative) / "index.html").read_text(encoding="utf-8")
        predicted = _anchors(page)
        served = set(SERVED_HEADING_ID.findall(html))
        wrong += [f"{relative}: served #{i}, not predicted" for i in sorted(served - predicted)]
        wrong += [f"{relative}: predicted #{i}, not on the page" for i in sorted(predicted) if f'id="{i}"' not in html]
    assert wrong == []
