"""Pure helpers for detecting Dolby Vision / HDR content and the FFmpeg
stderr signatures that mean our tonemap filter fell over.

Side-effect-free functions of their inputs (media-metadata fields or
a list of stderr lines). Grouping them here keeps the detection
vocabulary in one place so :func:`generate_images` stays focused on
pipeline orchestration.
"""

from __future__ import annotations

import re

# MediaInfo's names for the two HDR transfer curves: PQ (SMPTE ST 2084)
# and HLG (ARIB STD-B67).  The standards' own names are accepted too in
# case a MediaInfo build reports those instead of the short names.
_HDR_TRANSFERS = frozenset({"pq", "smpte st 2084", "hlg", "arib std-b67"})


def is_hdr_transfer(transfer_characteristics: str | None) -> bool:
    """Detect an HDR transfer curve (PQ or HLG).

    MediaInfo only fills ``HDR_Format`` when the file carries HDR
    metadata (mastering display, HDR10+, Dolby Vision).  A PQ or HLG
    stream without that metadata is still HDR and still needs tone
    mapping, so the transfer curve is the fallback signal.  Primaries
    alone are not: BT.2020 primaries with a BT.709 / BT.2020 transfer
    are SDR.

    Args:
        transfer_characteristics: Value of
            ``MediaInfo.video_tracks[0].transfer_characteristics``.

    Returns:
        bool: ``True`` for PQ or HLG, ``False`` otherwise.
    """
    if not transfer_characteristics:
        return False
    return transfer_characteristics.strip().lower() in _HDR_TRANSFERS


def is_dolby_vision(hdr_format: str | None) -> bool:
    """Detect any Dolby Vision content (any profile).

    Used to identify DV content so the caller can choose the correct
    tone-mapping strategy:

    * DV Profile 5 (no backward-compat HDR10 layer) — requires libplacebo.
    * DV Profile 7/8 (with HDR10 fallback) — the standard zscale/tonemap
      chain reads the HDR10 base layer and works correctly.

    Args:
        hdr_format: Value of ``MediaInfo.video_tracks[0].hdr_format``.

    Returns:
        bool: ``True`` if Dolby Vision metadata is present, ``False`` otherwise.
    """
    if not hdr_format or hdr_format == "None":
        return False

    return "dolby vision" in hdr_format.lower()


def is_dv_no_backward_compat(hdr_format: str | None, transfer_characteristics: str | None) -> bool:
    """Detect Dolby Vision content without a backward-compatible base layer (Profile 5).

    A Profile 5 base layer is IPTPQc2: its stream declares no transfer
    curve, so the zscale/tonemap chain cannot read it and only
    libplacebo's Dolby Vision reshaping gives correct colours.  Profiles
    7, 8.1, 8.4 and AV1 Profile 10 carry an HDR10 or HLG base layer and
    declare PQ or HLG, which the zscale chain tone maps directly.

    The transfer curve is the signal, not ``hdr_format``: MediaInfo keeps
    the profile tag (``dvhe.05``) in ``HDR_Format_Profile``, and an
    ``SMPTE ST 2086`` entry in ``hdr_format`` only means the file carries
    HDR10 static metadata.  Profile 8.1 web releases often ship without
    it, so ``hdr_format`` reads plain ``"Dolby Vision"`` for them too.

    Args:
        hdr_format: Value of ``MediaInfo.video_tracks[0].hdr_format``.
        transfer_characteristics: Value of
            ``MediaInfo.video_tracks[0].transfer_characteristics``.

    Returns:
        bool: ``True`` for Dolby Vision whose stream declares no PQ or
              HLG transfer, ``False`` otherwise.
    """
    return is_dolby_vision(hdr_format) and not is_hdr_transfer(transfer_characteristics)


def detect_dolby_vision_rpu_error(stderr_lines: list[str]) -> bool:
    """Detect FFmpeg Dolby Vision RPU parsing failures that can abort processing.

    This is intentionally narrow to avoid false positives. It matches a small
    allow-list of known fatal signatures from upstream FFmpeg/libdovi output.

    Args:
        stderr_lines: List of FFmpeg stderr lines

    Returns:
        bool: True if the Dolby Vision RPU error is detected
    """
    if not stderr_lines:
        return False

    # Known fatal Dolby Vision parsing signatures (extend as new cases are reported).
    # Keep these specific to avoid triggering on benign informational/warning messages.
    fatal_signatures = [
        "multiple dolby vision rpus found in one au",
        # Some FFmpeg builds append additional context after the core message.
        "multiple dolby vision rpus found in one au. skipping previous.",
    ]

    stderr_text = " ".join(stderr_lines).lower()
    return any(sig in stderr_text for sig in fatal_signatures)


def detect_zscale_colorspace_error(stderr_lines: list[str]) -> bool:
    """Detect zscale filter failures caused by unsupported colorspace conversions.

    Dolby Vision Profile 5 (and some other HDR flavours) use IPT-PQ or
    proprietary transfer characteristics that zscale cannot map to linear.
    FFmpeg emits ``code 3074: no path between colorspaces`` and then crashes
    the filter graph, typically producing exit code 187.

    Args:
        stderr_lines: List of FFmpeg stderr lines

    Returns:
        bool: True if a zscale colorspace error is detected
    """
    if not stderr_lines:
        return False

    stderr_text = " ".join(stderr_lines).lower()

    # Exact substring signatures — the most reliable patterns.
    fatal_signatures = [
        "no path between colorspaces",
        "zscale: generic error in an external library",
    ]
    if any(sig in stderr_text for sig in fatal_signatures):
        return True

    # FFmpeg may log the filter name in brackets with an address, e.g.
    # [Parsed_zscale_1 @ 0x55eb] Generic error in an external library
    # [vf#0:0/zscale @ 0x5f3a] Generic error in an external library
    # Match these even when "zscale:" doesn't appear as a bare prefix.
    if re.search(r"parsed_zscale_\d+.*generic error in an external library", stderr_text):
        return True
    if re.search(
        r"zscale\s*@\s*0x[0-9a-f]+\].*generic error in an external library",
        stderr_text,
    ):
        return True

    return False
