from collections import Counter

from media_preview_generator.markers.models import Candidate, Marker, MarkerType, Source
from tools.markers_eval.online import case_file, case_key, judge_online, plex_online, run_online, skipdb_segments
from tools.markers_eval.plex import PlexMarker

CASE = {
    "show": "Show",
    "imdb": "tt1",
    "season": 1,
    "episode": 2,
    "dur": 1320.0,
    "intro": [60.0, 90.0],
    "credits_start": 1290.0,
}
ORDER = ("chapters", "introdb", "skipdb", "season_audio", "server_markers")


def _result(tidb=None, idb=None):
    return {"case": CASE, "tidb": tidb, "idb": idb}


def _dump_row(kind, start_ms, end_ms, duration_ms):
    return {
        "imdb_id": "tt1",
        "season": 1,
        "episode": 2,
        "segment_type": kind,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": duration_ms,
    }


def test_skipdb_segments_pick_the_nearest_duration_and_label_the_match():
    dump = [_dump_row("intro", 61_000, 90_000, 1_300_000), _dump_row("intro", 60_500, 90_000, 1_319_000)]
    seg = skipdb_segments(CASE, dump)["intro"]
    assert (seg["start_ms"], seg["match"]) == (60_500, "exact")
    assert skipdb_segments(CASE, [_dump_row("intro", 1, 2, 1_000_000)])["intro"]["match"] == "out-of-range"
    assert skipdb_segments(CASE, [_dump_row("intro", 1, 2, 1_310_000)])["intro"]["match"] == "shifted"


def test_judge_online_matches_the_audit_rules():
    assert judge_online(MarkerType.INTRO, Marker(MarkerType.INTRO, 59_000, 94_000, ("x",)), CASE) == "useful"
    assert judge_online(MarkerType.INTRO, Marker(MarkerType.INTRO, 59_000, 95_500, ("x",)), CASE) == "wrong"
    assert judge_online(MarkerType.CREDITS, Marker(MarkerType.CREDITS, 1_279_000, 1_320_000, ("x",)), CASE) == "wrong"
    assert judge_online(MarkerType.CREDITS, Marker(MarkerType.CREDITS, 1_321_000, 1_325_000, ("x",)), CASE) == "late"


def test_run_online_decides_through_the_real_rules():
    idb = {"intro": {"start_ms": 60_000, "end_ms": 90_000}, "recap": None, "outro": None}
    dump = [_dump_row("intro", 60_500, 91_000, 1_319_000)]
    high = run_online([_result(idb=idb)], dump, order=ORDER, level="high")
    assert high["intro"] == Counter(useful=1)
    assert high["credits"] == Counter(missed=1)
    alone = run_online([_result(idb=idb)], [], order=ORDER, level="medium")
    assert alone["intro"] == Counter(missed=1)  # IntroDB alone never decides (spec §5.5 rule 6)


def test_run_online_leaves_out_sources_the_setting_has_off():
    tidb = {"intro": [{"start_ms": 60_000, "end_ms": 90_000}]}
    idb = {"intro": {"start_ms": 60_000, "end_ms": 89_000}, "recap": None, "outro": None}
    off = run_online([_result(tidb=tidb, idb=idb)], [], order=ORDER, level="high")
    assert off["intro"] == Counter(missed=1)
    dump = [_dump_row("intro", 60_000, 90_500, 1_320_000)]
    on = run_online([_result(tidb=tidb)], dump, order=("chapters", "theintrodb", *ORDER[1:]), level="high")
    assert on["intro"] == Counter(useful=1)


def test_extra_evidence_that_is_season_audio_and_a_servers_own_marker_agrees_only_with_g3_off():
    extra = {
        case_key(CASE): [
            Candidate(MarkerType.INTRO, 60_000, 90_000, Source.SEASON_AUDIO, 1.0, "3/3"),
            Candidate(MarkerType.INTRO, 61_000, 91_000, Source.SERVER_MARKERS, origin="plex"),
        ]
    }
    on = run_online([_result()], [], order=ORDER, level="high", extra=extra)
    off = run_online([_result()], [], order=ORDER, level="high", extra=extra, g3=False)
    assert on["intro"] == Counter(missed=1)  # ruling G3: they are never two agreeing sources
    assert off["intro"] == Counter(useful=1)
    # At Medium season audio decides alone, and the agreeing server marker doesn't hold it back (2026-09-24).
    assert run_online([_result()], [], order=ORDER, level="medium", extra=extra)["intro"] == Counter(useful=1)


def test_extra_server_credits_agree_with_skipdb_at_high():
    dump = [_dump_row("outro", 1_292_000, 1_320_000, 1_320_000)]
    extra = {case_key(CASE): [Candidate(MarkerType.CREDITS, 1_295_000, None, Source.SERVER_MARKERS, origin="plex")]}
    alone = run_online([_result()], dump, order=ORDER, level="high")
    both = run_online([_result()], dump, order=ORDER, level="high", extra=extra)
    assert alone["credits"] == Counter(missed=1)
    assert both["credits"] == Counter(useful=1)


def test_plex_online_judges_plexs_first_marker_of_each_type():
    baseline = {case_key(CASE): [PlexMarker("intro", 59_000, 80_000, False), PlexMarker("credits", 1_250_000, 1_300_000, False),
                                 PlexMarker("credits", 1_300_000, 1_320_000, True)]}  # fmt: skip
    counts = plex_online([_result()], baseline)
    assert counts["intro"] == Counter(useful=1)  # Plex's intro ends early: shows more, skips no story
    assert counts["credits"] == Counter(wrong=1)  # its first credits marker starts 40 s early
    assert plex_online([_result()], {}) == {"intro": Counter(missed=1), "credits": Counter(missed=1)}


def test_case_file_finds_the_one_episode_of_the_show_folder():
    files = [
        "/tv/Marvel's Daredevil (2015) {tvdb-1}/Season 03/Marvel's Daredevil (2015) - S03E02 - x.mkv",
        "/tv/Marvel's Daredevil (2015) {tvdb-1}/Season 03/Marvel's Daredevil (2015) - S03E03 - y.mkv",
        "/tv/Show Two (2020)/Season 01/Show Two - S01E02.mkv",
    ]
    case = {**CASE, "show": "Marvels Daredevil", "season": 3, "episode": 3}
    assert case_file(case, files) == files[1]
    assert case_file({**case, "episode": 9}, files) is None
    assert case_file(case, [*files, files[1].replace(" - y.mkv", " - y (1080p).mkv")]) is None  # two versions


def test_the_probed_frame_rate_reaches_the_decision():
    # A 25 fps file: IntroDB's film-rate intro (324-354 s) disagrees with season audio (310-338 s) as it is, and
    # agrees on the file's clock, so only a decision that knows the frame rate decides it.
    case = {**CASE, "dur": 2498.304, "intro": [310.0, 338.5]}
    idb = {"intro": {"start_ms": 324_000, "end_ms": 354_000}, "recap": None, "outro": None}
    extra = {case_key(case): [Candidate(MarkerType.INTRO, 310_000, 338_000, Source.SEASON_AUDIO, 1.0, "3/3")]}
    rows = [{"case": case, "tidb": None, "idb": idb}]
    unknown = run_online(rows, [], order=ORDER, level="medium", extra=extra)
    probed = run_online(rows, [], order=ORDER, level="medium", extra=extra, frame_rates={case_key(case): 25.0})
    assert unknown["intro"] == Counter(missed=1)  # "sources disagree"
    assert probed["intro"] == Counter(useful=1)
