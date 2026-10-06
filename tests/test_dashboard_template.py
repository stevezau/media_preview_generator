"""Dashboard markup contract: where controls live after the redesign, and the IDs the scripts select."""

from __future__ import annotations

import re

import pytest

from media_preview_generator.web.settings_manager import get_settings_manager


@pytest.fixture
def dashboard_html(tmp_path, monkeypatch) -> str:
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.auth import get_auth_token

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    get_settings_manager().set("setup_complete", True)
    client = app.test_client()
    client.post("/login", data={"token": get_auth_token()}, follow_redirects=False)
    return client.get("/").get_data(as_text=True)


def _section(html: str, start_id: str, end_id: str) -> str:
    start = html.index(f'id="{start_id}"')
    end = html.index(f'id="{end_id}"', start)
    return html[start:end]


class TestDashboardPlacement:
    def test_manage_groups_link_appears_once_inside_workers_header_when_page_renders(self, dashboard_html):
        assert dashboard_html.count(">Manage groups<") == 1
        workers = _section(dashboard_html, "dashboard-workers", "dashboard-all-jobs")
        assert 'href="/settings#section-workers"' in workers
        assert ">Manage groups<" in workers

    def test_quick_actions_has_four_tiles_and_no_libraries_tile_when_page_renders(self, dashboard_html):
        quick = _section(dashboard_html, "quickActionsTitle", "jobStatsTitle")
        assert 'id="quickRunNextSchedule"' in quick
        for href in ('href="/inspector"', 'href="/webhook-activity"', 'href="/logs"'):
            assert href in quick
        assert quick.count("dash-tile-link") == 4
        assert 'id="quickWebhookPending"' in quick
        assert "libraries on" not in quick
        assert "Counting libraries" not in dashboard_html

    def test_pause_control_mounts_in_workers_header_not_in_queue_when_page_renders(self, dashboard_html):
        assert dashboard_html.count('id="globalPauseResumeQueue"') == 1
        workers = _section(dashboard_html, "dashboard-workers", "dashboard-all-jobs")
        assert 'id="globalPauseResumeQueue"' in workers

    def test_system_card_mounts_group_steppers_and_has_no_manage_link_when_page_renders(self, dashboard_html):
        system = _section(dashboard_html, "systemCardTitle", "quickActionsTitle")
        assert 'id="systemWorkerGroups"' in system
        assert "Manage" not in system
        assert "System &amp; Workers" not in dashboard_html

    @pytest.mark.parametrize(
        "element_id",
        [
            "systemStatus",
            "mediaServersStatus",
            "dashboardVersion",
            "dashboardUpdateBadge",
            "statPending",
            "statRunning",
            "statCompleted",
            "statFailed",
            "statCancelled",
            "statTotal",
            "scheduleTeaserBody",
            "workerStatusContainer",
            "workerGroupDashboard",
            "workersHeaderCount",
            "pendingWebhooksChip",
            "jobSearch",
            "jobServerFilter",
            "jobLibraryFilter",
            "jobKindFilter",
            "jobStatusFilter",
            "jobQueue",
            "jobPerPageSelect",
            "jobPaginationInfo",
            "jobPaginationControls",
            "clearCompleted",
            "clearFailed",
            "clearCancelled",
        ],
    )
    def test_script_hook_ids_stay_present_when_page_renders(self, dashboard_html, element_id):
        assert len(re.findall(rf'id="{element_id}"', dashboard_html)) == 1

    def test_status_tabs_cover_every_status_the_clear_menu_and_stats_report_when_page_renders(self, dashboard_html):
        tabs = _section(dashboard_html, "jobStatusTabs", "jobSearch")
        statuses = re.findall(r'data-status="([a-z]*)"', tabs)
        assert statuses == ["", "running", "pending", "completed", "failed", "cancelled"]
