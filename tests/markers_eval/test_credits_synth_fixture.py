"""The lab fixture rebuild's one guard: fresh rows must still measure what the stored fixture pinned."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from media_preview_generator.markers.credits import rule_j
from media_preview_generator.markers.probe import MediaProbe
from tools.markers_eval import credits_synth_fixture as csf
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


class TestVersion1View:
    """``rows_of`` decodes with every step rule J version 1 didn't have patched off, and puts them all back.

    All four, not two: the refine window is decoded from the coarse start, so a band step that moves that start
    replaces the very fine rows the rebuild checks itself against. With only the guard and the overlay step off, the
    build stopped on the lab's second Synth Audio episode and the fixture could not be rebuilt at all.
    """

    STEPS = ("text_all_through", "overlay_boxes", "same_roll", "reach_back")
    DETECT = staticmethod(lambda planes: [])  # the app's own detector for the chosen decode path
    BUG = (4, 6, 46, 24)  # a channel logo, middle 25.5
    BAND = tuple((140, 20 + 30 * n, 180, 35 + 30 * n) for n in range(3))  # a roll's cards, middle 160.5

    @classmethod
    def _rows(cls) -> list:
        """A tail every one of the four steps has something to say about.

        The logo runs across the story and the roll's opening block, so it is an overlay; the block and the crawl are
        two runs in one band with names over footage between them, so they merge and the walk crosses the names; and
        the run starts at the first row, so the guard refuses the file.
        """
        rows = [(float(t), 1, 120.0, (cls.BUG,)) for t in range(0, 400, 2)]
        rows += [(float(t), 4, 10.0, (cls.BUG, *cls.BAND)) for t in range(400, 440, 2)]
        rows += [(float(t), 2, 120.0, cls.BAND[:2]) for t in range(440, 500, 2)]
        rows += [(float(t), 3, 10.0, cls.BAND) for t in range(500, 560, 2)]
        return rows

    def _rows_of(self, monkeypatch, watch, *, name="Synth Credits (2024)", decode="cpu"):
        asked = {}

        def find_credits(path, **kwargs):
            asked.update(path=path, **kwargs)
            return watch()

        monkeypatch.setattr(csf, "find_credits", find_credits)
        rows = csf.rows_of({"name": name}, Path("/lab/x.mkv"), decode=decode,
                           detect_boxes=self.DETECT, probe=lambda path: MediaProbe(600_000, ()))  # fmt: skip
        return rows, asked

    def test_every_step_version_1_lacked_is_off_while_the_file_is_decoded(self, monkeypatch):
        rows = self._rows()
        runs = rule_j.credit_runs(rows)
        assert len(runs) == 2
        last = runs[-1]
        walk = {"run": range(last[0], last[1] + 1), "raw": rows}

        def ask() -> dict:
            return {
                "text_all_through": rule_j.text_all_through(rows, rule_j.Coarse(0, last[1], rows[0][0])),
                "overlay_boxes": rule_j.overlay_boxes(rows),
                "same_roll": rule_j.same_roll(rows, runs),
                "reach_back": rule_j.reach_back(rows, last[0], *last, **walk),
            }

        # What the four really answer on these rows, before anything is patched: each one has something to say, so a
        # step left live would show up below rather than agreeing with its stub by accident.
        real = ask()
        assert real["text_all_through"] is True
        assert real["overlay_boxes"] == (self.BUG,)
        assert real["same_roll"] == 0  # the opening block is merged into the roll
        assert rows[real["reach_back"]][0] == 400.0 and rows[last[0]][0] == 500.0  # the walk crosses the names

        seen = {}

        def watch():
            seen.update(ask())
            return SimpleNamespace(key_rows=(), fine_rows=())

        self._rows_of(monkeypatch, watch)
        # The guard never refuses, no overlay is found, no earlier run is merged, and the walk stays where it started.
        assert seen == {"text_all_through": False, "overlay_boxes": (), "same_roll": 1, "reach_back": last[0]}
        assert all(seen[name] != real[name] for name in seen)

    @pytest.mark.parametrize(
        ("name", "decode", "is_episode", "gpu", "device"),
        [
            ("Synth Credits (2024)", "cpu", False, None, None),
            ("Synth Audio - S01E02", "cpu", True, None, None),
            ("Synth Credits (2024)", "gpu", False, "NVIDIA", "cuda:0"),
            ("Synth Audio - S01E02", "gpu", True, "NVIDIA", "cuda:0"),
        ],
        ids=["movie-cpu", "episode-cpu", "movie-gpu", "episode-gpu"],
    )
    def test_the_decode_is_asked_for_what_the_item_and_the_path_say(
        self, monkeypatch, name, decode, is_episode, gpu, device
    ):
        # `rows_of` decides these three itself, and the fixture is GPU or CPU rows by what it forwards here.
        _rows, asked = self._rows_of(
            monkeypatch, lambda: SimpleNamespace(key_rows=(), fine_rows=()), name=name, decode=decode
        )
        assert asked == {
            "path": "/lab/x.mkv",
            "duration_ms": 600_000,
            "is_episode": is_episode,
            "ffmpeg": "ffmpeg",
            "detect_boxes": self.DETECT,
            "gpu": gpu,
            "gpu_device_path": device,
        }

    def test_the_real_steps_are_back_when_the_decode_raises(self, monkeypatch):
        before = {name: getattr(rule_j, name) for name in self.STEPS}

        def watch():
            raise RuntimeError("ffmpeg went away")

        with pytest.raises(RuntimeError, match="ffmpeg went away"):
            self._rows_of(monkeypatch, watch)
        assert {name: getattr(rule_j, name) for name in self.STEPS} == before
