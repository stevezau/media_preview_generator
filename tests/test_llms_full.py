"""docs/llms-full.txt: drift, where it publishes, and that every link in it is real.

scripts/generate_llms_full.py builds it from _config.yml's nav, the pages, and the landing page's
_data files. These fail the way a stale or broken file would bite an agent reading it: a section
missing, a link that 404s, Liquid it can't read, a data-file edit that never reached the file.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

from scripts.generate_llms_full import (
    DOCS_DIR,
    OUTPUT_NAME,
    LlmsFullError,
    generate,
    load_config,
    nav_urls,
    published_sources,
    site_root,
)

# Session fixtures reused from test_docs_site.py (one Jekyll build per run). A test parameter
# named `site` requests the fixture; the per-line noqa silences the "redefinition" warning.
from tests.test_docs_site import build_output, site  # noqa: F401

pytestmark = pytest.mark.timeout(900)

COMMITTED = DOCS_DIR / OUTPUT_NAME
ROOT = site_root(load_config(DOCS_DIR))
# docs.yml adds this at deploy time; a local build never has it.
DEPLOY_TIME_PATHS = {"jellyfin-plugin/manifest.json"}
# Link targets, written apart from the generator's own patterns so a form it misses still shows up:
# inline (with or without a title) and reference-style definitions (`[label]: target "title"`).
INLINE_TARGET = re.compile(r"""\]\(([^)\s]+)(?:\s+(?:"[^"]*"|'[^']*'))?\)""")
DEFINITION_TARGET = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:[ \t]*(\S+)", re.MULTILINE)
# Each form a page may write, and what it must become in llms-full.txt.
LINK_FORMS = {
    "titled": ('[t](reference.md "The reference")', f'[t]({ROOT}reference/ "The reference")'),
    "single_quoted_title": ("[t](reference.md 'The reference')", f"[t]({ROOT}reference/ 'The reference')"),
    "definition": ("[t]: reference.md#infrastructure-variables", f"[t]: {ROOT}reference/#infrastructure-variables"),
    "titled_definition": ('[t]: reference.md "The reference"', f'[t]: {ROOT}reference/ "The reference"'),
    "site_path": ("[t](/faq/)", f"[t]({ROOT}faq/)"),
}


@pytest.fixture(scope="module")
def generated() -> str:
    return generate()


def _docs_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "docs"
    shutil.copytree(
        DOCS_DIR, copy, ignore=shutil.ignore_patterns("design", "_site", ".jekyll-cache", "vendor", ".bundle")
    )
    return copy


def _docs_copy_with(tmp_path: Path, markdown: str) -> Path:
    """A docs copy whose faq.md ends with `markdown`."""
    docs = _docs_copy(tmp_path)
    page = docs / "faq.md"
    page.write_text(page.read_text(encoding="utf-8") + f"\n{markdown}\n", encoding="utf-8")
    return docs


def _relative_targets(text: str) -> list[str]:
    targets = INLINE_TARGET.findall(text) + DEFINITION_TARGET.findall(text)
    return [t for t in targets if not t.startswith(("http://", "https://", "mailto:"))]


class TestDrift:
    def test_committed_file_matches_generator_output(self, generated: str) -> None:
        assert COMMITTED.read_text(encoding="utf-8") == generated, (
            "docs/llms-full.txt is out of date. Regenerate with: python scripts/generate_llms_full.py"
        )

    def test_publishes_at_its_own_url_not_over_llms_txt(self) -> None:
        # The header is lifted from llms.txt, itself a page with `permalink: /llms.txt`. Copied with its
        # front matter, it would publish llms-full.txt ON TOP of llms.txt (Shortlist shipped exactly that).
        head = COMMITTED.read_text(encoding="utf-8")[:200]
        assert "permalink: /llms-full.txt" in head
        assert "permalink: /llms.txt" not in head
        assert "render_with_liquid: false" in head

    def test_editing_a_data_file_changes_the_output(self, tmp_path: Path) -> None:
        docs = _docs_copy(tmp_path)
        faq_path = docs / "_data" / "faq.yml"
        faq = yaml.safe_load(faq_path.read_text(encoding="utf-8"))
        faq[0]["q"] = "Sentinel question 7f3a?"
        faq_path.write_text(yaml.safe_dump(faq, sort_keys=False, allow_unicode=True), encoding="utf-8")
        assert "Sentinel question 7f3a?" in generate(docs)

    def test_a_page_missing_from_nav_is_an_error(self, tmp_path: Path) -> None:
        docs = _docs_copy(tmp_path)
        config_path = docs / "_config.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["nav"] = [entry for entry in config["nav"] if entry["url"] != "/faq/"]
        config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
        with pytest.raises(LlmsFullError, match="faq.md"):
            generate(docs)

    @pytest.mark.parametrize(
        "link",
        ["[gone](no-such-page.md)", '[gone](no-such-page.md "Gone")', "[gone]: no-such-page.md"],
        ids=["inline", "titled", "definition"],
    )
    def test_a_dead_relative_link_is_an_error(self, tmp_path: Path, link: str) -> None:
        with pytest.raises(LlmsFullError, match="no-such-page.md"):
            generate(_docs_copy_with(tmp_path, link))

    def test_a_page_without_a_description_is_an_error(self, tmp_path: Path) -> None:
        docs = _docs_copy(tmp_path)
        page = docs / "faq.md"
        _, front, body = page.read_text(encoding="utf-8").split("---\n", 2)
        front_matter = yaml.safe_load(front)
        del front_matter["description"]
        page.write_text(f"---\n{yaml.safe_dump(front_matter, sort_keys=False)}---\n{body}", encoding="utf-8")
        with pytest.raises(LlmsFullError, match="faq.md.*description"):
            generate(docs)


class TestContent:
    def test_every_published_page_appears_once_in_nav_order(self, generated: str) -> None:
        expected = [ROOT] + [ROOT + url.strip("/") + "/" for url in nav_urls(load_config(DOCS_DIR))]
        assert re.findall(r"^Source: (\S+)$", generated, re.MULTILINE) == expected
        assert len(expected) == len(published_sources(DOCS_DIR))

    def test_no_liquid_an_agent_cannot_read(self, generated: str) -> None:
        # Template syntax the docs QUOTE (Jellyfin's {{Item.Path}}, Tdarr's {{{args...}}}) is content.
        assert re.findall(r"\{%|\{\{\s*(?:site|page)\.", generated) == []

    def test_quoted_template_syntax_survives(self, generated: str) -> None:
        assert "{{Item.Path}}" in generated
        assert "{{{args.inputFileObj._id}}}" in generated

    def test_no_relative_links_remain(self, generated: str) -> None:
        assert _relative_targets(generated) == []

    def test_no_markup_noise(self, generated: str) -> None:
        body = generated.split("\n---\n", 1)[1]  # everything after the file's own front matter
        assert "<!--" not in body
        assert '<a id="' not in body
        assert "application/ld+json" not in body

    def test_llms_txt_advertises_the_full_file(self) -> None:
        assert f"{ROOT}llms-full.txt" in (DOCS_DIR / "llms.txt").read_text(encoding="utf-8")

    def test_every_site_url_is_a_built_file(
        self,
        generated: str,
        site: Path,  # noqa: F811
    ) -> None:
        urls = {match.rstrip(".,;:") for match in re.findall(re.escape(ROOT) + r"[^\s)\"'<>]*", generated)}
        assert urls
        missing = []
        for url in sorted(urls):
            relative = url[len(ROOT) :].split("#", 1)[0].split("?", 1)[0]
            if relative in DEPLOY_TIME_PATHS:
                continue
            target = site / relative
            if not (target.is_file() or (target / "index.html").is_file()):
                missing.append(url)
        assert missing == []


class TestLinkForms:
    @pytest.mark.parametrize(("written", "expected"), LINK_FORMS.values(), ids=LINK_FORMS.keys())
    def test_every_link_form_becomes_an_absolute_url(self, tmp_path: Path, written: str, expected: str) -> None:
        output = generate(_docs_copy_with(tmp_path, written))
        assert expected in output
        assert _relative_targets(output) == []

    def test_the_relative_link_check_sees_every_form(self) -> None:
        sample = '[a](x.md "t")\n[b](y.md)\n[c]: z.md\n[d]: w.md "t"\n[e](https://example.com/)\n'
        assert _relative_targets(sample) == ["x.md", "y.md", "z.md", "w.md"]
