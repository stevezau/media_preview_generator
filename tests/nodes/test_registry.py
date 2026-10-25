"""NodeRegistry: the node state machine, capacity, reports from job code and the health loop."""

from __future__ import annotations

import errno
import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.access_results import AccessResultStore
from media_preview_generator.nodes.client import (
    UNREACHABLE_DNS,
    UNREACHABLE_REFUSED,
    UNREACHABLE_TIMEOUT,
    UNREACHABLE_TLS_CERTIFICATE,
    NodeAuthError,
    NodeProtocolError,
    NodeRateLimitedError,
    NodeTimeoutError,
    NodeUnreachableError,
)
from media_preview_generator.nodes.protocol import ROLE_PLEX_DB, ROLE_WORKER

APP_VERSION = "9.9.9"


def _info(uid="uid-1", version=APP_VERSION, roles=(ROLE_WORKER,), gpus=None, **extra):
    return {
        "ok": True,
        "node": {"id": uid, "name": "nuc", "version": version, "protocols": [1], "roles": list(roles)},
        "platform": {"os": "Linux", "arch": "x86_64", "python": "3.12"},
        "worker": {
            "gpus": gpus if gpus is not None else [],
            "cpu": {"cores": 8, "load1": 0.5, "ram_total": 16, "ram_available": 8},
            "busy": False,
        },
        **extra,
    }


GPU_OK = {"type": "NVIDIA", "device": "cuda:0", "name": "RTX", "status": "ok", "error": None}
GPU_BAD = {**GPU_OK, "status": "failed", "error": "driver gone"}


class FakeClient:
    """Answers ``info`` / ``rescan_gpus`` from scripted queues; an exception instance in a queue is raised."""

    def __init__(self, record):
        self.record = record
        self.infos: list = []
        self.default_info: object = _info()
        self.rescans: list = []
        self.info_calls = 0
        self.rescan_calls = 0

    def info(self):
        self.info_calls += 1
        answer = self.infos.pop(0) if self.infos else self.default_info
        if isinstance(answer, Exception):
            raise answer
        return answer

    def rescan_gpus(self):
        self.rescan_calls += 1
        answer = self.rescans.pop(0) if self.rescans else {"gpus": [GPU_OK]}
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, nodes=None, access_store=None, attempt_gap_s=0.0):
        self.nodes = nodes if nodes is not None else [_record()]
        self.revision = 1
        self.clients: dict[str, FakeClient] = {}
        self.uids: list[tuple[str, str]] = []
        self.legacy = ("connected", "1.2.0")
        self.quick = True
        self.unseen_asked: list[list[str]] = []
        self.libraries: list[str] = []
        self.clock = Clock()
        self.app_build = ""
        self.registry = reg.NodeRegistry(
            revision_provider=lambda: self.revision,
            nodes_provider=lambda: [dict(n) for n in self.nodes],
            client_factory=self._client,
            legacy_ping=lambda url, key: self.legacy,
            record_uid=lambda node_id, uid, url: self.uids.append((node_id, uid)),
            quick_access=lambda client, mappings, unseen: self.unseen_asked.append(unseen) or self.quick,
            library_names=lambda paths: list(self.libraries),
            app_version=lambda: APP_VERSION,
            app_build=lambda: self.app_build,
            attempt_gap_s=attempt_gap_s,
            clock=self.clock,
            wall_clock=lambda: 1_700_000_000.0,
            access_store=access_store,
        )

    def _client(self, record):
        client = FakeClient(record)
        self.clients[record["id"]] = client
        return client

    def client(self, node_id="n1") -> FakeClient:
        self.registry.views()  # builds the clients
        return self.clients[node_id]

    def change(self, **fields):
        self.nodes[0] = {**self.nodes[0], **fields}
        self.revision += 1


MAPPINGS = [{"app_prefix": "/data", "node_prefix": "/mnt"}]


def _record(**fields):
    return {
        "id": "n1",
        "node_uid": None,
        "name": "nuc",
        "url": "http://nuc:9494",
        "key": "k",
        "enabled": True,
        "path_mappings": list(MAPPINGS),
        **fields,
    }


@pytest.fixture
def h():
    harness = Harness()
    yield harness
    harness.registry.stop()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestStates:
    def test_a_new_node_is_connecting_until_its_first_check(self, h):
        view = h.registry.view("n1")
        assert (view.state, view.name, view.enabled) == (reg.STATE_CONNECTING, "nuc", True)
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "connecting to node")

    def test_a_node_that_answers_with_the_apps_version_is_ready(self, h):
        h.client().default_info = _info(gpus=[GPU_OK])
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_READY and view.reason == ""
        assert (view.version, view.roles, view.cpu_cores, view.legacy) == (APP_VERSION, (ROLE_WORKER,), 8, False)
        assert view.gpus == ({"type": "NVIDIA", "device": "cuda:0", "name": "RTX", "status": "ok"},)
        assert view.last_seen == 1_700_000_000.0
        assert h.registry.capacity("n1") == reg.NodeCapacity(True, True, "")
        # First contact records which node answers at this address.
        assert h.uids == [("n1", "uid-1")]

    def test_three_failed_attempts_make_it_offline(self, h):
        client = h.client()
        client.infos = [NodeUnreachableError("x")] * 3
        view = h.registry.check_now("n1")
        assert client.info_calls == 3
        assert view.state == reg.STATE_OFFLINE and view.reason.startswith("Nothing answered at this address.")
        assert "http://" not in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node offline")

    def test_a_node_that_takes_connections_but_never_answers_says_so(self, h):
        client = h.client()
        client.infos = [NodeTimeoutError("x")] * 3
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_OFFLINE
        assert view.reason.startswith("nuc accepts connections but doesn't answer.")

    @pytest.mark.parametrize(
        ("reason", "hint"),
        [
            (UNREACHABLE_TLS_CERTIFICATE, "nuc answers over https, but this app doesn't trust its certificate"),
            (UNREACHABLE_DNS, "Can't find nuc."),
            (UNREACHABLE_REFUSED, "Nothing is listening on that port."),
        ],
        ids=["untrusted-certificate", "unknown-host", "refused"],
    )
    def test_an_offline_node_says_why_nothing_answered(self, h, reason, hint):
        # It always read "didn't answer. Check that it is running": the card never told a node behind a private
        # certificate authority to set REQUESTS_CA_BUNDLE, though Add node did.
        h.client().default_info = NodeUnreachableError("http://nuc:9494 didn't answer", reason=reason)
        view = h.registry.check_now("n1")
        assert (view.state, view.cause) == (reg.STATE_OFFLINE, reg.OFFLINE_UNREACHABLE)
        assert hint in view.reason, view.reason

    def test_a_name_that_stops_resolving_after_the_node_was_ready_says_it_may_be_stopped(self, h):
        # On a shared Docker network a stopped container's name stops resolving, which read as a typo in the name.
        assert h.registry.check_now("n1").state == reg.STATE_READY
        h.client().default_info = NodeUnreachableError("http://nuc:9494 can't be found", reason=UNREACHABLE_DNS)
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_OFFLINE
        assert view.reason.startswith("Can't find nuc.") and "may be stopped or renamed" in view.reason, view.reason

    def test_https_to_a_node_that_speaks_http_says_use_http_not_restart_it(self, h):
        # The node's server never answers a TLS handshake: every attempt times out, which read "stuck: restart it".
        h.change(url="https://nuc:9494")
        h.client().default_info = NodeTimeoutError("https://nuc:9494 didn't answer in time", reason=UNREACHABLE_TIMEOUT)
        view = h.registry.check_now("n1")
        assert (view.state, view.cause) == (reg.STATE_OFFLINE, reg.OFFLINE_UNREACHABLE)
        assert "use http://" in view.reason and "restart" not in view.reason, view.reason

    @pytest.mark.parametrize("where", ["info", "older-helper-ping"])
    def test_a_check_that_cant_read_the_answer_takes_a_ready_node_offline(self, h, where):
        # The check used to end at the exception and leave the node's state alone: a ready node stayed ready, and
        # kept getting work, whatever it answered now.
        client = h.client()
        assert h.registry.check_now("n1").state == reg.STATE_READY
        if where == "info":
            client.default_info = RecursionError("maximum recursion depth exceeded")
        else:
            client.default_info = NodeProtocolError("404")

            def ping(url, key):
                raise RecursionError("maximum recursion depth exceeded")

            h.registry._legacy_ping = ping

        view = h.registry.check_now("n1")

        assert view.state == reg.STATE_OFFLINE and view.reason.startswith(
            "nuc answered in a way this app couldn't read"
        )
        assert h.registry.capacity("n1").cpu is False

    def test_one_failed_attempt_is_not_offline(self, h):
        client = h.client()
        client.infos = [NodeUnreachableError("x")]
        assert h.registry.check_now("n1").state == reg.STATE_READY
        assert client.info_calls == 2

    def test_offline_checks_back_off_to_five_minutes(self, h):
        client = h.client()
        client.default_info = NodeUnreachableError("down")
        delays = []
        for _ in range(5):
            h.registry.check_now("n1")
            delays.append(h.registry._nodes["n1"].next_check - h.clock.now)
        assert delays == [30.0, 60.0, 120.0, 300.0, 300.0]
        client.default_info = _info()
        h.registry.check_now("n1")
        assert h.registry._nodes["n1"].next_check - h.clock.now == reg.HEALTH_INTERVAL_S

    def test_an_offline_node_a_job_waits_for_is_checked_soon_not_after_its_back_off(self, h):
        """Back after a few misses, a node read "offline" for up to five minutes while its job kept waiting."""
        h.client().default_info = NodeUnreachableError("down")
        for _ in range(4):
            h.registry.check_now("n1")
        assert h.registry._nodes["n1"].next_check - h.clock.now == 300.0

        h.registry.work_waiting(["n1", "gone"])

        assert h.registry._nodes["n1"].next_check - h.clock.now == reg.WAITING_RECHECK_S

    def test_a_job_waiting_never_delays_a_check_or_hurries_a_node_that_is_up(self, h):
        h.client().default_info = NodeUnreachableError("down")
        h.registry.check_now("n1")
        h.registry._nodes["n1"].next_check = h.clock.now + 5.0
        h.registry.work_waiting(["n1"])
        assert h.registry._nodes["n1"].next_check - h.clock.now == 5.0

        h.client().default_info = _info()
        h.registry.check_now("n1")
        h.registry.work_waiting(["n1"])
        assert h.registry._nodes["n1"].next_check - h.clock.now == reg.HEALTH_INTERVAL_S

    def test_a_refused_key_is_key_refused_after_one_attempt(self, h):
        client = h.client()
        client.default_info = NodeAuthError("no")
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_KEY_REFUSED and client.info_calls == 1
        assert h.registry.capacity("n1").reason == "key refused"

    def test_a_worker_on_another_version_needs_an_update(self, h):
        h.client().default_info = _info(version="9.9.8")
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_UPDATE_NEEDED
        assert "9.9.8" in view.reason and APP_VERSION in view.reason
        assert view.reason.endswith(f"Update the node to {APP_VERSION}.")
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node needs an update")

    @pytest.mark.parametrize("version", ["9.9.10", "10.0.0"])
    def test_a_worker_newer_than_this_app_says_to_update_this_app(self, h, version):
        # "Update the node to 9.9.9" sent the user to downgrade a node that was already ahead.
        h.client().default_info = _info(version=version)
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_UPDATE_NEEDED
        assert (
            view.reason == f"nuc runs version {version} and this app runs {APP_VERSION}. Update this app to {version}."
        )

    @pytest.mark.parametrize(
        ("app_version", "node_version", "update"),
        [
            # Lab: an app on a :dev image read "Update the node to 4.4.3.dev5." for a node on 9.9.0.
            ("4.4.3.dev5", "9.9.0", "Update this app to 9.9.0."),
            ("5.1.0", "5.1.1.dev3", "Update this app to 5.1.1.dev3."),
            ("5.1.1.dev3", "5.1.0", "Update the node to 5.1.1.dev3."),
            ("5.1.1.dev3", "5.1.1", "Update this app to 5.1.1."),
            ("5.1.1", "5.1.1.dev3", "Update the node to 5.1.1."),
        ],
    )
    def test_a_dev_build_on_either_side_is_never_told_to_go_back(self, h, app_version, node_version, update):
        h.registry._app_version = lambda: app_version
        h.client().default_info = _info(version=node_version)
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_UPDATE_NEEDED
        assert view.reason.endswith(update), view.reason

    def test_a_worker_on_another_build_of_the_same_version_needs_an_update(self, h):
        # Pull request images and local builds all report one version: their commit tells them apart.
        h.app_build = "a" * 40
        info = _info()
        info["node"]["build"] = "b" * 40
        h.client().default_info = info

        view = h.registry.check_now("n1")

        assert view.state == reg.STATE_UPDATE_NEEDED
        assert "bbbbbbb" in view.reason and "aaaaaaa" in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node needs an update")

    @pytest.mark.parametrize(
        ("app_build", "node_build"),
        [
            ("a" * 40, "a" * 40),
            ("", "b" * 40),
            ("a" * 40, ""),
            ("abc1234" + "0" * 33, "abc1234"),
            ("abc1234", "ABC1234" + "0" * 33),
        ],
        ids=["same", "app-unknown", "node-unknown", "node-short", "app-short"],
    )
    def test_the_same_build_or_an_unknown_one_is_ready(self, h, app_build, node_build):
        h.app_build = app_build
        info = _info()
        info["node"]["build"] = node_build
        h.client().default_info = info

        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_a_short_commit_of_another_build_needs_an_update(self, h):
        h.app_build = "abc1234" + "0" * 33
        info = _info()
        info["node"]["build"] = "abc1235"
        h.client().default_info = info

        assert h.registry.check_now("n1").state == reg.STATE_UPDATE_NEEDED

    def test_a_plex_database_only_node_on_another_build_stays_ready(self, h):
        h.app_build = "a" * 40
        info = _info(roles=(ROLE_PLEX_DB,))
        info["node"]["build"] = "b" * 40
        h.client().default_info = info

        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_a_plex_database_only_node_needs_no_version_match(self, h):
        h.client().default_info = _info(version="1.0.0", roles=(ROLE_PLEX_DB,))
        assert h.registry.check_now("n1").state == reg.STATE_READY
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node doesn't run workers")

    @pytest.mark.parametrize(
        ("roles", "shown"),
        [((ROLE_PLEX_DB,), "1.2.0"), ((ROLE_WORKER, ROLE_PLEX_DB), APP_VERSION)],
        ids=["plex-database-only", "worker-too"],
    )
    def test_a_plex_database_only_node_shows_its_plex_database_roles_version(self, h, roles, shown):
        """Its package version is the app release it was built from (0.0.0.dev0 locally); the docs, the minimum and
        Servers -> Connection all speak of the Plex database role's version."""
        h.client().default_info = _info(roles=roles, agent={"version": "1.2.0"})
        assert h.registry.check_now("n1").version == shown

    def test_a_plex_database_role_older_than_the_agent_minimum_needs_an_update(self, h):
        h.client().default_info = _info(roles=(ROLE_PLEX_DB,), agent={"version": "0.1.0"})
        assert h.registry.check_now("n1").state == reg.STATE_UPDATE_NEEDED

    def test_a_reinstalled_node_with_a_new_id_stays_ready_and_its_new_id_is_recorded(self, h):
        # The saved key at the saved address is what makes it this node: a recreated container's new id is no refusal.
        h.change(node_uid="uid-original")
        h.client().default_info = _info(uid="uid-reinstalled")
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_READY and view.reason == ""
        assert h.registry.capacity("n1") == reg.NodeCapacity(True, True, "")
        assert h.uids == [("n1", "uid-reinstalled")]

    def test_the_id_on_record_is_not_written_again(self, h):
        h.change(node_uid="uid-1")
        h.client().default_info = _info(uid="uid-1")
        h.registry.check_now("n1")
        assert h.uids == []

    def test_a_worker_node_without_ffmpeg_is_not_ready_and_gets_no_work(self, h):
        info = _info()
        info["worker"]["ffmpeg"] = {"path": None, "version": None}
        h.client().default_info = info
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_NO_FFMPEG and "can't find FFmpeg" in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "FFmpeg missing on node")
        assert reg.STATE_NO_FFMPEG in reg.ALERT_STATES

    def test_a_worker_node_whose_ffmpeg_doesnt_run_is_not_ready_and_gets_no_work(self, h):
        info = _info()
        info["worker"]["ffmpeg"] = {"path": "/usr/bin/ffmpeg", "version": None}
        h.client().default_info = info
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_NO_FFMPEG and "/usr/bin/ffmpeg, but it doesn't run" in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "FFmpeg missing on node")

    def test_a_worker_node_with_ffmpeg_is_ready(self, h):
        info = _info()
        info["worker"]["ffmpeg"] = {"path": "/usr/bin/ffmpeg", "version": "7.1"}
        h.client().default_info = info
        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_a_worker_node_that_cant_use_its_temp_folder_gets_no_work(self, h):
        # Every start there would be refused, so a "ready" node would take each file and give it straight back.
        info = _info()
        problem = "The node can't write to its temp folder /node-tmp (Permission denied)"
        info["worker"]["temp"] = {"path": "/node-tmp", "free_bytes": 10**9, "problem": problem}
        h.client().default_info = info

        view = h.registry.check_now("n1")

        assert view.state == reg.STATE_TEMP_PROBLEM and reg.STATE_TEMP_PROBLEM in reg.ALERT_STATES
        assert view.reason.startswith("nuc's temp folder can't be used, so it gets no files: the node can't write")
        assert "/node-tmp (Permission denied)" in view.reason and "NODE_TEMP_DIR" in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node's temp folder can't be used")

    @pytest.mark.parametrize(
        ("problem", "advice"),
        [
            (errno.EROFS, "Mount it writable (not read-only)"),
            (errno.ENOSPC, "Free up space there"),
            (errno.EDQUOT, "Free up space there"),
            ("The node's temp folder /node-tmp filled up while working on a file (300 MB free)", "Free up space there"),
            (errno.EACCES, "Let the node's user write to it (in Docker, check PUID and PGID)"),
            (errno.EPERM, "Let the node's user write to it (in Docker, check PUID and PGID)"),
            (errno.ENOENT, "Create or mount that folder"),
            (errno.EIO, "Set NODE_TEMP_DIR on the node to a folder it can write to."),
        ],
        ids=["read-only", "full", "over-quota", "filled-by-a-task", "permission", "not-permitted", "missing", "other"],
    )
    def test_the_temp_folder_advice_fits_its_cause(self, h, problem, advice):
        # "Free up space there" was the advice for every cause, a read-only or unwritable folder too.
        from media_preview_generator.nodes.server.tasks import temp_folder_problem

        if isinstance(problem, int):
            problem = temp_folder_problem("/node-tmp", OSError(problem, os.strerror(problem)))
        info = _info()
        info["worker"]["temp"] = {"path": "/node-tmp", "free_bytes": 10**9, "problem": problem}
        h.client().default_info = info

        reason = h.registry.check_now("n1").reason

        assert reason.endswith(advice) or f". {advice}" in reason, reason
        assert "NODE_TEMP_DIR" in reason
        assert ("Free up space" in reason) == (advice == "Free up space there")

    def test_a_node_that_is_stopping_gets_no_work_and_says_it_is_restarting(self, h):
        # It answers /v1/info while it waits for its cancelled tasks' FFmpeg: read as ready, it was sent files again.
        info = _info()
        info["stopping"] = True
        h.client().default_info = info

        view = h.registry.check_now("n1")

        assert view.state == reg.STATE_OFFLINE
        assert view.reason == "nuc is restarting; it gets no files until it is back."
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node restarting")

    def test_a_restarting_node_is_checked_again_within_seconds_not_after_the_offline_back_off(self, h):
        # A docker restart is back in ~5 s; the offline back-off left it without files for a minute or more.
        client = h.client()
        stopping = _info()
        stopping["stopping"] = True
        not_answering = [NodeUnreachableError("starting")] * reg.CHECK_ATTEMPTS
        client.infos = [stopping, stopping, *not_answering, *not_answering]
        delays = []
        for _ in range(4):
            h.registry.check_now("n1")
            delays.append(h.registry._nodes["n1"].next_check - h.clock.now)
        assert delays == [reg.RESTART_RECHECK_S] * 4
        assert h.registry._nodes["n1"].offline_streak == 0

        h.registry.check_now("n1")
        assert h.registry.view("n1").state == reg.STATE_READY
        assert h.registry._nodes["n1"].next_check - h.clock.now == reg.HEALTH_INTERVAL_S

    @pytest.mark.parametrize(
        ("not_answering", "cause_after"),
        [
            (
                lambda: NodeUnreachableError("http://nuc:9494 refused", reason=UNREACHABLE_REFUSED),
                reg.OFFLINE_UNREACHABLE,
            ),
            (lambda: NodeTimeoutError("http://nuc:9494 dropped the connection before answering"), reg.OFFLINE_STUCK),
            (lambda: NodeRateLimitedError("http://nuc:9494 asks this app to wait"), reg.OFFLINE_RATE_LIMITED),
        ],
        ids=["refused", "dropped", "asks-to-wait"],
    )
    def test_a_restarting_node_that_stops_answering_still_says_it_is_restarting_until_its_window_ends(
        self, h, not_answering, cause_after
    ):
        """It said it stops, then refused connections while it started again: its card and the bell switched to
        "Nothing is listening on that port. Check the port..." for a node in the middle of a routine restart."""
        client = h.client()
        stopping = _info()
        stopping["stopping"] = True
        client.infos = [stopping]
        client.default_info = not_answering()
        h.registry.check_now("n1")

        view = h.registry.check_now("n1")

        restarting = cause_after != reg.OFFLINE_RATE_LIMITED  # a wait has something else to fix: its own words
        assert view.state == reg.STATE_OFFLINE
        assert view.cause == (reg.OFFLINE_RESTARTING if restarting else cause_after)
        assert (view.reason == "nuc is restarting; it gets no files until it is back.") is restarting
        assert (h.registry.capacity("n1").reason == "node restarting") is restarting

        h.clock.now += reg.RESTART_WINDOW_S
        view = h.registry.check_now("n1")
        assert (view.state, view.cause) == (reg.STATE_OFFLINE, cause_after)
        assert "restarting" not in view.reason

    @pytest.mark.parametrize(
        ("answer", "sent"),
        [
            (_info, True),
            (lambda: dict(_info(), worker={**_info()["worker"], "temp": {"path": "/t", "problem": "is full"}}), True),
            (lambda: NodeUnreachableError("http://nuc:9494 refused", reason=UNREACHABLE_REFUSED), False),
            (lambda: NodeAuthError("http://nuc:9494 refused the key"), False),
        ],
        ids=["ready", "temp-folder-full", "unreachable", "key-refused"],
    )
    def test_a_check_the_node_answers_sends_the_deletes_lost_while_it_was_away(self, h, monkeypatch, answer, sent):
        from media_preview_generator.nodes import executor

        calls = []
        monkeypatch.setattr(executor, "send_pending_deletes", lambda node_id, client: calls.append((node_id, client)))
        h.client().default_info = answer()

        h.registry.check_now("n1")

        assert calls == ([("n1", h.client())] if sent else [])

    def test_a_node_that_never_comes_back_from_a_restart_goes_back_to_the_offline_back_off(self, h):
        client = h.client()
        stopping = _info()
        stopping["stopping"] = True
        client.infos = [stopping]
        client.default_info = NodeUnreachableError("stopped for good")
        h.registry.check_now("n1")
        h.clock.now += reg.RESTART_WINDOW_S
        delays = []
        for _ in range(2):
            h.registry.check_now("n1")
            delays.append(h.registry._nodes["n1"].next_check - h.clock.now)
        assert delays == [30.0, 60.0]

    @pytest.mark.parametrize("temp", [{"path": "/node-tmp", "problem": None}, {"path": "/node-tmp"}, None])
    def test_a_temp_folder_without_a_problem_leaves_the_node_ready(self, h, temp):
        info = _info()
        info["worker"]["temp"] = temp
        h.client().default_info = info

        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_a_node_on_another_node_protocol_needs_an_update_naming_both_versions(self, h):
        node = {"id": "uid-1", "name": "nuc", "version": "9.10.0", "protocols": [2], "roles": [ROLE_WORKER]}
        legacy_asked = []
        h.registry._legacy_ping = lambda url, key: legacy_asked.append(url) or ("connected", "1.2.0")
        h.client().default_info = NodeProtocolError("another protocol", node=node)
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_UPDATE_NEEDED
        assert "9.10.0" in view.reason and APP_VERSION in view.reason
        assert (view.version, view.legacy) == ("9.10.0", False)
        assert legacy_asked == []

    def test_an_answer_without_a_node_block_is_tried_as_an_older_plex_helper(self, h):
        h.client().default_info = NodeProtocolError("doesn't know /v1/info")
        view = h.registry.check_now("n1")
        assert (view.state, view.legacy) == (reg.STATE_READY, True)

    def test_a_turned_off_node_is_disabled_and_never_asked(self, h):
        h.change(enabled=False)
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_DISABLED and h.client().info_calls == 0
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node turned off")


class TestLegacyHelper:
    def test_an_old_helper_that_answers_its_ping_is_a_ready_plex_database_node(self, h):
        h.client().default_info = NodeProtocolError("404")
        view = h.registry.check_now("n1")
        assert (view.state, view.legacy, view.roles, view.version) == (
            reg.STATE_READY,
            True,
            (ROLE_PLEX_DB,),
            "1.2.0",
        )
        assert h.registry.capacity("n1").cpu is False

    @pytest.mark.parametrize(
        ("ping", "state"),
        [
            (("incompatible", "0.5.0"), reg.STATE_UPDATE_NEEDED),
            (("incompatible", "3.0.0"), reg.STATE_UPDATE_NEEDED),
            (("rejected", ""), reg.STATE_KEY_REFUSED),
            (("unreachable", ""), reg.STATE_OFFLINE),
        ],
        ids=["too-old", "newer-protocol", "key-refused", "not-a-helper"],
    )
    def test_what_the_ping_says_decides_the_state(self, h, ping, state):
        h.client().default_info = NodeProtocolError("404")
        h.legacy = ping
        assert h.registry.check_now("n1").state == state


class TestAccessProblem:
    def _ready(self, h):
        h.registry.check_now("n1")
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_three_different_files_in_ten_minutes_is_an_access_problem(self, h):
        self._ready(h)
        for path in ("/data/a.mkv", "/data/b.mkv"):
            h.registry.report_not_visible("n1", path, "missing")
        assert h.registry.view("n1").state == reg.STATE_READY
        h.registry.report_not_visible("n1", "/data/c.mkv", "missing")
        view = h.registry.view("n1")
        assert view.state == reg.STATE_ACCESS_PROBLEM
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "fix media access first")

    def test_it_names_the_libraries_of_the_files_the_node_couldnt_see(self, h):
        self._ready(h)
        h.libraries = ["Open Films"]
        for path in ("/data/a.mkv", "/data/b.mkv", "/data/c.mkv"):
            h.registry.report_not_visible("n1", path, "missing")
        view = h.registry.view("n1")
        assert view.state == reg.STATE_ACCESS_PROBLEM
        assert view.reason.startswith("nuc couldn't see 3 different files in Open Films in 10 minutes")

    def test_the_libraries_are_named_from_the_deepest_library_folder_above_each_file(self, monkeypatch):
        from media_preview_generator.web import settings_manager

        servers = [
            {
                "id": "p1",
                "type": "plex",
                "name": "Home",
                "enabled": True,
                "url": "http://p:32400",
                "libraries": [
                    {"id": "1", "name": "Media", "remote_paths": ["/media"]},
                    {"id": "2", "name": "Open Films", "remote_paths": ["/media/openfilms/Movies"]},
                    {"id": "3", "name": "TV", "remote_paths": ["/media/tv"]},
                ],
            }
        ]

        class Settings:
            def get(self, key, default=None):
                return servers if key == "media_servers" else default

        monkeypatch.setattr(settings_manager, "get_settings_manager", lambda: Settings())
        paths = ["/media/openfilms/Movies/a.mkv", "/media/openfilms/Movies/b.mkv", "/media/other/c.mkv", "/elsewhere"]
        assert reg._default_library_names(paths) == ["Open Films", "Media"]

    def test_check_nows_quick_access_check_samples_the_stored_libraries(self, monkeypatch):
        from media_preview_generator.nodes import mappings
        from media_preview_generator.web import settings_manager

        servers = [{"id": "p1", "type": "plex", "name": "Home", "libraries": []}]

        class Settings:
            def get(self, key, default=None):
                return servers if key == "media_servers" else default

        asked = []
        monkeypatch.setattr(settings_manager, "get_settings_manager", lambda: Settings())
        monkeypatch.setattr(mappings, "quick_access_check", lambda *args: asked.append(args) or True)
        monkeypatch.setattr(mappings, "files_visible", lambda *args: asked.append(args) or True)
        assert reg._default_quick_access("client", list(MAPPINGS), ["/data/a.mkv"]) is True
        assert asked == [("client", list(MAPPINGS), servers), ("client", list(MAPPINGS), ["/data/a.mkv"])]

    def test_the_quick_access_check_fails_when_the_files_the_node_missed_are_still_unseen(self, monkeypatch):
        from media_preview_generator.nodes import mappings
        from media_preview_generator.web import settings_manager

        class Settings:
            def get(self, key, default=None):
                return []

        monkeypatch.setattr(settings_manager, "get_settings_manager", lambda: Settings())
        monkeypatch.setattr(mappings, "quick_access_check", lambda *args: True)
        monkeypatch.setattr(mappings, "files_visible", lambda *args: False)
        assert reg._default_quick_access("client", list(MAPPINGS), ["/data/a.mkv"]) is False

    def test_the_access_problem_reason_is_plain_words(self, h):
        self._ready(h)
        for path in ("/data/a.mkv", "/data/b.mkv", "/data/c.mkv"):
            h.registry.report_not_visible("n1", path, "missing")
        reason = h.registry.view("n1").reason
        assert "(the last one: not found on the node)" in reason
        # Shown on the Nodes page itself, and in the bell next to a "Test access" button.
        assert "on the Nodes page" not in reason and "run Test access" in reason

    def test_the_same_file_three_times_is_not(self, h):
        self._ready(h)
        for _ in range(3):
            h.registry.report_not_visible("n1", "/data/a.mkv", "missing")
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_files_spread_over_more_than_ten_minutes_are_not(self, h):
        self._ready(h)
        for path in ("/data/a.mkv", "/data/b.mkv", "/data/c.mkv"):
            h.registry.report_not_visible("n1", path, "missing")
            h.clock.now += 301
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_a_passing_test_access_clears_it(self, h):
        self.test_three_different_files_in_ten_minutes_is_an_access_problem(h)
        failing = {"libraries": [{"checked": 3, "ok": False}]}
        h.registry.record_access_result("n1", failing, tested_mappings=MAPPINGS)
        assert h.registry.view("n1").state == reg.STATE_ACCESS_PROBLEM
        h.registry.record_access_result(
            "n1", {"libraries": [{"checked": 3, "ok": True}, {"checked": 0}]}, tested_mappings=MAPPINGS
        )
        assert h.registry.view("n1").state == reg.STATE_READY
        assert h.registry.details("n1").access_result["libraries"][0]["ok"] is True

    def test_a_pass_of_mappings_replaced_since_the_test_began_changes_nothing(self, h):
        tested = list(h.nodes[0]["path_mappings"])
        h.change(path_mappings=[{"app_prefix": "/data", "node_prefix": "/volume1"}])
        # The new mappings fail on three files while the test of the old ones is still running.
        self.test_three_different_files_in_ten_minutes_is_an_access_problem(h)

        h.registry.record_access_result("n1", {"libraries": [{"checked": 3, "ok": True}]}, tested_mappings=tested)

        assert h.registry.view("n1").state == reg.STATE_ACCESS_PROBLEM
        assert h.registry.details("n1").access_result is None

    NEW_ROWS = [{"app_prefix": "/data", "node_prefix": "/volume1"}]
    PASS = {"libraries": [{"checked": 3, "ok": True}]}

    def test_a_test_of_unsaved_rows_is_adopted_when_those_rows_are_saved(self, h):
        h.registry.record_access_result("n1", self.PASS, tested_mappings=self.NEW_ROWS)
        assert h.registry.details("n1").access_result is None

        h.change(path_mappings=self.NEW_ROWS)

        assert h.registry.details("n1").access_result == self.PASS

    def test_a_held_test_is_dropped_when_the_address_changes_with_the_rows(self, h):
        h.registry.record_access_result("n1", self.PASS, tested_mappings=self.NEW_ROWS)

        h.change(url="http://other:9494", path_mappings=self.NEW_ROWS)

        assert h.registry.details("n1").access_result is None

    def test_a_held_test_is_dropped_when_the_key_changes(self, h):
        h.registry.record_access_result("n1", self.PASS, tested_mappings=self.NEW_ROWS)
        h.change(key="another-key")

        h.change(path_mappings=self.NEW_ROWS)

        assert h.registry.details("n1").access_result is None

    def test_a_held_test_expires_after_ten_minutes(self, h):
        h.registry.record_access_result("n1", self.PASS, tested_mappings=self.NEW_ROWS)
        h.clock.now += reg.PENDING_ACCESS_TTL_S + 1

        h.change(path_mappings=self.NEW_ROWS)

        assert h.registry.details("n1").access_result is None

    def test_a_test_of_an_address_the_node_no_longer_has_is_dropped(self, h):
        h.change(url="http://other:9494")

        h.registry.record_access_result("n1", self.PASS, tested_mappings=MAPPINGS, tested_url="http://nuc:9494")

        assert h.registry.details("n1").access_result is None

    def test_check_now_clears_it_only_when_the_quick_access_check_passes(self, h):
        self.test_three_different_files_in_ten_minutes_is_an_access_problem(h)
        h.quick = False
        assert h.registry.check_now("n1").state == reg.STATE_ACCESS_PROBLEM
        h.quick = True
        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_the_health_loop_clears_it_once_a_quick_access_check_passes_five_minutes_on(self, h):
        """Nothing cleared it but a check the user started: a node paused overnight stayed at 0 workers all day."""
        self.test_three_different_files_in_ten_minutes_is_an_access_problem(h)
        live = h.registry._nodes["n1"]

        live.next_check = h.clock.now
        h.registry._tick()
        assert _wait_for(lambda: not live.checking)
        assert h.registry.view("n1").state == reg.STATE_ACCESS_PROBLEM

        h.quick = False
        h.clock.now += reg.ACCESS_PROBLEM_RECHECK_S
        live.next_check = h.clock.now
        h.registry._tick()
        assert _wait_for(lambda: not live.checking)
        assert h.registry.view("n1").state == reg.STATE_ACCESS_PROBLEM

        h.quick = True
        live.next_check = h.clock.now
        h.registry._tick()
        assert _wait_for(lambda: not live.checking)
        assert h.registry.view("n1").state == reg.STATE_ACCESS_PROBLEM

        h.clock.now += reg.ACCESS_PROBLEM_RECHECK_S
        live.next_check = h.clock.now
        h.registry._tick()
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)
        # The files it couldn't see are checked again, not only one file per library.
        assert h.unseen_asked[-1] == ["/data/a.mkv", "/data/b.mkv", "/data/c.mkv"]

    def test_the_health_loop_keeps_the_recheck_for_a_node_that_isnt_ready_when_it_is_due(self, h):
        self.test_three_different_files_in_ten_minutes_is_an_access_problem(h)
        live = h.registry._nodes["n1"]
        h.clock.now += reg.ACCESS_PROBLEM_RECHECK_S
        h.client().infos = [NodeUnreachableError("gone")] * reg.CHECK_ATTEMPTS
        live.next_check = h.clock.now
        h.registry._tick()
        assert _wait_for(lambda: not live.checking)
        assert h.unseen_asked == []

        h.clock.now += 1
        live.next_check = h.clock.now
        h.registry._tick()
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)

    def test_a_quick_check_that_passes_after_a_new_pause_leaves_the_new_one(self, h):
        """A Test access cleared the pause it was checking and new misses raised another while it ran."""
        self.test_three_different_files_in_ten_minutes_is_an_access_problem(h)
        live = h.registry._nodes["n1"]

        def paused_anew_meanwhile(client, mappings, unseen):
            live.access_problem = "a newer pause"
            return True

        h.registry._quick_access = paused_anew_meanwhile
        view = h.registry.check_now("n1")

        assert (view.state, view.reason) == (reg.STATE_ACCESS_PROBLEM, "a newer pause")

    def test_a_library_no_row_covers_is_not_judged_but_a_covered_one_that_is_missing_still_fails(self):
        movies = {"library": "Movies", "checked": 3, "ok": True}
        unmapped = {"library": "Anime", "mapped": False, "checked": 0, "ok": False}
        missing = {"library": "Anime", "mapped": True, "checked": 3, "ok": False}
        assert reg.access_passed({"libraries": [movies, unmapped]}) is True
        assert reg.access_passed({"libraries": [movies, missing]}) is False
        assert reg.access_passed({"libraries": [unmapped]}) is False

    def test_a_test_access_that_checked_nothing_does_not_pass(self):
        assert reg.access_passed({"libraries": [{"checked": 0, "ok": False}]}) is False
        assert reg.access_passed(None) is False


class TestGpuLost:
    def _ready_with_gpu(self, h):
        h.client().default_info = _info(gpus=[GPU_OK])
        h.registry.check_now("n1")

    def test_a_gpu_error_whose_rescan_fails_is_gpu_lost_and_cpu_keeps_running(self, h):
        self._ready_with_gpu(h)
        client = h.client()
        client.rescans = [{"gpus": [GPU_BAD]}]
        h.registry.report_gpu_error("n1", "cuda:0")
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_GPU_LOST)
        capacity = h.registry.capacity("n1")
        assert (capacity.cpu, capacity.gpu) == (True, False) and "driver gone" in capacity.reason

    def test_a_missing_device_is_gpu_lost(self, h):
        self._ready_with_gpu(h)
        h.client().rescans = [{"worker": {"gpus": []}}]
        h.registry.report_gpu_error("n1", "cuda:0")
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_GPU_LOST)
        assert "no longer finds it" in h.registry.view("n1").reason

    def test_a_gpu_error_whose_rescan_is_fine_changes_nothing(self, h):
        self._ready_with_gpu(h)
        h.registry.report_gpu_error("n1", "cuda:0")
        assert _wait_for(lambda: h.client().rescan_calls == 1)
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_it_clears_when_the_gpu_is_back(self, h):
        self.test_a_gpu_error_whose_rescan_fails_is_gpu_lost_and_cpu_keeps_running(h)
        assert h.registry.rescan_gpus("n1").state == reg.STATE_READY

    def test_a_rescan_the_node_does_not_answer_raises_instead_of_answering_the_old_gpus(self, h):
        self._ready_with_gpu(h)
        h.client().rescans = [NodeUnreachableError("gone")]
        with pytest.raises(NodeUnreachableError):
            h.registry.rescan_gpus("n1")

    def test_the_health_loop_rechecks_a_lost_gpu_every_five_minutes(self, h):
        self.test_a_gpu_error_whose_rescan_fails_is_gpu_lost_and_cpu_keeps_running(h)
        client = h.client()
        calls = client.rescan_calls
        h.registry._nodes["n1"].next_check = h.clock.now + 10_000  # keep the plain check out of the way
        h.registry._tick()
        assert client.rescan_calls == calls
        h.clock.now += reg.GPU_LOST_RECHECK_S
        h.registry._tick()
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)

    def test_a_node_that_couldnt_detect_its_gpus_yet_pauses_gpu_members_until_it_can(self, h):
        from media_preview_generator.jobs.node_members import NodeHardware

        failing = _info()
        failing["worker"]["gpu_error"] = "RuntimeError: GPU busy"
        h.client().default_info = failing

        view = h.registry.check_now("n1")

        assert view.state == reg.STATE_GPU_LOST and "couldn't check its GPUs" in view.reason
        capacity = h.registry.capacity("n1")
        assert (capacity.cpu, capacity.gpu) == (True, False)
        hardware = NodeHardware(1, {"n1": view}, {"n1": capacity})
        assert hardware.member_can_come_back({"node": "n1", "resource": "gpu", "device": "cuda:0"})

        h.client().default_info = _info(gpus=[GPU_OK])
        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_a_gpu_error_the_node_sends_is_logged_as_one_line(self, h):
        from loguru import logger

        failing = _info()
        failing["worker"]["gpu_error"] = "GPU busy\n2026-10-15 03:00:00.000 | ERROR    | web.auth:login:88 - forged"
        h.client().default_info = failing
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(m.record["message"]), format="{message}")
        try:
            h.registry.check_now("n1")
        finally:
            logger.remove(sink)

        assert any("GPU busy\\n2026-10-15" in line for line in lines)
        assert not any("\n" in line for line in lines)

    def test_the_health_loop_rescans_a_node_that_couldnt_detect_its_gpus(self, h):
        failing = _info()
        failing["worker"]["gpu_error"] = "RuntimeError: GPU busy"
        h.client().default_info = failing
        h.registry.check_now("n1")
        h.registry._nodes["n1"].next_check = h.clock.now + 10_000
        h.clock.now += reg.GPU_LOST_RECHECK_S

        h.registry._tick()

        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)
        assert h.client().rescan_calls == 1

    def test_a_rescan_that_still_cant_detect_keeps_the_check_pending(self, h):
        failing = _info()
        failing["worker"]["gpu_error"] = "RuntimeError: GPU busy"
        h.client().default_info = failing
        h.registry.check_now("n1")
        h.client().rescans = [{"worker": {"gpus": [], "gpu_error": "RuntimeError: GPU busy"}}]

        assert h.registry.rescan_gpus("n1").state == reg.STATE_GPU_LOST

    @staticmethod
    def _gpu_detection_broken(h):
        broken = _info()
        broken["worker"]["gpu_error"] = "Failed to initialize NVML: Driver/library version mismatch"
        h.client().default_info = broken
        assert h.registry.check_now("n1").state == reg.STATE_GPU_LOST

    @staticmethod
    def _drop(h):
        h.registry._run_check = lambda *args, **kwargs: None  # keep the report's own check out of it
        h.registry.report_unavailable("n1")
        del h.registry._run_check

    def test_a_check_after_a_drop_that_finds_it_answering_vouches_for_its_cpu_members(self, h):
        self._gpu_detection_broken(h)
        self._drop(h)
        assert h.registry.checked_ready_since_unavailable("n1") is False

        h.registry.check_now("n1")

        assert h.registry.view("n1").state == reg.STATE_GPU_LOST
        assert h.registry.capacity("n1").cpu is True
        assert h.registry.checked_ready_since_unavailable("n1") is True

    def test_its_cpu_workers_take_files_again_once_a_check_after_a_drop_finds_it_answering(self, h):
        from media_preview_generator.jobs.worker import Worker, WorkerPool

        self._gpu_detection_broken(h)
        reg.set_registry(h.registry)
        try:
            pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
            worker = Worker(0, "CPU", node_id="n1", node_name="nuc")
            worker._done_event = pool._worker_done_event
            pool.workers.append(worker)
            pool.mark_node_suspect("n1")
            self._drop(h)
            assert pool._find_available_worker(kind="intro_credits") is None

            h.registry.check_now("n1")
            pool.refresh_suspect_nodes()

            assert pool._find_available_worker(kind="intro_credits") is worker
        finally:
            reg.set_registry(None)

    def test_a_plex_database_only_node_with_a_detection_error_stays_ready(self, h):
        failing = _info(roles=(ROLE_PLEX_DB,))
        failing["worker"]["gpu_error"] = "RuntimeError: GPU busy"
        h.client().default_info = failing

        assert h.registry.check_now("n1").state == reg.STATE_READY


class TestRevisionAndSettings:
    def test_revision_moves_on_state_changes_only(self, h):
        start = h.registry.revision
        h.registry.check_now("n1")
        after_ready = h.registry.revision
        assert after_ready > start
        h.registry.check_now("n1")
        assert h.registry.revision == after_ready
        h.client().default_info = NodeAuthError("no")
        h.registry.check_now("n1")
        assert h.registry.revision > after_ready

    def test_a_rename_bumps_the_revision_but_keeps_the_state(self, h):
        h.registry.check_now("n1")
        before = h.registry.revision
        h.change(name="renamed")
        assert h.registry.view("n1").name == "renamed"
        assert h.registry.view("n1").state == reg.STATE_READY
        assert h.registry.revision > before

    def test_a_renamed_nodes_reason_says_its_new_name_before_the_next_check(self, h):
        """The card said "tmp's temp folder can't be used" for a node renamed tmp-x until its next check."""
        h.client().default_info = NodeAuthError("no")
        h.registry.check_now("n1")
        assert h.registry.view("n1").reason.startswith("nuc refused the key")

        h.change(name="nuc-x")

        assert h.registry.view("n1").reason.startswith("nuc-x refused the key")

    def test_a_renamed_nodes_lost_gpu_and_paused_workers_say_its_new_name(self, h):
        h.registry.check_now("n1")
        live = h.registry._nodes["n1"]
        live.gpu_lost = "nuc lost its GPU cuda:0 (gone). Its CPU workers keep running."
        live.access_problem = "nuc couldn't see 3 different files in 10 minutes. Its workers are paused."

        h.change(name="nuc-x")

        assert h.registry.view("n1").reason.startswith("nuc-x couldn't see 3 different files")
        assert live.gpu_lost.startswith("nuc-x lost its GPU")

    @pytest.mark.parametrize(
        "fields",
        [{"url": "http://moved:9494"}, {"key": "new"}, {"path_mappings": []}],
        ids=["url", "key", "mappings"],
    )
    def test_a_new_address_key_or_mappings_goes_back_to_connecting(self, h, fields):
        h.registry.check_now("n1")
        old_client = h.client()
        h.change(**fields)
        assert h.registry.view("n1").state == reg.STATE_CONNECTING
        new_client = h.client()
        assert (new_client is not old_client) == ("path_mappings" not in fields)
        assert h.registry._nodes["n1"].next_check == 0.0

    def test_a_removed_node_has_no_capacity_and_no_client(self, h):
        h.nodes.clear()
        h.revision += 1
        assert h.registry.view("n1") is None
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node removed")
        assert h.registry.mappings("n1") == []
        with pytest.raises(KeyError):
            h.registry.client("n1")

    def test_mappings_come_from_the_record(self, h):
        assert [m.to_json() for m in h.registry.mappings("n1")] == [{"app_prefix": "/data", "node_prefix": "/mnt"}]

    def test_details_keep_the_last_info_while_offline(self, h):
        h.registry.check_now("n1")
        h.client().default_info = NodeUnreachableError("x")
        h.registry.check_now("n1")
        details = h.registry.details("n1")
        assert details.view.state == reg.STATE_OFFLINE
        assert details.cpu["cores"] == 8 and details.platform["os"] == "Linux"


class TestLoop:
    def test_the_loop_checks_new_nodes_and_stops(self, h):
        h.registry.start()
        h.registry.start()  # idempotent
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)
        h.registry.stop()
        assert h.registry._thread is None

    def test_report_unavailable_checks_now_without_the_loop(self, h):
        h.registry.check_now("n1")
        client = h.client()
        client.default_info = NodeUnreachableError("gone")
        h.registry.report_unavailable("n1")
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_OFFLINE)

    def test_report_unavailable_wakes_the_running_loop(self, h):
        h.registry.start()
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)
        h.client().default_info = NodeUnreachableError("gone")
        h.registry.report_unavailable("n1")
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_OFFLINE)

    def test_only_a_check_begun_after_a_drop_vouches_for_the_node(self, h):
        h.registry.check_now("n1")
        assert h.registry.checked_ready_since_unavailable("n1") is True
        client = h.client()
        started, release = threading.Event(), threading.Event()

        def slow_info():
            started.set()
            release.wait(5)
            return _info()

        client.info = slow_info
        checking = threading.Thread(target=h.registry.check_now, args=("n1",))
        checking.start()
        assert started.wait(5)
        h.registry._run_check = lambda *args, **kwargs: None  # keep the report's own check out of it
        h.registry.report_unavailable("n1")
        release.set()
        checking.join(5)

        # Ready, but that check began before the node dropped its file; the next one is due at once.
        assert h.registry.view("n1").state == reg.STATE_READY
        assert h.registry.checked_ready_since_unavailable("n1") is False
        assert h.registry._nodes["n1"].next_check == 0.0
        del h.registry._run_check
        client.info = lambda: _info()
        h.registry.check_now("n1")
        assert h.registry.checked_ready_since_unavailable("n1") is True

    def test_the_refused_polls_of_a_busy_node_whose_key_changed_cost_one_check(self, h):
        """16 busy workers on a node recreated with another key: each refused poll is reported, and a check per report
        (one more wrong key each) tripped the node's wrong-key limit, so it held back the right key pasted next."""
        h.registry.start()
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)
        client = h.client()
        checks_before = client.info_calls
        client.default_info = NodeAuthError("http://nuc:9494 refused the key")

        def refused_poll(turn: int) -> None:
            time.sleep(turn * 0.01)  # each task's poll comes round in its turn
            h.registry.report_unavailable("n1")

        tasks = [threading.Thread(target=refused_poll, args=(turn,)) for turn in range(16)]
        for task in tasks:
            task.start()
        for task in tasks:
            task.join(5)
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_KEY_REFUSED)
        time.sleep(0.3)  # a check a report asked for would have run by now

        assert client.info_calls - checks_before == 1
        assert not h.registry._nodes["n1"].checking

    @pytest.mark.parametrize(
        ("failure", "state", "since", "checked"),
        [
            (NodeAuthError("refused"), reg.STATE_KEY_REFUSED, 0.0, False),
            (NodeAuthError("refused"), reg.STATE_KEY_REFUSED, reg.KEY_REFUSED_RECHECK_S, True),
            (NodeUnreachableError("gone"), reg.STATE_OFFLINE, 0.0, True),
        ],
        ids=["refused-just-now", "refused-a-while-ago", "offline-just-now"],
    )
    def test_a_report_checks_the_node_now_unless_a_check_just_found_it_refusing_the_key(
        self, h, failure, state, since, checked
    ):
        h.registry.check_now("n1")
        h.client().default_info = failure
        h.registry.check_now("n1")
        assert h.registry.view("n1").state == state
        submitted: list = []
        h.registry._submit = lambda fn, *args: submitted.append(args)
        h.clock.now += since

        for _ in range(16):
            h.registry.report_unavailable("n1")

        assert bool(submitted) is checked
        assert (h.registry._nodes["n1"].next_check == 0.0) is checked
        assert h.registry.checked_ready_since_unavailable("n1") is False

    def test_a_check_that_finds_the_key_refused_isnt_followed_by_another_for_the_reports_meanwhile(self, h):
        h.registry.check_now("n1")
        client = h.client()
        started, release = threading.Event(), threading.Event()

        def refusing_info():
            started.set()
            release.wait(5)
            raise NodeAuthError("refused")

        client.info = refusing_info
        checking = threading.Thread(target=h.registry.check_now, args=("n1",))
        checking.start()
        assert started.wait(5)
        h.registry._run_check = lambda *args, **kwargs: None  # keep the report's own check out of it
        h.registry.report_unavailable("n1")
        release.set()
        checking.join(5)

        assert h.registry.view("n1").state == reg.STATE_KEY_REFUSED
        assert h.registry._nodes["n1"].next_check == h.clock.now + reg.HEALTH_INTERVAL_S

    def test_a_check_begun_after_a_drop_that_finds_the_node_down_vouches_for_nothing(self, h):
        h.registry.check_now("n1")
        h.registry._run_check = lambda *args, **kwargs: None
        h.registry.report_unavailable("n1")
        del h.registry._run_check
        h.client().infos = [NodeUnreachableError("x")] * 3
        h.registry.check_now("n1")
        assert h.registry.checked_ready_since_unavailable("n1") is False

    def test_unknown_nodes_are_ignored_by_every_report(self, h):
        h.registry.report_unavailable("nope")
        h.registry.report_not_visible("nope", "/x", "missing")
        h.registry.report_gpu_error("nope", "cuda:0")
        h.registry.record_access_result("nope", {}, tested_mappings=MAPPINGS)
        assert h.registry.check_now("nope") is None
        assert h.registry.rescan_gpus("nope") is None


class TestRequestedChecks:
    """Check now: the check runs on the health checks' pool and the request returns at once."""

    def _slow(self, client):
        started, release = threading.Event(), threading.Event()

        def slow_info():
            client.info_calls += 1
            started.set()
            release.wait(5)
            return _info()

        client.info = slow_info
        return started, release

    def test_it_returns_at_once_and_says_checking_until_the_check_is_done(self):
        h = Harness(attempt_gap_s=reg.ATTEMPT_GAP_S)
        client = h.client()
        client.default_info = NodeUnreachableError("http://nuc:9494 didn't answer: ConnectTimeout")
        try:
            started = time.monotonic()
            view = h.registry.request_check("n1")

            assert time.monotonic() - started < 0.5  # its attempts are 4 s apart
            assert view.checking is True
            assert _wait_for(lambda: client.info_calls == 1)
            assert h.registry.view("n1").checking is True
        finally:
            h.registry.stop()  # ends the wait before its next attempt
        assert _wait_for(lambda: not h.registry.view("n1").checking)
        assert h.registry.view("n1").state == reg.STATE_OFFLINE

    def test_asking_again_while_a_check_runs_starts_no_second_one(self, h):
        client = h.client()
        started, release = self._slow(client)
        h.registry.request_check("n1")
        assert started.wait(5)

        assert h.registry.request_check("n1").checking is True

        release.set()
        assert _wait_for(lambda: not h.registry.view("n1").checking)
        assert not _wait_for(lambda: client.info_calls > 1, timeout=0.3)
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_one_asked_for_while_another_check_runs_still_clears_a_fixed_access_problem(self, h):
        h.registry.check_now("n1")
        for path in ("/data/a.mkv", "/data/b.mkv", "/data/c.mkv"):
            h.registry.report_not_visible("n1", path, "missing")
        assert h.registry.view("n1").state == reg.STATE_ACCESS_PROBLEM
        started, release = self._slow(h.client())
        h.registry.report_unavailable("n1")  # a check of its own, which skips the quick access check
        assert started.wait(5)

        h.registry.request_check("n1")
        release.set()

        assert _wait_for(lambda: not h.registry.view("n1").checking)
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_a_turned_off_or_unknown_node_is_not_checked(self, h):
        h.change(enabled=False)
        assert h.registry.request_check("n1").checking is False
        assert h.client().info_calls == 0
        assert h.registry.request_check("nope") is None


def test_get_registry_is_a_singleton_and_set_registry_replaces_it():
    first = reg.get_registry()
    assert reg.get_registry() is first
    reg.set_registry(None)
    assert reg.get_registry() is not first


def test_the_app_factory_starts_the_registry_and_as_it_stops_frees_the_tasks_on_nodes_then_stops_it(monkeypatch):
    import media_preview_generator.web.app as app_mod
    from media_preview_generator.nodes.executor import cancel_running_tasks

    calls = []

    class Stub:
        def start(self):
            calls.append("start")

        def stop(self):
            calls.append("stop")

    # The autouse conftest fixture replaced the starter on this same monkeypatch; undo puts the real one back.
    monkeypatch.undo()
    stub = Stub()
    reg.set_registry(stub)
    registered = []
    monkeypatch.setattr(app_mod, "on_shutdown", lambda callback, name=None: registered.append((name, callback)))
    app_mod._start_node_registry()
    assert calls == ["start"]
    assert registered == [("node tasks", cancel_running_tasks), ("node registry", stub.stop)]


class _LockProbingStore(AccessResultStore):
    """Records, for each file read or write, whether another thread could take the registry's lock meanwhile."""

    def __init__(self, folder: str) -> None:
        super().__init__(lambda: folder)
        self.registry: reg.NodeRegistry | None = None
        self.calls: list[tuple[str, bool]] = []

    def _lock_free(self) -> bool:
        free: list[bool] = []

        def probe() -> None:
            got = self.registry._lock.acquire(timeout=1.0)
            if got:
                self.registry._lock.release()
            free.append(got)

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return free[0]

    def load(self, node_id, url, mappings):
        self.calls.append(("load", self._lock_free()))
        return super().load(node_id, url, mappings)

    def forget(self, node_id, **kwargs):
        self.calls.append(("forget", self._lock_free()))
        return super().forget(node_id, **kwargs)


class TestAccessStoreIoOutsideTheLock:
    """The store reads and writes a file; under the registry's lock every view() (each worker's capacity read, each
    page load) would wait on that disk."""

    RESULT = {"libraries": [{"checked": 3, "ok": True}]}

    def test_a_new_nodes_saved_result_is_read_outside_the_lock_and_still_handed_out(self, tmp_path):
        AccessResultStore(lambda: str(tmp_path)).save("n1", "http://nuc:9494", MAPPINGS, self.RESULT, 5.0)
        store = _LockProbingStore(str(tmp_path))
        h = Harness(access_store=store)
        store.registry = h.registry

        assert h.registry.details("n1").access_result == self.RESULT
        assert store.calls == [("load", True)]

    def test_changed_mappings_forget_the_result_outside_the_lock(self, tmp_path):
        store = _LockProbingStore(str(tmp_path))
        h = Harness(access_store=store)
        store.registry = h.registry
        h.registry.record_access_result("n1", self.RESULT, tested_mappings=MAPPINGS)

        h.change(path_mappings=[{"app_prefix": "/data", "node_prefix": "/volume1"}])
        h.registry.views()

        assert store.calls[-1] == ("forget", True)
        assert h.registry.details("n1").access_result is None
        assert json.loads((tmp_path / "node_access_results.json").read_text()) == {}

    def test_a_result_saved_for_the_new_mappings_before_the_old_one_is_forgotten_is_kept(self, tmp_path):
        store = AccessResultStore(lambda: str(tmp_path))
        new = [{"app_prefix": "/data", "node_prefix": "/volume1"}]
        store.save("n1", "http://nuc:9494", new, self.RESULT, 5.0)

        store.forget("n1", keep_url="http://nuc:9494", keep_mappings=new)

        assert store.load("n1", "http://nuc:9494", new) == (self.RESULT, 5.0)


class TestAccessResultSurvivesRestart:
    """The lab's app restart made every node's Setup Health ask for Test access again: the result lived in memory."""

    RESULT = {"libraries": [{"server": "Home", "library": "TV", "checked": 3, "ok": True, "files": []}]}

    @pytest.fixture
    def store(self, tmp_path):
        return AccessResultStore(lambda: str(tmp_path))

    def test_a_restarted_app_still_has_the_last_test_of_the_saved_mappings(self, store, tmp_path):
        before = Harness(access_store=store)
        before.registry.record_access_result("n1", self.RESULT, tested_mappings=MAPPINGS)

        after = Harness(access_store=store)  # a new process: nothing in memory

        details = after.registry.details("n1")
        assert details.access_result == self.RESULT
        assert details.access_checked_at == 1_700_000_000.0
        saved = json.loads((tmp_path / "node_access_results.json").read_text())
        assert saved["n1"]["mappings"] == [{"app_prefix": "/data", "node_prefix": "/mnt"}]
        assert saved["n1"]["url"] == "http://nuc:9494"

    def test_changing_the_nodes_mappings_forgets_its_result(self, store):
        h = Harness(access_store=store)
        h.registry.record_access_result("n1", self.RESULT, tested_mappings=MAPPINGS)

        h.change(path_mappings=[{"app_prefix": "/data", "node_prefix": "/volume1"}])

        assert h.registry.details("n1").access_result is None
        assert Harness(nodes=list(h.nodes), access_store=store).registry.details("n1").access_result is None

    def test_a_result_saved_for_other_mappings_is_never_handed_out(self, store):
        store.save("n1", "http://nuc:9494", [{"app_prefix": "/data", "node_prefix": "/old"}], self.RESULT, 1.0)
        assert Harness(access_store=store).registry.details("n1").access_result is None

    def test_a_result_saved_at_another_address_is_never_handed_out(self, store):
        """settings.json restored or edited by hand while the app was down: the old machine's result stayed."""
        store.save("n1", "http://old-machine:9494", MAPPINGS, self.RESULT, 1.0)
        assert Harness(access_store=store).registry.details("n1").access_result is None

    @pytest.mark.parametrize(
        "moved", [{"url": "http://new-machine:9494", "key": "its-key"}, {"key": "typed-again"}], ids=["address", "key"]
    )
    def test_a_new_address_or_key_forgets_the_result_with_the_same_mappings(self, store, moved):
        """Setup Health went on saying "the node sees its files" from the machine at the old address."""
        h = Harness(access_store=store)
        h.registry.record_access_result("n1", self.RESULT, tested_mappings=MAPPINGS)

        h.change(**moved)

        assert h.registry.details("n1").access_result is None
        assert Harness(nodes=list(h.nodes), access_store=store).registry.details("n1").access_result is None

    def test_a_result_for_mappings_replaced_during_the_test_is_not_saved(self, store, tmp_path):
        h = Harness(access_store=store)
        tested = list(h.nodes[0]["path_mappings"])
        h.change(path_mappings=[{"app_prefix": "/data", "node_prefix": "/volume1"}])

        h.registry.record_access_result("n1", self.RESULT, tested_mappings=tested)

        assert not (tmp_path / "node_access_results.json").exists()
        assert Harness(nodes=list(h.nodes), access_store=store).registry.details("n1").access_result is None

    def test_removed_nodes_are_dropped_from_the_file_on_the_next_save(self, store, tmp_path):
        store.save("gone", "http://gone:9494", [], self.RESULT, 1.0)
        Harness(access_store=store).registry.record_access_result("n1", self.RESULT, tested_mappings=MAPPINGS)
        assert set(json.loads((tmp_path / "node_access_results.json").read_text())) == {"n1"}

    def test_an_unreadable_file_reads_as_untested(self, store, tmp_path):
        (tmp_path / "node_access_results.json").write_text("{not json")
        assert Harness(access_store=store).registry.details("n1").access_result is None


class TestNewPlexDatabaseNodeId:
    """A Plex database node that reports a new id is taken silently only when its Plex is the one it serves."""

    def _harness(self, served):
        harness = Harness(nodes=[_record(node_uid="uid-1")])
        harness.registry._served_plex_ids = lambda node_id: list(served)
        return harness

    def _answer(self, h, uid, machine_identifier="plex-a", roles=(ROLE_PLEX_DB,)):
        info = _info(uid=uid, roles=roles)
        info["plex_db"] = {"machine_identifier": machine_identifier}
        h.client().default_info = info
        h.registry.check_now("n1")

    @pytest.mark.parametrize(
        ("served", "machine_identifier", "recorded"),
        [
            (["plex-a"], "plex-a", True),
            ([], "", True),
            (["plex-a"], "", False),
            ([""], "plex-a", False),
            (["plex-a", "plex-b"], "plex-a", False),
        ],
        ids=["same-plex", "no-server-uses-it", "node-doesnt-say", "server-doesnt-say", "two-plexes"],
    )
    def test_when_its_new_id_is_recorded(self, served, machine_identifier, recorded):
        h = self._harness(served)
        self._answer(h, "uid-2", machine_identifier)
        assert (h.uids == [("n1", "uid-2")]) is recorded
        assert (h.registry.plex_db_refusal("n1") == "") is recorded
        h.registry.stop()

    def test_a_worker_node_no_server_writes_through_is_recorded_with_its_new_id(self):
        h = self._harness([])
        self._answer(h, "uid-2", "", roles=(ROLE_WORKER,))
        assert h.uids == [("n1", "uid-2")] and h.registry.plex_db_refusal("n1") == ""
        h.registry.stop()

    def test_a_new_id_without_the_role_is_held_while_a_server_writes_through_the_node(self):
        # Taken because this answer named no Plex database role, the id would already match when the next answer
        # does: the Plex check never ran for the machine that writes that server's database.
        h = self._harness(["plex-a"])
        self._answer(h, "uid-2", "", roles=(ROLE_WORKER,))
        assert h.uids == []
        assert "Type the node's key again" in h.registry.plex_db_refusal("n1")
        h.registry.stop()

    def test_a_new_id_waits_for_the_next_check_while_the_servers_using_the_node_cant_be_read(self):
        # Unread settings can't say a server writes through the node, so they can't say none does either: taking
        # the id on an answer without the role would skip the Plex check for good, as above.
        h = self._harness([])

        def unreadable(node_id):
            raise OSError("settings.json unreadable")

        h.registry._served_plex_ids = unreadable
        self._answer(h, "uid-2", "", roles=(ROLE_WORKER,))

        assert h.uids == []
        assert h.registry.plex_db_hold("n1").waits  # neither taken nor held for a typed key: checked again soon
        assert h.registry._nodes["n1"].next_check <= h.clock.now + reg.WAITING_RECHECK_S

        h.registry._served_plex_ids = lambda node_id: ["plex-a"]
        self._answer(h, "uid-2", "", roles=(ROLE_WORKER,))
        assert h.uids == []
        assert "Type the node's key again" in h.registry.plex_db_refusal("n1")
        h.registry.stop()

    def test_a_hold_is_lifted_by_the_check_it_just_started(self):
        # The hold was returned the moment its lifting check began, so one more file was skipped after the impostor
        # had left the address.
        h = self._harness(["plex-a"])
        self._answer(h, "uid-2", "")
        assert "Type the node's key again" in h.registry.plex_db_refusal("n1")
        self._answer(h, "uid-1", "plex-a")  # the saved machine is back; the registry hasn't yet checked
        h.registry._nodes["n1"].unconfirmed_uid = "uid-2"
        h.registry._nodes["n1"].next_check = 0.0

        assert h.registry.plex_db_hold("n1") is None
        h.registry.stop()

    def test_the_refusal_lifts_when_the_saved_id_is_cleared(self):
        h = self._harness(["plex-a"])
        self._answer(h, "uid-2", "")
        assert "Type the node's key again" in h.registry.plex_db_refusal("n1")

        h.change(node_uid=None)

        assert h.registry.plex_db_refusal("n1") == ""
        h.registry.stop()

    def test_the_old_id_answering_again_lifts_the_refusal(self):
        h = self._harness(["plex-a"])
        self._answer(h, "uid-2", "")
        self._answer(h, "uid-1", "")
        assert h.registry.plex_db_refusal("n1") == ""
        h.registry.stop()

    UNCONFIRMED = reg.PlexDbHold(
        "nuc reports a new id and this app can't confirm it is the same Plex. Type the node's key again on its "
        "Settings tab to confirm."
    )

    def _new_id_with_every_server_off(self, machine_identifier):
        """The node reports a new id while no switched-on server uses it: nothing to compare with, so it is taken."""
        h = self._harness([])
        self._answer(h, "uid-2", machine_identifier)
        assert h.uids == [("n1", "uid-2")]
        h.change(node_uid="uid-2")  # as ``_default_record_uid`` saves it
        assert h.registry.plex_db_hold("n1") is None  # still nothing to protect while every server is off
        return h

    @pytest.mark.parametrize(
        ("served", "machine_identifier", "held"),
        [
            (["plex-a"], "plex-a", False),
            (["plex-a"], "", True),
            ([""], "plex-a", True),
            (["plex-a", "plex-b"], "plex-a", True),
        ],
        ids=["same-plex", "node-doesnt-say", "server-doesnt-say", "two-plexes"],
    )
    def test_a_new_id_taken_with_every_server_off_is_tied_to_their_plex_once_one_is_on(
        self, served, machine_identifier, held
    ):
        # Taken with every server using the node switched off, the id was never compared with their Plex, and
        # nothing compared it once one was switched back on: a different machine given the node's key and address,
        # beside a Plex with an empty identifier, got that server's Plex database work.
        h = self._new_id_with_every_server_off(machine_identifier)
        h.registry._served_plex_ids = lambda node_id: list(served)  # a server using the node is switched on

        hold = h.registry.plex_db_hold("n1")

        assert hold == (self.UNCONFIRMED if held else None)
        assert h.registry.view("n1").plex_db_unconfirmed is held
        self._answer(h, "uid-2", machine_identifier)  # the next check hears the saved id: it says nothing new
        assert h.registry.view("n1").plex_db_unconfirmed is held
        assert h.registry.plex_db_hold("n1") == (self.UNCONFIRMED if held else None)
        assert h.uids == [("n1", "uid-2")]
        h.registry.stop()

    def test_a_held_id_taken_with_every_server_off_is_released_by_typing_the_key_again(self):
        h = self._new_id_with_every_server_off("")
        h.registry._served_plex_ids = lambda node_id: ["plex-a"]
        assert h.registry.plex_db_hold("n1") == self.UNCONFIRMED

        h.change(node_uid=None)

        assert h.registry.plex_db_hold("n1") is None
        self._answer(h, "uid-2", "")  # recorded again as the node's first id: confirmed by the typed key
        assert h.uids == [("n1", "uid-2"), ("n1", "uid-2")]
        h.change(node_uid="uid-2")
        assert h.registry.plex_db_hold("n1") is None
        h.registry.stop()

    def test_a_held_id_taken_with_every_server_off_goes_ahead_once_the_server_names_the_nodes_plex(self):
        h = self._new_id_with_every_server_off("plex-a")
        h.registry._served_plex_ids = lambda node_id: [""]
        assert h.registry.plex_db_hold("n1") == self.UNCONFIRMED

        h.registry._served_plex_ids = lambda node_id: ["plex-a"]  # the server's identity is read

        assert h.registry.plex_db_hold("n1") is None
        assert not h.registry.view("n1").plex_db_unconfirmed
        h.registry.stop()

    def test_a_new_id_taken_with_every_server_off_waits_while_the_servers_cant_be_read(self):
        h = self._new_id_with_every_server_off("")

        def unreadable(node_id):
            raise OSError("settings.json unreadable")

        h.registry._served_plex_ids = unreadable
        hold = h.registry.plex_db_hold("n1")
        assert hold is not None and hold.waits and hold.message.startswith("Checking nuc before")

        h.registry._served_plex_ids = lambda node_id: ["plex-a"]
        assert h.registry.plex_db_hold("n1") == self.UNCONFIRMED
        h.registry.stop()

    def test_a_new_id_tied_to_their_plex_when_taken_goes_ahead_once_saved(self):
        h = self._harness(["plex-a"])
        self._answer(h, "uid-2", "plex-a")
        h.change(node_uid="uid-2")

        assert h.registry.plex_db_hold("n1") is None
        self._answer(h, "uid-2", "plex-a")
        assert h.registry.plex_db_hold("n1") is None
        assert h.uids == [("n1", "uid-2")] and not h.registry.view("n1").plex_db_unconfirmed
        h.registry.stop()


class TestPlexDatabaseNodeNotHeardSinceStart:
    """Which machine answers at a Plex database node's address is known only from a check that reached it.

    After a restart, or once a check found the node down, another machine (a new id, a Plex machine id it can't
    read) could be answering there with the saved key: until a check hears the node, its Plex database work waits.
    """

    @pytest.fixture
    def h(self):
        harness = Harness(nodes=[_record(node_uid="uid-1")])
        harness.registry._served_plex_ids = lambda node_id: ["plex-a"]
        yield harness
        harness.registry.stop()

    @staticmethod
    def _answer(uid):
        info = _info(uid=uid, roles=(ROLE_PLEX_DB,))
        info["plex_db"] = {"machine_identifier": ""}
        return info

    def test_work_asked_for_before_the_first_check_waits_for_it_and_a_swapped_machine_is_refused(self, h):
        h.client().default_info = self._answer("uid-2")

        hold = h.registry.plex_db_hold("n1")

        assert hold is not None and not hold.waits
        assert "reports a new id" in hold.message
        assert h.client().info_calls == 1

    def test_work_asked_for_before_the_first_check_goes_ahead_once_it_hears_the_saved_id(self, h):
        h.client().default_info = self._answer("uid-1")

        assert h.registry.plex_db_hold("n1") is None
        assert h.client().info_calls == 1

    def test_after_a_check_found_it_down_its_work_waits_and_its_next_check_comes_sooner(self, h):
        client = h.client()
        client.infos = [NodeUnreachableError("down")] * reg.CHECK_ATTEMPTS
        client.default_info = self._answer("uid-2")
        h.registry.check_now("n1")
        assert h.registry.view("n1").state == reg.STATE_OFFLINE

        hold = h.registry.plex_db_hold("n1")

        assert hold == reg.PlexDbHold(
            "Checking nuc before sending it Plex database work. That work waits until the node answers; nothing is "
            "lost.",
            waits=True,
        )
        assert h.registry._nodes["n1"].next_check == h.clock.now + reg.WAITING_RECHECK_S
        h.clock.now += reg.WAITING_RECHECK_S
        h.registry.plex_db_hold("n1")
        assert _wait_for(lambda: "reports a new id" in h.registry.plex_db_refusal("n1"))

    def test_work_asked_for_while_it_is_down_keeps_its_check_back_off(self, h):
        # Each request for Plex database work started a check of a node that was down: 8 s checks back to back.
        client = h.client()
        client.default_info = NodeUnreachableError("down")
        h.registry.check_now("n1")
        live = h.registry._nodes["n1"]

        for _ in range(3):
            assert h.registry.plex_db_hold("n1").waits
            assert _wait_for(lambda: not live.checking)

        assert client.info_calls == reg.CHECK_ATTEMPTS

    def test_a_node_heard_with_its_saved_id_that_goes_down_is_held_again(self, h):
        client = h.client()
        client.default_info = self._answer("uid-1")
        h.registry.check_now("n1")
        assert h.registry.plex_db_hold("n1") is None

        client.infos = [NodeUnreachableError("down")] * reg.CHECK_ATTEMPTS
        h.registry.check_now("n1")

        assert h.registry.plex_db_hold("n1").waits

    @pytest.mark.parametrize("how", ["reported-unavailable", "another-address"])
    def test_a_node_heard_with_its_saved_id_is_held_again_when(self, h, how):
        h.client().default_info = self._answer("uid-1")
        h.registry.check_now("n1")
        assert h.registry.plex_db_hold("n1") is None

        if how == "reported-unavailable":
            h.client().default_info = NodeUnreachableError("down")
            h.registry.report_unavailable("n1")
        else:
            h.change(url="http://other:9494")
            h.client().default_info = NodeUnreachableError("down")  # the new client: nothing answers there yet

        assert h.registry.plex_db_hold("n1").waits

    @pytest.mark.parametrize("after", ["app-start", "node-down"])
    def test_an_older_release_that_gives_no_id_is_held_until_updated_or_confirmed(self, h, after):
        # An answer without an id used to count as hearing the node: an older Plex helper beside another Plex, with
        # the saved key at the saved address, got this server's markers.
        client = h.client()
        if after == "node-down":
            client.default_info = self._answer("uid-1")
            h.registry.check_now("n1")
            client.infos = [NodeUnreachableError("down")] * reg.CHECK_ATTEMPTS
            h.registry.check_now("n1")
        client.default_info = NodeProtocolError("404")  # an older Plex helper doesn't know /v1/info
        h.legacy = ("connected", "1.2.0")
        if after == "node-down":
            h.registry.check_now("n1")

        hold = h.registry.plex_db_hold("n1")

        assert hold is not None and not hold.waits
        assert hold.message == f"{reg.no_id_reason('nuc')} Until then this app doesn't use it for Plex's database."
        # Every Plex database request is refused, so its card, list and bell say it needs an update, not Ready.
        view = h.registry.view("n1")
        assert (view.state, view.reason, view.legacy) == (reg.STATE_UPDATE_NEEDED, reg.no_id_reason("nuc"), True)
        h.change(node_uid=None)  # its key typed again
        assert h.registry.plex_db_hold("n1") is None
        assert h.registry.check_now("n1").state == reg.STATE_READY

    @pytest.mark.parametrize("held", ["new-id", "no-id"])
    def test_a_held_node_is_checked_again_soon_so_the_saved_machines_return_lifts_the_hold(self, h, held):
        # Lab: once another machine left the node's address, files went on being skipped ("Type the node's key
        # again") until the regular check, 30 s later: the hold returned before bringing that check forward.
        client = h.client()
        client.default_info = self._answer("uid-1")
        h.registry.check_now("n1")
        client.default_info = self._answer("uid-2") if held == "new-id" else NodeProtocolError("404")
        h.registry.check_now("n1")  # the check that held it was a moment ago

        hold = h.registry.plex_db_hold("n1")  # a file's write asks

        assert hold is not None and not hold.waits
        client.default_info = self._answer("uid-1")  # the saved machine is back at the address
        h.clock.now += reg.WAITING_RECHECK_S
        h.registry._tick()
        assert _wait_for(lambda: not h.registry.view("n1").checking)
        assert h.registry.plex_db_hold("n1") is None

    def test_a_node_that_refuses_the_key_without_an_id_is_held_with_its_own_reason(self, h):
        h.client().default_info = NodeAuthError("401")

        hold = h.registry.plex_db_hold("n1")

        assert hold is not None and not hold.waits
        assert hold.message.startswith("nuc refused the key.")

    def test_a_new_id_isnt_taken_for_the_saved_one_while_it_is_being_settled(self, h):
        client = h.client()
        client.default_info = self._answer("uid-1")
        h.registry.check_now("n1")
        seen = []
        confirmed = h.registry._new_uid_confirmed

        def spy(node_id, record, outcome):
            seen.append(h.registry._nodes["n1"].heard_uid)
            return confirmed(node_id, record, outcome)

        h.registry._new_uid_confirmed = spy
        client.default_info = self._answer("uid-2")
        h.registry.check_now("n1")

        assert seen == [None]

    def test_a_check_begun_before_a_restore_doesnt_vouch_for_the_restored_id(self, h):
        # A restore with the same address and key but another saved id: the check that began before it heard the
        # old id and stored it as heard, cleared the hold and put the next check 30 s out, so the restored record's
        # Plex database work went to the machine with the old id.
        client = h.client()
        asked, release = threading.Event(), threading.Event()

        def slow_info():
            asked.set()
            release.wait(5)
            return self._answer("uid-1")

        client.info = slow_info
        check = threading.Thread(target=h.registry.check_now, args=("n1",))
        check.start()
        assert asked.wait(5)
        h.change(node_uid="uid-2")
        h.registry.views()
        release.set()
        check.join(5)

        live = h.registry._nodes["n1"]
        assert live.heard_uid is None
        assert live.next_check == 0.0
        client.info = lambda: self._answer("uid-1")
        assert h.registry.plex_db_hold("n1") is not None

    def test_a_node_without_a_saved_id_is_never_held(self, h):
        h.change(node_uid=None)
        h.client().default_info = NodeUnreachableError("down")

        assert h.registry.plex_db_hold("n1") is None
        assert h.client().info_calls == 0


@pytest.mark.parametrize(
    ("protocols", "state"),
    [([1], reg.STATE_UPDATE_NEEDED), ([1, 2], reg.STATE_READY)],
    ids=["from-before-the-id-check", "with-the-id-check"],
)
def test_a_plex_database_node_from_before_the_id_check_needs_an_update(h, protocols, state):
    # Its role ignores the saved id the app sends, so it would run a request meant for another machine: the app sends
    # it protocol 2, which it refuses, and the Nodes page says why instead of showing it ready.
    info = _info(roles=(ROLE_PLEX_DB,))
    info["agent"] = {"version": "1.2.0", "protocols": protocols, "capabilities": []}
    h.client().default_info = info

    view = h.registry.check_now("n1")

    assert view.state == state
    if state == reg.STATE_UPDATE_NEEDED:
        assert view.reason == "nuc's Plex database role is version 1.2.0, too old for this app. Update the node."


def test_a_node_asking_this_apps_address_to_wait_reads_offline_not_key_refused(h):
    from media_preview_generator.nodes.client import NodeRateLimitedError

    h.client().default_info = NodeRateLimitedError("http://nuc:9494 asks this app to wait")

    view = h.registry.check_now("n1")

    assert view.state == reg.STATE_OFFLINE
    assert "too many wrong keys came from this app's address" in view.reason


def _stopping_info():
    info = _info()
    info["stopping"] = True
    return info


@pytest.mark.parametrize(
    ("answer", "cause"),
    [
        (lambda: NodeUnreachableError("http://nuc:9494 didn't answer"), reg.OFFLINE_UNREACHABLE),
        (lambda: NodeTimeoutError("http://nuc:9494 didn't answer in time"), reg.OFFLINE_STUCK),
        (_stopping_info, reg.OFFLINE_RESTARTING),
    ],
    ids=["unreachable", "stuck", "restarting"],
)
def test_an_offline_node_says_why_so_only_a_node_that_didnt_answer_is_sent_to_its_address(h, answer, cause):
    """Restarting, stuck and asking to wait all read "isn't answering" with a "Check the address" button."""
    h.client().default_info = answer()
    assert h.registry.check_now("n1").cause == cause
    h.client().default_info = _info()
    assert (h.registry.check_now("n1").state, h.registry.view("n1").cause) == (reg.STATE_READY, "")


def test_a_node_asking_to_wait_and_something_that_isnt_a_node_say_why_they_are_offline(h):
    from media_preview_generator.nodes.client import NodeRateLimitedError

    h.client().default_info = NodeRateLimitedError("http://nuc:9494 asks this app to wait")
    assert h.registry.check_now("n1").cause == reg.OFFLINE_RATE_LIMITED
    h.client().default_info = NodeProtocolError("not a node")
    h.legacy = ("unreachable", "")
    assert h.registry.check_now("n1").cause == reg.OFFLINE_NOT_A_NODE


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_stopping_info, "node restarting"),
        (lambda: NodeRateLimitedError("http://nuc:9494 asks this app to wait"), "node asks to wait"),
        (lambda: NodeUnreachableError("http://nuc:9494 didn't answer"), "node offline"),
        (lambda: NodeTimeoutError("http://nuc:9494 didn't answer in time"), "node offline"),
    ],
    ids=["restarting", "asks_to_wait", "unreachable", "stuck"],
)
def test_the_short_reason_a_node_gets_no_work_says_when_it_is_only_restarting_or_asks_to_wait(h, answer, reason):
    """The wait banner and the Workers page's "0 now" chips said "node offline" for both, while Setup Health and the
    bell said "rst is restarting" / "rl asks this app to wait": down in one place, nothing to do in the next."""
    h.client().default_info = answer()
    h.registry.check_now("n1")
    assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, reason)


def test_the_workers_page_says_the_same_words_for_each_cause():
    """The editor's chips judge from the node list (app.js NODE_STATES), the saved member rows from capacity()."""
    js = (Path(reg.__file__).resolve().parents[1] / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    words = {}
    for state in reg._CAUSE_STATES:
        causes = re.search(r"\n    " + state + r": \{[^\n]*causes: \{([^}]*)\}", js).group(1)
        words.update(re.findall(r"(\w+): '([^']+)'", causes))
    assert words == reg._CAUSE_CAPACITY_REASONS


@pytest.mark.parametrize(
    ("node_version", "cause", "reason"),
    [("9.9.10", reg.UPDATE_APP_BEHIND, "app needs an update"), ("9.9.8", "", "node needs an update")],
    ids=["node-ahead", "node-behind"],
)
def test_a_worker_node_ahead_of_this_app_says_the_app_needs_the_update(h, node_version, cause, reason):
    """Its reason and Setup Health said "Update this app", while its chips said "node needs an update"."""
    h.client().default_info = _info(version=node_version)
    view = h.registry.check_now("n1")
    assert (view.state, view.cause) == (reg.STATE_UPDATE_NEEDED, cause)
    assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, reason)


def test_a_turned_off_node_keeps_no_cause_from_its_last_check(h):
    h.client().default_info = _info(version="9.9.10")
    h.registry.check_now("n1")
    h.change(enabled=False)
    view = next(v for v in h.registry.views() if v.id == "n1")
    assert (view.state, view.cause) == (reg.STATE_DISABLED, "")
    assert h.registry.capacity("n1").reason == "node turned off"


@pytest.mark.parametrize(
    ("last", "cause"),
    [("wait", reg.OFFLINE_RATE_LIMITED), ("refused", reg.OFFLINE_UNREACHABLE)],
    ids=["wait", "refused"],
)
def test_an_offline_node_is_read_by_its_last_attempt_not_by_one_wait_among_them(h, last, cause):
    # One 429 among the attempts was reported as the cause though the node then refused connections: "something at
    # this app's address has an old key" sent the user after the wrong fix.
    from media_preview_generator.nodes.client import NodeRateLimitedError

    wait = NodeRateLimitedError("http://nuc:9494 asks this app to wait")
    refused = NodeUnreachableError("http://nuc:9494 didn't answer")
    attempts = [wait, refused] * reg.CHECK_ATTEMPTS
    h.client().infos = attempts[: reg.CHECK_ATTEMPTS - 1] + [wait if last == "wait" else refused]

    assert h.registry.check_now("n1").cause == cause


def test_one_wait_then_an_answer_is_ready_not_offline(h):
    """Something sharing the address keeps it at the edge of the limit: a check held back once must not flap."""
    from media_preview_generator.nodes.client import NodeRateLimitedError

    h.client().infos = [NodeRateLimitedError("http://nuc:9494 asks this app to wait")]

    assert h.registry.check_now("n1").state == reg.STATE_READY


class TestTwinOfAPlexDatabaseNode:
    """A Plex database node answered by the machine of another saved node must not take that machine's Plex writes.

    Node B (``n2``, saved id ``uid-b``) serves a Plex; machine A (``uid-a``, beside another Plex) is saved as ``n1``.
    When A answers at B's address the check reads "added twice", and with either Plex's machine id unknown nothing
    proves A's Plex is B's.
    """

    def _harness(self):
        nodes = [
            _record(id="n1", name="nuc-a", url="http://a:9494", node_uid="uid-a"),
            _record(id="n2", name="nuc-b", url="http://b:9494", node_uid="uid-b"),
        ]
        harness = Harness(nodes=nodes)
        harness.registry._served_plex_ids = lambda node_id: [""] if node_id == "n2" else []
        return harness

    def _answer(self, h, node_id, uid):
        info = _info(uid=uid, roles=(ROLE_PLEX_DB,))
        info["plex_db"] = {"machine_identifier": ""}
        h.client(node_id).default_info = info
        h.registry.check_now(node_id)

    def test_its_plex_database_work_is_refused_with_a_plain_reason(self):
        h = self._harness()
        self._answer(h, "n2", "uid-a")

        assert h.registry.view("n2").state == reg.STATE_DUPLICATE
        refusal = h.registry.plex_db_refusal("n2")
        assert "nuc-b is nuc-a again" in refusal and "Plex's database" in refusal
        assert h.uids == []
        h.registry.stop()

    def test_renaming_the_first_of_the_two_shows_in_the_seconds_reason(self):
        h = self._harness()
        self._answer(h, "n2", "uid-a")

        h.nodes[0] = {**h.nodes[0], "name": "nuc-a2"}
        h.revision += 1

        assert h.registry.view("n2").reason.startswith("nuc-b is nuc-a2 again, added at another address.")
        h.registry.stop()

    def test_a_twin_check_holds_the_other_machines_id_instead_of_clearing_the_hold(self):
        h = self._harness()
        h.nodes = [h.nodes[1]]  # B alone first: A answers there with a new id, held back
        h.revision += 1
        self._answer(h, "n2", "uid-a")
        assert "Type the node's key again" in h.registry.plex_db_refusal("n2")

        h.nodes = [_record(id="n1", name="nuc-a", url="http://a:9494", node_uid="uid-a"), h.nodes[0]]
        h.revision += 1
        self._answer(h, "n2", "uid-a")  # now a twin of n1

        # Still held once n1 is removed, until a check of B alone decides.
        assert h.registry._nodes["n2"].unconfirmed_uid == "uid-a"
        h.registry.stop()

    def test_a_twin_stays_held_after_a_mapping_edit_puts_it_back_to_checking(self):
        # A restored settings file can hold two records with one id. The twin's check kept that id as the one it
        # heard, so a mapping edit (the node back to "checking") let its Plex database work through to the other
        # machine.
        h = self._harness()
        h.nodes[1] = {**h.nodes[1], "node_uid": "uid-a"}
        h.revision += 1
        self._answer(h, "n2", "uid-a")
        assert h.registry.view("n2").state == reg.STATE_DUPLICATE

        h.nodes[1] = {**h.nodes[1], "path_mappings": [{"app_prefix": "/media", "node_prefix": "/mnt"}]}
        h.revision += 1

        assert h.registry.view("n2").state == reg.STATE_CONNECTING
        assert h.registry.plex_db_hold("n2") is not None
        h.registry.stop()

    def test_a_new_id_held_back_is_never_taken_for_the_one_heard(self):
        h = self._harness()
        self._answer(h, "n2", "uid-new")

        assert h.registry._nodes["n2"].unconfirmed_uid == "uid-new"
        assert h.registry._nodes["n2"].heard_uid is None
        h.registry.stop()


def _plex_using(server_id: str, identity: str, *, enabled: bool = True, node_on: bool = True) -> dict:
    return {
        "id": server_id,
        "type": "plex",
        "name": server_id,
        "enabled": enabled,
        "server_identity": identity,
        "markers": {"enabled": True, "plex": {"agent": {"enabled": node_on, "node_id": "n1"}}},
    }


class TestServersThatWriteThroughANode:
    """Which Plex servers ``_default_served_plex_ids`` says write through a node (their new-id check)."""

    @pytest.fixture
    def sm(self, tmp_path):
        from media_preview_generator.web import settings_manager as sm_mod
        from media_preview_generator.web.settings_manager import SettingsManager

        manager = SettingsManager(config_dir=str(tmp_path))
        sm_mod._settings_manager = manager
        yield manager
        sm_mod.reset_settings_manager()

    @pytest.mark.parametrize(
        "other",
        [
            _plex_using("old", "plex-b", enabled=False),
            _plex_using("old", "", enabled=False),
            _plex_using("old", "plex-b", node_on=False),
        ],
        ids=["server-switched-off", "server-switched-off-never-connected", "its-node-switched-off"],
    )
    def test_a_server_that_writes_nothing_through_the_node_doesnt_count(self, sm, other):
        """Counted, it turned a clean "same Plex" answer into "two Plexes": the node's new id was held and its Plex
        database work refused until the key was typed again."""
        sm.apply_changes(updates={"media_servers": [_plex_using("home", "plex-a"), other]})

        assert reg._default_served_plex_ids("n1") == ["plex-a"]

        h = Harness(nodes=[_record(node_uid="uid-1")])
        h.registry._served_plex_ids = reg._default_served_plex_ids
        info = _info(uid="uid-2", roles=(ROLE_PLEX_DB,))
        info["plex_db"] = {"machine_identifier": "plex-a"}
        h.client().default_info = info
        h.registry.check_now("n1")
        assert h.uids == [("n1", "uid-2")] and h.registry.plex_db_refusal("n1") == ""
        h.registry.stop()


class TestRecordedNodeId:
    """What ``_default_record_uid`` writes to settings.json."""

    @pytest.fixture
    def sm(self, tmp_path):
        from media_preview_generator.web import settings_manager as sm_mod
        from media_preview_generator.web.settings_manager import SettingsManager

        manager = SettingsManager(config_dir=str(tmp_path))
        sm_mod._settings_manager = manager
        manager.update_nodes([{**_record(), "added_at": "2026-10-15T00:00:00+00:00"}])
        yield manager
        sm_mod.reset_settings_manager()

    def test_a_new_id_replaces_the_one_on_record(self, sm):
        reg._default_record_uid("n1", "uid-first", "http://nuc:9494")
        reg._default_record_uid("n1", "uid-reinstalled", "http://nuc:9494")
        assert sm.nodes[0]["node_uid"] == "uid-reinstalled"

    def test_the_id_is_recorded_on_a_record_whose_name_breaks_the_rules(self, sm):
        """A record "invalid as saved" (a v24 name made from a Plex server's) refused every check's write of the id,
        so a Plex database node never got the saved id its requests name."""
        sm.apply_changes(updates={"nodes": [{**sm.nodes[0], "name": "Plex helper (Casa ‍)"}]})

        reg._default_record_uid("n1", "uid-first", "http://nuc:9494")

        assert sm.nodes[0]["node_uid"] == "uid-first"

    def test_an_id_learnt_at_an_address_the_node_no_longer_has_is_not_recorded(self, sm):
        reg._default_record_uid("n1", "uid-old-machine", "http://old-nuc:9494")
        assert sm.nodes[0]["node_uid"] is None

    def test_a_check_of_the_old_address_finishing_after_an_address_change_records_nothing(self, sm):
        """The PUT saves the new address before the registry syncs, so the check's ``live.client`` guard can't see
        the change: the address the id came from is what keeps the old machine's id off the new address."""
        release, asked = threading.Event(), threading.Event()

        class SlowOldMachine(FakeClient):
            def info(self):
                asked.set()
                release.wait(5)
                return _info(uid="old-machine")

        registry = reg.NodeRegistry(client_factory=SlowOldMachine, app_version=lambda: APP_VERSION, attempt_gap_s=0.0)
        registry.views()
        check = threading.Thread(target=registry._run_check, args=("n1",))
        check.start()
        assert asked.wait(5)
        with sm.locked():
            sm.update_nodes([{**sm.nodes[0], "url": "http://new-nuc:9494", "node_uid": None}])
        release.set()
        check.join(5)

        assert (sm.nodes[0]["url"], sm.nodes[0]["node_uid"]) == ("http://new-nuc:9494", None)


class TestAgainstARealNode:
    """The registry's check against the node's own server."""

    KEY = "registry-real-node-key-0123456789"

    @pytest.fixture
    def node_url(self, tmp_path, monkeypatch):
        from werkzeug.serving import make_server

        from media_preview_generator.nodes.server import app as node_app
        from media_preview_generator.nodes.server import info

        for name in ("NODE_KEY", "AGENT_TOKEN", "NODE_ROLES"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "node-tmp"))
        monkeypatch.setattr(info, "_ffmpeg", lambda: {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 7.1"})
        app = node_app.create_node_app(config_dir=str(tmp_path / "config"), roles=["worker"], key=self.KEY, ops={})
        app.extensions[node_app.EXTENSION].worker_info._detect = lambda: []
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}"
        server.shutdown()
        thread.join(5)
        app.extensions[node_app.EXTENSION].close()

    def test_a_worker_node_on_another_node_protocol_needs_an_update(self, node_url):
        import requests

        from media_preview_generator import __version__
        from media_preview_generator.nodes import protocol
        from media_preview_generator.nodes.client import NodeAddress, NodeClient

        class NewerAppClient(NodeClient):
            """The app after a protocol bump (it asks for protocol 2); the node is still on protocol 1."""

            def _headers(self):
                return {"Authorization": f"Bearer {TestAgainstARealNode.KEY}", protocol.PROTOCOL_HEADER: "2"}

        record = _record(url=node_url, key=self.KEY, path_mappings=[])
        session = requests.Session()
        session.trust_env = False
        registry = reg.NodeRegistry(
            revision_provider=lambda: 1,
            nodes_provider=lambda: [record],
            record_uid=lambda *_: None,
            legacy_ping=lambda url, key: pytest.fail("a node is never asked the old helper's ping"),
            app_version=lambda: "0.0.1",
            attempt_gap_s=0.0,
        )
        outcome = registry._probe(record, NewerAppClient(NodeAddress(node_url, self.KEY), session=session))
        assert outcome.state == reg.STATE_UPDATE_NEEDED, outcome.reason
        assert __version__ in outcome.reason and "0.0.1" in outcome.reason


def test_a_saved_address_urllib3_refuses_reads_offline_not_connecting_forever():
    # Saved before the address rules refused it: requests prepares it, urllib3 refuses it before connecting.
    record = {"id": "n1", "name": "nas", "url": "http://nas..lan:9494", "key": "k", "enabled": True}
    registry = reg.NodeRegistry(
        nodes_provider=lambda: [record], revision_provider=lambda: 1, record_uid=lambda *a: None, attempt_gap_s=0.0
    )
    view = registry.check_now("n1")
    assert view is not None and view.state == reg.STATE_OFFLINE


def test_a_newly_typed_name_that_does_not_resolve_is_not_called_stopped(h):
    assert h.registry.check_now("n1").state == reg.STATE_READY
    h.change(url="http://nuc-typo:9494")
    h.client().default_info = NodeUnreachableError("http://nuc-typo:9494 can't be found", reason=UNREACHABLE_DNS)
    view = h.registry.check_now("n1")
    assert view.state == reg.STATE_OFFLINE
    assert "may be stopped or renamed" not in view.reason
    assert "Check the name" in view.reason


class TestCleanGpusKeepsTheVulkanCheck:
    def test_known_string_keys_survive_and_the_rest_is_dropped(self):
        from media_preview_generator.nodes.registry import VULKAN_TEXT_MAX, _clean_gpus

        raw = [
            {
                "type": "INTEL",
                "device": "/dev/dri/renderD128",
                "name": "UHD",
                "vulkan": {
                    "ok": False,
                    "cause": "kernel_too_old",
                    "title": "T",
                    "why": "W" * (VULKAN_TEXT_MAX + 50),
                    "fix": "line one\nline two",
                    "facts": {"params": {"x": 1}},
                    "evil": "<script>",
                },
            }
        ]

        (gpu,) = _clean_gpus(raw)

        assert set(gpu["vulkan"]) == {"ok", "cause", "title", "why", "fix"}
        assert len(gpu["vulkan"]["why"]) == VULKAN_TEXT_MAX
        assert "\n" not in gpu["vulkan"]["fix"]
        assert gpu["vulkan"]["ok"] is False

    @pytest.mark.parametrize("junk", [None, "text", 5, ["x"]])
    def test_a_vulkan_that_is_not_a_dict_is_left_out(self, junk):
        from media_preview_generator.nodes.registry import _clean_gpus

        (gpu,) = _clean_gpus([{"type": "INTEL", "vulkan": junk}])

        assert "vulkan" not in gpu

    def test_ok_must_be_literally_true(self):
        from media_preview_generator.nodes.registry import _clean_gpus

        (gpu,) = _clean_gpus([{"type": "INTEL", "vulkan": {"ok": "yes", "title": "x"}}])

        assert gpu["vulkan"]["ok"] is False
