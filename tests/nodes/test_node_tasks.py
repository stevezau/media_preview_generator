"""The node's task table: statuses, cancel/pause, log capture and paging, expiry, artifacts (spec §3 Task lifetime)."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import threading
import time

import pytest
from loguru import logger

from media_preview_generator.nodes import protocol
from media_preview_generator.nodes.protocol import OpError
from media_preview_generator.nodes.server import tasks

WAIT_S = 10.0


def _until(check, timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x" * 10)
    return root


@pytest.fixture
def make_table(tmp_path):
    made: list[tasks.TaskTable] = []

    def make(ops, **kwargs) -> tasks.TaskTable:
        table = tasks.TaskTable(ops, temp_root=str(tmp_path / "node-tmp"), reap_interval_s=0.02, **kwargs)
        made.append(table)
        return table

    yield make
    for table in made:
        table.close()


def _spec(media, op: str = "test.op", **extra) -> dict:
    return {"op": op, "path": "/data/a.mkv", "size": 10, "mtime_ns": None,
            "mapping": [{"app_prefix": "/data", "node_prefix": str(media)}], "gpu": None, "params": {}, **extra}  # fmt: skip


def _finished(table: tasks.TaskTable, task_id: str) -> dict:
    _until(lambda: table.poll(task_id)["status"] != protocol.STATUS_RUNNING)
    return table.poll(task_id)


class TestStatuses:
    def test_done_carries_the_result_and_the_node_path(self, make_table, media):
        seen = {}

        def op(ctx, node_path, params):
            seen.update(path=node_path, params=params, gpu=ctx.gpu, temp=ctx.temp_dir)
            ctx.progress(50, frames=3)
            return {"answer": 42}

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media, gpu={"type": "NVIDIA", "device": "cuda:0"}, params={"k": 1}))

        state = _finished(table, task_id)

        assert state["status"] == protocol.STATUS_DONE
        assert state["result"] == {"answer": 42} and state["error"] is None
        assert state["progress"] == {"percent": 50.0, "frames": 3}
        assert seen["path"] == os.path.realpath(media / "a.mkv")
        assert seen["params"] == {"k": 1} and seen["gpu"] == ("NVIDIA", "cuda:0")
        assert os.path.isdir(seen["temp"]) and seen["temp"].startswith(table.temp_root)

    def test_progress_keeps_lists_and_dicts_as_json_not_their_text(self, make_table, media):
        def op(ctx, node_path, params):
            ctx.progress(40, args=[4.0, (10, "1.5x"), float("nan")], fallbacks=["CPU"], extra={"k": float("inf")})
            return {}

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["progress"] == {
            "percent": 40.0,
            "args": [4.0, [10, "1.5x"], None],
            "fallbacks": ["CPU"],
            "extra": {"k": None},
        }

    def test_file_not_visible_runs_no_op(self, make_table, media):
        called = []
        table = make_table({"test.op": lambda *a: called.append(1) or {}})

        state = _finished(table, table.start(_spec(media, size=11)))

        assert state["status"] == protocol.STATUS_FILE_NOT_VISIBLE
        assert state["error"]["kind"] == protocol.KIND_FILE_NOT_VISIBLE
        assert state["error"]["reason"] == protocol.NOT_VISIBLE_DIFFERENT_FILE
        assert not called

    def test_any_exception_fails_with_type_and_message(self, make_table, media):
        def op(ctx, node_path, params):
            raise RuntimeError("boom")

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["status"] == protocol.STATUS_FAILED
        assert state["error"] == {"kind": protocol.KIND_FAILED, "message": "RuntimeError: boom"}
        assert not any("Traceback" in line["message"] for line in state["log"])

    def test_op_error_keeps_its_kind_and_extra(self, make_table, media):
        def op(ctx, node_path, params):
            raise OpError(protocol.KIND_CODEC_NOT_SUPPORTED, "no hevc", codec="hevc")

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["status"] == protocol.STATUS_FAILED
        assert state["error"] == {"kind": protocol.KIND_CODEC_NOT_SUPPORTED, "message": "no hevc", "codec": "hevc"}

    def test_op_error_cancelled_is_status_cancelled(self, make_table, media):
        def op(ctx, node_path, params):
            raise OpError(protocol.KIND_CANCELLED, "stopped")

        table = make_table({"test.op": op})

        assert _finished(table, table.start(_spec(media)))["status"] == protocol.STATUS_CANCELLED

    def test_the_apps_cancellation_error_is_status_cancelled(self, make_table, media):
        from media_preview_generator.processing.generator import CancellationError

        def op(ctx, node_path, params):
            raise CancellationError("cancelled by user")

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["status"] == protocol.STATUS_CANCELLED
        assert state["error"]["kind"] == protocol.KIND_CANCELLED

    def test_non_dict_result_fails(self, make_table, media):
        table = make_table({"test.op": lambda *a: ["not", "a", "dict"]})

        assert _finished(table, table.start(_spec(media)))["status"] == protocol.STATUS_FAILED

    def test_result_that_isnt_json_fails_instead_of_breaking_the_poll(self, make_table, media):
        table = make_table({"test.op": lambda *a: {"x": object()}})

        state = _finished(table, table.start(_spec(media)))

        assert state["status"] == protocol.STATUS_FAILED and "TypeError" in state["error"]["message"]

    def test_gpu_failed_asks_for_a_gpu_recheck(self, make_table, media):
        rechecks = []

        def op(ctx, node_path, params):
            raise OpError(protocol.KIND_GPU_FAILED, "device lost")

        table = make_table({"test.op": op}, on_gpu_failed=lambda: rechecks.append(1))

        _finished(table, table.start(_spec(media)))

        _until(lambda: rechecks == [1])

    def test_other_failures_dont_recheck_gpus(self, make_table, media):
        rechecks = []
        table = make_table({"test.op": lambda *a: {}}, on_gpu_failed=lambda: rechecks.append(1))

        _finished(table, table.start(_spec(media)))

        assert rechecks == []


class TestSpecValidation:
    @pytest.mark.parametrize(
        "change",
        [
            {"op": "nope"},
            {"op": 3},
            {"path": ""},
            {"size": "10"},
            {"size": True},
            {"mtime_ns": -1},
            {"mapping": "x"},
            {"mapping": [{"app_prefix": "/data"}]},
            {"gpu": {"type": "NVIDIA"}},
            {"params": []},
            {"client_task_id": 5},
            {"client_task_id": "short"},
            {"client_task_id": "../../etc/passwd-0123456789"},
            {"client_task_id": "x" * 65},
        ],
    )
    def test_malformed_spec_is_refused(self, make_table, media, change):
        table = make_table({"test.op": lambda *a: {}})

        with pytest.raises(tasks.TaskSpecError):
            table.start({**_spec(media), **change})

    def test_non_object_spec_is_refused(self, make_table):
        with pytest.raises(tasks.TaskSpecError):
            make_table({}).start([])


class TestTempFolder:
    def test_a_temp_root_removed_after_start_is_made_again(self, make_table, media):
        table = make_table({"test.op": lambda *a: {}})
        shutil.rmtree(table.temp_root)  # a host's tmp cleaner, or a share remounted under NODE_TEMP_DIR

        task_id = table.start(_spec(media))

        assert _finished(table, task_id)["status"] == protocol.STATUS_DONE

    def test_a_full_temp_disk_refuses_the_start_with_the_cause(self, make_table, media, monkeypatch):
        table = make_table({"test.op": lambda *a: {}})

        def full(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(tasks.tempfile, "mkdtemp", full)

        with pytest.raises(tasks.TaskStartError, match="temp folder .* is full \\(No space left on device\\)"):
            table.start(_spec(media))
        assert table.busy() == 0

    @pytest.mark.parametrize(("code", "said"), [(errno.ENOSPC, "is full"), (errno.EROFS, "can't write to")])
    def test_a_temp_folder_that_fills_or_turns_read_only_mid_task_says_so(self, make_table, media, code, said):
        def op(ctx, node_path, params):
            tar = os.path.join(ctx.temp_dir, "frames.tar")
            raise OSError(code, os.strerror(code), tar)

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["status"] == protocol.STATUS_FAILED and state["error"]["kind"] == protocol.KIND_TEMP_FOLDER
        message = state["error"]["message"]
        assert message.startswith("The node") and said in message and table.temp_root in message, message
        assert ("twice its frames' size" in message) == (code == errno.ENOSPC)

    def test_a_temp_folder_error_raised_through_another_exception_still_says_so(self, make_table, media):
        def op(ctx, node_path, params):
            try:
                raise OSError(errno.ENOSPC, "No space left on device")
            except OSError as exc:
                raise RuntimeError("writing the frames failed") from exc

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert "temp folder" in state["error"]["message"] and "is full" in state["error"]["message"]

    @pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM], ids=["no-permission", "not-permitted"])
    def test_ffmpeg_refused_the_temp_folder_by_permission_holds_the_node_with_the_puid_advice(
        self, make_table, media, monkeypatch, code
    ):
        from media_preview_generator.nodes.registry import _temp_folder_advice
        from media_preview_generator.processing.generator import _FOLDER_WRITE_TEXT, WorkingFolderError

        def refused(*_args, **_kwargs):
            raise PermissionError(errno.EACCES, "Permission denied")

        def op(ctx, node_path, params):
            # The folder refuses the node's user from now on: a new folder there fails too.
            monkeypatch.setattr(tasks.tempfile, "mkdtemp", refused)
            raise WorkingFolderError(code, _FOLDER_WRITE_TEXT[code][0], os.path.join(ctx.temp_dir, "frames"))

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["status"] == protocol.STATUS_FAILED and state["error"]["kind"] == protocol.KIND_TEMP_FOLDER
        message = state["error"]["message"]
        assert message.startswith(f"The node can't write to its temp folder {table.temp_root} ("), message
        assert "twice its frames' size" not in message
        assert "check PUID and PGID" in _temp_folder_advice(message), message

    @pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM], ids=["no-permission", "not-permitted"])
    def test_a_permission_error_in_a_temp_folder_the_node_can_write_is_the_files(self, make_table, media, code):
        """The node's user made the task's folder: a refusal there while the folder takes new files is a security
        policy on one file, or FFmpeg's catch-all error (-1) read as EPERM, not the node's PUID/PGID."""
        from media_preview_generator.processing.generator import _FOLDER_WRITE_TEXT, WorkingFolderError

        def op(ctx, node_path, params):
            raise WorkingFolderError(code, _FOLDER_WRITE_TEXT[code][0], os.path.join(ctx.temp_dir, "frames"))

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["status"] == protocol.STATUS_FAILED and state["error"]["kind"] == protocol.KIND_FAILED
        assert state["error"]["message"].startswith(f"WorkingFolderError: [Errno {code}]"), state["error"]

    @pytest.mark.parametrize(
        "named", [lambda media: str(media / "a.mkv"), lambda media: None], ids=["the-video", "no-file-named"]
    )
    def test_a_permission_error_not_on_a_file_in_the_temp_folder_is_not_blamed_on_it(self, make_table, media, named):
        # A video the node's user may not read fails with the same errno.
        def op(ctx, node_path, params):
            raise PermissionError(errno.EACCES, "Permission denied", named(media))

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["error"]["kind"] == protocol.KIND_FAILED
        assert state["error"]["message"].startswith("PermissionError: [Errno 13]"), state["error"]

    def test_a_read_only_error_outside_the_temp_folder_is_not_blamed_on_it(self, make_table, media):
        def op(ctx, node_path, params):
            raise OSError(errno.EROFS, "Read-only file system", str(media / "a.mkv"))

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["error"]["message"].startswith("OSError: [Errno 30]")

    def test_the_probe_names_a_temp_folder_the_node_cant_write(self, tmp_path):
        blocker = tmp_path / "a-file"
        blocker.write_text("x")

        problem = tasks.probe_temp_folder(str(blocker / "node-tmp"))

        assert problem is not None and problem.startswith("The node can't write to its temp folder")
        assert tasks.probe_temp_folder(str(tmp_path / "fine")) is None
        assert os.listdir(tmp_path / "fine") == []

    def test_a_node_without_the_worker_role_makes_no_temp_folder(self, tmp_path):
        table = tasks.TaskTable({}, temp_root=None)
        try:
            with pytest.raises(tasks.TaskSpecError):
                table.start({"op": "preview.frames", "path": "/a", "mapping": []})
        finally:
            table.close()
        assert os.listdir(tmp_path) == []


class TestATempFolderThatFilledUp:
    """After a task fills the temp folder its folder is removed and a fresh check finds room, so the node keeps the
    problem itself: until the folder has more room than that task started with, or a task started later finishes. A
    fill beside other tasks' files lasts only until none runs and the room is back."""

    MB = 1 << 20

    @pytest.fixture
    def free(self, monkeypatch):
        space = {"bytes": 4 * self.MB}
        monkeypatch.setattr(tasks, "_free_bytes", lambda path: space["bytes"])
        return space

    @staticmethod
    def _table(make_table, gate: threading.Event | None = None):
        def fills(ctx, node_path, params):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), os.path.join(ctx.temp_dir, "frames.tar"))

        def fits(ctx, node_path, params):
            if gate is not None:
                gate.wait(WAIT_S)
            return {}

        def read_only(ctx, node_path, params):
            raise OSError(errno.EROFS, os.strerror(errno.EROFS), os.path.join(ctx.temp_dir, "frames.tar"))

        return make_table({"test.fills": fills, "test.fits": fits, "test.read_only": read_only})

    def test_the_folder_reads_full_after_the_task_with_the_room_it_had(self, make_table, media, free):
        table = self._table(make_table)
        assert table.filled_problem() is None

        _finished(table, table.start(_spec(media, "test.fills")))

        assert table.filled_problem() == f"The node's temp folder {table.temp_root} filled up while working on a file " \
            "(4 MB free)"  # fmt: skip

    def test_it_reads_full_until_the_folder_has_more_room_than_the_task_had(self, make_table, media, free):
        table = self._table(make_table)
        _finished(table, table.start(_spec(media, "test.fills")))

        free["bytes"] += self.MB  # the same room give or take a little: other files on a shared disk come and go
        assert table.filled_problem() is not None

        free["bytes"] = 64 * self.MB
        assert table.filled_problem() is None
        free["bytes"] = 4 * self.MB
        assert table.filled_problem() is None, "once cleared it stays cleared until a task fills the folder again"

    def test_a_task_started_after_it_filled_up_that_finishes_clears_it(self, make_table, media, free):
        table = self._table(make_table)
        _finished(table, table.start(_spec(media, "test.fills")))

        assert _finished(table, table.start(_spec(media, "test.fits")))["status"] == protocol.STATUS_DONE

        assert table.filled_problem() is None

    def test_a_task_already_running_when_it_filled_up_doesnt_clear_it_by_finishing(self, make_table, media, free):
        gate = threading.Event()
        table = self._table(make_table, gate)
        running = table.start(_spec(media, "test.fits"))
        _finished(table, table.start(_spec(media, "test.fills")))
        free["bytes"] = 2 * self.MB  # less room than the filling task started with

        gate.set()

        assert _finished(table, running)["status"] == protocol.STATUS_DONE
        assert table.filled_problem() is not None

    def test_a_fill_beside_other_tasks_lasts_until_none_runs_and_the_room_is_back(self, make_table, media, free):
        # Four workers' files that fit one at a time filled the folder together: the one that failed first started
        # with the folder's whole room, so waiting for more room than that held the node until it restarted.
        gate = threading.Event()
        table = self._table(make_table, gate)
        running = table.start(_spec(media, "test.fits"))
        _finished(table, table.start(_spec(media, "test.fills")))
        assert table.filled_problem() is not None, "another task still runs there"

        gate.set()
        _finished(table, running)

        assert table.filled_problem() is None

    def test_a_finished_task_waiting_for_its_delete_doesnt_make_the_fill_a_shared_one(self, make_table, media, free):
        # Its files were there when the filling task started, so that task filled the room it had by itself and the
        # next file that size fills it again. Counted as running beside it, the folder read fine again at once.
        table = self._table(make_table)
        assert _finished(table, table.start(_spec(media, "test.fits")))["status"] == protocol.STATUS_DONE

        _finished(table, table.start(_spec(media, "test.fills")))

        assert table.filled_problem() is not None

    def test_a_read_only_folder_isnt_one_that_filled_up(self, make_table, media, free):
        # The node's fresh check finds a read-only folder by itself.
        table = self._table(make_table)

        _finished(table, table.start(_spec(media, "test.read_only")))

        assert table.filled_problem() is None

    def test_without_its_free_space_it_reads_full_until_no_task_runs(self, make_table, media, monkeypatch):
        # It used to wait for a later task to finish, and the app sends none while the folder reads full.
        monkeypatch.setattr(tasks, "_free_bytes", lambda path: None)
        gate = threading.Event()
        table = self._table(make_table, gate)
        running = table.start(_spec(media, "test.fits"))
        _finished(table, table.start(_spec(media, "test.fills")))

        assert table.filled_problem() == f"The node's temp folder {table.temp_root} filled up while working on a file"
        gate.set()
        _finished(table, running)
        assert table.filled_problem() is None


class TestClientTaskId:
    CLIENT_ID = "0123456789abcdef0123456789abcdef"

    def test_the_clients_id_is_the_task_id(self, make_table, media):
        table = make_table({"test.op": lambda *a: {"ok": 1}})
        assert table.start(_spec(media, client_task_id=self.CLIENT_ID)) == self.CLIENT_ID
        assert _finished(table, self.CLIENT_ID)["result"] == {"ok": 1}

    def test_a_repeated_start_returns_the_same_task_and_runs_the_op_once(self, make_table, media):
        runs = []
        release = threading.Event()

        def op(ctx, node_path, params):
            runs.append(ctx.temp_dir)
            release.wait(WAIT_S)
            return {}

        table = make_table({"test.op": op})
        first = table.start(_spec(media, client_task_id=self.CLIENT_ID))
        _until(lambda: runs)
        again = table.start(_spec(media, client_task_id=self.CLIENT_ID))
        release.set()
        _finished(table, first)

        assert first == again == self.CLIENT_ID
        assert len(runs) == 1
        temp_root = os.path.dirname(runs[0])
        assert [n for n in os.listdir(temp_root) if n.startswith(tasks.TASK_DIR_PREFIX)] == [os.path.basename(runs[0])]

    def test_without_a_client_id_each_start_is_a_new_task(self, make_table, media):
        table = make_table({"test.op": lambda *a: {}})
        assert table.start(_spec(media)) != table.start(_spec(media))

    def test_deleting_by_the_client_id_cancels_a_task_whose_start_answer_was_lost(self, make_table, media):
        seen = {}

        def op(ctx, node_path, params):
            seen["temp"] = ctx.temp_dir
            _until(ctx.cancel_check)
            return {}

        table = make_table({"test.op": op})
        table.start(_spec(media, client_task_id=self.CLIENT_ID))
        _until(lambda: "temp" in seen)

        assert table.delete(self.CLIENT_ID)

        _until(lambda: not os.path.exists(seen["temp"]))
        assert self.CLIENT_ID not in table

    def test_a_start_that_arrives_after_its_delete_never_runs(self, make_table, media):
        runs = []
        table = make_table({"test.op": lambda *a: runs.append(1) or {}})

        assert table.delete(self.CLIENT_ID) is False  # the DELETE overtook its start
        with pytest.raises(tasks.TaskDeletedError):
            table.start(_spec(media, client_task_id=self.CLIENT_ID))

        assert self.CLIENT_ID not in table and runs == []
        assert [n for n in os.listdir(table.temp_root) if n.startswith(tasks.TASK_DIR_PREFIX)] == []

    def test_a_deleted_id_is_forgotten_after_its_time(self, make_table, media, monkeypatch):
        table = make_table({"test.op": lambda *a: {}})
        now = [1000.0]
        monkeypatch.setattr(tasks.time, "monotonic", lambda: now[0])
        table.delete(self.CLIENT_ID)
        now[0] += tasks.DELETED_ID_KEEP_S + 1

        assert table.start(_spec(media, client_task_id=self.CLIENT_ID)) == self.CLIENT_ID

    def test_only_so_many_deleted_ids_are_kept(self, make_table, media):
        table = make_table({"test.op": lambda *a: {}})
        ids = [f"{n:032x}" for n in range(tasks.DELETED_IDS_MAX + 1)]
        for task_id in ids:
            table.delete(task_id)

        assert table.start(_spec(media, client_task_id=ids[0])) == ids[0]  # the oldest made room
        with pytest.raises(tasks.TaskDeletedError):
            table.start(_spec(media, client_task_id=ids[-1]))


class TestCancelAndPause:
    def test_cancel_stops_the_op_and_is_cancelled(self, make_table, media):
        started = threading.Event()

        def op(ctx, node_path, params):
            started.set()
            _until(ctx.cancel_check)
            return {"partial": True}

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media))
        started.wait(WAIT_S)

        assert table.cancel(task_id)
        state = _finished(table, task_id)

        assert state["status"] == protocol.STATUS_CANCELLED
        assert state["result"] is None

    def test_exception_after_cancel_is_cancelled_not_failed(self, make_table, media):
        started = threading.Event()

        def op(ctx, node_path, params):
            started.set()
            _until(ctx.cancel_check)
            raise RuntimeError("ffmpeg killed")

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media))
        started.wait(WAIT_S)
        table.cancel(task_id)

        assert _finished(table, task_id)["status"] == protocol.STATUS_CANCELLED

    def test_pause_and_resume_reach_pause_check(self, make_table, media):
        seen: list[bool] = []

        def op(ctx, node_path, params):
            _until(ctx.pause_check)
            seen.append(True)
            _until(lambda: not ctx.pause_check())
            seen.append(False)
            return {}

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media))

        assert table.set_paused(task_id, True)
        _until(lambda: seen == [True])
        assert table.set_paused(task_id, False)

        assert _finished(table, task_id)["status"] == protocol.STATUS_DONE
        assert seen == [True, False]

    def test_a_cancelled_task_never_reads_as_paused(self, make_table, media):
        started, release = threading.Event(), threading.Event()
        seen = {}

        def op(ctx, node_path, params):
            started.set()
            release.wait(WAIT_S)
            seen["paused"] = ctx.pause_check()
            return {}

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media))
        started.wait(WAIT_S)
        table.set_paused(task_id, True)
        table.cancel(task_id)
        release.set()
        _finished(table, task_id)

        assert seen == {"paused": False}

    def test_unknown_task_is_false(self, make_table):
        table = make_table({})

        assert not table.cancel("nope") and not table.set_paused("nope", True) and not table.delete("nope")
        assert table.poll("nope") is None


class TestLog:
    def test_loguru_lines_from_the_op_thread_are_captured(self, make_table, media):
        def op(ctx, node_path, params):
            logger.info("decoding {}", "frames")
            logger.debug("too chatty to keep")
            ctx.log("warning", "direct line")
            return {}

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        messages = [(line["level"], line["message"]) for line in state["log"]]
        assert ("INFO", "decoding frames") in messages
        assert ("WARNING", "direct line") in messages
        assert messages.count(("WARNING", "direct line")) == 1
        assert not any("too chatty" in m for _, m in messages)

    def test_a_per_file_line_is_tagged_for_the_apps_relay(self, make_table, media):
        from media_preview_generator.logging_config import per_file_logger

        def op(ctx, node_path, params):
            per_file_logger.info("how the run ended")
            logger.info("plain line")
            return {}

        table = make_table({"test.op": op})

        lines = {line["message"]: line for line in _finished(table, table.start(_spec(media)))["log"]}

        assert lines["how the run ended"]["per_file"] is True
        assert "per_file" not in lines["plain line"]

    def test_other_threads_only_when_they_register(self, make_table, media):
        def op(ctx, node_path, params):
            def helper(capture: bool) -> None:
                if capture:
                    with ctx.capture_thread():
                        logger.info("from a registered thread")
                else:
                    logger.info("from a stray thread")

            for capture in (True, False):
                thread = threading.Thread(target=helper, args=(capture,))
                thread.start()
                thread.join()
            return {}

        table = make_table({"test.op": op})

        messages = [line["message"] for line in _finished(table, table.start(_spec(media)))["log"]]

        assert "from a registered thread" in messages
        assert "from a stray thread" not in messages

    def test_unknown_level_is_logged_as_info(self, make_table, media):
        def op(ctx, node_path, params):
            ctx.log("loud", "hello")
            return {}

        table = make_table({"test.op": op})

        log = _finished(table, table.start(_spec(media)))["log"]

        assert {"seq": 0, "level": "INFO", "message": "hello"} in log

    def test_log_from_pages_by_seq(self, make_table, media):
        def op(ctx, node_path, params):
            for n in range(5):
                ctx.log("info", f"line {n}")
            return {}

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media))
        _finished(table, task_id)

        page = table.poll(task_id, log_from=3)

        assert [line["seq"] for line in page["log"]] == [3, 4]
        assert page["next_log"] == 5
        assert table.poll(task_id, log_from=5)["log"] == []

    def test_log_keeps_the_last_2000_lines_and_seq_keeps_counting(self, make_table, media):
        def op(ctx, node_path, params):
            for n in range(tasks.TASK_LOG_LINES + 10):
                ctx.log("info", f"line {n}")
            return {}

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media))
        state = _finished(table, task_id)

        assert len(state["log"]) == tasks.TASK_LOG_LINES
        assert state["log"][0]["seq"] == 10
        assert state["next_log"] == tasks.TASK_LOG_LINES + 10


class TestExpiry:
    def test_unpolled_running_task_is_cancelled_and_its_temp_dir_removed(self, make_table, media):
        seen = {}

        def op(ctx, node_path, params):
            seen["temp"] = ctx.temp_dir
            _until(ctx.cancel_check)
            seen["cancelled"] = True
            return {}

        table = make_table({"test.op": op}, unpolled_expiry_s=0.2)
        task_id = table.start(_spec(media))

        _until(lambda: task_id not in table)

        _until(lambda: seen.get("cancelled") and not os.path.exists(seen["temp"]))

    def test_polling_keeps_a_running_task_alive(self, make_table, media):
        release = threading.Event()

        def op(ctx, node_path, params):
            release.wait(WAIT_S)
            return {}

        table = make_table({"test.op": op}, unpolled_expiry_s=0.3)
        task_id = table.start(_spec(media))
        for _ in range(10):
            assert table.poll(task_id) is not None
            time.sleep(0.1)
        release.set()

        assert _finished(table, task_id)["status"] == protocol.STATUS_DONE

    def test_finished_task_kept_until_its_keep_time_after_the_last_poll(self, make_table, media):
        table = make_table({"test.op": lambda ctx, p, q: {"temp": ctx.temp_dir}}, finished_keep_s=0.3)
        task_id = table.start(_spec(media))
        temp = _finished(table, task_id)["result"]["temp"]

        # Expiry drops the task from the table first and removes its temp dir just after, so wait for both.
        _until(lambda: task_id not in table and not os.path.exists(temp))

    def test_delete_removes_at_once(self, make_table, media):
        table = make_table({"test.op": lambda ctx, p, q: {"temp": ctx.temp_dir}})
        task_id = table.start(_spec(media))
        temp = _finished(table, task_id)["result"]["temp"]

        assert table.delete(task_id)

        assert task_id not in table and not os.path.exists(temp)

    def test_delete_while_running_cancels_and_cleans_up_when_the_op_returns(self, make_table, media):
        seen = {}

        def op(ctx, node_path, params):
            seen["temp"] = ctx.temp_dir
            _until(ctx.cancel_check)
            return {}

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media))
        _until(lambda: "temp" in seen)

        table.delete(task_id)

        _until(lambda: not os.path.exists(seen["temp"]))

    def test_closing_cancels_running_ops_and_waits_for_them(self, make_table, media):
        """A node that stops (SIGTERM) waits for its cancelled ops, so their FFmpeg is stopped, not orphaned."""
        seen, stopped = {}, threading.Event()

        def op(ctx, node_path, params):
            seen["temp"] = ctx.temp_dir
            _until(ctx.cancel_check)
            time.sleep(0.3)  # stopping FFmpeg takes a moment
            stopped.set()
            return {}

        table = make_table({"test.op": op})
        table.start(_spec(media))
        _until(lambda: "temp" in seen)

        table.close(wait_s=5)

        assert stopped.is_set() and not os.path.exists(seen["temp"])

    def test_closing_waits_no_longer_than_asked_for_an_op_that_never_stops(self, make_table, media, monkeypatch):
        release = threading.Event()
        warned: list[str] = []
        monkeypatch.setattr(tasks.logger, "warning", lambda message, *args: warned.append(message.format(*args)))

        def op(ctx, node_path, params):
            release.wait(10)
            return {}

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media))
        start = time.monotonic()
        try:
            table.close(wait_s=0.3)
            waited = time.monotonic() - start
        finally:
            release.set()

        assert 0.25 <= waited < 2
        assert [line for line in warned if task_id[:8] in line and "still running" in line]

    def test_leftover_task_dirs_from_a_dead_process_are_removed_at_start(self, tmp_path):
        root = tmp_path / "node-tmp"
        (root / f"{tasks.TASK_DIR_PREFIX}old").mkdir(parents=True)
        (root / "someone-elses").mkdir()

        tasks.TaskTable({}, temp_root=str(root)).close()

        assert sorted(os.listdir(root)) == ["someone-elses"]

    def test_the_ffmpeg_logs_a_killed_node_left_in_its_temp_folder_are_removed_at_start(self, tmp_path, monkeypatch):
        """A preview's FFmpeg writes its log to the node's temp folder, outside the task's own dir; a node killed
        mid-preview (kill -9, the OOM killer, a stop past its grace) never removes it."""
        from media_preview_generator.utils import scratch_dir

        root = tmp_path / "node-tmp"
        root.mkdir()
        monkeypatch.setenv("MODE", "node")
        monkeypatch.setenv("NODE_TEMP_DIR", str(root))
        assert scratch_dir() == str(root)
        (root / "ffmpeg_output_4150646_128408868542144_1792054993907846558.log").write_text("frame=  120 fps= 60\n")
        (root / "ffmpeg_output_notes.txt").write_text("not one of FFmpeg's logs\n")

        tasks.TaskTable({}, temp_root=str(root)).close()

        assert sorted(os.listdir(root)) == ["ffmpeg_output_notes.txt"]

    def test_without_node_temp_dir_ffmpeg_logs_go_where_the_start_sweep_looks(self, tmp_path, monkeypatch):
        import tempfile

        from media_preview_generator.utils import scratch_dir

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setenv("MODE", "node")
        monkeypatch.delenv("NODE_TEMP_DIR", raising=False)
        root = tasks.default_temp_root()
        os.makedirs(root)
        log = os.path.join(scratch_dir(), "ffmpeg_output_4150646_1_2.log")
        with open(log, "w") as handle:
            handle.write("frame=  120 fps= 60\n")

        tasks.TaskTable({}, temp_root=root).close()

        assert not os.path.exists(log)


class TestStopping:
    """SIGTERM closes the table first; the server answers until the cancelled tasks' FFmpeg has stopped."""

    def test_a_closing_table_refuses_new_tasks_and_runs_none(self, make_table, media):
        # A file requeued while the node stopped was started there again, never cancelled, and died with the node.
        ran: list[int] = []
        table = make_table({"test.op": lambda ctx, node_path, params: ran.append(1) or {}})
        assert not table.stopping

        table.close()

        with pytest.raises(tasks.NodeStoppingError, match="This node is stopping"):
            table.start(_spec(media))
        assert table.stopping and table.busy() == 0 and ran == []
        assert os.listdir(table.temp_root) == []

    def test_a_task_the_stop_cancelled_is_answered_as_stopped_not_unknown(self, make_table, media):
        # Emptied at close, the table answered the app's next poll "unknown task", which the app counts as the node
        # dropping the file: two routine restarts failed a healthy file.
        table = make_table({"test.op": lambda ctx, node_path, params: _until(ctx.cancel_check) or {}})
        task_id = table.start(_spec(media))

        table.close(wait_s=5)

        with pytest.raises(tasks.NodeStoppingError, match="restarting"):
            table.poll(task_id)
        with pytest.raises(tasks.NodeStoppingError):
            table.get(task_id)
        assert table.busy() == 0 and os.listdir(table.temp_root) == []
        assert table.delete(task_id) is True and table.poll(task_id) is None

    def test_the_stopping_node_waits_until_the_app_asked_after_each_task_it_stopped(self, make_table, media):
        table = make_table({"test.op": lambda ctx, node_path, params: _until(ctx.cancel_check) or {}})
        task_id = table.start(_spec(media))
        table.close(wait_s=5)

        def app_polls_later() -> None:
            time.sleep(0.3)
            with contextlib.suppress(tasks.NodeStoppingError):
                table.poll(task_id)

        poller = threading.Thread(target=app_polls_later)
        poller.start()
        started = time.monotonic()
        table.wait_until_told(timeout_s=5)
        waited = time.monotonic() - started
        poller.join(5)

        assert 0.25 <= waited < 2

    def test_the_stopping_node_waits_no_longer_than_asked_for_an_app_that_never_asks(self, make_table, media):
        table = make_table({"test.op": lambda ctx, node_path, params: _until(ctx.cancel_check) or {}})
        table.start(_spec(media))
        table.close(wait_s=5)

        started = time.monotonic()
        table.wait_until_told(timeout_s=0.3)

        assert 0.25 <= time.monotonic() - started < 2

    def test_a_start_whose_folder_was_made_as_the_stop_began_is_refused_and_leaves_nothing(
        self, make_table, media, monkeypatch
    ):
        table = make_table({"test.op": lambda ctx, node_path, params: {}})
        make_temp_dir = table._make_temp_dir

        def stop_meanwhile(task_id: str) -> str:
            path = make_temp_dir(task_id)
            table.close()
            return path

        monkeypatch.setattr(table, "_make_temp_dir", stop_meanwhile)

        with pytest.raises(tasks.NodeStoppingError):
            table.start(_spec(media))
        assert table.busy() == 0 and os.listdir(table.temp_root) == []


class TestArtifact:
    def test_set_artifact_inside_the_temp_dir(self, make_table, media):
        def op(ctx, node_path, params):
            path = os.path.join(ctx.temp_dir, "frames.tar")
            with open(path, "wb") as handle:
                handle.write(b"tar")
            ctx.set_artifact(path)
            return {}

        table = make_table({"test.op": op})
        task_id = table.start(_spec(media))
        _finished(table, task_id)

        assert open(table.get(task_id).artifact, "rb").read() == b"tar"

    def test_artifact_outside_the_temp_dir_is_refused(self, make_table, media):
        def op(ctx, node_path, params):
            ctx.set_artifact(node_path)
            return {}

        table = make_table({"test.op": op})

        state = _finished(table, table.start(_spec(media)))

        assert state["status"] == protocol.STATUS_FAILED and "ValueError" in state["error"]["message"]
