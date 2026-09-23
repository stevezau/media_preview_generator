"""MkDocs build hooks for the documentation site.

Wired in via ``hooks:`` in ``mkdocs.yml``. Everything here derives from ``mkdocs.yml``
(``site_url``, ``site_name``, ...) and from the docs themselves, so there is no second
copy of any URL or FAQ text to keep in sync.
"""

from __future__ import annotations

import json
import posixpath
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import Files
from mkdocs.structure.nav import Navigation
from mkdocs.structure.pages import Page

FAQ_PAGE = "faq.md"
# docs/README.md is the docs hub on GitHub; on the site that role belongs to index.md.
GITHUB_DOCS_HUB = "README.md"
SITE_HOME = "index.md"
# Repo-root files served verbatim at the site root. llms-full.txt may not exist yet.
LLMS_FILES = ("llms.txt", "llms-full.txt")

_MARKDOWN_LINK = re.compile(r"\]\((?P<target>[^)\s#]+)(?P<anchor>#[^)\s]*)?\)")


def on_page_markdown(markdown: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    """Point links at the GitHub docs hub (docs/README.md) to the site home instead.

    docs/README.md is excluded from the site, so without this every "Back to Docs"
    link would 404 on the site while still working on GitHub.

    Args:
        markdown: The page's Markdown source.
        page: The page being built.
        config: The MkDocs config.
        files: All files in the build.

    Returns:
        The Markdown with hub links rewritten to index.md.
    """
    page_dir = posixpath.dirname(page.file.src_uri)

    def _retarget(match: re.Match[str]) -> str:
        target = match["target"]
        if "://" in target or posixpath.normpath(posixpath.join(page_dir, target)) != GITHUB_DOCS_HUB:
            return match[0]
        home = posixpath.relpath(SITE_HOME, page_dir or ".")
        return f"]({home}{match['anchor'] or ''})"

    return _MARKDOWN_LINK.sub(_retarget, markdown)


def on_page_context(context: dict[str, Any], page: Page, config: MkDocsConfig, nav: Navigation) -> dict[str, Any]:
    """Attach the page's JSON-LD blocks for ``main.html`` to print in ``<head>``.

    Args:
        context: The template context for this page.
        page: The page being rendered (its HTML is already in ``page.content``).
        config: The MkDocs config.
        nav: The site navigation.

    Returns:
        The context with ``structured_data``: a list of JSON strings, one per block.
    """
    blocks = [_software_application(config), _website(config)]
    if page.file.src_uri == FAQ_PAGE:
        # Google retired the FAQ rich result, so this earns no search snippet. It is kept
        # because AI crawlers and other indexes read schema.org types; do not treat it as
        # ranking work. Built from the rendered FAQ page itself, never a second copy.
        questions = extract_faq(page.content or "")
        if questions:
            blocks.append(_faq_page(questions))
    context["structured_data"] = [_script_safe_json(block) for block in blocks]
    return context


def on_post_build(config: MkDocsConfig) -> None:
    """Write robots.txt and copy the repo-root llms files into the site root.

    Args:
        config: The MkDocs config.
    """
    site_dir = Path(config.site_dir)
    # robots.txt only takes effect at a host root, which site_url is on the custom domain.
    robots = f"User-agent: *\nAllow: /\n\nSitemap: {config.site_url}sitemap.xml\n"
    (site_dir / "robots.txt").write_text(robots, encoding="utf-8")

    repo_root = Path(config.config_file_path).parent
    for name in LLMS_FILES:
        source = repo_root / name
        if source.is_file():
            shutil.copyfile(source, site_dir / name)


def extract_faq(html: str) -> list[tuple[str, str]]:
    """Pull (question, answer) pairs out of the rendered FAQ page.

    Each ``<h3>`` is a question; its answer is the text up to the next heading or ``<hr>``.

    Args:
        html: The rendered HTML of docs/faq.md.

    Returns:
        The question/answer pairs, in page order, as plain text.
    """
    parser = _FaqParser()
    parser.feed(html)
    parser.close()
    return parser.pairs


class _FaqParser(HTMLParser):
    _BLOCK_TAGS = frozenset({"p", "li", "tr", "td", "th", "br", "pre", "div", "ul", "ol", "table"})
    _ANSWER_ENDS = frozenset({"h1", "h2", "h4", "h5", "h6", "hr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pairs: list[tuple[str, str]] = []
        self._question: list[str] | None = None
        self._answer: list[str] = []
        self._in_question = False
        self._in_permalink = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h3":
            self._flush()
            self._question = []
            self._in_question = True
        elif tag in self._ANSWER_ENDS:
            self._flush()
        elif tag == "a" and "headerlink" in (dict(attrs).get("class") or "").split():
            self._in_permalink = True
        elif tag in self._BLOCK_TAGS:
            self._answer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3":
            self._in_question = False
        elif tag == "a":
            self._in_permalink = False

    def handle_data(self, data: str) -> None:
        if self._in_permalink or self._question is None:
            return
        (self._question if self._in_question else self._answer).append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        if self._question is not None:
            question = _squash(self._question)
            answer = _squash(self._answer)
            if question and answer:
                self.pairs.append((question, answer))
        self._question = None
        self._answer = []
        self._in_question = False


def _squash(chunks: list[str]) -> str:
    return " ".join("".join(chunks).split())


def _software_application(config: MkDocsConfig) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        # SoftwareSourceCode is what makes codeRepository a valid property here.
        "@type": ["SoftwareApplication", "SoftwareSourceCode"],
        "name": config.site_name,
        "description": config.site_description,
        "url": config.site_url,
        "applicationCategory": "MultimediaApplication",
        "operatingSystem": "Linux (Docker)",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "license": "https://opensource.org/licenses/MIT",
        "author": {"@type": "Person", "name": config.site_author, "url": config.extra["author_url"]},
        "downloadUrl": config.extra["download_url"],
        "codeRepository": config.repo_url,
    }


def _website(config: MkDocsConfig) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": config.site_name,
        "description": config.site_description,
        "url": config.site_url,
    }


def _faq_page(questions: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": question, "acceptedAnswer": {"@type": "Answer", "text": answer}}
            for question, answer in questions
        ],
    }


def _script_safe_json(data: dict[str, Any]) -> str:
    # Escaping "<" keeps any "</script>" inside the data from closing the tag early.
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
