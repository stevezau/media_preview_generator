#!/usr/bin/env python3
"""Build docs/llms-full.txt: every docs page's text in one file, for AI agents.

docs/llms.txt is the index (what this is, how it differs, a described list of links). This is the
companion an agent fetches when it wants the whole corpus in one request. Ported from the sibling
project's scripts/build_llms_full.py (Shortlist):

- Page order comes from `nav` in docs/_config.yml, the list that drives the sidebar, pager and
  breadcrumbs. A published page missing from nav is an error, never a silent drop.
- The landing page is built from docs/_data/*.yml, so its stats, tour, features, works-with and FAQ
  are flattened from those files into the home section. Editing a data file changes this output,
  and the drift test catches a forgotten regeneration.
- Front matter, Liquid comments, JSON-LD, HTML comments and heading anchors are stripped; `.md` and
  image links (inline, titled or reference-style definitions) become absolute site URLs, resolved
  from the linking page's own folder. Site paths such as /faq/ get the site's root in front.

Committed rather than built by Jekyll, so it can be diffed in review. Jekyll serves it verbatim at
/llms-full.txt (`render_with_liquid: false`: the docs quote template syntax such as {{Item.Path}}
that Liquid would otherwise eat).

Usage:
    python scripts/generate_llms_full.py           # write docs/llms-full.txt
    python scripts/generate_llms_full.py --check   # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
OUTPUT_NAME = "llms-full.txt"
OUTPUT_FRONT_MATTER = "---\nlayout: null\nsitemap: false\npermalink: /llms-full.txt\nrender_with_liquid: false\n---\n"

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
LIQUID_COMMENT = re.compile(r"\{%-?\s*comment\s*-?%\}.*?\{%-?\s*endcomment\s*-?%\}", re.DOTALL)
JSON_LD = re.compile(r'<script type="application/ld\+json">.*?</script>', re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Explicit heading anchors (`## Connection  <a id="connection"></a>`): markup, not prose.
ANCHOR_TAG = re.compile(r'[ \t]*<a id="[^"]*"></a>')
INCLUDE = re.compile(r"\{%-?\s*include\s+([\w.-]+)\s*-?%\}")
RELATIVE_URL = re.compile(r"""\{\{\s*['"]([^'"]+)['"]\s*\|\s*relative_url\s*\}\}""")
PAGE_VAR = re.compile(r"\{\{\s*page\.(\w+)(?:\s*\|[^}]*)?\}\}")
SITE_VAR = re.compile(r"\{\{\s*site\.(\w+)(?:\s*\|[^}]*)?\}\}")
LEFTOVER_LIQUID = re.compile(r"\{%|\{\{\s*(?:site|page)\.")
HTML_TAG = re.compile(r"<[^>]+>")
SVG = re.compile(r"<svg\b.*?</svg>", re.DOTALL)
# `[text](target)` and `![alt](target)`, optionally with a "title" or 'title' kept as written.
# Targets in these docs never contain spaces.
MD_LINK = re.compile(r"""(!?\[[^\]]*\]\()([^)\s]+)((?:\s+(?:"[^"]*"|'[^']*'))?\))""")
# A reference-style definition, `[label]: target "optional title"`; `[^1]:` is a footnote.
LINK_DEFINITION = re.compile(r"^( {0,3}\[(?!\^)[^\]]+\]:[ \t]*)(\S+)", re.MULTILINE)
SITE_ABSOLUTE_LINK = re.compile(r"\]\(/")
BLANK_RUN = re.compile(r"\n{3,}")
SKIP_TARGET = ("http://", "https://", "mailto:", "{{")


class LlmsFullError(RuntimeError):
    """The docs can't be flattened faithfully: a page missing from nav, a dead link, unknown Liquid."""


def load_config(docs_dir: Path) -> dict:
    """Parse docs/_config.yml."""
    return yaml.safe_load((docs_dir / "_config.yml").read_text(encoding="utf-8"))


def site_root(config: dict) -> str:
    """The site's absolute root URL with a trailing slash, e.g. https://mediapreviewgenerator.dev/."""
    return f"{config['url']}{config.get('baseurl') or ''}/"


def nav_urls(config: dict) -> list[str]:
    """Every page URL in nav, depth-first: the site's own reading order."""
    urls: list[str] = []
    for entry in config.get("nav", []):
        urls.append(entry["url"])
        urls.extend(child["url"] for child in entry.get("children", []))
    return urls


def excluded_folders(docs_dir: Path) -> set[str]:
    """Top-level folders _config.yml's `exclude:` keeps off the site, e.g. {"design", "benchmark"}.

    Args:
        docs_dir: The Jekyll source folder.

    Returns:
        Folder names without their trailing slash.
    """
    return {entry.rstrip("/") for entry in load_config(docs_dir).get("exclude") or [] if entry.endswith("/")}


def published_sources(docs_dir: Path) -> list[Path]:
    """Markdown files Jekyll publishes: not docs/README.md, excluded folders or Jekyll's own folders."""
    excluded = excluded_folders(docs_dir)
    sources = []
    for path in sorted(docs_dir.rglob("*.md")):
        parts = path.relative_to(docs_dir).parts
        if path.name == "README.md" and len(parts) == 1:
            continue
        if parts[0] in excluded or any(part.startswith(("_", ".")) for part in parts):
            continue
        sources.append(path)
    return sources


def _page_url(docs_dir: Path, source: Path, root: str) -> str:
    slug = source.relative_to(docs_dir).with_suffix("").as_posix()
    return root if slug == "index" else f"{root}{slug}/"


def _format(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return f"{value.day} {value:%B %Y}"
    return str(value)


def _resolve(target: str, source: Path, docs_dir: Path, root: str) -> str:
    """One relative link or image target to an absolute site URL."""
    path, suffix = re.match(r"([^?#]*)(.*)", target).groups()
    if not path:  # "#anchor" on the same page
        return _page_url(docs_dir, source, root) + suffix
    if path.startswith("/"):  # a site path, e.g. /faq/: Jekyll's permalinks decide it, as in the link test
        return root + target.lstrip("/")
    resolved = (source.parent / path).resolve()
    where = source.relative_to(docs_dir)
    if docs_dir not in resolved.parents:
        raise LlmsFullError(f"{where}: {target!r} points outside docs/")
    if not resolved.exists():
        raise LlmsFullError(f"{where}: {target!r} does not exist")
    relative = resolved.relative_to(docs_dir).as_posix()
    if relative == "README.md":
        return root + suffix  # the GitHub docs hub; its job on the site is the home page's
    if resolved.suffix == ".md":
        return _page_url(docs_dir, resolved, root) + suffix
    return root + relative + suffix


def _absolute_links(body: str, source: Path, docs_dir: Path, root: str) -> str:
    def _one(match: re.Match[str]) -> str:
        target = match.group(2)
        if target.startswith(SKIP_TARGET):
            return match.group(0)
        after = match.string[match.end(2) : match.end()]  # an inline link's title and ")"; empty for a definition
        return f"{match.group(1)}{_resolve(target, source, docs_dir, root)}{after}"

    return LINK_DEFINITION.sub(_one, MD_LINK.sub(_one, body))


def _include_as_text(docs_dir: Path, name: str) -> str:
    """Flatten an HTML include to its readable text, one block per line."""
    raw = (docs_dir / "_includes" / name).read_text(encoding="utf-8")
    raw = SVG.sub("", LIQUID_COMMENT.sub("", raw))
    raw = re.sub(r"</(li|figcaption|p|div|h[1-6])>", "\n", raw)
    text = HTML_TAG.sub("", raw).replace("&rsquo;", "'").replace("&amp;", "&").replace("&nbsp;", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _render(body: str, front: dict, config: dict, source: Path, docs_dir: Path, root: str) -> str:
    body = ANCHOR_TAG.sub("", HTML_COMMENT.sub("", body))
    if front.get("render_with_liquid") is not False:
        body = JSON_LD.sub("", LIQUID_COMMENT.sub("", body))
        body = INCLUDE.sub(lambda m: _include_as_text(docs_dir, m.group(1)), body)
        body = RELATIVE_URL.sub(lambda m: root + m.group(1).lstrip("/"), body)
        body = SITE_VAR.sub(lambda m: str(config.get(m.group(1), m.group(0))), body)
        body = PAGE_VAR.sub(lambda m: _format(front.get(m.group(1))), body)
        leftover = LEFTOVER_LIQUID.search(body)
        if leftover:
            raise LlmsFullError(
                f"{source.relative_to(docs_dir)}: Liquid this script can't flatten: {leftover.group(0)!r}"
            )
    body = _absolute_links(body, source, docs_dir, root)
    return BLANK_RUN.sub("\n\n", body).strip()


def _data(docs_dir: Path, name: str) -> list:
    path = docs_dir / "_data" / f"{name}.yml"
    if not path.is_file():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _squash(text: object) -> str:
    return " ".join(str(text).split())


def _landing_text(docs_dir: Path, root: str) -> str:
    """The landing page's data-driven sections, in page order, as Markdown."""
    parts: list[str] = []
    if stats := _data(docs_dir, "stats"):
        rows = [f"- **{s['value']}{s.get('unit') or ''} {s['label']}**: {_squash(s['note'])}" for s in stats]
        parts.append("## At a glance\n\n" + "\n".join(rows))
    if tour := _data(docs_dir, "tour"):
        rows = [f"{i}. **{s['title']}.** {_squash(s['body'])}" for i, s in enumerate(tour, 1)]
        parts.append("## How it works\n\n" + "\n".join(rows))
    if features := _data(docs_dir, "features"):
        parts.append("## Features\n\n" + "\n".join(f"- **{f['title']}.** {_squash(f['body'])}" for f in features))
    if works := _data(docs_dir, "works_with"):
        rows = [
            f"- **{g['group']}:** " + "; ".join(f"{i['name']} ({_squash(i['note'])})" for i in g["items"])
            for g in works
        ]
        parts.append("## Works with\n\n" + "\n".join(rows))
    if faq := _data(docs_dir, "faq"):
        parts.append("## Common questions\n\n" + "\n\n".join(f"**{q['q']}**\n{_squash(q['a'])}" for q in faq))
    # Data files can't use .md links (jekyll-relative-links only rewrites pages): they write site paths.
    return SITE_ABSOLUTE_LINK.sub(f"]({root}", "\n\n".join(parts))


def _page_section(source: Path, config: dict, docs_dir: Path, root: str) -> str:
    raw = source.read_text(encoding="utf-8")
    match = FRONT_MATTER.match(raw)
    if not match:
        raise LlmsFullError(f"{source.relative_to(docs_dir)} has no front matter")
    front = yaml.safe_load(match.group(1)) or {}
    if missing := [key for key in ("title", "description") if not front.get(key)]:
        raise LlmsFullError(f"{source.relative_to(docs_dir)}: front matter has no {' or '.join(missing)}")
    body = _render(raw[match.end() :], front, config, source, docs_dir, root)
    if source == docs_dir / "index.md":
        body = "\n\n".join(part for part in (body, _landing_text(docs_dir, root)) if part)
    parts = [f"# {front['title']}", f"Source: {_page_url(docs_dir, source, root)}", "", _squash(front["description"])]
    if front.get("facts_checked"):
        parts.append(f"Facts checked: {_format(front['facts_checked'])}.")
    if body:
        parts += ["", body]
    return "\n".join(parts)


def generate(docs_dir: Path = DOCS_DIR) -> str:
    """Build the full llms-full.txt content (front matter included, one trailing newline).

    Args:
        docs_dir: The Jekyll source folder (tests pass an edited copy).

    Returns:
        The file content.

    Raises:
        LlmsFullError: A page is missing from nav, nav names a missing page, a page has no title or
            description, a link is dead or leaves docs/, or a page uses Liquid this script doesn't
            flatten.
    """
    docs_dir = docs_dir.resolve()
    config = load_config(docs_dir)
    root = site_root(config)
    ordered = [docs_dir / "index.md"] + [docs_dir / f"{url.strip('/')}.md" for url in nav_urls(config)]
    if absent := [p.relative_to(docs_dir).as_posix() for p in ordered if not p.is_file()]:
        raise LlmsFullError(f"nav names pages that don't exist: {', '.join(absent)}")
    if unlisted := [p.relative_to(docs_dir).as_posix() for p in published_sources(docs_dir) if p not in ordered]:
        raise LlmsFullError(f"page(s) not in _config.yml nav, so they would be dropped: {', '.join(unlisted)}")
    index = FRONT_MATTER.sub("", (docs_dir / "llms.txt").read_text(encoding="utf-8"), count=1)
    header = index.split("\n## Docs")[0].rstrip()
    sections = [_page_section(page, config, docs_dir, root) for page in ordered]
    return (
        OUTPUT_FRONT_MATTER
        + f"{header}\n\n# Full documentation\n\n"
        + f"Every page of the {config['title']} documentation, in the site's own reading order. "
        + f"The index version of this file is at {root}llms.txt\n\n"
        + "\n\n---\n\n".join(sections)
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: write docs/llms-full.txt, or with --check report whether it is stale."""
    parser = argparse.ArgumentParser(description="Build docs/llms-full.txt from the docs site.")
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = parser.parse_args(argv)
    try:
        content = generate()
    except LlmsFullError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = DOCS_DIR / OUTPUT_NAME
    if args.check:
        if not out.is_file() or out.read_text(encoding="utf-8") != content:
            print(f"{out} is stale. Regenerate with:\n    python scripts/generate_llms_full.py", file=sys.stderr)
            return 1
        print(f"{out} is up to date.")
        return 0
    out.write_text(content, encoding="utf-8")
    print(f"wrote {out}: {len(content):,} bytes, {content.count(chr(10) + 'Source: ')} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
