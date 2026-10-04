"""Chapter artifact lifecycles, separate from an already completed BIF."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from media_preview_generator.output.plex_bundle import PlexBundleAdapter
from media_preview_generator.output.plex_hash import calculate_plex_hash, get_source_fingerprint
from media_preview_generator.processing import chapters
from media_preview_generator.processing.generator import CancellationError
from media_preview_generator.processing.multi_server import MultiServerStatus, PublisherStatus, process_canonical_path
from media_preview_generator.servers import ServerRegistry
from media_preview_generator.servers.plex_chapters import Chapter, ChapterError, ChapterTarget


@pytest.fixture
def config():
    return SimpleNamespace(
        tonemap_algorithm="hable", ffmpeg_threads=2, thumbnail_quality=2, ffmpeg_path="ffmpeg", log_level="INFO"
    )


@pytest.fixture
def plan(tmp_path, config):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"stable video bytes")
    target = ChapterTarget(
        1,
        2,
        3,
        calculate_plex_hash(source),
        str(source),
        source.stat().st_size,
        100,
        (Chapter(1, 0, 1000, "", 10, 20), Chapter(2, 1000, 2000, "", 11, 21)),
        "machine",
    )
    return chapters.ChapterPlan(
        MagicMock(),
        str(source),
        get_source_fingerprint(source),
        tmp_path / "Chapters",
        {"version": 1, "width": 1280, "quality": 4, "tonemap": "hable"},
        target,
        chapters.ChapterOutcome("pending", total=2),
    )


def _image(path, color="red"):
    Image.new("RGB", (1280, 720), color).save(path, "JPEG")


@pytest.fixture
def extraction(monkeypatch):
    monkeypatch.setattr(chapters.MediaInfo, "parse", lambda _path: object())
    run = MagicMock(side_effect=lambda _source, _start, path, *_args, **_kw: _image(path))
    monkeypatch.setattr(chapters, "extract_chapter_frame", run)
    register = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", register)
    return run, register


def test_partial_failure_keeps_success_and_retries_only_failed_chapter(plan, config, extraction):
    run, register = extraction
    plan.folder.mkdir()
    old = plan.folder / "chapter2.jpg"
    _image(old, "blue")
    old_bytes = old.read_bytes()

    def first(_source, start, output, *_args, **_kw):
        if start:
            raise RuntimeError("decoder failed")
        _image(output)

    run.side_effect = first
    result = chapters.publish_chapters(plan, config)
    assert (result.status, result.completed, result.total) == ("pending", 1, 2)
    assert old.read_bytes() == old_bytes
    register.assert_not_called()
    assert set(chapters._fresh_images(plan)) == {"1"}
    run.reset_mock()
    run.side_effect = lambda _source, _start, path, *_args, **_kw: _image(path, "green")
    result = chapters.publish_chapters(plan, config)
    assert (result.status, result.completed) == ("ready", 2)
    assert run.call_count == 1
    assert run.call_args.args[1] == 1000
    assert set(register.call_args.args[2]) == {1, 2}


def test_registration_retry_reuses_images(plan, config, extraction):
    run, register = extraction
    register.side_effect = ChapterError("Plex unavailable", code="registration")
    assert chapters.publish_chapters(plan, config).status == "pending"
    assert run.call_count == 2
    run.reset_mock()
    register.side_effect = None
    assert chapters.publish_chapters(plan, config).status == "ready"
    run.assert_not_called()


def test_registered_fresh_images_need_no_work_but_cleared_refs_do(plan, config, extraction):
    run, register = extraction
    assert chapters.publish_chapters(plan, config).status == "ready"
    images = chapters._fresh_images(plan)
    plan.target = replace(
        plan.target,
        chapters=tuple(
            replace(
                c, thumb_url=f"/library/media/2/chapterImages/{c.index}?mpgChapter={images[str(c.index)]['sha256']}"
            )
            for c in plan.target.chapters
        ),
    )
    assert not chapters.chapter_work_needed(plan)
    assert chapters.chapter_work_needed(plan, regenerate=True)
    run.reset_mock()
    register.reset_mock()
    assert chapters.publish_chapters(plan, config).status == "ready"
    run.assert_not_called()
    register.assert_not_called()
    plan.target = replace(plan.target, chapters=tuple(replace(c, thumb_url="") for c in plan.target.chapters))
    assert chapters.chapter_work_needed(plan)
    assert chapters.publish_chapters(plan, config).status == "ready"
    run.assert_not_called()
    register.assert_called_once()


def test_committed_refs_without_api_visibility_remain_pending(plan, config, extraction):
    run, register = extraction

    def committed_but_unverified(_server, _target, revisions):
        plan.target = replace(
            plan.target,
            chapters=tuple(
                replace(c, thumb_url=f"/library/media/2/chapterImages/{c.index}?mpgChapter={revisions[c.index]}")
                for c in plan.target.chapters
            ),
        )
        raise ChapterError("Plex API verification failed", code="registration")

    register.side_effect = committed_but_unverified
    assert chapters.publish_chapters(plan, config).status == "pending"
    run.reset_mock()
    assert chapters.chapter_work_needed(plan)
    assert chapters.publish_chapters(plan, config).status == "pending"
    run.assert_not_called()
    assert chapters.chapter_work_needed(plan)
    register.side_effect = None
    assert chapters.publish_chapters(plan, config).status == "ready"
    run.assert_not_called()
    assert not chapters.chapter_work_needed(plan)


def test_changed_chapter_timestamp_rebuilds_only_that_image(plan, config, extraction):
    run, _register = extraction
    chapters.publish_chapters(plan, config)
    run.reset_mock()
    plan.target = replace(
        plan.target, chapters=(plan.target.chapters[0], replace(plan.target.chapters[1], start_ms=1100))
    )
    chapters.publish_chapters(plan, config)
    assert run.call_count == 1
    assert run.call_args.args[1] == 1100


def test_corrupt_image_does_not_invalidate_other_images(plan, config, extraction):
    run, _ = extraction
    chapters.publish_chapters(plan, config)
    (plan.folder / "chapter2.jpg").write_bytes(b"not an image")
    run.reset_mock()
    chapters.publish_chapters(plan, config)
    assert run.call_count == 1
    assert run.call_args.args[1] == 1000


def test_truncated_jpeg_with_valid_header_is_not_fresh(plan, config, extraction):
    run, _ = extraction
    chapters.publish_chapters(plan, config)
    path = plan.folder / "chapter2.jpg"
    data = path.read_bytes()
    path.write_bytes(data[:-100])
    with Image.open(path) as image:
        assert image.size == (1280, 720)
    assert "2" not in chapters._fresh_images(plan)
    with pytest.raises(OSError):
        chapters._revision(path)


def test_force_failure_does_not_leave_prior_manifest_fresh(plan, config, extraction):
    run, _ = extraction
    chapters.publish_chapters(plan, config)
    run.side_effect = RuntimeError("extraction failed")
    result = chapters.publish_chapters(plan, config, regenerate=True)
    assert result.status == "pending"
    assert result.completed == 0
    assert chapters._fresh_images(plan) == {}


def test_source_changed_during_extraction_never_replaces_image_or_registers(plan, config, extraction):
    run, register = extraction

    def replace_source(_source, _start, output, *_args, **_kw):
        _image(output)
        Path(plan.canonical_path).write_bytes(b"replacement source")

    run.side_effect = replace_source
    result = chapters.publish_chapters(plan, config)
    assert result.status == "pending"
    assert not (plan.folder / "chapter1.jpg").exists()
    register.assert_not_called()


def test_cancel_preserves_existing_outputs_and_stops_registration(plan, config, extraction):
    run, register = extraction
    with pytest.raises(CancellationError):
        chapters.publish_chapters(plan, config, cancel_check=lambda: True)
    run.assert_not_called()
    register.assert_not_called()


def test_stale_plex_hash_is_pending_without_image_work(plan, config, monkeypatch):
    server = plan.server
    server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_k: replace(plan.target, bundle_hash="stale-hash"),
    )
    prepared = chapters.prepare_chapters(
        server, SimpleNamespace(output={"plex_config_folder": str(plan.folder.parent)}), plan.canonical_path, config
    )
    assert prepared.target is None
    assert prepared.outcome.status == "pending"
    assert "current source" in prepared.outcome.message
    assert not plan.folder.exists()


@pytest.mark.parametrize("kind", ["sdr", "pq", "hlg"])
def test_timestamp_runner_uses_correct_color_path_and_bounded_threads(tmp_path, config, monkeypatch, kind):
    track = SimpleNamespace(
        hdr_format="Dolby Vision" if kind == "dv5" else None,
        transfer_characteristics={"pq": "PQ", "hlg": "HLG"}.get(kind),
    )
    output = tmp_path / "frame.jpg"
    runner = MagicMock(side_effect=lambda **_kw: (_image(output), (0, 0.1, 1, []))[1])
    factory = MagicMock(return_value=runner)
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", factory)
    chapters.extract_chapter_frame("movie.mkv", 1234, output, config, media_info=SimpleNamespace(video_tracks=[track]))
    kwargs = factory.call_args.kwargs
    assert kwargs["chapter_start_ms"] == 1234
    assert kwargs["chapter_output"] == str(output)
    assert kwargs["ffmpeg_threads_override"] == 2
    assert kwargs["gpu"] is None
    assert kwargs["path_kind"] == ("sdr" if kind == "sdr" else "hdr10_zscale")
    assert runner.call_args.kwargs == {"use_skip": False}


def test_dv_without_compatible_base_fails_without_starting_unselected_gpu(tmp_path, config, monkeypatch):
    track = SimpleNamespace(hdr_format="Dolby Vision", transfer_characteristics=None)
    factory = MagicMock()
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", factory)
    with pytest.raises(chapters.UnsupportedChapterFormatError, match="Dolby Vision"):
        chapters.extract_chapter_frame(
            "movie.mkv", 0, tmp_path / "frame.jpg", config, media_info=SimpleNamespace(video_tracks=[track])
        )
    factory.assert_not_called()


def _registry(plan, enabled=True):
    return ServerRegistry.from_settings(
        [
            {
                "id": "plex",
                "type": "plex",
                "name": "Plex",
                "enabled": True,
                "url": "http://plex.invalid",
                "auth": {"token": "test"},
                "libraries": [
                    {
                        "id": "1",
                        "name": "Movies",
                        "remote_paths": [str(Path(plan.canonical_path).parent)],
                        "enabled": True,
                    }
                ],
                "output": {
                    "adapter": "plex_bundle",
                    "plex_config_folder": str(plan.folder.parent / "plex"),
                    "chapter_thumbnails": enabled,
                },
            }
        ]
    )


@pytest.mark.parametrize("check_only", [False, True])
def test_existing_bif_chapter_backfill_does_not_extract_or_pack_bif(
    plan, mock_config, extraction, monkeypatch, check_only
):
    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    generate = MagicMock(side_effect=AssertionError("BIF extraction must not run"))
    pack = MagicMock(side_effect=AssertionError("BIF packing must not run"))
    monkeypatch.setattr("media_preview_generator.processing.multi_server.generate_images", generate)
    monkeypatch.setattr("media_preview_generator.processing.generator.generate_bif", pack)
    result = process_canonical_path(plan.canonical_path, registry, mock_config, check_only=check_only)
    assert result.status is (MultiServerStatus.NEEDS_GENERATION if check_only else MultiServerStatus.PUBLISHED)
    assert bif.read_bytes() == b"existing BIF"
    generate.assert_not_called()
    pack.assert_not_called()
    if check_only:
        extraction[0].assert_not_called()
    else:
        assert extraction[0].call_count == 2
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"


def test_disabled_chapters_never_resolve_metadata(plan, mock_config, monkeypatch):
    registry = _registry(plan, enabled=False)
    prepare = MagicMock(side_effect=AssertionError("disabled chapter lookup"))
    monkeypatch.setattr(chapters, "prepare_chapters", prepare)
    process_canonical_path(plan.canonical_path, registry, mock_config, check_only=True)
    prepare.assert_not_called()


def test_pending_chapters_preserve_bif_success_and_request_retry(plan, mock_config, monkeypatch):
    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    plan.target = None
    plan.outcome = chapters.ChapterOutcome("pending", message="Plex has not indexed this source yet")
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    result = process_canonical_path(plan.canonical_path, registry, mock_config, check_only=True)
    publisher = result.publishers[0]
    assert publisher.status is PublisherStatus.PUBLISHED_PENDING_CHAPTERS
    assert publisher.artifacts["bif"]["status"] == "skipped_output_exists"
    from media_preview_generator.processing.retry_queue import PENDING_PUBLISHER_STATUSES

    assert publisher.status.value in PENDING_PUBLISHER_STATUSES


@pytest.mark.parametrize("source_chapters, expected", [((), "none"), ((object(),), "pending")])
def test_empty_plex_rows_require_positive_source_chapter_check(plan, config, monkeypatch, source_chapters, expected):
    plan.server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_kw: replace(plan.target, chapters=()),
    )
    probe = MagicMock(return_value=SimpleNamespace(chapters=source_chapters, duration_ms=2000))
    monkeypatch.setattr(chapters, "probe_media", probe)
    result = chapters.prepare_chapters(
        plan.server,
        SimpleNamespace(output={"plex_config_folder": str(plan.folder.parent)}),
        plan.canonical_path,
        config,
    )
    assert result.outcome.status == expected
    assert probe.call_args.kwargs["timeout_s"] == 5


@pytest.mark.parametrize("failure", ["timeout", "source_changed", "cancel"])
def test_empty_plex_rows_never_complete_after_probe_failure_or_source_race(plan, config, monkeypatch, failure):
    from media_preview_generator.markers.probe import ProbeTimeoutError

    plan.server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_kw: replace(plan.target, chapters=()),
    )
    cancelled = False

    def probe(*_a, **_kw):
        nonlocal cancelled
        if failure == "timeout":
            raise ProbeTimeoutError("metadata probe timed out")
        if failure == "source_changed":
            Path(plan.canonical_path).write_bytes(b"replacement source")
        if failure == "cancel":
            cancelled = True
        return SimpleNamespace(chapters=(), duration_ms=2000)

    monkeypatch.setattr(chapters, "probe_media", probe)

    def prepare():
        return chapters.prepare_chapters(
            plan.server,
            SimpleNamespace(output={"plex_config_folder": str(plan.folder.parent)}),
            plan.canonical_path,
            config,
            cancel_check=lambda: cancelled,
        )

    if failure == "cancel":
        with pytest.raises(CancellationError):
            prepare()
    else:
        result = prepare()
        assert result.target is None and result.outcome.status == "pending"
