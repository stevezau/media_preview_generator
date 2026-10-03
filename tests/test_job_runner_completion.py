"""Tests for the job-completion badge classification rule.

The classifier decides whether a finished job ends the run with a red
"Failed" badge or an amber "Completed with warnings" badge. The rule
used to start with ``bool(failures)`` — any single FFmpeg crash flipped
the badge red regardless of how many items succeeded. On the 128k-item
scan job ``deea99db`` this meant 1 failure out of 128000 successes
rendered as "Failed", which misrepresented the run.

The fix: only treat the run as a hard failure when nothing succeeded.
Partial-failure jobs drop to the warning branch the code already had.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from media_preview_generator.web.routes.job_runner import (
    _chain_leftover_warning,
    _classify_job_completion,
    _file_problem_clauses,
    _format_retry_wait_server_label,
    _not_found_message,
    _not_indexed_message,
    _retry_completion_message,
    _retry_job_label,
)


def _job(publishers=None, *, server_id=None, server_name=None, server_type=None, config=None):
    """Stand-in for a ``Job`` row: only the attributes the SUT reads."""
    return SimpleNamespace(
        publishers=publishers or [],
        server_id=server_id,
        server_name=server_name,
        server_type=server_type,
        config=config or {},
    )


def _pending_pub(server_name, server_type, count=1):
    return {
        "server_name": server_name,
        "server_type": server_type,
        "counts": {"published_pending_registration": count},
    }


class TestFormatRetryWaitServerLabel:
    """Covers the five branches that produce different retry-wait copy.

    The fix this guards (chain ``2f7132d5``, 2026-05-13) was a label-rendering
    regression where the source-pill server was conflated with the publish
    target. Each cell below produces visibly different copy — the matrix
    must be tested so the conflation can't silently come back.
    """

    def test_single_pending_publisher_uses_publisher_name(self):
        parent = _job(publishers=[_pending_pub("Jellyfin NAS", "jellyfin")])
        retry = _job(server_id="plex-main", server_name="Plex", server_type="plex")
        assert _format_retry_wait_server_label(parent, retry) == "Jellyfin NAS"

    def test_two_pending_publishers_use_and(self):
        parent = _job(
            publishers=[
                _pending_pub("Plex Living Room", "plex"),
                _pending_pub("Jellyfin NAS", "jellyfin"),
            ]
        )
        assert _format_retry_wait_server_label(parent, _job()) == "Plex Living Room and Jellyfin NAS"

    def test_three_plus_pending_publishers_use_oxford_comma(self):
        parent = _job(
            publishers=[
                _pending_pub("Plex", "plex"),
                _pending_pub("Emby", "emby"),
                _pending_pub("Jellyfin", "jellyfin"),
            ]
        )
        assert _format_retry_wait_server_label(parent, _job()) == "Plex, Emby, and Jellyfin"

    def test_pin_matches_source_when_no_publishers_data(self):
        """No parent publishers info, but an explicit publish-pin equal to
        the source attribution → name that server.
        """
        retry = _job(
            server_id="plex-main",
            server_name="Plex Main",
            server_type="plex",
            config={"server_id": "plex-main"},
        )
        assert _format_retry_wait_server_label(None, retry) == "Plex Main"

    def test_pin_differs_from_source_falls_back_to_generic(self):
        """Source attribution (top-level server_id) is Plex, but the explicit
        publish-pin is something else — naming the source would re-introduce
        the chain ``2f7132d5`` source-vs-target conflation, so we go generic.
        """
        retry = _job(
            server_id="plex-main",
            server_name="Plex",
            server_type="plex",
            config={"server_id": "jellyfin-nas"},
        )
        assert _format_retry_wait_server_label(None, retry) == "the media server"

    def test_no_publishers_no_pin_falls_back_to_generic(self):
        assert _format_retry_wait_server_label(None, _job()) == "the media server"

    @pytest.mark.parametrize(
        "publishers",
        [
            [{"server_name": "X", "counts": "not-a-dict"}],
            ["not-a-dict"],
            [{"server_name": "Y", "counts": {"some_other_status": 5}}],
        ],
    )
    def test_malformed_or_non_pending_publishers_dont_name_a_server(self, publishers):
        """Defensive: persisted ``publishers`` is JSON-text so a partial write
        or hand-edited row could surface non-dict entries or unfamiliar status
        keys. Those must collapse to the generic fallback, not raise.
        """
        assert _format_retry_wait_server_label(_job(publishers=publishers), _job()) == "the media server"


class TestClassifyJobCompletion:
    def test_single_ffmpeg_crash_amid_128k_successes_is_warning_not_failure(self):
        """Production incident deea99db: 128000 succeeded, 1 FFmpeg crash,
        1 silent publisher failure. Users saw a red badge and assumed the
        whole run broke. Anything with even one successful publish is a
        warning, never a hard failure.
        """
        result = _classify_job_completion(
            failures=[{"file": "/anime.mkv", "exit_code": 218, "reason": "x"}],
            outcome={
                "generated": 1,
                "skipped_output_exists": 127999,
                "failed": 2,
            },
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=0,
            resolved_count=0,
        )
        assert result == "warning", (
            f"1-in-128k FFmpeg crash must not flip the badge red when 128000 items succeeded; "
            f"got {result!r}. This was the deea99db regression."
        )

    def test_every_item_failed_is_hard_failure(self):
        """When nothing succeeded, the badge must be red — the all-failed
        gate is what the user relies on to see "go fix this now".
        """
        result = _classify_job_completion(
            failures=[{"file": "/a.mkv", "exit_code": 1, "reason": "x"}],
            outcome={"failed": 10},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=0,
            resolved_count=0,
        )
        assert result == "error"

    def test_retry_exhausted_with_no_successes_is_hard_failure(self):
        """A retry chain that exhausts AND served nothing (no previews made)
        is a terminal failure — the user's webhook never got served.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={"generated": 0, "skipped_file_not_found": 1},
            is_retry=True,
            retry_paths=["/still-missing.mkv"],
            spawned_retry_id=None,
            total_paths=1,
            resolved_count=1,
        )
        assert result == "error"

    def test_retry_exhausted_with_successes_is_warning_not_failure(self):
        """A retry chain that exhausted but still GENERATED previews is a
        partial failure (amber), not a total failure (red). Regression: job
        67f7bf2a generated 11 previews with 0 hard failures but showed red
        "Failed" purely because the chain couldn't register a few genuinely-
        missing files. retry_exhausted must respect success_total like every
        other gate.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={"generated": 11, "skipped_file_not_found": 4},
            is_retry=True,
            retry_paths=["/still-missing-1.mkv", "/still-missing-2.mkv"],
            spawned_retry_id=None,
            total_paths=15,
            resolved_count=15,
        )
        assert result == "warning"

    def test_all_not_found_without_spawn_is_hard_failure(self):
        """Every file Plex resolved was missing on disk AND no retry was
        scheduled: nothing more is going to happen, so surface as a hard
        failure rather than letting it look green-with-a-footnote.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={"generated": 0, "skipped_file_not_found": 5},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=5,
            resolved_count=5,
        )
        assert result == "error"

    @pytest.mark.parametrize("spawned_retry_id", [None, "retry-1"], ids=["no-retry", "retry-queued"])
    @pytest.mark.parametrize("success_key", ["generated", "skipped_bif_exists", "published", "skipped_output_exists"])
    def test_not_found_beside_any_success_is_warning(self, success_key, spawned_retry_id):
        """Nightly scan c091d637: 115,831 previews already existed, 6 files weren't found, 2 failed, and the badge
        was red "Failed … check path mapping configuration". Files not found only fail the job when nothing in any
        success column succeeded, whichever column that is.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={success_key: 115831, "skipped_file_not_found": 6, "failed": 2},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=spawned_retry_id,
            total_paths=0,
            resolved_count=0,
        )
        assert result == "warning"

    def test_all_not_found_with_a_retry_queued_is_warning(self):
        """Nothing succeeded yet, but a retry is queued for the missing files: not a final failure."""
        result = _classify_job_completion(
            failures=[],
            outcome={"generated": 0, "skipped_file_not_found": 5},
            is_retry=False,
            retry_paths=["/data/a.mkv"],
            spawned_retry_id="retry-1",
            total_paths=5,
            resolved_count=5,
        )
        assert result == "warning"

    def test_nothing_resolved_is_hard_failure(self):
        """The user submitted paths but none resolved against Plex — the
        job did nothing, and the badge must reflect that.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=3,
            resolved_count=0,
        )
        assert result == "error"

    def test_partial_failure_multi_server_scan_is_warning(self):
        """A multi-server full scan with some ``published`` /
        ``skipped_output_exists`` successes and some ``failed`` items
        is an amber warning — the successful ones are real work.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={"published": 50, "skipped_output_exists": 40, "failed": 10},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=0,
            resolved_count=0,
        )
        assert result == "warning"

    def test_no_owners_alone_is_warning_not_error(self):
        """A ``no_owners`` outcome means the files are outside any
        configured library — that's a config hint, not a broken run.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={"published": 5, "no_owners": 2},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=0,
            resolved_count=0,
        )
        assert result == "warning"


class TestRetryCompletionMessage:
    """Cover the matrix that produces the per-job tail log line for retries.

    Live regression (2026-05-14, 15 failed jobs): every exhausted retry
    chain wrote ``INFO - Retry job completed successfully`` on the child
    job's tail even though the parent had just been marked FAILED with
    "Source server did not register N file(s) after 3 retry attempts".
    The contradiction made per-job logs unreliable for diagnosis.

    The branching variable is ``(retry_paths empty?, spawned_retry_id set?)``.
    Cell ``(non-empty, None)`` is chain-exhausted — the only cell that
    must NOT log success. The other live cells route through different
    code paths (``error_parts`` non-empty), but the helper still has to
    handle the scheduled-retry case for completeness.
    """

    def test_chain_exhausted_logs_warning_with_pending_summary(self):
        """retry_paths non-empty + no spawn = chain exhausted = WARNING."""
        level, msg = _retry_completion_message(
            retry_paths=["/data/x.mkv"],
            spawned_retry_id=None,
            pending_by_server={"JellyTest": 1},
            effective_max=3,
        )
        assert level == "WARNING", "exhausted chains must NOT log INFO success"
        assert msg == (
            "1 file(s) still weren't indexed by the media server after 3 retries, so no more retries are queued. "
            "The next scheduled scan will pick them up. (JellyTest pending × 1)"
        )

    def test_chain_exhausted_orders_pending_servers_by_count(self):
        """Multi-server pending counts sort highest-first so the most-blocked
        server is named first — keeps the message scannable when 3+ servers
        are pending."""
        level, msg = _retry_completion_message(
            retry_paths=["/a.mkv", "/b.mkv", "/c.mkv"],
            spawned_retry_id=None,
            pending_by_server={"Plex": 1, "JellyTest": 3, "EmbyTest": 2},
            effective_max=3,
        )
        assert level == "WARNING"
        assert msg.index("JellyTest pending × 3") < msg.index("EmbyTest pending × 2") < msg.index("Plex pending × 1"), (
            f"pending servers must sort by count descending; got {msg!r}"
        )

    def test_chain_exhausted_falls_back_when_pending_by_server_empty(self):
        """If pending_by_server got cleared but retry_paths still has entries,
        the message must still carry a count rather than ending with a stray
        semicolon — exhausted-chain warnings always need *some* signal.
        """
        level, msg = _retry_completion_message(
            retry_paths=["/a.mkv", "/b.mkv"],
            spawned_retry_id=None,
            pending_by_server={},
            effective_max=3,
        )
        assert level == "WARNING"
        assert "2 path(s) still pending" in msg, f"empty pending_by_server must fall back to a path count; got {msg!r}"

    def test_retry_succeeded_logs_info_success(self):
        """retry_paths empty = chain succeeded = original INFO message preserved."""
        level, msg = _retry_completion_message(
            retry_paths=[],
            spawned_retry_id=None,
            pending_by_server={},
            effective_max=3,
        )
        assert level == "INFO"
        assert msg == "Retry job completed successfully"

    def test_spawned_next_retry_logs_info_success(self):
        """If we DID spawn another retry, this child completed its work and
        the chain is still alive — INFO is correct (the WARNING about the
        next-retry schedule is logged elsewhere, lines 1239-1242).
        """
        level, msg = _retry_completion_message(
            retry_paths=["/a.mkv"],
            spawned_retry_id="next-retry-job-id",
            pending_by_server={"JellyTest": 1},
            effective_max=3,
        )
        assert level == "INFO"
        assert msg == "Retry job completed successfully"


class TestItemCompleteLogLevel:
    """The checking stage finishes every already-done file of a scheduled scan; its "Library scan completed: …
    (success)" line per file was a quarter of 557k INFO lines in 12 h. It's DEBUG now; the job's "Processing complete:
    N already existed" line counts those files. Failures and worker completions stay INFO."""

    @pytest.mark.parametrize(
        ("display_name", "success", "level"),
        [
            ("Library scan", True, "DEBUG"),
            ("Library scan", False, "INFO"),
            ("GPU Worker 3 (NVIDIA TITAN RTX)", True, "INFO"),
            ("CPU Worker 1", False, "INFO"),
        ],
        ids=["check-stage-done", "check-stage-failed", "worker-done", "worker-failed"],
    )
    def test_level(self, display_name, success, level):
        from loguru import logger

        from media_preview_generator.jobs.dispatcher import CHECK_STAGE_WORKER_NAME
        from media_preview_generator.web.routes.job_runner import _log_item_complete

        assert CHECK_STAGE_WORKER_NAME == "Library scan"
        records: list[tuple[str, str]] = []
        sink = logger.add(lambda m: records.append((m.record["level"].name, m.record["message"])), level="DEBUG")
        try:
            _log_item_complete(display_name, "CSI S05E06", success)
        finally:
            logger.remove(sink)
        outcome = "success" if success else "failed"
        assert records == [(level, f"{display_name} completed: 'CSI S05E06' ({outcome})")]


class TestNotIndexedMessage:
    """The completion text for files still not indexed when this job queued no retry for them.

    Job c95a5453, the last of 3 retries, said "They'll be retried automatically — slow backoff: 1m → 2m → 5m → 15m →
    60m" while no retry was queued and its chain head was marked failed. Every cell must say what really happens.
    """

    @pytest.mark.parametrize(
        ("is_retry", "retry_attempt", "effective_max", "expected"),
        [
            (
                True,
                3,
                3,
                "2 file(s) still weren't indexed by the media server after 3 retries, so no more retries are queued. "
                "The next scheduled scan will pick them up.",
            ),
            (
                True,
                1,
                1,
                "2 file(s) still weren't indexed by the media server after 1 retry, so no more retries are queued. "
                "The next scheduled scan will pick them up.",
            ),
            (
                False,
                0,
                0,
                "2 file(s) weren't indexed by the media server yet, and retries are off (Settings → Retry policy). "
                "The next scheduled scan will pick them up.",
            ),
            (
                False,
                0,
                3,
                "2 file(s) weren't indexed by the media server yet, and no retry was queued for them. "
                "The next scheduled scan will pick them up.",
            ),
            (
                True,
                1,
                3,
                "2 file(s) weren't indexed by the media server yet, and no retry was queued for them. "
                "The next scheduled scan will pick them up.",
            ),
        ],
        ids=["last-retry", "last-of-one-retry", "retries-off", "original-not-flagged", "mid-chain-not-flagged"],
    )
    def test_message_says_what_happens_next(self, is_retry, retry_attempt, effective_max, expected):
        msg = _not_indexed_message(2, is_retry=is_retry, retry_attempt=retry_attempt, effective_max=effective_max)
        assert msg == expected
        assert "retried automatically" not in msg and "backoff" not in msg


class TestNotFoundMessage:
    """What a job says about files that weren't on disk. Only a job where nothing succeeded blames path mappings."""

    @pytest.mark.parametrize(
        ("nothing_succeeded", "retry_scheduled", "expected"),
        [
            (True, False, "6 of 20 items skipped (file not found locally) — check path mapping configuration"),
            (True, True, "6 of 20 items had stale Plex paths — Plex rescan triggered, retry scheduled"),
            (False, False, "6 file(s) weren't found on disk"),
            (False, True, "6 file(s) weren't found on disk"),
        ],
        ids=["nothing-succeeded", "nothing-succeeded-retry-queued", "partial", "partial-retry-queued"],
    )
    def test_message(self, nothing_succeeded, retry_scheduled, expected):
        msg = _not_found_message(6, 20, nothing_succeeded=nothing_succeeded, retry_scheduled=retry_scheduled)
        assert msg == expected


class TestFileProblemClauses:
    """A finished job's summary says each count once. Scan f9ced906 ended "2 failed file(s). 286 file(s) weren't found
    on disk. 2 of 116125 item(s) failed (but 115806 succeeded)": the two failed files twice."""

    PATH_MAPPINGS = "items skipped (file not found locally) — check path mapping configuration"
    NOTHING_MADE = "item(s) failed; no previews were generated. Check the per-item logs above."

    @pytest.mark.parametrize(
        ("failure_count", "outcome", "retry_scheduled", "expected"),
        [
            (0, {"generated": 3}, False, []),
            (
                2,
                {"generated": 3, "skipped_bif_exists": 5, "failed": 2},
                False,
                ["2 file(s) failed", "8 of 10 file(s) were fine"],
            ),
            (2, {"failed": 2}, False, [f"2 of 2 {NOTHING_MADE}"]),
            (0, {"skipped_bif_exists": 14, "skipped_file_not_found": 6}, False, ["6 file(s) weren't found on disk"]),
            (0, {"skipped_file_not_found": 6}, False, [f"6 of 6 {PATH_MAPPINGS}"]),
            (
                0,
                {"skipped_file_not_found": 6},
                True,
                ["6 of 6 items had stale Plex paths — Plex rescan triggered, retry scheduled"],
            ),
            (
                2,
                {
                    "generated": 185,
                    "skipped_bif_exists": 115621,
                    "skipped_file_not_found": 286,
                    "skipped_source_gone": 31,
                    "failed": 2,
                },
                False,
                ["2 file(s) failed", "286 file(s) weren't found on disk", "115,806 of 116,125 file(s) were fine"],
            ),
            (
                0,
                {"published": 4, "skipped_file_not_found": 1, "failed": 2},
                True,
                ["2 file(s) failed", "1 file(s) weren't found on disk", "4 of 7 file(s) were fine"],
            ),
            # Nothing made and files not found: the not-found clause says the run made nothing; the files FFmpeg
            # failed on are still named.
            (2, {"skipped_file_not_found": 5, "failed": 2}, False, ["2 file(s) failed", f"5 of 7 {PATH_MAPPINGS}"]),
            (0, {"skipped_file_not_found": 5, "failed": 2}, False, [f"5 of 7 {PATH_MAPPINGS}"]),
            (2, {"generated": 3}, False, ["2 file(s) failed"]),
            (2, None, False, ["2 file(s) failed"]),
            (0, {"published": 3, "failed": 1}, False, ["1 file(s) failed", "3 of 4 file(s) were fine"]),
            (1, {"published": 3, "failed": 2}, False, ["2 file(s) failed", "3 of 5 file(s) were fine"]),
            (0, {"skipped_not_indexed": 2, "failed": 1}, False, ["1 file(s) failed", "2 of 3 file(s) were fine"]),
            (1234, {"published": 1, "failed": 1234}, False, ["1,234 file(s) failed", "1 of 1,235 file(s) were fine"]),
        ],
        ids=[
            "no-problem",
            "failed",
            "failed-nothing-made",
            "not-found",
            "not-found-nothing-made",
            "not-found-nothing-made-retry-queued",
            "failed-and-not-found-with-replaced",
            "failed-and-not-found-retry-queued",
            "failed-and-not-found-nothing-made",
            "item-failures-and-not-found-nothing-made",
            "ffmpeg-failures-not-in-the-tally",
            "ffmpeg-failures-no-tally",
            "item-failures-without-ffmpeg-failures",
            "tally-is-the-count",
            "waiting-on-the-server-is-fine",
            "thousands",
        ],
    )
    def test_each_count_is_said_once(self, failure_count, outcome, retry_scheduled, expected):
        assert _file_problem_clauses(failure_count, outcome, retry_scheduled=retry_scheduled) == expected


class TestRetryJobLabel:
    """A retry is named after its parent, but counts the files the retry itself runs ("Retry: 8 files" ran 4)."""

    @pytest.mark.parametrize(
        ("parent_name", "paths", "expected"),
        [
            ("8 files", ["/d/a.mkv", "/d/b.mkv", "/d/c.mkv", "/d/d.mkv"], "Retry: 4 files"),
            ("8 files", ["/d/a.mkv"], "Retry: 1 file"),
            ("Manual: 8 files", ["/d/a.mkv", "/d/b.mkv"], "Retry: Manual: 2 files"),
            ("Retry: 8 files", ["/d/a.mkv", "/d/b.mkv", "/d/c.mkv"], "Retry: 3 files"),
            ("Retry: 1 file", ["/d/a.mkv", "/d/b.mkv"], "Retry: 2 files"),
            ("The Show S01E01", ["/d/a.mkv"], "Retry: The Show S01E01"),
            ("TV Shows", ["/d/a.mkv", "/d/b.mkv"], "Retry: TV Shows"),
            ("", ["/d/a.mkv"], "Retry: a.mkv"),
            ("", ["/d/a.mkv", "/d/b.mkv"], "Retry: 2 files"),
        ],
        ids=[
            "count-name-fewer-paths",
            "count-name-one-path",
            "prefixed-count-name",
            "retry-of-a-retry",
            "one-file-retry-grows",
            "title-kept",
            "library-name-kept",
            "no-parent-name-one-path",
            "no-parent-name",
        ],
    )
    def test_label(self, parent_name, paths, expected):
        assert _retry_job_label(parent_name, paths) == expected


class TestChainLeftoverWarning:
    """A retry chain that finished keeps a warning for its job's files that no retry covered, instead of ending
    green: scan 55b098af had 27 files not on disk beside the one file its retries were for."""

    @pytest.mark.parametrize(
        ("not_found", "failed", "expected"),
        [
            (0, 0, None),
            (27, 0, "27 file(s) weren't found on disk"),
            (0, 2, "2 file(s) failed"),
            (27, 2, "2 file(s) failed. 27 file(s) weren't found on disk"),
        ],
        ids=["all-finished", "not-found", "failed", "both"],
    )
    def test_warning(self, not_found, failed, expected):
        assert _chain_leftover_warning(not_found, failed) == expected
