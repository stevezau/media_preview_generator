"""A multi-server full scan dispatches at its job's priority.

The gate admits the job at ``job.priority``; the dispatcher then orders items
by the tracker's priority. A scan submitted at NORMAL regardless of the job
let a Low scan compete with Normal work, and a High one wait behind it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs import orchestrator
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.web.jobs import PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_NORMAL

MODULE = "media_preview_generator.jobs.orchestrator"


def _config():
    return SimpleNamespace(
        gpu_threads=0,
        cpu_threads=1,
        working_tmp_folder="/tmp/work",
        plex_url="",
        plex_token="",
        webhook_paths=None,
        server_id_filter=None,
        plex_library_ids=None,
    )


def _dispatch_with_fake_dispatcher(priority):
    dispatcher = MagicMock()
    dispatcher.submit_items.return_value.get_result.return_value = {"outcome": {}}
    items = [(MagicMock(), ProcessableItem(canonical_path="/data/a.mkv", server_id="srv-a"))]
    with patch("media_preview_generator.jobs.dispatcher.get_dispatcher", return_value=dispatcher):
        orchestrator._dispatch_processable_items(
            items,
            config=_config(),
            registry=MagicMock(),
            selected_gpus=[],
            job_id="job-1",
            priority=priority,
        )
    return dispatcher.submit_items.call_args.kwargs


class TestScanDispatchPriority:
    @pytest.mark.parametrize("priority", [PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW])
    def test_items_submitted_at_job_priority(self, priority):
        kwargs = _dispatch_with_fake_dispatcher(priority)

        assert kwargs["job_id"] == "job-1"
        assert kwargs["priority"] == priority

    def test_items_submitted_at_normal_when_no_priority_given(self):
        kwargs = _dispatch_with_fake_dispatcher(None)

        assert kwargs["priority"] == PRIORITY_NORMAL

    def test_full_scan_forwards_priority_to_dispatch(self):
        registry = MagicMock()
        registry.configs.return_value = [SimpleNamespace(id="srv-a", enabled=True)]
        with (
            patch(f"{MODULE}._build_multi_server_registry", return_value=registry),
            patch(f"{MODULE}._enumerate_items_for_servers", return_value=([("cfg", "item")], [])),
            patch(f"{MODULE}._dispatch_processable_items", return_value={}) as mock_dispatch,
        ):
            orchestrator._run_full_scan_multi_server(_config(), selected_gpus=[], job_id="job-1", priority=PRIORITY_LOW)

        assert mock_dispatch.call_args.kwargs["priority"] == PRIORITY_LOW

    def test_run_processing_forwards_job_priority_to_full_scan(self):
        with (
            patch(f"{MODULE}._resolve_pinned_server", return_value=(None, None)),
            patch(f"{MODULE}._classify_processing_mode", return_value="full_scan"),
            patch(f"{MODULE}._should_use_multi_server_full_scan", return_value=True),
            patch(f"{MODULE}._run_full_scan_multi_server", return_value={}) as mock_scan,
        ):
            orchestrator.run_processing(_config(), selected_gpus=[], job_id="job-1", priority=PRIORITY_HIGH)

        assert mock_scan.call_args.kwargs["job_id"] == "job-1"
        assert mock_scan.call_args.kwargs["priority"] == PRIORITY_HIGH

    def test_run_processing_forwards_job_priority_to_a_recently_added_scan(self):
        # A scheduled Recently Added scan runs through run_processing too (its config carries recently_added_since).
        config = _config()
        config.recently_added_since = orchestrator._utcnow()
        config.recently_added_library_ids = ["7"]
        with (
            patch(f"{MODULE}._resolve_pinned_server", return_value=(None, None)),
            patch(f"{MODULE}._run_recently_added_multi_server", return_value={}) as mock_scan,
        ):
            orchestrator.run_processing(config, selected_gpus=[], job_id="job-1", priority=PRIORITY_HIGH)

        assert mock_scan.call_args.kwargs["job_id"] == "job-1"
        assert mock_scan.call_args.kwargs["library_ids"] == ["7"]
        assert mock_scan.call_args.kwargs["priority"] == PRIORITY_HIGH

    def test_recently_added_scan_forwards_priority_to_dispatch(self):
        registry = MagicMock()
        registry.configs.return_value = [SimpleNamespace(id="srv-a", enabled=True, type="plex")]
        item = ProcessableItem(canonical_path="/data/a.mkv", server_id="srv-a")
        with (
            patch(f"{MODULE}._build_multi_server_registry", return_value=registry),
            patch(f"{MODULE}._enumerate_items_for_servers", return_value=([("cfg", item)], [])),
            patch(f"{MODULE}._remember_item_origins"),
            patch(f"{MODULE}._queue_intro_credits_follow_ups"),
            patch(f"{MODULE}._dispatch_processable_items", return_value={}) as mock_dispatch,
        ):
            orchestrator._run_recently_added_multi_server(
                _config(), selected_gpus=[], job_id="job-1", priority=PRIORITY_LOW
            )

        assert mock_dispatch.call_args.kwargs["priority"] == PRIORITY_LOW


class TestModesTheEarlyReturnsTake:
    @pytest.mark.parametrize("mode", ["recently_added", "refuse_malformed_webhook"])
    def test_a_mode_an_early_return_should_have_taken_fails_loudly_naming_both(self, mode):
        # run_processing returns early for a Recently Added scan and for a malformed webhook job; the later dispatch
        # only knows webhook paths and full scans. If an edit ever lifts either early return, the job must fail
        # loudly there instead of degrading into a full scan, and the message must say which returns to look at.
        modes = iter(["full_scan", "full_scan", mode])
        with (
            patch(f"{MODULE}._resolve_pinned_server", return_value=(None, None)),
            patch(f"{MODULE}._classify_processing_mode", side_effect=lambda config: next(modes)),
            patch(f"{MODULE}._should_use_multi_server_full_scan", return_value=False),
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as sm,
            patch(f"{MODULE}._run_plex_full_scan_phase") as full_scan,
            pytest.raises(AssertionError) as raised,
        ):
            sm.return_value.get.return_value = []
            orchestrator.run_processing(_config(), selected_gpus=[], job_id="job-1")
        full_scan.assert_not_called()
        message = str(raised.value)
        assert repr(mode) in message
        assert "Recently Added" in message and "malformed webhook" in message
