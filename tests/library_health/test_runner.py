"""Tests for library_health.runner."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.library_health import runner as runner_module
from media_preview_generator.library_health.models import (
    Cell,
    CellState,
    CheckCancelled,
    Feature,
    LibraryResult,
    ServerResult,
    TodoFile,
)
from media_preview_generator.library_health.runner import REREAD_INTERVAL_S, HealthRunner
from media_preview_generator.library_health.store import HealthStore
from media_preview_generator.servers.base import ServerType


def _cfg(server_id, server_type=ServerType.PLEX, enabled=True):
    return SimpleNamespace(id=server_id, type=server_type, name=f"name-{server_id}", enabled=enabled)


class FakeRegistry:
    def __init__(self, entries):
        self._cfgs = [cfg for cfg, _ in entries]
        self._servers = {cfg.id: server for cfg, server in entries}

    def configs(self):
        return list(self._cfgs)

    def get(self, server_id):
        return self._servers.get(server_id)


def _result(server_id, marker="new"):
    lib = LibraryResult("1", marker, "movie", 1, {Feature.PREVIEWS: Cell(CellState.COUNTED, total=1)})
    return ServerResult(server_id, f"name-{server_id}", "plex", [lib], [])


def _not_showing(item_id):
    return TodoFile(Feature.PREVIEWS, f"/m/{item_id}", item_id, item_id, True, "1")


@pytest.fixture
def store(tmp_path):
    return HealthStore(str(tmp_path / "health.db"))


@pytest.fixture
def plex_server():
    server = MagicMock()
    return server


def _make_runner(store, registry, sleep=lambda _s: None):
    return HealthRunner(store, registry_factory=lambda: registry, markers_db_path=lambda: "/nope", sleep=sleep)


@pytest.fixture(autouse=True)
def _no_markers_db(monkeypatch):
    monkeypatch.setattr(runner_module, "nothing_found", lambda _path: {})


def test_single_flight(store, monkeypatch, plex_server):
    release = threading.Event()
    calls = []

    def fake_count(cfg, server, **_kw):
        calls.append(cfg.id)
        release.wait(5)
        return _result(cfg.id)

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))

    started, first = runner.start_check()
    second_started, second = runner.start_check()
    release.set()
    runner.wait(5)

    assert started is True
    assert second_started is False
    assert second.kind == "check"
    assert calls == ["a"]
    assert runner.progress() is None


def test_failed_server_keeps_previous_rows(store, monkeypatch, plex_server):
    store.replace_server(_result("a", "old-a"))
    store.replace_server(_result("b", "old-b"))

    def fake_count(cfg, server, **_kw):
        if cfg.id == "b":
            raise RuntimeError("boom")
        return _result(cfg.id, "new-a")

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server), (_cfg("b"), plex_server)]))

    runner.start_check()
    runner.wait(5)

    names = {r.server_id: r.libraries[0].name for r in store.load()}
    assert names == {"a": "new-a", "b": "old-b"}
    assert "boom" in runner.server_error("b")
    assert runner.server_error("a") == ""


def test_server_error_cleared_when_next_check_succeeds(store, monkeypatch, plex_server):
    fail = {"on": True}

    def fake_count(cfg, server, **_kw):
        if fail["on"]:
            raise RuntimeError("boom")
        return _result(cfg.id)

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    runner.wait(5)
    assert runner.server_error("a")

    fail["on"] = False
    runner.start_check()
    runner.wait(5)

    assert runner.server_error("a") == ""


def test_disabled_server_skipped_and_removed_servers_dropped(store, monkeypatch, plex_server):
    store.replace_server(_result("gone"))
    store.replace_server(_result("off"))
    seen = []

    def fake_count(cfg, server, **_kw):
        seen.append(cfg.id)
        return _result(cfg.id)

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server), (_cfg("off", enabled=False), plex_server)]))

    runner.start_check()
    runner.wait(5)

    assert seen == ["a"]
    assert [r.server_id for r in store.load()] == ["a"]


@pytest.mark.parametrize(
    ("server_type", "plex_calls", "embyish_calls"),
    [(ServerType.PLEX, 1, 0), (ServerType.EMBY, 0, 1), (ServerType.JELLYFIN, 0, 1)],
)
def test_counter_chosen_by_server_type(store, monkeypatch, plex_server, server_type, plex_calls, embyish_calls):
    plex_mock = MagicMock(return_value=_result("a"))
    embyish_mock = MagicMock(return_value=_result("a"))
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", plex_mock)
    monkeypatch.setattr(runner_module.embyish_counter, "count_embyish", embyish_mock)
    runner = _make_runner(store, FakeRegistry([(_cfg("a", server_type), plex_server)]))

    runner.start_check()
    runner.wait(5)

    assert plex_mock.call_count == plex_calls
    assert embyish_mock.call_count == embyish_calls


def test_cancel_stops_and_clears_progress(store, monkeypatch, plex_server):
    in_counter = threading.Event()
    counted = []

    def fake_count(cfg, server, *, cancel_check, **_kw):
        counted.append(cfg.id)
        in_counter.set()
        while not cancel_check():
            threading.Event().wait(0.01)
        raise CheckCancelled

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    store.replace_server(_result("a", "old-a"))
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server), (_cfg("b"), plex_server)]))
    runner.start_check()
    assert in_counter.wait(5)

    assert runner.cancel() is True
    runner.wait(5)

    assert counted == ["a"]
    assert runner.progress() is None
    assert runner.cancel() is False
    assert [r.libraries[0].name for r in store.load()] == ["old-a"]
    assert runner.last_error() == ""


def test_progress_is_a_snapshot(store, monkeypatch, plex_server):
    release = threading.Event()
    in_counter = threading.Event()

    def fake_count(cfg, server, *, progress, **_kw):
        progress("Checking markers", 3, 10)
        in_counter.set()
        release.wait(5)
        return _result(cfg.id)

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    assert in_counter.wait(5)

    snap = runner.progress()
    snap.done = 99
    live = runner.progress()
    release.set()
    runner.wait(5)

    assert (live.step, live.done, live.total, live.server_name) == ("Checking markers", 3, 10, "name-a")


def test_registry_failure_sets_last_error(store):
    runner = HealthRunner(store, registry_factory=lambda: None, markers_db_path=lambda: "/nope")

    runner.start_check()
    runner.wait(5)

    assert runner.last_error() == "Could not read your media servers"
    assert runner.progress() is None


def test_unexpected_exception_sets_last_error_and_clears_current(store, monkeypatch, plex_server):
    monkeypatch.setattr(runner_module, "nothing_found", MagicMock(side_effect=RuntimeError("db gone")))
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))

    runner.start_check()
    runner.wait(5)

    assert "db gone" in runner.last_error()
    assert runner.progress() is None
    assert runner.start_check()[0] is True
    runner.wait(5)


def test_reread_dedups_paces_and_rechecks(store, monkeypatch):
    store.replace_server(
        ServerResult("p", "Plex", "plex", [], [_not_showing("11"), _not_showing("12"), _not_showing("11")])
    )
    plex_api = MagicMock()
    server = MagicMock()
    server._connect.return_value = plex_api
    sleeps = []
    count_mock = MagicMock(return_value=_result("p"))
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", count_mock)
    runner = _make_runner(store, FakeRegistry([(_cfg("p"), server)]), sleep=sleeps.append)

    started, progress = runner.start_reread("p")
    runner.wait(5)

    assert started is True
    assert progress.kind == "reread"
    paths = [c.args[0] for c in plex_api.query.call_args_list]
    assert paths == ["/library/metadata/11/analyze", "/library/metadata/12/analyze"]
    for call in plex_api.query.call_args_list:
        assert call.kwargs["method"] is plex_api._session.put
    assert sleeps == [REREAD_INTERVAL_S]
    assert REREAD_INTERVAL_S == 0.5
    assert count_mock.call_count == 1
    assert count_mock.call_args.args[0].id == "p"
    assert runner.progress() is None


def test_reread_failure_does_not_stop_the_rest(store, monkeypatch):
    store.replace_server(ServerResult("p", "Plex", "plex", [], [_not_showing("11"), _not_showing("12")]))
    plex_api = MagicMock()
    plex_api.query.side_effect = [RuntimeError("500"), None]
    server = MagicMock()
    server._connect.return_value = plex_api
    count_mock = MagicMock(return_value=_result("p"))
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", count_mock)
    runner = _make_runner(store, FakeRegistry([(_cfg("p"), server)]))

    runner.start_reread("p")
    runner.wait(5)

    assert plex_api.query.call_count == 2
    assert count_mock.call_count == 1


def test_reread_skips_non_numeric_ids(store, monkeypatch):
    store.replace_server(ServerResult("p", "Plex", "plex", [], [_not_showing("11"), _not_showing("../x")]))
    plex_api = MagicMock()
    server = MagicMock()
    server._connect.return_value = plex_api
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", MagicMock(return_value=_result("p")))
    runner = _make_runner(store, FakeRegistry([(_cfg("p"), server)]))

    runner.start_reread("p")
    runner.wait(5)

    assert [c.args[0] for c in plex_api.query.call_args_list] == ["/library/metadata/11/analyze"]


def test_reread_cancel_stops_showing_sent_items_as_not_showing(store, monkeypatch):
    store.replace_server(ServerResult("p", "Plex", "plex", [], [_not_showing("11"), _not_showing("12")]))
    holder = {}
    runner = _make_runner(store, FakeRegistry([(_cfg("p"), MagicMock())]), sleep=lambda _s: holder["runner"].cancel())
    holder["runner"] = runner

    runner.start_reread("p")
    runner.wait(5)

    assert store.reread_items("p") == [("12", "1")]


def test_reread_cancel_skips_recheck(store, monkeypatch):
    store.replace_server(ServerResult("p", "Plex", "plex", [], [_not_showing("11"), _not_showing("12")]))
    server = MagicMock()
    count_mock = MagicMock(return_value=_result("p"))
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", count_mock)
    holder = {}

    def sleep_then_cancel(_s):
        holder["runner"].cancel()

    runner = _make_runner(store, FakeRegistry([(_cfg("p"), server)]), sleep=sleep_then_cancel)
    holder["runner"] = runner

    runner.start_reread("p")
    runner.wait(5)

    assert server._connect.return_value.query.call_count == 1
    assert count_mock.call_count == 0
    assert runner.progress() is None


def test_reread_rejected_while_check_runs(store, monkeypatch, plex_server):
    release = threading.Event()
    in_counter = threading.Event()

    def fake_count(cfg, server, **_kw):
        in_counter.set()
        release.wait(5)
        return _result(cfg.id)

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    store.replace_server(ServerResult("a", "Plex", "plex", [], [_not_showing("11")]))
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    assert in_counter.wait(5)

    started, refusal = runner.start_reread("a")
    release.set()
    runner.wait(5)

    assert started is False
    assert (refusal.message, refusal.status) == ("A check or re-read is already running", 409)
    plex_server._connect.assert_not_called()


@pytest.mark.parametrize("server_type", [ServerType.EMBY, ServerType.JELLYFIN])
def test_reread_rejects_non_plex_server(store, server_type):
    runner = _make_runner(store, FakeRegistry([(_cfg("e", server_type), MagicMock())]))

    started, refusal = runner.start_reread("e")

    assert started is False
    assert "Plex" in refusal.message
    assert refusal.status == 400
    assert runner.progress() is None


def test_reread_rejects_unknown_server_and_empty_list(store, plex_server):
    runner = _make_runner(store, FakeRegistry([(_cfg("p"), plex_server)]))

    started, missing = runner.start_reread("missing")
    assert started is False
    assert missing.status == 404
    started, refusal = runner.start_reread("p")

    assert started is False
    assert (refusal.message, refusal.status) == ("Nothing to re-read", 400)
    assert runner.progress() is None


def test_reread_rejected_when_registry_unreadable(store):
    runner = HealthRunner(store, registry_factory=lambda: None, markers_db_path=lambda: "/nope")

    started, refusal = runner.start_reread("p")

    assert started is False
    assert (refusal.message, refusal.status) == ("Could not read your media servers", 400)


def test_nightly_check_never_raises(monkeypatch):
    fake = MagicMock()
    fake.start_check.side_effect = RuntimeError("nope")
    monkeypatch.setattr(runner_module, "get_runner", lambda: fake)

    runner_module.nightly_check()

    fake.start_check.assert_called_once_with("nightly")


def test_limited_check_keeps_other_enabled_servers_rows(store, monkeypatch):
    store.replace_server(ServerResult("p", "Plex", "plex", [], [_not_showing("11")]))
    store.replace_server(_result("other", "old-other"))
    store.replace_server(_result("off"))
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", MagicMock(return_value=_result("p")))
    registry = FakeRegistry(
        [(_cfg("p"), MagicMock()), (_cfg("other"), MagicMock()), (_cfg("off", enabled=False), MagicMock())]
    )
    runner = _make_runner(store, registry)

    runner.start_reread("p")
    runner.wait(5)

    assert sorted(r.server_id for r in store.load()) == ["other", "p"]


def _good_two_libraries():
    libs = [
        LibraryResult(
            "1", "Movies", "movie", 10, {Feature.PREVIEWS: Cell(CellState.COUNTED, total=10, done=7, not_showing=2)}
        ),
        LibraryResult("2", "Shows", "show", 5, {Feature.PREVIEWS: Cell(CellState.COUNTED, total=5, done=5)}),
    ]
    todo = [
        TodoFile(Feature.PREVIEWS, "/m/todo1", "todo1", "11", False, "1"),
        TodoFile(Feature.PREVIEWS, "/m/ns1", "ns1", "12", True, "1"),
        TodoFile(Feature.PREVIEWS, "/m/ns2", "ns2", "13", True, "1"),
    ]
    return ServerResult("a", "name-a", "plex", libs, todo)


def _failing_library(library_id, name, message="db locked"):
    cell = Cell(CellState.ERROR, reason=message)
    return LibraryResult(library_id, name, "movie", 0, {Feature.PREVIEWS: cell})


def test_whole_server_failure_keeps_old_numbers_and_todo_rows(store, monkeypatch, plex_server):
    store.replace_server(_good_two_libraries())

    def fake_count(cfg, server, **_kw):
        libs = [_failing_library("1", "Movies"), _failing_library("2", "Shows")]
        return ServerResult(cfg.id, "name-a", "plex", libs, [])

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    runner.wait(5)

    movies = store.load()[0].libraries[0].cells[Feature.PREVIEWS]
    assert (movies.state, movies.total, movies.done, movies.not_showing, movies.reason) == (
        CellState.ERROR,
        10,
        7,
        2,
        "db locked",
    )
    assert movies.todo == 3
    assert store.list_todo("a", "1", Feature.PREVIEWS)[0] == 1
    assert store.list_todo("a", "1", Feature.PREVIEWS, not_showing=True)[0] == 2
    assert store.reread_items("a") == [("12", "1"), ("13", "1")]
    assert runner.server_error("a") == "db locked"


def test_only_the_failing_library_carries_forward(store, monkeypatch, plex_server):
    store.replace_server(_good_two_libraries())

    def fake_count(cfg, server, **_kw):
        fresh = LibraryResult("2", "Shows", "show", 6, {Feature.PREVIEWS: Cell(CellState.COUNTED, total=6, done=1)})
        todo = [TodoFile(Feature.PREVIEWS, "/s/new", "new", "21", False, "2")]
        return ServerResult(cfg.id, "name-a", "plex", [_failing_library("1", "Movies"), fresh], todo)

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    runner.wait(5)

    movies, shows = store.load()[0].libraries
    assert (movies.cells[Feature.PREVIEWS].state, movies.cells[Feature.PREVIEWS].total) == (CellState.ERROR, 10)
    assert (shows.cells[Feature.PREVIEWS].state, shows.cells[Feature.PREVIEWS].total) == (CellState.COUNTED, 6)
    assert store.list_todo("a", "2", Feature.PREVIEWS)[0] == 1
    assert store.list_todo("a", "1", Feature.PREVIEWS)[0] == 1
    assert runner.server_error("a") == ""


def test_error_without_previous_numbers_stays_zero(store, monkeypatch, plex_server):
    def fake_count(cfg, server, **_kw):
        return ServerResult(cfg.id, "name-a", "plex", [_failing_library("1", "Movies")], [])

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    runner.wait(5)

    cell = store.load()[0].libraries[0].cells[Feature.PREVIEWS]
    assert (cell.state, cell.total) == (CellState.ERROR, 0)
    assert runner.failed_servers() == [("a", "name-a", "plex", "db locked")]


def test_check_sets_checked_at_and_duration(store, monkeypatch, plex_server):
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", lambda cfg, server, **_kw: _result(cfg.id))
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    runner.wait(5)

    stored = store.load()[0]
    assert stored.checked_at > 0
    assert stored.duration_s >= 0


def test_removed_server_leaves_no_failure_behind(store, monkeypatch, plex_server):
    fail = {"on": True}
    monkeypatch.setattr(
        runner_module.plex_counter,
        "count_plex",
        lambda cfg, server, **_kw: (_ for _ in ()).throw(RuntimeError("boom")) if fail["on"] else _result(cfg.id),
    )
    registry = FakeRegistry([(_cfg("a"), plex_server), (_cfg("b"), plex_server)])
    runner = _make_runner(store, registry)
    runner.start_check()
    runner.wait(5)
    assert {entry[0] for entry in runner.failed_servers()} == {"a", "b"}

    registry._cfgs = [cfg for cfg in registry._cfgs if cfg.id != "b"]
    runner.start_check()
    runner.wait(5)

    assert [entry[0] for entry in runner.failed_servers()] == ["a"]


def test_disabled_server_leaves_no_failure_behind(store, monkeypatch, plex_server):
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", MagicMock(side_effect=RuntimeError("boom")))
    registry = FakeRegistry([(_cfg("a"), plex_server)])
    runner = _make_runner(store, registry)
    runner.start_check()
    runner.wait(5)
    assert runner.failed_servers()

    registry._cfgs = [_cfg("a", enabled=False)]
    runner.start_check()
    runner.wait(5)

    assert runner.failed_servers() == []


def test_error_stays_visible_while_the_next_check_runs(store, monkeypatch, plex_server):
    in_counter, release = threading.Event(), threading.Event()
    state = {"fail": True}

    def fake_count(cfg, server, **_kw):
        if state["fail"]:
            raise RuntimeError("boom")
        in_counter.set()
        release.wait(5)
        return _result(cfg.id)

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    runner.wait(5)
    assert runner.server_error("a") == "boom"

    state["fail"] = False
    runner.start_check()
    assert in_counter.wait(5)
    assert runner.server_error("a") == "boom"
    release.set()
    runner.wait(5)

    assert runner.server_error("a") == ""


def test_cancelled_check_leaves_existing_errors(store, monkeypatch, plex_server):
    state = {"fail": True}
    holder = {}

    def fake_count(cfg, server, **_kw):
        if state["fail"]:
            raise RuntimeError("boom")
        holder["runner"].cancel()
        return _result(cfg.id)

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server), (_cfg("b"), plex_server)]))
    holder["runner"] = runner
    runner.start_check()
    runner.wait(5)
    assert {entry[0] for entry in runner.failed_servers()} == {"a", "b"}

    state["fail"] = False
    runner.start_check()
    runner.wait(5)

    # "a" finished and cleared; the cancel stopped the run before "b" was rechecked.
    assert runner.server_error("a") == ""
    assert runner.server_error("b") == "boom"


def test_failed_check_replaces_the_error_message(store, monkeypatch, plex_server):
    messages = iter(["first", "second"])

    def fake_count(cfg, server, **_kw):
        raise RuntimeError(next(messages))

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fake_count)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    for _ in range(2):
        runner.start_check()
        runner.wait(5)

    assert runner.server_error("a") == "second"


def _count_failing_everywhere(cfg, server, **_kw):
    libs = [_failing_library("1", "Movies"), _failing_library("2", "Shows")]
    return ServerResult(cfg.id, "name-a", "plex", libs, [])


def test_two_consecutive_failures_keep_the_carried_numbers_and_rows(store, monkeypatch, plex_server):
    store.replace_server(_good_two_libraries())
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", _count_failing_everywhere)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    for _ in range(2):
        runner.start_check()
        runner.wait(5)

    movies = store.load()[0].libraries[0].cells[Feature.PREVIEWS]
    assert (movies.state, movies.total, movies.done, movies.not_showing) == (CellState.ERROR, 10, 7, 2)
    assert store.list_todo("a", "1", Feature.PREVIEWS)[0] == 1
    assert store.reread_items("a") == [("12", "1"), ("13", "1")]


def test_success_after_a_carried_error_replaces_the_cell_and_drops_rows(store, monkeypatch, plex_server):
    store.replace_server(_good_two_libraries())
    monkeypatch.setattr(runner_module.plex_counter, "count_plex", _count_failing_everywhere)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    runner.wait(5)

    def fresh(cfg, server, **_kw):
        lib = LibraryResult("1", "Movies", "movie", 4, {Feature.PREVIEWS: Cell(CellState.COUNTED, total=4, done=4)})
        return ServerResult(cfg.id, "name-a", "plex", [lib], [])

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", fresh)
    runner.start_check()
    runner.wait(5)

    stored = store.load()[0]
    cell = stored.libraries[0].cells[Feature.PREVIEWS]
    assert (cell.state, cell.total, cell.done, cell.not_showing) == (CellState.COUNTED, 4, 4, 0)
    assert [lib.library_id for lib in stored.libraries] == ["1"]
    assert store.list_todo("a", "1", Feature.PREVIEWS)[0] == 0
    assert store.reread_items("a") == []
    assert runner.server_error("a") == ""


def test_library_removed_between_runs_is_not_carried(store, monkeypatch, plex_server):
    store.replace_server(_good_two_libraries())

    def only_library_one_fails(cfg, server, **_kw):
        return ServerResult(cfg.id, "name-a", "plex", [_failing_library("1", "Movies")], [])

    monkeypatch.setattr(runner_module.plex_counter, "count_plex", only_library_one_fails)
    runner = _make_runner(store, FakeRegistry([(_cfg("a"), plex_server)]))
    runner.start_check()
    runner.wait(5)

    stored = store.load()[0]
    assert [lib.library_id for lib in stored.libraries] == ["1"]
    assert store.list_todo("a", "2", Feature.PREVIEWS)[0] == 0
