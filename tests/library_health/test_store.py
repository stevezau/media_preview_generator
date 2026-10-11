"""Tests for library_health.store."""

import threading

import pytest

from media_preview_generator.library_health.models import (
    Cell,
    CellState,
    Feature,
    LibraryResult,
    ServerResult,
    TodoFile,
)
from media_preview_generator.library_health.store import HealthStore


def _result(server_id="s1", todo=None) -> ServerResult:
    lib = LibraryResult(
        "1",
        "Movies",
        "movie",
        10,
        {
            Feature.PREVIEWS: Cell(CellState.COUNTED, total=10, done=6, nothing_found=1, not_showing=2),
            Feature.LOUDNESS: Cell(CellState.OFF, reason="off"),
        },
    )
    return ServerResult(server_id, "Plex", "plex", [lib], todo or [], access="acc", checked_at=5.0, duration_s=1.5)


@pytest.fixture
def store(tmp_path):
    return HealthStore(str(tmp_path / "health.db"))


def test_replace_server_roundtrip(store):
    store.replace_server(_result(todo=[TodoFile(Feature.PREVIEWS, "/a", "A", "9", library_id="1")]))

    loaded = store.load()

    assert len(loaded) == 1
    assert loaded[0].todo == []
    assert loaded[0].access == "acc"
    cell = loaded[0].libraries[0].cells[Feature.PREVIEWS]
    assert (cell.total, cell.done, cell.nothing_found, cell.not_showing, cell.todo) == (10, 6, 1, 2, 3)
    assert loaded[0].libraries[0].cells[Feature.LOUDNESS].state == CellState.OFF


def test_to_dict_has_no_todo_but_cell_has(store):
    result = _result()
    assert "todo" not in result.to_dict()
    assert result.to_dict()["libraries"][0]["cells"]["previews"]["todo"] == 3


def test_replace_is_atomic_on_error(store, monkeypatch):
    store.replace_server(_result(todo=[TodoFile(Feature.PREVIEWS, "/old", "Old", library_id="1")]))
    bad = _result(todo=[TodoFile(Feature.PREVIEWS, "/new", "New", library_id="1")])
    # an unencodable path makes the insert fail after the deletes ran
    object.__setattr__(bad.todo[0], "path", object())

    with pytest.raises(Exception):  # noqa: B017, PT011
        store.replace_server(bad)

    assert len(store.load()) == 1
    assert store.list_todo("s1", "1", Feature.PREVIEWS)[1][0].path == "/old"


def test_list_todo_filter_and_paging(store):
    rows = [TodoFile(Feature.PREVIEWS, f"/m/{i:03d}.mkv", f"T{i}", library_id="1") for i in range(250)]
    rows.append(TodoFile(Feature.PREVIEWS, "/m/Special_100%.mkv", "S", library_id="1"))
    store.replace_server(_result(todo=rows))

    total, page = store.list_todo("s1", "1", Feature.PREVIEWS, offset=100, limit=50)
    assert (total, len(page), page[0].path) == (251, 50, "/m/100.mkv")

    assert store.list_todo("s1", "1", Feature.PREVIEWS, q="SPECIAL_100%")[0] == 1
    assert store.list_todo("s1", "1", Feature.PREVIEWS, q="/m/02")[0] == 10
    assert len(store.list_todo("s1", "1", Feature.PREVIEWS, limit=9999)[1]) == 251
    assert len(store.list_todo("s1", "1", Feature.PREVIEWS, limit=0)[1]) == 1


def test_list_todo_limit_clamped_to_500(store):
    rows = [TodoFile(Feature.PREVIEWS, f"/m/{i:04d}", "t", library_id="1") for i in range(600)]
    store.replace_server(_result(todo=rows))

    assert len(store.list_todo("s1", "1", Feature.PREVIEWS, limit=9999)[1]) == 500


def test_list_todo_separates_not_showing(store):
    store.replace_server(
        _result(
            todo=[
                TodoFile(Feature.PREVIEWS, "/a", "A", "1", library_id="1"),
                TodoFile(Feature.PREVIEWS, "/b", "B", "2", not_showing=True, library_id="1"),
            ]
        )
    )

    assert [t.path for t in store.list_todo("s1", "1", Feature.PREVIEWS)[1]] == ["/a"]
    assert [t.path for t in store.list_todo("s1", "1", Feature.PREVIEWS, not_showing=True)[1]] == ["/b"]


def test_reread_items_distinct(store):
    store.replace_server(
        _result(
            todo=[
                TodoFile(Feature.PREVIEWS, "/a-part1", "A", "42", not_showing=True, library_id="1"),
                TodoFile(Feature.PREVIEWS, "/a-part2", "A", "42", not_showing=True, library_id="1"),
                TodoFile(Feature.PREVIEWS, "/c", "C", "43", library_id="1"),
            ]
        )
    )

    assert store.reread_items("s1") == [("42", "1")]


def test_remove_missing_servers(store):
    store.replace_server(_result("s1", todo=[TodoFile(Feature.PREVIEWS, "/a", "A", library_id="1")]))
    store.replace_server(_result("s2"))

    store.remove_missing_servers({"s2"})

    assert [r.server_id for r in store.load()] == ["s2"]
    assert store.list_todo("s1", "1", Feature.PREVIEWS)[0] == 0


def test_concurrent_use_from_threads(store):
    errors = []

    def work(n):
        try:
            for _ in range(20):
                store.replace_server(_result(f"s{n}", todo=[TodoFile(Feature.PREVIEWS, "/a", "A", library_id="1")]))
                store.load()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(store.load()) == 4


def test_default_store_is_created_once_at_config_dir(tmp_path, monkeypatch):
    from media_preview_generator.library_health import store as store_module

    monkeypatch.setattr(store_module, "_default", None)
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))

    first = store_module.default_store()
    assert store_module.default_store() is first
    assert (tmp_path / "library_health.db").exists()
    monkeypatch.setattr(store_module, "_default", None)


def test_remove_not_showing_lowers_the_stored_count(store):
    rows = [TodoFile(Feature.PREVIEWS, f"/{n}", str(n), str(n), not_showing=True, library_id="1") for n in ("11", "12")]
    store.replace_server(_result(todo=rows))

    store.remove_not_showing("s1", ["11"])

    assert store.load()[0].libraries[0].cells[Feature.PREVIEWS].not_showing == 1
    assert store.reread_items("s1") == [("12", "1")]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM todo WHERE server_id = ? AND not_showing = 1 AND item_id = ?",
        "SELECT DISTINCT item_id, path FROM todo WHERE server_id = ? AND not_showing = 1 AND item_id != ''",
    ],
)
def test_not_showing_lookups_use_an_index(store, sql):
    """Review & fix clears and lists rows by item; a full scan per item froze sflix's store for minutes."""
    params = ("s1", "1")[: sql.count("?")]
    plan = " ".join(row[-1] for row in store._conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))
    assert "idx_todo_not_showing" in plan, plan
