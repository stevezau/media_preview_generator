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

from unittest.mock import patch

import pytest

from media_preview_generator.jobs.dispatcher import get_dispatcher, reset_dispatcher
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

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
        with app.app_context():
            assert get_settings_manager().cpu_threads == 4
        assert get_dispatcher() is None

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
