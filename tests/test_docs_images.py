"""Image reference integrity for README/docs/mkdocs — and the social preview.

Two things are easy to get wrong when screenshots get renamed or added:
1. A doc references an image that no longer exists (typo, forgot to rename
   the reference alongside the file, deleted an image but left the link).
2. An image sits in docs/images/ that nothing references any more (dead
   weight — or worse, a leftover with real data in it).

This scans README.md, DOCKERHUB_README.md, docs/**/*.md (excluding
docs/design/ — internal planning docs, not shipped), mkdocs.yml, and
docs_theme/** for image references and checks both directions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
IMAGES_DIR = DOCS_DIR / "images"

IMAGE_EXTS = (".png", ".webp", ".jpg", ".jpeg", ".svg", ".gif", ".ico")

# Markdown ``![alt](path)``, HTML ``src="..."``/``href="..."``, and a bare
# YAML ``key: path/to/image.ext`` (covers mkdocs.yml's logo/favicon/social_image).
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_HTML_ATTR_RE = re.compile(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']')
_YAML_IMG_RE = re.compile(r"[:=]\s*([^\s'\"#]+\.(?:png|webp|jpe?g|svg|gif|ico))\b")

# Absolute GitHub URLs that point back into this same repo — mapped back to
# a repo-relative path rather than treated as an unresolvable external link.
_RAW_GITHUB_RE = re.compile(r"https?://raw\.githubusercontent\.com/stevezau/media_preview_generator/[^/]+/(.+)")
_BLOB_GITHUB_RE = re.compile(r"https?://github\.com/stevezau/media_preview_generator/(?:raw|blob)/[^/]+/(.+)")

_THEME_SCAN_SUFFIXES = (".py", ".html", ".css", ".js", ".yml", ".yaml", ".md")


def _extract_refs(text: str) -> list[str]:
    return _MD_IMAGE_RE.findall(text) + _HTML_ATTR_RE.findall(text) + _YAML_IMG_RE.findall(text)


def _is_image_ref(ref: str) -> bool:
    bare = ref.split("#", 1)[0].split("?", 1)[0]
    return bare.lower().endswith(IMAGE_EXTS)


def _resolve_ref(ref: str, source_file: Path) -> Path | None:
    """Resolve a raw reference string found in ``source_file`` to a repo path.

    Returns None for references we intentionally don't check — external
    hosts unrelated to this repo (shields.io badges, etc.).
    """
    bare = ref.split("#", 1)[0].split("?", 1)[0]

    m = _RAW_GITHUB_RE.match(bare) or _BLOB_GITHUB_RE.match(bare)
    if m:
        return REPO_ROOT / m.group(1)

    if bare.startswith(("http://", "https://")):
        return None

    if bare.startswith("/"):
        return REPO_ROOT / bare.lstrip("/")

    if source_file.name == "mkdocs.yml":
        base = DOCS_DIR  # mkdocs.yml paths (logo/favicon/social_image) are relative to docs_dir
    else:
        base = source_file.parent

    return (base / bare).resolve()


def _all_doc_sources() -> list[Path]:
    sources = [REPO_ROOT / "README.md", REPO_ROOT / "DOCKERHUB_README.md"]
    if DOCS_DIR.exists():
        for md in sorted(DOCS_DIR.rglob("*.md")):
            if "design" in md.relative_to(DOCS_DIR).parts:
                continue
            sources.append(md)
    mkdocs_yml = REPO_ROOT / "mkdocs.yml"
    if mkdocs_yml.exists():
        sources.append(mkdocs_yml)
    theme_dir = REPO_ROOT / "docs_theme"
    if theme_dir.exists():
        sources.extend(p for p in sorted(theme_dir.rglob("*")) if p.is_file() and p.suffix in _THEME_SCAN_SUFFIXES)
    return [p for p in sources if p.exists()]


def _scan(sources: list[Path]) -> dict[Path, list[tuple[str, Path | None]]]:
    """Map each source file to its (raw_ref, resolved_path_or_None) image refs."""
    result: dict[Path, list[tuple[str, Path | None]]] = {}
    for src in sources:
        text = src.read_text(encoding="utf-8", errors="ignore")
        refs = [ref for ref in _extract_refs(text) if _is_image_ref(ref)]
        result[src] = [(ref, _resolve_ref(ref, src)) for ref in refs]
    return result


class TestReferencedImagesExist:
    def test_no_broken_image_references_when_scanning_docs(self):
        # Arrange
        scan = _scan(_all_doc_sources())

        # Act
        missing = [
            f"{src.relative_to(REPO_ROOT)}: {raw!r} -> {resolved}"
            for src, refs in scan.items()
            for raw, resolved in refs
            if resolved is not None and not resolved.exists()
        ]

        # Assert
        assert not missing, "Referenced images that don't exist on disk:\n" + "\n".join(missing)


class TestDocsImagesAllReferenced:
    def test_no_unreferenced_files_when_scanning_docs_images_dir(self):
        # Arrange
        if not IMAGES_DIR.exists():
            pytest.skip("docs/images/ does not exist")
        scan = _scan(_all_doc_sources())

        # Act
        referenced = {resolved.resolve() for refs in scan.values() for _raw, resolved in refs if resolved is not None}
        actual_images = {p.resolve() for p in IMAGES_DIR.iterdir() if p.is_file()}
        unreferenced = actual_images - referenced

        # Assert
        assert not unreferenced, (
            "Files in docs/images/ that no README/docs/mkdocs/theme reference points at:\n"
            + "\n".join(sorted(str(p.relative_to(REPO_ROOT)) for p in unreferenced))
        )


class TestSocialPreviewImage:
    def test_social_preview_is_2to1_baseline_jpeg_when_present(self):
        # Arrange
        path = IMAGES_DIR / "social-preview.jpg"
        assert path.exists(), "docs/images/social-preview.jpg is missing"

        # Act
        with Image.open(path) as im:
            fmt = im.format
            width, height = im.size
            progressive = im.info.get("progressive")
            progression = im.info.get("progression")

        # Assert
        assert fmt == "JPEG"
        assert width == 2 * height, f"expected a 2:1 aspect ratio, got {width}x{height}"
        assert not progressive
        assert not progression
