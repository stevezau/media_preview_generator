"""The lab fixture rebuild's one guard: fresh rows must still measure what the stored fixture pinned."""

from __future__ import annotations

import pytest

from tools.markers_eval.credits_synth_fixture import same_measurements

# A stored fixture row (positions included) and the same frame decoded again.
STORED = [[12.5, 2, 18.25, [[40, 24, 128, 44], [41, 55, 130, 75]]], [14.5, 0, 9.5, []]]
FRESH = [(12.5, 2, 18.25, ((40, 24, 128, 44), (41, 55, 130, 75))), (14.5, 0, 9.5, ())]


@pytest.mark.parametrize(
    ("fresh", "same"),
    [
        (FRESH, True),
        # The positions are the new part of the row, so a box that moved must NOT fail the rebuild.
        ([(12.5, 2, 18.25, ((1, 2, 3, 4), (5, 6, 7, 8))), FRESH[1]], True),
        ([(12.5, 2, 18.25, ()), FRESH[1]], True),
        ([(99.0, 2, 18.25, FRESH[0][3]), FRESH[1]], False),
        ([(12.5, 3, 18.25, FRESH[0][3]), FRESH[1]], False),
        ([(12.5, 2, 30.0, FRESH[0][3]), FRESH[1]], False),
        (FRESH[:1], False),
        (FRESH + [(16.5, 1, 40.0, ())], False),
        ([], False),
    ],
)
def test_same_measurements_compares_time_count_and_luma_row_for_row(fresh, same):
    assert same_measurements(STORED, list(fresh)) is same


def test_same_measurements_reads_stored_numbers_as_numbers():
    # The fixture is JSON: a time that round-tripped as 12 rather than 12.5 is a real difference, an int 2 is not.
    assert same_measurements([[12, 2, 18.25, []]], [(12.0, 2, 18.25, ())]) is True
    assert same_measurements([[12, 2, 18, []]], [(12.0, 2, 18.25, ())]) is False
