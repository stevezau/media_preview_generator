"""Credits text harness rows on synthetic files: the spec tally, the Plex comparison, the gate, the cache."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.markers.credits import rule_j
from media_preview_generator.markers.credits.detector import CREDITS_TEXT_VERSION, CreditsTextResult
from media_preview_generator.markers.credits.frames import GpuDecodeError
from media_preview_generator.markers.probe import Chapter, MediaProbe
from tools.markers_eval import credits_text as ct
from tools.markers_eval.decode_cache import DecodeCache
from tools.markers_eval.plex import PlexMarker

DUR = 6_000_000
# Two credit cards, one over the other: the boxes a dark row of a roll holds.
CARDS = ((40, 24, 128, 44), (41, 55, 130, 75))
BUG = (4, 6, 46, 24)  # a channel logo the run was read without
FILES = [
    {"file": "/m/A (2001)/A.mkv", "credits_start": 5700.0},   # text right, Plex 40 s early: they disagree
    {"file": "/m/B (2002)/B.mkv", "credits_start": 5500.0},   # text 20 s early, Plex 15 s early: they agree
    {"file": "/m/C (2003)/C.mkv", "credits_start": 5600.0},   # no roll found, Plex right
    {"file": "/m/D (2004)/D.mkv", "credits_start": 5400.0},   # text and Plex agree 60 s late
    {"file": "/m/E (2005)/E.mkv", "credits_start": 5600.0},   # a scene after the roll: text ends at 5890 s (Q3)
]  # fmt: skip
ANSWERS = {"/m/A (2001)/A.mkv": (5702.0, None), "/m/B (2002)/B.mkv": (5480.0, None), "/m/C (2003)/C.mkv": (None, None),
           "/m/D (2004)/D.mkv": (5460.0, None), "/m/E (2005)/E.mkv": (5601.0, 5890.0)}  # fmt: skip
BASELINE = {
    "/m/A (2001)/A.mkv": [PlexMarker("credits", 5_660_000, DUR, True)],
    "/m/B (2002)/B.mkv": [PlexMarker("credits", 5_485_000, DUR, True)],
    "/m/C (2003)/C.mkv": [PlexMarker("credits", 5_601_000, DUR, True)],
    "/m/D (2004)/D.mkv": [PlexMarker("credits", 5_462_000, DUR, True)],
    "/m/E (2005)/E.mkv": [PlexMarker("credits", 5_603_000, DUR, True)],
}


def probe(path: str) -> MediaProbe:
    return MediaProbe(DUR, (Chapter(0, None, "Film"),))


def test_rule_tally_uses_the_spec_metric():
    tally = ct.RuleTally()
    for start, truth in ((100.0, 95.0), (60.0, 100.0), (150.0, 100.0), (None, 100.0), (104.0, 100.0)):
        tally.add(start, truth)
    assert tally.as_dict() == {"files": 5, "within_5s": 2, "within_10s": 2, "within_30s": 2, "early": 1, "late": 1, "none": 1}  # fmt: skip
    assert not tally.meets_spec()


def test_text_candidates():
    assert ct.text_candidates(None) == []
    (c,) = ct.text_candidates(5702.0004)
    assert (c.type.value, c.start_ms, c.end_ms, c.source.value) == ("credits", 5_702_000, None, "credits_text")
    (ended,) = ct.text_candidates(5702.0, 5890.0004)
    assert (ended.start_ms, ended.end_ms) == (5_702_000, 5_890_000)


def test_compare_text_rows_are_what_the_pipeline_publishes():
    rows = ct.compare_text(FILES, {}, answers=ANSWERS, probe=probe, baseline=BASELINE, is_movie=True)
    assert rows.plex == Counter(useful=2, wrong=2, late=1)
    assert rows.text == Counter(useful=2, wrong=1, missed=1, late=1)
    # A: 42 s apart → review; B: they agree while both early → wrong; C: Plex alone never decides → missed;
    # D: they agree 60 s late → late; E: they agree, and the text's end stops the skip before the scene.
    assert rows.high == rows.medium == Counter(useful=1, wrong=1, missed=2, late=1)
    assert rows.text_and_server_only == 3
    assert (rows.ends_found, rows.ends_published) == (1, Counter(high=1, medium=1))
    e = rows.files[-1]
    assert (e["high"], e["text_end"]) == ((5601.0, 5890.0), 5890.0)
    names = [f["name"] for f in rows.files]
    assert names == ["A (2001)", "B (2002)", "C (2003)", "D (2004)", "E (2005)"] and all("/" not in n for n in names)
    assert not any("chapters" in key for f in rows.files for key in f)  # chapters are the truth, never a row


def test_compare_text_judges_episodes_by_the_tv_rules():
    # Credits 1000 s before the end: a movie's can't start there (at most 900 s from the end), an episode's can.
    early_roll = [{"file": "/tv/S (2001)/Season 01/S - S01E01.mkv", "credits_start": 5000.0}]
    answers = {early_roll[0]["file"]: (5001.0, None)}
    baseline = {early_roll[0]["file"]: [PlexMarker("credits", 5_002_000, DUR, True)]}
    tv = ct.compare_text(early_roll, {}, answers=answers, probe=probe, baseline=baseline, is_movie=False)
    movie = ct.compare_text(early_roll, {}, answers=answers, probe=probe, baseline=baseline, is_movie=True)
    assert tv.high == tv.medium == Counter(useful=1)
    assert movie.high == movie.medium == Counter(missed=1)
    assert movie.files[0]["medium_reason"] == "2 candidate(s) failed sanity checks"


@pytest.mark.parametrize(("files", "medium_cap", "high_cap"), [(80, 2, 1), (205, 5, 3), (100, 2, 1), (40, 1, 1)])
def test_wrong_caps_round_up(files, medium_cap, high_cap):
    assert (ct.wrong_cap(files, ct.MEDIUM_WRONG_PERCENT), ct.wrong_cap(files, ct.HIGH_WRONG_PERCENT)) == (medium_cap, high_cap)  # fmt: skip


def _rows(medium, high, plex):
    return ct.TextRows(plex=Counter(plex), text=Counter(), high=Counter(high), medium=Counter(medium))


@pytest.mark.parametrize(
    ("medium", "high", "plex", "files", "failing"),
    [
        ({"useful": 56, "wrong": 1}, {"useful": 39, "wrong": 0}, {"useful": 47, "wrong": 13}, 80, []),
        # Measured while planning on the 80: High's one wrong answer is inside the 1 % cap (rounded up: 1 of 80).
        ({"useful": 56, "wrong": 1}, {"useful": 39, "wrong": 1}, {"useful": 47, "wrong": 13}, 80, []),
        ({"useful": 56, "wrong": 1}, {"useful": 39, "wrong": 2}, {"useful": 47, "wrong": 13}, 80, ["High wrong <= 1 (1% of 80)"]),
        # Beats Plex on usefulness and wrong answers, but over the 2 % cap: precision first.
        ({"useful": 60, "wrong": 3}, {"useful": 40, "wrong": 0}, {"useful": 47, "wrong": 13}, 80, ["Medium wrong <= 2 (2% of 80)"]),
        # Inside the caps but looser than a Plex that is better on this set.
        ({"useful": 150, "wrong": 3}, {"useful": 100, "wrong": 1}, {"useful": 140, "wrong": 2}, 205, ["Medium wrong <= Plex wrong"]),
        ({"useful": 139, "wrong": 1}, {"useful": 100, "wrong": 2}, {"useful": 140, "wrong": 2}, 205, ["Medium useful >= Plex useful"]),
        ({"useful": 150, "wrong": 1}, {"useful": 100, "wrong": 4}, {"useful": 140, "wrong": 5}, 205, ["High wrong <= 3 (1% of 205)"]),
        ({"useful": 150, "wrong": 1}, {"useful": 100, "wrong": 2}, {"useful": 140, "wrong": 1}, 205, ["High wrong <= Plex wrong"]),
    ],
)  # fmt: skip
def test_gate_checks_name_every_failing_check(medium, high, plex, files, failing):
    checks = ct.gate_checks(_rows(medium, high, plex), files)
    assert [name for name, ok in checks.items() if not ok] == failing
    assert ct.beats_plex(_rows(medium, high, plex), files) is (failing == [])


def test_merge_rows_adds_the_80s_two_halves():
    movies = ct.compare_text(FILES[:2], {}, answers=ANSWERS, probe=probe, baseline=BASELINE, is_movie=True)
    tv = ct.compare_text(FILES[2:], {}, answers=ANSWERS, probe=probe, baseline=BASELINE, is_movie=True)
    merged = ct.merge_rows([movies, tv])
    whole = ct.compare_text(FILES, {}, answers=ANSWERS, probe=probe, baseline=BASELINE, is_movie=True)
    assert (merged.plex, merged.text, merged.high, merged.medium) == (whole.plex, whole.text, whole.high, whole.medium)
    assert (merged.text_and_server_only, merged.ends_found, merged.ends_published) == (3, 1, Counter(high=1, medium=1))
    assert len(merged.files) == 5


# One card per box, stacked down the frame: a row's boxes. rule J version 3 reads them to reach a start back, and
# epilogue_like reads the run the start belongs to (`Coarse.run_index`), not the reached-back row.
def _cards(boxes):
    return tuple((40, 24 + 30 * n, 128, 44 + 30 * n) for n in range(boxes))


def _dark(t, boxes=0):
    return (float(t), boxes, 10.0, _cards(boxes))


STORY = [(float(t), 0, 120.0, ()) for t in range(0, 90, 2)]


@pytest.mark.parametrize(
    ("rows", "start", "expected"),
    [
        ([*STORY, *[_dark(t, 3) for t in range(90, 160, 2)]], 90.0, False),                      # a plain roll
        ([*STORY, *[_dark(t, 1) for t in range(88, 100, 2)], *[_dark(t, 3) for t in range(100, 160, 2)]], 88.0, True),  # 12 s of cards
        ([*STORY, *[_dark(t, 1) for t in range(90, 100, 2)], *[_dark(t, 3) for t in range(100, 160, 2)]], 90.0, False),  # 10 s: not more
        ([*STORY, *[_dark(t, 2) for t in range(90, 100, 2)], *[_dark(t) for t in range(100, 130, 2)], *[_dark(t, 2) for t in range(130, 190, 2)]], 90.0, True),  # black gap
        ([*STORY, *[(float(t), 0, 120.0, ()) for t in range(90, 100, 2)]], None, False),          # no answer
    ],
)  # fmt: skip
def test_epilogue_like(rows, start, expected):
    assert ct.epilogue_like(rows, start) is expected


def test_epilogue_like_bounds_the_run_by_the_run_not_the_reached_back_start():
    # Version 3 can reach a start back before the run, and the rows are in ffmpeg's output order, so slicing from
    # `coarse.index` can cover rows that aren't the run's -- or, when the row the walk reached was emitted after the
    # whole run, nothing at all. The shape here is the second: a lit two-box frame at 98 s, in the cards' own band,
    # emitted last. Bounded by `run_index` the run is the 31 card and black rows and the 22 s gap in them makes the
    # answer epilogue-shaped; bounded by `index` the slice is empty and every answer of this shape stops being
    # frame-checked.
    story = [(float(t), 0, 120.0, ()) for t in range(0, 98, 2)]
    run = [(float(t), 2, 10.0, _cards(2)) for t in range(100, 110, 2)]
    run += [(float(t), 0, 10.0, ()) for t in range(110, 130, 2)]  # black, which never breaks a run
    run += [(float(t), 2, 10.0, _cards(2)) for t in range(130, 162, 2)]
    rows = [*story, *run, (98.0, 2, 120.0, _cards(2))]  # the reached-back frame, emitted after the run
    coarse = rule_j.coarse_start(rows)
    assert (coarse.pts_s, coarse.index, coarse.run_index, coarse.end_index) == (98.0, 80, 49, 79)
    assert ct.epilogue_like(rows, 98.0) is True
    assert ct.epilogue_like(rows[:-1], 100.0) is True  # the same run, reached from its own first card


def test_epilogue_like_counts_the_runs_frames_without_the_bug():
    # The same run either way; what differs is how dense its frames read. Under the bug every card frame reads three
    # boxes from the first, so the answer looks like a plain roll; without it, the first three-box frame is 14 s in
    # and the answer is shaped like epilogue cards.
    bug = (4, 6, 46, 24)
    rows = [(float(t), 1, 120.0, (bug,)) if t % 8 == 0 else (float(t), 0, 120.0, ()) for t in range(0, 90, 2)]
    rows += [(float(t), 3, 10.0, (*_cards(2), bug)) for t in range(90, 104, 2)]
    rows += [(float(t), 4, 10.0, (*_cards(3), bug)) for t in range(104, 160, 2)]
    without = rule_j.without_overlays(rows, (bug,))
    assert rule_j.overlay_boxes(rows) == (bug,)
    assert rule_j.coarse_start(rows).pts_s == rule_j.coarse_start(rows, without=without).pts_s == 90.0
    assert ct.epilogue_like([(row[0], row[1], row[2], ()) for row in rows], 90.0) is False  # the raw run's counts
    assert ct.epilogue_like(rows, 90.0) is True


def test_epilogue_like_reads_the_run_the_way_rule_j_did():
    # A channel bug over lit story and over a night scene that ends it. To the rows as they were decoded the run opens
    # on that scene at 80 s; rule J read it without the bug and answered 90 s. Both the run's frames and the window
    # the card gaps are measured in ("the run's first 30 s") hang off that start, so the verdict differs: 90 s of
    # cards, a 12 s black gap, then more cards is epilogue-shaped from 90 s and not from 80 s. This verdict picks
    # which answers the owner frame-checks, so it has to be the run the answer came from.
    bug = (4, 6, 46, 24)
    rows = [(float(t), 1, 120.0, (bug,)) if t % 8 == 0 else (float(t), 0, 120.0, ()) for t in range(0, 80, 2)]
    rows += [(float(t), 1, 10.0, (bug,)) for t in range(80, 90, 2)]  # the night scene
    rows += [(float(t), 3, 10.0, (*_cards(2), bug)) for t in range(90, 114, 2)]
    rows += [(float(t), 0, 10.0, ()) for t in range(114, 126, 2)]  # black, which never breaks a run
    rows += [(float(t), 3, 10.0, (*_cards(2), bug)) for t in range(126, 160, 2)]
    assert rule_j.overlay_boxes(rows) == (bug,)
    assert rule_j.coarse_start(rows).pts_s == 80.0  # the raw run opens on the night scene
    assert rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, (bug,))).pts_s == 90.0
    assert ct.epilogue_like([(row[0], row[1], row[2], ()) for row in rows], 90.0) is False  # the raw run's shape
    assert ct.epilogue_like(rows, 90.0) is True


def test_epilogue_like_reads_the_run_from_the_rows_rule_j_read_when_it_is_handed_them():
    # An answer from the 640x360 reading: rule J read its keyframes without the text the 320x180 reading had boxed
    # (an epilogue card at 80-88 s, on a night scene), which the key rows can't show. Read from the key rows, the run
    # opens on that card at 80 s and isn't epilogue-shaped; read from the rows rule J found it on, it opens at 90 s on
    # 12 s of cards, a black gap, then more cards -- the shape the frame check has to see.
    card = (60, 70, 250, 84)
    rows = [(float(t), 0, 120.0, ()) for t in range(0, 80, 2)]
    rows += [(float(t), 1, 10.0, (card,)) for t in range(80, 90, 2)]
    rows += [(float(t), 3, 10.0, _cards(3)) for t in range(90, 114, 2)]
    rows += [(float(t), 0, 10.0, ()) for t in range(114, 126, 2)]
    rows += [(float(t), 3, 10.0, _cards(3)) for t in range(126, 160, 2)]
    run_rows = [(t, 0, luma, ()) if boxes == (card,) else (t, n, luma, boxes) for t, n, luma, boxes in rows]
    assert rule_j.overlay_boxes(rows) == ()
    assert ct.epilogue_like(rows, 90.0, ()) is False
    assert ct.epilogue_like(rows, 90.0, (), run_rows) is True


def test_epilogue_like_finds_the_run_on_the_rows_the_detector_found_it_on():
    # A lower third over lit story, its words boxed three to a frame at 640x360 every 4 s from 5604 s to 5640 s: a run
    # in the key rows. Only 5604 s and 5640 s hold text the 320x180 reading didn't box, 36 s apart: no run on the rows
    # the detector finds its runs on. Found on the key rows instead, the run's first two credit frames are 36 s apart,
    # and the verdict would be "epilogue-like" for a run the detector never answered from.
    seen = ((42, 142, 90, 152), (95, 142, 138, 152), (42, 120, 98, 132))
    new = ((160, 142, 200, 152), (205, 142, 240, 152), (160, 120, 230, 132))
    rows, run_rows = [], []
    for t in range(5500, 5700, 2):
        if t % 4 or not 5604 <= t <= 5640:
            rows.append((float(t), 0, 120.0, ()))
            run_rows.append((float(t), 0, 120.0, ()))
            continue
        fresh = new if t in (5604, 5640) else ()
        rows.append((float(t), 3 + len(fresh), 110.0, (*seen, *fresh)))
        run_rows.append((float(t), len(fresh), 110.0, fresh))
    assert rule_j.coarse_start(rows) is not None and rule_j.coarse_start(run_rows) is None
    assert ct.epilogue_like(rows, 5604.0, (), run_rows) is False


@pytest.mark.parametrize(
    ("text", "end", "truth", "epilogue", "reasons"),
    [
        (5600.0, None, 5600.0, False, []),
        (5585.0, None, 5600.0, False, ["early 10-30 s"]),
        (5560.0, None, 5600.0, False, ["early >30 s"]),
        (5640.0, None, 5600.0, False, ["late >30 s"]),
        (5600.0, 5890.0, 5600.0, False, ["end kept"]),
        (5590.0, None, 5600.0, True, ["epilogue-like"]),
        (None, None, 5600.0, False, []),
    ],
)  # fmt: skip
def test_sheet_reasons(monkeypatch, text, end, truth, epilogue, reasons):
    seen = []

    def epilogue_like(rows, start, overlays, run_rows):
        seen.append((overlays, run_rows))
        return epilogue

    monkeypatch.setattr(ct, "epilogue_like", epilogue_like)
    run_rows = [(5590.0, 2, 10.0, CARDS)]
    assert ct.sheet_reasons({"text": text, "text_end": end, "truth": truth}, [], (BUG,), run_rows) == reasons
    # The overlays and rows the run was read without and from are forwarded, not re-derived: `epilogue_like`'s own
    # docstring says why.
    assert seen == ([] if text is None else [((BUG,), run_rows)])


def _row(path, text, end=None, truth=5600.0, medium=None):
    return {"file": path, "name": path.split("/")[2], "text": text, "text_end": end, "truth": truth, "duration": 6000.0,
            "medium": medium, "high": None}  # fmt: skip


@pytest.mark.parametrize(
    ("before", "after", "moved"),
    [
        ((5600.0, None), (5610.0, None), []),                 # 10 s exactly: not moved
        ((5600.0, None), (5589.9, None), ["start"]),
        ((5600.0, 5800.0), (5600.0, 5811.0), ["end"]),
        ((5600.0, None), (5580.0, 5800.0), ["start", "end"]),  # an end gained
        ((5600.0, 5800.0), (5600.0, None), ["end"]),           # an end lost
        ((None, None), (5600.0, None), ["start"]),             # an answer gained
        ((5600.0, None), (None, None), ["start"]),             # an answer lost
        ((None, None), (None, None), []),
    ],
)  # fmt: skip
def test_changed_answers_lists_every_start_or_end_that_moved_more_than_10_s(before, after, moved):
    path = "/m/A (2001)/A.mkv"
    changed = ct.changed_answers({"movies40": [_row(path, *after)]}, {"movies40": [_row(path, *before)]})
    assert [c["moved"] for c in changed] == ([moved] if moved else [])
    if moved:
        assert (changed[0]["start_before"], changed[0]["end_before"]) == before
        assert changed[0]["set"] == "movies40" and changed[0]["detail"]["text"] == after[0]


def test_changed_answers_compare_each_set_with_its_own_rows():
    # A movie in both movies40 and the 205 is compared per set; a set or a file the earlier run lacks is skipped.
    path, other = "/m/A (2001)/A.mkv", "/m/B (2002)/B.mkv"
    now = {"movies40": [_row(path, 5500.0)], "movie_credit_truth": [_row(path, 5500.0), _row(other, 5000.0)],
           "tv40": [_row("/tv/T/S01/T - S01E01.mkv", 1000.0)]}  # fmt: skip
    before = {"movies40": [_row(path, 5500.0)], "movie_credit_truth": [_row(path, 5600.0)]}
    changed = ct.changed_answers(now, before)
    assert [(c["set"], c["detail"]["file"]) for c in changed] == [("movie_credit_truth", path)]


def test_a_changed_entry_holds_names_and_distances_never_a_path():
    path = "/m/A (2001)/A.mkv"
    (change,) = ct.changed_answers(
        {"movies40": [_row(path, 5520.0, 5900.0, medium=(5520.0, 5900.0))]}, {"movies40": [_row(path, 5640.0)]}
    )
    entry = ct._changed_entry(change)
    assert entry == {"set": "movies40", "name": "A (2001)", "moved": ["start", "end"],
                     "start_minus_truth": [40.0, -80.0], "end_minus_duration": [None, -100.0],
                     "medium_minus_truth": -80.0, "high_minus_truth": None}  # fmt: skip
    assert "/" not in json.dumps(entry)


def test_sheets_are_named_by_time_and_never_written_twice(tmp_path):
    detail = _row("/m/A (2001)/A.mkv", 5520.4, 5900.0)
    with patch.object(ct.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        ct._write_sheets("/ff", tmp_path, "movies40", detail)
    outs = [call.args[0][-1] for call in run.call_args_list]
    assert [Path(out).name.rsplit("-", 1)[1] for out in outs] == ["5520.jpg", "5900.jpg"]
    assert Path(outs[1]).name.startswith(Path(outs[0]).name.rsplit("-", 1)[0] + "-end")
    assert [call.args[0][4] for call in run.call_args_list] == ["5480.4", "5860.0"]  # -ss: 40 s before each
    for out in outs:
        Path(out).write_bytes(b"jpg")
    with patch.object(ct.subprocess, "run") as again:
        ct._write_sheets("/ff", tmp_path, "movies40", detail)
        ct._write_sheets("/ff", tmp_path, "movies40", {**detail, "text_end": None})
    assert again.call_count == 0


def test_the_pipeline_reads_credit_text_only_for_online_cases_left_undecided():
    verdicts = [
        {"key": "a", "type": "credits", "verdict": "useful"},
        {"key": "b", "type": "credits", "verdict": "missed"},
        {"key": "b", "type": "intro", "verdict": "useful"},
        {"key": "c", "type": "intro", "verdict": "missed"},
        {"key": "d", "type": "credits", "verdict": "wrong"},
    ]
    assert ct.undecided_credits(verdicts) == {"b"}


HDR_ARGV = ["/ff/ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=color_transfer:stream_side_data=dv_profile", "-of", "json", "/m/A.mkv"]  # fmt: skip


@pytest.mark.parametrize(
    ("stream", "kind"),
    [
        ({"color_transfer": "bt709"}, "sdr"),
        ({"color_transfer": "smpte2084"}, "hdr10"),
        ({"color_transfer": "arib-std-b67"}, "hdr10"),
        ({"color_transfer": "smpte2084", "side_data_list": [{"dv_profile": 8}]}, "dv_other"),
        ({"side_data_list": [{"dv_profile": 5}]}, "dv5"),
        ({"side_data_list": [{"dv_profile": 5}, {"dv_profile": 8}]}, "dv5"),
    ],
)
def test_hdr_kind(stream, kind):
    with patch.object(ct.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps({"streams": [stream]}), returncode=0)) as run:  # fmt: skip
        assert ct.hdr_kind("/m/A.mkv", ffprobe="/ff/ffprobe") == kind
    # The whole argv: without the side-data entry ffprobe never reports a Dolby Vision profile at all.
    assert run.call_args.args[0] == HDR_ARGV
    assert run.call_args.kwargs == {"capture_output": True, "text": True, "timeout": ct.HDR_PROBE_TIMEOUT_S}


@pytest.mark.parametrize(
    "outcome",
    [
        SimpleNamespace(stdout="", returncode=1),  # ffprobe couldn't open the file
        SimpleNamespace(stdout=json.dumps({"streams": [{"color_transfer": "smpte2084"}]}), returncode=1),
        SimpleNamespace(stdout="not json", returncode=0),
        SimpleNamespace(stdout="[]", returncode=0),
        SimpleNamespace(stdout="", returncode=0),
        subprocess.TimeoutExpired(HDR_ARGV, 60),
        FileNotFoundError("/ff/ffprobe"),
    ],
    ids=["exit-1", "exit-1-with-output", "not-json", "not-an-object", "empty-exit-0", "timeout", "no-ffprobe"],
)
def test_a_probe_that_fails_is_unreadable_not_sdr(outcome, loguru_caplog):
    run = {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome}
    with patch.object(ct.subprocess, "run", **run):
        assert ct.hdr_kind("/m/A (2001)/A.mkv", ffprobe="/ff/ffprobe") == "unreadable"
    assert "A (2001)" in loguru_caplog.text and "/m/" not in loguru_caplog.text


def test_sheet_command_tiles_the_80_s_around_an_answer():
    assert ct.sheet_command("/ff", "/m/A.mkv", 5702.0, "/tmp/s.jpg") == [
        "/ff", "-v", "error", "-ss", "5662.0", "-t", "80", "-i", "/m/A.mkv",
        "-vf", "fps=1/10,scale=320:-2,tile=4x2", "-frames:v", "1", "-y", "/tmp/s.jpg",
    ]  # fmt: skip


def test_cache_runs_the_app_once_per_identity_and_version(tmp_path, monkeypatch):
    media = tmp_path / "A.mkv"
    media.write_bytes(b"x")
    calls = []

    def find(path, **kwargs):
        calls.append(kwargs)
        return CreditsTextResult(
            5702.0,
            5890.0,
            ((5700.0, 2, 12.0, CARDS),),
            ((5701.0, 2, 12.0, CARDS),),
            ((5890.0, 2, 12.0, CARDS),),
            (BUG,),
            2,
            ((5700.0, 1, 12.0, CARDS[:1]),),
        )

    monkeypatch.setattr(ct, "find_credits", find)
    cache = ct.CreditsTextCache(tmp_path / "cache", ffmpeg="/ff", decode="gpu", gpu_device="cuda:0",
                                detect_boxes=lambda p: [()] * len(p), backend=lambda: "webgpu cuda:0",
                                probe=lambda p: MediaProbe(DUR, ()))  # fmt: skip
    first = cache.result(str(media), is_episode=False)
    second = cache.result(str(media), is_episode=False)
    stored_cards = [list(box) for box in CARDS]
    # ``scale`` says the answer came from the 640x360 reading of a tail the 320x180 one found nothing in, and
    # ``runs`` holds the rows rule J found the run on there.
    assert first == second == {"start_s": 5702.0, "end_s": 5890.0, "key": [[5700.0, 2, 12.0, stored_cards]],
                               "fine": [[5701.0, 2, 12.0, stored_cards]],
                               "end": [[5890.0, 2, 12.0, stored_cards]],
                               "overlays": [list(BUG)], "scale": 2,
                               "runs": [[5700.0, 1, 12.0, stored_cards[:1]]]}  # fmt: skip
    assert len(calls) == 1
    assert (calls[0]["duration_ms"], calls[0]["gpu"], calls[0]["gpu_device_path"], calls[0]["is_episode"]) == (DUR, "NVIDIA", "cuda:0", False)  # fmt: skip
    monkeypatch.setattr(ct, "CREDITS_TEXT_VERSION", CREDITS_TEXT_VERSION + 1)
    cache.result(str(media), is_episode=False)
    assert len(calls) == 2


def test_a_change_to_the_detectors_code_measures_again_with_the_same_version(tmp_path, monkeypatch):
    # The ledger's stale-cache incident: rule J changed twice while CREDITS_TEXT_VERSION stayed 1 (nothing had shipped),
    # and the cache kept serving the old answers.
    media = tmp_path / "A.mkv"
    media.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(
        ct, "find_credits", lambda path, **kw: calls.append(kw) or CreditsTextResult(5702.0, None, (), (), ())
    )
    monkeypatch.setattr(ct, "detector_digest", lambda: "before")
    _cache_for(tmp_path, None).result(str(media), is_episode=False)
    _cache_for(tmp_path, None).result(str(media), is_episode=False)
    assert len(calls) == 1
    monkeypatch.setattr(ct, "detector_digest", lambda: "after")
    _cache_for(tmp_path, None).result(str(media), is_episode=False)
    assert len(calls) == 2


def test_the_detector_digest_follows_every_source_file_it_names(tmp_path):
    patterns = ("markers/credits/*.py", "processing/hwaccel.py")
    (tmp_path / "markers/credits").mkdir(parents=True)
    (tmp_path / "processing").mkdir()
    (tmp_path / "markers/credits/rule_j.py").write_text("GAP_S = 24\n")
    (tmp_path / "markers/credits/NOTICE.txt").write_text("not code")
    (tmp_path / "processing/hwaccel.py").write_text("ARGS = ['-hwaccel', 'cuda']\n")
    (tmp_path / "processing/generator.py").write_text("unrelated = 1\n")
    before = ct.detector_digest(tmp_path, patterns)
    assert before == ct.detector_digest(tmp_path, patterns) and len(before) == 16
    for unrelated in ("markers/credits/NOTICE.txt", "processing/generator.py"):
        (tmp_path / unrelated).write_text("changed")
        assert ct.detector_digest(tmp_path, patterns) == before
    digests = {before}
    for path, text in (("processing/hwaccel.py", "ARGS = []\n"), ("markers/credits/rule_j.py", "GAP_S = 25\n"),
                       ("markers/credits/detector.py", "")):  # fmt: skip
        (tmp_path / path).write_text(text)
        digests.add(ct.detector_digest(tmp_path, patterns))
    # A renamed module with the same text is another module to its importers.
    (tmp_path / "markers/credits/rule_j.py").rename(tmp_path / "markers/credits/rule_k.py")
    digests.add(ct.detector_digest(tmp_path, patterns))
    assert len(digests) == 5


# Package modules the hashed files import that change no credits text answer: data classes and locks, the job
# plumbing the detector reports through, the Vulkan probe, which only picks the helper's device (the self-test
# keeps a GPU whose box counts differ from the CPU's out), and the playback speeds decide reads online times by.
NOT_ANSWER_CODE = {"markers.models", "markers.locks", "markers.pipeline", "markers.store", "markers.speed",
                   "processing.generator", "gpu.vulkan_probe"}  # fmt: skip


def _package_imports(path, root):
    """``media_preview_generator`` modules a file imports (module level or inside functions), as dotted names."""
    import ast

    parts = list(path.relative_to(root).with_suffix("").parts)
    package = parts if parts[-1] == "__init__" else parts[:-1]
    package = [p for p in package if p != "__init__"]
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".", 1)[1] for a in node.names if a.name.startswith("media_preview_generator.")}
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = package[: len(package) - node.level + 1] + (node.module.split(".") if node.module else [])
        elif (node.module or "").startswith("media_preview_generator"):
            base = node.module.split(".")[1:]
        else:
            continue
        for alias in node.names:
            sub = base + [alias.name]
            is_module = (root.joinpath(*sub).with_suffix(".py")).is_file() or (
                root.joinpath(*sub) / "__init__.py"
            ).is_file()
            found.add(".".join(sub if is_module else base))
    return found


def test_the_import_guard_reads_every_import_form(tmp_path):
    for module in ("markers/credits/__init__.py", "markers/credits/rule_j.py", "markers/models.py",
                   "processing/hwaccel.py", "processing/filters.py", "gpu/vulkan_probe.py"):  # fmt: skip
        (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / module).write_text("")
    source = tmp_path / "markers/credits/frames.py"
    source.write_text(
        "import os\n"
        "import media_preview_generator.processing.filters\n"
        "from . import rule_j\n"
        "from ..models import Candidate\n"
        "from ...processing.hwaccel import hwaccel_decode_args\n"
        "from media_preview_generator.markers import models\n"
        "def later():\n"
        "    from ...gpu.vulkan_probe import get_vulkan_device_info\n"
    )
    assert _package_imports(source, tmp_path) == {
        "processing.filters", "markers.credits.rule_j", "markers.models", "processing.hwaccel", "gpu.vulkan_probe"
    }  # fmt: skip


def test_every_package_module_the_detector_imports_is_hashed_or_changes_no_answer():
    root = ct.PACKAGE_ROOT
    hashed = {
        ".".join(p.relative_to(root).with_suffix("").parts).removesuffix(".__init__") for p in ct.detector_files()
    }
    assert {"markers.credits.rule_j", "markers.credits.frames", "markers.probe", "processing.hwaccel"} <= hashed
    imported = set().union(*(_package_imports(p, root) for p in ct.detector_files()))
    assert imported - hashed - NOT_ANSWER_CODE == set()
    assert NOT_ANSWER_CODE <= imported  # a stale allowlist entry would hide the next module that does matter


def _cache_for(tmp_path, find, decode="gpu", gpu_device="cuda:0", backend="webgpu cuda:0"):
    return ct.CreditsTextCache(tmp_path / "cache", ffmpeg="/ff", decode=decode, gpu_device=gpu_device,
                               detect_boxes=lambda p: [()] * len(p), backend=lambda: backend,
                               probe=lambda p: MediaProbe(DUR, ()))  # fmt: skip


@pytest.mark.parametrize(
    ("first", "second", "answered_again"),
    [
        (("gpu", "cuda:0", "webgpu cuda:0"), ("gpu", "cuda:0", "webgpu cuda:0"), False),  # the same run again
        (("gpu", "cuda:0", "webgpu cuda:0"), ("gpu", "cuda:0", "cpu"), True),  # the self-test chose the CPU this time
        (("gpu", "cuda:0", "webgpu cuda:0"), ("gpu", "cuda:1", "webgpu cuda:1"), True),  # another card decodes, counts
        (("gpu", "cuda:0", "cpu"), ("gpu", "cuda:1", "cpu"), True),  # another card decodes, the CPU counts both times
        (("gpu", "cuda:0", "cpu"), ("cpu", "cuda:0", "cpu"), True),  # a CPU run is another decode path
        (("cpu", "cuda:0", "cpu"), ("cpu", "cuda:1", "cpu"), False),  # --gpu-device is unused on the CPU path
    ],
)  # fmt: skip
def test_an_answer_is_kept_per_decode_path_card_and_text_detection_backend(tmp_path, monkeypatch, first, second,
                                                                           answered_again):  # fmt: skip
    media = tmp_path / "A.mkv"
    media.write_bytes(b"x")
    calls = []

    def find(path, **kwargs):
        calls.append(kwargs)
        return CreditsTextResult(5702.0, None, (), (), ())

    monkeypatch.setattr(ct, "find_credits", find)
    _cache_for(tmp_path, find, *first).result(str(media), is_episode=False)
    _cache_for(tmp_path, find, *second).result(str(media), is_episode=False)
    assert len(calls) == (2 if answered_again else 1)
    decode, gpu_device, _ = second
    assert calls[-1]["gpu_device_path"] == (gpu_device if decode == "gpu" else None)


def test_another_ffmpeg_build_answers_again(tmp_path, monkeypatch):
    media = tmp_path / "A.mkv"
    media.write_bytes(b"x")
    calls, asked = [], []

    def find(path, **kwargs):
        calls.append(kwargs)
        return CreditsTextResult(5702.0, None, (), (), ())

    monkeypatch.setattr(ct, "find_credits", find)
    for build in ("8.0.1-3ubuntu2", "8.0.1-3ubuntu2", "8.1-1ubuntu1"):
        monkeypatch.setattr(ct, "ffmpeg_build", lambda ffmpeg, b=build: asked.append(ffmpeg) or f"ffmpeg version {b}")
        cache = _cache_for(tmp_path, find)
        cache.result(str(media), is_episode=False)
        cache.result(str(media), is_episode=False)
    assert (len(calls), asked) == (2, ["/ff", "/ff", "/ff"])  # one answer per build; each cache asks once


def test_an_answer_whose_backend_changed_while_it_was_read_is_not_kept(tmp_path, monkeypatch):
    # The GPU helper demoted to the CPU mid-file: its boxes came from both, so no later run may be handed them.
    media = tmp_path / "A.mkv"
    media.write_bytes(b"x")
    calls = []
    backends = iter(["webgpu cuda:0", "cpu"])

    def find(path, **kwargs):
        calls.append(kwargs)
        return CreditsTextResult(5702.0, None, (), (), ())

    monkeypatch.setattr(ct, "find_credits", find)
    cache = ct.CreditsTextCache(tmp_path / "cache", ffmpeg="/ff", decode="gpu", gpu_device="cuda:0",
                                detect_boxes=lambda p: [()] * len(p), backend=lambda: next(backends),
                                probe=lambda p: MediaProbe(DUR, ()))  # fmt: skip
    assert cache.result(str(media), is_episode=False)["start_s"] == 5702.0
    _cache_for(tmp_path, find).result(str(media), is_episode=False)
    assert len(calls) == 2


def test_a_file_the_gpu_cannot_decode_is_read_on_the_cpu_as_the_worker_does(tmp_path, monkeypatch):
    media = tmp_path / "AV1.mkv"
    media.write_bytes(b"x")
    calls = []

    def find(path, **kwargs):
        calls.append(kwargs)
        if kwargs["gpu"] is not None:
            raise GpuDecodeError("ffmpeg exited 69 on the GPU")
        return CreditsTextResult(5702.0, None, ((5700.0, 2, 12.0, CARDS),), (), ())

    monkeypatch.setattr(ct, "find_credits", find)
    cache = _cache_for(tmp_path, find)
    answer = cache.result(str(media), is_episode=False)
    assert answer["start_s"] == 5702.0
    assert [(c["gpu"], c["gpu_device_path"]) for c in calls] == [("NVIDIA", "cuda:0"), (None, None)]
    assert cache.gpu_fallbacks == {ct._name(str(media))} and "/" not in next(iter(cache.gpu_fallbacks))
    # A second cache with the same folder reports the fallback from the marker beside the cached answer.
    again = _cache_for(tmp_path, find)
    assert again.result(str(media), is_episode=False) == answer
    assert again.gpu_fallbacks == cache.gpu_fallbacks
    assert len(calls) == 2


def test_a_gpu_decode_that_works_never_falls_back(tmp_path, monkeypatch):
    media = tmp_path / "A.mkv"
    media.write_bytes(b"x")
    calls = []

    def find(path, **kwargs):
        calls.append(kwargs)
        return CreditsTextResult(5702.0, None, (), (), ())

    monkeypatch.setattr(ct, "find_credits", find)
    cache = _cache_for(tmp_path, find)
    cache.result(str(media), is_episode=False)
    assert [c["gpu"] for c in calls] == ["NVIDIA"]
    assert cache.gpu_fallbacks == set()


@pytest.mark.parametrize("sets", [("80", " 205"), ("90",), ("80", "205", "1000"), ("",)])
def test_an_unknown_set_stops_the_run_instead_of_reporting_a_clean_gate(sets, tmp_path, monkeypatch):
    # "--sets '80, 205'" splits to ("80", " 205"): the 205 would be skipped and the run would exit 0 on the 80 alone.
    def never(*args, **kwargs):
        raise AssertionError("the run must not start before the sets are checked")

    monkeypatch.setattr(ct, "_detection_on", never)
    monkeypatch.setattr(ct, "evidence_dir", never)
    with pytest.raises(ValueError, match="unknown set"):
        ct.run_credits_text(decode="cpu", gpu_device="cuda:0", sets=sets, online=False, cache_root=tmp_path,
                            ffmpeg="/ff", ffprobe="/ffp", baseline_path=tmp_path / "b.json", sheets_dir=None)  # fmt: skip


def test_the_command_exits_non_zero_on_an_unknown_set(monkeypatch):
    from tools.markers_eval import __main__ as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/ff")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["credits-text", "--sets", "80, 205", "--cache", "/tmp/markers-eval-unused"])
    assert exit_info.value.code != 0
    assert "' 205'" in str(exit_info.value.code)


def test_the_cache_refuses_data_folders():
    with pytest.raises(ValueError, match="/data"):
        ct.CreditsTextCache(__import__("pathlib").Path("/data/cache"), ffmpeg="/ff", decode="cpu", gpu_device=None,
                            detect_boxes=lambda p: [], backend=lambda: "cpu", probe=lambda p: MediaProbe(DUR, ()))  # fmt: skip


GATE_FILES = {
    "movies40": [{"file": "/m/M1 (2001)/M1.mkv", "credits_start": 5700.0}, {"file": "/m/M2 (2002)/M2.mkv", "credits_start": 5600.0}],
    # 1000 s before the end: only the TV rules let credits start there, so this row fails if tv40 is judged as movies.
    "tv40": [{"file": "/tv/T (2003)/Season 01/T - S01E01.mkv", "credits_start": 5000.0}],
    "movie_credit_truth": [{"file": "/m/X1 (2010)/X1.mkv", "credits_start": 5700.0}, {"file": "/m/X2 (2011)/X2.mkv", "credits_start": 5500.0}],
}  # fmt: skip
GATE_ANSWERS = {"/m/M1 (2001)/M1.mkv": 5701.0, "/m/M2 (2002)/M2.mkv": 5602.0,
                "/tv/T (2003)/Season 01/T - S01E01.mkv": 5001.0, "/m/X1 (2010)/X1.mkv": 5700.0,
                "/m/X2 (2011)/X2.mkv": 5501.0}  # fmt: skip
GATE_PLEX = {"/m/M1 (2001)/M1.mkv": 5702.0, "/m/M2 (2002)/M2.mkv": 5601.0,
             "/tv/T (2003)/Season 01/T - S01E01.mkv": 5002.0, "/m/X1 (2010)/X1.mkv": 5703.0,
             "/m/X2 (2011)/X2.mkv": 5502.0}  # fmt: skip


class _GateRun:
    """``run_credits_text`` end to end with the harness's own building blocks replaced by recording fakes: evidence
    files, probes, Plex's markers, the app's credits text answers, HDR kinds and the helper pool. Every fake records
    the arguments ``run_credits_text`` hands it."""

    def __init__(
        self, tmp_path, monkeypatch, *, missing=(), spec_within_10s=3, failing_path=None, cache_error=None, gone=()
    ):
        self.cache_calls, self.closed, self.seen = [], 0, {}
        evidence = tmp_path / "evidence"
        (evidence / "credits").mkdir(parents=True)
        for name, rows in {**GATE_FILES, "adjudicated": {}}.items():
            (evidence / f"credits/{name}.json").write_text(json.dumps(rows))
        run = self
        self.counted = []

        def detect_boxes(planes):
            run.counted.append(planes.shape)
            return [()] * len(planes)

        def backend():
            return "webgpu cuda:0" if run.counted else None

        class FakeProbes:
            def __init__(self, root, *, ffprobe):
                run.seen["probes"] = (root, ffprobe)

            def probe(self, path):
                return MediaProbe(DUR, ())

        class FakeCache:
            def __init__(self, root, **kwargs):
                run.seen["cache"] = (root, kwargs)
                if cache_error is not None:
                    raise cache_error
                self.detector_digest, self.gpu_fallbacks = "d" * 16, set()

            def result(self, path, *, is_episode):
                run.cache_calls.append((path, is_episode))
                if path == failing_path:
                    raise RuntimeError("decode failed")
                start = None if path in missing else GATE_ANSWERS[path]
                # One keyframe, the 640x360 reading's: rule J read it without a card the 320x180 reading boxed.
                return {
                    "start_s": start,
                    "end_s": None,
                    "key": [[5700.0, 2, 10.0, [list(box) for box in CARDS]]],
                    "fine": [],
                    "end": [],
                    "overlays": [list(BUG)],
                    "scale": 2,
                    "runs": [[5700.0, 1, 10.0, [list(CARDS[1])]]],
                }

        def close():
            run.closed += 1

        def detection(decode, gpu_device):
            run.seen["detection"] = (decode, gpu_device)
            return ct.TextDetection(detect_boxes, backend, close)

        def hdr_kind(path, *, ffprobe):
            run.seen.setdefault("hdr_ffprobe", set()).add(ffprobe)
            return "hdr10" if "M1" in path else "sdr"

        monkeypatch.setattr(ct, "evidence_dir", lambda: evidence)
        monkeypatch.setattr(ct, "ProbeCache", FakeProbes)
        monkeypatch.setattr(ct, "CreditsTextCache", FakeCache)
        monkeypatch.setattr(ct, "_detection_on", detection)
        monkeypatch.setattr(ct, "load_baseline", lambda path: run.seen.update(baseline=path) or {p: [PlexMarker("credits", int(s * 1000), DUR, True)] for p, s in GATE_PLEX.items()})  # fmt: skip
        monkeypatch.setattr(ct, "hdr_kind", hdr_kind)
        monkeypatch.setattr(ct, "on_disk", lambda path: path not in gone)
        monkeypatch.setattr(ct, "SPEC_WITHIN_10S", spec_within_10s)
        self.tmp_path, self.detect_boxes = tmp_path, detect_boxes

    def __call__(self, sets=("80", "205"), decode="gpu", gpu_device="cuda:0"):
        return ct.run_credits_text(decode=decode, gpu_device=gpu_device, sets=sets, online=False,
                                   cache_root=self.tmp_path / "cache", ffmpeg="/ff", ffprobe="/ffp",
                                   baseline_path=self.tmp_path / "b.json", sheets_dir=None)  # fmt: skip


@pytest.mark.parametrize(
    ("decode", "verdict", "backend"),
    [
        ("gpu", None, None),                      # before the helper's first request: no key yet
        ("gpu", "webgpu", "webgpu cuda:1"),       # the self-test kept the card: keyed on it
        ("gpu", "cpu", "cpu"),                    # the self-test chose the CPU: a CPU run's key
        ("cpu", "cpu", "cpu"),                    # a CPU run
    ],
)  # fmt: skip
def test_the_detection_reports_the_backend_the_pool_actually_used(monkeypatch, decode, verdict, backend):
    # This string is the decode cache's key: a GPU run whose self-test chose the CPU must share a CPU run's rows.
    from media_preview_generator.markers.credits import textdet_helper

    asked = []

    class Pool:
        def backend_of(self, gpu, gpu_device_path):
            asked.append((gpu, gpu_device_path))
            return verdict

        def detect_boxes(self, planes, *, gpu, gpu_device_path):
            asked.append(("detect", gpu, gpu_device_path))
            return [()] * len(planes)

        def close_all(self):
            asked.append("closed")

    monkeypatch.setattr(textdet_helper, "get_textdet_pool", Pool)
    detection = ct._detection_on(decode, "cuda:1")
    assert detection.backend() == backend
    gpu, device = ("NVIDIA", "cuda:1") if decode == "gpu" else (None, None)
    detection.detect_boxes([object()])
    detection.close()
    assert asked == [(gpu, device), ("detect", gpu, device), "closed"]


@pytest.mark.parametrize(("decode", "gpu_device"), [("gpu", "cuda:1"), ("cpu", "cuda:0")])
def test_the_run_hands_its_decode_path_and_tools_to_every_part(tmp_path, monkeypatch, decode, gpu_device):
    # A --decode cpu run that measured the GPU path (or the wrong card) would still report "decode": "cpu".
    run = _GateRun(tmp_path, monkeypatch)
    summary, _, _ = run(decode=decode, gpu_device=gpu_device)
    assert run.seen["detection"] == (decode, gpu_device)
    root, kwargs = run.seen["cache"]
    assert root == tmp_path / "cache"
    assert kwargs.pop("probe").__self__.__class__.__name__ == "FakeProbes"
    decodes = kwargs.pop("decodes")
    assert isinstance(decodes, DecodeCache) and decodes.digest == ct.decode_digest()
    # The helper is started on one blank frame before the first file, so the cache is told a backend from the start.
    assert run.counted[0] == (1, 180, 320) and decodes._backend() == "webgpu cuda:0"
    assert summary["text_detection"] == "webgpu cuda:0"
    assert kwargs.pop("backend")() == "webgpu cuda:0"
    assert kwargs == {"ffmpeg": "/ff", "decode": decode, "gpu_device": gpu_device, "detect_boxes": run.detect_boxes}
    assert run.seen["probes"] == (tmp_path / "cache", "/ffp")
    assert run.seen["hdr_ffprobe"] == {"/ffp"}
    assert run.seen["baseline"] == tmp_path / "b.json"
    assert summary["decode"] == decode


def test_the_gate_passes_only_when_every_set_and_rule_j_pass(tmp_path, monkeypatch):
    summary, details, passed = _GateRun(tmp_path, monkeypatch)()
    assert passed is True
    assert summary["gate"]["80"]["files"] == 3 and summary["gate"]["205"]["files"] == 2
    assert all(summary["gate"]["80"]["checks"].values()) and all(summary["gate"]["205"]["checks"].values())
    assert summary["gate"]["80"]["medium"] == {"useful": 3}  # the 80's halves merged, the TV row judged as TV
    assert summary["sets"]["tv40"]["medium"] == {"useful": 1}
    assert summary["detector_digest"] == "d" * 16
    assert set(details) == {"movies40", "tv40", "movie_credit_truth"}


def test_rule_j_counts_the_80_only_and_splits_it_by_hdr_kind(tmp_path, monkeypatch):
    summary, _, _ = _GateRun(tmp_path, monkeypatch)()
    assert summary["rule_j_80"] == {"files": 3, "within_5s": 3, "within_10s": 3, "within_30s": 3, "early": 0,
                                    "late": 0, "none": 0, "meets_spec": True}  # fmt: skip
    assert {k: v["files"] for k, v in summary["rule_j_80_by_kind"].items()} == {"hdr10": 1, "sdr": 2}


def test_episodes_are_read_with_the_episode_tail_and_movies_with_the_movie_tail(tmp_path, monkeypatch):
    run = _GateRun(tmp_path, monkeypatch)
    run()
    assert dict(run.cache_calls) == {"/m/M1 (2001)/M1.mkv": False, "/m/M2 (2002)/M2.mkv": False,
                                     "/tv/T (2003)/Season 01/T - S01E01.mkv": True, "/m/X1 (2010)/X1.mkv": False,
                                     "/m/X2 (2011)/X2.mkv": False}  # fmt: skip


@pytest.mark.parametrize(
    ("missing", "spec_within_10s", "failing"),
    [
        # A roll found nowhere in the 80 while Plex has it: the 80 fails even though the 205 (run after it) passes.
        (("/m/M2 (2002)/M2.mkv",), 2, ("80", "Medium useful >= Plex useful")),
        (("/m/X2 (2011)/X2.mkv",), 3, ("205", "Medium useful >= Plex useful")),
    ],
    ids=["80-fails-205-passes", "205-fails-80-passes"],
)
def test_one_failing_set_fails_the_whole_gate(tmp_path, monkeypatch, missing, spec_within_10s, failing):
    summary, _, passed = _GateRun(tmp_path, monkeypatch, missing=missing, spec_within_10s=spec_within_10s)()
    group, check = failing
    other = "205" if group == "80" else "80"
    assert summary["gate"][group]["checks"][check] is False
    assert all(summary["gate"][other]["checks"].values())
    assert summary["rule_j_80"]["meets_spec"] is True
    assert passed is False


def test_rule_j_below_the_spec_fails_a_gate_whose_sets_pass(tmp_path, monkeypatch):
    summary, _, passed = _GateRun(tmp_path, monkeypatch, spec_within_10s=4)()
    assert all(all(g["checks"].values()) for g in summary["gate"].values())
    assert summary["rule_j_80"]["meets_spec"] is False
    assert passed is False


def test_a_run_lists_and_sheets_every_answer_that_moved_since_an_earlier_run(tmp_path, monkeypatch):
    run = _GateRun(tmp_path, monkeypatch)
    _, details, _ = run()
    assert "changed" not in run()[0]
    before = json.loads(json.dumps(details))
    before["movie_credit_truth"][1]["text"] = 5480.0  # X2: 5501 now, 21 s later than before
    before["tv40"][0]["text"] = 4995.0  # 6 s: not a change
    written = []
    monkeypatch.setattr(ct, "_write_sheet", lambda ffmpeg, path, around, out: written.append((path, around, out)))
    # Every row is worth a look on its own run; against an earlier one only the row that moved gets a sheet.
    # The stub takes `overlays` and `run_rows` positionally and records them: `run_credits_text` has to forward
    # what the stored answer kept, or `epilogue_like` re-gathers them from rows that may be the joined ones, or that
    # still hold the text the 320x180 reading boxed.
    forwarded = []

    def sheet_reasons(detail, key_rows, overlays, run_rows):
        forwarded.append((overlays, run_rows))
        return ["late >30 s"]

    monkeypatch.setattr(ct, "sheet_reasons", sheet_reasons)
    summary, _, _ = ct.run_credits_text(decode="gpu", gpu_device="cuda:0", sets=("80", "205"), online=False,
                                        cache_root=tmp_path / "cache", ffmpeg="/ff", ffprobe="/ffp",
                                        baseline_path=tmp_path / "b.json", sheets_dir=tmp_path / "sheets",
                                        before=before)  # fmt: skip
    assert [(c["set"], c["name"], c["moved"]) for c in summary["changed"]] == [
        ("movie_credit_truth", "X2 (2011)", ["start"])
    ]
    assert summary["changed"][0]["start_minus_truth"] == [-20.0, 1.0]
    assert [(path, around) for path, around, _ in written] == [("/m/X2 (2011)/X2.mkv", 5501.0)]
    # The stored answer's own overlays and rule rows, by value: forwarding the key rows, or a literal [], would pass
    # an arity check.
    assert forwarded and forwarded == [([BUG], [(5700.0, 1, 10.0, CARDS[1:])])] * len(forwarded)


def test_only_the_chosen_set_is_run_and_judged(tmp_path, monkeypatch):
    run = _GateRun(tmp_path, monkeypatch, missing=("/m/M2 (2002)/M2.mkv",))
    summary, _, passed = run(sets=("205",))
    assert set(summary["gate"]) == {"205"} and "rule_j_80" not in summary
    assert {path for path, _ in run.cache_calls} == {"/m/X1 (2010)/X1.mkv", "/m/X2 (2011)/X2.mkv"}
    assert passed is True


@pytest.mark.parametrize("name", sorted(ct.REGRESSION_SETS))
def test_a_regression_set_is_read_from_its_truth_file_as_episodes_and_never_gated(tmp_path, monkeypatch, name):
    # A frame-checked truth file of episodes ({"<file>": first card}, "_" keys are notes): each file is read with the
    # episode tail, judged against its own truth like any set, and reported beside the gate but never in it -- one
    # wrong answer here must not fail Q4's gate, and a set that isn't asked for isn't read.
    run = _GateRun(tmp_path, monkeypatch)
    right, early = "/tv/R (2021)/Season 01/R - S01E01.mkv", "/tv/R (2021)/Season 01/R - S01E02.mkv"
    truth_file = tmp_path / "evidence" / ct.REGRESSION_SETS[name]
    truth_file.parent.mkdir(parents=True, exist_ok=True)
    truth_file.write_text(json.dumps({"_about": "frame checks", right: 5001.0, early: 5100.0}))
    monkeypatch.setitem(GATE_ANSWERS, right, 5003.0)
    monkeypatch.setitem(GATE_ANSWERS, early, 5070.0)  # 30 s before its first card
    summary, details, passed = run(sets=(name,))
    assert run.cache_calls == [(right, True), (early, True)]
    assert summary["sets"][name]["files"] == 2 and summary["sets"][name]["text"] == {"useful": 1, "wrong": 1}
    assert [(f["name"], f["truth"], f["text"]) for f in details[name]] == [
        ("R (2021) Season 01", 5001.0, 5003.0), ("R (2021) Season 01", 5100.0, 5070.0)
    ]  # fmt: skip
    assert summary["gate"] == {} and "rule_j_80" not in summary and passed is True
    assert set(details) == {name}


def test_a_file_gone_from_disk_is_left_out_of_every_row_and_named(tmp_path, monkeypatch):
    # Radarr replaced one of the 205 since its truth was taken: that file has nothing to read, so it leaves the set
    # (its gate counts the files that are there) and is named, never read or allowed to end the run.
    run = _GateRun(tmp_path, monkeypatch, gone=("/m/X2 (2011)/X2.mkv",))
    summary, details, _ = run(sets=("205",))
    assert [path for path, _ in run.cache_calls] == ["/m/X1 (2010)/X1.mkv"]
    assert summary["gone"] == [{"set": "movie_credit_truth", "name": "X2 (2011)"}]
    assert summary["gate"]["205"]["files"] == 1 and [f["file"] for f in details["movie_credit_truth"]] == [
        "/m/X1 (2010)/X1.mkv"
    ]


def test_the_helpers_are_stopped_once_even_when_a_file_fails(tmp_path, monkeypatch):
    run = _GateRun(tmp_path, monkeypatch, failing_path="/m/X1 (2010)/X1.mkv")
    with pytest.raises(RuntimeError, match="decode failed"):
        run()
    assert run.closed == 1
    ok = _GateRun(tmp_path / "again", monkeypatch)
    ok()
    assert ok.closed == 1


def test_the_helpers_are_stopped_when_the_cache_cannot_be_made(tmp_path, monkeypatch):
    run = _GateRun(tmp_path, monkeypatch, cache_error=ValueError("the credits text cache must not live under /data*"))
    with pytest.raises(ValueError, match="/data"):
        run()
    assert run.closed == 1 and run.cache_calls == []


def test_a_corrupt_evidence_file_keeps_its_traceback(tmp_path, monkeypatch):
    # json.JSONDecodeError is a ValueError: the unknown-set exit must not swallow it into a one-line message.
    from tools.markers_eval import __main__ as cli

    _GateRun(tmp_path, monkeypatch)
    (tmp_path / "evidence/credits/adjudicated.json").write_text("{not json")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/ff")
    with pytest.raises(json.JSONDecodeError):
        cli.main(["credits-text", "--sets", "80", "--cache", str(tmp_path / "cache"),
                  "--plex-baseline", str(tmp_path / "b.json")])  # fmt: skip


def test_a_failed_sheet_is_logged_and_the_run_goes_on(tmp_path, loguru_caplog):
    out = tmp_path / "movies40-abc.jpg"
    timeout = subprocess.TimeoutExpired(["ffmpeg"], ct.SHEET_TIMEOUT_S)
    with patch.object(ct.subprocess, "run", side_effect=timeout) as run:
        ct._write_sheet("/ff", "/m/A (2001)/A.mkv", 5702.0, out)
    assert run.call_args.args[0] == ct.sheet_command("/ff", "/m/A (2001)/A.mkv", 5702.0, str(out))
    assert run.call_args.kwargs["timeout"] == ct.SHEET_TIMEOUT_S
    with patch.object(ct.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
        ct._write_sheet("/ff", "/m/A (2001)/A.mkv", 5702.0, out)
    assert "TimeoutExpired" in loguru_caplog.text and "ffmpeg exited 1" in loguru_caplog.text
    assert "/m/" not in loguru_caplog.text


@pytest.mark.parametrize(
    ("spec", "constant", "values"),
    [
        ("rule_j.BAND_TOLERANCE_PX=16,24,32,40", "BAND_TOLERANCE_PX", (16.0, 24.0, 32.0, 40.0)),
        ("rule_j.OVERLAY_LEAST=3,4,5", "OVERLAY_LEAST", (3, 4, 5)),
        (" rule_j.ROLL_TEXT_SHARE =0.4,0.5", "ROLL_TEXT_SHARE", (0.4, 0.5)),
    ],
)
def test_a_sweep_spec_is_read_with_the_constants_own_type(spec, constant, values):
    # An int constant read as a float would make `OVERLAY_LEAST` 4.0 and compare differently to `len(story)`.
    assert ct.parse_sweep(spec) == (constant, values)
    assert all(type(v) is type(getattr(rule_j, constant)) for v in values)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("rule_j.NO_SUCH_THING=1", "no constant named NO_SUCH_THING"),
        ("detector.BAND_TOLERANCE_PX=16", "wants rule_j."),
        ("rule_j.BAND_TOLERANCE_PX", "wants rule_j."),
        ("rule_j.BAND_TOLERANCE_PX=", "wants rule_j."),
        ("rule_j.RULE_J=1", "only numbers can be swept"),
        ("rule_j.OVERLAY_LEAST=4.5", "takes int values"),
        ("rule_j.band_of=1", "no constant named band_of"),
    ],
)
def test_a_sweep_spec_that_names_nothing_sweepable_is_refused(spec, message):
    with pytest.raises(ct.SweepError, match=re.escape(message)):
        ct.parse_sweep(spec)


@pytest.mark.parametrize(
    ("body", "escapes"),
    [
        # The shape that was actually published: `in_band` bound the tolerance as a default, so the monkeypatch
        # reached `same_roll` and nothing else and `in_band` read the shipped band in every cell.
        (
            "def in_band(row, band, tol=X):\n    return tol\n",
            ["rule_j.in_band() binds it as a default argument, read once at import"],
        ),
        ("def f(*, tol=X):\n    return tol\n", ["rule_j.f() binds it as a default argument, read once at import"]),
        (
            "f = lambda tol=X: tol\n",
            ["rule_j.<lambda>() binds it as a default argument, read once at import",
             "rule_j.f is built from it at import"],
        ),  # fmt: skip
        ("import functools\n@functools.lru_cache(maxsize=X)\ndef f():\n    return 1\n",
         ["rule_j.f's decorator is given it at import"]),  # fmt: skip
        ("def deco(n):\n    return lambda c: c\n@deco(X)\nclass P:\n    pass\n",
         ["rule_j.P's decorator is given it at import"]),  # fmt: skip
        ("HALF = X / 2\n", ["rule_j.HALF is built from it at import"]),
        ("HALF: float = X / 2\n", ["rule_j.HALF is built from it at import"]),
        ("HALF = 1.0\nHALF += X\n", ["rule_j.HALF is built from it at import"]),
        ("if (HALF := X / 2):\n    pass\n", ["rule_j.HALF is built from it at import"]),
        # A dataclass field default -- what `RuleParams.gap_s` would be if it were written from a constant.
        ("class Params:\n    tol: float = X\n", ["rule_j.tol is built from it at import"]),
        # Every block that runs at import, not only the three an earlier version of the walk descended into.
        ("if True:\n    HALF = X\n", ["rule_j.HALF is built from it at import"]),
        ("try:\n    HALF = X\nexcept Exception:\n    HALF = 0.0\n", ["rule_j.HALF is built from it at import"]),
        ("try:\n    import os\nexcept ImportError:\n    HALF = X\n", ["rule_j.HALF is built from it at import"]),
        ("V = 1\nmatch V:\n    case 1:\n        HALF = X\n", ["rule_j.HALF is built from it at import"]),
        ("import contextlib\nwith contextlib.suppress(Exception):\n    HALF = X\n",
         ["rule_j.HALF is built from it at import"]),  # fmt: skip
        ("for _ in range(1):\n    HALF = X\n", ["rule_j.HALF is built from it at import"]),
        # Read in a function body: a fresh lookup every call, which a patch does reach.
        ("def same_roll(rows):\n    return X\n", []),
        ("def reach_back(p):\n    limit = min(p, 1.5 * X)\n    return limit\n", []),
    ],
    ids=["default-arg", "kwonly-default", "lambda-default", "decorator-arg", "class-decorator", "module-assign",
         "module-annassign", "module-augassign", "module-walrus", "class-field", "nested-if", "try-body",
         "except-handler", "match-case", "with-body", "for-body", "read-in-a-body", "local-built-in-a-body"],
)  # fmt: skip
def test_every_way_a_constant_escapes_a_patch_is_reported(body, escapes):
    assert ct.unreachable_by_patch("X", "X = 32.0\n" + body) == escapes


def test_every_swept_constant_is_reachable_in_the_tree_as_it_stands():
    # The nine the docs' sweeps cover: if one of them ever grows an import-time copy, the sweep must refuse rather
    # than publish a table of the shipped value.
    swept = ("BAND_TOLERANCE_PX", "ROLL_TEXT_SHARE", "OVERLAY_IOU", "OVERLAY_SPAN_SHARE", "OVERLAY_KEYFRAME_SHARE",
             "OVERLAY_LEAST", "OVERLAY_CONTAINMENT", "ANCHOR_SPACING_FACTOR", "TEXT_ALL_THROUGH_SHARE")  # fmt: skip
    assert {name: ct.unreachable_by_patch(name) for name in swept} == dict.fromkeys(swept, [])


def test_another_module_reading_it_off_rule_j_at_import_is_reported(tmp_path, monkeypatch):
    # `def f(tol=rule_j.BAND_TOLERANCE_PX)` elsewhere binds the value at import and leaves no attribute of that name
    # behind, so the `from ... import` check alone doesn't see it.
    module = tmp_path / "pretend.py"
    module.write_text("from media_preview_generator.markers.credits import rule_j\n\n\n"
                      "def widen(px, tol=rule_j.BAND_TOLERANCE_PX):\n    return px + tol\n")  # fmt: skip
    theirs = SimpleNamespace(rule_j=ct.rule_j, __file__=str(module))
    monkeypatch.setitem(sys.modules, "media_preview_generator.markers.pretend", theirs)
    assert ct.unreachable_by_patch("BAND_TOLERANCE_PX") == [
        "media_preview_generator.markers.pretend.widen() binds it as a default argument, read once at import"
    ]


def test_a_constant_another_module_imported_by_value_is_reported(monkeypatch):
    # `from .rule_j import BAND_TOLERANCE_PX` keeps a copy the sweep's setattr never touches.
    copy = SimpleNamespace(BAND_TOLERANCE_PX=32.0)
    monkeypatch.setitem(sys.modules, "media_preview_generator.markers.pretend", copy)
    assert ct.unreachable_by_patch("BAND_TOLERANCE_PX") == [
        "media_preview_generator.markers.pretend has its own BAND_TOLERANCE_PX: it imported the value, not the module"
    ]


@pytest.mark.parametrize(
    "constant",
    ["BAND_TOLERANCE_PX", "ROLL_TEXT_SHARE", "OVERLAY_IOU", "OVERLAY_SPAN_SHARE", "OVERLAY_KEYFRAME_SHARE",
     "OVERLAY_LEAST", "OVERLAY_CONTAINMENT", "ANCHOR_SPACING_FACTOR", "TEXT_ALL_THROUGH_SHARE"],
)  # fmt: skip
def test_every_constant_the_docs_sweep_is_reachable_by_a_patch_today(constant):
    assert ct.unreachable_by_patch(constant) == []


def test_a_sweep_refuses_before_it_measures_anything(tmp_path, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("the sweep must not start before its specs are checked")

    monkeypatch.setattr(ct, "_detection_on", never)
    monkeypatch.setattr(ct, "evidence_dir", never)
    for specs, message in ((["rule_j.NOPE=1"], "no constant named NOPE"), ([], "needs at least one")):
        with pytest.raises(ct.SweepError, match=message):
            ct.sweep_credits_text(decode="cpu", gpu_device="cuda:0", sets=("80",), specs=specs,
                                  cache_root=tmp_path, ffmpeg="/ff", ffprobe="/ffp", baseline_path=tmp_path / "b")  # fmt: skip
    with pytest.raises(ValueError, match="unknown set"):
        ct.sweep_credits_text(decode="cpu", gpu_device="cuda:0", sets=("90",),
                              specs=["rule_j.BAND_TOLERANCE_PX=16"], cache_root=tmp_path, ffmpeg="/ff",
                              ffprobe="/ffp", baseline_path=tmp_path / "b")  # fmt: skip


def test_a_sweep_of_a_constant_a_patch_would_not_reach_is_refused(tmp_path, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("the sweep must not start when a cell would measure the shipped value")

    monkeypatch.setattr(ct, "_detection_on", never)
    monkeypatch.setattr(ct, "unreachable_by_patch", lambda constant, source=None: [f"{constant} is bound at import"])
    with pytest.raises(ct.SweepError, match="BAND_TOLERANCE_PX is bound at import"):
        ct.sweep_credits_text(decode="cpu", gpu_device="cuda:0", sets=("80",),
                              specs=["rule_j.BAND_TOLERANCE_PX=16,32"], cache_root=tmp_path, ffmpeg="/ff",
                              ffprobe="/ffp", baseline_path=tmp_path / "b")  # fmt: skip


class _SweepRun:
    """``sweep_credits_text`` end to end with the app's own ``find_credits`` replaced by one that reads whichever
    constants rule J is set to right now, so a cell that never reached the rule shows up as an unmoved answer."""

    def __init__(self, tmp_path, monkeypatch, *, shared_file=False):
        self.asked, self.closed = [], 0
        evidence = tmp_path / "evidence"
        (evidence / "credits").mkdir(parents=True)
        sets = {name: list(rows) for name, rows in GATE_FILES.items()}
        if shared_file:
            # The 40 movies of the 80 are all in the 205: one file, two sets, one answer per cell.
            sets["movie_credit_truth"].append(sets["movies40"][0])
        for name, rows in {**sets, "adjudicated": {}}.items():
            (evidence / f"credits/{name}.json").write_text(json.dumps(rows))
        run = self

        class FakeProbes:
            def __init__(self, root, *, ffprobe):
                pass

            def probe(self, path):
                return MediaProbe(DUR, ())

        def find_credits(path, **kwargs):
            # The answer moves with the constant, so a cell served from a cache or run before the patch is visible.
            run.asked.append((path, rule_j.BAND_TOLERANCE_PX, rule_j.ROLL_TEXT_SHARE))
            return CreditsTextResult(GATE_ANSWERS[path] - rule_j.BAND_TOLERANCE_PX, None, (), (), ())

        monkeypatch.setattr(ct, "evidence_dir", lambda: evidence)
        monkeypatch.setattr(ct, "ProbeCache", FakeProbes)
        monkeypatch.setattr(ct, "find_credits", find_credits)
        monkeypatch.setattr(ct, "_detection_on", lambda decode, device: ct.TextDetection(
            lambda planes: [()] * len(planes), lambda: "cpu", lambda: setattr(run, "closed", run.closed + 1)
        ))  # fmt: skip
        monkeypatch.setattr(ct, "load_baseline", lambda path: {p: [PlexMarker("credits", int(s * 1000), DUR, True)]
                                                               for p, s in GATE_PLEX.items()})  # fmt: skip
        self.tmp_path = tmp_path

    def __call__(self, specs, sets=("80", "205")):
        return ct.sweep_credits_text(decode="cpu", gpu_device="cuda:0", sets=sets, specs=specs,
                                     cache_root=self.tmp_path / "cache", ffmpeg="/ff", ffprobe="/ffp",
                                     baseline_path=self.tmp_path / "b.json")  # fmt: skip


def test_every_cell_is_measured_with_its_own_value(tmp_path, monkeypatch):
    run = _SweepRun(tmp_path, monkeypatch)
    summary, cells = run(["rule_j.BAND_TOLERANCE_PX=16,32"])
    assert [cell.values for cell in cells] == [(("BAND_TOLERANCE_PX", 16.0),), (("BAND_TOLERANCE_PX", 32.0),)]
    assert [cell.shipped for cell in cells] == [False, True]
    # Five files per cell, each asked while the rule held that cell's value -- never the shipped one by default.
    assert {band for _path, band, _share in run.asked[:5]} == {16.0}
    assert {band for _path, band, _share in run.asked[5:]} == {32.0}
    assert summary["cells"] == 2 and summary["sets"] == ["205", "80"]


def test_a_cross_product_sweeps_every_pair(tmp_path, monkeypatch):
    run = _SweepRun(tmp_path, monkeypatch)
    _summary, cells = run(["rule_j.BAND_TOLERANCE_PX=16,32", "rule_j.ROLL_TEXT_SHARE=0.4,0.5"], sets=("80",))
    assert [cell.values for cell in cells] == [
        (("BAND_TOLERANCE_PX", 16.0), ("ROLL_TEXT_SHARE", 0.4)),
        (("BAND_TOLERANCE_PX", 16.0), ("ROLL_TEXT_SHARE", 0.5)),
        (("BAND_TOLERANCE_PX", 32.0), ("ROLL_TEXT_SHARE", 0.4)),
        (("BAND_TOLERANCE_PX", 32.0), ("ROLL_TEXT_SHARE", 0.5)),
    ]
    assert [cell.shipped for cell in cells] == [False, False, False, True]
    assert sorted({pair[1:] for pair in run.asked}) == [(16.0, 0.4), (16.0, 0.5), (32.0, 0.4), (32.0, 0.5)]


def test_the_constants_are_put_back_even_when_a_cell_raises(tmp_path, monkeypatch):
    run = _SweepRun(tmp_path, monkeypatch)
    before = (rule_j.BAND_TOLERANCE_PX, rule_j.ROLL_TEXT_SHARE)
    run(["rule_j.BAND_TOLERANCE_PX=16,32", "rule_j.ROLL_TEXT_SHARE=0.4"], sets=("80",))
    assert (rule_j.BAND_TOLERANCE_PX, rule_j.ROLL_TEXT_SHARE) == before
    assert run.closed == 1

    def blow_up(*args, **kwargs):
        raise RuntimeError("the card went away")

    monkeypatch.setattr(ct, "find_credits", blow_up)
    with pytest.raises(RuntimeError, match="the card went away"):
        run(["rule_j.BAND_TOLERANCE_PX=16"], sets=("80",))
    assert (rule_j.BAND_TOLERANCE_PX, rule_j.ROLL_TEXT_SHARE) == before


def test_the_sweep_table_carries_the_columns_the_docs_sweep_tables_do(tmp_path, monkeypatch):
    run = _SweepRun(tmp_path, monkeypatch)
    summary, cells = run(["rule_j.BAND_TOLERANCE_PX=16,32"])
    assert summary["table"].splitlines()[0] == (
        "| BAND_TOLERANCE_PX | 80: rule J / Medium useful / wrong | 205: Medium useful / wrong "
        "| 205: alone useful / wrong | decoded / reused |"
    )
    shipped = cells[1]
    assert summary["table"].splitlines()[3] == (
        f"| **32.0** | {shipped.rule.within_10s} / {shipped.rows['80'].medium['useful']} / "
        f"{shipped.rows['80'].medium['wrong']} | {shipped.rows['205'].medium['useful']} / "
        f"{shipped.rows['205'].medium['wrong']} | {shipped.rows['205'].text['useful']} / "
        f"{shipped.rows['205'].text['wrong']} | 0 / 0 |"
    )


def test_the_command_prints_the_table_and_exits_zero(tmp_path, monkeypatch, capsys):
    from tools.markers_eval import __main__ as cli

    seen = {}

    def sweep(**kwargs):
        seen.update(kwargs)
        return {"table": "| a |\n|---|\n| 1 |", "cells": 1}, []

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/ff")
    monkeypatch.setattr("tools.markers_eval.credits_text.sweep_credits_text", sweep)
    out = tmp_path / "sweep.json"
    assert cli.main(["credits-text", "--decode", "cpu", "--sets", "80",
                     "--sweep", "rule_j.BAND_TOLERANCE_PX=16,32", "--cache", str(tmp_path),
                     "--json", str(out)]) == 0  # fmt: skip
    # Every argument the command is responsible for building, not just the ones it copies: a wrong baseline or a
    # wrong ffprobe is exactly what would corrupt a published sweep table's useful / wrong columns.
    assert seen == {
        "decode": "cpu",
        "gpu_device": "cuda:0",
        "sets": ("80",),
        "specs": ["rule_j.BAND_TOLERANCE_PX=16,32"],
        "cache_root": tmp_path,
        "ffmpeg": "/ff",
        "ffprobe": cli.ffprobe_path_for("/ff"),
        "baseline_path": cli.evidence_dir() / cli.DEFAULT_BASELINE,
    }
    assert "| a |" in capsys.readouterr().out
    assert json.loads(out.read_text()) == {"cells": 1, "table": "| a |\n|---|\n| 1 |"}


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--online"], "--sweep cannot be combined with --online"),
        (["--sheets", "SHEETS"], "--sweep cannot be combined with --sheets"),
        (["--changed-since", "BEFORE"], "--sweep cannot be combined with --changed-since"),
        (
            ["--online", "--sheets", "SHEETS", "--changed-since", "BEFORE"],
            "--sweep cannot be combined with --changed-since, --online, --sheets",
        ),
    ],
    ids=["online", "sheets", "changed-since", "all-three"],
)
def test_a_sweep_refuses_the_flags_it_would_have_dropped(tmp_path, monkeypatch, flags, message):
    # A sweep reports cells, not one run: taking these and ignoring them would look like a diff that found nothing.
    from tools.markers_eval import __main__ as cli

    ran = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/ff")
    monkeypatch.setattr("tools.markers_eval.credits_text.sweep_credits_text", lambda **kwargs: ran.append(kwargs))
    (tmp_path / "before.json").write_text(json.dumps({"details": []}))
    filled = [str(tmp_path / "sheets") if f == "SHEETS" else str(tmp_path / "before.json") if f == "BEFORE" else f
              for f in flags]  # fmt: skip
    with pytest.raises(SystemExit) as exit_:
        cli.main(["credits-text", "--decode", "cpu", "--sets", "80", "--cache", str(tmp_path),
                  "--sweep", "rule_j.BAND_TOLERANCE_PX=16,32", *filled])  # fmt: skip
    assert str(exit_.value) == message
    assert ran == []


def test_a_file_in_both_sets_is_read_once_per_cell(tmp_path, monkeypatch):
    # Both sets must see the same answer for a shared file, and a cell must not pay for it twice.
    run = _SweepRun(tmp_path, monkeypatch, shared_file=True)
    _summary, cells = run(["rule_j.BAND_TOLERANCE_PX=16,32"])
    shared = GATE_FILES["movies40"][0]["file"]
    per_cell = [pair[0] for pair in run.asked].count(shared) / len(cells)
    assert per_cell == 1
    for cell in cells:
        rows = {row["file"]: row["text"] for row in cell.rows["80"].files}
        assert rows[shared] == {row["file"]: row["text"] for row in cell.rows["205"].files}[shared]
