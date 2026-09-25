"""Per-GPU ``ffmpeg_threads`` reaches each GPU worker's preview FFmpeg.

``gpu_config`` carries an ``ffmpeg_threads`` value per GPU. ``load_config``
folds them into one ``Config.ffmpeg_threads`` (the max across enabled GPUs),
so each worker has to forward its own value as ``ffmpeg_threads_override``
or every GPU runs with the largest GPU's thread cap.
"""

from unittest.mock import MagicMock, patch

from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.processing import CodecNotSupportedError
from tests.conftest import _ms, _pi


def _run(worker: Worker) -> None:
    worker.assign_task(_pi("k1", title="T", media_type="movie"), MagicMock(regenerate_thumbnails=False), MagicMock())
    worker.current_thread.join(timeout=2)


class TestWorkerForwardsFfmpegThreads:
    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_gpu_worker_forwards_its_own_threads_when_set(self, mock_process):
        mock_process.return_value = _ms("generated")
        worker = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "RTX", ffmpeg_threads=3)

        _run(worker)

        kwargs = mock_process.call_args.kwargs
        assert kwargs["gpu"] == "NVIDIA"
        assert kwargs["ffmpeg_threads_override"] == 3

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_each_gpu_in_pool_forwards_its_own_threads_when_values_differ(self, mock_process):
        mock_process.return_value = _ms("generated")
        pool = WorkerPool(
            gpu_workers=2,
            cpu_workers=0,
            selected_gpus=[
                ("NVIDIA", "cuda:0", {"name": "Big", "workers": 1, "ffmpeg_threads": 8}),
                ("INTEL", "/dev/dri/renderD128", {"name": "Small", "workers": 1, "ffmpeg_threads": 1}),
            ],
        )

        seen = {}
        for worker in pool.workers:
            _run(worker)
            kwargs = mock_process.call_args.kwargs
            seen[kwargs["gpu_device_path"]] = kwargs["ffmpeg_threads_override"]

        assert seen == {"cuda:0": 8, "/dev/dri/renderD128": 1}

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_cpu_worker_forwards_no_override(self, mock_process):
        mock_process.return_value = _ms("generated")
        worker = Worker(0, "CPU")

        _run(worker)

        kwargs = mock_process.call_args.kwargs
        assert kwargs["gpu"] is None
        assert kwargs["ffmpeg_threads_override"] is None

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_gpu_worker_cpu_fallback_forwards_no_override(self, mock_process):
        """The in-place CPU retry runs like a CPU worker: no GPU thread cap."""
        mock_process.side_effect = [CodecNotSupportedError("hevc 10-bit"), _ms("generated")]
        worker = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "RTX", ffmpeg_threads=3)

        _run(worker)

        first, second = mock_process.call_args_list
        assert (first.kwargs["gpu"], first.kwargs["ffmpeg_threads_override"]) == ("NVIDIA", 3)
        assert (second.kwargs["gpu"], second.kwargs["ffmpeg_threads_override"]) == (None, None)


class TestReconcileAppliesFfmpegThreads:
    """A saved per-GPU thread change applies live, like the worker count does."""

    def test_reconcile_updates_threads_on_existing_workers_when_changed(self):
        pool = WorkerPool(
            gpu_workers=2,
            cpu_workers=0,
            selected_gpus=[("NVIDIA", "cuda:0", {"name": "GPU0", "workers": 2, "ffmpeg_threads": 2})],
        )
        busy = pool.workers[0]
        busy.is_busy = True

        pool.reconcile_gpu_workers([("NVIDIA", "cuda:0", {"name": "GPU0", "workers": 2, "ffmpeg_threads": 6})])

        assert [w.ffmpeg_threads for w in pool.workers] == [6, 6]

    def test_reconcile_gives_added_workers_the_new_threads_when_count_rises(self):
        pool = WorkerPool(
            gpu_workers=1,
            cpu_workers=0,
            selected_gpus=[("NVIDIA", "cuda:0", {"name": "GPU0", "workers": 1, "ffmpeg_threads": 2})],
        )

        pool.reconcile_gpu_workers([("NVIDIA", "cuda:0", {"name": "GPU0", "workers": 3, "ffmpeg_threads": 5})])

        gpu_workers = [w for w in pool.workers if w.worker_type == "GPU"]
        assert len(gpu_workers) == 3
        assert [w.ffmpeg_threads for w in gpu_workers] == [5, 5, 5]

    def test_reconcile_leaves_other_gpus_threads_alone(self):
        pool = WorkerPool(
            gpu_workers=2,
            cpu_workers=0,
            selected_gpus=[
                ("NVIDIA", "cuda:0", {"name": "A", "workers": 1, "ffmpeg_threads": 2}),
                ("INTEL", "/dev/dri/renderD128", {"name": "B", "workers": 1, "ffmpeg_threads": 4}),
            ],
        )

        pool.reconcile_gpu_workers(
            [
                ("NVIDIA", "cuda:0", {"name": "A", "workers": 1, "ffmpeg_threads": 7}),
                ("INTEL", "/dev/dri/renderD128", {"name": "B", "workers": 1, "ffmpeg_threads": 4}),
            ]
        )

        by_device = {w.gpu_device: w.ffmpeg_threads for w in pool.workers}
        assert by_device == {"cuda:0": 7, "/dev/dri/renderD128": 4}
