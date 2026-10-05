"""Keep queue controls stable during hover, and apply fresh rows after hover ends.

The unified queue replaces its tbody on refresh. Rebuilding between mousedown
and mouseup can swallow a Cancel click, so both container and descendant hover
must defer replacement without permanently freezing the displayed jobs.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from ._mocks import mock_dashboard_defaults


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestQueueHoverDefer:
    @pytest.mark.parametrize("hover_target", ["container", "descendant"])
    def test_queue_render_defers_while_hovered_and_refreshes_afterward(
        self, authed_page: Page, app_url: str, hover_target: str
    ) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        result = authed_page.evaluate(
            """hoverTarget => {
                const container = document.getElementById('jobQueue');
                jobs = [{id: 'hover-job', status: 'running', library_name: 'Original library',
                         config: {}, progress: {percent: 30, processed_items: 3, total_items: 10}}];
                updateJobQueue(true);
                const originalRow = document.getElementById('job-row-hover-job');
                const originalButton = originalRow.querySelector('button');
                const originalMatches = container.matches.bind(container);
                const originalQuery = container.querySelector.bind(container);
                container.matches = selector => selector === ':hover'
                    ? hoverTarget === 'container' : originalMatches(selector);
                container.querySelector = selector => selector === ':hover'
                    ? (hoverTarget === 'descendant' ? originalButton : null) : originalQuery(selector);
                try {
                    jobs[0] = {...jobs[0], library_name: 'Updated library'};
                    updateJobQueue();
                    const deferred = {
                        rowPreserved: document.getElementById('job-row-hover-job') === originalRow,
                        buttonPreserved: originalRow.querySelector('button') === originalButton,
                        title: originalRow.querySelector('.queue-job-title').textContent,
                        pending: _jobQueueUpdatePending,
                    };
                    container.matches = selector => selector === ':hover' ? false : originalMatches(selector);
                    container.querySelector = selector => selector === ':hover' ? null : originalQuery(selector);
                    updateJobQueue();
                    return {...deferred,
                        refreshedTitle: container.querySelector('.queue-job-title').textContent,
                        replacedAfterHover: document.getElementById('job-row-hover-job') !== originalRow,
                        pendingAfterHover: _jobQueueUpdatePending};
                } finally {
                    container.matches = originalMatches;
                    container.querySelector = originalQuery;
                }
            }""",
            hover_target,
        )
        assert result == {
            "rowPreserved": True,
            "buttonPreserved": True,
            "title": "Original library",
            "pending": True,
            "refreshedTitle": "Updated library",
            "replacedAfterHover": True,
            "pendingAfterHover": False,
        }

    def test_queue_render_replaces_stale_rows_when_not_hovered(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        result = authed_page.evaluate(
            """() => {
                const container = document.getElementById('jobQueue');
                jobs = [{id: 'old-job', status: 'running', library_name: 'Library A',
                         config: {}, progress: {percent: 30, processed_items: 3, total_items: 10}}];
                updateJobQueue(true);
                const initialTitle = container.querySelector('.queue-job-title').textContent;
                const originalMatches = container.matches.bind(container);
                const originalQuery = container.querySelector.bind(container);
                container.matches = selector => selector === ':hover' ? false : originalMatches(selector);
                container.querySelector = selector => selector === ':hover' ? null : originalQuery(selector);
                try {
                    jobs = [{id: 'new-job', status: 'running', library_name: 'Library B',
                             config: {}, progress: {percent: 60, processed_items: 6, total_items: 10}}];
                    updateJobQueue();
                    return {initialTitle,
                        updatedTitle: container.querySelector('.queue-job-title').textContent,
                        oldRowRemoved: !document.getElementById('job-row-old-job'),
                        newRowPresent: !!document.getElementById('job-row-new-job'),
                        rowCount: container.querySelectorAll('.job-row').length,
                        pending: _jobQueueUpdatePending};
                } finally {
                    container.matches = originalMatches;
                    container.querySelector = originalQuery;
                }
            }"""
        )
        assert result == {
            "initialTitle": "Library A",
            "updatedTitle": "Library B",
            "oldRowRemoved": True,
            "newRowPresent": True,
            "rowCount": 1,
            "pending": False,
        }
