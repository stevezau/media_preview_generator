"""Docs site (Jekyll, docs/): build output, head tags, JSON-LD, crawler files, GitHub-Markdown parity.

Builds the real site once per test session with the same `bundle exec jekyll build` the Pages
deploy runs (tests/docs_toolchain.py), shared by every xdist worker through a file lock, then
inspects the output. Each assertion is something a reader, a crawler or a Jellyfin server would
trip over on the live site.

Docs pages carry no Liquid of their own: they are also read on github.com, where Liquid prints as
text. Layouts, includes and _data do the templating.
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from filelock import FileLock

from scripts.generate_llms_full import published_sources
from tests.docs_toolchain import DOCS_DIR, build_site, run_bundle

pytestmark = pytest.mark.timeout(900)

CONFIG: dict = yaml.safe_load((DOCS_DIR / "_config.yml").read_text(encoding="utf-8"))
SITE_ROOT = f"{CONFIG['url']}{CONFIG.get('baseurl') or ''}/"
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
PROSE = re.compile(r'<main class="prose"[^>]*>(.*?)</main>', re.DOTALL)
PARAGRAPH = re.compile(r"<p>(.*?)</p>", re.DOTALL)

# Every URL the MkDocs site served on 2026-09-23. Pretty permalinks keep them, so bookmarks, search
# results and the github.io -> mediapreviewgenerator.dev 301s all still land on a page.
LEGACY_PATHS = [
    "",
    "getting-started/",
    "comparison/",
    "plex-preview-thumbnails-slow/",
    "plex-preview-thumbnails-gpu/",
    "jellyfin-trickplay-gpu/",
    "emby-bif-thumbnails-gpu/",
    "sonarr-radarr-preview-thumbnails/",
    "hdr-dolby-vision-thumbnails/",
    "guides/",
    "guides/previews-readiness/",
    "multi-server/",
    "reference/",
    "faq/",
    "llms.txt",
    "llms-full.txt",
    "sitemap.xml",
    "robots.txt",
]
# Pages people open to look something up, not from a search about a server: their <title> may
# leave out Plex, Emby and Jellyfin.
REFERENCE_PAGES = {
    "faq/index.html",
    "getting-started/index.html",
    "guides/index.html",
    "guides/previews-readiness/index.html",
    "reference/index.html",
    "credits/index.html",
}
# kramdown's typographic rewrites (curly quotes, dashes, ellipsis). GitHub never makes them, so a
# built page may not contain more of any of these than its Markdown source does.
TYPOGRAPHY = "“”‘’–—…"
# FAQ heading ids the MkDocs site served until the FAQ was reworded for search (2026-09). Deep links
# to them sit in issues and forum posts, so each reworded heading carries its old id as well.
OLD_FAQ_IDS = [
    "what-does-this-tool-do",
    "what-plexembyjellyfin-settings-should-i-use",
    "does-this-work-on-windows",
    "does-this-generate-chapter-thumbnails",
    "can-i-use-this-without-a-gpu",
    "is-docker-required-is-there-a-standalone-exe",
    "can-i-run-this-on-a-different-machine-than-my-media-servers",
    "does-this-work-with-jellyfin-or-emby",
    "how-do-i-know-which-gpus-are-detected",
    "can-i-use-multiple-gpus",
    "which-gpu-should-i-use",
    "hdr--dolby-vision-support",
    "how-many-threads-should-i-use",
    "whats-thumbnail-quality-1-10",
    "generation-feels-disk-bound-on-my-multi-disk-setup-unraidmergerfsjbod--how-do-i-speed-it-up",
    "how-do-i-get-the-authentication-token",
    "can-i-process-specific-libraries-only",
    "how-do-i-regenerate-existing-thumbnails",
    "why-is-it-skipping-some-files",
    "why-does-eta-show-calculating-for-so-long",
]
# The films on the lab servers the site's player and HDR pictures were captured from, with each
# film's and each poster's licence as checked at source. docs/credits.md must credit every one.
FILMS: list[dict] = json.loads((DOCS_DIR / "design" / "site-redesign-lab" / "films.json").read_text(encoding="utf-8"))
FILMS_SECTION = re.compile(r'<h2 id="films">.*?</h2>(.*?)(?=<h2)', re.DOTALL)
LIST_ITEM = re.compile(r"<li>(.*?)</li>", re.DOTALL)

PLUGIN_CASES = {
    "marker_alone": "<blockquote>\n  <p>[!NOTE]</p>\n  <p>Body.</p>\n</blockquote>\n",
    "marker_inline": "<blockquote>\n  <p>[!TIP]\nBody on the same paragraph.</p>\n</blockquote>\n",
    "nested": (
        "<blockquote>\n  <p>[!WARNING]\nOuter.</p>\n  <blockquote>\n    <p>Inner quote.</p>\n"
        "  </blockquote>\n  <p>After.</p>\n</blockquote>\n<p>Outside.</p>\n"
    ),
    "plain_quote": "<blockquote>\n  <p>Just a quote.</p>\n</blockquote>\n",
    "two_alerts": (
        "<blockquote>\n  <p>[!NOTE]\nOne.</p>\n</blockquote>\n<blockquote>\n  <p>[!CAUTION]\nTwo.</p>\n</blockquote>\n"
    ),
}


def strip_front_matter(text: str) -> str:
    return FRONT_MATTER.sub("", text, count=1)


def front_matter(path: Path) -> dict:
    match = FRONT_MATTER.match(path.read_text(encoding="utf-8"))
    return (yaml.safe_load(match.group(1)) or {}) if match else {}


def built_page(site: Path, source: Path) -> Path:
    relative = source.relative_to(DOCS_DIR).with_suffix("").as_posix()
    return site / "index.html" if relative == "index" else site / relative / "index.html"


def faq_data() -> list[dict]:
    return yaml.safe_load((DOCS_DIR / "_data" / "faq.yml").read_text(encoding="utf-8"))


def _exists(site: Path, relative: str) -> bool:
    if relative == "":
        return (site / "index.html").is_file()
    if relative.endswith("/"):
        return (site / relative / "index.html").is_file()
    return (site / relative).is_file()


@dataclass
class Head:
    title: str = ""
    canonical: str | None = None
    description: str | None = None
    og_image: str | None = None
    og_url: str | None = None
    twitter_card: str | None = None
    google_verification: str | None = None
    json_ld: list[dict] = field(default_factory=list)


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.head = Head()
        self._in_title = False
        self._json: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "link" and attr.get("rel") == "canonical":
            self.head.canonical = attr.get("href")
        elif tag == "meta":
            key, value = attr.get("name") or attr.get("property"), attr.get("content")
            if key == "description":
                self.head.description = value
            elif key == "og:image":
                self.head.og_image = value
            elif key == "og:url":
                self.head.og_url = value
            elif key == "twitter:card":
                self.head.twitter_card = value
            elif key == "google-site-verification":
                self.head.google_verification = value
        elif tag == "script" and attr.get("type") == "application/ld+json":
            self._json = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.head.title += data
        if self._json is not None:
            self._json.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._json is not None:
            self.head.json_ld.append(json.loads("".join(self._json)))
            self._json = None


def parse_head(path: Path) -> Head:
    parser = _HeadParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.head


def json_ld_types(block: dict) -> set[str]:
    declared = block.get("@type")
    return set(declared) if isinstance(declared, list) else {declared}


@pytest.fixture(scope="session")
def site(tmp_path_factory: pytest.TempPathFactory, worker_id: str) -> Path:
    shared = tmp_path_factory.getbasetemp()
    if worker_id != "master":
        shared = shared.parent  # one folder for every xdist worker, so the site is built once
    out = shared / "docs-site"
    log = shared / "docs-site.log"
    with FileLock(str(shared / "docs-site.lock")):
        if not log.exists():
            result = build_site(out)
            log.write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode != 0:
                pytest.fail(f"jekyll build failed:\n{result.stdout}\n{result.stderr}")
        elif not (out / "index.html").is_file():
            pytest.fail(f"an earlier jekyll build failed; its output is in {log}")
    return out


@pytest.fixture(scope="session")
def build_output(site: Path) -> str:
    return (site.parent / "docs-site.log").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def pages(site: Path) -> dict[str, Head]:
    # 404.html is served with a 404 status and has no canonical URL.
    return {
        path.relative_to(site).as_posix(): parse_head(path)
        for path in sorted(site.rglob("*.html"))
        if path.name != "404.html"
    }


class TestBuild:
    def test_build_emits_no_warnings(self, build_output: str) -> None:
        # kramdown's show_warnings is on, so an unclosed tag or a broken table lands here instead
        # of rendering wrong without a word.
        noisy = [line for line in build_output.splitlines() if re.search(r"(?i)warn|deprecat|error", line)]
        assert noisy == []

    def test_every_legacy_url_still_resolves(self, site: Path) -> None:
        assert [path for path in LEGACY_PATHS if not _exists(site, path)] == []

    def test_every_published_source_becomes_a_page(self, site: Path) -> None:
        missing = [
            s.relative_to(DOCS_DIR).as_posix() for s in published_sources(DOCS_DIR) if not built_page(site, s).is_file()
        ]
        assert missing == []

    def test_design_docs_hub_and_toolchain_files_are_not_published(self, site: Path) -> None:
        for name in (
            "design",
            "benchmark",
            "README",
            "README.html",
            "README.md",
            "Gemfile",
            "Gemfile.lock",
            ".ruby-version",
            "_plugins",
        ):
            assert not (site / name).exists(), name

    def test_docs_build_never_emits_jellyfin_manifest(self, site: Path) -> None:
        # docs.yml adds the LIVE manifest at deploy time; a copy built from docs/ would shadow it.
        assert not (site / "jellyfin-plugin" / "manifest.json").exists()

    def test_llms_files_are_served_exactly_as_committed(self, site: Path) -> None:
        for name in ("llms.txt", "llms-full.txt"):
            committed = strip_front_matter((DOCS_DIR / name).read_text(encoding="utf-8"))
            assert (site / name).read_text(encoding="utf-8") == committed, name

    def test_cname_names_the_configured_host(self, site: Path) -> None:
        assert (site / "CNAME").read_text(encoding="utf-8").strip() == CONFIG["url"].removeprefix("https://")

    def test_search_index_holds_each_pages_rendered_text(self, site: Path) -> None:
        # search.json renders mid-build, when pages after it in build order still hold their Markdown.
        # Left as is, search snippets show "**" and "](" to the reader.
        entries = json.loads((site / "search.json").read_text(encoding="utf-8"))
        assert len(entries) > 10
        # "**bold" and "](" are Markdown; a redacted "****" token in a code sample is not.
        raw = {entry["url"]: re.findall(r"\*\*[A-Za-z]|\]\(", entry["content"]) for entry in entries}
        assert {url: tokens for url, tokens in raw.items() if tokens} == {}

        # An empty or cut-short entry has no Markdown in it either, so each nav page's entry must also
        # hold most of the words the page itself shows under its heading.
        indexed = {entry["url"]: len(entry["content"].split()) for entry in entries}
        urls = [entry["url"] for entry in CONFIG["nav"]]
        urls += [child["url"] for entry in CONFIG["nav"] for child in entry.get("children", [])]
        short = {}
        for url in urls:
            prose = PROSE.search((site / url.strip("/") / "index.html").read_text(encoding="utf-8")).group(1)
            body = re.sub(r'<div class="prose__head">.*?</div>', "", prose, count=1, flags=re.DOTALL)
            shown = len(re.sub(r"<[^>]+>", " ", body).split())
            if indexed.get(url, 0) < max(30, shown // 2):
                short[url] = (indexed.get(url, 0), shown)
        assert len(urls) > 10
        assert short == {}


class TestGithubMarkdownParity:
    def test_alerts_render_as_callouts(self, site: Path) -> None:
        assert 'class="callout callout--important"' in (site / "getting-started" / "index.html").read_text(
            encoding="utf-8"
        )
        leftovers = [
            path.relative_to(site).as_posix()
            for path in site.rglob("*.html")
            if re.search(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", path.read_text(encoding="utf-8"))
        ]
        assert leftovers == []

    def test_plugin_handles_nesting_neighbours_and_relative_paths(self, tmp_path: Path) -> None:
        cases = tmp_path / "cases.json"
        cases.write_text(json.dumps(PLUGIN_CASES), encoding="utf-8")
        code = (
            "cases = JSON.parse(File.read(ARGV[0])); "
            "out = cases.transform_values { |h| GithubMarkdown.convert_alerts(h) }; "
            'out["links_root"] = GithubMarkdown.absolutize(%q(<img src="images/a.webp"><a href="README.md#top">Docs</a> '
            '<a href="#x">x</a> <a href="https://e.x/README.md">e</a> <a href="/abs/">a</a>), ".", ""); '
            'out["links_nested"] = GithubMarkdown.absolutize(%q(<img src="../images/b.webp"><a href="../README.md">Docs</a> '
            '<a href="data.csv?x=1">d</a>), "guides", "/base"); '
            "puts JSON.generate(out)"
        )
        result = run_bundle(
            ["ruby", "-rjekyll", "-rjson", "-r./_plugins/github_markdown.rb", "-e", code, str(cases)], out_dir=tmp_path
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout.strip().splitlines()[-1])

        alone = out["marker_alone"]
        assert 'class="callout callout--note"' in alone
        assert '<p class="callout__label">Note</p><p>Body.</p>' in alone
        assert "<p></p>" not in alone and "[!" not in alone and "<blockquote" not in alone
        inline = out["marker_inline"]
        assert "callout--tip" in inline and "Body on the same paragraph." in inline and "<blockquote" not in inline
        nested = out["nested"]
        assert nested.count("<blockquote>") == 1 and nested.count("</blockquote>") == 1
        assert (
            nested.index("Inner quote.")
            < nested.index("After.")
            < nested.rindex("</div></div>")
            < nested.index("Outside.")
        )
        assert out["plain_quote"] == PLUGIN_CASES["plain_quote"]
        assert out["two_alerts"].count('class="callout ') == 2
        assert out["links_root"] == (
            '<img src="/images/a.webp"><a href="/#top">Docs</a> <a href="#x">x</a> '
            '<a href="https://e.x/README.md">e</a> <a href="/abs/">a</a>'
        )
        assert (
            out["links_nested"]
            == '<img src="/base/images/b.webp"><a href="/base/">Docs</a> <a href="/base/guides/data.csv?x=1">d</a>'
        )

    def test_plugin_reads_picture_sizes_from_every_header_layout(self, tmp_path: Path) -> None:
        # One file per layout the size reader parses. 300 px needs more than one byte in every
        # layout's size field, so a byte-order or offset slip gives a wrong number, not a lucky one.
        from PIL import Image

        images = tmp_path / "images"
        images.mkdir()
        Image.new("RGB", (300, 40), "red").save(images / "lossy.webp", lossless=False)
        Image.new("RGB", (300, 40), "red").save(images / "lossless.webp", lossless=True)
        Image.new("RGBA", (300, 40), (255, 0, 0, 128)).save(images / "alpha.webp", lossless=False)
        Image.new("RGB", (300, 40), "red").save(images / "shot.png")
        Image.new("RGB", (300, 40), "red").save(images / "shot.jpg")
        (images / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"/>')
        webps = ("lossy.webp", "lossless.webp", "alpha.webp")
        assert sorted((images / name).read_bytes()[12:16] for name in webps) == [b"VP8 ", b"VP8L", b"VP8X"]
        # A picture whose size the plugin can't read (an SVG has none in pixels) keeps its tag as
        # written: failing the build would also block a plugin release, which builds these docs.
        html_in = (
            '<p><img src="/base/images/lossy.webp" alt="a" /> '
            '<img src="/base/images/shot.png" width="9" alt="b"> <img src="https://e.x/images/c.webp" alt="c" /> '
            '<img src="/base/images/logo.svg" alt="d" /> <img src="/base/images/shot.jpg" alt="e" /></p>'
        )
        code = (
            "dir = ARGV[0]; "
            "out = %w[lossy.webp lossless.webp alpha.webp shot.png shot.jpg logo.svg]"
            '.to_h { |n| [n, GithubMarkdown.pixel_size("#{dir}/images/#{n}")] }; '
            'out["html"] = GithubMarkdown.add_image_sizes(ARGV[1], dir, "/base"); '
            "puts JSON.generate(out)"
        )
        result = run_bundle(
            ["ruby", "-rjekyll", "-rjson", "-r./_plugins/github_markdown.rb", "-e", code, str(tmp_path), html_in],
            out_dir=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout.strip().splitlines()[-1])
        assert {name: out[name] for name in (*webps, "shot.png")} == dict.fromkeys((*webps, "shot.png"), [300, 40])
        assert out["shot.jpg"] is None and out["logo.svg"] is None
        assert out["html"] == (
            '<p><img src="/base/images/lossy.webp" alt="a" width="150" height="20" /> '
            '<img src="/base/images/shot.png" width="9" alt="b"> <img src="https://e.x/images/c.webp" alt="c" /> '
            '<img src="/base/images/logo.svg" alt="d" /> <img src="/base/images/shot.jpg" alt="e" /></p>'
        )

    def test_docs_pictures_reserve_half_their_pixel_size(self, site: Path) -> None:
        # The landing page's rule (TestLanding) for pictures in the Markdown pages, where the plugin
        # adds width/height: without them the text below a picture jumps down as it loads.
        from PIL import Image

        sized = {}
        for source in published_sources(DOCS_DIR):
            if front_matter(source).get("layout") == "home":
                continue
            prose = PROSE.search(built_page(site, source).read_text(encoding="utf-8")).group(1)
            for tag in re.findall(r'<img[^>]+src="/images/[^"]+"[^>]*>', prose):
                name = re.search(r'src="/images/([^"]+)"', tag).group(1)
                reserved = re.search(r'width="(\d+)" height="(\d+)"', tag)
                with Image.open(DOCS_DIR / "images" / name) as image:
                    doubled = tuple(2 * int(side) for side in reserved.groups()) if reserved else None
                    sized[f"{source.name}: {name}"] = (image.size, doubled)
        assert "plex-preview-thumbnails-gpu.md: player-plex.webp" in sized
        assert {key: sizes for key, sizes in sized.items() if sizes[0] != sizes[1]} == {}

    def test_alert_markers_are_escaped_before_conversion_outside_code_fences_only(self, tmp_path: Path) -> None:
        # kramdown warns on every bare `[!NOTE]` (a reference link with no definition), so the plugin
        # escapes the bracket first; a fenced example of the syntax must still print as written.
        fenced = "````markdown\n```\n> [!WARNING]\n```\n> [!NOTE]\n````\n~~~\n> [!CAUTION]\n~~~\n"
        source = f"> [!NOTE]\n> Body with [!TIP] inside.\n\n  > > [!TIP]\n\n{fenced}> [!IMPORTANT] after\n"
        (tmp_path / "source.md").write_text(source, encoding="utf-8")
        code = "print GithubMarkdown.escape_alert_markers(File.read(ARGV[0]))"
        result = run_bundle(
            ["ruby", "-rjekyll", "-r./_plugins/github_markdown.rb", "-e", code, str(tmp_path / "source.md")],
            out_dir=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == (
            f"> \\[!NOTE]\n> Body with [!TIP] inside.\n\n  > > \\[!TIP]\n\n{fenced}> \\[!IMPORTANT] after\n"
        )

    def test_every_docs_table_scrolls_inside_its_own_box(self, site: Path) -> None:
        # Wrapped at build time, not by site.js: with the script blocked, a wide table otherwise
        # makes the whole page scroll sideways on a phone.
        tables, unwrapped = 0, {}
        for source in published_sources(DOCS_DIR):
            if front_matter(source).get("layout") == "home":
                continue
            prose = PROSE.search(built_page(site, source).read_text(encoding="utf-8")).group(1)
            tables += prose.count("<table")
            loose = prose.count("<table") - prose.count('<div class="table-scroll"><table')
            if loose:
                unwrapped[source.name] = loose
        assert tables > 10
        assert unwrapped == {}

    def test_no_list_or_table_is_swallowed_into_a_paragraph(self, site: Path) -> None:
        # A "- item" or "| a | b |" line with no blank line before it renders as literal text in a <p>.
        offenders = []
        for path in site.rglob("*.html"):
            for paragraph in PARAGRAPH.findall(path.read_text(encoding="utf-8")):
                for line in paragraph.splitlines():
                    line = line.strip()
                    if re.match(r"(?:[-*+]|\d+\.)\s+\S", line) or re.match(r"\|.*\|$", line):
                        offenders.append(f"{path.relative_to(site)}: {line[:80]}")
        assert offenders == []

    def test_literal_template_braces_survive_liquid(self, site: Path) -> None:
        assert "{{Item.Path}}" in (site / "multi-server" / "index.html").read_text(encoding="utf-8")
        assert "{{{args.inputFileObj._id}}}" in (site / "guides" / "index.html").read_text(encoding="utf-8")

    def test_pages_quoting_template_braces_opt_out_of_liquid(self) -> None:
        offenders = [
            source.relative_to(DOCS_DIR).as_posix()
            for source in published_sources(DOCS_DIR)
            if re.search(r"\{\{|\{%", strip_front_matter(source.read_text(encoding="utf-8")))
            and front_matter(source).get("render_with_liquid") is not False
        ]
        assert offenders == []

    def test_relative_images_and_hub_links_point_at_site_paths(self, site: Path) -> None:
        prose = PROSE.search((site / "multi-server" / "index.html").read_text(encoding="utf-8")).group(1)
        sources = re.findall(r'<img[^>]+src="([^"]+)"', prose)
        assert sources and all(src.startswith("/images/") for src in sources)
        assert all((site / src.lstrip("/")).is_file() for src in sources)
        offenders = [
            path.relative_to(site).as_posix()
            for path in site.rglob("*.html")
            if re.search(r'href="(?!https?:)[^"]*README(?:\.md|/|\.html)', path.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_markdown_typography_is_left_as_written(self, site: Path) -> None:
        offenders = []
        for source in published_sources(DOCS_DIR):
            if front_matter(source).get("layout") == "home":
                continue
            prose = PROSE.search(built_page(site, source).read_text(encoding="utf-8"))
            rendered = html.unescape(prose.group(1))
            raw = source.read_text(encoding="utf-8")
            offenders += [f"{source.name}: {char!r}" for char in TYPOGRAPHY if rendered.count(char) > raw.count(char)]
        assert offenders == []


class TestCrawlerFiles:
    def test_sitemap_lists_only_published_pages_on_the_site_host(self, site: Path) -> None:
        namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [loc.text for loc in ET.parse(site / "sitemap.xml").getroot().findall("sm:url/sm:loc", namespace)]
        assert SITE_ROOT in locs
        assert all(loc.startswith(SITE_ROOT) for loc in locs), locs
        assert [loc for loc in locs if re.search(r"design/|README|llms|search\.json|404", loc)] == []

    def test_robots_allows_everything_and_names_the_absolute_sitemap(self, site: Path) -> None:
        lines = (site / "robots.txt").read_text(encoding="utf-8").splitlines()
        assert "User-agent: *" in lines and "Allow: /" in lines
        assert f"Sitemap: {SITE_ROOT}sitemap.xml" in lines


class TestHeadTags:
    def test_every_page_has_its_own_canonical_description_and_social_card(self, pages: dict[str, Head]) -> None:
        assert pages
        site_description = " ".join(CONFIG["description"].split())
        for name, head in pages.items():
            expected = SITE_ROOT + ("" if name == "index.html" else name.removesuffix("index.html"))
            assert head.canonical == expected, name
            assert head.og_url == expected, name
            assert head.description and head.description.strip(), name
            assert head.description != site_description, name
            assert len(head.description) <= 155, name
            assert head.og_image == SITE_ROOT + "images/social-preview.jpg", name
            assert head.twitter_card == "summary_large_image", name

    def test_titles_are_unique_and_search_facing_ones_name_a_server(self, pages: dict[str, Head]) -> None:
        titles = [head.title for head in pages.values()]
        assert len(titles) == len(set(titles))
        for name, head in pages.items():
            if name not in REFERENCE_PAGES:
                assert re.search(r"Plex|Emby|Jellyfin", head.title), f"{name}: {head.title!r}"

    def test_home_describes_the_app_and_the_website(self, pages: dict[str, Head]) -> None:
        blocks = pages["index.html"].json_ld
        app = next(block for block in blocks if "SoftwareApplication" in json_ld_types(block))
        website = next(block for block in blocks if "WebSite" in json_ld_types(block))
        assert app["url"] == SITE_ROOT
        assert app["author"] == {"@type": "Person", "name": CONFIG["author"]["name"], "url": CONFIG["author"]["url"]}
        assert app["offers"]["price"] == "0"
        assert app["codeRepository"] == CONFIG["repo"]
        assert app["downloadUrl"] == CONFIG["docker_image_url"]
        assert website["url"] == SITE_ROOT

    def test_no_page_carries_a_search_console_token(self, pages: dict[str, Head]) -> None:
        # Search Console verifies this property by DNS. The meta-tag token that used to be here belonged
        # to a different Google account, so none should come back.
        assert "google_site_verification" not in CONFIG
        assert [name for name, head in pages.items() if head.google_verification] == []

    def test_docs_pages_carry_a_breadcrumb_ending_on_themselves(self, pages: dict[str, Head]) -> None:
        for name, head in pages.items():
            if name == "index.html":
                continue
            crumbs = [block for block in head.json_ld if "BreadcrumbList" in json_ld_types(block)]
            assert len(crumbs) == 1, name
            items = crumbs[0]["itemListElement"]
            assert [item["position"] for item in items] == list(range(1, len(items) + 1)), name
            assert items[0]["item"] == SITE_ROOT, name
            assert items[-1]["item"] == head.canonical, name
            assert all(item["name"] for item in items), name


class TestFaq:
    def test_faq_json_ld_matches_the_data_file(self, pages: dict[str, Head]) -> None:
        expected = [entry["q"] for entry in faq_data()]
        assert 6 <= len(expected) <= 8
        carriers = {
            name: [block for block in head.json_ld if "FAQPage" in json_ld_types(block)] for name, head in pages.items()
        }
        carriers = {name: blocks for name, blocks in carriers.items() if blocks}
        assert set(carriers) == {"faq/index.html", "index.html"}
        for name, blocks in carriers.items():
            assert len(blocks) == 1, name
            entities = blocks[0]["mainEntity"]
            assert [entity["name"] for entity in entities] == expected, name
            assert all(entity["acceptedAnswer"]["text"].strip() for entity in entities), name
            # A "[More](/page/)" link is for the page's readers; left in, the answer ends "... More".
            assert [e["name"] for e in entities if e["acceptedAnswer"]["text"].endswith("More")] == [], name

    def test_every_faq_data_question_is_a_heading_on_the_faq_page(self, site: Path) -> None:
        page = (site / "faq" / "index.html").read_text(encoding="utf-8")
        headings = {
            html.unescape(re.sub(r"<[^>]+>", "", heading)).strip()
            for heading in re.findall(r"<h[23][^>]*>(.*?)</h[23]>", page, re.DOTALL)
        }
        assert [entry["q"] for entry in faq_data() if entry["q"] not in headings] == []

    def test_links_to_the_old_faq_headings_still_land(self, site: Path) -> None:
        page = (site / "faq" / "index.html").read_text(encoding="utf-8")
        ids = set(re.findall(r'\sid="([^"]+)"', page))
        assert [old for old in OLD_FAQ_IDS if old not in ids] == []


LANDING_SECTIONS = ["how-it-works", "three-servers", "hdr", "features", "works-with", "compare", "install", "faq"]
COMPOSE_BLOCK = re.compile(r"<span>docker-compose\.yml</span>.*?<pre><code>(.*?)</code></pre>", re.DOTALL)


class TestLanding:
    def test_sections_appear_in_spec_order(self, site: Path) -> None:
        home = (site / "index.html").read_text(encoding="utf-8")
        positions = [home.find(f'id="{section}"') for section in LANDING_SECTIONS]
        assert -1 not in positions, dict(zip(LANDING_SECTIONS, positions, strict=True))
        assert positions == sorted(positions)

    def test_every_feature_card_link_is_a_built_page(self, site: Path) -> None:
        # home.html renders a card's `url` itself, so test_docs_links (Markdown links only) never sees it.
        features = yaml.safe_load((DOCS_DIR / "_data" / "features.yml").read_text(encoding="utf-8"))
        urls = [card["url"] for card in features if card.get("url")]
        assert urls, "no feature card carries a url, so this test checks nothing"
        home = (site / "index.html").read_text(encoding="utf-8")
        baseurl = CONFIG.get("baseurl") or ""
        for url in urls:
            assert _exists(site, url.lstrip("/")), url
            assert f'class="card__more" href="{baseurl}{url}"' in home, url

    def test_stats_come_from_the_data_file(self, site: Path) -> None:
        stats = yaml.safe_load((DOCS_DIR / "_data" / "stats.yml").read_text(encoding="utf-8"))
        home = (site / "index.html").read_text(encoding="utf-8")
        assert home.count('<li class="stat">') == len(stats)
        # The modifier is what lays three stats out as one row; a stale count falls back to four columns.
        assert f'class="stats stats--{len(stats)}"' in home
        labels = re.findall(r'<p class="stat__label">(.*?)</p>', home)
        assert [html.unescape(label) for label in labels] == [stat["label"] for stat in stats]

    def test_only_the_nearly_square_tour_captures_are_narrowed(self, site: Path) -> None:
        tour = yaml.safe_load((DOCS_DIR / "_data" / "tour.yml").read_text(encoding="utf-8"))
        home = (site / "index.html").read_text(encoding="utf-8")
        panels = re.findall(r'<figure class="(tour-panel[^"]*)">', home)
        assert panels == [
            "tour-panel tour-panel--tall" if step["height"] > 0.6 * step["width"] else "tour-panel" for step in tour
        ]
        assert "tour-panel tour-panel--tall" in panels  # the webhook page today; the branch is live

    def test_quick_start_compose_is_one_service_matching_docker_run(self, site: Path) -> None:
        # docker-compose.example.yml holds three alternative services with one container name and
        # port, so the old "curl it, then docker compose up -d" started all three and failed.
        block = COMPOSE_BLOCK.search((site / "index.html").read_text(encoding="utf-8"))
        assert block, "no docker-compose.yml block on the landing page"
        compose = yaml.safe_load(html.unescape(block.group(1)))
        (service,) = compose["services"].values()
        assert service["image"] == f"{CONFIG['docker_image']}:latest"
        assert service["ports"] == ["8080:8080"]
        assert service["environment"] == ["PUID=1000", "PGID=1000"]
        assert [volume.split(":")[1] for volume in service["volumes"]] == ["/media", "/plex", "/config"]
        assert service["devices"] == ["/dev/dri:/dev/dri"]

        # Following its NVIDIA comment (delete the two devices lines, uncomment the five below) must
        # give a valid file that asks Docker for the NVIDIA GPUs.
        lines = html.unescape(block.group(1)).splitlines()
        start = next(i for i, line in enumerate(lines) if line.strip().startswith("# NVIDIA: delete"))
        nvidia = lines[: start - 2] + [line.replace("# ", "", 1) for line in lines[start + 1 : start + 6]]
        nvidia += lines[start + 6 :]
        (gpu_service,) = yaml.safe_load("\n".join(nvidia))["services"].values()
        assert "devices" not in gpu_service
        (reservation,) = gpu_service["deploy"]["resources"]["reservations"]["devices"]
        assert reservation == {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}

    def test_landing_pictures_reserve_half_their_pixel_size(self, site: Path) -> None:
        # width/height stop the page reflowing as pictures load; a stale pair does the opposite.
        from PIL import Image

        home = (site / "index.html").read_text(encoding="utf-8")
        tags = re.findall(r'<img[^>]+src="/images/[^"]+"[^>]*>', home)
        sized = re.findall(r'<img[^>]+src="/images/([^"]+)"[^>]*width="(\d+)" height="(\d+)"', home)
        assert len(sized) == len(tags), "a landing picture has no numeric width/height"
        for name, width, height in sized:
            with Image.open(DOCS_DIR / "images" / name) as image:
                assert image.size == (2 * int(width), 2 * int(height)), name

    def test_every_landing_picture_has_alt_text(self, site: Path) -> None:
        home = (site / "index.html").read_text(encoding="utf-8")
        pictures = re.findall(r'<img[^>]+src="/images/[^"]+"[^>]*>', home)
        assert pictures
        assert [tag for tag in pictures if not re.search(r'alt="[^"]+"', tag)] == []

    def test_no_placeholder_figure_ships(self, site: Path) -> None:
        # The hero, three-player and HDR figures stood in as marked placeholders until the lab
        # captures were approved (plan Task 9 Step 8). None may come back, and each figure shows its
        # capture; test_landing_pictures_reserve_half_their_pixel_size opens every one.
        home = (site / "index.html").read_text(encoding="utf-8")
        assert re.findall(r'data-placeholder="([^"]+)"', home) == []
        assert "Placeholder" not in re.sub(r"<[^>]+>", " ", home)
        figures = re.findall(r'src="/images/((?:player|players|hdr)-[^"]+)"', home)
        assert figures == ["player-plex.webp", "players-3up.webp", "hdr-before-after.webp"]


class TestDocsContent:
    def test_comparison_date_comes_from_front_matter(self, site: Path) -> None:
        source = DOCS_DIR / "comparison.md"
        checked = front_matter(source)["facts_checked"]
        body = strip_front_matter(source.read_text(encoding="utf-8"))
        assert "Facts checked" not in body
        # A date typed into the body goes stale the next time facts_checked is bumped.
        assert f"{checked.day} {checked:%B %Y}" not in body
        expected = f"Facts checked {checked.day} {checked:%B %Y}."
        assert expected in (site / "comparison" / "index.html").read_text(encoding="utf-8")

    def test_comparison_discloses_authorship_and_invites_corrections(self, site: Path) -> None:
        page = (site / "comparison" / "index.html").read_text(encoding="utf-8")
        assert "This is my project" in page
        assert "https://github.com/stevezau/media_preview_generator/issues/new" in page
        assert 'id="first-decide-which-problem-you-have"' in page

    def test_credits_name_every_film_and_poster_with_its_own_licence(self, site: Path) -> None:
        # Checked on the built page, not the Markdown: kramdown reads an unescaped "|" in a list item
        # as a table cell, which splits "(CC) Blender Foundation | mango.blender.org" in two. One item
        # per film; the film's licence is checked before " Poster: " and the poster's after it, so a
        # poster credited under its film's licence (Elephants Dream: film CC BY 2.5, poster CC BY 4.0)
        # fails instead of passing on a URL some other film's item happens to carry.
        page = (site / "credits" / "index.html").read_text(encoding="utf-8")
        films_html = FILMS_SECTION.search(page).group(1)
        assert "<table" not in films_html
        items = [html.unescape(item) for item in LIST_ITEM.findall(films_html)]
        by_title = {re.match(r"\s*<strong>(.*?)</strong>", item).group(1): item for item in items}
        films = [film for film in FILMS if film["video"]]
        assert sorted(by_title) == sorted(film["title"] for film in films)
        for film in films:
            title = film["title"]
            film_part, _, poster_part = by_title[title].partition(" Poster: ")
            assert film["attribution"] in film_part, title
            assert f'<a href="{film["licence_url"]}">{film["licence"]}</a>' in film_part, title
            assert f'href="{film["licence_source"]}"' in film_part, title
            assert f'href="{film["source_page"]}"' in film_part, title
            if film.get("poster"):
                assert film["poster_attribution"] in poster_part, title
                assert f'<a href="{film["poster_licence_url"]}">{film["poster_licence"]}</a>' in poster_part, title
                assert f'href="{film["poster_page"]}"' in poster_part, title
            else:
                assert poster_part == "", title

    @pytest.mark.parametrize("page", ["index.html", "faq/index.html"], ids=["home", "docs"])
    def test_home_and_docs_footers_credit_the_films(self, site: Path, page: str) -> None:
        # The base row only: the Documentation column above it links /credits/ from nav anyway, so a
        # check on the whole footer passes with the credits line deleted. Home and docs pages use
        # different layouts, hence one of each.
        built = (site / page).read_text(encoding="utf-8")
        base = re.search(r'<div class="footer__base">(.*?)</div>', built, re.DOTALL).group(1)
        assert "Blender Foundation" in base
        assert f'href="{CONFIG.get("baseurl") or ""}/credits/"' in base
        if any(film.get("hdr") for film in FILMS if film["video"]):
            assert "Netflix" in base
