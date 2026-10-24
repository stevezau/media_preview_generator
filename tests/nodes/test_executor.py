"""nodes.executor: one op on a node for a remote worker -- start, poll, log lines, cancel, pause, errors."""

from __future__ import annotations

import base64
import gzip
import io
import os
import re
import socket
import tarfile
import threading
import time

import pytest
import requests
from loguru import logger

from media_preview_generator.jobs.worker import is_job_thread_for, job_thread
from media_preview_generator.nodes import client as client_mod
from media_preview_generator.nodes import executor, protocol
from media_preview_generator.nodes.client import (
    READ_TIMEOUT_S,
    UNREACHABLE_DNS,
    UNREACHABLE_REFUSED,
    UNREACHABLE_TIMEOUT,
    UNREACHABLE_TLS,
    NodeAddress,
    NodeAuthError,
    NodeCantStartError,
    NodeClient,
    NodeRateLimitedError,
    NodeStoppingError,
    NodeTaskDeletedError,
    NodeTimeoutError,
    NodeUnknownTaskError,
    NodeUnreachableError,
)
from media_preview_generator.nodes.executor import (
    FileNotVisibleError,
    NodeTaskCancelledError,
    NodeTaskError,
    NodeUnavailableError,
    Placement,
    current_placement,
    extract_artifact,
    placed_on,
    run_on_node,
)
from media_preview_generator.nodes.registry import set_registry
from media_preview_generator.processing.generator import FALLBACK_HWACCEL, FALLBACK_STALL, CodecNotSupportedError

from .executor_fakes import NODE_ID, NODE_NAME, FakeRegistry, ScriptedClient, finished, running, unreachable

PLACEMENT = Placement(NODE_ID, NODE_NAME)

# `truncate -s 4000000000 00001.jpg && tar --sparse --format=gnu -cf bomb.tar 00001.jpg` (10,240 bytes), gzipped.
SPARSE_BOMB_GZ_B64 = (
    "H4sIAAAAAAAC/+3QQQrDMAxEUR3FJyhjW7YP0hMYCoWsQpvcP8mikFUChXRR/tuMQALBSFK8DePTrrO+UHXfMraifX5YLKm6sppnU1ynZuFuPzC/"
    "p/4KwR596kd3Z/uv5VJzqsm3FvaNXPIAAAAAAAAAAAAAAAAAAPAnFj+Ygq8AKAAA"
)


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(executor, "POLL_INTERVAL_S", 0.0)
    # Polls take no time here, so the node counts as gone on the failed-poll count alone.
    monkeypatch.setattr(executor, "NODE_GONE_AFTER_S", 0.0)
    monkeypatch.setattr(executor, "_pending_deletes", {})
    yield
    set_registry(None)


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "data" / "Show" / "S01E01.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0123456789")
    return str(path)


def _install(client, **kwargs) -> FakeRegistry:
    registry = FakeRegistry({NODE_ID: client}, **kwargs)
    set_registry(registry)
    return registry


def _run(media, **kwargs):
    kwargs.setdefault("gpu", None)
    kwargs.setdefault("cancel_check", None)
    with placed_on(PLACEMENT):
        return run_on_node(protocol.OP_INTRO_FINGERPRINT, media, {"duration_ms": 1000}, **kwargs)


class _FakeTime:
    """Stands in for the executor's ``time`` module: ``sleep`` moves ``monotonic`` on, at once."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def _wait_until(check, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return False


def test_frames_are_unpacked_from_the_downloaded_buffer_without_copying_it(tmp_path):
    import tracemalloc

    frames = [(f"{n}.jpg", os.urandom(1024 * 1024)) for n in range(1, 25)]
    data = bytearray(_tar(frames))
    tracemalloc.start()
    try:
        names = extract_artifact(data, str(tmp_path))
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert names == sorted(name for name, _ in frames)
    assert (tmp_path / "7.jpg").read_bytes() == frames[6][1]
    assert peak < len(data) / 2
    assert extract_artifact(memoryview(_tar(frames[:1])), str(tmp_path / "again")) == ["1.jpg"]


def _tar(entries: list[tuple[str, bytes]], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


class TestPlacement:
    def test_placed_on_sets_and_restores_the_threads_placement(self):
        assert current_placement() is None
        with placed_on(PLACEMENT):
            assert current_placement() == PLACEMENT
            with placed_on(None):
                assert current_placement() is None
            assert current_placement() == PLACEMENT
        assert current_placement() is None

    def test_a_placement_belongs_to_its_own_thread(self):
        seen = []
        with placed_on(PLACEMENT):
            thread = threading.Thread(target=lambda: seen.append(current_placement()))
            thread.start()
            thread.join()
        assert seen == [None]

    def test_run_on_node_without_a_placement_is_a_bug(self, media):
        _install(ScriptedClient([finished(protocol.STATUS_DONE, result={})]))
        with pytest.raises(RuntimeError, match="placement"):
            run_on_node(protocol.OP_INTRO_FINGERPRINT, media, {}, gpu=None, cancel_check=None)


class TestStartAndResult:
    def test_spec_carries_the_apps_path_identity_mapping_gpu_and_params(self, media):
        client = ScriptedClient([running(), finished(protocol.STATUS_DONE, result={"points": 1})])
        _install(client)

        result = _run(media, gpu=("NVIDIA", "cuda:0"))

        assert result == {"points": 1}
        stat = os.stat(media)
        assert re.fullmatch(r"[0-9a-f]{32}", client.spec["client_task_id"])
        assert client.spec == {
            "client_task_id": client.spec["client_task_id"],
            "op": protocol.OP_INTRO_FINGERPRINT,
            "path": media,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "mapping": [{"app_prefix": "/", "node_prefix": "/mnt/media"}],
            "gpu": {"type": "NVIDIA", "device": "cuda:0"},
            "params": {"duration_ms": 1000},
        }
        assert _wait_until(lambda: client.named("delete") == [("delete",)])

    def test_cpu_task_sends_no_gpu(self, media):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        _install(client)
        _run(media)
        assert client.spec["gpu"] is None

    def test_missing_app_file_raises_oserror_before_the_node_is_asked(self, tmp_path):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        _install(client)
        with pytest.raises(FileNotFoundError):
            _run(str(tmp_path / "gone.mkv"))
        assert client.calls == []

    def test_a_removed_node_is_unavailable(self, media):
        set_registry(FakeRegistry({}))
        with pytest.raises(NodeUnavailableError, match="no longer configured"):
            _run(media)

    def test_log_from_follows_next_log(self, media):
        client = ScriptedClient(
            [running(next_log=3), running(next_log=5), finished(protocol.STATUS_DONE, result={}, next_log=5)]
        )
        _install(client)
        _run(media)
        assert [c[1] for c in client.named("poll")] == [0, 3, 5]


class TestArtifact:
    def test_frames_are_unpacked_into_the_folder(self, media, tmp_path):
        dest = tmp_path / "frames"
        dest.mkdir()
        tar = _tar([("0000000000.jpg", b"a"), ("0000000005.jpg", b"b")])
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={"frames": 2})], artifact=tar)
        _install(client)

        _run(media, artifact_to=str(dest))

        assert sorted(os.listdir(dest)) == ["0000000000.jpg", "0000000005.jpg"]
        assert (dest / "0000000005.jpg").read_bytes() == b"b"
        assert client.named("artifact")
        assert _wait_until(lambda: client.named("delete") == [("delete",)])

    @pytest.mark.parametrize(
        ("entries", "symlink"),
        [
            ([("../evil.jpg", b"x")], None),
            ([("sub/x.jpg", b"x")], None),
            ([("/abs.jpg", b"x")], None),
            ([("ok.jpg", b"x")], "link.jpg"),
        ],
    )
    def test_a_tar_with_anything_but_bare_files_is_refused_and_nothing_is_written(
        self, media, tmp_path, entries, symlink
    ):
        dest = tmp_path / "frames"
        dest.mkdir()
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})], artifact=_tar(entries, symlink=symlink))
        _install(client)

        with pytest.raises(NodeTaskError, match="unexpected entry"):
            _run(media, artifact_to=str(dest))

        assert os.listdir(dest) == []
        assert not (tmp_path / "evil.jpg").exists()

    def test_garbage_artifact_is_a_task_error(self, media, tmp_path):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})], artifact=b"not a tar at all")
        _install(client)
        with pytest.raises(NodeTaskError, match="couldn't be unpacked"):
            _run(media, artifact_to=str(tmp_path))

    def test_a_sparse_member_declaring_gigabytes_is_refused_and_nothing_is_written(self, tmp_path):
        # 10 KB of tar that unpacks into a 4,000,000,000-byte "JPEG" (see SPARSE_BOMB_GZ_B64).
        data = gzip.decompress(base64.b64decode(SPARSE_BOMB_GZ_B64))
        assert len(data) == 10240
        with pytest.raises(NodeTaskError, match="unexpected entry"):
            extract_artifact(data, str(tmp_path))
        assert os.listdir(tmp_path) == []

    def test_a_frame_bigger_than_any_preview_frame_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(executor, "MAX_FRAME_BYTES", 4)
        with pytest.raises(NodeTaskError, match="far bigger"):
            extract_artifact(_tar([("1.jpg", b"1234"), ("2.jpg", b"12345")]), str(tmp_path))
        assert os.listdir(tmp_path) == []

    def test_more_frames_than_any_preview_has_are_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(executor, "MAX_FRAMES", 2)
        assert extract_artifact(_tar([("1.jpg", b"a"), ("2.jpg", b"b")]), str(tmp_path)) == ["1.jpg", "2.jpg"]
        with pytest.raises(NodeTaskError, match="far bigger"):
            extract_artifact(_tar([("3.jpg", b"a"), ("4.jpg", b"b"), ("5.jpg", b"c")]), str(tmp_path))
        assert sorted(os.listdir(tmp_path)) == ["1.jpg", "2.jpg"]

    def test_frames_adding_up_past_the_artifact_cap_are_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(executor, "MAX_ARTIFACT_BYTES", 8)
        with pytest.raises(NodeTaskError, match="far bigger"):
            extract_artifact(_tar([("1.jpg", b"12345"), ("2.jpg", b"12345")]), str(tmp_path))
        assert os.listdir(tmp_path) == []

    def test_artifact_download_failure_is_node_unavailable(self, media, tmp_path):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        client.fetch_artifact = lambda task_id, cancel_check=None: (_ for _ in ()).throw(unreachable())
        registry = _install(client)
        with pytest.raises(NodeUnavailableError) as raised:
            _run(media, artifact_to=str(tmp_path))
        assert raised.value.started is True
        assert len(client.named("poll")) == 1
        assert registry.reports == [("unavailable", NODE_ID)]

    def test_a_fetch_that_waited_out_its_timeout_is_not_tried_again(self, media, tmp_path):
        # Each fetch waits up to ARTIFACT_TIMEOUT_S (minutes) for the next byte: three of them held the worker ~15 min.
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])

        def fetch(task_id, cancel_check=None):
            client.calls.append(("artifact",))
            raise NodeTimeoutError("http://n didn't answer in time", reason=UNREACHABLE_TIMEOUT)

        client.fetch_artifact = fetch
        _install(client)

        with pytest.raises(NodeUnavailableError, match="didn't send the frames"):
            _run(media, artifact_to=str(tmp_path))
        assert len(client.named("artifact")) == 1

    def test_a_cancel_while_the_frames_fetch_fails_ends_the_file_without_another_fetch(self, media, tmp_path):
        cancelled = []
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])

        def fetch(task_id, cancel_check=None):
            client.calls.append(("artifact",))
            cancelled.append(True)  # the job is cancelled while this fetch fails
            raise NodeUnreachableError("http://n answered HTTP 502")

        client.fetch_artifact = fetch
        _install(client)

        with pytest.raises(NodeTaskCancelledError):
            _run(media, artifact_to=str(tmp_path), cancel_check=lambda: bool(cancelled))
        assert len(client.named("artifact")) == 1

    @pytest.mark.parametrize(
        "error",
        [
            NodeRateLimitedError("http://n asks this app to wait: too many wrong keys came from here"),
            NodeTimeoutError("http://n dropped the connection while answering"),
            NodeUnreachableError("http://n answered HTTP 502"),
            NodeUnreachableError("http://n can't be reached (connection refused)", reason=UNREACHABLE_REFUSED),
        ],
        ids=["429", "connection-dropped", "gateway-502", "refused"],
    )
    def test_a_passing_error_fetching_the_finished_frames_is_tried_again_not_a_drop(self, media, tmp_path, error):
        """The node keeps a finished task for minutes: the finished frames were thrown away and the file went back
        in the queue with a drop counted (two fail it) for a 429 or a moment's drop."""
        tar = _tar([("0000000000.jpg", b"a")])
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={"frames": 1})], artifact=tar)
        answers = [error, error]

        def fetch(task_id, cancel_check=None):
            client.calls.append(("artifact",))
            if answers:
                raise answers.pop(0)
            return tar

        client.fetch_artifact = fetch
        registry = _install(client)
        dest = tmp_path / "frames"
        dest.mkdir()

        assert _run(media, artifact_to=str(dest)) == {"frames": 1}

        assert os.listdir(dest) == ["0000000000.jpg"]
        assert len(client.named("artifact")) == 3
        assert registry.reports == []

    @pytest.mark.parametrize(
        ("error", "started"),
        [
            (NodeRateLimitedError("http://n asks this app to wait"), False),
            (NodeUnreachableError("http://n answered HTTP 502"), True),
        ],
        ids=["429-is-no-drop", "a-5xx-is-a-drop"],
    )
    def test_a_fetch_failing_as_long_as_a_poll_may_is_given_up(self, media, tmp_path, monkeypatch, error, started):
        clock = _FakeTime()
        monkeypatch.setattr(executor, "time", clock)
        monkeypatch.setattr(executor, "POLL_INTERVAL_S", 1.0)
        monkeypatch.setattr(executor, "NODE_GONE_AFTER_S", 12.0)
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        fetched_at: list[float] = []

        def fetch(task_id, cancel_check=None):
            fetched_at.append(clock.now)
            if len(fetched_at) > 1000:  # a build that never gives up fails here instead of hanging
                return _tar([])
            raise error

        client.fetch_artifact = fetch
        _install(client)

        with pytest.raises(NodeUnavailableError, match="didn't send the frames") as raised:
            _run(media, artifact_to=str(tmp_path))

        assert raised.value.started is started
        assert fetched_at[-1] - fetched_at[0] == 12.0
        assert len(fetched_at) == 13
        reason = executor.REQUEUE_NODE_UNAVAILABLE if started else executor.REQUEUE_NODE_NOT_STARTED
        assert executor.requeue_for(raised.value).reason == reason

    @pytest.mark.parametrize(
        ("error", "started"),
        [(NodeUnknownTaskError("unknown_task"), True), (NodeStoppingError("http://n is restarting"), False)],
        ids=["forgot-the-task", "restarting"],
    )
    def test_a_node_that_no_longer_has_the_frames_isnt_asked_again(self, media, tmp_path, error, started):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        fetched: list[str] = []
        client.fetch_artifact = lambda task_id, cancel_check=None: (
            fetched.append(task_id) or (_ for _ in ()).throw(error)
        )
        _install(client)

        with pytest.raises(NodeUnavailableError) as raised:
            _run(media, artifact_to=str(tmp_path))

        assert raised.value.started is started
        assert fetched == ["t1"]


@pytest.fixture
def stalling_node():
    """``(start, gets)``: ``start(stall_at)`` runs a listener whose every GET stalls (held open, nothing more sent)
    before its answer's headers, or after the headers and the first 64 KB of a 10 MB tar (a cable pulled, the node
    machine asleep), and returns its URL; ``gets`` holds when each GET came."""
    stop = threading.Event()
    servers: list[socket.socket] = []
    gets: list[float] = []

    def handle(conn: socket.socket, stall_at: str) -> None:
        with conn:
            received = b""
            while b"\r\n\r\n" not in received:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                received += chunk
            gets.append(time.monotonic())
            if stall_at == "mid-body":
                head = b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: 10000000\r\n\r\n"
                conn.sendall(head + b"\0" * 65536)
            stop.wait(30)

    def start(stall_at: str) -> str:
        server = socket.create_server(("127.0.0.1", 0))
        servers.append(server)

        def serve() -> None:
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                threading.Thread(target=handle, args=(conn, stall_at), daemon=True).start()

        threading.Thread(target=serve, daemon=True).start()
        return f"http://127.0.0.1:{server.getsockname()[1]}"

    yield start, gets
    stop.set()
    for server in servers:
        server.close()


class TestAStalledFramesDownload:
    """The task finished on the node and the frames' download then stalls. Each fetch waited 300 s for the next byte;
    one that stalled after its headers was taken for a passing error and fetched three times (15 minutes), and a cancel
    let go of the worker only after the whole wait."""

    @pytest.mark.parametrize("cancelled", [False, True], ids=["running", "cancelled"])
    @pytest.mark.parametrize("stall_at", ["before-headers", "mid-body"])
    def test_is_given_up_after_one_read_timeout_and_not_fetched_again(
        self, media, tmp_path, monkeypatch, stalling_node, stall_at, cancelled
    ):
        monkeypatch.setattr(client_mod, "ARTIFACT_TIMEOUT_S", 1.0)  # only how long each wait is
        monkeypatch.setattr(client_mod, "ARTIFACT_HEADERS_TIMEOUT_S", 1.0)
        start, gets = stalling_node
        session = requests.Session()
        session.trust_env = False  # a proxy from the environment must not sit between the test and 127.0.0.1
        real = NodeClient(NodeAddress(start(stall_at), "k" * 24), session=session)
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        client.fetch_artifact = real.fetch_artifact
        _install(client)
        expected = NodeTaskCancelledError if cancelled else NodeUnavailableError

        with pytest.raises(expected, match="didn't send the frames"):
            _run(media, artifact_to=str(tmp_path / "frames"), cancel_check=lambda: cancelled and bool(gets))
        ended = time.monotonic()

        assert len(gets) == 1
        assert ended - gets[0] < client_mod.ARTIFACT_TIMEOUT_S + 2.0


class TestNodeDrops:
    def test_three_failed_polls_in_a_row_make_the_node_unavailable(self, media):
        client = ScriptedClient([running(), unreachable(), unreachable(), unreachable()])
        registry = _install(client)

        with pytest.raises(NodeUnavailableError) as raised:
            _run(media)

        assert raised.value.node_id == NODE_ID and raised.value.node_name == NODE_NAME
        assert "stopped answering" in str(raised.value)
        assert len(client.named("poll")) == 4
        assert registry.reports == [("unavailable", NODE_ID)]
        # Still asked to drop it: a node back from a blip stops the task now, not beside the file's next try.
        assert _wait_until(lambda: client.named("delete") == [("delete",)])

    def test_a_blip_shorter_than_the_give_up_time_does_not_drop_a_task_the_node_still_runs(self, media, monkeypatch):
        # Refused connections fail at once: four failed polls in a row take a fraction of the give-up time.
        monkeypatch.setattr(executor, "POLL_INTERVAL_S", 0.02)
        monkeypatch.setattr(executor, "NODE_GONE_AFTER_S", 1.0)
        client = ScriptedClient(
            [running(), unreachable(), unreachable(), unreachable(), unreachable(),
             finished(protocol.STATUS_DONE, result={"ok": 1})]
        )  # fmt: skip
        registry = _install(client)

        assert _run(media) == {"ok": 1}
        assert registry.reports == []

    def test_polls_failing_for_the_whole_give_up_time_drop_it(self, media, monkeypatch):
        monkeypatch.setattr(executor, "POLL_INTERVAL_S", 0.02)
        monkeypatch.setattr(executor, "NODE_GONE_AFTER_S", 0.3)
        client = ScriptedClient([running(), unreachable()])
        _install(client)

        started = time.monotonic()
        with pytest.raises(NodeUnavailableError, match="stopped answering"):
            _run(media)

        assert time.monotonic() - started >= 0.3
        assert len(client.named("poll")) > executor.MAX_FAILED_POLLS + 1
        assert _wait_until(lambda: client.named("delete") == [("delete",)])

    def test_a_node_that_stopped_answering_doesnt_hold_the_requeue_for_its_delete(self, media, monkeypatch):
        monkeypatch.setattr(executor, "POLL_INTERVAL_S", 0.02)
        monkeypatch.setattr(executor, "NODE_GONE_AFTER_S", 0.1)
        release = threading.Event()
        timeouts: list = []

        class HungDelete(ScriptedClient):
            def delete_task(self, task_id, *, read_timeout=None):
                timeouts.append(read_timeout)
                release.wait(10)
                super().delete_task(task_id)

        client = HungDelete([running(), unreachable()])
        _install(client)

        started = time.monotonic()
        with pytest.raises(NodeUnavailableError, match="stopped answering"):
            _run(media)
        given_up_after = time.monotonic() - started

        try:
            assert given_up_after < 2.0
            assert _wait_until(lambda: timeouts == [executor.GONE_DELETE_TIMEOUT_S])
        finally:
            release.set()
        assert _wait_until(lambda: client.deleted == ["t1"])

    def test_a_node_that_forgot_the_task_is_not_asked_to_delete_it(self, media):
        client = ScriptedClient([running(), NodeUnknownTaskError("unknown_task")])
        _install(client)
        with pytest.raises(NodeUnavailableError):
            _run(media)
        assert client.named("delete") == []

    def test_polls_held_back_by_the_wrong_key_limit_keep_waiting_for_a_task_the_node_runs(self, media):
        # Something else at this app's address with an old key: the node answers 429 to every request from here for
        # a while, and the task keeps running. Read as the node going away, two of those failed the file.
        waits = [NodeRateLimitedError("http://n asks this app to wait")] * (executor.MAX_FAILED_POLLS + 3)
        client = ScriptedClient([running(), unreachable(), *waits, finished(protocol.STATUS_DONE, result={"ok": 1})])
        registry = _install(client)

        assert _run(media) == {"ok": 1}
        assert registry.reports == []

    def test_polls_held_back_past_the_nodes_unpolled_task_expiry_give_the_file_back(self, media, monkeypatch):
        # A poll the node turns away (429) never reaches the task, so the node drops it once no poll has reached it for
        # its unpolled-task expiry. Polls held back for good went on forever: the file never ended or went elsewhere.
        clock = _FakeTime()
        monkeypatch.setattr(executor, "time", clock)
        monkeypatch.setattr(executor, "POLL_INTERVAL_S", 1.0)

        def held(client):
            # Ends the run on a build that never gives up, so the test fails instead of hanging.
            if len(client.named("poll")) < 1000:
                return NodeRateLimitedError("http://n asks this app to wait")
            return finished(protocol.STATUS_DONE, result={})

        client = ScriptedClient([running(), held])
        registry = _install(client)

        started = clock.now
        with pytest.raises(NodeUnavailableError, match="kept asking this app to wait") as raised:
            _run(media)

        assert protocol.TASK_UNPOLLED_EXPIRY_S < clock.now - started <= executor.RATE_LIMITED_GIVE_UP_S + 2.0
        assert raised.value.started is False  # nothing the node or the file did: no drop counts against the file
        assert registry.reports == [("unavailable", NODE_ID)]

    def test_a_task_the_node_expired_while_its_polls_were_held_back_gives_the_file_back_with_no_drop(
        self, media, monkeypatch
    ):
        # The 429s end between the node's unpolled-task expiry and this app's give-up: the next poll reaches the node,
        # which has dropped the task by then and answers 404. Read as "it restarted?", that counted a drop.
        clock = _FakeTime()
        monkeypatch.setattr(executor, "time", clock)
        monkeypatch.setattr(executor, "POLL_INTERVAL_S", 1.0)
        held_until = clock.now + protocol.TASK_UNPOLLED_EXPIRY_S + 5.0

        def poll(client):
            if clock.now < held_until:
                return NodeRateLimitedError("http://n asks this app to wait")
            return NodeUnknownTaskError("unknown_task")

        client = ScriptedClient([running(), *([poll] * 200)])
        registry = _install(client)

        with pytest.raises(NodeUnavailableError, match="kept asking this app to wait") as raised:
            _run(media)

        assert raised.value.started is False
        assert registry.reports == [("unavailable", NODE_ID)]
        assert client.named("delete") == []

    def test_a_task_forgotten_after_a_long_gap_with_no_held_back_poll_is_read_as_a_restart(self, media, monkeypatch):
        # No 429 came back (the app was paused past the node's unpolled-task expiry): the wrong-keys message would
        # blame something that never happened, and the node going away counts a drop as usual.
        clock = _FakeTime()
        monkeypatch.setattr(executor, "time", clock)
        monkeypatch.setattr(executor, "POLL_INTERVAL_S", 1.0)

        def answered_then_paused(client):
            clock.now += protocol.TASK_UNPOLLED_EXPIRY_S + 5.0
            return running()

        client = ScriptedClient([running(), answered_then_paused, NodeUnknownTaskError("unknown_task")])
        _install(client)

        with pytest.raises(NodeUnavailableError, match="no longer knows the task") as raised:
            _run(media)

        assert raised.value.started is not False

    def test_polls_held_back_now_and_then_keep_waiting_for_a_task_the_node_still_runs(self, media, monkeypatch):
        clock = _FakeTime()
        monkeypatch.setattr(executor, "time", clock)
        monkeypatch.setattr(executor, "POLL_INTERVAL_S", 1.0)
        wait = NodeRateLimitedError("http://n asks this app to wait")
        steps = ([wait] * 50 + [running()]) * 3
        client = ScriptedClient([running(), *steps, finished(protocol.STATUS_DONE, result={"ok": 1})])
        _install(client)
        started = clock.now

        assert _run(media) == {"ok": 1}
        assert clock.now - started > executor.RATE_LIMITED_GIVE_UP_S

    def test_a_cancelled_task_held_back_by_the_wrong_key_limit_is_still_given_up_on(self, media, monkeypatch):
        monkeypatch.setattr(executor, "CANCEL_WAIT_S", 0.0)
        client = ScriptedClient([running(), NodeRateLimitedError("http://n asks this app to wait")])
        _install(client)

        with pytest.raises(NodeTaskCancelledError, match="didn't stop the cancelled task"):
            _run(media, cancel_check=lambda: bool(client.named("poll")))  # cancelled once the task runs

    def test_two_failed_polls_then_an_answer_carry_on(self, media):
        client = ScriptedClient(
            [unreachable(), unreachable(), running(), unreachable(), finished(protocol.STATUS_DONE, result={"ok": 1})]
        )
        registry = _install(client)
        assert _run(media) == {"ok": 1}
        assert registry.reports == []

    def test_a_node_that_stopped_the_task_to_restart_gives_the_file_back_without_a_drop(self, media):
        # Two routine restarts while a file ran there failed it: "it dropped the file twice".
        client = ScriptedClient([running(), NodeStoppingError("http://n is restarting")])
        registry = _install(client)
        with pytest.raises(NodeUnavailableError, match=f"^{NODE_NAME} is restarting$") as raised:
            _run(media)
        assert raised.value.started is False
        assert registry.reports == [("unavailable", NODE_ID)]

    def test_a_node_that_restarts_before_sending_the_frames_gives_the_file_back_without_a_drop(self, media, tmp_path):
        class Restarting(ScriptedClient):
            def fetch_artifact(self, task_id, cancel_check=None):
                raise NodeStoppingError("http://n is restarting")

        _install(Restarting([finished(protocol.STATUS_DONE, result={})]))
        with pytest.raises(NodeUnavailableError, match="didn't send the frames") as raised:
            _run(media, artifact_to=str(tmp_path))
        assert raised.value.started is False

    def test_a_node_that_forgot_the_task_is_unavailable_at_once(self, media):
        client = ScriptedClient([running(), NodeUnknownTaskError("unknown_task")])
        registry = _install(client)
        with pytest.raises(NodeUnavailableError, match="no longer knows"):
            _run(media)
        assert registry.reports == [("unavailable", NODE_ID)]

    @pytest.mark.parametrize(
        "error",
        [
            unreachable(),
            NodeAuthError("refused the key"),
            # Its temp folder can't be used: the file goes back in the queue and the node is checked now, rather than
            # the file failing while the node stays ready for the next one.
            NodeCantStartError("The node can't write to its temp folder /node-tmp (Permission denied)"),
            # Stopping (SIGTERM): it goes elsewhere, with no drop counted against the file.
            NodeStoppingError("http://n is restarting"),
        ],
    )
    def test_a_node_that_wont_start_the_task_is_unavailable_and_never_ran_it(self, media, error):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})], start_error=error)
        registry = _install(client)
        with pytest.raises(NodeUnavailableError, match="couldn't start") as raised:
            _run(media)
        assert raised.value.started is False
        assert executor.requeue_for(raised.value).reason == executor.REQUEUE_NODE_NOT_STARTED
        assert registry.reports == [("unavailable", NODE_ID)]

    @pytest.mark.parametrize(
        "error",
        [
            NodeTimeoutError("http://n didn't answer in time", reason=UNREACHABLE_TIMEOUT),
            NodeTimeoutError("http://n dropped the connection before answering"),
            NodeUnreachableError("http://n answered HTTP 502"),
            NodeUnreachableError("http://n answered HTTP 504"),
        ],
        ids=["no-answer-in-time", "connection-dropped", "gateway-502", "gateway-504"],
    )
    def test_a_start_that_may_have_reached_the_node_is_cancelled_there_by_its_client_id(self, media, error):
        """A gateway in front of the node forwards the start and answers 502/504 itself: the node runs the task."""
        timeouts: list = []

        class Client(ScriptedClient):
            def delete_task(self, task_id, *, read_timeout=None):
                timeouts.append(read_timeout)
                super().delete_task(task_id)

        client = Client([running()], start_error=error)
        registry = _install(client)
        with pytest.raises(NodeUnavailableError, match="couldn't start") as raised:
            _run(media)
        assert raised.value.started is False
        assert _wait_until(lambda: client.deleted == [client.started_with["client_task_id"]])
        assert timeouts == [executor.GONE_DELETE_TIMEOUT_S]
        assert client.named("poll") == []
        assert registry.reports == [("unavailable", NODE_ID)]

    def test_a_start_that_timed_out_doesnt_hold_the_requeue_for_its_delete(self, media):
        release = threading.Event()

        class HungDelete(ScriptedClient):
            def delete_task(self, task_id, *, read_timeout=None):
                release.wait(10)  # a hung node: the DELETE waits out its read timeout
                super().delete_task(task_id)

        client = HungDelete([running()], start_error=NodeTimeoutError("http://n didn't answer in time"))
        _install(client)

        started = time.monotonic()
        try:
            with pytest.raises(NodeUnavailableError, match="couldn't start"):
                _run(media)
            given_up_after = time.monotonic() - started
        finally:
            release.set()

        assert given_up_after < 2.0
        assert _wait_until(lambda: client.deleted == [client.started_with["client_task_id"]])

    def test_a_start_the_node_refuses_as_already_deleted_never_ran(self, media):
        client = ScriptedClient(
            [running()], start_error=NodeTaskDeletedError("this task was deleted before it started")
        )
        _install(client)
        with pytest.raises(NodeUnavailableError, match="couldn't start") as raised:
            _run(media)
        assert raised.value.started is False
        assert executor.requeue_for(raised.value).reason == executor.REQUEUE_NODE_NOT_STARTED
        assert client.deleted == [] and client.named("poll") == []

    def test_each_task_gets_its_own_client_id(self, media):
        ids = []
        for _ in range(2):
            client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
            _install(client)
            _run(media)
            ids.append(client.spec["client_task_id"])
        assert ids[0] != ids[1]

    @pytest.mark.parametrize(
        "error",
        [
            NodeUnreachableError("http://n didn't answer (connection refused)", reason=UNREACHABLE_REFUSED),
            NodeUnreachableError("http://n didn't answer (no machine called n was found)", reason=UNREACHABLE_DNS),
            NodeUnreachableError("http://n didn't answer over https", reason=UNREACHABLE_TLS),
            unreachable(),
            NodeAuthError("http://n refused the key"),
            # The node's own answers that it took nothing: a 503 node_stopping is the node, not a gateway.
            NodeCantStartError("The node can't write to its temp folder /node-tmp (Permission denied)"),
            NodeStoppingError("http://n is restarting"),
            # Too many wrong keys from this address: the node compared nothing and ran nothing.
            NodeRateLimitedError("http://n asks this app to wait: too many wrong keys came from here"),
        ],
        ids=["refused", "no-such-machine", "tls", "connect-timeout", "key-refused", "temp-folder", "node-stopping",
             "rate-limited"],
    )  # fmt: skip
    def test_a_start_that_never_reached_the_node_cancels_nothing(self, media, error):
        client = ScriptedClient([running()], start_error=error)
        _install(client)
        with pytest.raises(NodeUnavailableError):
            _run(media)
        time.sleep(0.1)  # a DELETE would be sent from a thread of its own
        assert client.named("delete") == []

    def test_a_removed_node_never_ran_the_task(self, media):
        set_registry(FakeRegistry({}))
        with pytest.raises(NodeUnavailableError) as raised:
            _run(media)
        assert raised.value.started is False

    @pytest.mark.parametrize(
        "script",
        [[running(), unreachable(), unreachable(), unreachable()], [running(), NodeUnknownTaskError("unknown_task")]],
        ids=["stopped-answering", "forgot-the-task"],
    )
    def test_a_drop_after_the_start_counts_as_a_drop(self, media, script):
        _install(ScriptedClient(script))
        with pytest.raises(NodeUnavailableError) as raised:
            _run(media)
        assert raised.value.started is True
        assert executor.requeue_for(raised.value).reason == executor.REQUEUE_NODE_UNAVAILABLE


class _DeleteAnswers(ScriptedClient):
    """Each DELETE raises the next of ``answers`` (None, or none left: it is delivered)."""

    def __init__(self, script: list, answers: list) -> None:
        super().__init__(script)
        self.answers = list(answers)
        self.timeouts: list = []

    def delete_task(self, task_id: str, *, read_timeout: float | None = None) -> None:
        self.calls.append(("delete",))
        self.timeouts.append(read_timeout)
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            raise answer
        self.deleted.append(task_id)


class TestFreeingTheTask:
    """Until a DELETE reaches it, the node keeps a finished task and its temp dir (a preview's frames) 10 minutes."""

    DONE = [finished(protocol.STATUS_DONE, result={"ok": 1})]

    def test_a_delete_lost_to_a_passing_error_is_sent_again_without_holding_the_file(self, media, monkeypatch):
        monkeypatch.setattr(executor, "DELETE_RETRY_DELAYS_S", (0.5,))
        lost = NodeUnreachableError("http://n didn't answer (connection refused)", reason=UNREACHABLE_REFUSED)
        client = _DeleteAnswers(self.DONE, [lost])
        _install(client)

        started = time.monotonic()
        assert _run(media) == {"ok": 1}
        assert time.monotonic() - started < 0.4
        assert client.deleted == []

        assert _wait_until(lambda: client.deleted == ["t1"])
        assert client.timeouts == [executor.GONE_DELETE_TIMEOUT_S] * 2

    def test_a_finished_file_is_not_held_by_the_delete_of_a_node_that_stopped_answering(self, media, monkeypatch):
        """A node that freezes right after it sent the result (or a link that drops after a 1 GB artifact): each DELETE
        waits out its read timeout, while the file's publish and its worker slot wait for none of them."""
        monkeypatch.setattr(executor, "DELETE_RETRY_DELAYS_S", (0.0,))
        waits: list[float] = []

        class HangsAfterResult(ScriptedClient):
            def delete_task(self, task_id: str, *, read_timeout: float = READ_TIMEOUT_S) -> None:
                waits.append(read_timeout)
                time.sleep(read_timeout)
                raise NodeTimeoutError("http://n didn't answer in time")

        _install(HangsAfterResult(self.DONE))

        started = time.monotonic()
        assert _run(media) == {"ok": 1}
        assert time.monotonic() - started < 1.0
        assert _wait_until(lambda: waits == [executor.GONE_DELETE_TIMEOUT_S] * 2, timeout=10)

    @pytest.mark.parametrize("node_gone", [False, True], ids=["after-a-finished-task", "after-the-node-went-away"])
    def test_a_delete_nothing_answers_is_given_up_after_its_retries(self, media, monkeypatch, node_gone):
        monkeypatch.setattr(executor, "DELETE_RETRY_DELAYS_S", (0.0, 0.0))
        script = [running(), unreachable()] if node_gone else self.DONE
        client = _DeleteAnswers(script, [NodeTimeoutError("http://n didn't answer in time")] * 10)
        _install(client)

        if node_gone:
            with pytest.raises(NodeUnavailableError):
                _run(media)
        else:
            _run(media)

        assert _wait_until(lambda: len(client.named("delete")) == 3)
        time.sleep(0.1)
        assert len(client.named("delete")) == 3 and client.deleted == []

    @pytest.mark.parametrize("node_gone", [False, True], ids=["after-a-finished-task", "after-the-node-went-away"])
    def test_a_delete_nothing_answers_is_sent_again_once_the_node_answers_a_check(self, media, monkeypatch, node_gone):
        """An outage longer than the retries (~5 s) left the node a finished task's temp dir (a preview's frames and
        their tar) for 10 minutes."""
        monkeypatch.setattr(executor, "DELETE_RETRY_DELAYS_S", (0.0, 0.0))
        script = [running(), unreachable()] if node_gone else self.DONE
        lost = NodeTimeoutError("http://n didn't answer in time")
        client = _DeleteAnswers(script, [lost, lost, lost, lost])
        _install(client)

        if node_gone:
            with pytest.raises(NodeUnavailableError):
                _run(media)
        else:
            _run(media)

        assert _wait_until(lambda: executor._pending_deletes == {NODE_ID: {"t1": None}})
        assert client.deleted == []
        executor.send_pending_deletes("another-node", client)
        executor.send_pending_deletes(NODE_ID, client)  # it answers a check, but the DELETE is lost again
        assert _wait_until(lambda: len(client.named("delete")) == 4 and executor._pending_deletes)
        assert executor._pending_deletes == {NODE_ID: {"t1": None}}
        executor.send_pending_deletes(NODE_ID, client)
        assert _wait_until(lambda: client.deleted == ["t1"])
        assert executor._pending_deletes == {} and client.timeouts == [executor.GONE_DELETE_TIMEOUT_S] * 5

    def test_a_node_away_for_long_keeps_only_its_latest_lost_deletes(self, monkeypatch):
        monkeypatch.setattr(executor, "MAX_PENDING_DELETES", 2)
        for task_id in ("t1", "t2", "t3"):
            executor._keep_pending(NODE_ID, task_id)
        executor._keep_pending("node-b", "t9")

        assert executor._pending_deletes == {NODE_ID: {"t2": None, "t3": None}, "node-b": {"t9": None}}

    @pytest.mark.parametrize(
        "answer",
        [NodeUnknownTaskError("unknown_task"), NodeAuthError("http://n refused the key")],
        ids=["node-doesnt-have-it", "key-refused"],
    )
    def test_a_delete_the_node_answered_is_not_sent_again(self, media, monkeypatch, answer):
        monkeypatch.setattr(executor, "DELETE_RETRY_DELAYS_S", (0.0, 0.0))
        client = _DeleteAnswers(self.DONE, [answer])
        _install(client)

        _run(media)

        assert _wait_until(lambda: len(client.named("delete")) == 1)
        time.sleep(0.1)
        assert len(client.named("delete")) == 1


class _KeyChanged(ScriptedClient):
    """A node recreated with a new key: from the first ``refused_from`` request on, every request is refused (401)."""

    def __init__(self, script: list, refused_from: str) -> None:
        super().__init__(script)
        self.refused_from = refused_from
        self.refused: list[str] = []

    def _gate(self, name: str) -> None:
        if self.refused or name == self.refused_from:
            self.refused.append(name)
            raise NodeAuthError("http://n refused the key")

    def start_task(self, spec: dict) -> str:
        self._gate("start")
        return super().start_task(spec)

    def poll_task(self, task_id: str, *, log_from: int = 0) -> dict:
        self._gate("poll")
        return super().poll_task(task_id, log_from=log_from)

    def cancel_task(self, task_id: str) -> None:
        self._gate("cancel")
        super().cancel_task(task_id)

    def pause_task(self, task_id: str) -> None:
        self._gate("pause")
        super().pause_task(task_id)

    def fetch_artifact(self, task_id: str, cancel_check=None) -> bytes:
        self._gate("artifact")
        return super().fetch_artifact(task_id)

    def delete_task(self, task_id: str, *, read_timeout: float | None = None) -> None:
        self._gate("delete")
        super().delete_task(task_id, read_timeout=read_timeout)


class TestARefusedKeyMidTask:
    """A node recreated with a new key (or its NODE_KEY changed) while a file runs there: each wrong key from this
    app's address brings the node closer to holding the address back, the app's own check and the right key included,
    so the first refusal ends the file there and nothing more is sent."""

    @pytest.mark.parametrize(
        ("refused_from", "script", "ends_with", "says"),
        [
            pytest.param("start", [running()], NodeUnavailableError, "couldn't start the task", id="start"),
            pytest.param("poll", [running()], NodeUnavailableError, "NAS refused the key", id="poll"),
            pytest.param("pause", [running()], NodeUnavailableError, "NAS refused the key", id="pause"),
            # The job's cancel was refused: the file ends as cancelled, as the job asked.
            pytest.param("cancel", [running()], NodeTaskCancelledError, "NAS refused the key", id="cancel"),
            pytest.param(
                "artifact", [finished(protocol.STATUS_DONE, result={})], NodeUnavailableError, "NAS refused the key",
                id="artifact",
            ),
        ],
    )  # fmt: skip
    def test_the_first_refusal_ends_the_file_there_and_nothing_more_is_sent(
        self, media, tmp_path, refused_from, script, ends_with, says
    ):
        client = _KeyChanged(script, refused_from)
        registry = _install(client)
        polled = lambda: bool(client.named("poll"))  # noqa: E731 - the job's cancel or pause comes after one poll

        with pytest.raises(ends_with, match=says) as raised:
            _run(
                media,
                artifact_to=str(tmp_path),
                cancel_check=polled if refused_from == "cancel" else None,
                pause_check=polled if refused_from == "pause" else None,
            )

        time.sleep(0.1)  # a DELETE would be sent from a thread of its own
        assert client.refused == [refused_from]
        # The check this starts reads the refused key for the node's card.
        assert registry.reports == [("unavailable", NODE_ID)]
        if ends_with is NodeUnavailableError:
            # Not the file's doing: it goes elsewhere with no drop counted.
            assert raised.value.started is False
            assert executor.requeue_for(raised.value).reason == executor.REQUEUE_NODE_NOT_STARTED


class TestErrors:
    @pytest.mark.parametrize("other", [None, False, True], ids=["own-file", "own-file-said-so", "other-file"])
    def test_file_not_visible_is_raised_with_the_reason_and_reported_unless_it_isnt_the_workers_own(self, media, other):
        """A season sibling the node can't see is left out by the step reading it; reported, each episode of its
        season counted it towards pausing the node."""
        error = {"kind": protocol.KIND_FILE_NOT_VISIBLE, "message": "no such file", "reason": "missing"}
        client = ScriptedClient([finished(protocol.STATUS_FILE_NOT_VISIBLE, error=error)])
        registry = _install(client)

        with pytest.raises(FileNotVisibleError) as raised:
            if other is None:
                _run(media)
            else:
                with executor.reading_other_file(other):
                    _run(media)

        assert (raised.value.node_id, raised.value.node_name, raised.value.reason) == (NODE_ID, NODE_NAME, "missing")
        assert registry.reports == ([] if other else [("not_visible", NODE_ID, media, "missing")])

    def test_a_file_no_mapping_covers_never_reaches_the_node_and_does_not_count_against_it(self, media, monkeypatch):
        """Files of a library the node has no row for were sent to it anyway and paused the node's other libraries."""
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        registry = _install(client)
        monkeypatch.setattr(registry, "mappings", lambda node_id: [protocol.PathMapping("/elsewhere", "/mnt/x")])

        with pytest.raises(FileNotVisibleError) as raised:
            _run(media)

        assert raised.value.reason == protocol.NOT_VISIBLE_UNMAPPED
        assert client.spec is None
        assert registry.reports == []

    def test_a_node_saying_unmapped_does_not_count_against_it(self, media):
        error = {"kind": protocol.KIND_FILE_NOT_VISIBLE, "message": "x", "reason": protocol.NOT_VISIBLE_UNMAPPED}
        registry = _install(ScriptedClient([finished(protocol.STATUS_FILE_NOT_VISIBLE, error=error)]))

        with pytest.raises(FileNotVisibleError):
            _run(media)

        assert registry.reports == []

    def test_file_not_visible_says_why_in_plain_words_without_the_nodes_path(self, media):
        error = {"kind": protocol.KIND_FILE_NOT_VISIBLE, "message": "no such file: /mnt/nas/a.mkv", "reason": "missing"}
        _install(ScriptedClient([finished(protocol.STATUS_FILE_NOT_VISIBLE, error=error)]))

        with pytest.raises(FileNotVisibleError) as raised:
            _run(media)

        assert str(raised.value) == f"{NODE_NAME} can't see the file: not found on the node"

    def test_codec_not_supported_raises_the_apps_exception_with_its_kind(self, media):
        error = {"kind": protocol.KIND_CODEC_NOT_SUPPORTED, "message": "GPU can't", "fallback_kind": FALLBACK_STALL}
        client = ScriptedClient([finished(protocol.STATUS_FAILED, error=error)])
        registry = _install(client)

        with pytest.raises(CodecNotSupportedError, match="GPU can't") as raised:
            _run(media, gpu=("NVIDIA", "cuda:0"))

        assert raised.value.kind == FALLBACK_STALL
        assert registry.reports == []

    def test_gpu_failure_reports_the_device_and_raises_codec_not_supported(self, media):
        error = {"kind": protocol.KIND_GPU_FAILED, "message": "cuda error"}
        client = ScriptedClient([finished(protocol.STATUS_FAILED, error=error)])
        registry = _install(client)

        with pytest.raises(CodecNotSupportedError) as raised:
            _run(media, gpu=("NVIDIA", "cuda:0"))

        assert raised.value.kind == FALLBACK_HWACCEL
        assert registry.reports == [("gpu_error", NODE_ID, "cuda:0")]

    def test_gpu_failure_naming_its_exception_is_left_to_the_op_to_raise_again(self, media):
        error = {"kind": protocol.KIND_GPU_FAILED, "message": "timed out", "error_type": "GpuAttemptFailedError"}
        client = ScriptedClient([finished(protocol.STATUS_FAILED, error=error)])
        registry = _install(client)

        with pytest.raises(NodeTaskError) as raised:
            _run(media, gpu=("NVIDIA", "cuda:0"))

        assert raised.value.error_type == "GpuAttemptFailedError" and raised.value.message == "timed out"
        assert registry.reports == [("gpu_error", NODE_ID, "cuda:0")]

    @pytest.mark.parametrize(
        ("kind", "error_type", "raised_type"),
        [
            (protocol.KIND_GPU_FAILED, "", CodecNotSupportedError),
            (protocol.KIND_CODEC_NOT_SUPPORTED, "", CodecNotSupportedError),
            ("failed", "RuntimeError", NodeTaskError),
            (protocol.KIND_TEMP_FOLDER, "", NodeUnavailableError),
            (protocol.KIND_CANCELLED, "", NodeTaskCancelledError),
        ],
        ids=["gpu_failed", "codec_not_supported", "failed", "temp_folder", "cancelled"],
    )
    def test_an_error_names_the_file_by_this_apps_path(self, media, monkeypatch, kind, error_type, raised_type):
        """The CPU-fallback line and the worker card's reason named the file by the node's own path."""
        app_root = os.path.dirname(os.path.dirname(media))
        node_path = "/mnt/nas" + media[len(app_root) :]
        error = {"kind": kind, "message": f"FFmpeg failed for {node_path} (exit code 218)", "error_type": error_type}
        registry = _install(ScriptedClient([finished(protocol.STATUS_FAILED, error=error)]))
        monkeypatch.setattr(
            registry, "mappings", lambda node_id: [protocol.PathMapping(app_prefix=app_root, node_prefix="/mnt/nas")]
        )

        with pytest.raises(raised_type) as raised:
            _run(media, gpu=("NVIDIA", "cuda:0"))

        text = str(raised.value) + str(getattr(raised.value, "message", ""))
        assert node_path not in text
        assert f"FFmpeg failed for {media} (exit code 218)" in text

    def test_a_failed_tasks_message_is_one_line_of_bounded_length(self, media):
        # The app logs it ("Could not extract preview frames ...") and shows it on the file's row in the job.
        forged = "decode slow\n2026-10-15 03:00:00.000 | ERROR    | web.auth:login:88 - forged" + "x" * 5_000_000
        error = {"kind": "failed", "message": forged, "error_type": "Runtime\nError"}
        _install(ScriptedClient([finished(protocol.STATUS_FAILED, error=error)]))

        with pytest.raises(NodeTaskError) as raised:
            _run(media)

        assert str(raised.value).startswith("decode slow\\n2026-10-15 03:00:00.000 | ERROR")
        assert "\n" not in str(raised.value) and len(str(raised.value)) < protocol.RELAY_LINE_MAX + 100
        assert raised.value.error_type == "Runtime\\nError"

    def test_a_reason_the_app_doesnt_know_for_a_file_the_node_cant_see_is_one_line(self, media):
        error = {"kind": protocol.KIND_FILE_NOT_VISIBLE, "message": "x", "reason": "gone\n12:00 | ERROR | forged"}
        _install(ScriptedClient([finished(protocol.STATUS_FILE_NOT_VISIBLE, error=error)]))

        with pytest.raises(FileNotVisibleError) as raised:
            _run(media)

        assert str(raised.value) == f"{NODE_NAME} can't see the file: gone\\n12:00 | ERROR | forged"

    def test_failure_carries_the_nodes_message_type_and_extra(self, media):
        error = {"kind": "failed", "message": "boom", "error_type": "LoudnessError", "stderr_tail": ["a"]}
        _install(ScriptedClient([finished(protocol.STATUS_FAILED, error=error)]))

        with pytest.raises(NodeTaskError) as raised:
            _run(media)

        assert isinstance(raised.value, RuntimeError)
        assert (raised.value.kind, raised.value.message, raised.value.error_type) == ("failed", "boom", "LoudnessError")
        assert raised.value.extra == {"stderr_tail": ["a"]}


class TestCancelAndPause:
    def test_cancel_is_forwarded_and_the_cancelled_task_raises(self, media):
        cancel = threading.Event()

        def answer(client):
            if client.named("cancel"):
                return finished(protocol.STATUS_CANCELLED, error={"kind": "cancelled", "message": "stopped"})
            cancel.set()
            return running()

        client = ScriptedClient([answer])
        _install(client)

        with pytest.raises(NodeTaskCancelledError):
            _run(media, cancel_check=cancel.is_set)

        assert len(client.named("cancel")) == 1
        assert _wait_until(lambda: client.named("delete") == [("delete",)])

    def test_a_cancel_lost_to_one_transient_error_is_sent_again(self, media, monkeypatch):
        monkeypatch.setattr(executor, "CANCEL_WAIT_S", 0.3)

        class FlakyCancel(ScriptedClient):
            def cancel_task(self, task_id: str) -> None:
                super().cancel_task(task_id)
                if len(self.named("cancel")) == 1:
                    raise unreachable()

        client = FlakyCancel([running()])
        _install(client)

        with pytest.raises(NodeTaskCancelledError):
            _run(media, cancel_check=lambda: True)

        assert len(client.named("cancel")) == 2  # sent again until delivered, then never again

    def test_an_undelivered_cancel_is_sent_again_each_slice_without_extra_polls(self, media, monkeypatch):
        monkeypatch.setattr(executor, "POLL_INTERVAL_S", 0.5)
        monkeypatch.setattr(executor, "_SLICE_S", 0.05)

        class CancelLostTwice(ScriptedClient):
            def cancel_task(self, task_id: str) -> None:
                super().cancel_task(task_id)
                if len(self.named("cancel")) <= 2:
                    raise unreachable()

        def answer(client):
            if len(client.named("cancel")) >= 3:
                return finished(protocol.STATUS_CANCELLED, error={"kind": "cancelled", "message": "stopped"})
            return running()

        client = CancelLostTwice([answer])
        _install(client)

        with pytest.raises(NodeTaskCancelledError):
            _run(media, cancel_check=lambda: True)

        assert len(client.named("cancel")) == 3
        assert len(client.named("poll")) == 2  # the retries rode the slices; one poll after the delivered cancel

    def test_a_cancel_while_the_node_goes_away_ends_as_cancelled(self, media):
        cancel = threading.Event()
        cancel.set()
        client = ScriptedClient([unreachable()])
        _install(client)
        with pytest.raises(NodeTaskCancelledError):
            _run(media, cancel_check=cancel.is_set)

    def test_pause_and_resume_follow_the_pause_check(self, media):
        paused = {"now": False}
        polls = {"n": 0}

        def answer(client):
            polls["n"] += 1
            paused["now"] = polls["n"] in (2, 3)
            if polls["n"] >= 5:
                return finished(protocol.STATUS_DONE, result={})
            return running()

        client = ScriptedClient([answer])
        _install(client)

        _run(media, pause_check=lambda: paused["now"])

        controls = [c[0] for c in client.calls if c[0] in ("pause", "resume")]
        assert controls == ["pause", "resume"]

    def test_time_paused_on_the_node_counts_on_a_shared_freeze(self, media):
        from media_preview_generator.markers.freeze import Freeze

        paused = {"now": True}
        polls = {"n": 0}

        def answer(client):
            polls["n"] += 1
            if polls["n"] == 3:
                paused["now"] = False
            return finished(protocol.STATUS_DONE, result={}) if polls["n"] >= 4 else running()

        freeze = Freeze(lambda: paused["now"])
        _install(ScriptedClient([answer]))
        _run(media, pause_check=freeze)
        assert freeze.held_s > 0


class TestLogsAndProgress:
    def test_node_lines_land_in_the_jobs_log_with_the_node_and_device_prefix(self, media):
        lines = []
        state = running(log=[{"seq": 0, "level": "WARNING", "message": "decode {braces} slow"}], next_log=1)
        _install(ScriptedClient([state, finished(protocol.STATUS_DONE, result={})]))
        sink = logger.add(
            lambda m: lines.append((m.record["level"].name, m.record["message"])),
            filter=lambda r: is_job_thread_for(r["thread"].id, "job-1"),
            format="{message}",
        )
        try:

            def work():
                with job_thread("job-1"):
                    _run(media, gpu=("NVIDIA", "cuda:0"))

            thread = threading.Thread(target=work)
            thread.start()
            thread.join()
        finally:
            logger.remove(sink)
        assert ("WARNING", "NAS/GPU0 · decode {braces} slow") in lines

    def test_node_lines_name_the_file_by_this_apps_path(self, media, monkeypatch):
        app_root = os.path.dirname(os.path.dirname(media))
        node_path = "/mnt/nas" + media[len(app_root) :]
        lines = []
        state = running(log=[{"seq": 0, "level": "INFO", "message": f"decoding {node_path} at 3x"}], next_log=1)
        registry = _install(ScriptedClient([state, finished(protocol.STATUS_DONE, result={})]))
        monkeypatch.setattr(
            registry, "mappings", lambda node_id: [protocol.PathMapping(app_prefix=app_root, node_prefix="/mnt/nas")]
        )
        sink = logger.add(lambda m: lines.append(m.record["message"]), format="{message}")
        try:
            _run(media)
        finally:
            logger.remove(sink)
        assert f"NAS/CPU · decoding {media} at 3x" in lines

    @pytest.mark.parametrize(
        ("op", "gpu", "expected"),
        [
            (protocol.OP_LOUDNESS_ANALYZE, None, "NAS/CPU · Started loudness analysis: S01E01.mkv"),
            (protocol.OP_INTRO_FINGERPRINT, None, "NAS/CPU · Started season audio fingerprint: S01E01.mkv"),
            (protocol.OP_INTRO_END_PICTURE, ("NVIDIA", "cuda:0"), "NAS/GPU0 · Started end-picture frames: S01E01.mkv"),
            (protocol.OP_CREDITS_FIND, ("NVIDIA", "cuda:0"), "NAS/GPU0 · Started credit text: S01E01.mkv"),
            (protocol.OP_PREVIEW_FRAMES, ("NVIDIA", "cuda:0"), "NAS/GPU0 · Started preview frames: S01E01.mkv"),
        ],
        ids=["loudness", "fingerprint", "end-picture", "credits", "previews"],
    )
    def test_a_started_line_names_the_node_as_soon_as_it_takes_the_task(self, media, op, gpu, expected):
        """The lab's loudness log said "[CPU]: Started processing" only when the file was done, and markers steps on
        a node never named it."""
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(m.record["message"]), format="{message}")

        def first_poll(_client):
            # Logged before the node is ever polled, so before any line or progress of the task.
            assert expected in lines
            return finished(protocol.STATUS_DONE, result={})

        try:
            _install(ScriptedClient([first_poll]))
            with placed_on(PLACEMENT):
                run_on_node(op, media, {}, gpu=gpu, cancel_check=None)
        finally:
            logger.remove(sink)
        assert lines.count(expected) == 1

    def test_no_started_line_when_the_node_never_took_the_task(self, media):
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(m.record["message"]), format="{message}")
        try:
            _install(ScriptedClient([finished(protocol.STATUS_DONE)], start_error=unreachable()))
            with pytest.raises(NodeUnavailableError):
                _run(media)
        finally:
            logger.remove(sink)
        assert not [line for line in lines if "Started" in line]

    def test_placement_label_is_the_prefix_of_the_nodes_lines(self):
        _install(ScriptedClient([finished(protocol.STATUS_DONE)]))
        assert executor.placement_label(None) == ""
        with placed_on(PLACEMENT):
            assert executor.placement_label(("NVIDIA", "cuda:0")) == "NAS/GPU0"
            assert executor.placement_label(None) == "NAS/CPU"

    def test_the_markers_reading_line_names_the_node_it_reads_on(self):
        from media_preview_generator.markers.pipeline import _node_label

        _install(ScriptedClient([finished(protocol.STATUS_DONE)]))
        assert _node_label("NVIDIA", "cuda:0") == ""  # this app's own worker: "on the GPU (...)" as before
        with placed_on(PLACEMENT):
            assert _node_label("NVIDIA", "cuda:0") == "NAS/GPU0"
            assert _node_label(None, None) == "NAS/CPU"

    def test_cpu_tasks_are_prefixed_cpu_and_unknown_levels_log_at_info(self, media):
        lines = []
        state = running(log=[{"seq": 0, "level": "LOUD", "message": "hi"}], next_log=1)
        _install(ScriptedClient([state, finished(protocol.STATUS_DONE, result={})]))
        sink = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])), format="{message}")
        try:
            _run(media)
        finally:
            logger.remove(sink)
        assert ("INFO", "NAS/CPU · hi") in lines

    @pytest.mark.parametrize("per_file", [True, None], ids=["tagged", "untagged"])
    def test_a_line_the_node_tagged_as_one_files_story_stays_out_of_the_apps_log(self, media, per_file):
        from media_preview_generator.logging_config import _app_sink_filter

        line = {"seq": 0, "level": "INFO", "message": "FFmpeg failed while extracting frames"}
        if per_file:
            line["per_file"] = True
        _install(ScriptedClient([running(log=[line], next_log=1), finished(protocol.STATUS_DONE, result={})]))
        job_log, app_log = [], []
        sinks = [
            logger.add(lambda m: job_log.append(m.record["message"]), format="{message}"),
            logger.add(
                lambda m: app_log.append(m.record["message"]),
                format="{message}",
                filter=_app_sink_filter("INFO", console=False),
            ),
        ]
        try:
            _run(media)
        finally:
            for sink in sinks:
                logger.remove(sink)
        said = "NAS/CPU · FFmpeg failed while extracting frames"
        assert said in job_log
        assert (said in app_log) is (not per_file)

    def _relayed(self, media, message):
        lines = []
        state = running(log=[{"seq": 0, "level": "INFO", "message": message}], next_log=1)
        _install(ScriptedClient([state, finished(protocol.STATUS_DONE, result={})]))
        sink = logger.add(lambda m: lines.append(m.record["message"]), format="{message}")
        try:
            _run(media)
        finally:
            logger.remove(sink)
        return lines

    def test_a_node_line_with_a_line_break_stays_one_line_in_the_apps_log(self, media):
        forged = "decode slow\n2026-10-15 03:00:00.000 | ERROR    | web.auth:login:88 - forged\r\x1b[31m"
        lines = self._relayed(media, forged)
        assert (
            "NAS/CPU · decode slow\\n2026-10-15 03:00:00.000 | ERROR    | web.auth:login:88 - forged\\r\\x1b[31m"
            in lines
        )
        assert not any("\n" in line or "\r" in line for line in lines)

    @pytest.mark.parametrize(
        ("char", "spelled"),
        [("\u2028", "\\u2028"), ("\u2029", "\\u2029"), ("\x85", "\\x85"), ("\x9b", "\\x9b")],
        ids=["line-separator", "paragraph-separator", "next-line", "8-bit-csi"],
    )
    def test_a_unicode_line_break_or_8_bit_control_in_a_node_line_is_escaped(self, media, char, spelled):
        lines = self._relayed(media, f"decode slow{char}2026-10-15 03:00:00.000 | ERROR | forged")
        assert f"NAS/CPU · decode slow{spelled}2026-10-15 03:00:00.000 | ERROR | forged" in lines
        assert not any(char in line for line in lines)

    def test_a_long_node_line_is_cut(self, media):
        from media_preview_generator.nodes.protocol import RELAY_LINE_MAX

        lines = self._relayed(media, "x" * 5_000_000)
        assert "NAS/CPU · " + "x" * RELAY_LINE_MAX in lines
        assert max(len(line) for line in lines) < RELAY_LINE_MAX + 100

    def test_progress_is_handed_on_once_per_change(self, media):
        seen = []
        script = [
            running(progress={"percent": 10.0, "args": [1]}),
            running(progress={"percent": 10.0, "args": [1]}),
            running(progress={"percent": 50.0, "args": [5]}),
            finished(protocol.STATUS_DONE, result={}, progress={"percent": 100.0, "args": [10]}),
        ]
        _install(ScriptedClient(script))
        _run(media, progress_callback=seen.append)
        assert [p["percent"] for p in seen] == [10.0, 50.0, 100.0]

    def test_a_failing_progress_callback_never_fails_the_task(self, media):
        _install(ScriptedClient([finished(protocol.STATUS_DONE, result={"x": 1}, progress={"percent": 1.0})]))

        def boom(_state):
            raise ValueError("display")

        assert _run(media, progress_callback=boom) == {"x": 1}


class TestAppStop:
    """A stopping app frees its tasks on nodes, so none runs on beside the ones its revived job starts."""

    def test_only_tasks_still_running_are_freed_and_a_node_that_does_not_answer_holds_up_no_other(self, media):
        release = threading.Event()

        class Node(ScriptedClient):
            def __init__(self, hangs: bool) -> None:
                super().__init__([lambda client: running() if not client.deleted else NodeUnknownTaskError("gone")])
                self.hangs = hangs
                self.timeouts: list = []

            def delete_task(self, task_id: str, *, read_timeout: float | None = None) -> None:
                self.timeouts.append(read_timeout)
                if self.hangs:
                    release.wait(5)
                super().delete_task(task_id, read_timeout=read_timeout)

        asleep, awake, done = Node(hangs=True), Node(hangs=False), ScriptedClient([finished(protocol.STATUS_DONE)])
        set_registry(FakeRegistry({"asleep": asleep, "awake": awake, "done": done}))
        _run_on("done", media)
        workers = [threading.Thread(target=_run_on, args=(name, media), daemon=True) for name in ("asleep", "awake")]
        for worker in workers:
            worker.start()
        try:
            assert _wait_until(lambda: asleep.named("poll") and awake.named("poll"))

            began = time.monotonic()
            freed = executor.cancel_running_tasks(budget_s=0.3)
            took = time.monotonic() - began

            assert freed == 2 and took < 1.0
            assert awake.deleted == ["t1"] and awake.timeouts == [0.3]
            assert asleep.timeouts == [0.3] and asleep.deleted == []
            assert done.deleted == ["t1"]  # its own delete when it finished; nothing more
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
        assert executor.cancel_running_tasks() == 0

    def test_no_task_starts_on_a_node_once_the_app_is_stopping(self, media):
        """A file the stop gave back would start on another node, and nothing would ever free that task."""
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        _install(client)
        assert executor.cancel_running_tasks() == 0

        with pytest.raises(NodeTaskCancelledError, match="the app is stopping"):
            _run(media)
        assert client.named("start") == []

    def test_a_task_whose_start_is_still_on_its_way_is_freed_by_the_id_it_asked_for(self, media):
        class SlowStart(ScriptedClient):
            """The node takes the start only once ``release`` is set; it refuses one whose id was deleted first."""

            def __init__(self) -> None:
                super().__init__([running()])
                self.starting, self.release = threading.Event(), threading.Event()

            def start_task(self, spec: dict) -> str:
                self.started_with = spec
                self.starting.set()
                self.release.wait(5)
                if spec[protocol.CLIENT_TASK_ID_FIELD] in self.deleted:
                    raise NodeTaskDeletedError("http://n: the app deleted this task before its start arrived")
                return super().start_task(spec)

        client = SlowStart()
        _install(client)
        ended: list[type[BaseException]] = []

        def work() -> None:
            try:
                _run(media)
            except Exception as exc:  # noqa: BLE001 - the test reads how the file ended
                ended.append(type(exc))

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            assert client.starting.wait(5)
            assert executor.cancel_running_tasks(budget_s=0.3) == 1
            assert client.deleted == [client.started_with[protocol.CLIENT_TASK_ID_FIELD]]
        finally:
            client.release.set()
            worker.join(5)
        assert ended == [NodeTaskCancelledError]
        assert client.named("poll") == []


def _run_on(node_id: str, media: str) -> None:
    """One task on ``node_id``; how it ends doesn't matter here."""
    try:
        with placed_on(Placement(node_id, node_id)):
            run_on_node(protocol.OP_INTRO_FINGERPRINT, media, {"duration_ms": 1000}, gpu=None, cancel_check=None)
    except Exception:  # noqa: BLE001
        pass
