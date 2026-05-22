"""Scan-phase decoupling on the full-scan dispatcher (issue #243).

The orchestrator runs a cheap ``check_only=True`` pass on every item BEFORE
acquiring a generation permit. Items that are already fresh (or otherwise
terminal) are recorded without ever taking a generator slot; only items that
report NEEDS_GENERATION go on to a (capped) generation call. These tests lock
in:

* a terminal scan result takes NO generation call (no permit/slot),
* a NEEDS_GENERATION item takes exactly one generation call,
* the generation cap holds even when the scan sweeps at higher concurrency,
* per-item accounting still matches the item count,
* skipped items are attributed to the "Library scan" worker label.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from media_preview_generator.jobs.orchestrator import _run_full_scan_multi_server
from media_preview_generator.processing.multi_server import MultiServerStatus
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerConfig, ServerType


def _config(cpu_threads: int = 1, scan_workers: int = 0):
    return SimpleNamespace(
        gpu_threads=0,
        cpu_threads=cpu_threads,
        scan_workers=scan_workers,
        working_tmp_folder="/tmp/work",
        plex_url="",
        plex_token="",
        webhook_paths=None,
        server_id_filter=None,
    )


def _server_config(server_id, server_type=ServerType.JELLYFIN):
    return ServerConfig(
        id=server_id,
        type=server_type,
        name=f"Test {server_type.value}",
        enabled=True,
        url="http://test",
        auth={"access_token": "t"},
    )


def _registry_with_items(items):
    cfg = _server_config("srv-a", ServerType.JELLYFIN)
    registry_mock = MagicMock()
    registry_mock.configs.return_value = [cfg]
    proc = MagicMock()
    proc.list_canonical_paths.return_value = iter(items)
    return registry_mock, proc


def _skip_result(path):
    return MagicMock(
        status=MultiServerStatus.SKIPPED,
        publishers=[MagicMock(status=MagicMock(value="skipped_output_exists"))],
        canonical_path=path,
        message="All outputs fresh; FFmpeg skipped",
    )


def _published_result(path):
    return MagicMock(
        status=MultiServerStatus.PUBLISHED,
        publishers=[MagicMock(status=MagicMock(value="published"))],
        canonical_path=path,
        message="",
    )


def _run(config, mock_pcp, items, selected_gpus=None):
    registry_mock, proc = _registry_with_items(items)
    with (
        patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
        patch("media_preview_generator.processing.get_processor_for", return_value=proc),
        patch("media_preview_generator.processing.multi_server.process_canonical_path", mock_pcp),
    ):
        mock_sm.return_value.get.return_value = [{"id": "srv-a", "type": "jellyfin", "enabled": True}]
        mock_registry.from_settings.return_value = registry_mock
        return _run_full_scan_multi_server(config, selected_gpus=selected_gpus or [])


class TestScanPhaseSkip:
    def test_fresh_item_takes_no_generation_call(self):
        """An already-fresh item is decided by the scan pass alone."""
        items = [ProcessableItem(canonical_path="/data/fresh.mkv", server_id="srv-a")]

        def pcp(**kwargs):
            assert kwargs.get("check_only") is True, "a fresh item must never reach a generation call"
            return _skip_result(kwargs["canonical_path"])

        mock_pcp = MagicMock(side_effect=pcp)
        counts = _run(_config(cpu_threads=2), mock_pcp, items)

        gen_calls = [c for c in mock_pcp.call_args_list if not c.kwargs.get("check_only")]
        assert gen_calls == [], "fresh item must not trigger any generation (no permit/slot)"
        assert counts.get("skipped_output_exists", 0) == 1

    def test_needs_generation_item_takes_one_generation_call(self):
        items = [ProcessableItem(canonical_path="/data/new.mkv", server_id="srv-a")]

        def pcp(**kwargs):
            if kwargs.get("check_only"):
                return MagicMock(status=MultiServerStatus.NEEDS_GENERATION, publishers=[])
            return _published_result(kwargs["canonical_path"])

        mock_pcp = MagicMock(side_effect=pcp)
        counts = _run(_config(cpu_threads=2), mock_pcp, items)

        gen_calls = [c for c in mock_pcp.call_args_list if not c.kwargs.get("check_only")]
        assert len(gen_calls) == 1
        assert counts.get("published", 0) == 1

    def test_skipped_item_attributed_to_library_scan_worker(self):
        """Files-panel attribution: a scan-decided skip is labelled 'Library scan'."""
        items = [ProcessableItem(canonical_path="/data/fresh.mkv", server_id="srv-a")]
        mock_pcp = MagicMock(side_effect=lambda **kw: _skip_result(kw["canonical_path"]))

        with patch("media_preview_generator.processing.generator._notify_file_result") as mock_notify:
            _run(_config(cpu_threads=2), mock_pcp, items)

        # worker_label is the 4th positional arg to _notify_file_result.
        assert mock_notify.called
        worker_label = mock_notify.call_args.args[3]
        assert worker_label == "Library scan", f"skip should be attributed to 'Library scan', got {worker_label!r}"


class TestScanPhaseAccountingParity:
    def test_mixed_skip_and_generate_counts_every_item(self):
        items = [ProcessableItem(canonical_path=f"/data/skip-{i}.mkv", server_id="srv-a") for i in range(3)] + [
            ProcessableItem(canonical_path=f"/data/gen-{i}.mkv", server_id="srv-a") for i in range(2)
        ]

        def pcp(**kwargs):
            path = kwargs["canonical_path"]
            if kwargs.get("check_only"):
                if "skip-" in path:
                    return _skip_result(path)
                return MagicMock(status=MultiServerStatus.NEEDS_GENERATION, publishers=[])
            return _published_result(path)

        mock_pcp = MagicMock(side_effect=pcp)
        counts = _run(_config(cpu_threads=2), mock_pcp, items)

        # 3 skipped + 2 published, every item accounted for.
        assert counts.get("skipped_output_exists", 0) == 3
        assert counts.get("published", 0) == 2


class TestGenerationCapHoldsUnderHighScanConcurrency:
    def test_concurrent_generations_never_exceed_cap(self):
        """The core #243 safety property: even when many items sweep through
        the scan at high concurrency, the number of *simultaneous* generation
        calls never exceeds the generator count (cpu_threads here).
        """
        cap = 2
        n_items = 24
        items = [ProcessableItem(canonical_path=f"/data/x-{i}.mkv", server_id="srv-a") for i in range(n_items)]

        active = 0
        max_active = 0
        lock = threading.Lock()

        def pcp(**kwargs):
            nonlocal active, max_active
            if kwargs.get("check_only"):
                # Every item needs generation → forces them all to contend
                # for the capped permits.
                return MagicMock(status=MultiServerStatus.NEEDS_GENERATION, publishers=[])
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.02)  # hold the permit long enough to overlap
            finally:
                with lock:
                    active -= 1
            return _published_result(kwargs["canonical_path"])

        mock_pcp = MagicMock(side_effect=pcp)
        # scan_workers high so the sweep itself is wide; generation cap = cpu_threads.
        counts = _run(_config(cpu_threads=cap, scan_workers=32), mock_pcp, items)

        assert max_active <= cap, (
            f"generation concurrency exceeded the cap: max_active={max_active} > cap={cap}. "
            "The scan phase must not let more than `generators` FFmpeg calls run at once."
        )
        assert counts.get("published", 0) == n_items
