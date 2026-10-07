"""'Add CPU group for loudness' only appears when loudness is on but no CPU group can take it."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    mock_settings_backups,
    mock_settings_get,
    mock_setup_status,
    mock_system_status,
    mock_worker_groups,
)

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


LOUDNESS_WARNING = {"code": "no_eligible_workers", "job_type": "loudness", "message": "Plex loudness has no worker."}


def _open_settings(page: Page, app_url: str, *, cpu_job_types: list[str], warnings: list[dict]) -> None:
    api = mock_worker_groups(page)
    api["state"]["groups"][0]["job_types"] = cpu_job_types
    api["state"]["warnings"] = warnings
    mock_settings_get(page)
    mock_setup_status(page, complete=True)
    mock_system_status(page)
    mock_settings_backups(page)
    page.goto(f"{app_url}/settings#section-workers")
    expect(page.locator("#workerGroupAdd")).to_be_visible()


class TestAddCpuLoudnessGroupButton:
    def test_hidden_when_no_server_needs_loudness_workers(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, cpu_job_types=["previews"], warnings=[])

        expect(authed_page.locator("#workerGroupAddCpu")).to_be_hidden()

    def test_hidden_when_a_cpu_group_already_takes_loudness(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, cpu_job_types=["previews", "loudness"], warnings=[LOUDNESS_WARNING])

        expect(authed_page.locator("#workerGroupAddCpu")).to_be_hidden()

    def test_shown_when_loudness_has_no_eligible_worker(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, cpu_job_types=["previews"], warnings=[LOUDNESS_WARNING])

        expect(authed_page.locator("#workerGroupAddCpu")).to_be_visible()

    def test_hidden_again_once_a_cpu_loudness_group_is_drafted(self, authed_page: Page, app_url: str) -> None:
        _open_settings(authed_page, app_url, cpu_job_types=["previews"], warnings=[LOUDNESS_WARNING])

        authed_page.locator("#workerGroupAddCpu").click()

        expect(authed_page.locator("#workerGroupAddCpu")).to_be_hidden()
