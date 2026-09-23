"""llms-full.txt: drift, cleanliness, and every emitted URL is a real built page.

``scripts/generate_llms_full.py`` builds llms-full.txt from mkdocs.yml's nav plus
the docs themselves. These tests fail the same way a stale/broken llms-full.txt
would bite an agent reading it: a link that 404s, a front-matter leak, a page
missing or duplicated.

Reuses the ``site``/``mkdocs_config`` fixtures from test_docs_site.py (one real
``mkdocs build --strict``) instead of building the site again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mkdocs.config.defaults import MkDocsConfig

from scripts.generate_llms_full import OUTPUT_PATH, generate, nav_pages

# Reused pytest fixtures (module-scoped: one `mkdocs build --strict` shared across
# every test here rather than building the site again). Every test parameter named
# `mkdocs_config`/`site` below is a fixture *request*, not a redefinition of these
# imports -- ruff's F811 can't tell the difference, hence the per-line `noqa: F811`.
from tests.test_docs_site import mkdocs_config, site  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent

# The `site` fixture runs a real `mkdocs build --strict` subprocess (see
# test_docs_site.py); give it the same headroom that module its timeout does.
pytestmark = pytest.mark.timeout(180)

# Deploy-time-only URL: docs.yml fetches the LIVE jellyfin-plugin/manifest.json and
# ships it at this path when the site deploys; a local `mkdocs build` never produces
# it (see the "Hard constraint" section of docs/design/discoverability.md). Docs
# pages reference it as a plain absolute URL, which is correctly left untouched by
# the generator's link resolution -- but it can never resolve against a local build.
_DEPLOY_TIME_URLS = {"jellyfin-plugin/manifest.json"}


@pytest.fixture(scope="module")
def generated() -> str:
    return generate()


class TestDrift:
    def test_committed_file_matches_generator_output(self, generated: str) -> None:
        assert OUTPUT_PATH.is_file(), (
            f"{OUTPUT_PATH} doesn't exist. Generate it with: python scripts/generate_llms_full.py"
        )
        committed = OUTPUT_PATH.read_text(encoding="utf-8")
        assert committed == generated, (
            f"{OUTPUT_PATH} is out of date with the docs. Regenerate with:\n    python scripts/generate_llms_full.py"
        )


class TestCleanliness:
    def test_no_front_matter_or_permalink_markers(self, generated: str) -> None:
        assert "permalink:" not in generated
        # Every docs page's real front matter is "---\ndescription: ...\n---\n"; a bare
        # "---" horizontal rule (used throughout the docs as a divider) is never
        # immediately followed by "description:", so this stays precise.
        assert "---\ndescription:" not in generated

    def test_no_html_comments_or_script_blocks(self, generated: str) -> None:
        assert "<!--" not in generated
        assert "<script" not in generated.lower()

    def test_no_relative_markdown_links_remain(self, generated: str) -> None:
        offenders = [
            target
            for target in re.findall(r"\]\(([^)\s]+)\)", generated)
            if not target.startswith(("http://", "https://", "#", "mailto:"))
        ]
        assert offenders == []

    def test_ends_with_exactly_one_trailing_newline(self, generated: str) -> None:
        assert generated.endswith("\n")
        assert not generated.endswith("\n\n")


class TestPageCoverage:
    def test_every_nav_page_appears_exactly_once(
        self,
        generated: str,
        mkdocs_config: MkDocsConfig,  # noqa: F811
    ) -> None:
        pages, _files = nav_pages(mkdocs_config)
        separators = re.findall(r"^PAGE: (.+)$", generated, re.MULTILINE)

        assert len(separators) == len(pages)
        assert separators == [page.title for page in pages]


class TestUrlCoverage:
    def test_every_site_url_resolves_to_a_real_built_file(
        self,
        generated: str,
        site: Path,  # noqa: F811
        mkdocs_config: MkDocsConfig,  # noqa: F811
    ) -> None:
        site_url = mkdocs_config.site_url
        urls = {
            match.rstrip(".,;")  # trailing prose punctuation, not part of a directory-style URL
            for match in re.findall(re.escape(site_url) + r'[^\s)"\'<>]*', generated)
        }
        assert urls, "no site_url-prefixed URLs found in the generated output"

        missing = []
        for url in urls:
            relative = url[len(site_url) :].split("#", 1)[0]
            if relative in _DEPLOY_TIME_URLS:
                continue
            if not _built_path_exists(site, relative):
                missing.append(url)

        assert missing == []


def _built_path_exists(site_dir: Path, relative: str) -> bool:
    """True if ``relative`` (a path under site_url, "" for the root) exists in the build."""
    if relative in ("", "."):
        return (site_dir / "index.html").is_file()
    candidate = site_dir / relative
    if candidate.is_file():
        return True
    if relative.endswith("/"):
        return (candidate / "index.html").is_file()
    return False


class TestLlmsTxtAdvertisesFullFile:
    def test_llms_txt_links_the_llms_full_url(self, mkdocs_config: MkDocsConfig) -> None:  # noqa: F811
        llms_txt = REPO_ROOT / "llms.txt"
        assert llms_txt.is_file(), "llms.txt doesn't exist at the repo root"
        expected_url = mkdocs_config.site_url + "llms-full.txt"
        content = llms_txt.read_text(encoding="utf-8")
        assert expected_url in content, (
            f"llms.txt doesn't yet link {expected_url} -- it's owned by another lane's "
            "rewrite of llms.txt, not this test; flag it rather than editing llms.txt here."
        )
