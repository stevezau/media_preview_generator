from collections import Counter

from media_preview_generator.markers.probe import Chapter, MediaProbe
from tools.markers_eval.credits import chapter_rules, compare_credits_with_plex, judge_credits, title_coverage
from tools.markers_eval.plex import PlexMarker

PROBES = {
    "/m/A (2001)/A.mkv": MediaProbe(6_000_000, (Chapter(0, 5_700_000, "Film"), Chapter(5_700_000, None, "End Credits"))),
    "/m/B (2002)/B.mkv": MediaProbe(6_000_000, (Chapter(0, 5_500_000, "Film"), Chapter(5_500_000, 5_800_000, "End Credits"), Chapter(5_800_000, None, "End Credits"))),
    "/m/C (2003)/C.mkv": MediaProbe(6_000_000, (Chapter(0, 4_800_000, "Film"), Chapter(4_800_000, None, "End Credits"))),
}  # fmt: skip


def test_judge_credits():
    assert [judge_credits(s, 100.0) for s in (None, 89.0, 95.0, 131.0)] == ["missed", "wrong", "useful", "late"]


def test_title_coverage_lists_unrecognised_and_ending_titles():
    movies = [
        {"file": "/m/A (2001)/A.mkv", "chapters": ["Opening", "Ending Credits"]},
        {"file": "/m/B (2002) {tmdb-2}/B.mkv", "chapters": ["Part 1", "Ending"]},
        {"file": "/m/C (2003)/C.mkv", "chapters": ["Part 1", "Rolling Titles"]},
        {"file": "/tv/Show (2020) {tvdb-1}/Season 02/Show - S02E01.mkv", "chapters": ["OP", "ED", "Credits"]},
    ]
    report = title_coverage(movies)
    assert report.tally == Counter(found=2, not_found=2)  # "Ending" isn't a credits title today (ledger L165)
    assert report.titles_missed == ["Ending", "Rolling Titles"]
    assert report.ending_titles == ["B (2002)"]  # "Ending Credits" is counted as credits, so it isn't listed
    assert report.several_credits == ["Show (2020) Season 02"]


def test_chapter_rules_judges_against_adjudicated_truth_and_reports_two_credits_chapters():
    files = [
        {"file": "/m/A (2001)/A.mkv", "credits_start": 5690.0},
        {"file": "/m/B (2002)/B.mkv", "credits_start": 5500.0},
    ]
    adjudicated = {"B.mkv": {"truth": 5460.0, "reason": "names over footage"}}
    report = chapter_rules(files, adjudicated, probe=PROBES.__getitem__)
    assert report.tally == Counter(useful=1, late=1)
    assert report.several_credits == ["B (2002)"]


def test_compare_credits_with_plex_decides_like_the_app():
    files = [
        {"file": "/m/A (2001)/A.mkv", "credits_start": 5690.0},
        {"file": "/m/B (2002)/B.mkv", "credits_start": 5500.0},
        {"file": "/m/C (2003)/C.mkv", "credits_start": 4800.0},
    ]
    adjudicated = {"B.mkv": {"truth": 5460.0, "reason": "names over footage"}}
    baseline = {
        # Plex's own credits start 60 s after the chapter: rule 7 moves our start to it (late).
        "/m/A (2001)/A.mkv": [PlexMarker("credits", 5_760_000, 6_000_000, True)],
        # Plex starts 60 s before the truth; it covers our (last) chapter start, so nothing moves.
        "/m/B (2002)/B.mkv": [PlexMarker("credits", 5_400_000, 6_000_000, True)],
        "/m/C (2003)/C.mkv": [],
    }
    rows = compare_credits_with_plex(files, adjudicated, probe=PROBES.__getitem__, baseline=baseline, is_movie=True)
    assert rows.plex == Counter(late=1, wrong=1, missed=1)
    assert rows.chapters == Counter(useful=1, late=1, missed=1)  # C: movie credits 1200 s from the end
    assert rows.high == rows.medium == Counter(late=2, missed=1)
    by_file = {row["file"]: row for row in rows.files}
    assert by_file["/m/A (2001)/A.mkv"]["chapters"] == 5700.0
    assert by_file["/m/A (2001)/A.mkv"]["high"] == 5760.0 and by_file["/m/A (2001)/A.mkv"]["shortened"] is True
    assert by_file["/m/B (2002)/B.mkv"]["high"] == 5800.0 and by_file["/m/B (2002)/B.mkv"]["shortened"] is False
    assert by_file["/m/B (2002)/B.mkv"]["truth"] == 5460.0
    assert by_file["/m/C (2003)/C.mkv"]["high"] is None
    tv = compare_credits_with_plex(files[2:], {}, probe=PROBES.__getitem__, baseline=baseline, is_movie=False)
    assert tv.high == Counter(useful=1)  # the 900 s bound is for movies only
