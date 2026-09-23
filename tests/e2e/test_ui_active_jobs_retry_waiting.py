"""Active Jobs panel — the "Waiting to retry" render path.

Regression for a ``ReferenceError: retryAttempt is not defined`` in
``updateActiveJobs()`` (app.js): the retry-waiting branch referenced
``retryAttempt``/``maxRetries`` without ever declaring them, so any job
whose ``progress.retry_eta`` is in the future threw mid-render. The
exception aborted the whole function before ``container.innerHTML`` was
reassigned, so the panel silently stayed on its stale "Idle" markup — a
real job waiting to retry would never show as such on the dashboard.
Caught while building the docs "tour-retry" screenshot (regen_readme.py),
which was the first thing to actually render this state in a browser.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from ._mocks import mock_dashboard_defaults


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestActiveJobsRetryWaiting:
    def test_retry_waiting_job_renders_without_a_js_error(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        errors: list[str] = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))

        result = authed_page.evaluate(
            """
            () => {
                const container = document.getElementById('activeJobsContainer');
                if (!container) return {error: 'no activeJobsContainer'};

                const now = Date.now();
                window.updateActiveJobs([{
                    id: 'retry-demo',
                    status: 'running',
                    library_name: 'Sintel (2010)',
                    progress: {
                        percent: 0,
                        total_items: 1,
                        processed_items: 0,
                        retry_eta: new Date(now + 100000).toISOString(),
                        retry_wait_total: 120,
                    },
                    config: {is_retry_chain: true, retry_attempt: 2, max_retries: 5},
                }]);

                return {innerText: container.innerText};
            }
            """
        )

        assert result.get("error") is None, f"Setup failed: {result.get('error')!r}"
        assert errors == [], f"updateActiveJobs threw in the browser: {errors}"
        assert "Waiting to retry" in result["innerText"]
        assert "Attempt 2 of 5" in result["innerText"]
