"""The reproduction gate on synthetic seasons: port == reference, drift reported, tally from truth."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S
from tools.markers_eval import fp3_reference
from tools.markers_eval.data import EvalEpisode
from tools.markers_eval.intros import reproduce


def _fps(seed: int, episodes: int = 3):
    rng = np.random.default_rng(seed)
    intro = rng.integers(0, 2**32, size=200, dtype=np.uint64).astype("<u4")
    out = {}
    for e in range(1, episodes + 1):
        body = rng.integers(0, 2**32, size=900, dtype=np.uint64).astype("<u4")
        body[100 + 10 * e : 300 + 10 * e] = intro
        out[f"/eval/Show/Season 01/Show - S01E{e:02d}.mkv"] = body
    return out


def _episodes(fps, *, stored_shift_s: float = 0.0):
    files = sorted(fps)
    ref = fp3_reference.analyse_points(fps, files)
    eps = []
    for f in files:
        seg = ref[f]["segment"]
        stored = (seg[0] + stored_shift_s, seg[1] + stored_shift_s, seg[2]) if seg else None
        e = int(f[-6:-4])
        truth = ((100 + 10 * e) * POINT_S, (299 + 10 * e) * POINT_S)
        eps.append(EvalEpisode("/eval/Show/Season 01", f, truth, None, stored))
    return eps


def test_identical_segments_pass_with_every_episode_useful():
    fps = _fps(1)
    report = reproduce(_episodes(fps), points=fps.__getitem__)
    assert report.port_vs_reference == [] and report.drift == []
    assert report.tally.as_dict() == {"useful": 3, "wrong": 0, "missed": 0}
    assert (report.seasons, report.episodes) == (1, 3)


def test_stored_segments_that_differ_are_reported_as_drift_not_as_a_port_failure():
    fps = _fps(2)
    report = reproduce(_episodes(fps, stored_shift_s=3.0), points=fps.__getitem__)
    assert report.port_vs_reference == []
    assert len(report.drift) == 3 and {d["file"] for d in report.drift} == set(fps)


def test_a_port_that_disagrees_with_the_reference_fails_the_gate(monkeypatch):
    fps = _fps(3)
    from tools.markers_eval import intros

    monkeypatch.setattr(intros, "season_intros", lambda points: {f: None for f in points})
    report = reproduce(_episodes(fps), points=fps.__getitem__)
    assert len(report.port_vs_reference) == 3
    assert report.passed is False


def test_passed_needs_the_spec_numbers(monkeypatch):
    fps = _fps(4)
    report = reproduce(_episodes(fps), points=fps.__getitem__)
    assert report.passed is False  # 3 useful < 91: a synthetic season can't pass the real-data gate


def test_full_folder_matches_every_episode_file_in_the_folder_not_just_the_eval_lists(tmp_path):
    folder = tmp_path / "Show" / "Season 01"
    folder.mkdir(parents=True)
    fps = {str(folder / Path(k).name): v for k, v in _fps(5, episodes=4).items()}
    for f in [*fps, folder / "Show - S01E01-trailer.mkv", folder / "Show - S01E01.nfo"]:
        Path(f).touch()
    eval_files = sorted(fps)[:2]
    episodes = _episodes({f: fps[f] for f in eval_files})
    asked = []

    def points(f):
        asked.append(f)
        return fps[f]

    lists = reproduce(episodes, points=points)
    assert sorted(asked) == eval_files and lists.drift == []
    asked.clear()
    full = reproduce(episodes, points=points, full_folder=True)
    assert sorted(asked) == sorted(fps)
    assert full.port_vs_reference == []
    assert full.tally.as_dict() == {"useful": 2, "wrong": 0, "missed": 0}
    # Stored answers came from the 2-file group (support 1); the whole folder gives 3 supporting episodes.
    assert [d["stored"][2] for d in full.drift] == [1, 1] and [d["now"][2] for d in full.drift] == [3, 3]
    assert (full.seasons, full.episodes) == (1, 2)


def test_the_gate_judges_the_season_step_and_reports_what_the_silence_guard_dropped():
    from media_preview_generator.markers.audio.season import SILENCE_POINT

    rng = np.random.default_rng(8)
    fps = {}
    for e, at in ((1, 200), (2, 450), (3, 700)):
        body = rng.integers(0, 2**32, size=2_000, dtype=np.uint64).astype("<u4")
        body[at : at + 200] = SILENCE_POINT  # a shared 25 s silence and nothing else in common
        fps[f"/eval/Show/Season 01/Show - S01E{e:02d}.mkv"] = body
    episodes = [EvalEpisode("/eval/Show/Season 01", f, (10.0, 40.0), None, None) for f in sorted(fps)]
    report = reproduce(episodes, points=fps.__getitem__)
    assert report.port_vs_reference == []
    assert report.matcher_tally.as_dict() == {"useful": 0, "wrong": 3, "missed": 0}
    assert report.tally.as_dict() == {"useful": 0, "wrong": 0, "missed": 3}
    assert [(d["file"], d["verdict"]) for d in report.silence_dropped] == [(f, "wrong") for f in sorted(fps)]


def test_a_skipped_pair_is_reported_with_the_intro_length_runs_the_matcher_finds(monkeypatch):
    from tools.markers_eval import intros

    fps = _fps(6)
    monkeypatch.setattr(intros, "holds_no_intro", lambda a, b: True)
    report = reproduce(_episodes(fps), points=fps.__getitem__)
    files = sorted(fps)
    assert [(p["a"], p["b"]) for p in report.skipped_pairs] == [(files[0], files[1]), (files[0], files[2]),
                                                                (files[1], files[2])]  # fmt: skip
    assert all(p["intro_length_runs"] >= 1 for p in report.skipped_pairs)
    assert report.matcher_tally.as_dict()["useful"] == 3 and report.tally.as_dict()["missed"] == 3


@pytest.mark.parametrize(
    ("skipped", "dropped", "passed"),
    [
        ([], [], True),
        ([{"intro_length_runs": 0}], [{"verdict": "wrong"}, {"verdict": None}], True),
        ([{"intro_length_runs": 1}], [], False),  # skipping lost something the matcher could find
        ([], [{"verdict": "useful"}], False),  # the silence guard lost a useful answer
    ],
)
def test_the_gate_refuses_skipped_pairs_with_runs_and_useful_answers_the_guard_dropped(skipped, dropped, passed):
    from tools.markers_eval.intros import SPEC_V3, ReproductionReport
    from tools.markers_eval.score import Tally

    report = ReproductionReport(tally=Tally(*SPEC_V3), skipped_pairs=skipped, silence_dropped=dropped)
    assert report.passed is passed


def test_the_summary_never_reports_a_port_comparison_it_skipped(monkeypatch, capsys):
    # --no-reference skips the gate's first check; "port_vs_reference": 0 would read as "the port matches it".
    import json
    from types import SimpleNamespace

    from tools.markers_eval import __main__ as cli
    from tools.markers_eval.intros import SPEC_V3, ReproductionReport
    from tools.markers_eval.score import Tally

    asked = []

    def fake_reproduce(episodes, *, points, with_reference, full_folder):
        asked.append(with_reference)
        return ReproductionReport(tally=Tally(*SPEC_V3))

    monkeypatch.setattr(cli, "_cache", lambda args: SimpleNamespace(points=None))
    monkeypatch.setattr(cli, "load_v3_results", lambda: [])
    monkeypatch.setattr(cli, "reproduce", fake_reproduce)
    assert cli.main(["reproduce", "--no-reference"]) == 0
    assert json.loads(capsys.readouterr().out)["port_vs_reference"] == "not checked"
    assert cli.main(["reproduce"]) == 0
    assert json.loads(capsys.readouterr().out)["port_vs_reference"] == 0
    assert asked == [False, True]
