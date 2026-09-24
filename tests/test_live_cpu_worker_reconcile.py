"""Saving ``cpu_threads`` resizes the live worker pool, the way ``gpu_config`` saves already do.

Production wiring at ``api_settings.py:_apply_post_save_hooks``: when ``cpu_threads``
is in the saved fields, ``WorkerPool.reconcile_cpu_workers`` brings the running
pool to the saved count. Before this, the count only changed on restart.

Every test drives the real ``POST /api/settings`` route against a real
``WorkerPool`` registered as the dispatcher's pool, then counts the pool's
workers — so the saved value reaching the pool is what's asserted, not just
that a hook ran.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs.dispatcher import get_dispatcher, reset_dispatcher
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import (
    SettingsManager,
    get_settings_manager,
    reset_settings_manager,
)

TOKEN = "test-token-12345678"
GPU_DEVICE = "cuda:0"
GPU_INFO = {"name": "Fake GPU", "workers": 1, "ffmpeg_threads": 2}


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    reset_dispatcher()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    reset_dispatcher()
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_AUTH_TOKEN", TOKEN)
    return create_app(config_dir=str(tmp_path))


def _cpu_workers(pool: WorkerPool) -> list:
    return [w for w in pool._snapshot_workers() if w.worker_type == "CPU"]


def _gpu_workers(pool: WorkerPool) -> list:
    return [w for w in pool._snapshot_workers() if w.worker_type == "GPU"]


def _live_pool(app, *, cpu: int, gpu: int = 0) -> WorkerPool:
    """Register a real pool as the dispatcher's, with settings.json agreeing with it."""
    selected = [("nvidia", GPU_DEVICE, {**GPU_INFO, "workers": gpu})] if gpu else []
    with app.app_context():
        get_settings_manager().update({"cpu_threads": cpu})
    pool = WorkerPool(gpu_workers=gpu, cpu_workers=cpu, selected_gpus=selected)
    get_dispatcher(pool)
    return pool


def _save(app, body: dict):
    return app.test_client().post("/api/settings", json=body, headers={"X-Auth-Token": TOKEN})


def _scale(app, action: str, worker_type: str, count: int):
    return app.test_client().post(
        f"/api/workers/{action}",
        json={"worker_type": worker_type, "count": count},
        headers={"X-Auth-Token": TOKEN},
    )


def _saved_cpu_threads(app) -> int:
    with app.app_context():
        return get_settings_manager().cpu_threads


def _gpu_config(workers: int) -> list[dict]:
    return [{"device": GPU_DEVICE, "enabled": True, "workers": workers, "ffmpeg_threads": 2}]


def _fake_detected_gpu():
    """Stand in for GPU detection, so selections come from the saved gpu_config (not a patched result)."""
    from media_preview_generator.web.routes import _helpers

    return patch.dict(_helpers._gpu_cache, {"result": [{"type": "nvidia", "device": GPU_DEVICE, "name": "Fake GPU"}]})


def _gpu_workers_in(value) -> int | None:
    entries = value if isinstance(value, list) else []
    return next((e.get("workers") for e in entries if isinstance(e, dict) and e.get("device") == GPU_DEVICE), None)


class TestSaveSettingsReconcilesCpuWorkers:
    def test_live_pool_resized_when_cpu_threads_saved(self, app):
        pool = _live_pool(app, cpu=2)

        resp = _save(app, {"cpu_threads": 5})

        assert resp.status_code == 200
        assert len(_cpu_workers(pool)) == 5

    def test_live_pool_shrinks_idle_cpu_workers_when_cpu_threads_lowered(self, app):
        pool = _live_pool(app, cpu=4)

        resp = _save(app, {"cpu_threads": 1})

        assert resp.status_code == 200
        assert len(_cpu_workers(pool)) == 1
        assert pool._pending_removals["CPU"] == 0

    def test_busy_cpu_worker_retired_after_task_when_cpu_threads_lowered(self, app):
        pool = _live_pool(app, cpu=2)
        first, second = _cpu_workers(pool)
        first.is_busy = True
        second.is_busy = True

        resp = _save(app, {"cpu_threads": 1})

        # Neither worker is pulled mid-task: both stay, one is due to retire.
        assert resp.status_code == 200
        assert _cpu_workers(pool) == [first, second]
        assert pool._pending_removals["CPU"] == 1
        assert len(_cpu_workers(pool)) - pool._pending_removals["CPU"] == 1

        first.is_busy = False
        assert pool._retire_idle_worker_if_scheduled(first) is True
        assert _cpu_workers(pool) == [second]
        assert pool._pending_removals["CPU"] == 0

    def test_raise_after_pending_shrink_cancels_removal_instead_of_adding(self, app):
        pool = _live_pool(app, cpu=2)
        busy = _cpu_workers(pool)
        for w in busy:
            w.is_busy = True

        _save(app, {"cpu_threads": 1})
        assert pool._pending_removals["CPU"] == 1
        resp = _save(app, {"cpu_threads": 2})

        assert resp.status_code == 200
        assert _cpu_workers(pool) == busy
        assert pool._pending_removals["CPU"] == 0

    def test_unchanged_cpu_threads_with_pending_removal_is_noop(self, app):
        # Autosave repeats the lowered value on every later save; it must not retire a second busy worker.
        pool = _live_pool(app, cpu=2)
        busy = _cpu_workers(pool)
        for w in busy:
            w.is_busy = True

        _save(app, {"cpu_threads": 1})
        resp = _save(app, {"cpu_threads": 1})

        assert resp.status_code == 200
        assert _cpu_workers(pool) == busy
        assert pool._pending_removals["CPU"] == 1

    def test_second_shrink_while_busy_counts_pending_removals(self, app):
        pool = _live_pool(app, cpu=3)
        busy = _cpu_workers(pool)
        for w in busy:
            w.is_busy = True

        _save(app, {"cpu_threads": 2})
        resp = _save(app, {"cpu_threads": 1})

        assert resp.status_code == 200
        assert _cpu_workers(pool) == busy
        assert pool._pending_removals["CPU"] == 2

    def test_unchanged_cpu_threads_is_noop(self, app):
        # The Settings page autosave sends cpu_threads on every save, so an
        # unchanged value must leave the running workers exactly as they are.
        pool = _live_pool(app, cpu=3)
        before = _cpu_workers(pool)
        before[0].is_busy = True

        resp = _save(app, {"cpu_threads": 3})

        assert resp.status_code == 200
        assert _cpu_workers(pool) == before
        assert pool._pending_removals["CPU"] == 0

    def test_cpu_threads_zero_removes_all_cpu_workers(self, app):
        pool = _live_pool(app, cpu=3, gpu=1)

        resp = _save(app, {"cpu_threads": 0})

        assert resp.status_code == 200
        assert _cpu_workers(pool) == []
        assert len(_gpu_workers(pool)) == 1

    def test_no_pool_yet_is_noop(self, app):
        assert get_dispatcher() is None

        resp = _save(app, {"cpu_threads": 4})

        assert resp.status_code == 200
        assert resp.get_json()["cpu_workers_retiring"] == 0
        with app.app_context():
            assert get_settings_manager().cpu_threads == 4
        assert get_dispatcher() is None

    def test_gpu_raise_after_pending_shrink_keeps_busy_workers(self, app):
        # The GPU twin of the CPU row above: lower 3 -> 1 while busy, then back to 3, and 3 must stay.
        pool = _live_pool(app, cpu=0, gpu=3)
        busy = _gpu_workers(pool)
        for w in busy:
            w.is_busy = True

        with _fake_detected_gpu():
            _save(app, {"gpu_config": _gpu_config(1)})
            assert sum(w._pending_removal for w in busy) == 2
            resp = _save(app, {"gpu_config": _gpu_config(3)})

        assert resp.status_code == 200
        for w in busy:
            w.is_busy = False
        pool._apply_deferred_removals()
        assert _gpu_workers(pool) == busy

    def test_gpu_workers_untouched_by_cpu_threads_save(self, app):
        pool = _live_pool(app, cpu=1, gpu=2)
        gpus_before = _gpu_workers(pool)

        resp = _save(app, {"cpu_threads": 3})

        assert resp.status_code == 200
        assert _gpu_workers(pool) == gpus_before
        assert len(_cpu_workers(pool)) == 3

    def test_gpu_and_cpu_change_in_same_save_both_apply(self, app):
        pool = _live_pool(app, cpu=1, gpu=1)
        three_gpu_workers = [("nvidia", GPU_DEVICE, {**GPU_INFO, "workers": 3})]

        with patch(
            "media_preview_generator.web.routes.job_runner._build_selected_gpus",
            return_value=three_gpu_workers,
        ):
            resp = _save(
                app,
                {
                    "cpu_threads": 4,
                    "gpu_config": [{"device": GPU_DEVICE, "enabled": True, "workers": 3, "ffmpeg_threads": 2}],
                },
            )

        assert resp.status_code == 200
        assert len(_gpu_workers(pool)) == 3
        assert {w.gpu_device for w in _gpu_workers(pool)} == {GPU_DEVICE}
        assert len(_cpu_workers(pool)) == 4

    @pytest.mark.parametrize(
        ("start", "busy", "saved", "retiring"),
        [(3, 0, 1, 0), (2, 2, 1, 1), (3, 3, 1, 2)],
        ids=["idle-workers-go-now", "one-busy-worker", "two-busy-workers"],
    )
    def test_save_response_counts_busy_workers_still_retiring(self, app, start, busy, saved, retiring):
        pool = _live_pool(app, cpu=start)
        for w in _cpu_workers(pool)[:busy]:
            w.is_busy = True

        resp = _save(app, {"cpu_threads": saved})

        assert resp.status_code == 200
        assert resp.get_json()["cpu_workers_retiring"] == retiring

    def test_retiring_count_includes_workers_an_earlier_save_scheduled(self, app):
        pool = _live_pool(app, cpu=2)
        for w in _cpu_workers(pool):
            w.is_busy = True

        _save(app, {"cpu_threads": 1})
        resp = _save(app, {"cpu_threads": 0})

        assert resp.get_json()["cpu_workers_retiring"] == 2


class TestJobStartReconcilesCpuWorkers:
    def test_preview_job_pool_sized_from_saved_cpu_threads_not_job_snapshot(self, app, tmp_path):
        # Saved while no pool existed, so the save had nothing to resize; the job's config snapshot still says 2.
        _save(app, {"cpu_threads": 4})
        assert get_dispatcher() is None
        callback = self._capture_pool_callback(app, tmp_path, cpu_threads=2)

        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        callback(pool)

        assert len(_cpu_workers(pool)) == 4

    def test_save_landing_during_job_start_leaves_pool_at_saved_count(self, app, tmp_path):
        _save(app, {"cpu_threads": 2})
        callback = self._capture_pool_callback(app, tmp_path, cpu_threads=2)
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        job_start_read = threading.Event()
        save_done = threading.Event()
        job_start: dict = {}
        real_get = SettingsManager.get

        def get_then_let_the_save_run(self, key, default=None):
            # Job start reads the saved 2, then waits while a save stores and applies 5. Without the lock job start
            # then applies its stale 2. With it, the save can't store 5 until job start has finished applying 2.
            value = real_get(self, key, default)
            if key == "cpu_threads" and threading.get_ident() == job_start.get("thread"):
                job_start_read.set()
                save_done.wait(timeout=1.0)
            return value

        def start_job() -> None:
            job_start["thread"] = threading.get_ident()
            callback(pool)

        with patch.object(SettingsManager, "get", get_then_let_the_save_run):
            starter = threading.Thread(target=start_job)
            starter.start()
            assert job_start_read.wait(timeout=5)
            save_status = _save(app, {"cpu_threads": 5}).status_code
            save_done.set()
            starter.join(timeout=10)

        assert save_status == 200
        assert _saved_cpu_threads(app) == 5
        assert len(_cpu_workers(pool)) == 5

    def test_save_landing_during_job_start_leaves_pool_at_saved_gpu_config(self, app, tmp_path):
        with _fake_detected_gpu():
            _save(app, {"gpu_config": _gpu_config(1)})
            callback = self._capture_pool_callback(app, tmp_path, cpu_threads=1)
            pool = WorkerPool(gpu_workers=1, cpu_workers=0, selected_gpus=[("nvidia", GPU_DEVICE, dict(GPU_INFO))])
            job_start_read = threading.Event()
            save_done = threading.Event()
            job_start: dict = {}
            real_get = SettingsManager.get

            def get_then_let_the_save_run(self, key, default=None):
                # Job start reads the saved 1-worker config, then waits while a save stores and applies 4. Without
                # the lock job start then applies its stale 1.
                value = real_get(self, key, default)
                if (
                    key == "gpu_config"
                    and threading.get_ident() == job_start.get("thread")
                    and not job_start_read.is_set()
                ):
                    job_start_read.set()
                    save_done.wait(timeout=1.0)
                return value

            def start_job() -> None:
                job_start["thread"] = threading.get_ident()
                callback(pool)

            with patch.object(SettingsManager, "get", get_then_let_the_save_run):
                starter = threading.Thread(target=start_job)
                starter.start()
                assert job_start_read.wait(timeout=5)
                save_status = _save(app, {"gpu_config": _gpu_config(4)}).status_code
                save_done.set()
                starter.join(timeout=10)

        assert save_status == 200
        assert len(_gpu_workers(pool)) == 4

    @staticmethod
    def _capture_pool_callback(app, tmp_path, *, cpu_threads: int):
        """Start a preview job with run_processing stubbed; return the worker_pool_callback it was handed."""
        captured: dict = {}
        stale_config = MagicMock(
            cpu_threads=cpu_threads,
            gpu_threads=0,
            path_mappings=[],
            tmp_folder=str(tmp_path),
            plex_url="http://test",
            plex_token="token",
        )
        with (
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=lambda config, *a, **kw: captured.update(kw),
            ),
            patch("media_preview_generator.config.load_config", return_value=stale_config),
            patch(
                "media_preview_generator.processing.generator._verify_tmp_folder_health",
                return_value=(True, []),
            ),
            patch("media_preview_generator.utils.setup_working_directory", return_value=str(tmp_path / "work")),
            patch("media_preview_generator.gpu.detect.detect_all_gpus", return_value=[]),
        ):
            resp = app.test_client().post("/api/jobs", json={}, headers={"X-Auth-Token": TOKEN})
        assert resp.status_code == 201
        return captured["worker_pool_callback"]


class TestWorkersApiSavesCpuCount:
    """``/api/workers/add|remove`` save the CPU count, so the next Settings save doesn't undo them."""

    def test_add_cpu_saves_count_and_resizes_pool(self, app):
        pool = _live_pool(app, cpu=2)

        resp = _scale(app, "add", "CPU", 1)

        assert resp.status_code == 200
        assert resp.get_json()["added"] == 1
        assert len(_cpu_workers(pool)) == 3
        assert _saved_cpu_threads(app) == 3

    def test_remove_idle_cpu_saves_count_and_resizes_pool(self, app):
        pool = _live_pool(app, cpu=3)

        resp = _scale(app, "remove", "CPU", 1)

        assert resp.status_code == 200
        data = resp.get_json()
        assert (data["removed"], data["scheduled_removal"], data["unavailable"]) == (1, 0, 0)
        assert len(_cpu_workers(pool)) == 2
        assert _saved_cpu_threads(app) == 2

    def test_remove_busy_cpu_waits_for_current_file_and_saves_count(self, app):
        pool = _live_pool(app, cpu=2)
        busy = _cpu_workers(pool)
        for w in busy:
            w.is_busy = True

        resp = _scale(app, "remove", "CPU", 1)

        data = resp.get_json()
        assert (data["removed"], data["scheduled_removal"], data["unavailable"]) == (0, 1, 0)
        assert _cpu_workers(pool) == busy
        assert pool._pending_removals["CPU"] == 1
        assert _saved_cpu_threads(app) == 1

    def test_remove_more_than_saved_reports_unavailable(self, app):
        pool = _live_pool(app, cpu=1)

        resp = _scale(app, "remove", "CPU", 3)

        data = resp.get_json()
        assert (data["removed"], data["scheduled_removal"], data["unavailable"]) == (1, 0, 2)
        assert _cpu_workers(pool) == []
        assert _saved_cpu_threads(app) == 0

    def test_settings_page_loads_the_api_change_so_its_next_save_keeps_it(self, app):
        pool = _live_pool(app, cpu=2)
        _scale(app, "add", "CPU", 1)

        shown = app.test_client().get("/api/settings", headers={"X-Auth-Token": TOKEN}).get_json()["cpu_threads"]
        _save(app, {"cpu_threads": shown})

        assert shown == 3
        assert len(_cpu_workers(pool)) == 3

    def test_gpu_add_leaves_saved_cpu_count_alone(self, app):
        # GPU counts are per GPU in Settings; the API's GPU change isn't saved (unchanged behaviour).
        pool = _live_pool(app, cpu=2, gpu=1)

        resp = _scale(app, "add", "GPU", 1)

        assert resp.get_json()["added"] == 1
        assert len(_gpu_workers(pool)) == 2
        assert len(_cpu_workers(pool)) == 2
        assert _saved_cpu_threads(app) == 2


def _loader_thread_errors(app) -> list[str]:
    """What the config loader's thread check says about the saved CPU count (the next job runs it)."""
    from media_preview_generator.config import _validate_thread_config

    errors: list[str] = []
    _validate_thread_config(0, _saved_cpu_threads(app), 2, errors)
    return errors


class TestCpuWorkerCountLimit:
    """The saved CPU count stays within what ``load_config`` accepts (0-32), on every path that saves it."""

    @pytest.mark.parametrize("value", [33, -1], ids=["above-max", "negative"])
    def test_settings_save_rejects_out_of_range_cpu_threads(self, app, value):
        pool = _live_pool(app, cpu=2)

        resp = _save(app, {"cpu_threads": value})

        assert resp.status_code == 400
        assert resp.get_json()["error"] == f"cpu_threads must be between 0 and 32 (got {value})"
        assert _saved_cpu_threads(app) == 2
        assert len(_cpu_workers(pool)) == 2
        assert _loader_thread_errors(app) == []

    def test_settings_save_accepts_the_maximum(self, app):
        pool = _live_pool(app, cpu=2)

        resp = _save(app, {"cpu_threads": 32})

        assert resp.status_code == 200
        assert _saved_cpu_threads(app) == 32
        assert len(_cpu_workers(pool)) == 32
        assert _loader_thread_errors(app) == []

    def test_workers_api_add_past_the_maximum_is_rejected(self, app):
        pool = _live_pool(app, cpu=31)

        resp = _scale(app, "add", "CPU", 2)

        assert resp.status_code == 400
        assert resp.get_json()["error"] == "cpu_threads must be between 0 and 32; adding 2 to 31 would make 33"
        assert _saved_cpu_threads(app) == 31
        assert len(_cpu_workers(pool)) == 31
        assert _loader_thread_errors(app) == []

    def test_workers_api_add_up_to_the_maximum_is_accepted(self, app):
        pool = _live_pool(app, cpu=31)

        resp = _scale(app, "add", "CPU", 1)

        assert resp.status_code == 200
        assert _saved_cpu_threads(app) == 32
        assert len(_cpu_workers(pool)) == 32
        assert _loader_thread_errors(app) == []

    def test_workers_api_remove_from_above_the_maximum_lands_on_the_maximum(self, app):
        # A count saved before the cap existed: a decrease goes straight to the maximum, as the stepper does.
        pool = _live_pool(app, cpu=40)

        resp = _scale(app, "remove", "CPU", 1)

        assert resp.status_code == 200
        data = resp.get_json()
        assert (data["removed"], data["scheduled_removal"], data["unavailable"]) == (8, 0, 0)
        assert _saved_cpu_threads(app) == 32
        assert len(_cpu_workers(pool)) == 32
        assert _loader_thread_errors(app) == []

    def test_system_config_reports_the_maximum_for_the_dashboard_stepper(self, app):
        from media_preview_generator.config import MAX_CPU_THREADS

        without_config = app.test_client().get("/api/system/config", headers={"X-Auth-Token": TOKEN}).get_json()
        loaded = SimpleNamespace(
            plex_url="http://plex:32400",
            plex_token="t",
            plex_config_folder="/plex",
            plex_verify_ssl=True,
            plex_local_videos_path_mapping="",
            plex_videos_path_mapping="",
            plex_bif_frame_interval=5,
            thumbnail_quality=4,
            regenerate_thumbnails=False,
            gpu_config=[],
            gpu_threads=0,
            cpu_threads=2,
            ffmpeg_threads=2,
            log_level="INFO",
        )
        with patch("media_preview_generator.config.get_cached_config", return_value=loaded):
            with_config = app.test_client().get("/api/system/config", headers={"X-Auth-Token": TOKEN}).get_json()

        assert without_config["cpu_threads_max"] == MAX_CPU_THREADS == 32
        assert with_config["cpu_threads_max"] == MAX_CPU_THREADS


class TestWorkersApiConcurrency:
    def test_two_concurrent_adds_both_land(self, app):
        pool = _live_pool(app, cpu=2)
        request_threads: set[int] = set()
        both_read = threading.Barrier(2)
        real_get = SettingsManager.get

        def get_then_wait_for_the_other_request(self, key, default=None):
            # Each request reads the saved count, then waits until the other has read it too. Without a lock both
            # read 2 and both save 3. With one, the second request can't read until the first has saved 3.
            value = real_get(self, key, default)
            if key == "cpu_threads" and threading.get_ident() in request_threads:
                try:
                    both_read.wait(timeout=1.0)
                except threading.BrokenBarrierError:
                    pass
            return value

        responses = []

        def add_one() -> None:
            request_threads.add(threading.get_ident())
            responses.append(_scale(app, "add", "CPU", 1).status_code)

        with patch.object(SettingsManager, "get", get_then_wait_for_the_other_request):
            threads = [threading.Thread(target=add_one) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert responses == [200, 200]
        assert _saved_cpu_threads(app) == 4
        assert len(_cpu_workers(pool)) == 4


class TestConcurrentSettingsSaves:
    def test_pool_ends_at_the_last_saved_cpu_count(self, app):
        pool = _live_pool(app, cpu=2)
        first_read_its_count = threading.Event()
        second_save_done = threading.Event()
        first_save: dict = {}
        real_get = SettingsManager.get

        def get_then_let_the_second_save_run(self, key, default=None):
            # The first save's hook reads its own count (3), then waits while the second save stores and applies 4.
            # Without the lock the first hook then applies its stale 3. With it, the second save can't store 4
            # until the first hook has finished applying 3.
            value = real_get(self, key, default)
            if key == "cpu_threads" and value == 3 and threading.get_ident() == first_save.get("thread"):
                first_read_its_count.set()
                second_save_done.wait(timeout=1.0)
            return value

        def save_three() -> None:
            first_save["thread"] = threading.get_ident()
            first_save["status"] = _save(app, {"cpu_threads": 3}).status_code

        with patch.object(SettingsManager, "get", get_then_let_the_second_save_run):
            first = threading.Thread(target=save_three)
            first.start()
            assert first_read_its_count.wait(timeout=5)
            second_status = _save(app, {"cpu_threads": 4}).status_code
            second_save_done.set()
            first.join(timeout=10)

        assert (first_save["status"], second_status) == (200, 200)
        assert _saved_cpu_threads(app) == 4
        assert len(_cpu_workers(pool)) == 4

    def test_pool_ends_at_the_last_saved_gpu_config(self, app):
        pool = _live_pool(app, cpu=0, gpu=1)
        first_read_its_config = threading.Event()
        second_save_done = threading.Event()
        first_save: dict = {}
        real_get = SettingsManager.get

        def get_then_let_the_second_save_run(self, key, default=None):
            # The first save's GPU hook reads its own config (3 workers), then waits while the second save stores
            # and applies 4. Without the lock the first hook then applies its stale 3.
            value = real_get(self, key, default)
            if (
                key == "gpu_config"
                and _gpu_workers_in(value) == 3
                and threading.get_ident() == first_save.get("thread")
                and not first_read_its_config.is_set()
            ):
                first_read_its_config.set()
                second_save_done.wait(timeout=1.0)
            return value

        def save_three() -> None:
            first_save["thread"] = threading.get_ident()
            first_save["status"] = _save(app, {"gpu_config": _gpu_config(3)}).status_code

        with _fake_detected_gpu(), patch.object(SettingsManager, "get", get_then_let_the_second_save_run):
            first = threading.Thread(target=save_three)
            first.start()
            assert first_read_its_config.wait(timeout=5)
            second_status = _save(app, {"gpu_config": _gpu_config(4)}).status_code
            second_save_done.set()
            first.join(timeout=10)

        assert (first_save["status"], second_status) == (200, 200)
        with app.app_context():
            assert _gpu_workers_in(get_settings_manager().gpu_config) == 4
        assert len(_gpu_workers(pool)) == 4


class TestGpuDetection:
    """GPU detection is slow (ffmpeg probes per GPU), so it must never run under the settings lock and must run once."""

    @pytest.fixture()
    def detections(self, monkeypatch):
        """Stub detection with one fake GPU; record, per run, whether this thread held the settings lock."""
        from media_preview_generator.web.routes import _helpers

        runs: list[bool] = []

        def detect_all_gpus(*args, **kwargs):
            runs.append(get_settings_manager()._lock._is_owned())
            return [("nvidia", GPU_DEVICE, {"name": "Fake GPU"})]

        monkeypatch.setattr("media_preview_generator.gpu.detect.detect_all_gpus", detect_all_gpus)
        _helpers.clear_gpu_cache()
        yield runs
        _helpers.clear_gpu_cache()

    @staticmethod
    def _rescan_lands_when_the_lock_is_taken():
        """Clear the GPU cache right after the settings lock is taken, as a "Re-scan GPUs" landing then would."""
        from media_preview_generator.web.routes import _helpers

        real_locked = SettingsManager.locked

        @contextmanager
        def locked(self):
            with real_locked(self) as sm:
                _helpers.clear_gpu_cache()
                yield sm

        return patch.object(SettingsManager, "locked", locked)

    def test_save_hook_never_detects_gpus_while_holding_the_settings_lock(self, app, detections):
        pool = _live_pool(app, cpu=0, gpu=1)

        with self._rescan_lands_when_the_lock_is_taken():
            resp = _save(app, {"gpu_config": _gpu_config(3)})

        assert resp.status_code == 200
        assert not any(detections), f"detection ran under the settings lock: {detections}"
        # The locked resize used the list detected before the lock, not an empty fallback.
        assert len(_gpu_workers(pool)) == 3

    def test_job_start_never_detects_gpus_while_holding_the_settings_lock(self, app, tmp_path, detections):
        from media_preview_generator.web.routes import _helpers

        _save(app, {"gpu_config": _gpu_config(2)})
        callback = TestJobStartReconcilesCpuWorkers._capture_pool_callback(app, tmp_path, cpu_threads=1)
        pool = WorkerPool(gpu_workers=1, cpu_workers=0, selected_gpus=[("nvidia", GPU_DEVICE, dict(GPU_INFO))])
        # Starting the job cached the empty list its patched detection returned; start this check from a cold cache
        # so the job-start warm-up detects the fake GPU.
        _helpers.clear_gpu_cache()
        detections.clear()

        with self._rescan_lands_when_the_lock_is_taken():
            callback(pool)

        assert not any(detections), f"detection ran under the settings lock: {detections}"
        assert len(_gpu_workers(pool)) == 2

    def test_concurrent_callers_share_one_detection(self, monkeypatch):
        from media_preview_generator.web.routes import _helpers

        calls: list[int] = []
        second_detection_started = threading.Event()

        def detect_all_gpus(*args, **kwargs):
            # The first detection waits until a second one starts too; with single-flight detection none does,
            # and it gives up after 1 s.
            calls.append(threading.get_ident())
            if len(calls) == 1:
                second_detection_started.wait(timeout=1.0)
            else:
                second_detection_started.set()
            return [("nvidia", GPU_DEVICE, {"name": "Fake GPU"})]

        monkeypatch.setattr("media_preview_generator.gpu.detect.detect_all_gpus", detect_all_gpus)
        _helpers.clear_gpu_cache()
        results: list = []
        try:
            threads = [threading.Thread(target=lambda: results.append(_helpers._ensure_gpu_cache())) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        finally:
            _helpers.clear_gpu_cache()

        assert len(calls) == 1
        assert len(results) == 2
        assert results[0] == results[1] == [{"type": "nvidia", "device": GPU_DEVICE, "name": "Fake GPU"}]


class TestGpuListSurvivesARescan:
    """A "Re-scan GPUs" clearing the cache right after a route's warm-up must not blank that route's GPU list.

    Each route warms the cache with ``_ensure_gpu_cache()`` and must use the list it returns: reading the cache
    again afterwards finds it empty while the re-scan runs.
    """

    DETECTED = {"type": "NVIDIA", "device": GPU_DEVICE, "name": "Fake GPU"}

    @pytest.fixture(autouse=True)
    def _detect_one_gpu(self, monkeypatch):
        from media_preview_generator.web.routes import _helpers

        monkeypatch.setattr(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            lambda *args, **kwargs: [("NVIDIA", GPU_DEVICE, {"name": "Fake GPU"})],
        )
        _helpers.clear_gpu_cache()
        yield
        _helpers.clear_gpu_cache()

    @staticmethod
    def _rescan_lands_after_the_warm_up(route_module: str):
        """Clear the GPU cache as soon as the route's warm-up returns, as a re-scan landing then would."""
        from media_preview_generator.web.routes import _helpers

        def warm_up_then_rescan_clears() -> list[dict]:
            gpus = _helpers._ensure_gpu_cache()
            _helpers.clear_gpu_cache()
            return gpus

        return patch(f"media_preview_generator.web.routes.{route_module}._ensure_gpu_cache", warm_up_then_rescan_clears)

    def test_rescan_response_lists_the_gpus_it_detected(self, app):
        with self._rescan_lands_after_the_warm_up("api_system"):
            resp = app.test_client().post("/api/system/rescan-gpus", headers={"X-Auth-Token": TOKEN})

        assert resp.status_code == 200
        assert resp.get_json()["gpus"] == [self.DETECTED]

    def test_system_status_lists_the_detected_gpus(self, app):
        with self._rescan_lands_after_the_warm_up("api_system"):
            resp = app.test_client().get("/api/system/status", headers={"X-Auth-Token": TOKEN})

        assert resp.status_code == 200
        assert resp.get_json()["gpus"] == [self.DETECTED]

    def test_idle_worker_list_keeps_the_saved_gpu_rows(self, app):
        # No pool exists yet, so the Workers panel is built from the saved counts and the detected GPUs.
        with app.app_context():
            get_settings_manager().update({"cpu_threads": 1, "gpu_config": _gpu_config(2)})

        with self._rescan_lands_after_the_warm_up("api_jobs"):
            resp = app.test_client().get("/api/jobs/workers", headers={"X-Auth-Token": TOKEN})

        assert resp.status_code == 200
        assert [w["worker_type"] for w in resp.get_json()["workers"]] == ["GPU", "GPU", "CPU"]

    def test_vulkan_warning_names_the_detected_gpu(self, app):
        from media_preview_generator.gpu import _reset_vulkan_device_cache

        no_graphics_capability = {
            "nvidia_capabilities": "compute,video,utility",
            "nvidia_capabilities_has_graphics": False,
            "nvidia_icd_json_path": None,
            "libnvidia_glvkspirv_found": False,
            "libegl_nvidia_found": False,
        }
        _reset_vulkan_device_cache()
        try:
            with (
                self._rescan_lands_after_the_warm_up("api_vulkan"),
                patch(
                    "media_preview_generator.gpu.vulkan_probe._probe_vulkan_device",
                    return_value="llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)",
                ),
                patch("media_preview_generator.web.routes.api_vulkan.glob.glob", return_value=[]),
                patch(
                    "media_preview_generator.web.routes.api_vulkan._diagnose_vulkan_environment",
                    return_value=no_graphics_capability,
                ),
            ):
                resp = app.test_client().get("/api/system/vulkan")
        finally:
            _reset_vulkan_device_cache()

        assert resp.status_code == 200
        assert "<strong>Your GPU:</strong> Fake GPU" in resp.get_json()["warning"]
