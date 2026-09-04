"""Copy-direction guard for the thumbnail-quality scale (issue #286).

`thumbnail_quality` is handed verbatim to FFmpeg's `-q:v`, which is a
qscale: **lower is better**.  Every user-facing surface that named the
scale drifted the other way and shipped, because nothing pinned the
direction.  These tests cover each surface as its own cell — the two
templates and the Unraid CA template are independent copies that have
already drifted together once.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

REPO_ROOT = Path(__file__).resolve().parent.parent

QUALITY_COPY_SURFACES = {
    "settings.html": REPO_ROOT / "media_preview_generator/web/templates/settings.html",
    "setup.html": REPO_ROOT / "media_preview_generator/web/templates/setup.html",
    "unraid-template": REPO_ROOT / "unraid-templates/media-preview-generator.xml",
    "faq.md": REPO_ROOT / "docs/faq.md",
    "reference.md": REPO_ROOT / "docs/reference.md",
    "llms.txt": REPO_ROOT / "llms.txt",
}

# The wording each surface must carry, one entry per key above.
# ``test_every_surface_has_a_direction_row`` fails if the two drift apart.
DIRECTION_PHRASE = {
    "settings.html": "lower is better",
    "setup.html": "lower is better",
    "unraid-template": "lower = better quality",
    "faq.md": "lower numbers = higher quality",
    "reference.md": "lower = better quality",
    "llms.txt": "lower = sharper",
}

# Tells that the scale is being described the wrong way round.  Regexes,
# not fixed phrases: the original substring list missed a reworded FAQ
# sentence ("10 really is as sharp as it goes") that meant the opposite
# of the copy it replaced.
INVERTED_PATTERNS = (
    r"higher\s*(?:=|is)\s*better",
    r"\b10\s*(?:=|is|really is)(?![^.]{0,30}lower)[^.]{0,30}sharp",
    r"\braise\b[^.]{0,40}sharp(?:er|est)",
    r"\b1\s*=\s*smallest and blurriest",
    r"raise if thumbs look blocky",
    r"\b10\s*=\s*highest",
)


# How to carve the quality copy out of each surface.  Explicit windows
# rather than a keyword line-filter: the first version kept only lines
# containing "quality", which silently excluded the FAQ paragraph about
# the qscale clamp — inverting that paragraph left every test green.
# Every pattern here must match, so a template restructure fails loudly
# instead of shrinking the guarded region to nothing.
SURFACE_SCOPE: dict[str, tuple[str, ...]] = {
    "settings.html": (r'<label for="thumbnailQuality".*?</label>',),
    "setup.html": (
        r'<label for="thumbnailQuality".*?</label>',
        # setup.html puts the visible hint in a .form-text under the slider.
        r'<div class="form-text">\s*Quality:.*?</div>',
    ),
    "unraid-template": (r"^.*THUMBNAIL_QUALITY.*$",),
    # The whole FAQ answer, heading to the next bold heading.
    "faq.md": (r"\*\*What's thumbnail quality 1-10\?\*\*.*?(?=\n\*\*)",),
    "reference.md": (r"^.*[Pp]review quality.*$",),
    "llms.txt": (r"^.*thumbnail quality.*$",),
}


def _quality_copy(name: str) -> str:
    """Return only the copy that talks about thumbnail quality, lower-cased.

    Scoped rather than whole-file: these templates carry a ⓘ tooltip on
    nearly every control, so a whole-file match would let an unrelated
    "lower is better" elsewhere satisfy the guard while this control's
    copy re-inverted.
    """
    text = QUALITY_COPY_SURFACES[name].read_text(encoding="utf-8")
    chunks = []
    for pattern in SURFACE_SCOPE[name]:
        found = re.findall(pattern, text, re.DOTALL | re.MULTILINE)
        assert found, f"{name}: scope pattern matched nothing — {pattern!r}"
        chunks.extend(found)
    return "\n".join(chunks).lower()


def test_every_surface_has_a_scope() -> None:
    """A new surface must not be added without saying how to scope it."""
    assert set(SURFACE_SCOPE) == set(QUALITY_COPY_SURFACES), (
        f"surfaces without a scope pattern: {set(QUALITY_COPY_SURFACES) - set(SURFACE_SCOPE)}"
    )


@pytest.mark.parametrize("name", sorted(QUALITY_COPY_SURFACES))
def test_surface_never_claims_higher_is_better(name: str) -> None:
    """No user-facing surface may describe the qscale as higher-is-better."""
    text = _quality_copy(name)
    for pattern in INVERTED_PATTERNS:
        hit = re.search(pattern, text)
        assert not hit, f"{name} describes thumbnail quality backwards: {hit.group(0)!r} matches {pattern!r}"


@pytest.mark.parametrize(("name", "expected"), sorted(DIRECTION_PHRASE.items()))
def test_surface_states_the_direction(name: str, expected: str) -> None:
    """Each surface must state the direction, not merely avoid stating it wrong.

    Every key in ``QUALITY_COPY_SURFACES`` gets a row here — the first
    version of this file listed ``reference.md`` as a covered surface but
    never asserted on it, so inverting that table cell stayed green.
    """
    assert expected in _quality_copy(name), f"{name} no longer states the quality direction ('{expected}')"


def test_every_surface_has_a_direction_row() -> None:
    """Guard the guard: a new surface must not be added without a cell."""
    assert set(DIRECTION_PHRASE) == set(QUALITY_COPY_SURFACES), (
        f"surfaces without a direction assertion: {set(QUALITY_COPY_SURFACES) - set(DIRECTION_PHRASE)}"
    )


@pytest.mark.parametrize("name", ["settings.html", "setup.html"])
def test_visible_label_hints_the_direction_without_hovering(name: str) -> None:
    """The always-visible label carries the hint; the tooltip is not the only source."""
    assert "(lower = sharper)" in _quality_copy(name), f"{name} lost the visible lower-is-sharper hint"


def test_setting_reaches_ffmpeg_as_qscale(tmp_path) -> None:
    """Anchor the copy to the behaviour it describes.

    Asserted at the ``subprocess.Popen`` boundary rather than by grepping
    ``ffmpeg_runner.py``: a source-text match would break on a ``ruff
    format`` reflow and would still pass if a later ``args +=`` overrode
    ``-q:v``. If this fails the mapping changed and every string above
    needs re-deriving.
    """
    config = SimpleNamespace(
        ffmpeg_path="/usr/bin/ffmpeg",
        plex_bif_frame_interval=10,
        thumbnail_quality=7,
        log_level="INFO",
        ffmpeg_threads=1,
    )
    captured: list[list[str]] = []

    def popen_side_effect(cmd, *_args, **_kwargs):
        captured.append(cmd)
        proc = MagicMock()
        proc.poll.side_effect = [0, 0, 0, 0]
        proc.returncode = 0
        proc.pid = 4242
        return proc

    with patch("subprocess.Popen", side_effect=popen_side_effect):
        runner = create_ffmpeg_runner(
            video_file="/fake/source.mkv",
            output_folder=str(tmp_path),
            gpu=None,
            gpu_device_path=None,
            config=config,
            progress_callback=None,
            ffmpeg_threads_override=None,
            cancel_check=None,
            pause_check=None,
            path_kind="sdr",
            libplacebo_vf=None,
            use_libplacebo=False,
            dv5_software_fallback=False,
            base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
            fps_filter="fps=fps=0.1:round=up",
            hdr10_zscale_chain="",
        )
        runner(use_skip=False, init_vulkan=False)

    assert captured, "subprocess.Popen was never called"
    cmd = captured[0]
    assert "-q:v" in cmd, f"FFmpeg argv carries no -q:v: {cmd}"
    # The last -q:v wins if it were ever set twice; assert on that one.
    last = len(cmd) - 1 - cmd[::-1].index("-q:v")
    assert cmd[last + 1] == "7", f"thumbnail_quality=7 did not reach -q:v: {cmd}"
