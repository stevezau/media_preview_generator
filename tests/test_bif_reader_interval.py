"""The time between a BIF's frames: header multiplier times the index's timestamp step ("0 s interval" fix)."""

import struct

import pytest

from media_preview_generator.bif_reader import BIF_MAGIC, interval_from_index, read_bif_metadata
from media_preview_generator.inspector.previews import interval_for


def _write_bif(path, *, multiplier_ms: int, timestamps: list[int]) -> str:
    """Write a BIF with the given header multiplier and index timestamps (one tiny JPEG per timestamp)."""
    frames = [b"\xff\xd8\xff" + bytes([i % 256]) * 8 for i in range(len(timestamps))]
    header = BIF_MAGIC + struct.pack("<III", 0, len(frames), multiplier_ms) + b"\x00" * 44
    offset = len(header) + 8 * (len(frames) + 1)
    index = b""
    for ts, frame in zip(timestamps, frames, strict=True):
        index += struct.pack("<II", ts, offset)
        offset += len(frame)
    index += struct.pack("<II", 0xFFFFFFFF, offset)
    with open(path, "wb") as f:
        f.write(header + index + b"".join(frames))
    return str(path)


class TestReadBifMetadataInterval:
    def test_interval_is_the_multiplier_when_timestamps_count_frames(self, tmp_path):
        # This app's writer: the interval in the header, 0, 1, 2... in the index.
        bif = _write_bif(tmp_path / "app.bif", multiplier_ms=5000, timestamps=[0, 1, 2, 3])

        assert read_bif_metadata(bif).frame_interval_ms == 5000

    def test_interval_is_read_from_the_timestamps_when_the_header_says_0(self, tmp_path):
        # The bug: other writers leave the multiplier at 0 (Roku: 1000 ms) and count seconds.
        bif = _write_bif(tmp_path / "other.bif", multiplier_ms=0, timestamps=[0, 2, 4, 6])

        meta = read_bif_metadata(bif)

        assert meta.frame_interval_ms == 2000
        assert meta.frame_count == 4

    def test_interval_multiplies_the_step_by_a_nonzero_multiplier(self, tmp_path):
        bif = _write_bif(tmp_path / "tens.bif", multiplier_ms=1000, timestamps=[0, 10, 20])

        assert read_bif_metadata(bif).frame_interval_ms == 10000

    def test_one_odd_step_does_not_skew_the_interval(self, tmp_path):
        bif = _write_bif(tmp_path / "odd.bif", multiplier_ms=0, timestamps=[0, 2, 4, 5, 7, 9])

        assert read_bif_metadata(bif).frame_interval_ms == 2000

    @pytest.mark.parametrize("multiplier_ms", [5000, 0])
    def test_single_frame_file_keeps_the_header_field(self, tmp_path, multiplier_ms):
        bif = _write_bif(tmp_path / "one.bif", multiplier_ms=multiplier_ms, timestamps=[0])

        assert read_bif_metadata(bif).frame_interval_ms == multiplier_ms

    @pytest.mark.parametrize("multiplier_ms", [5000, 0])
    def test_all_equal_timestamps_keep_the_header_field(self, tmp_path, multiplier_ms):
        bif = _write_bif(tmp_path / "flat.bif", multiplier_ms=multiplier_ms, timestamps=[0, 0, 0, 0])

        assert read_bif_metadata(bif).frame_interval_ms == multiplier_ms


class TestHeaderIntervalKeptAsWritten:
    def test_the_header_field_is_kept_beside_the_derived_interval(self, tmp_path):
        """Frame reuse compares the header field as written; only the Inspector reads the index's steps."""
        meta = read_bif_metadata(_write_bif(tmp_path / "other.bif", multiplier_ms=0, timestamps=[0, 2, 4, 6]))
        assert meta.frame_interval_ms == 2000
        assert meta.header_interval_ms == 0

    def test_this_apps_bif_reads_the_same_either_way(self, tmp_path):
        meta = read_bif_metadata(_write_bif(tmp_path / "ours.bif", multiplier_ms=5000, timestamps=[0, 1, 2, 3]))
        assert meta.frame_interval_ms == meta.header_interval_ms == 5000


class TestIntervalFromIndex:
    @pytest.mark.parametrize(
        ("multiplier_ms", "timestamps", "expected"),
        [
            (5000, [0, 1, 2], 5000),
            (0, [0, 2, 4], 2000),
            (1000, [0, 10, 20], 10000),
            (0, [0, 2, 4, 5, 7, 9], 2000),
            (5000, [0], 5000),
            (0, [], 0),
            (2000, [3, 3, 3], 2000),
        ],
    )
    def test_interval_from_index_when_given_header_and_timestamps(self, multiplier_ms, timestamps, expected):
        assert interval_from_index(multiplier_ms, timestamps) == expected


class TestIntervalFor:
    def test_stated_0_with_a_known_duration_is_duration_over_frames(self):
        assert interval_for({"frame_count": 10, "interval_ms": 0}, 100_000) == 10_000

    def test_stated_far_off_the_length_is_replaced_by_the_derived_interval(self):
        # 10 frames over 100 s: 10 s apart; a stated 5 s is 50% off.
        assert interval_for({"frame_count": 10, "interval_ms": 5000}, 100_000) == 10_000

    def test_stated_close_to_the_length_is_kept(self):
        # Derived 10 000; 11 000 is 10% off, inside the 25% tolerance.
        assert interval_for({"frame_count": 10, "interval_ms": 11_000}, 100_000) == 11_000

    @pytest.mark.parametrize("duration_ms", [None, 0])
    def test_stated_is_kept_when_the_duration_is_unknown(self, duration_ms):
        assert interval_for({"frame_count": 10, "interval_ms": 5000}, duration_ms) == 5000

    def test_stated_is_kept_when_there_are_no_frames(self):
        assert interval_for({"frame_count": 0, "interval_ms": 5000}, 100_000) == 5000

    def test_0_when_neither_the_preview_nor_the_length_can_tell(self):
        assert interval_for({"frame_count": 10, "interval_ms": 0}, None) == 0
