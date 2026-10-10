"""E2E: Tools -> Library health renders a stored check, lists to-do files, and opens the Plex fix dialog."""

from __future__ import annotations

import re
import time
from collections.abc import Generator
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from media_preview_generator.library_health.models import (
    Cell,
    CellState,
    Feature,
    LibraryResult,
    ServerResult,
    TodoFile,
)
from media_preview_generator.library_health.store import HealthStore

SCREENSHOT = Path(__file__).resolve().parents[2] / ".superpowers/sdd/implementation-plan/task-9-screenshot.png"
TODO_PATHS = [f"/media/Movies/Film {n}/Film {n}.mkv" for n in (1, 2, 3)]


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _result() -> ServerResult:
    movies = LibraryResult(
        library_id="1",
        name="Movies",
        kind="movie",
        total=10,
        cells={
            Feature.PREVIEWS: Cell(CellState.COUNTED, total=10, done=7, not_showing=3),
            Feature.LOUDNESS: Cell(CellState.COUNTED, total=10, done=2),
            Feature.INTRO: Cell(CellState.NOT_APPLICABLE, reason="Not used for films"),
            Feature.CREDITS: Cell(CellState.OFF, reason="Off for this library"),
        },
    )
    todo = [TodoFile(Feature.PREVIEWS, path, f"Film {i}", library_id="1") for i, path in enumerate(TODO_PATHS, 1)]
    todo += [
        TodoFile(Feature.PREVIEWS, f"/media/Movies/Shown {i}/Shown {i}.mkv", f"Shown {i}", f"{i}", True, "1")
        for i in range(1, 4)
    ]
    return ServerResult(
        server_id="e2e-plex",
        name="Plex",
        type="plex",
        libraries=[movies],
        todo=todo,
        access="Reads Plex's database directly",
        checked_at=time.time() - 120,
        duration_s=12,
    )


@pytest.fixture
def seeded_health(tmp_path_factory) -> Generator[HealthStore, None, None]:
    config_dir = next(tmp_path_factory.getbasetemp().glob("config_main*"))
    store = HealthStore(str(config_dir / "library_health.db"))
    store.replace_server(_result())
    yield store
    store.remove_missing_servers(set())


@pytest.mark.e2e
class TestLibraryHealthPage:
    def test_card_renders_every_cell_state(self, authed_page: Page, app_url: str, seeded_health) -> None:
        authed_page.goto(f"{app_url}/library-health")

        card = authed_page.locator(".lh-server")
        expect(card).to_be_visible()
        expect(card).to_contain_text("Plex")
        expect(card).to_contain_text("10 files · 1 library")
        expect(card).to_contain_text("Reads Plex's database directly")
        expect(card.get_by_role("button", name="3 to do")).to_be_visible()
        expect(card.get_by_role("button", name="8 to do")).to_be_visible()
        expect(card).to_contain_text("Not used for films")
        expect(card).to_contain_text("Off for this library")
        expect(card.get_by_role("link", name="Change in Servers")).to_have_attribute("href", "/servers")
        expect(authed_page.locator("#lhSide")).to_contain_text("Last checked")
        expect(authed_page.get_by_role("button", name="3 not showing in Plex")).to_be_visible()
        expect(authed_page.get_by_role("progressbar")).to_have_count(0)

        SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
        authed_page.screenshot(path=str(SCREENSHOT), full_page=True)

    def test_empty_library_says_no_files_instead_of_all_done(
        self, authed_page: Page, app_url: str, seeded_health: HealthStore
    ) -> None:
        result = _result()
        result.libraries.append(
            LibraryResult(
                library_id="2",
                name="Empty",
                kind="movie",
                total=0,
                cells={
                    Feature.PREVIEWS: Cell(CellState.COUNTED),
                    Feature.LOUDNESS: Cell(CellState.COUNTED),
                    Feature.INTRO: Cell(CellState.NOT_APPLICABLE, reason="Not used for films"),
                    Feature.CREDITS: Cell(CellState.COUNTED),
                },
            )
        )
        result.duration_s = 0.2
        seeded_health.replace_server(result)

        authed_page.goto(f"{app_url}/library-health")

        row = authed_page.locator(".lh-table tbody tr", has_text="Empty")
        expect(row).to_contain_text("No files")
        expect(row).not_to_contain_text("All made")
        expect(row).not_to_contain_text("All checked")
        expect(authed_page.locator("#lhSide")).to_contain_text("took under 1 s")

    def test_todo_number_opens_file_list_with_inspector_links(
        self, authed_page: Page, app_url: str, seeded_health
    ) -> None:
        authed_page.goto(f"{app_url}/library-health")
        authed_page.locator(".lh-server").get_by_role("button", name="3 to do").click()

        modal = authed_page.locator("#lhFilesModal")
        expect(modal).to_be_visible()
        expect(modal.locator("#lhFilesTitle")).to_have_text("Movies · Previews")
        rows = modal.locator(".lh-file")
        expect(rows).to_have_count(3)
        links = modal.get_by_role("link", name=re.compile("Open in Inspector"))
        for index, path in enumerate(TODO_PATHS):
            href = links.nth(index).get_attribute("href")
            assert href is not None and href.startswith("/inspector?path=")
            assert href == f"/inspector?path={quote(path, safe='')}"
        expect(modal.get_by_role("button", name="Start preview job for these 3 files")).to_be_visible()

    def test_not_showing_note_lists_the_three_files(self, authed_page: Page, app_url: str, seeded_health) -> None:
        authed_page.goto(f"{app_url}/library-health")
        authed_page.get_by_role("button", name="3 not showing in Plex").click()

        expect(authed_page.locator("#lhFilesModal .lh-file")).to_have_count(3)
        expect(authed_page.locator("#lhFilesModal")).to_contain_text("Shown 1")

    def test_review_and_fix_opens_the_dialog(self, authed_page: Page, app_url: str, seeded_health) -> None:
        authed_page.goto(f"{app_url}/library-health")
        expect(authed_page.locator(".lh-attn")).to_contain_text("3 previews are made but Plex isn't showing them")
        authed_page.get_by_role("button", name="Review & fix").click()

        dialog = authed_page.locator("#lhFixModal")
        expect(dialog).to_be_visible()
        expect(dialog.locator("#lhFixTitle")).to_have_text("Ask Plex to show 3 previews")
        expect(dialog).to_contain_text("Movies")
        expect(dialog.get_by_role("button", name="Start")).to_be_visible()
        expect(dialog.get_by_role("button", name="Cancel")).to_be_visible()

    def test_no_servers_shows_the_empty_state(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/library-health")

        empty = authed_page.locator(".lh-empty")
        expect(empty).to_contain_text("No media servers set up yet")
        expect(empty.get_by_role("link", name="Set up a server")).to_have_attribute("href", "/servers")

    def test_stale_count_with_no_file_rows_starts_no_job(self, authed_page: Page, app_url: str, seeded_health) -> None:
        # Loudness shows "8 to do" but the store holds no loudness rows: an empty path list would mean "every library".
        posts: list[str] = []
        authed_page.route(
            re.compile(r".*/api/(jobs|loudness/jobs|markers/jobs)(/manual)?$"),
            lambda route: (
                (posts.append(route.request.url), route.abort())
                if route.request.method == "POST"
                else route.continue_()
            ),
        )
        authed_page.goto(f"{app_url}/library-health")
        authed_page.locator(".lh-server").get_by_role("button", name="8 to do").click()
        modal = authed_page.locator("#lhFilesModal")
        modal.get_by_role("button", name="Start loudness job for these 8 files").click()
        expect(modal.locator("#lhFilesConfirmText")).to_have_text("Start a loudness job for 8 files in Movies?")
        modal.get_by_role("button", name="Start", exact=True).click()

        expect(authed_page.locator("#toastNotification")).to_contain_text("Nothing left to do here")
        assert posts == []

    @pytest.mark.parametrize(
        ("todo_count", "row_count", "expect_library_job"),
        [(1000, 1000, False), (1001, 5, True)],
        ids=["at-the-limit-sends-paths", "over-the-limit-sends-library"],
    )
    def test_job_body_switches_from_paths_to_library_at_the_limit(
        self,
        authed_page: Page,
        app_url: str,
        seeded_health: HealthStore,
        todo_count: int,
        row_count: int,
        expect_library_job: bool,
    ) -> None:
        result = _result()
        result.libraries[0].cells[Feature.LOUDNESS] = Cell(CellState.COUNTED, total=todo_count, done=0)
        result.todo = [
            TodoFile(Feature.LOUDNESS, f"/media/Movies/L{i}/L{i}.mkv", f"L{i}", library_id="1")
            for i in range(row_count)
        ]
        seeded_health.replace_server(result)
        bodies: list[dict] = []

        def capture(route) -> None:
            bodies.append(route.request.post_data_json)
            route.fulfill(status=200, json={"job_id": "x"})

        authed_page.route(
            re.compile(r".*/api/loudness/jobs$"),
            lambda route: capture(route) if route.request.method == "POST" else route.continue_(),
        )
        authed_page.goto(f"{app_url}/library-health")
        authed_page.locator(".lh-server").get_by_role("button", name=re.compile(r"^1,?00[01] to do$")).click()
        modal = authed_page.locator("#lhFilesModal")
        modal.get_by_role("button", name=re.compile("^Start loudness job")).click()
        modal.get_by_role("button", name="Start", exact=True).click()

        expect(authed_page.locator("#toastNotification")).to_contain_text("Job started")
        assert len(bodies) == 1
        if expect_library_job:
            assert bodies[0] == {"libraries": [{"server_id": "e2e-plex", "library_id": "1"}]}
        else:
            assert len(bodies[0]["file_paths"]) == todo_count
