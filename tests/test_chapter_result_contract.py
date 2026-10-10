"""Chapter details survive file persistence and retry aggregation."""

import pytest

from media_preview_generator.jobs.orchestrator import (
    _outcome_for_multi_server_status,
    _publisher_rows_from_result,
    fold_publisher_rows_into_aggregate,
    merge_chain_publishers_best_per_path,
)
from media_preview_generator.output.plex_bundle import PlexBundleAdapter
from media_preview_generator.output.plex_hash import calculate_plex_hash, get_source_fingerprint
from media_preview_generator.processing.chapters import ChapterOutcome, ChapterPlan
from media_preview_generator.processing.multi_server import (
    MultiServerResult,
    MultiServerStatus,
    PublisherResult,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.processing.retry_queue import publisher_needs_retry, publisher_retry_count
from media_preview_generator.servers import ServerRegistry
from media_preview_generator.web.jobs import JobManager
from media_preview_generator.web.routes.job_runner import _chapter_completion_warning


@pytest.mark.parametrize(
    "publisher,bif,chapter,expected",
    [
        ("published", "skipped_output_exists", {"status": "ready", "updated": True}, "updated"),
        ("published", "published", {"status": "ready", "updated": True}, "updated"),
        ("published", "published", {"status": "ready", "updated": False}, "already_existed"),
        ("skipped_output_exists", "skipped_output_exists", {"status": "ready", "updated": False}, "already_existed"),
        ("published", "skipped_output_exists", {"status": "ready", "updated": False}, "already_existed"),
        ("published", "skipped_output_exists", {"status": "ready"}, "updated"),
        ("skipped_output_exists", "skipped_output_exists", {"status": "ready"}, "already_existed"),
        ("published", "published", {"status": "ready"}, "ready"),
        ("published_chapters_failed", "published", {"status": "failed", "updated": True}, "failed"),
        ("published_pending_chapters", "skipped_output_exists", {"status": "waiting"}, "waiting"),
        ("published_pending_chapters", "skipped_output_exists", {"status": "pending"}, "incomplete"),
        ("skipped_output_exists", "skipped_output_exists", {"status": "none"}, "none"),
    ],
)
def test_live_and_chain_chapter_counts_preserve_exact_output_provenance(publisher, bif, chapter, expected):
    row = {
        "server_id": "plex",
        "status": publisher,
        "frame_source": "output_existed" if bif == "skipped_output_exists" else "extracted",
        "artifacts": {"bif": {"status": bif}, "chapters": chapter},
    }
    aggregate = {}
    fold_publisher_rows_into_aggregate(aggregate, [row])
    merged = merge_chain_publishers_best_per_path([{"file": "/movie.mkv", "servers": [row]}])
    for result in (aggregate["plex"], merged[0]):
        assert result["chapter_counts"] == {expected: 1}
        assert result["frame_sources"] == {row["frame_source"]: 1}
        assert result["counts"] == {publisher: 1}


def test_chapter_only_updates_are_counted_separately_from_nine_existing_scrubbers(tmp_path):
    manager = JobManager(config_dir=str(tmp_path))
    job = manager.create_job(library_name="Chapters")
    aggregate = {}
    for index in range(9):
        row = {
            "server_id": "plex",
            "status": "published",
            "frame_source": "output_existed",
            "artifacts": {
                "bif": {"status": "skipped_output_exists"},
                "chapters": {"status": "ready", "updated": True},
            },
        }
        fold_publisher_rows_into_aggregate(aggregate, [row])
        manager.record_file_result(job.id, f"/movie-{index}.mkv", "generated", servers=[row])
    merged = merge_chain_publishers_best_per_path(manager.get_file_results(job.id))
    for result in (aggregate["plex"], merged[0]):
        assert result["chapter_counts"] == {"updated": 9}
        assert result["frame_sources"] == {"output_existed": 9}
        assert result["counts"] == {"published": 9}


@pytest.mark.parametrize(
    "final,expected",
    [
        ({"status": "ready", "updated": False}, "updated"),
        ({"status": "ready"}, "updated"),
        ({"status": "waiting"}, "waiting"),
        ({"status": "failed"}, "failed"),
        ({"status": "none"}, "none"),
        ({"status": "skipped"}, "skipped"),
    ],
)
def test_chain_retains_chapter_work_only_while_latest_output_is_ready(final, expected):
    rows = []
    for chapter in ({"status": "ready", "updated": True}, final):
        status = {"waiting": "published_pending_chapters", "failed": "published_chapters_failed"}.get(
            chapter["status"], "skipped_output_exists"
        )
        rows.append(
            {
                "file": "/movie.mkv",
                "servers": [{"id": "plex", "status": status, "artifacts": {"chapters": chapter}}],
            }
        )
    merged = merge_chain_publishers_best_per_path(rows)
    assert merged[0]["chapter_counts"] == {expected: 1}


@pytest.mark.parametrize(
    "status,expected",
    [("published_chapters_failed", "failed"), ("published_pending_chapters", "incomplete")],
)
def test_legacy_chapter_attention_without_artifacts_remains_counted(status, expected):
    row = {"server_id": "plex", "status": status}
    aggregate = {}
    fold_publisher_rows_into_aggregate(aggregate, [row])
    merged = merge_chain_publishers_best_per_path([{"file": "/movie.mkv", "servers": [row]}])
    assert aggregate["plex"]["chapter_counts"] == merged[0]["chapter_counts"] == {expected: 1}


@pytest.mark.parametrize(
    "last_status,last_flag,expected",
    [
        ("published_chapters_failed", True, 1),
        ("published_chapters_failed", False, 0),
        ("published_pending_chapters", True, 1),
        ("published_pending_chapters", False, 0),
        ("published", False, 0),
        ("skipped_output_exists", False, 0),
    ],
)
def test_latest_chapter_attempt_controls_retry_eligibility(last_status, last_flag, expected):
    rows = [
        {
            "file": "/movie.mkv",
            "servers": [{"id": "plex", "status": status, "artifacts": {"chapters": {"retryable": retryable}}}],
        }
        for status, retryable in [("published_chapters_failed", not last_flag), (last_status, last_flag)]
    ]
    merged = merge_chain_publishers_best_per_path(rows)
    assert merged[0]["counts"] == {last_status: 1}
    assert publisher_retry_count(merged[0]) == expected


def test_live_chapter_aggregate_keeps_retryable_and_terminal_failures_separate():
    rows = [
        {
            "server_id": "plex",
            "status": status,
            "artifacts": {"chapters": {"retryable": retryable}},
        }
        for status, retryable in [
            ("published_chapters_failed", True),
            ("published_chapters_failed", False),
            ("published_pending_chapters", True),
            ("published_pending_chapters", False),
        ]
    ]
    aggregate = {}
    fold_publisher_rows_into_aggregate(aggregate, rows)
    assert aggregate["plex"]["counts"] == {"published_chapters_failed": 2, "published_pending_chapters": 2}
    assert publisher_retry_count(aggregate["plex"]) == 2


@pytest.mark.parametrize("chapter_status", ["waiting", "failed", "none"])
@pytest.mark.parametrize("check_only", [False, True])
def test_existing_bif_with_no_chapter_output_is_not_counted_as_generated(
    tmp_path, mock_config, monkeypatch, chapter_status, check_only
):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"stable video bytes")
    plex_folder = tmp_path / "plex"
    registry = ServerRegistry.from_settings(
        [
            {
                "id": "plex",
                "type": "plex",
                "name": "Plex",
                "enabled": True,
                "url": "http://plex.invalid",
                "auth": {"token": "test"},
                "libraries": [{"id": "1", "name": "Movies", "remote_paths": [str(tmp_path)], "enabled": True}],
                "output": {
                    "adapter": "plex_bundle",
                    "plex_config_folder": str(plex_folder),
                    "chapter_thumbnails": True,
                },
            }
        ]
    )
    bif = PlexBundleAdapter.bundle_bif_path(str(plex_folder), calculate_plex_hash(source))
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    plan = ChapterPlan(
        None,
        str(source),
        get_source_fingerprint(source),
        tmp_path / "Chapters",
        {},
        outcome=ChapterOutcome(
            chapter_status, message="Chapter metadata result", retryable=chapter_status == "waiting"
        ),
    )
    # Supply the metadata decision; the real preview fast path and output accounting run below.
    monkeypatch.setattr("media_preview_generator.processing.chapters.prepare_chapters", lambda *_a, **_kw: plan)

    result = process_canonical_path(str(source), registry, mock_config, check_only=check_only)

    assert result.status is MultiServerStatus.SKIPPED
    assert bif.read_bytes() == b"existing BIF"
    outcome = _outcome_for_multi_server_status(result.status).value
    assert outcome == "skipped_bif_exists"
    rows = _publisher_rows_from_result(result, str(source))
    manager = JobManager(config_dir=str(tmp_path / "jobs"))
    job = manager.create_job(library_name="Chapters")
    manager.record_file_result(job.id, str(source), outcome, servers=rows)
    stored = manager.get_file_results(job.id)
    publisher = stored[0]["servers"][0]
    assert publisher["artifacts"]["bif"]["status"] == "skipped_output_exists"
    assert publisher["artifacts"]["chapters"]["status"] == chapter_status
    assert publisher_needs_retry(publisher) is (chapter_status == "waiting")
    warning = _chapter_completion_warning(merge_chain_publishers_best_per_path(stored), include_pending=True)
    assert bool(warning) is (chapter_status != "none")


def test_chapter_artifacts_survive_serialization_and_jsonl(tmp_path):
    artifacts = {
        "bif": {"status": "published"},
        "chapters": {
            "status": "pending",
            "completed": 2,
            "total": 3,
            "message": "One chapter is pending",
        },
    }
    result = MultiServerResult(
        "/movie.mkv",
        MultiServerStatus.PUBLISHED,
        [
            PublisherResult(
                "plex",
                "Plex",
                "plex_bundle",
                PublisherStatus.PUBLISHED_PENDING_CHAPTERS,
                artifacts=artifacts,
            )
        ],
    )
    rows = _publisher_rows_from_result(result, "/movie.mkv")
    assert rows[0]["artifacts"] == artifacts
    manager = JobManager(config_dir=str(tmp_path))
    job = manager.create_job(library_name="Chapters")
    manager.record_file_result(job.id, "/movie.mkv", "generated", servers=rows)
    stored = manager.get_file_results(job.id)[0]["servers"][0]
    assert stored["status"] == "published_pending_chapters"
    assert stored["artifacts"] == artifacts


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["published", "published_pending_chapters"], "published_pending_chapters"),
        (["published_pending_chapters", "published_chapters_failed"], "published_chapters_failed"),
        (["published_pending_chapters", "published"], "published"),
        (["published_pending_chapters", "skipped_output_exists"], "skipped_output_exists"),
    ],
)
def test_retry_merge_does_not_hide_incomplete_chapters_behind_bif_success(statuses, expected):
    rows = [{"file": "/movie.mkv", "servers": [{"id": "plex", "status": status}]} for status in statuses]
    aggregate = merge_chain_publishers_best_per_path(rows)
    assert aggregate[0]["counts"] == {expected: 1}


@pytest.mark.parametrize("original_source", ["extracted", "cache_hit", "output_existed"])
def test_chapter_only_repair_keeps_original_bif_provenance(tmp_path, original_source):
    manager = JobManager(config_dir=str(tmp_path))
    job = manager.create_job(library_name="Chapters")
    for status, source in [("published_pending_chapters", original_source), ("published", "output_existed")]:
        manager.record_file_result(
            job.id,
            "/movie.mkv",
            "generated",
            servers=[
                {
                    "server_id": "plex",
                    "status": status,
                    "frame_source": source,
                }
            ],
        )
    rows = manager.get_file_results(job.id, dedup_by_path=False)
    aggregate = merge_chain_publishers_best_per_path(rows)
    assert aggregate[0]["counts"] == {"published": 1}
    assert aggregate[0]["frame_sources"] == {original_source: 1}
