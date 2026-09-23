"""Docs site build (mkdocs.yml): exclusions, SEO head tags, JSON-LD, sitemap/robots.

Builds the real site once per module with ``mkdocs build --strict`` and inspects the
output, so these tests fail on the same things a Pages deploy would.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
from mkdocs.config import load_config
from mkdocs.config.defaults import MkDocsConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# A cold build plus Material's asset copy can take several seconds on a CI runner.
pytestmark = pytest.mark.timeout(180)


@dataclass
class _Head:
    canonical: str | None = None
    description: str | None = None
    og_image: str | None = None
    json_ld: list[dict] = field(default_factory=list)


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.head = _Head()
        self._in_json_ld = False
        self._json_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "link" and attr.get("rel") == "canonical":
            self.head.canonical = attr.get("href")
        elif tag == "meta" and attr.get("name") == "description":
            self.head.description = attr.get("content")
        elif tag == "meta" and attr.get("property") == "og:image":
            self.head.og_image = attr.get("content")
        elif tag == "script" and attr.get("type") == "application/ld+json":
            self._in_json_ld = True
            self._json_chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_json_ld:
            self.head.json_ld.append(json.loads("".join(self._json_chunks)))
            self._in_json_ld = False


def _parse_head(page: Path) -> _Head:
    parser = _HeadParser()
    parser.feed(page.read_text(encoding="utf-8"))
    return parser.head


def _types(block: dict) -> set[str]:
    declared = block.get("@type")
    return set(declared) if isinstance(declared, list) else {declared}


def _faq_headings() -> list[str]:
    """Question headings (``### ...``) in docs/faq.md, ignoring fenced code."""
    headings = []
    in_fence = False
    for line in (DOCS_DIR / "faq.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("### "):
            headings.append(re.sub(r"[`*_]", "", line[4:]).strip())
    return headings


@pytest.fixture(scope="module")
def mkdocs_config() -> MkDocsConfig:
    return load_config(config_file=str(REPO_ROOT / "mkdocs.yml"))


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("site")
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=170,
    )
    if result.returncode != 0:
        pytest.fail(f"mkdocs build --strict failed:\n{result.stdout}\n{result.stderr}")
    return out


@pytest.fixture(scope="module")
def pages(site: Path) -> dict[str, _Head]:
    # 404.html is served with a 404 status and has no canonical URL by design.
    return {
        str(path.relative_to(site)): _parse_head(path)
        for path in sorted(site.rglob("*.html"))
        if path.name != "404.html"
    }


class TestBuildOutput:
    def test_strict_build_produces_home_page_when_docs_are_valid(self, site: Path) -> None:
        assert (site / "index.html").is_file()
        assert (site / "faq" / "index.html").is_file()

    def test_design_docs_and_github_hub_are_not_published_when_excluded(self, site: Path) -> None:
        assert not (site / "design").exists()
        assert not (site / "README").exists()
        assert not (site / "README.html").exists()
        for design_doc in (DOCS_DIR / "design").glob("*.md"):
            assert not (site / "design" / design_doc.stem).exists()

    def test_hub_links_point_at_site_home_when_docs_link_readme(self, site: Path) -> None:
        # docs/README.md is the GitHub hub and is not published; links to it must be retargeted.
        offenders = [
            str(path.relative_to(site))
            for path in site.rglob("*.html")
            if re.search(r'href="(?!https?://)[^"]*README(/|\.html|\.md)', path.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_github_alerts_render_as_admonitions_when_built(self, site: Path) -> None:
        html = (site / "getting-started" / "index.html").read_text(encoding="utf-8")
        assert 'class="admonition important"' in html
        for path in site.rglob("*.html"):
            assert not re.search(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", path.read_text(encoding="utf-8")), path

    def test_docs_build_never_emits_jellyfin_manifest(self, site: Path) -> None:
        # The live manifest is added by docs.yml at deploy time; a copy in docs/ would shadow it.
        assert not (site / "jellyfin-plugin" / "manifest.json").exists()

    def test_llms_files_are_copied_verbatim_when_present_at_repo_root(self, site: Path) -> None:
        assert (REPO_ROOT / "llms.txt").is_file()
        for name in ("llms.txt", "llms-full.txt"):
            source = REPO_ROOT / name
            if source.is_file():
                assert (site / name).read_bytes() == source.read_bytes()
            else:
                assert not (site / name).exists()


class TestCrawlerFiles:
    def test_every_sitemap_url_is_on_site_url_when_built(self, site: Path, mkdocs_config: MkDocsConfig) -> None:
        namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [loc.text for loc in ET.parse(site / "sitemap.xml").getroot().findall("sm:url/sm:loc", namespace)]

        assert mkdocs_config.site_url in locs
        assert all(loc.startswith(mkdocs_config.site_url) for loc in locs), locs
        assert not [loc for loc in locs if "/design/" in loc or "README" in loc]

    def test_robots_allows_everything_and_names_absolute_sitemap(self, site: Path, mkdocs_config: MkDocsConfig) -> None:
        robots = (site / "robots.txt").read_text(encoding="utf-8").splitlines()

        assert "User-agent: *" in robots
        assert "Allow: /" in robots
        assert f"Sitemap: {mkdocs_config.site_url}sitemap.xml" in robots


class TestHeadTags:
    def test_every_page_has_canonical_description_and_og_image(
        self, pages: dict[str, _Head], mkdocs_config: MkDocsConfig
    ) -> None:
        assert pages
        for name, head in pages.items():
            assert head.canonical and head.canonical.startswith(mkdocs_config.site_url), name
            # Each page needs its own front-matter description, not Material's site-wide fallback.
            assert head.description and head.description.strip(), name
            assert head.description != mkdocs_config.site_description, name
            assert len(head.description) <= 155, name
            assert head.og_image == mkdocs_config.site_url + mkdocs_config.extra["social_image"], name

    def test_home_json_ld_describes_app_and_website_from_config(
        self, pages: dict[str, _Head], mkdocs_config: MkDocsConfig
    ) -> None:
        blocks = pages["index.html"].json_ld
        app = next(block for block in blocks if "SoftwareApplication" in _types(block))
        website = next(block for block in blocks if "WebSite" in _types(block))

        assert app["url"] == mkdocs_config.site_url
        assert app["author"] == {
            "@type": "Person",
            "name": mkdocs_config.site_author,
            "url": mkdocs_config.extra["author_url"],
        }
        assert app["offers"]["price"] == "0"
        assert app["codeRepository"] == mkdocs_config.repo_url
        assert website["url"] == mkdocs_config.site_url

    def test_faq_json_ld_has_one_question_per_faq_heading(self, pages: dict[str, _Head]) -> None:
        faq = next(block for block in pages["faq/index.html"].json_ld if "FAQPage" in _types(block))
        questions = [entry["name"] for entry in faq["mainEntity"]]

        assert questions == _faq_headings()
        assert len(questions) > 0
        assert all(entry["acceptedAnswer"]["text"] for entry in faq["mainEntity"])

    def test_faq_json_ld_appears_only_on_faq_page(self, pages: dict[str, _Head]) -> None:
        with_faq = [name for name, head in pages.items() if any("FAQPage" in _types(b) for b in head.json_ld)]
        assert with_faq == ["faq/index.html"]
