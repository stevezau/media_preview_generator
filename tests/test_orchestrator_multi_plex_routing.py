"""Regression: a full-library scan pinned to a non-first Plex server in a
multi-Plex install must enumerate the PINNED server, not media_servers[0].

This guards against GitHub issue #244: user had two Plex servers (no
Emby/Jellyfin), pinned a library scan to the second Plex, the job ran
against the first one. Root cause: ``_should_use_multi_server_full_scan``
returned False for "2 Plex, no non-Plex" so dispatch fell through to the
legacy ``_run_plex_full_scan_phase``, whose enumerator picked the first
Plex out of ``registry.configs()`` and ignored ``config.server_id_filter``.

The fix routes multi-Plex installs through ``_run_full_scan_multi_server``
which honors ``server_id_filter``. Single-Plex
installs keep their existing legacy path so the WorkerPool's
``worker_pool_callback`` / ``item_complete_callback`` wiring is preserved.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.jobs.orchestrator import (
    _should_use_multi_server_full_scan,
    run_processing,
)


def _full_scan_config(server_id_filter: str | None = None):
    """Bare config that satisfies ``run_processing``'s preconditions for a
    legitimate full library scan (no webhook markers, Plex configured)."""
    return SimpleNamespace(
        webhook_paths=None,
        webhook_source=None,
        server_id_filter=server_id_filter,
        plex_url="http://plex-a.example:32400",
        plex_token="tok-a",
        plex_library_ids=None,
        gpu_threads=0,
        cpu_threads=1,
        working_tmp_folder="/tmp/test-multi-plex-routing-nonexistent",
    )


class TestShouldUseMultiServerFullScan:
    """Pin the gate function itself — the matrix that decides which
    dispatch path handles a full-library scan."""

    @pytest.mark.parametrize(
        ("servers", "pin", "pinned_type", "expected"),
        [
            pytest.param(
                [("plex-a", "plex", True)],
                None,
                "",
                False,
                id="single-plex-keeps-legacy-path",
            ),
            # The #244 bug: the legacy enumerator picks media_servers[0] regardless of the pin.
            pytest.param(
                [("plex-a", "plex", True), ("plex-b", "plex", True)],
                "plex-b",
                "plex",
                True,
                id="two-plex-with-pin",
            ),
            pytest.param(
                [("plex-a", "plex", True), ("plex-b", "plex", True)],
                None,
                "",
                True,
                id="two-plex-no-pin",
            ),
            pytest.param(
                [("plex-a", "plex", True), ("plex-b", "plex", False)],
                None,
                "",
                False,
                id="disabled-second-plex-keeps-legacy-path",
            ),
            pytest.param(
                [("plex-a", "plex", True), ("jf-1", "jellyfin", True)],
                None,
                "",
                True,
                id="plex-plus-jellyfin",
            ),
            pytest.param(
                [("plex-a", "plex", True), ("plex-b", "plex", True), ("plex-c", "plex", True)],
                None,
                "",
                True,
                id="three-plex",
            ),
            # With one enabled Plex the pin to the disabled one is ignored: the enabled Plex is the effective one.
            pytest.param(
                [("plex-a", "plex", True), ("plex-b", "plex", False)],
                "plex-b",
                "plex",
                False,
                id="pin-to-disabled-plex",
            ),
            # The multi-server path warns on a pin that matches nothing, instead of silently scanning the first Plex.
            pytest.param(
                [("plex-a", "plex", True), ("plex-b", "plex", True)],
                "plex-ghost",
                "",
                True,
                id="pin-to-unknown-id",
            ),
        ],
    )
    def test_gate_matrix(self, servers, pin, pinned_type, expected):
        entries = [{"id": sid, "type": stype, "enabled": enabled} for sid, stype, enabled in servers]
        with patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.return_value.get.return_value = entries
            config = _full_scan_config(server_id_filter=pin)
            assert _should_use_multi_server_full_scan(config, pinned_type=pinned_type) is expected


class TestRunProcessingRoutesMultiPlex:
    """Integration: assert ``run_processing`` actually picks the right
    branch and forwards the pin. This is the high-level contract that
    matters to the user — patching only the boundary makes the test
    bug-blind (the legacy path could be called with the wrong pin and
    the test would still pass)."""

    def test_multi_plex_pin_calls_multi_server_with_pin(self):
        """The full chain: 2-Plex install + Plex pin → dispatch hits
        ``_run_full_scan_multi_server`` with ``server_id_filter`` set to
        the pin. The legacy ``_run_plex_full_scan_phase`` must NOT be
        called — that path's enumerator picks ``media_servers[0]`` and
        the second Plex would never be scanned."""
        config = _full_scan_config(server_id_filter="plex-b")
        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch(
                "media_preview_generator.jobs.orchestrator._run_full_scan_multi_server",
                return_value={"generated": 0},
            ) as mock_multi,
            patch("media_preview_generator.jobs.orchestrator._run_plex_full_scan_phase") as mock_legacy,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": True},
            ]
            run_processing(config, selected_gpus=[])

        mock_legacy.assert_not_called()
        mock_multi.assert_called_once()
        # Pin must be forwarded — without this assertion the test is
        # bug-blind (D34-shape regression: multi-server path called but
        # with the wrong pin). The fix's whole point is that "plex-b"
        # gets all the way down to the enumerator's filter.
        assert mock_multi.call_args.kwargs.get("server_id_filter") == "plex-b"

    def test_single_plex_pin_still_uses_legacy_path(self):
        """Control: single-Plex installs keep using the legacy path so
        no callback wiring is silently lost. The pin matches the only
        Plex configured, so the legacy enumerator's first-Plex-wins
        semantics happen to do the right thing here."""
        config = _full_scan_config(server_id_filter="plex-only")
        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.jobs.orchestrator._run_full_scan_multi_server") as mock_multi,
            patch(
                "media_preview_generator.jobs.orchestrator._run_plex_full_scan_phase",
                return_value=True,
            ) as mock_legacy,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-only", "type": "plex", "enabled": True},
            ]
            run_processing(config, selected_gpus=[])

        mock_multi.assert_not_called()
        mock_legacy.assert_called_once()


class TestEnumeratorPicksEnabledPlex:
    """Defence in depth at the legacy enumerator: even if the gate ever
    regresses and lets a "[disabled-first, enabled-second]" layout
    through, the enumerator must pick the *enabled* Plex — never connect
    to a server the user explicitly turned off."""

    def test_picks_enabled_when_first_config_is_disabled(self):
        """Layout: ``media_servers=[disabled Plex-A, enabled Plex-B]``.
        The enumerator must select Plex-B and call its
        ``list_canonical_paths`` — connecting to Plex-A here would be
        the bug shape (disabled server still queried).
        """
        from unittest.mock import MagicMock

        from media_preview_generator.jobs.orchestrator import _enumerate_plex_full_scan_items
        from media_preview_generator.servers.base import ServerType

        disabled_cfg = MagicMock()
        disabled_cfg.type = ServerType.PLEX
        disabled_cfg.enabled = False
        disabled_cfg.id = "plex-a"
        disabled_cfg.name = "Plex-A"

        enabled_cfg = MagicMock()
        enabled_cfg.type = ServerType.PLEX
        enabled_cfg.enabled = True
        enabled_cfg.id = "plex-b"
        enabled_cfg.name = "Plex-B"

        registry = MagicMock()
        registry.configs.return_value = [disabled_cfg, enabled_cfg]

        config = SimpleNamespace(plex_library_ids=None)

        processor = MagicMock()
        processor.list_canonical_paths.return_value = iter([])

        with patch(
            "media_preview_generator.processing.get_processor_for",
            return_value=processor,
        ):
            list(_enumerate_plex_full_scan_items(config, registry))

        # Pin the SUT's contract: the *enabled* Plex's config is what
        # gets handed to the processor — not media_servers[0].
        processor.list_canonical_paths.assert_called_once()
        call_args = processor.list_canonical_paths.call_args
        assert call_args.args[0] is enabled_cfg, (
            f"enumerator passed wrong ServerConfig: expected enabled Plex-B, got {call_args.args[0]!r}"
        )
