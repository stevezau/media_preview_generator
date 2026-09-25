"""The reproduction gate on synthetic seasons: port == reference, drift reported, tally from truth; what the season
step's guards change; and the season step on an intro truth file."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.speed import FILM_FPS, PAL_FPS
from tools.markers_eval import fp3_reference
from tools.markers_eval.data import EvalEpisode
from tools.markers_eval.intros import reproduce


class _EndPictures:
    """A fake end-picture check: every cluster's pictures match unless its episode is in ``differ``."""

    def __init__(self, differ=()):
        self.differ = set(differ)
        self.asked: list[tuple[str, float]] = []

    def for_episode(self, target):
        def passes(candidate):
            self.asked.append((target, candidate.segment.start_s))
            return target not in self.differ

        return passes


ALIKE = _EndPictures()


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
    report = reproduce(_episodes(fps), points=fps.__getitem__, end_pictures=ALIKE)
    assert report.port_vs_reference == [] and report.drift == []
    assert report.tally.as_dict() == {"useful": 3, "wrong": 0, "missed": 0}
    assert (report.seasons, report.episodes) == (1, 3)


def test_stored_segments_that_differ_are_reported_as_drift_not_as_a_port_failure():
    fps = _fps(2)
    report = reproduce(_episodes(fps, stored_shift_s=3.0), points=fps.__getitem__, end_pictures=ALIKE)
    assert report.port_vs_reference == []
    assert len(report.drift) == 3 and {d["file"] for d in report.drift} == set(fps)


def test_a_port_that_disagrees_with_the_reference_fails_the_gate(monkeypatch):
    fps = _fps(3)
    from tools.markers_eval import intros

    monkeypatch.setattr(intros, "season_intros", lambda points: {f: None for f in points})
    report = reproduce(_episodes(fps), points=fps.__getitem__, end_pictures=ALIKE)
    assert len(report.port_vs_reference) == 3
    assert report.passed is False


def test_passed_needs_the_spec_numbers(monkeypatch):
    fps = _fps(4)
    report = reproduce(_episodes(fps), points=fps.__getitem__, end_pictures=ALIKE)
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

    lists = reproduce(episodes, points=points, end_pictures=ALIKE)
    assert sorted(asked) == eval_files and lists.drift == []
    asked.clear()
    full = reproduce(episodes, points=points, end_pictures=ALIKE, full_folder=True)
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
    report = reproduce(episodes, points=fps.__getitem__, end_pictures=ALIKE)
    assert report.port_vs_reference == []
    assert report.matcher_tally.as_dict() == {"useful": 0, "wrong": 3, "missed": 0}
    assert report.tally.as_dict() == {"useful": 0, "wrong": 0, "missed": 3}
    assert [(d["file"], d["verdict"]) for d in report.silence_dropped] == [(f, "wrong") for f in sorted(fps)]


def test_a_skipped_pair_is_reported_with_the_intro_length_runs_the_matcher_finds(monkeypatch):
    from tools.markers_eval import intros

    fps = _fps(6)
    monkeypatch.setattr(intros, "holds_no_intro", lambda a, b: True)
    report = reproduce(_episodes(fps), points=fps.__getitem__, end_pictures=ALIKE)
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
    from tools.markers_eval.intros import SPEC_SEASON_STEP, ReproductionReport
    from tools.markers_eval.score import Tally

    report = ReproductionReport(tally=Tally(*SPEC_SEASON_STEP), skipped_pairs=skipped, silence_dropped=dropped)
    assert report.passed is passed


@pytest.mark.parametrize(
    ("tally", "passed"),
    [((91, 12, 15), True), ((92, 11, 15), True), ((90, 12, 16), False), ((91, 13, 14), False)],
    ids=["the-spec", "better", "one-useful-fewer", "the-matcher-alone-s-wrong-count"],
)
def test_the_gate_holds_the_season_step_to_its_guarded_numbers(tally, passed):
    # 91 / 12 / 15 on the 118 (§14 2026-09-24); the matcher alone's 13 wrong no longer passes.
    from tools.markers_eval.intros import ReproductionReport
    from tools.markers_eval.score import Tally

    assert ReproductionReport(tally=Tally(*tally)).passed is passed


def test_the_summary_never_reports_a_port_comparison_it_skipped(monkeypatch, capsys):
    # --no-reference skips the gate's first check; "port_vs_reference": 0 would read as "the port matches it".
    import json
    from types import SimpleNamespace

    from tools.markers_eval import __main__ as cli
    from tools.markers_eval.intros import SPEC_SEASON_STEP, ReproductionReport
    from tools.markers_eval.score import Tally

    asked = []

    wired = []

    def fake_reproduce(episodes, *, points, end_pictures, with_reference, full_folder, speed, retimed):
        asked.append(with_reference)
        wired.append((speed, retimed))
        return ReproductionReport(tally=Tally(*SPEC_SEASON_STEP))

    monkeypatch.setattr(cli, "_cache", lambda args: SimpleNamespace(points=None, speed="speed", retimed="retimed"))
    monkeypatch.setattr(cli, "load_v3_results", lambda: [])
    monkeypatch.setattr(cli, "reproduce", fake_reproduce)
    assert cli.main(["reproduce", "--no-reference"]) == 0
    assert json.loads(capsys.readouterr().out)["port_vs_reference"] == "not checked"
    assert cli.main(["reproduce"]) == 0
    assert json.loads(capsys.readouterr().out)["port_vs_reference"] == 0
    assert asked == [False, True]
    assert wired == [("speed", "retimed")] * 2  # the step matches at one speed, as the app does


def test_an_early_intro_whose_end_picture_differs_is_passed_over_and_reported():
    fps = _fps(9)  # the shared intro starts at 13.6-15.9 s: every episode's is checked
    files = sorted(fps)
    differ = _EndPictures(differ={files[0]})
    report = reproduce(_episodes(fps), points=fps.__getitem__, end_pictures=differ)
    assert {target for target, _ in differ.asked} == set(files)
    assert report.tally.as_dict() == {"useful": 2, "wrong": 0, "missed": 1}
    (changed,) = report.guards_changed
    assert (changed["file"], changed["matcher_verdict"], changed["step"], changed["step_verdict"]) == (
        files[0],
        "useful",
        None,
        "missed",
    )


def _truth_season(tmp_path, episodes=4):
    folder = tmp_path / "Show" / "Season 01"
    folder.mkdir(parents=True)
    fps = {str(folder / Path(k).name): v for k, v in _fps(10, episodes=episodes).items()}
    for f in fps:
        Path(f).touch()
    return fps


def test_season_truth_matches_each_file_with_its_whole_season_and_counts_an_answer_where_there_is_none_as_wrong(
    tmp_path,
):
    from tools.markers_eval.intros import season_truth

    fps = _truth_season(tmp_path)
    files = sorted(fps)
    truth = {
        files[0]: (110 * POINT_S, 309 * POINT_S),
        files[1]: (0.0, 5.0),  # a truth the planted intro doesn't meet
        files[2]: None,  # "no intro": an answer is wrong
    }
    asked = []

    def points(f):
        asked.append(f)
        return fps[f]

    report = season_truth(truth, points=points, end_pictures=ALIKE)
    assert sorted(set(asked)) == files  # the fourth file, not in the truth, is still matched with
    assert report.tally.as_dict() == {"useful": 1, "wrong": 2, "missed": 0} and report.none_ok == 0
    assert report.details[files[2]]["verdict"] == "wrong" and report.details[files[2]]["answer"][2] == 3


def test_season_truth_counts_no_answer_for_no_intro_apart(tmp_path):
    from tools.markers_eval.intros import season_truth

    fps = _truth_season(tmp_path, episodes=3)
    files = sorted(fps)
    report = season_truth({files[0]: None}, points=fps.__getitem__, end_pictures=_EndPictures(differ={files[0]}))
    assert report.tally.as_dict() == {"useful": 0, "wrong": 0, "missed": 0} and report.none_ok == 1


def _mixed_speed_season(tmp_path):
    """Four episodes, the fourth a 25 fps release: its own fingerprint holds a sped-up theme nothing else matches, and
    its fingerprint slowed to film speed holds the others' theme at the points ``_fps`` planted it."""
    fps = _truth_season(tmp_path, episodes=4)
    files = sorted(fps)
    fast = files[3]
    native = dict(fps)
    native[fast] = fps[fast].copy()
    native[fast][140:340] = np.random.default_rng(99).integers(0, 2**32, size=200, dtype=np.uint64).astype("<u4")
    speeds = {f: FILM_FPS for f in files[:3]} | {fast: PAL_FPS}
    return native, fps, speeds, fast


def test_season_truth_matches_a_file_at_another_speed_on_its_retimed_fingerprint(tmp_path):
    from tools.markers_eval.intros import season_truth

    native, film_speed, speeds, fast = _mixed_speed_season(tmp_path)
    retimes = []

    def retimed(path, factor):
        retimes.append((path, factor))
        return film_speed[path]

    factor = FILM_FPS / PAL_FPS
    truth = {fast: (140 * POINT_S * factor, 339 * POINT_S * factor)}
    report = season_truth(truth, points=native.__getitem__, end_pictures=ALIKE, speed=speeds.get, retimed=retimed)
    assert retimes == [(fast, pytest.approx(factor))]
    assert report.tally.as_dict() == {"useful": 1, "wrong": 0, "missed": 0}
    start, end, support = report.details[fast]["answer"]
    assert (start, end) == (pytest.approx(truth[fast][0], abs=POINT_S), pytest.approx(truth[fast][1], abs=POINT_S))
    assert support == 3
    # Without the speeds the same season finds nothing for it: what the app did before.
    before = season_truth(truth, points=native.__getitem__, end_pictures=ALIKE)
    assert before.tally.as_dict() == {"useful": 0, "wrong": 0, "missed": 1}


def test_season_truth_matches_a_re_encode_whose_audio_was_never_sped_up_as_it_plays(tmp_path):
    # RuPaul's Drag Race UK S08E04: a 23.976 fps release of a 25 fps show, its audio untouched. The app hears which
    # speed a file's audio plays at (season.clock_by_audio); the harness must too.
    from tools.markers_eval.intros import season_truth

    fps = _truth_season(tmp_path, episodes=4)
    files = sorted(fps)
    reencode = files[3]
    speeds = {f: PAL_FPS for f in files[:3]} | {reencode: FILM_FPS}
    stretched = np.random.default_rng(7).integers(0, 2**32, size=len(fps[reencode]), dtype=np.uint64).astype("<u4")
    retimes = []

    def retimed(path, factor):
        retimes.append((path, factor))
        return stretched  # sped up, the audio matches nothing

    truth = {reencode: (140 * POINT_S, 339 * POINT_S)}
    report = season_truth(truth, points=fps.__getitem__, end_pictures=ALIKE, speed=speeds.get, retimed=retimed)
    assert retimes == [(reencode, pytest.approx(PAL_FPS / FILM_FPS))]
    assert report.tally.as_dict() == {"useful": 1, "wrong": 0, "missed": 0}
    start, end, support = report.details[reencode]["answer"]
    assert (start, end) == (
        pytest.approx(truth[reencode][0], abs=POINT_S),
        pytest.approx(truth[reencode][1], abs=POINT_S),
    )
    assert support == 3


def test_a_film_rate_episode_counts_the_retimed_one_as_support_in_the_gate(tmp_path):
    native, film_speed, speeds, fast = _mixed_speed_season(tmp_path)
    files = sorted(native)
    episodes = [EvalEpisode(str(Path(files[0]).parent), files[0], (110 * POINT_S, 309 * POINT_S), None, None)]
    report = reproduce(episodes, points=native.__getitem__, end_pictures=ALIKE, with_reference=False,
                       full_folder=True, speed=speeds.get, retimed=lambda path, factor: film_speed[path])  # fmt: skip
    assert report.tally.as_dict() == {"useful": 1, "wrong": 0, "missed": 0}


@pytest.mark.parametrize(("expect", "code"), [(None, 0), ("1,1", 0), ("2,1", 1), ("1,0", 1)])
def test_the_season_truth_command_exits_on_its_expected_numbers(tmp_path, monkeypatch, capsys, expect, code):
    import json
    from types import SimpleNamespace

    from tools.markers_eval import __main__ as cli

    fps = _truth_season(tmp_path, episodes=3)
    files = sorted(fps)
    truth_file = tmp_path / "truth.json"
    truth_file.write_text(json.dumps({files[0]: [110 * POINT_S, 309 * POINT_S], files[1]: None}))
    cache = SimpleNamespace(points=fps.__getitem__, speed=lambda path: None, retimed=None)
    monkeypatch.setattr(cli, "_cache", lambda args: cache)
    monkeypatch.setattr(cli, "_end_pictures", lambda args: ALIKE)
    argv = ["season-truth", "--truth", str(truth_file)] + (["--expect", expect] if expect else [])
    assert cli.main(argv) == code
    summary = json.loads(capsys.readouterr().out)
    assert summary["tally"] == {"useful": 1, "wrong": 1, "missed": 0} and summary["none_ok"] == 0
