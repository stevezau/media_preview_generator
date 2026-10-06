"""Markup contract for the redesigned setup wizard and login pages (IDs the scripts and e2e tests select)."""

from __future__ import annotations

import re

import pytest
from flask import render_template


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    flask_app = create_app(config_dir=str(tmp_path))
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


def _login_html(app, **context) -> str:
    with app.test_request_context("/login"):
        return render_template("login.html", **context)


@pytest.fixture
def setup_html(app) -> str:
    return app.test_client().get("/setup").get_data(as_text=True)


class TestLoginPage:
    def test_token_field_keeps_ids_and_has_one_submit_when_page_renders(self, app):
        html = _login_html(app)

        assert re.search(r'<input[^>]*id="token"[^>]*name="token"[^>]*required[^>]*autofocus', html)
        assert 'type="password"' in html
        assert 'name="csrf_token"' in html
        assert html.count('type="submit"') == 1

    def test_reveal_toggle_is_a_labelled_non_submit_button_when_page_renders(self, app):
        html = _login_html(app)

        button = re.search(r'<button[^>]*id="tokenReveal"[^>]*>', html).group(0)
        assert 'type="button"' in button
        assert 'aria-label="Show token"' in button
        assert 'aria-pressed="false"' in button

    def test_help_is_collapsed_by_default(self, app):
        html = _login_html(app)

        details = re.search(r'<details[^>]*id="loginTokenHelp"[^>]*>', html).group(0)
        assert " open" not in details
        assert "/config/auth.json" in html
        assert "WEB_AUTH_TOKEN" in html

    @pytest.mark.parametrize("context", [{"error": "Invalid token"}, {"rate_limited": True}])
    def test_help_opens_when_sign_in_failed_or_was_rate_limited(self, app, context):
        html = _login_html(app, **context)

        assert re.search(r'<details[^>]*id="loginTokenHelp"[^>]* open', html)

    def test_wrong_token_shows_danger_alert_and_marks_field_invalid(self, app):
        html = _login_html(app, error="Invalid token")

        assert "alert-danger" in html
        assert "didn&rsquo;t work" in html
        assert 'aria-invalid="true"' in html

    def test_rate_limited_shows_warning_without_invalid_field(self, app):
        html = _login_html(app, rate_limited=True)

        assert "Too many attempts" in html
        assert "alert-danger" not in html
        assert "aria-invalid" not in html

    def test_expired_page_shows_warning_when_flagged(self, app):
        html = _login_html(app, page_expired=True)

        assert "This sign-in page expired" in html
        assert "alert-danger" not in html


class TestSetupPage:
    def test_step_labels_name_what_each_step_does_when_page_renders(self, setup_html):
        labels = re.findall(
            r'class="progress-step[^"]*" data-step="(\d)">\s*<div class="step-number">\d</div>\s*<span[^>]*>([^<]+)</span>',
            setup_html,
        )

        assert labels == [("1", "Server type"), ("2", "Connect"), ("3", "Paths"), ("4", "Options"), ("5", "Security")]

    def test_skip_lives_inside_the_step_one_card_as_a_secondary_button(self, setup_html):
        step1 = setup_html[setup_html.index('data-step="1">\n') : setup_html.index("<!-- Step 2")]

        button = re.search(r'<button[^>]*id="skipSetupBtn"[^>]*>', step1).group(0)
        assert "btn-outline-secondary" in button
        assert "Skip for now" in step1
        assert step1.index("setup-card-footer") < step1.index('id="skipSetupBtn"')

    @pytest.mark.parametrize("vendor", ["plex", "emby", "jellyfin"])
    def test_vendor_cards_are_neutral_buttons_when_page_renders(self, setup_html, vendor):
        button = re.search(rf'<button[^>]*data-vendor="{vendor}"[^>]*>', setup_html).group(0)

        assert "wizard-vendor-btn" in button
        assert "btn-outline" not in button

    def test_finish_is_the_primary_amber_button(self, setup_html):
        button = re.search(r'<button[^>]*id="finishSetup"[^>]*>', setup_html).group(0)

        assert "btn-primary" in button
        assert "btn-success" not in button

    def test_every_wizard_control_id_survives_the_redesign(self, setup_html):
        ids = [
            *(f"step{n}Back" for n in range(2, 6)),
            *(f"step{n}Next" for n in range(1, 5)),
            "finishSetup",
            "vendorPickerBackBtn",
            "manualPlexTestBtn",
            "serverUrlApply",
            "wizardPlexConfigFolder",
            "wizardPlexConfigFolderBrowseBtn",
            "setupAddPathMappingBtn",
            "setupCheckFile",
            "setupSamplePath",
            "setupAddExcludePathBtn",
            "newToken",
            "confirmToken",
            "libraryGrid",
            "librariesCard",
        ]

        missing = [i for i in ids if f'id="{i}"' not in setup_html]
        assert missing == []

    def test_path_mappings_and_excludes_sit_in_the_advanced_disclosure(self, setup_html):
        advanced = setup_html[setup_html.index('id="setupAdvancedDetails"') :]

        for control in ("setupAddPathMappingBtn", "setupAddExcludePathBtn"):
            assert advanced.index(f'id="{control}"') > 0
        assert setup_html.index('id="setupCheckFile"') < setup_html.index('id="setupAdvancedDetails"')

    def test_slim_top_bar_replaces_the_app_navbar_during_setup(self, setup_html):
        assert 'id="setupLogoutBtn"' in setup_html
        assert 'id="setupThemeBtn"' in setup_html
