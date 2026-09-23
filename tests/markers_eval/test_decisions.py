from pathlib import Path

import numpy as np
import pytest

from media_preview_generator.markers import decide as decide_module
from media_preview_generator.markers.models import SERVER_SOURCES, Source
from tools.markers_eval.data import EvalEpisode
from tools.markers_eval.decisions import DecisionRows, compare_with_plex, g3_differences, g3_rule, season_segments
from tools.markers_eval.plex import PlexMarker
from tools.markers_eval.score import Tally


def _ep(name, truth=(60.0, 90.0)):
    return EvalEpisode(season="/tv/S/Season 01", file=f"/tv/S/Season 01/S - S01E0{name[-1]}.mkv", truth_intro=truth,
                       truth_credits=None, v3_segment=None, duration_s=1320.0)  # fmt: skip


def test_rows_for_plex_audio_high_and_medium_with_g3_on_and_off():
    e1, e2, e3 = _ep("e1"), _ep("e2"), _ep("e3")
    segments = {e1.file: (60.2, 89.8, 2, 2), e2.file: (60.0, 90.0, 2, 2), e3.file: None}
    baseline = {
        e1.file: [PlexMarker("intro", 60_000, 90_500, False)],  # agrees with audio
        e2.file: [PlexMarker("intro", 140_000, 170_000, False)],  # Plex wrong, disagrees
        e3.file: [PlexMarker("intro", 61_000, 90_000, False)],  # Plex alone
    }
    on = compare_with_plex([e1, e2, e3], segments, baseline)
    assert on.plex.as_dict() == {"useful": 2, "wrong": 1, "missed": 0}
    assert on.audio.as_dict() == {"useful": 2, "wrong": 0, "missed": 1}
    # Ruling G3 (shipped): season audio and a server's own marker never make two agreeing sources, so High decides
    # nothing. At Medium an agreeing Plex marker doesn't hold season audio back (2026-09-24): e1 is decided by audio
    # alone; e2's Plex marker disagrees, and e3 has no audio answer.
    assert on.high.as_dict() == {"useful": 0, "wrong": 0, "missed": 3}
    assert on.medium.as_dict() == {"useful": 1, "wrong": 0, "missed": 2}
    e1_on = next(row for row in on.episodes if row["file"] == e1.file)
    assert (e1_on["medium_segment"], e1_on["medium_reason"]) == ((60.2, 89.8), "single source (season_audio)")
    off = compare_with_plex([e1, e2, e3], segments, baseline, g3=False)
    assert off.plex.as_dict() == on.plex.as_dict() and off.audio.as_dict() == on.audio.as_dict()
    assert off.high.as_dict() == off.medium.as_dict() == {"useful": 1, "wrong": 0, "missed": 2}
    e1_off = next(row for row in off.episodes if row["file"] == e1.file)
    assert e1_off["high"] == "useful" and e1_off["high_segment"] == (60.2, 89.8)  # audio's end, the later start
    assert e1_off["high_reason"] == "sources agree: season_audio, server_markers"
    assert on.gate() is False and off.gate() is False  # Medium isn't as useful as Plex here


def test_the_gate_needs_medium_as_useful_as_plex_and_no_more_wrong():
    e1, e2, e3 = _ep("e1"), _ep("e2"), _ep("e3")
    segments = {e1.file: (60.2, 89.8, 3, 3), e2.file: (60.0, 90.0, 3, 3), e3.file: (59.5, 90.2, 3, 3)}
    baseline = {
        e1.file: [PlexMarker("intro", 60_000, 90_500, False)],
        e2.file: [PlexMarker("intro", 140_000, 170_000, False)],
        e3.file: [],
    }
    on = compare_with_plex([e1, e2, e3], segments, baseline)
    off = compare_with_plex([e1, e2, e3], segments, baseline, g3=False)
    assert on.plex.as_dict() == {"useful": 1, "wrong": 1, "missed": 1}
    assert on.audio.as_dict() == {"useful": 3, "wrong": 0, "missed": 0}
    # e3: season audio alone decides an intro (owner 2026-09-24, overriding R2), and so does e1: Plex's agreeing marker
    # no longer holds it back. Only High still needs G3 off for e1.
    assert on.medium.as_dict() == off.medium.as_dict() == {"useful": 2, "wrong": 0, "missed": 1}
    # With R2 there were none of e3's, and G3 on failed the gate here (Medium 0 useful against Plex's 1).
    assert (on.gate(), off.gate()) == (True, True)
    assert [(d["file"], d["g3_off"]) for d in g3_differences(on, off)] == [(e1.file, "useful")]


def test_wrong_answers_that_skip_story_are_counted_per_row():
    e1, e2, e3 = _ep("e1"), _ep("e2"), _ep("e3")
    baseline = {
        e1.file: [PlexMarker("intro", 60_000, 80_000, False)],  # ends 10 s early: shows more
        e2.file: [PlexMarker("intro", 60_000, 99_000, False)],  # ends 9 s late: skips story
        e3.file: [PlexMarker("intro", 44_000, 90_000, False)],  # starts 16 s early: skips story
    }
    rows = compare_with_plex([e1, e2, e3], {}, baseline)
    assert rows.plex.as_dict() == {"useful": 0, "wrong": 3, "missed": 0}
    assert rows.skips_story["plex"] == 2


@pytest.mark.parametrize(
    ("medium", "high", "passed"),
    [
        (Tally(5, 2, 0), Tally(4, 2, 0), True),
        (Tally(4, 2, 0), Tally(4, 2, 0), False),  # Medium less useful than Plex
        (Tally(9, 3, 0), Tally(4, 2, 0), False),  # Medium more wrong than Plex
        (Tally(5, 2, 0), Tally(4, 3, 0), False),  # High more wrong than Plex
    ],
)
def test_gate_cells(medium, high, passed):
    assert DecisionRows(plex=Tally(5, 2, 0), medium=medium, high=high).gate() is passed


def test_g3_rule_off_is_scoped_to_the_block():
    shipped = decide_module._AUDIO_OR_SERVER
    assert Source.SEASON_AUDIO in shipped
    with pytest.raises(RuntimeError), g3_rule(False):
        assert decide_module._AUDIO_OR_SERVER == SERVER_SOURCES
        raise RuntimeError
    assert decide_module._AUDIO_OR_SERVER is shipped
    with g3_rule(True):
        assert decide_module._AUDIO_OR_SERVER is shipped


def _fps(seed, episodes):
    rng = np.random.default_rng(seed)
    intro = rng.integers(0, 2**32, size=200, dtype=np.uint64).astype("<u4")
    out = {}
    for e in range(1, episodes + 1):
        body = rng.integers(0, 2**32, size=900, dtype=np.uint64).astype("<u4")
        body[100 + 10 * e : 300 + 10 * e] = intro
        out[e] = body
    return out


def test_season_segments_eval_lists_and_full_folder(tmp_path):
    folder = tmp_path / "S" / "Season 01"
    folder.mkdir(parents=True)
    fps = {str(folder / f"S - S01E{e:02d}.mkv"): v for e, v in _fps(7, 4).items()}
    for f in fps:
        Path(f).touch()
    eval_files = sorted(fps)[:2]
    eps = [EvalEpisode(str(folder), f, (0.0, 1.0), None, None, 1320.0) for f in eval_files]
    asked = []

    def points(f):
        asked.append(f)
        return fps[f]

    lists = season_segments(eps, points=points, full_folder=False)
    assert sorted(asked) == eval_files
    assert [lists[f][2:] for f in eval_files] == [(1, 1), (1, 1)]  # support / others within the eval's own list
    asked.clear()
    full = season_segments(eps, points=points, full_folder=True)
    assert sorted(asked) == sorted(fps)
    assert [full[f][2:] for f in eval_files] == [(3, 3), (3, 3)]
    assert set(full) == set(eval_files)


def test_the_harness_decides_in_the_apps_source_order():
    # decide() breaks ties by source order, so an order the app no longer uses would measure other decisions.
    import copy

    from media_preview_generator.markers import pipeline
    from media_preview_generator.markers.settings import DEFAULT_GLOBAL_MARKERS, load_global, validate_global
    from tools.markers_eval.decisions import ORDER
    from tools.markers_eval.online import DEFAULT_ORDER, THEINTRODB_ORDER

    raw = copy.deepcopy(DEFAULT_GLOBAL_MARKERS)
    assert pipeline._decision_order(load_global(validate_global(raw, None)[0])) == DEFAULT_ORDER
    for source in raw["sources"]:
        source["enabled"] = True
    assert pipeline._decision_order(load_global(validate_global(raw, None)[0])) == THEINTRODB_ORDER == ORDER
