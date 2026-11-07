"""The HDR routing rule: ``hdr_detection.hdr_reason``, listed in ``jobs.routing.RULES`` as "hdr"."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from pymediainfo import MediaInfo

from media_preview_generator.jobs import routing
from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker
from media_preview_generator.jobs.routing import GPU, Requirement, file_requirement
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.processing.hdr_detection import hdr_reason
from media_preview_generator.processing.multi_server import MultiServerResult, MultiServerStatus
from media_preview_generator.processing.types import ProcessableItem
from tests.test_dv_routing_real_fields import REAL_ROWS, SDR

HDR10_FIXTURE = Path(__file__).parent / "fixtures" / "media" / "hdr10_tiny.mkv"
MEDIAINFO = "media_preview_generator.processing.generator.MediaInfo"
PROCESS = "media_preview_generator.processing.multi_server.process_canonical_path"


def _info(hdr_format: str | None, transfer: str | None) -> MagicMock:
    return MagicMock(video_tracks=[MagicMock(hdr_format=hdr_format, transfer_characteristics=transfer)])


class TestHdrReason:
    @pytest.mark.parametrize(
        ("hdr_format", "transfer", "expected"),
        [
            ("Dolby Vision", "PQ", "Dolby Vision"),  # Profile 8.1
            ("Dolby Vision", None, "Dolby Vision"),  # Profile 5
            ("Dolby Vision", "HLG", "Dolby Vision"),  # Profile 8.4
            ("Dolby Vision / SMPTE ST 2094 App 4", "PQ", "Dolby Vision"),
            ("SMPTE ST 2094 App 4", "PQ", "HDR"),  # HDR10+
            ("SMPTE ST 2086", "PQ", "HDR"),  # HDR10
            (None, "PQ", "HDR"),
            (None, "SMPTE ST 2084", "HDR"),
            (None, "HLG", "HDR"),
            (None, "ARIB STD-B67", "HDR"),
            ("SMPTE ST 2086", None, "HDR"),
            (None, "BT.709", None),
            (None, "BT.2020 (10-bit)", None),  # BT.2020 without an HDR curve is SDR
            ("None", None, None),
            ("", "", None),
            (None, None, None),
        ],
    )
    def test_names_the_hdr_the_fields_declare(self, hdr_format, transfer, expected):
        assert hdr_reason("/m/Film (2020).mkv", _info(hdr_format, transfer)) == expected

    def test_a_file_mediainfo_couldnt_open_or_without_video_isnt_labelled(self):
        assert hdr_reason("/m/Film (2020) [DV HDR10].mkv", None) is None  # its name isn't read
        assert hdr_reason("/m/Film (2020).mkv", MagicMock(video_tracks=[])) is None

    @pytest.mark.parametrize("row", REAL_ROWS)
    def test_a_real_file_is_labelled_exactly_when_its_previews_are_tone_mapped(self, row):
        """The rows test_dv_routing_real_fields.py runs through generate_images: routing and processing agree."""
        info = _info(row.hdr_format, row.transfer_characteristics)
        assert (hdr_reason("/m/Film (2020).mkv", info) is not None) is (row.route != SDR)


class TestHdrRule:
    @staticmethod
    def _config(on: bool) -> MagicMock:
        return MagicMock(routing_rules={"hdr": on})

    def test_the_rule_is_listed_needs_a_gpu_and_starts_off(self):
        (rule,) = [rule for rule in routing.RULES if rule.id == "hdr"]

        assert (rule.capability, rule.reason) == (GPU, hdr_reason)
        assert routing.rule_states({})["hdr"] is False

    @pytest.mark.skipif(not MediaInfo.can_parse(), reason="libmediainfo isn't installed")
    def test_a_real_hdr10_file_needs_a_gpu(self):
        assert file_requirement(str(HDR10_FIXTURE), self._config(True)) == Requirement(frozenset({GPU}), "HDR")

    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            (("Dolby Vision / SMPTE ST 2086", "PQ"), "Dolby Vision"),
            (("SMPTE ST 2086", "PQ"), "HDR"),
            ((None, "BT.709"), None),
        ],
        ids=["dolby-vision", "hdr10", "sdr"],
    )
    def test_the_check_labels_a_file_by_its_video_reading_only_its_headers(self, fields, reason):
        tracker = JobTracker("j1", [], self._config(True), MagicMock())
        item = ProcessableItem(canonical_path="/m/Film (2020).mkv", server_id="s1", title="Film (2020).mkv")
        result = MultiServerResult(canonical_path=item.canonical_path, status=MultiServerStatus.NEEDS_GENERATION)

        with patch(PROCESS, return_value=result), patch(MEDIAINFO) as mediainfo:
            mediainfo.parse.return_value = _info(*fields)
            JobDispatcher(WorkerPool(0, 0, []))._run_check(tracker, item)

        assert list(tracker.item_queue) == [item]
        assert tracker.requirements == ({item.canonical_path: Requirement(frozenset({GPU}), reason)} if reason else {})
        assert mediainfo.parse.call_args_list == [call(item.canonical_path, parse_speed=0)]

    def test_with_the_rule_off_the_check_reads_nothing(self):
        tracker = JobTracker("j1", [], self._config(False), MagicMock())
        item = ProcessableItem(canonical_path="/m/Film (2020).mkv", server_id="s1", title="Film (2020).mkv")
        result = MultiServerResult(canonical_path=item.canonical_path, status=MultiServerStatus.NEEDS_GENERATION)

        with patch(PROCESS, return_value=result), patch(MEDIAINFO) as mediainfo:
            JobDispatcher(WorkerPool(0, 0, []))._run_check(tracker, item)

        assert list(tracker.item_queue) == [item]
        assert tracker.requirements == {}
        mediainfo.parse.assert_not_called()
