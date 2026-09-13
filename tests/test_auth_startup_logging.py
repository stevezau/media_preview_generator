"""Tests for the startup token banner (``log_token_on_startup``).

The banner is the only place an operator sees *which* token the app actually
uses. Issue #263: a user set ``WEB_AUTH_TOKEN`` but the generic banner still
said "Check /config/auth.json for the full token", making it look like the env
var was ignored. These tests pin the banner to the real token source for every
combination of (env var set?) × (auth.json present?).
"""

import json

import pytest
from loguru import logger


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    """Point the auth module at a temp config dir."""
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr("media_preview_generator.web.auth.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    return auth_file


def _capture_banner(func) -> str:
    """Run ``func`` and return everything it logged as one newline-joined string."""
    messages: list[str] = []
    sink_id = logger.add(messages.append, format="{message}", level="DEBUG")
    try:
        func()
    finally:
        logger.remove(sink_id)
    return "\n".join(str(m) for m in messages)


def _write_saved_token(auth_file: str, token: str) -> None:
    with open(auth_file, "w") as f:
        json.dump({"token": token}, f)


class TestStartupBanner:
    def test_env_only_reports_env_source(self, mock_auth_config, monkeypatch):
        """Env var set, no auth.json: banner names env as the source, not auth.json."""
        monkeypatch.setenv("WEB_AUTH_TOKEN", "env-secret-token-abcd")
        from media_preview_generator.web.auth import log_token_on_startup

        out = _capture_banner(log_token_on_startup)

        assert "****abcd" in out, "masked env token should be shown"
        assert "WEB_AUTH_TOKEN environment variable" in out
        # The misleading line that caused #263 must NOT appear when env-controlled.
        assert "Check /config/auth.json for the full token" not in out
        assert "jq -r .token" not in out, "no jq/cat hint when the token isn't in auth.json"

    def test_env_overrides_saved_token_warns(self, mock_auth_config, monkeypatch):
        """Env var AND a *different* saved token: banner shows the env token and warns the saved one is ignored."""
        _write_saved_token(mock_auth_config, "saved-token-wxyz")
        monkeypatch.setenv("WEB_AUTH_TOKEN", "env-secret-token-abcd")
        from media_preview_generator.web.auth import log_token_on_startup

        out = _capture_banner(log_token_on_startup)

        assert "****abcd" in out, "the env token (not the saved one) is the active token"
        assert "****wxyz" not in out, "the shadowed saved token must not be presented as active"
        assert mock_auth_config in out, "operator is told which file holds the ignored token"
        assert "ignored" in out.lower() or "overrid" in out.lower()

    def test_env_matches_saved_token_no_warning(self, mock_auth_config, monkeypatch):
        """Env var equals the saved token: no 'ignored' noise, just the env source."""
        _write_saved_token(mock_auth_config, "env-secret-token-abcd")
        monkeypatch.setenv("WEB_AUTH_TOKEN", "env-secret-token-abcd")
        from media_preview_generator.web.auth import log_token_on_startup

        out = _capture_banner(log_token_on_startup)

        assert "WEB_AUTH_TOKEN environment variable" in out
        assert "ignored" not in out.lower()

    def test_config_only_reports_config_source(self, mock_auth_config, monkeypatch):
        """No env var, token in auth.json: banner names auth.json and gives the cat/jq hint."""
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        _write_saved_token(mock_auth_config, "saved-token-wxyz")
        from media_preview_generator.web.auth import log_token_on_startup

        out = _capture_banner(log_token_on_startup)

        assert "****wxyz" in out
        assert mock_auth_config in out
        assert "cat" in out, "config-sourced token should tell the user how to read it"
        assert "WEB_AUTH_TOKEN" in out, "should hint at pinning via the env var"

    def test_whitespace_padded_env_token_is_trimmed(self, mock_auth_config, monkeypatch):
        """A trailing newline/space in WEB_AUTH_TOKEN must not break matching.

        Headers are stripped on the way in, so the stored token must be too —
        otherwise the env var silently 'doesn't work' (issue #263 shape).
        """
        monkeypatch.setenv("WEB_AUTH_TOKEN", "  padded-token-1234\n")
        from media_preview_generator.web.auth import (
            get_auth_token,
            is_token_env_controlled,
            validate_token,
        )

        assert get_auth_token() == "padded-token-1234"
        assert is_token_env_controlled() is True
        assert validate_token("padded-token-1234") is True

    def test_whitespace_only_env_token_treated_as_unset(self, mock_auth_config, monkeypatch):
        """A blank/whitespace WEB_AUTH_TOKEN must not lock auth to an unusable token."""
        _write_saved_token(mock_auth_config, "saved-token-wxyz")
        monkeypatch.setenv("WEB_AUTH_TOKEN", "   ")
        from media_preview_generator.web.auth import get_auth_token, is_token_env_controlled

        assert is_token_env_controlled() is False
        assert get_auth_token() == "saved-token-wxyz", "falls back to the saved token"

    def test_external_auth_skips_token(self, mock_auth_config, monkeypatch):
        """AUTH_METHOD=external: no token is printed at all."""
        monkeypatch.setenv("WEB_AUTH_TOKEN", "env-secret-token-abcd")
        monkeypatch.setenv("AUTH_METHOD", "external")
        from media_preview_generator.web.auth import log_token_on_startup

        out = _capture_banner(log_token_on_startup)

        assert "authentication DISABLED" in out
        assert "****abcd" not in out
        assert "Token:" not in out
