"""Global layout polish: one page gutter, a queue table that is never cut off, and a phone row that cannot overlap."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from . import test_intro_credits_jobs_ui as jobs_ui
from ._mocks import mock_dashboard_defaults

dashboard = jobs_ui.dashboard
LONG_TITLE = "The Extremely Long Library Name For Television Shows (Remastered) S01E01-E24 Collection"


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup: None) -> None:
    return complete_setup


def _jobs() -> list[dict]:
    return [
        jobs_ui._job(
            f"{index:08x}-0000-4000-8000-000000000001",
            status=status,
            library_name=LONG_TITLE,
            config={"source": "sonarr"},
            progress={"percent": 40.0 if status == "running" else 0, "processed_items": 4, "total_items": 11},
        )
        for index, status in enumerate(["running", "pending", "failed"], start=1)
    ]


@pytest.mark.e2e
class TestQueueLayout:
    def test_jobs_table_is_not_cut_off_when_the_window_is_768_wide(self, dashboard: Callable[..., Page]) -> None:
        page = dashboard(_jobs())
        page.set_viewport_size({"width": 768, "height": 900})
        expect(page.locator("#jobQueue tr.job-row").first).to_be_visible()
        wrap = page.locator(".queue-table-wrap")
        assert wrap.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        card = page.locator(".dashboard-queue-card").bounding_box()
        for cell in ("queue-created", "queue-actions", "queue-progress-cell", "queue-status"):
            box = page.locator(f"#jobQueue tr.job-row td.{cell}").first.bounding_box()
            assert box["x"] >= card["x"], cell
            assert box["x"] + box["width"] <= card["x"] + card["width"] + 0.5, cell

    def test_no_header_wraps_mid_word_when_the_table_is_shown(self, dashboard: Callable[..., Page]) -> None:
        page = dashboard(_jobs())
        page.set_viewport_size({"width": 1280, "height": 900})
        expect(page.locator("#jobQueue tr.job-row").first).to_be_visible()
        assert page.locator(".jobs-table thead th").evaluate_all(
            "ths => ths.every(th => getComputedStyle(th).whiteSpace === 'nowrap')"
        )

    def test_priority_and_actions_do_not_overlap_when_the_window_is_390_wide(
        self, dashboard: Callable[..., Page]
    ) -> None:
        page = dashboard(_jobs())
        page.set_viewport_size({"width": 390, "height": 900})
        expect(page.locator("#jobQueue tr.job-row").first).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        rows = page.locator("#jobQueue tr.job-row")
        for index in range(rows.count()):
            boxes = {
                name: rows.nth(index).locator(f"td.{name}").bounding_box()
                for name in ("queue-status", "queue-priority", "queue-actions", "queue-progress-cell", "queue-created")
            }
            names = list(boxes)
            for i, first in enumerate(names):
                for second in names[i + 1 :]:
                    a, b = boxes[first], boxes[second]
                    overlap_x = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
                    overlap_y = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
                    assert not (overlap_x > 1 and overlap_y > 1), f"{first} overlaps {second} in row {index}"

    def test_row_shows_at_most_two_quiet_tags_when_it_has_one_server(self, dashboard: Callable[..., Page]) -> None:
        job = jobs_ui._job(
            "aaaaaaaa-0000-4000-8000-000000000009",
            status="running",
            library_names=["TV Shows"],
            server_id="plex-1",
            server_name="Home Plex",
            server_type="plex",
            config={"source": "sonarr"},
            publishers=[],
        )
        page = dashboard([job])
        metadata = page.locator(f"#job-row-{job['id']} .queue-metadata")
        expect(metadata.locator(".badge:not(.job-kind-badge)")).to_have_count(2)
        expect(metadata).not_to_contain_text("Home Plex")
        expect(page.locator(f"#job-row-{job['id']} .tico")).to_have_attribute("title", "Previews · Home Plex")

    def test_status_only_filter_does_not_repeat_the_job_count_when_a_tab_is_chosen(
        self, dashboard: Callable[..., Page]
    ) -> None:
        page = dashboard(_jobs())
        page.locator("#jobStatusTabs button[data-status='running']").click()
        expect(page.locator("#jobFilterCount")).to_be_hidden()
        page.locator("#jobSearch").fill("Extremely")
        expect(page.locator("#jobFilterCount")).to_be_visible()

    def test_running_row_has_no_animated_progress_stripes_when_rendered(self, dashboard: Callable[..., Page]) -> None:
        page = dashboard(_jobs())
        expect(page.locator("#jobQueue tr.job-row").first).to_be_visible()
        assert page.locator("#jobQueue .progress-bar-animated").count() == 0
        assert page.locator("#jobQueue .progress-bar-striped").count() == 0

    def test_dashboard_runs_at_most_two_infinite_animations_when_a_job_is_running(
        self, dashboard: Callable[..., Page]
    ) -> None:
        page = dashboard(_jobs())
        expect(page.locator("#jobQueue tr.job-row").first).to_be_visible()
        expect(page.locator("#jobQueue .progress-bar").first).to_be_visible()
        count = page.evaluate(
            "document.getAnimations().filter(a => a.effect && a.effect.getComputedTiming().iterations === Infinity).length"
        )
        assert count <= 2


@pytest.mark.e2e
class TestPageGutter:
    @pytest.mark.parametrize("width", [1920, 1440, 390])
    def test_every_page_starts_at_the_same_left_edge_when_loaded(
        self, authed_page: Page, app_url: str, width: int
    ) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": width, "height": 900})
        edges: dict[str, tuple[float, str]] = {}
        for path in ("/", "/servers", "/automation", "/settings", "/inspector", "/webhook-activity"):
            authed_page.goto(f"{app_url}{path}")
            authed_page.wait_for_load_state("domcontentloaded")
            edges[path] = authed_page.evaluate(
                "() => { const m = document.getElementById('main-content');"
                " return [m.getBoundingClientRect().left, getComputedStyle(m).paddingLeft + '/' + getComputedStyle(m).maxWidth]; }"
            )
        assert len({tuple(value) for value in edges.values()}) == 1, edges

    @pytest.mark.parametrize("width", [1920, 1440, 1100, 768, 390])
    def test_first_content_element_has_the_same_left_x_when_pages_are_compared(
        self, authed_page: Page, app_url: str, width: int
    ) -> None:
        """Pages must not add a container or margin of their own on top of ``.page-shell``."""
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": width, "height": 900})
        first_element = {
            "/": ".dashboard-layout .card",
            "/servers": ".servers-page h1",
            "/automation": ".automation-sidebar, .automation-mobile-nav",
            "/settings": ".settings-page .page-title",
            "/logs": ".log-crumb",
            "/webhook-activity": ".wh-crumb",
            "/inspector": ".insp-crumb",
        }
        lefts: dict[str, float] = {}
        for path, selector in first_element.items():
            authed_page.goto(f"{app_url}{path}")
            authed_page.wait_for_load_state("domcontentloaded")
            lefts[path] = authed_page.evaluate(
                "(sel) => { const el = [...document.querySelectorAll(sel)].find(e => e.getBoundingClientRect().width > 0);"
                " return el.getBoundingClientRect().left; }",
                selector,
            )
        assert len({round(left, 1) for left in lefts.values()}) == 1, lefts

    def test_navbar_row_shares_the_page_gutter_when_wide(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": 1920, "height": 900})
        authed_page.goto(f"{app_url}/")
        edges = authed_page.evaluate(
            "() => [document.querySelector('nav.navbar > .page-shell').getBoundingClientRect().left,"
            " document.getElementById('main-content').getBoundingClientRect().left]"
        )
        assert edges[0] == edges[1]
