"""Independent, source-checked chapter image work within a Previews job."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image
from requests import RequestException

from ..config.paths import expand_path_mapping_candidates
from ..markers.probe import ProbeError, ProbeStalledError, ProbeTimeoutError, ffprobe_path_for, probe_media
from ..markers.publishers.base import Capability, PublishError
from ..output.plex_bundle import PlexBundleAdapter
from ..output.plex_hash import SourceFileChangedError, SourceFingerprint, calculate_plex_hash, get_source_fingerprint
from .ffmpeg_runner import STALL_WATCHDOG_LINE, create_ffmpeg_runner
from .generator import CancellationError, MediaInfo
from .hdr_detection import is_dv_no_backward_compat, is_hdr_transfer

if TYPE_CHECKING:
    from ..servers.plex_chapters import ChapterTarget

_WIDTH = 1280
_QUALITY = 4
_LOCKS = tuple(threading.Lock() for _ in range(64))


class UnsupportedChapterFormatError(RuntimeError):
    """No safe chapter color conversion is available for this source."""

    code = "unsupported"


class ChapterExtractionStalledError(TimeoutError):
    """The managed runner stopped a seek; defer the rest of this source."""


class ChapterSourceCorruptionError(RuntimeError):
    """The demuxer identified malformed container data while extracting."""


def _check_fatal_extraction(returncode: int, stderr: list[str], start_ms: int) -> None:
    """Stop a chapter set only for explicit container corruption or a watchdog stop."""
    if returncode == 0:
        return
    if any(
        "[matroska" in line.lower() and "invalid as first byte of an ebml number" in line.lower() for line in stderr
    ):
        raise ChapterSourceCorruptionError(
            f"Chapter extraction failed at {start_ms}ms: malformed Matroska container data; "
            "remaining chapter attempts stopped. Completed images are preserved."
        )
    if STALL_WATCHDOG_LINE in stderr:
        raise ChapterExtractionStalledError(
            f"Chapter extraction stalled at {start_ms}ms and was stopped by the watchdog; "
            "remaining chapter attempts stopped. Completed images are preserved."
        )


@dataclass(frozen=True)
class ChapterOutcome:
    """Chapter completion independent of an item's scrubber result."""

    status: str
    completed: int = 0
    total: int = 0
    message: str = ""
    retryable: bool = False

    def to_dict(self) -> dict:
        """Return the persisted and displayed artifact outcome."""
        return asdict(self)


@dataclass
class ChapterPlan:
    """An immutable server snapshot plus the local source it must describe."""

    server: object
    canonical_path: str
    source_fingerprint: SourceFingerprint
    folder: Path
    profile: dict
    target: ChapterTarget | None = None
    outcome: ChapterOutcome = field(default_factory=lambda: ChapterOutcome("queued"))

    @property
    def manifest_path(self) -> Path:
        """Return the app-owned completion manifest beside the JPEGs."""
        return self.folder / "chapter-thumbnails.mpg.json"


def _failure(exc: Exception, completed: int = 0, total: int = 0) -> ChapterOutcome:
    from ..utils import redact_secrets

    code = getattr(exc, "code", "")
    waiting = code == "pending_index"
    retryable = (
        waiting
        or code == "source_changed"
        or isinstance(
            exc, SourceFileChangedError | ProbeTimeoutError | ProbeStalledError | RequestException | TimeoutError
        )
    )
    if isinstance(exc, PublishError):
        retryable = retryable or exc.state in {
            None,
            Capability.READY,
            Capability.UNREACHABLE,
            Capability.AGENT_UNAVAILABLE,
        }
    if code == "unsupported":
        retryable = False
    message = str(exc)
    if isinstance(exc, PermissionError):
        message = f"Cannot write or read chapter files: {exc}. Check the app user's access to the source and Plex Media folder."
    return ChapterOutcome("waiting" if waiting else "failed", completed, total, redact_secrets(message), retryable)


def prepare_chapters(
    server, server_config, canonical_path: str, config, *, item_id_hint=None, cancel_check=None
) -> ChapterPlan:
    """Resolve only an enabled Plex server's exact source and chapter map.

    API or indexing failures remain separate from the existing BIF work.
    """
    from ..servers.plex_chapters import ChapterError, resolve_chapter_target

    fingerprint = get_source_fingerprint(canonical_path)
    bundle_hash = calculate_plex_hash(canonical_path)
    bif_path = PlexBundleAdapter.bundle_bif_path(server_config.output["plex_config_folder"], bundle_hash)
    plan = ChapterPlan(
        server,
        canonical_path,
        fingerprint,
        bif_path.parent.parent / "Chapters",
        {"version": 1, "width": _WIDTH, "quality": _QUALITY, "tonemap": config.tonemap_algorithm},
    )
    try:
        last_error = None
        for remote_path in expand_path_mapping_candidates(canonical_path, server.path_mappings):
            try:
                plan.target = resolve_chapter_target(server, remote_path, item_id_hint=item_id_hint)
                break
            except ChapterError as exc:
                if exc.code != "pending_index":
                    raise
                last_error = exc
        if plan.target is None:
            raise last_error or ChapterError("Waiting for Plex to index this source", code="pending_index")
        if plan.target.bundle_hash != bundle_hash or plan.target.source_size != fingerprint[2]:
            raise ChapterError("Waiting for Plex to analyze the current source", code="pending_index")
        _check_source(plan)
        if not plan.target.chapters:
            if cancel_check and cancel_check():
                raise CancellationError("Chapter metadata check cancelled")
            source_metadata = probe_media(canonical_path, ffprobe=ffprobe_path_for(config.ffmpeg_path), timeout_s=5)
            if cancel_check and cancel_check():
                raise CancellationError("Chapter metadata check cancelled")
            _check_source(plan)
            if source_metadata.duration_ms is None or source_metadata.duration_ms <= 0:
                raise ProbeError("Could not confirm the source chapter metadata")
            if source_metadata.chapters:
                raise ChapterError("Plex has not analyzed the source chapters yet", code="pending_index")
            plan.outcome = ChapterOutcome("none", message="No chapters")
        else:
            plan.outcome = ChapterOutcome("queued", total=len(plan.target.chapters))
    except CancellationError:
        raise
    except (PublishError, ProbeError, OSError, ValueError, RuntimeError) as exc:
        plan.target = None
        plan.outcome = _failure(exc)
    return plan


def _check_source(plan: ChapterPlan) -> None:
    if get_source_fingerprint(plan.canonical_path) != plan.source_fingerprint:
        raise SourceFileChangedError("Source changed during chapter processing; retry when the file is stable.")


def _revision(path: Path) -> str:
    with Image.open(path) as image:
        if image.format != "JPEG" or image.width != _WIDTH or image.height < 1:
            raise ValueError(
                f"Chapter extraction expected a 1280px JPEG; got {image.format} {image.width}x{image.height}"
            )
        image.load()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fresh_images(plan: ChapterPlan) -> dict[str, dict]:
    try:
        manifest = json.loads(plan.manifest_path.read_text())
        if manifest.get("source") != list(plan.source_fingerprint) or manifest.get("profile") != plan.profile:
            return {}
        entries = manifest.get("images", {})
        fresh = {}
        for chapter in plan.target.chapters:
            entry = entries.get(str(chapter.index), {})
            if entry.get("start_ms") != chapter.start_ms or entry.get("end_ms") != chapter.end_ms:
                continue
            path = plan.folder / f"chapter{chapter.index}.jpg"
            try:
                if entry.get("sha256") == _revision(path):
                    fresh[str(chapter.index)] = entry
            except (OSError, ValueError):
                continue
        return fresh
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _registered(plan: ChapterPlan, images: dict[str, dict]) -> bool:
    return bool(plan.target) and all(
        chapter.thumb_url
        == f"/library/media/{plan.target.media_id}/chapterImages/{chapter.index}?mpgChapter={images.get(str(chapter.index), {}).get('sha256', '')}"
        for chapter in plan.target.chapters
    )


def _registration_key(plan: ChapterPlan, images: dict[str, dict]) -> dict:
    return {
        "machine": plan.target.machine_identifier,
        "item": plan.target.rating_key,
        "media": plan.target.media_id,
        "part": plan.target.part_id,
        "images": {index: entry["sha256"] for index, entry in images.items()},
    }


def _verified(plan: ChapterPlan, images: dict[str, dict]) -> bool:
    try:
        return json.loads(plan.manifest_path.read_text()).get("verified") == _registration_key(plan, images)
    except (OSError, ValueError, AttributeError):
        return False


def chapter_work_needed(plan: ChapterPlan, *, regenerate: bool = False) -> bool:
    """Check images and refs without starting FFmpeg or modifying Plex."""
    if plan.target is None or not plan.target.chapters:
        return False
    images = _fresh_images(plan)
    return (
        regenerate
        or len(images) != len(plan.target.chapters)
        or not _registered(plan, images)
        or not _verified(plan, images)
    )


def _write_manifest(plan: ChapterPlan, images: dict[str, dict], *, verified: bool = False) -> None:
    payload = {"source": list(plan.source_fingerprint), "profile": plan.profile, "images": images}
    if verified:
        payload["verified"] = _registration_key(plan, images)
    fd, name = tempfile.mkstemp(prefix=".mpg-chapters-", suffix=".json", dir=plan.folder)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(payload, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, plan.manifest_path)
    finally:
        Path(name).unlink(missing_ok=True)


def extract_chapter_frame(
    video_path: str,
    start_ms: int,
    output: Path,
    config,
    *,
    cancel_check=None,
    pause_check=None,
    ffmpeg_threads_override: int | None = None,
    media_info=None,
) -> None:
    """Extract one timestamp using managed CPU decode and the existing color transforms.

    HDR10 and compatible Dolby Vision use zscale. Dolby Vision without a
    compatible base layer is explicitly unsupported in this CPU path.
    The shared runner owns cancellation, pause and stall handling.
    """
    if cancel_check and cancel_check():
        raise CancellationError("Chapter extraction cancelled")
    media_info = media_info or MediaInfo.parse(video_path)
    if not media_info.video_tracks:
        raise ValueError("No video stream for chapter extraction")
    track = media_info.video_tracks[0]
    transfer = track.transfer_characteristics
    dv = is_dv_no_backward_compat(track.hdr_format, transfer)
    if dv:
        raise UnsupportedChapterFormatError(
            "Chapter thumbnails do not yet support Dolby Vision without an HDR10/HLG base layer; scrubber previews are preserved."
        )
    hdr = is_hdr_transfer(transfer) or (track.hdr_format not in (None, "None", "") and not dv)
    local_config = copy.copy(config)
    local_config.thumbnail_quality = _QUALITY
    threads = max(1, int(ffmpeg_threads_override or config.ffmpeg_threads or 2))
    scale = f"scale=w={_WIDTH}:h=-2"
    # Container color metadata may be absent from decoded frames (notably PQ).
    # Supply only the input properties the source actually declares.
    input_colors = []
    for option, value, choices in (
        (
            "tin",
            transfer,
            {"pq": "smpte2084", "smpte st 2084": "smpte2084", "hlg": "arib-std-b67", "arib std-b67": "arib-std-b67"},
        ),
        ("pin", getattr(track, "color_primaries", None), {"bt.2020": "bt2020", "bt.709": "bt709"}),
        (
            "min",
            getattr(track, "matrix_coefficients", None),
            {"bt.2020 non-constant": "bt2020nc", "bt.2020 constant": "bt2020c", "bt.709": "bt709"},
        ),
    ):
        mapped = choices.get(value.strip().lower()) if isinstance(value, str) else None
        if mapped:
            input_colors.append(f"{option}={mapped}")
    zscale_input = ":".join([*input_colors, "t=linear", "npl=100"])
    tonemap = (
        f"zscale={zscale_input},format=gbrpf32le,"
        f"zscale=p=bt709,tonemap={config.tonemap_algorithm}:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
    )
    runner_options = dict(
        video_file=video_path,
        output_folder=str(output.parent),
        gpu=None,
        gpu_device_path=None,
        config=local_config,
        progress_callback=None,
        ffmpeg_threads_override=threads,
        cancel_check=cancel_check,
        pause_check=pause_check,
        path_kind="hdr10_zscale" if hdr else "sdr",
        libplacebo_vf=None,
        use_libplacebo=False,
        dv5_software_fallback=False,
        base_scale=scale,
        fps_filter="null",
        hdr10_zscale_chain=tonemap,
        chapter_output=str(output),
    )
    runner = create_ffmpeg_runner(**runner_options, chapter_start_ms=start_ms)
    rc, _, _, stderr = runner(use_skip=False)
    _check_fatal_extraction(rc, stderr, start_ms)
    stderr_text = "\n".join(stderr).lower()
    premature_end = "file ended prematurely" in stderr_text
    no_frame = "no filtered frames" in stderr_text or (rc == 0 and not output.exists())
    try:
        duration_ms = float(track.duration)
    except (AttributeError, TypeError, ValueError):
        duration_ms = 0
    # Plex can round a final chapter past the last video frame. Seek at most
    # one second before the chapter; container duration can extend past video.
    if no_frame and not premature_end and 1000 <= duration_ms < float("inf") and abs(start_ms - duration_ms) <= 1000:
        fallback_ms = max(0, start_ms - 1000)
        if fallback_ms < start_ms:
            if cancel_check and cancel_check():
                raise CancellationError("Chapter extraction cancelled")
            runner = create_ffmpeg_runner(**runner_options, chapter_start_ms=fallback_ms)
            rc, _, _, stderr = runner(use_skip=False)
            _check_fatal_extraction(rc, stderr, fallback_ms)
            premature_end = "file ended prematurely" in "\n".join(stderr).lower()
    if rc != 0:
        detail = "; source ended prematurely" if premature_end else ""
        raise RuntimeError(f"Chapter extraction failed at {start_ms}ms (FFmpeg exit {rc}){detail}")
    _revision(output)


def publish_chapters(
    plan: ChapterPlan,
    config,
    *,
    regenerate: bool = False,
    cancel_check=None,
    pause_check=None,
    ffmpeg_threads_override: int | None = None,
    chapter_progress_callback: Callable[[dict | None], None] | None = None,
) -> ChapterOutcome:
    """Publish missing images atomically and register only a complete current set."""
    from ..servers.plex_chapters import register_chapters

    if plan.target is None or not plan.target.chapters:
        return plan.outcome
    images = {}
    total = len(plan.target.chapters)
    processed = failed = 0

    def report(stage: str) -> None:
        if chapter_progress_callback:
            chapter_progress_callback(
                {"stage": stage, "processed": processed, "total": total, "ready": len(images), "failed": failed}
            )

    lock = _LOCKS[hash(str(plan.folder)) % len(_LOCKS)]
    report("waiting")
    while not lock.acquire(timeout=0.1):
        if cancel_check and cancel_check():
            raise CancellationError("Chapter processing cancelled")
    try:
        report("preparing")
        _check_source(plan)
        plan.folder.mkdir(parents=True, exist_ok=True)
        images = {} if regenerate else _fresh_images(plan)
        processed = len(images)
        if regenerate:
            _write_manifest(plan, images)
        errors = []
        media_info = None
        for chapter in plan.target.chapters:
            if cancel_check and cancel_check():
                raise CancellationError("Chapter processing cancelled")
            if str(chapter.index) in images:
                continue
            report("extracting")
            _check_source(plan)
            try:
                if media_info is None:
                    media_info = MediaInfo.parse(plan.canonical_path)
                with tempfile.TemporaryDirectory(prefix=".mpg-chapter-", dir=plan.folder) as temp:
                    staged = Path(temp) / "frame.jpg"
                    extract_chapter_frame(
                        plan.canonical_path,
                        chapter.start_ms,
                        staged,
                        config,
                        cancel_check=cancel_check,
                        pause_check=pause_check,
                        ffmpeg_threads_override=ffmpeg_threads_override,
                        media_info=media_info,
                    )
                    revision = _revision(staged)
                    _check_source(plan)
                    os.replace(staged, plan.folder / f"chapter{chapter.index}.jpg")
                    images[str(chapter.index)] = {
                        "start_ms": chapter.start_ms,
                        "end_ms": chapter.end_ms,
                        "sha256": revision,
                    }
                    _write_manifest(plan, images)
            except CancellationError:
                raise
            except (ChapterExtractionStalledError, ChapterSourceCorruptionError):
                failed += 1
                processed += 1
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(exc)
                failed += 1
            processed += 1
            report("extracting")
        _check_source(plan)
        if len(images) != total:
            report("failed")
            return _failure(
                errors[0] if errors else RuntimeError("Chapter images are incomplete"),
                len(images),
                total,
            )
        if cancel_check and cancel_check():
            raise CancellationError("Chapter processing cancelled")
        if not _registered(plan, images) or not _verified(plan, images):
            report("registering")
            register_chapters(
                plan.server, plan.target, {int(index): entry["sha256"] for index, entry in images.items()}
            )
            _check_source(plan)
            _write_manifest(plan, images, verified=True)
        _check_source(plan)
        report("complete")
        return ChapterOutcome("ready", total, total, "Chapter thumbnails ready")
    except CancellationError:
        raise
    except (PublishError, OSError, ValueError, RuntimeError) as exc:
        report("failed")
        return _failure(exc, len(images), total)
    finally:
        lock.release()
