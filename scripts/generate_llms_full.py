#!/usr/bin/env python3
"""Build ``llms-full.txt``: every published docs page concatenated into one file.

The page set and order come from ``mkdocs.yml``'s ``nav`` (loaded the way MkDocs
itself loads it, so the custom ``!!python/object/apply`` slugify tag doesn't need
reimplementing) via MkDocs's own ``get_files``/``get_navigation``, which already
honours ``exclude_docs``. Nothing here hardcodes a page list.

``docs_theme/hooks.py`` copies the repo-root ``llms.txt`` and ``llms-full.txt``
into the built site root at ``on_post_build``, so this script only needs to write
the repo-root file; MkDocs ships it verbatim.

Usage:
    python scripts/generate_llms_full.py            # write repo-root llms-full.txt
    python scripts/generate_llms_full.py --check     # exit 1 if the committed file is stale

Failure mode:
    Raises loudly (``IncludeSyntaxError``, ``LinkResolutionError``) rather than
    silently dropping or mis-linking content -- an agent reading llms-full.txt
    has no way to notice a quietly broken link or a flattened-away include.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

from mkdocs.config import load_config
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import Files, get_files
from mkdocs.structure.nav import get_navigation
from mkdocs.structure.pages import Page

REPO_ROOT = Path(__file__).resolve().parent.parent
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"
LLMS_TXT = REPO_ROOT / "llms.txt"
OUTPUT_PATH = REPO_ROOT / "llms-full.txt"

# Kept in sync with docs_theme/hooks.py's GITHUB_DOCS_HUB: docs/README.md is the
# GitHub docs hub, excluded from the site, whose role on the site belongs to index.md.
GITHUB_DOCS_HUB = "README.md"

# pymdownx.snippets (`--8<--`) and Jinja `{% include %}` / `{% macro %}` pull in
# content this script can't see just by reading the page's own file. None of the
# current docs use them (checked by hand); if one starts to, this must fail loudly
# rather than silently ship a page with a hole in it.
_INCLUDE_RE = re.compile(r"--8<--|\{%[-+]?\s*(?:include|macro)\b")

_FRONT_MATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)

# `[text](target)` and `![alt](target)`; target excludes whitespace and an
# optional `"title"`, which none of the docs currently use.
_MARKDOWN_LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^)\s]+)(\))")

_SEPARATOR_BAR = "=" * 80


class IncludeSyntaxError(RuntimeError):
    """Raised when a page uses include/snippet syntax this script can't flatten."""


class LinkResolutionError(RuntimeError):
    """Raised when a relative link in a page can't be resolved to a built URL."""


def load_mkdocs_config() -> MkDocsConfig:
    """Load ``mkdocs.yml`` the way MkDocs itself does (handles the custom YAML tags)."""
    return load_config(str(MKDOCS_YML))


def nav_pages(config: MkDocsConfig) -> tuple[list[Page], Files]:
    """Return the published pages in nav order, plus the full build ``Files`` set.

    Args:
        config: The loaded MkDocs config.

    Returns:
        The nav's pages in traversal order, and the ``Files`` collection used to
        resolve links (image/page targets not in the nav, e.g. non-nav assets).
    """
    files = get_files(config)
    navigation = get_navigation(files, config)
    return list(navigation.pages), files


def _page_url(config: MkDocsConfig, url: str) -> str:
    """Join a MkDocs-relative file URL (e.g. ``"getting-started/"``) onto ``site_url``."""
    return urljoin(config.site_url, url)


def _strip_noise(markdown: str) -> str:
    """Strip front matter, HTML comments, ``<script>`` blocks and Jinja comments."""
    markdown = _FRONT_MATTER_RE.sub("", markdown)
    markdown = _HTML_COMMENT_RE.sub("", markdown)
    markdown = _SCRIPT_BLOCK_RE.sub("", markdown)
    markdown = _JINJA_COMMENT_RE.sub("", markdown)
    return markdown


def _resolve_links(markdown: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    """Rewrite every relative Markdown link/image target to an absolute site URL.

    ``x.md``, ``../x.md``, ``x.md#anchor`` and image paths resolve to
    ``site_url``-absolute URLs, the same way the built site resolves them.
    Links to ``docs/README.md`` retarget to the site root, mirroring
    ``docs_theme/hooks.py``'s ``on_page_markdown``. Absolute http(s) links,
    ``mailto:`` links and pure ``#anchor`` links are left alone, except a pure
    ``#anchor`` is made absolute to this page's own URL for consistency.

    Args:
        markdown: The page's Markdown, already stripped of front matter/comments.
        page: The page being processed (for its directory, to resolve relative targets).
        config: The loaded MkDocs config.
        files: The full build ``Files`` collection, to resolve a target to its built URL.

    Returns:
        The Markdown with every relative link/image target made absolute.

    Raises:
        LinkResolutionError: A relative target doesn't resolve to any file MkDocs builds.
    """
    page_dir = posixpath.dirname(page.file.src_uri)
    own_url = _page_url(config, page.file.url)

    def _retarget(match: re.Match[str]) -> str:
        prefix, target, suffix = match.group(1), match.group(2), match.group(3)
        path_part, has_anchor, anchor = target.partition("#")

        if "://" in path_part or path_part.startswith("mailto:"):
            return match[0]

        if not path_part:
            # Pure "#anchor": make it absolute to this page for a self-contained file.
            return f"{prefix}{own_url}#{anchor}{suffix}" if has_anchor else match[0]

        normalized = (
            posixpath.normpath(posixpath.join(page_dir, path_part)) if page_dir else posixpath.normpath(path_part)
        )

        if normalized == GITHUB_DOCS_HUB:
            resolved = _page_url(config, files.src_uris["index.md"].url)
        else:
            target_file = files.src_uris.get(normalized)
            if target_file is None:
                raise LinkResolutionError(
                    f"{page.file.src_uri}: link target {target!r} resolves to "
                    f"{normalized!r}, which is not a file MkDocs builds"
                )
            resolved = _page_url(config, target_file.url)

        if has_anchor:
            resolved = f"{resolved}#{anchor}"
        return f"{prefix}{resolved}{suffix}"

    return _MARKDOWN_LINK_RE.sub(_retarget, markdown)


def _render_page(page: Page, config: MkDocsConfig, files: Files) -> str:
    """Read, check, clean and link-resolve one page's Markdown source."""
    raw = Path(page.file.abs_src_path).read_text(encoding="utf-8")
    if _INCLUDE_RE.search(raw):
        raise IncludeSyntaxError(
            f"{page.file.src_uri}: uses include/snippet syntax (--8<-- or a Jinja "
            "include/macro) that generate_llms_full.py doesn't flatten yet. Extend "
            "_render_page() to inline it before regenerating, don't drop the content."
        )
    cleaned = _strip_noise(raw)
    resolved = _resolve_links(cleaned, page, config, files)
    return resolved.strip()


def _page_separator(title: str, url: str) -> str:
    return f"\n\n{_SEPARATOR_BAR}\nPAGE: {title}\nURL: {url}\n{_SEPARATOR_BAR}\n\n"


def generate() -> str:
    """Build the full ``llms-full.txt`` content.

    Returns:
        The deterministic file content (header + every nav page), trailing newline included.
    """
    config = load_mkdocs_config()
    pages, files = nav_pages(config)

    header = _FRONT_MATTER_RE.sub("", LLMS_TXT.read_text(encoding="utf-8")).strip()
    # No trailing "." directly after the URL: a period glued onto the end would become
    # part of it when a reader (or a test) extracts every site_url-prefixed URL by regex.
    intro = f"This file concatenates every page of the docs at {config.site_url}"
    parts = [header, "", intro]

    for page in pages:
        url = _page_url(config, page.file.url)
        parts.append(_page_separator(page.title, url))
        parts.append(_render_page(page, config, files))

    return "\n".join(parts).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed llms-full.txt doesn't match the generator output.",
    )
    args = parser.parse_args(argv)

    content = generate()

    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.is_file() else None
        if current != content:
            print(
                f"{OUTPUT_PATH} is stale. Regenerate with:\n    python scripts/generate_llms_full.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT_PATH} is up to date.")
        return 0

    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
