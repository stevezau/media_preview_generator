"""Probe-level advisories + dismissing them.

Two things are pinned here:

1. ``fuse.shfs`` (Unraid user shares) produces an *Unraid-specific* advisory,
   not the "network filesystem" one — it is local storage, and telling an
   Unraid user otherwise is both wrong and the most common false alarm this
   check can raise. ``fuse.mergerfs`` (a local union) raises nothing at all.

2. The advisories are opinions, not errors — the app works. A user who has read
   one and decided their setup is fine can dismiss it forever. The *blocking*
   condition (``writable=False``) is deliberately NOT dismissible: nothing saves
   until it's fixed, so hiding it would hide the reason the app looks broken.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.config_health import (
    filter_dismissed_warnings,
    probe_config_health,
)


@pytest.fixture()
def app(tmp_path):
    """Flask app on a throwaway config dir.

    Two bits of isolation, both load-bearing under xdist:

    * ``create_app`` calls ``schedule_manager.start()``, spinning up APScheduler
      background threads and a SQLite jobstore. Nothing here touches scheduling,
      and an un-stopped scheduler per test leaks threads into whatever else runs
      on this worker — which is exactly how ``test_processing`` starts flaking.
    * These tests *write* settings (dismissals persist) and the settings manager
      is a process-wide singleton, so it's reset on the way in and out.
    """
    from media_preview_generator.web.settings_manager import reset_settings_manager

    config_dir = str(tmp_path / "cfg")
    os.makedirs(config_dir, exist_ok=True)
    with (
        patch.dict(
            os.environ,
            {
                "CONFIG_DIR": config_dir,
                "WEB_AUTH_TOKEN": "test-token-12345678",
                "WEB_PORT": "8099",
            },
        ),
        patch("media_preview_generator.web.app.get_schedule_manager") as mock_sched,
    ):
        mock_sched.return_value = MagicMock()
        reset_settings_manager()
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        try:
            yield flask_app
        finally:
            reset_settings_manager()


@pytest.fixture()
def client(app):
    return app.test_client()


def _headers():
    return {"Authorization": "Bearer test-token-12345678"}


class TestUnraidAdvisory:
    def test_shfs_warns_as_an_unraid_share_not_a_network_share(self, tmp_path):
        with patch(
            "media_preview_generator.web.config_health._mount_for_path",
            return_value=("fuse.shfs", {"rw"}),
        ):
            health = probe_config_health(str(tmp_path))

        kinds = [w["kind"] for w in health["warnings"]]
        assert "unraid_share" in kinds
        assert "network_fs" not in kinds, "Unraid user shares are local — not a network share"
        # The old code set network_fs for every fuse mount; it must not now.
        assert health["network_fs"] is None
        assert health["fs_advisory"] == "unraid_share"

    def test_the_unraid_message_names_the_actual_fix(self, tmp_path):
        with patch(
            "media_preview_generator.web.config_health._mount_for_path",
            return_value=("fuse.shfs", {"rw"}),
        ):
            health = probe_config_health(str(tmp_path))
        msg = next(w["message"] for w in health["warnings"] if w["kind"] == "unraid_share")
        assert "/mnt/cache" in msg  # actionable
        assert "network share" not in msg.replace("not a network share", "")  # not mislabelled

    def test_mergerfs_raises_no_advisory_at_all(self, tmp_path):
        with patch(
            "media_preview_generator.web.config_health._mount_for_path",
            return_value=("fuse.mergerfs", {"rw"}),
        ):
            health = probe_config_health(str(tmp_path))
        assert health["warnings"] == []
        assert health["network_fs"] is None
        assert health["fs_advisory"] is None


class TestFilterDismissedWarnings:
    def test_keeps_warnings_that_were_never_dismissed(self):
        warnings = [{"kind": "unraid_share", "message": "m"}, {"kind": "low_space", "message": "m"}]
        assert filter_dismissed_warnings(warnings, []) == warnings

    def test_drops_a_dismissed_kind(self):
        warnings = [{"kind": "unraid_share", "message": "m"}, {"kind": "low_space", "message": "m"}]
        kept = filter_dismissed_warnings(warnings, ["unraid_share"])
        assert [w["kind"] for w in kept] == ["low_space"]

    def test_tolerates_a_mangled_dismissal_value(self):
        # settings.json is user-editable; a hand-mangled value must not 500.
        warnings = [{"kind": "unraid_share", "message": "m"}]
        assert filter_dismissed_warnings(warnings, None) == warnings
        assert filter_dismissed_warnings(warnings, "unraid_share") == warnings


class TestDismissEndpoint:
    def test_dismissing_a_kind_hides_it_from_the_banner(self, client):
        with patch(
            "media_preview_generator.web.config_health._mount_for_path",
            return_value=("fuse.shfs", {"rw"}),
        ):
            before = client.get("/api/system/config-health", headers=_headers()).get_json()
            assert any(w["kind"] == "unraid_share" for w in before["config"]["warnings"])

            resp = client.post(
                "/api/system/config-health/dismiss",
                json={"kind": "unraid_share"},
                headers=_headers(),
            )
            assert resp.status_code == 200

            after = client.get("/api/system/config-health", headers=_headers()).get_json()

        assert not any(w["kind"] == "unraid_share" for w in after["config"]["warnings"]), (
            "a dismissed advisory came back"
        )

    @pytest.mark.parametrize("kind", ["network_fs", "unraid_share", "low_space"])
    def test_every_dismissible_kind_round_trips(self, client, kind):
        """All three advisories must persist a dismissal, not just the Unraid one."""
        from media_preview_generator.web.settings_manager import get_settings_manager

        resp = client.post("/api/system/config-health/dismiss", json={"kind": kind}, headers=_headers())
        assert resp.status_code == 200
        assert kind in get_settings_manager().get("dismissed_config_warnings", [])

    def test_dismissing_is_idempotent(self, client):
        for _ in range(2):
            client.post(
                "/api/system/config-health/dismiss",
                json={"kind": "unraid_share"},
                headers=_headers(),
            )
        from media_preview_generator.web.settings_manager import get_settings_manager

        assert get_settings_manager().get("dismissed_config_warnings", []).count("unraid_share") == 1

    @pytest.mark.parametrize(
        "body",
        [
            {"kind": "not_a_thing"},
            # writable=False isn't an advisory — hiding it would hide the reason
            # nothing saves.
            {"kind": "not_writable"},
            {"kind": "read_only_mount"},
            # Unhashable / wrong-typed values must 400, never 500: ``x in
            # frozenset`` raises TypeError on a list/dict, and this route is
            # reachable unauthenticated before setup.
            {"kind": ["network_fs"]},
            {"kind": {"a": 1}},
            {"kind": 123},
            {"kind": None},
            {},
        ],
    )
    def test_bad_kind_is_rejected_with_400_never_500(self, client, body):
        resp = client.post("/api/system/config-health/dismiss", json=body, headers=_headers())
        assert resp.status_code == 400, f"expected 400 for {body!r}, got {resp.status_code}"

    def test_dismissals_can_never_hide_the_blocking_writable_error(self, client):
        """The invariant: even with every advisory dismissed, an unwritable
        /config still reports writable=False with its detail + hint.

        True today by construction (advisories are only built when writable),
        but a refactor that moved the blocker into ``warnings`` would otherwise
        pass the whole suite.
        """
        for kind in ("network_fs", "unraid_share", "low_space"):
            client.post("/api/system/config-health/dismiss", json={"kind": kind}, headers=_headers())

        unwritable = {
            "path": "/config",
            "writable": False,
            "status": "not_writable",
            "detail": "Config folder /config isn't writable by this container.",
            "hint": "On the host run `chown -R 1000:1000 <your config folder>`.",
            "warnings": [],
        }
        with patch(
            "media_preview_generator.web.config_health.probe_config_health",
            return_value=unwritable,
        ):
            payload = client.get("/api/system/config-health", headers=_headers()).get_json()

        assert payload["config"]["writable"] is False
        assert payload["config"]["detail"]
        assert payload["config"]["hint"]
