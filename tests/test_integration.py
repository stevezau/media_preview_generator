"""
Real end-to-end pipeline integration.

:class:`TestRealProcessCanonicalPathIntegration` runs a live
``process_canonical_path`` end-to-end with mocks only at true system
boundaries (FFmpeg subprocess, the BIF writer and the per-server adapter so
we don't write into a real Plex bundle). It catches regressions where a bug
deep in the pipeline goes undetected because every test stubbed the function
under test. The dispatcher's own contract is covered in ``test_job_dispatcher.py``.
"""

from unittest.mock import MagicMock, patch


class TestRealProcessCanonicalPathIntegration:
    """End-to-end pipeline test with mocks only at true system boundaries.

    The point of this class — versus the dispatch-contract tests above —
    is to exercise the *real* ``process_canonical_path`` body. Stubbing
    that function is exactly the D31 anti-pattern: a bug in its frame-
    extraction or publisher fan-out logic could ship for days because
    every test mocked the function under test. Here we mock only:

      * ``generate_images`` (FFmpeg subprocess) — true subprocess boundary.
      * The per-server adapter — we don't write into a real Plex bundle
        directory; we capture the ``publish`` call and assert on its args.
      * ``os.makedirs`` / ``os.listdir`` of the FFmpeg output dir
        (filesystem boundary).

    Source videos are real temporary files, so the source identity checks run.
    """

    def test_real_dispatch_publishes_via_adapter(self, tmp_path):
        """A canonical path resolves to one publisher and the adapter receives a real BifBundle."""
        from media_preview_generator.processing.multi_server import (
            MultiServerStatus,
            process_canonical_path,
        )

        registry = MagicMock()
        server = MagicMock(id="plex-1", name="plex-1")
        adapter = MagicMock()
        adapter.name = "plex_bundle"

        # The adapter will be asked for an output path; return a real
        # path under tmp_path so any incidental fs interaction is safe.
        out_path = tmp_path / "out" / "index-sd.bif"
        adapter.compute_output_paths.return_value = [out_path]
        adapter.publish.return_value = None

        config = MagicMock()
        config.working_tmp_folder = str(tmp_path / "work")
        config.tmp_folder = str(tmp_path / "frames")
        config.plex_bif_frame_interval = 5
        config.thumbnail_interval = 5
        config.server_display_name = "plex-1"

        source = tmp_path / "media" / "movies" / "Test (2024)" / "Test (2024).mkv"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"synthetic video source")
        canonical = str(source)

        with (
            patch(
                "media_preview_generator.processing.multi_server._resolve_publishers",
                return_value=[(server, adapter, "rk-1")],
            ),
            patch(
                "media_preview_generator.processing.multi_server._resolve_item_id_for",
                return_value="rk-1",
            ),
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=False,
            ),
            patch(
                "media_preview_generator.processing.multi_server.generate_images",
                return_value=(True, 12, "h264", 320, 30.0, 320),
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.listdir",
                return_value=[f"{i:05d}.jpg" for i in range(1, 13)],
            ),
            patch(
                "media_preview_generator.processing.multi_server.write_meta",
            ),
        ):
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=config,
                use_frame_cache=False,
            )

        assert result.status is MultiServerStatus.PUBLISHED
        assert len(result.publishers) == 1
        publisher = result.publishers[0]
        assert publisher.server_id == "plex-1"
        assert publisher.adapter_name == "plex_bundle"

        # The real _publish_one inside process_canonical_path should have
        # invoked publish exactly once with the canonical path the
        # dispatcher started from. This is the seam D31 broke — when
        # production accidentally stored the URL form for an item id, the
        # bundle path doubled. The assertion below is the canary: any
        # future regression that reshapes what flows into the adapter
        # would fail here.
        adapter.publish.assert_called_once()
        call_args = adapter.publish.call_args
        bundle_arg = call_args.args[0]
        item_id_arg = call_args.args[2] if len(call_args.args) >= 3 else call_args.kwargs.get("item_id")
        assert bundle_arg.canonical_path == canonical
        assert bundle_arg.frame_count == 12
        assert call_args.args[1] == [out_path]
        from media_preview_generator.output.plex_hash import get_source_fingerprint

        assert bundle_arg.source_fingerprint == get_source_fingerprint(canonical)
        assert item_id_arg == "rk-1"
        # Item id must be the bare ratingKey, NOT the URL form (D31 guardrail).
        assert not str(item_id_arg).startswith("/library/metadata/"), (
            f"D31 regression: item id leaked URL form to adapter: {item_id_arg!r}"
        )

    def test_real_dispatch_handles_no_owners(self, tmp_path):
        """Real process_canonical_path returns NO_OWNERS when no publisher resolves."""
        from media_preview_generator.processing.multi_server import (
            MultiServerStatus,
            process_canonical_path,
        )

        registry = MagicMock()
        config = MagicMock()
        config.working_tmp_folder = str(tmp_path / "work")
        config.tmp_folder = str(tmp_path / "frames")
        config.plex_bif_frame_interval = 5
        config.thumbnail_interval = 5

        with patch(
            "media_preview_generator.processing.multi_server._resolve_publishers",
            return_value=[],
        ):
            result = process_canonical_path(
                canonical_path="/data/movies/Unowned.mkv",
                registry=registry,
                config=config,
                use_frame_cache=False,
            )

        assert result.status is MultiServerStatus.NO_OWNERS
        assert result.publishers == []
