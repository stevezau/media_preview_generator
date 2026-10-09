"""
Basic functionality tests for media_preview_generator.
"""

import pytest


class TestBasicFunctionality:
    """Test basic functionality without complex mocking."""

    def test_package_imports(self):
        """Test that the package can be imported."""
        import re

        import media_preview_generator

        assert hasattr(media_preview_generator, "__version__")
        # Version should be in PEP 440 format (e.g., "2.0.0", "2.1.2.post0", "0.0.0+unknown", "2.3.1.dev5+g1234abc")
        # Pattern matches setuptools-scm generated versions
        version_pattern = r"^\d+\.\d+\.\d+(?:\.(?:post|dev)\d+)?(?:\+[a-zA-Z0-9.-]+)?$"
        assert re.match(version_pattern, media_preview_generator.__version__), (
            f"Version '{media_preview_generator.__version__}' doesn't match PEP 440 format"
        )

    def test_web_module_importable(self, tmp_path, monkeypatch):
        """create_app() returns a real Flask app with registered routes (not just a callable).

        CI gotcha: ``auth.py`` evaluates ``CONFIG_DIR`` and ``AUTH_FILE`` at
        module-import time from the env var, defaulting to ``/config``.
        Passing ``config_dir=tmp_path`` to create_app doesn't redirect those
        constants. On CI runners (no /config write access), create_app's
        ``log_token_on_startup()`` chain hits PermissionError.

        Override AUTH_FILE before calling create_app — that's the only path
        that touches /config in the no-config code path. The rest of
        create_app respects the explicit config_dir we pass in.
        """
        import flask

        from media_preview_generator.web import auth as _auth
        from media_preview_generator.web.app import create_app

        monkeypatch.setattr(_auth, "AUTH_FILE", str(tmp_path / "auth.json"))
        monkeypatch.setattr(_auth, "CONFIG_DIR", str(tmp_path))

        app = create_app(config_dir=str(tmp_path))
        assert isinstance(app, flask.Flask)
        # The app must have registered URL rules — guards against the failure
        # mode where blueprint registration silently raises on import and
        # leaves an empty Flask app behind.
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert len(rules) > 0
        # /api endpoints are mandatory — the dashboard depends on them.
        assert any(r.startswith("/api/") for r in rules), f"No /api/ routes registered: {rules[:5]}"

    def test_socketio_polling_only(self, tmp_path, monkeypatch):
        """Regression: SocketIO must refuse WebSocket upgrades.

        With ``async_mode="threading"``, every accepted WebSocket connection
        pins one gunicorn thread for its lifetime. Page refreshes leave
        CLOSE_WAIT sockets and the 8-thread pool quickly exhausts —
        producing the user-visible "Failed to fetch" toast on Pause and
        a frozen UI under heavy emit load.

        Commit 59d862a fixed this with ``allow_upgrades=False`` + a
        polling-only client transport. The setting was lost during the
        ``plex_generate_previews → media_preview_generator`` package
        rename and the freeze regressed. Lock it in here so the next
        rename or refactor doesn't silently re-introduce it.
        """
        from media_preview_generator.web import auth as _auth
        from media_preview_generator.web.app import create_app, socketio

        monkeypatch.setattr(_auth, "AUTH_FILE", str(tmp_path / "auth.json"))
        monkeypatch.setattr(_auth, "CONFIG_DIR", str(tmp_path))

        create_app(config_dir=str(tmp_path))
        eio_server = socketio.server.eio
        assert eio_server.allow_upgrades is False, (
            "SocketIO must refuse WebSocket upgrades — see commit 59d862a "
            "and create_app()'s socketio.init_app() docstring."
        )

    def test_no_cli_module(self):
        """Test that CLI module has been removed."""
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("media_preview_generator.cli")
