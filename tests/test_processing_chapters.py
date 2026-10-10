"""Chapter artifact lifecycles, separate from an already completed BIF."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from media_preview_generator.output.plex_bundle import PlexBundleAdapter
from media_preview_generator.output.plex_hash import calculate_plex_hash, get_source_fingerprint
from media_preview_generator.processing import chapters, multi_server
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
        chapters.ChapterOutcome("queued", total=2),
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
    assert (result.status, result.completed, result.total) == ("failed", 1, 2)
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
    outcome = chapters.publish_chapters(plan, config)
    assert outcome.status == "failed" and outcome.retryable
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


def test_committed_refs_without_api_visibility_fail_verification_and_can_retry(plan, config, extraction):
    run, register = extraction

    def committed_but_unverified(_server, _target, revisions, *, verify_source):
        verify_source()
        plan.target = replace(
            plan.target,
            chapters=tuple(
                replace(c, thumb_url=f"/library/media/2/chapterImages/{c.index}?mpgChapter={revisions[c.index]}")
                for c in plan.target.chapters
            ),
        )
        raise ChapterError("Plex API verification failed", code="registration")

    register.side_effect = committed_but_unverified
    outcome = chapters.publish_chapters(plan, config)
    assert outcome.status == "failed" and outcome.retryable
    run.reset_mock()
    assert chapters.chapter_work_needed(plan)
    outcome = chapters.publish_chapters(plan, config)
    assert outcome.status == "failed" and outcome.retryable
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
    assert result.status == "failed"
    assert result.completed == 0
    assert chapters._fresh_images(plan) == {}


def test_source_changed_during_extraction_never_replaces_image_or_registers(plan, config, extraction):
    run, register = extraction

    def replace_source(_source, _start, output, *_args, **_kw):
        _image(output)
        Path(plan.canonical_path).write_bytes(b"replacement source")

    run.side_effect = replace_source
    result = chapters.publish_chapters(plan, config)
    assert result.status == "failed" and result.retryable
    assert not (plan.folder / "chapter1.jpg").exists()
    register.assert_not_called()


def test_cancel_preserves_existing_outputs_and_stops_registration(plan, config, extraction):
    run, register = extraction
    with pytest.raises(CancellationError):
        chapters.publish_chapters(plan, config, cancel_check=lambda: True)
    run.assert_not_called()
    register.assert_not_called()


def test_stale_plex_hash_waits_for_plex_without_image_work(plan, config, monkeypatch):
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
    assert prepared.outcome.status == "waiting" and prepared.outcome.retryable
    assert "current source" in prepared.outcome.message
    assert not plan.folder.exists()


@pytest.mark.parametrize("size_delta, expected", [(0, "queued"), (1, "waiting")])
def test_bulk_scan_takes_plex_hash_on_size_match_without_reading_the_file(
    plan, config, monkeypatch, size_delta, expected
):
    plan.server.path_mappings = []
    server_hash = "f" + "0" * 39
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_k: replace(
            plan.target, bundle_hash=server_hash, source_size=plan.target.source_size + size_delta
        ),
    )
    monkeypatch.setattr(chapters, "calculate_plex_hash", MagicMock(side_effect=AssertionError("file was read")))
    plex_config = plan.folder.parent / "plex"
    prepared = chapters.prepare_chapters(
        plan.server,
        SimpleNamespace(output={"plex_config_folder": str(plex_config)}),
        plan.canonical_path,
        config,
        trust_server_hash=True,
    )
    assert prepared.outcome.status == expected
    if expected == "queued":
        bif = PlexBundleAdapter.bundle_bif_path(str(plex_config), server_hash)
        assert prepared.folder == bif.parent.parent / "Chapters"
    else:
        assert prepared.target is None


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
    expected_scale = "scale=w=1280:h=-2"
    if kind != "sdr":
        expected_scale += ":in_range=tv:out_range=pc,format=yuvj420p"
        assert kwargs["hdr10_zscale_chain"].endswith("zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    assert kwargs["base_scale"] == expected_scale
    assert kwargs["path_kind"] == ("sdr" if kind == "sdr" else "hdr10_zscale")
    assert runner.call_args.kwargs == {"use_skip": False}


@pytest.mark.parametrize("failure", [RuntimeError("FFmpeg exit 234"), ValueError("Invalid JPEG"), OSError("No frame")])
def test_extraction_errors_fail_instead_of_waiting_for_plex(plan, config, extraction, failure):
    run, register = extraction
    run.side_effect = failure

    result = chapters.publish_chapters(plan, config)

    assert result.status == "failed"
    assert (result.completed, result.total) == (0, 2)
    assert result.message == str(failure)
    register.assert_not_called()


def test_rejected_image_reports_actual_dimensions(tmp_path):
    output = tmp_path / "frame.jpg"
    Image.new("RGB", (1278, 534)).save(output, "JPEG")

    with pytest.raises(ValueError, match="expected a 1280px JPEG; got JPEG 1278x534"):
        chapters._revision(output)


@pytest.mark.parametrize(
    "error, status, retryable",
    [
        (ChapterError("Plex has not indexed the source", code="pending_index"), "waiting", True),
        (ChapterError("Plex chapter snapshot changed", code="source_changed"), "failed", True),
        (ChapterError("API verification failed", code="registration"), "failed", True),
        (ChapterError("Unsupported source", code="unsupported"), "failed", False),
        (chapters.SourceFileChangedError("File changed"), "failed", True),
        (chapters.ProbeTimeoutError("Probe timed out"), "failed", True),
        (chapters.ProbeStalledError("Probe stalled"), "failed", True),
        (chapters.ProbeError("Unreadable source"), "failed", False),
        (chapters.RequestException("Connection failed"), "failed", True),
        (TimeoutError("Connection timed out"), "failed", True),
        (PermissionError("Read-only folder"), "failed", False),
        (FileNotFoundError("Missing source"), "failed", False),
        (RuntimeError("Decoder failed"), "failed", False),
        (ValueError("Wrong image dimensions"), "failed", False),
        (chapters.UnsupportedChapterFormatError("Unsupported Dolby Vision"), "skipped", False),
        (ChapterError("Split version", code="unsupported_source"), "skipped", False),
        (ChapterError("Other version", code="other_version"), "skipped", False),
    ],
)
def test_chapter_failure_and_retry_eligibility_are_independent(error, status, retryable):
    result = chapters._failure(error, completed=2, total=3)

    assert result.status == status
    assert result.retryable is retryable
    assert (result.completed, result.total) == (2, 3)
    assert str(error) in result.message
    assert result.to_dict()["retryable"] is retryable


@pytest.mark.parametrize("state", [None, *chapters.Capability])
def test_only_transient_publisher_capability_failures_can_retry(state):
    result = chapters._failure(chapters.PublishError("Registration unavailable", state=state))

    assert result.status == "failed"
    assert result.retryable is (
        state
        in {None, chapters.Capability.READY, chapters.Capability.UNREACHABLE, chapters.Capability.AGENT_UNAVAILABLE}
    )


def test_unwritable_chapter_folder_fails_with_actionable_reason(plan, config, extraction, monkeypatch):
    mkdir = Path.mkdir

    def denied(path, *args, **kwargs):
        if path == plan.folder:
            raise PermissionError("Permission denied: Chapters")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)

    result = chapters.publish_chapters(plan, config)

    assert result.status == "failed" and not result.retryable
    assert "Check the app user's access" in result.message
    extraction[0].assert_not_called()
    extraction[1].assert_not_called()


def test_dv_without_compatible_base_fails_without_starting_unselected_gpu(tmp_path, config, monkeypatch):
    track = SimpleNamespace(hdr_format="Dolby Vision", transfer_characteristics=None)
    factory = MagicMock()
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", factory)
    with pytest.raises(chapters.UnsupportedChapterFormatError, match="Dolby Vision"):
        chapters.extract_chapter_frame(
            "movie.mkv", 0, tmp_path / "frame.jpg", config, media_info=SimpleNamespace(video_tracks=[track])
        )
    factory.assert_not_called()


def test_no_chapters_code_maps_to_none_outcome():
    result = chapters._failure(ChapterError("No chapters", code="no_chapters"), completed=2, total=3)

    assert result.status == "none"
    assert result.message == "No chapters"
    assert result.retryable is False


def test_no_video_stream_is_unsupported_source(tmp_path, config):
    with pytest.raises(chapters.UnsupportedChapterFormatError, match="No video stream"):
        chapters.extract_chapter_frame(
            "movie.mkv", 0, tmp_path / "frame.jpg", config, media_info=SimpleNamespace(video_tracks=[])
        )
    assert chapters._failure(chapters.UnsupportedChapterFormatError("No video stream")).status == "skipped"


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
    progress = []
    previews = multi_server._process_canonical_path_previews

    def check_scrubber_progress(*args, **kwargs):
        assert progress[-1] is None  # Clear chapter preparation before any scrubber work.
        return previews(*args, **kwargs)

    preview_boundary = MagicMock(side_effect=check_scrubber_progress)
    monkeypatch.setattr(multi_server, "_process_canonical_path_previews", preview_boundary)
    result = process_canonical_path(
        plan.canonical_path, registry, mock_config, check_only=check_only, chapter_progress_callback=progress.append
    )
    assert result.status is (MultiServerStatus.NEEDS_GENERATION if check_only else MultiServerStatus.PUBLISHED)
    assert bif.read_bytes() == b"existing BIF"
    generate.assert_not_called()
    pack.assert_not_called()
    preview_boundary.assert_called_once()
    if check_only:
        extraction[0].assert_not_called()
    else:
        assert extraction[0].call_count == 2
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"
        assert result.publishers[0].artifacts["chapters"]["updated"] is True
        assert progress[-1] == {"stage": "complete", "processed": 2, "total": 2, "ready": 2, "failed": 0}
    assert progress[0]["stage"] == "preparing"


def test_disabled_chapters_never_resolve_metadata(plan, mock_config, monkeypatch):
    registry = _registry(plan, enabled=False)
    prepare = MagicMock(side_effect=AssertionError("disabled chapter lookup"))
    monkeypatch.setattr(chapters, "prepare_chapters", prepare)
    process_canonical_path(plan.canonical_path, registry, mock_config, check_only=True)
    prepare.assert_not_called()


@pytest.mark.parametrize(
    "source, webhook_source, webhook_paths, trusted",
    [
        (None, None, None, True),
        ("sonarr", None, None, False),
        (None, "radarr", None, False),
        (None, None, ["/data/x.mkv"], False),
    ],
)
def test_only_bulk_scans_let_chapter_planning_trust_plex_hash(
    plan, mock_config, monkeypatch, source, webhook_source, webhook_paths, trusted
):
    registry = _registry(plan)
    mock_config.webhook_source = webhook_source
    mock_config.webhook_paths = webhook_paths
    prepare = MagicMock(return_value=plan)
    monkeypatch.setattr(chapters, "prepare_chapters", prepare)
    process_canonical_path(plan.canonical_path, registry, mock_config, check_only=True, source=source)
    assert prepare.call_args.kwargs["trust_server_hash"] is trusted


@pytest.mark.parametrize("already_registered", [False, True])
def test_chapter_update_provenance_distinguishes_registration_from_reused_outputs(
    plan, mock_config, config, extraction, monkeypatch, already_registered
):
    run, register = extraction
    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    assert chapters.publish_chapters(plan, config).status == "ready"
    images = chapters._fresh_images(plan)
    if already_registered:
        plan.target = replace(
            plan.target,
            chapters=tuple(
                replace(
                    c, thumb_url=f"/library/media/2/chapterImages/{c.index}?mpgChapter={images[str(c.index)]['sha256']}"
                )
                for c in plan.target.chapters
            ),
        )
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    run.reset_mock()
    register.reset_mock()

    result = process_canonical_path(plan.canonical_path, registry, mock_config)

    assert result.status is (MultiServerStatus.SKIPPED if already_registered else MultiServerStatus.PUBLISHED)
    artifact = result.publishers[0].artifacts["chapters"]
    assert artifact["status"] == "ready"
    assert artifact["updated"] is (not already_registered)
    run.assert_not_called()
    if already_registered:
        register.assert_not_called()
    else:
        verify_source = register.call_args.kwargs["verify_source"]
        assert callable(verify_source)
        verify_source()
        register.assert_called_once_with(
            plan.server,
            plan.target,
            {int(index): entry["sha256"] for index, entry in images.items()},
            verify_source=verify_source,
        )


def test_ffmpeg_failure_preserves_existing_bif_and_records_chapter_warning(plan, mock_config, monkeypatch, tmp_path):
    from media_preview_generator.jobs.orchestrator import (
        _outcome_for_multi_server_status,
        _publisher_rows_from_result,
        merge_chain_publishers_best_per_path,
    )
    from media_preview_generator.web.jobs import JobManager
    from media_preview_generator.web.routes.job_runner import _chapter_completion_warning

    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    track = SimpleNamespace(hdr_format=None, transfer_characteristics=None)
    monkeypatch.setattr(chapters.MediaInfo, "parse", lambda _path: SimpleNamespace(video_tracks=[track]))
    runner = MagicMock(return_value=(234, 0.1, 0, ["File ended prematurely"]))
    factory = MagicMock(return_value=runner)
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", factory)
    register = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", register)

    result = process_canonical_path(plan.canonical_path, registry, mock_config)

    assert result.status is MultiServerStatus.SKIPPED
    assert bif.read_bytes() == b"existing BIF"
    assert [call.kwargs["chapter_start_ms"] for call in factory.call_args_list] == [0, 1000]
    assert all(call.kwargs["base_scale"] == "scale=w=1280:h=-2" for call in factory.call_args_list)
    assert all(call.kwargs == {"use_skip": False} for call in runner.call_args_list)
    register.assert_not_called()
    manager = JobManager(config_dir=str(tmp_path / "jobs"))
    job = manager.create_job(library_name="Chapters")
    manager.record_file_result(
        job.id,
        plan.canonical_path,
        _outcome_for_multi_server_status(result.status).value,
        servers=_publisher_rows_from_result(result, plan.canonical_path),
    )
    stored = manager.get_file_results(job.id)
    assert stored[0]["outcome"] == "skipped_bif_exists"
    outcome = stored[0]["servers"][0]["artifacts"]["chapters"]
    assert outcome["status"] == "failed"
    assert "FFmpeg exit 234" in outcome["message"]
    assert "chapter thumbnails failed for 1" in _chapter_completion_warning(
        merge_chain_publishers_best_per_path(stored), include_pending=True
    )


def test_waiting_chapters_preserve_bif_success_and_request_retry(plan, mock_config, monkeypatch):
    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    plan.target = None
    plan.outcome = chapters.ChapterOutcome("waiting", message="Plex has not indexed this source yet", retryable=True)
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    result = process_canonical_path(plan.canonical_path, registry, mock_config, check_only=True)
    publisher = result.publishers[0]
    assert publisher.status is PublisherStatus.PUBLISHED_PENDING_CHAPTERS
    assert publisher.artifacts["bif"]["status"] == "skipped_output_exists"
    assert publisher.artifacts["chapters"]["updated"] is False
    from media_preview_generator.processing.retry_queue import PENDING_PUBLISHER_STATUSES

    assert publisher.status.value in PENDING_PUBLISHER_STATUSES


@pytest.mark.parametrize("source_chapters, expected", [((), "none"), ((object(),), "waiting")])
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
        assert result.target is None and result.outcome.status == "failed"
        assert result.outcome.retryable


@pytest.mark.parametrize("cached", [0, 1, 2])
def test_chapter_progress_counts_cached_images_and_reports_registration(plan, config, extraction, cached):
    run, register = extraction
    if cached:
        assert chapters.publish_chapters(plan, config).status == "ready"
        if cached == 1:
            (plan.folder / "chapter2.jpg").unlink()
    run.reset_mock()
    register.reset_mock()
    progress = []
    during_registration = []
    register.side_effect = lambda *_a, **_kw: during_registration.append(progress[-1])

    result = chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append)

    assert result.status == "ready"
    assert [call.args[1] for call in run.call_args_list] == [0, 1000][cached:]
    expected = {"stage": "registering", "processed": 2, "total": 2, "ready": 2, "failed": 0}
    assert during_registration == [expected]
    assert progress[-1] == {**expected, "stage": "complete"}
    if cached < 2:
        first_attempt = next(snapshot for snapshot in progress if snapshot["stage"] == "extracting")
        assert first_attempt == {"stage": "extracting", "processed": cached, "total": 2, "ready": cached, "failed": 0}
    assert [snapshot["processed"] for snapshot in progress] == sorted(snapshot["processed"] for snapshot in progress)


def test_failed_chapter_attempt_advances_work_progress_without_counting_as_ready(plan, config, extraction):
    run, register = extraction

    def extract(_source, start, output, *_args, **_kwargs):
        if start == 0:
            raise RuntimeError("Cannot decode first chapter")
        _image(output)

    run.side_effect = extract
    progress = []
    result = chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append)

    assert result.status == "failed"
    assert progress[-1] == {"stage": "failed", "processed": 2, "total": 2, "ready": 1, "failed": 1}
    assert {"stage": "extracting", "processed": 1, "total": 2, "ready": 0, "failed": 1} in progress
    assert all(snapshot["stage"] != "complete" for snapshot in progress)
    register.assert_not_called()


def test_chapter_registration_failure_keeps_completed_image_progress(plan, config, extraction):
    _, register = extraction
    register.side_effect = ChapterError("Plex verification failed", code="registration")
    progress = []

    result = chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append)

    assert result.status == "failed"
    assert progress[-2:] == [
        {"stage": "registering", "processed": 2, "total": 2, "ready": 2, "failed": 0},
        {"stage": "failed", "processed": 2, "total": 2, "ready": 2, "failed": 0},
    ]


def test_cancellation_never_reports_remaining_chapter_work_complete(plan, config, extraction):
    run, register = extraction
    progress = []
    with pytest.raises(CancellationError):
        chapters.publish_chapters(
            plan,
            config,
            chapter_progress_callback=progress.append,
            cancel_check=lambda: bool(progress and progress[-1]["processed"] == 1),
        )
    assert progress[-1] == {"stage": "extracting", "processed": 1, "total": 2, "ready": 1, "failed": 0}
    assert [call.args[1] for call in run.call_args_list] == [0]
    register.assert_not_called()


def test_waiting_for_chapter_lock_reports_no_attempts_and_can_cancel(plan, config, extraction, monkeypatch):
    lock = MagicMock()
    lock.acquire.return_value = False
    monkeypatch.setattr(chapters, "_LOCKS", (lock,))
    progress = []
    with pytest.raises(CancellationError):
        chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append, cancel_check=lambda: True)
    assert progress == [{"stage": "waiting", "processed": 0, "total": 2, "ready": 0, "failed": 0}]
    lock.acquire.assert_called_once_with(timeout=0.1)
    lock.release.assert_not_called()
    extraction[0].assert_not_called()


def _prepare_for(plan, config, monkeypatch, **kwargs):
    plan.server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target", lambda *_a, **_k: plan.target
    )
    plex_config = Path(plan.canonical_path).parent / "plex"
    return chapters.prepare_chapters(
        plan.server,
        SimpleNamespace(output={"plex_config_folder": str(plex_config)}),
        plan.canonical_path,
        config,
        **kwargs,
    )


def test_unsupported_format_is_stamped_and_next_prepare_skips_all_work(plan, config, extraction, monkeypatch):
    run, register = extraction
    run.side_effect = chapters.UnsupportedChapterFormatError("Chapter thumbnails do not yet support Dolby Vision")
    plan.folder = _prepare_for(plan, config, monkeypatch).folder

    first = chapters.publish_chapters(plan, config)

    assert first.status == "skipped"
    assert first.retryable is False
    register.assert_not_called()
    again = _prepare_for(plan, config, monkeypatch)
    assert again.target is None
    assert again.outcome.status == "skipped"
    assert "Dolby Vision" in again.outcome.message
    assert chapters.chapter_work_needed(again) is False


def test_unsupported_stamp_is_ignored_by_regenerate_and_by_a_changed_file(plan, config, extraction, monkeypatch):
    run, _register = extraction
    run.side_effect = chapters.UnsupportedChapterFormatError("Unsupported Dolby Vision")
    plan.folder = _prepare_for(plan, config, monkeypatch).folder
    chapters.publish_chapters(plan, config)

    assert _prepare_for(plan, config, monkeypatch, regenerate=True).target is not None
    Path(plan.canonical_path).write_bytes(b"different, longer video bytes")
    plan.target = replace(plan.target, source_size=Path(plan.canonical_path).stat().st_size)
    assert _prepare_for(plan, config, monkeypatch).outcome.status != "skipped"


def test_unsupported_format_reports_skipped_stage_without_counting_failures(plan, config, extraction):
    run, _register = extraction
    run.side_effect = chapters.UnsupportedChapterFormatError("Unsupported Dolby Vision")
    events = []

    chapters.publish_chapters(plan, config, chapter_progress_callback=events.append)

    assert events[-1]["stage"] == "skipped"
    assert events[-1]["failed"] == 0


def _past_end_on_second_chapter(run):
    def extract(_source, start_ms, path, *_args, **_kw):
        if start_ms == 1000:
            raise chapters.ChapterPastEndError("Chapter timestamp 1000ms is outside the current video's duration")
        _image(path)

    run.side_effect = extract


def test_past_end_chapter_gets_no_thumbnail_and_the_rest_register(plan, config, extraction):
    run, register = extraction
    _past_end_on_second_chapter(run)
    events = []

    result = chapters.publish_chapters(plan, config, chapter_progress_callback=events.append)

    assert result.status == "ready"
    assert "1 chapter starts after the video ends and has no thumbnail" in result.message
    assert events[-1]["failed"] == 0
    assert list(register.call_args.args[2]) == [1]
    manifest = json.loads(plan.manifest_path.read_text())
    assert manifest["images"]["2"] == {"start_ms": 1000, "end_ms": 2000, "past_end": True}
    assert "sha256" in manifest["images"]["1"]
    assert manifest["verified"]["images"].keys() == {"1"}


def test_second_publish_does_not_seek_a_past_end_chapter_again(plan, config, extraction):
    run, register = extraction
    _past_end_on_second_chapter(run)
    chapters.publish_chapters(plan, config)
    register_after_first = register.call_count
    run.reset_mock()
    plan.target = replace(
        plan.target,
        chapters=(
            replace(
                plan.target.chapters[0],
                thumb_url=f"/library/media/2/chapterImages/1?mpgChapter={_manifest_sha(plan, '1')}",
            ),
            plan.target.chapters[1],
        ),
    )

    assert chapters.chapter_work_needed(plan) is False
    result = chapters.publish_chapters(plan, config)

    assert result.status == "ready"
    run.assert_not_called()
    assert register.call_count == register_after_first


def _manifest_sha(plan, index):
    return json.loads(plan.manifest_path.read_text())["images"][index]["sha256"]


def test_every_chapter_past_end_still_fails(plan, config, extraction):
    run, register = extraction
    run.side_effect = chapters.ChapterPastEndError("Chapter timestamp is outside the current video's duration")

    result = chapters.publish_chapters(plan, config)

    assert result.status == "failed"
    assert "All 2 chapters start after the video ends" in result.message
    register.assert_not_called()


def test_all_past_end_fails_with_a_clear_message_on_every_scan(plan, config, extraction):
    run, register = extraction
    run.side_effect = chapters.ChapterPastEndError("Chapter timestamp is outside the current video's duration")
    first = chapters.publish_chapters(plan, config)
    run.reset_mock()

    second = chapters.publish_chapters(plan, config)

    assert first.status == second.status == "failed"
    assert second.message == first.message
    assert second.message.startswith("All 2 chapters start after the video ends")
    assert second.retryable is False
    run.assert_not_called()
    register.assert_not_called()


def test_past_end_chapter_removes_its_stale_image(plan, config, extraction):
    run, _register = extraction
    _past_end_on_second_chapter(run)
    plan.folder.mkdir(parents=True)
    stale = plan.folder / "chapter2.jpg"
    _image(stale)

    chapters.publish_chapters(plan, config)

    assert not stale.exists()
    assert (plan.folder / "chapter1.jpg").exists()


def test_corrupt_index_with_past_end_chapter_is_ready_with_note(plan, config, extraction, monkeypatch):
    run, register = extraction
    monkeypatch.setattr(chapters, "inspect_seek_index", lambda *a, **kw: "Invalid Cues")
    track = SimpleNamespace(duration=1500)
    monkeypatch.setattr(chapters.MediaInfo, "parse", lambda _path: SimpleNamespace(video_tracks=[track]))
    plan.target = replace(
        plan.target, chapters=(plan.target.chapters[0], replace(plan.target.chapters[1], start_ms=9000))
    )
    recovered = []

    def recover(_plan, chapter, staged):
        recovered.append(chapter.index)
        _image(staged)
        return {"timestamp_ms": 0}

    monkeypatch.setattr(chapters, "recover_bif_frame", recover)

    result = chapters.publish_chapters(plan, config)

    assert result.status == "ready"
    assert "starts after the video ends" in result.message
    assert recovered == [1]
    run.assert_not_called()
    assert list(register.call_args.args[2]) == [1]
