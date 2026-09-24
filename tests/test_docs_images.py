"""Image references across the README, Docker Hub README, docs, site templates and Unraid templates.

Two failures nothing else catches: a reference to an image that isn't there (Jekyll and GitHub both
render a broken picture without failing the build), and an image in docs/images/ or
docs/assets/img/ that nothing uses (dead weight, or a stray capture). Sources write references in
two shapes, each with its own pattern; one that missed a shape would hide a whole file's worth.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

from scripts.generate_llms_full import excluded_folders

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
IMAGE_DIRS = {"images": DOCS / "images", "assets/img": DOCS / "assets" / "img"}
EXT = r"(?:png|jpe?g|webp|svg|gif|ico)"
# images/x.webp or assets/img/x.svg after any prefix: Markdown, HTML src/href, Liquid
# '/images/x.webp' | relative_url, and raw.githubusercontent.com / github.com URLs.
PATH_REF = re.compile(rf"(?<![\w-])(images|assets/img)/([A-Za-z0-9._-]+\.{EXT})\b")
# _data/*.yml names a bare file that the layout prefixes with /images/: `image: tour-extract.webp`.
DATA_REF = re.compile(rf"^\s*-?\s*image:\s*['\"]?([A-Za-z0-9._-]+\.{EXT})['\"]?\s*$", re.MULTILINE)
MAX_BYTES = 500 * 1024  # .pre-commit-config.yaml check-added-large-files --maxkb=500
SOCIAL_TEMPLATE = REPO_ROOT / "tests" / "e2e" / "snapshots" / "assets" / "social_preview.html"


def _sources() -> list[Path]:
    fixed = [REPO_ROOT / "README.md", REPO_ROOT / "DOCKERHUB_README.md", DOCS / "_config.yml", DOCS / "404.html"]
    globbed = [
        *DOCS.glob("_data/*.yml"),
        *DOCS.glob("_layouts/*.html"),
        *DOCS.glob("_includes/*.html"),
        *(REPO_ROOT / "unraid-templates").glob("*.xml"),
        *(p for p in DOCS.rglob("*.md") if not excluded_folders(DOCS) & set(p.relative_to(DOCS).parts)),
    ]
    return sorted({path for path in fixed + globbed if path.is_file()})


def _references(source: Path) -> set[Path]:
    text = source.read_text(encoding="utf-8", errors="ignore")
    refs = {IMAGE_DIRS[folder] / name for folder, name in PATH_REF.findall(text)}
    if source.parent.name == "_data":
        refs |= {IMAGE_DIRS["images"] / name for name in DATA_REF.findall(text)}
    return refs


def _committed_images() -> list[Path]:
    folders = [folder for folder in IMAGE_DIRS.values() if folder.is_dir()]
    return sorted(path for folder in folders for path in folder.iterdir() if path.is_file())


@pytest.mark.parametrize("source", _sources(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_every_referenced_image_exists(source: Path) -> None:
    missing = sorted(p.relative_to(REPO_ROOT).as_posix() for p in _references(source) if not p.is_file())
    assert not missing, f"{source.relative_to(REPO_ROOT)} points at images that don't exist: {missing}"


def test_no_committed_image_is_unreferenced() -> None:
    referenced = set().union(*(_references(source) for source in _sources()))
    orphans = [p.relative_to(REPO_ROOT).as_posix() for p in _committed_images() if p not in referenced]
    assert not orphans, f"committed but referenced nowhere: {orphans}"


def test_committed_images_fit_under_the_pre_commit_cap() -> None:
    big = [
        f"{p.relative_to(REPO_ROOT)} ({p.stat().st_size // 1024} KB)"
        for p in _committed_images()
        if p.stat().st_size > MAX_BYTES
    ]
    assert not big


def test_social_preview_is_a_1280x640_baseline_jpeg_under_300kb() -> None:
    path = DOCS / "images" / "social-preview.jpg"
    with Image.open(path) as image:
        assert image.format == "JPEG"
        assert image.size == (1280, 640)
        assert not image.info.get("progressive") and not image.info.get("progression")
    assert path.stat().st_size <= 300 * 1024


def test_social_card_template_draws_the_current_logo() -> None:
    template = SOCIAL_TEMPLATE.read_text(encoding="utf-8")
    logo = (DOCS / "assets" / "img" / "logo.svg").read_text(encoding="utf-8")
    frame = re.search(r'<path d="(M25 8H51[^"]+)"', logo).group(1)
    assert frame in template
    assert 'id="players"' in template
