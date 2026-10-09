"""The amber Resume button keeps dark text and a dark icon, also on hover."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from ._mocks import mock_dashboard_defaults
from .test_inspector_behaviours import _SOCKET_STUB

pytestmark = pytest.mark.e2e

_ACCENT_INK = "rgb(26, 18, 3)"


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _resume_colors(page: Page) -> list[str]:
    return page.evaluate(
        """() => {
            const b = document.querySelector('.dash-resume'), i = b.querySelector('i');
            return [getComputedStyle(b).color, getComputedStyle(i).color];
        }"""
    )


def test_resume_button_text_and_icon_are_dark_on_amber(authed_page: Page, app_url: str) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.add_init_script(_SOCKET_STUB)
    page.goto(f"{app_url}/")
    page.wait_for_selector("#globalPauseResumeQueue", state="attached")
    page.evaluate(
        """() => {
            document.getElementById('globalPauseResumeQueue').innerHTML =
                '<button type="button" class="btn dash-btn-sm dash-resume"><i class="bi bi-play-fill"></i><span>Resume</span></button>';
        }"""
    )

    assert _resume_colors(page) == [_ACCENT_INK, _ACCENT_INK]
    page.hover(".dash-resume")
    assert _resume_colors(page) == [_ACCENT_INK, _ACCENT_INK]
