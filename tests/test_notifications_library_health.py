"""Bell notifications from the last Library health check: previews Plex isn't showing, and a failed check."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.library_health.models import Cell, CellState, Feature, LibraryResult, ServerResult
from media_preview_generator.library_health.store import HealthStore
from media_preview_generator.web.notifications import (
    LIBRARY_HEALTH_CHECK_FAILED_ID,
    LIBRARY_HEALTH_NOT_SHOWING_ID,
    _build_library_health_check_failed_notification,
    _build_library_health_not_showing_notification,
    build_active_notifications,
    is_session_only_dismissal,
    reset_session,
)


def _server(server_id: str, server_type: str, *counts: int, state: CellState = CellState.COUNTED) -> ServerResult:
    libraries = [
        LibraryResult(
            str(i),
            f"Library {i}",
            "show",
            100,
            {Feature.PREVIEWS: Cell(state, total=100, done=100, not_showing=count)},
        )
        for i, count in enumerate(counts, 1)
    ]
    return ServerResult(server_id, server_id.title(), server_type, libraries, [])


class _Runner:
    def __init__(self, *, kind: str | None = None, last_error: str = "", failed: list | None = None) -> None:
        self._progress = SimpleNamespace(kind=kind) if kind else None
        self._last_error = last_error
        self._failed = failed or []

    def progress(self):
        return self._progress

    def last_error(self) -> str:
        return self._last_error

    def failed_servers(self) -> list:
        return self._failed


@pytest.fixture
def store(tmp_path):
    health = HealthStore(str(tmp_path / "library_health.db"))
    with patch("media_preview_generator.library_health.store.default_store", return_value=health):
        yield health


@pytest.fixture
def runner():
    current = {"runner": _Runner()}
    with patch("media_preview_generator.library_health.runner.get_runner", side_effect=lambda: current["runner"]):
        yield current


@pytest.fixture(autouse=True)
def _clear_session_dismissals():
    reset_session()
    yield
    reset_session()


class TestNotShowingNotification:
    def test_totals_every_plex_library_and_links_to_the_fix_dialog(self, store, runner) -> None:
        store.replace_server(_server("plex-1", "plex", 388, 24, 6599))

        notif = _build_library_health_not_showing_notification()

        assert notif is not None
        assert notif["id"] == LIBRARY_HEALTH_NOT_SHOWING_ID
        assert notif["title"] == "7,011 previews are made but Plex isn't showing them"
        assert notif["action"] == {"label": "Review & fix", "href": "/library-health?fix=plex-1"}
        assert notif["dismissable"] is True
        assert notif.get("permanent_dismissable", True) is True

    def test_names_no_library_because_the_endpoint_is_public(self, store, runner) -> None:
        store.replace_server(_server("plex-1", "plex", 5))

        notif = _build_library_health_not_showing_notification()

        assert notif is not None
        assert "Library 1" not in notif["title"] + notif["body_html"]

    def test_two_plex_servers_link_to_the_page_not_one_dialog(self, store, runner) -> None:
        store.replace_server(_server("plex-a", "plex", 3))
        store.replace_server(_server("plex-b", "plex", 4))

        notif = _build_library_health_not_showing_notification()

        assert notif is not None
        assert notif["title"].startswith("7 previews")
        assert notif["action"]["href"] == "/library-health"

    @pytest.mark.parametrize(
        "server",
        [
            _server("plex-1", "plex", 0),
            _server("jf", "jellyfin", 9),
            _server("emby", "emby", 9),
            _server("plex-1", "plex", 9, state=CellState.ERROR),
        ],
        ids=["plex-zero", "jellyfin", "emby", "plex-cell-error"],
    )
    def test_nothing_to_fix_shows_nothing(self, store, runner, server) -> None:
        store.replace_server(server)

        assert _build_library_health_not_showing_notification() is None

    def test_no_check_yet_shows_nothing(self, store, runner) -> None:
        assert _build_library_health_not_showing_notification() is None

    def test_hidden_while_plex_is_re_reading(self, store, runner) -> None:
        store.replace_server(_server("plex-1", "plex", 12))
        runner["runner"] = _Runner(kind="reread")

        assert _build_library_health_not_showing_notification() is None

    def test_shown_while_an_ordinary_check_runs(self, store, runner) -> None:
        store.replace_server(_server("plex-1", "plex", 12))
        runner["runner"] = _Runner(kind="check")

        assert _build_library_health_not_showing_notification() is not None

    def test_permanent_dismissal_hides_it(self, store, runner) -> None:
        store.replace_server(_server("plex-1", "plex", 12))

        ids = [n["id"] for n in build_active_notifications(dismissed_permanent=[LIBRARY_HEALTH_NOT_SHOWING_ID])]

        assert LIBRARY_HEALTH_NOT_SHOWING_ID not in ids


class TestCheckFailedNotification:
    @pytest.mark.parametrize(
        "failed_runner",
        [_Runner(last_error="Plex database is locked"), _Runner(failed=[("plex-1", "Plex", "plex", "timed out")])],
        ids=["whole-run", "one-server"],
    )
    def test_shown_after_a_failed_check_without_the_error_text(self, runner, failed_runner) -> None:
        runner["runner"] = failed_runner

        notif = _build_library_health_check_failed_notification()

        assert notif is not None
        assert notif["id"] == LIBRARY_HEALTH_CHECK_FAILED_ID
        assert notif["action"]["href"] == "/library-health"
        assert "locked" not in notif["body_html"] and "timed out" not in notif["body_html"]
        assert notif["permanent_dismissable"] is False

    def test_nothing_when_the_last_check_worked(self, runner) -> None:
        assert _build_library_health_check_failed_notification() is None

    def test_only_dismissable_until_restart(self) -> None:
        assert is_session_only_dismissal(LIBRARY_HEALTH_CHECK_FAILED_ID)
        assert not is_session_only_dismissal(LIBRARY_HEALTH_NOT_SHOWING_ID)


class TestDismissRoutes:
    @pytest.mark.parametrize("notification_id", [LIBRARY_HEALTH_NOT_SHOWING_ID, LIBRARY_HEALTH_CHECK_FAILED_ID])
    def test_ids_are_known_to_the_dismiss_routes(self, notification_id) -> None:
        from media_preview_generator.web.routes.api_system import _is_known_notification_id

        assert _is_known_notification_id(notification_id)
