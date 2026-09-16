from media_preview_generator.markers.models import Candidate, MarkerType, Source
from tools.markers_eval.plex import PlexMarker, export_sql, parse_export, parse_scale_dump, server_candidates


def test_parse_export_keeps_markers_and_files_without_any():
    lines = [
        '/tv/Show/Season 01/Show - S01E01.mkv|intro|1609|32897|{"pv:version":"5"}|1319744|abc\n',
        '/tv/Show/Season 01/Show - S01E01.mkv|credits|1290000|1319744|{"pv:final":"1"}|1319744|abc\n',
        "/tv/Show/Season 01/Show - S01E02.mkv|||||1300000|def\n",
    ]
    out = parse_export(lines)
    assert out["/tv/Show/Season 01/Show - S01E01.mkv"] == [
        PlexMarker("intro", 1609, 32897, False),
        PlexMarker("credits", 1_290_000, 1_319_744, True),
    ]
    assert out["/tv/Show/Season 01/Show - S01E02.mkv"] == []


def test_export_sql_is_read_only_and_escapes_folder_names():
    sql = export_sql(["/tv/Bob's 100% Show/Season 01"])
    assert "LIKE '/tv/Bob''s 100\\% Show/Season 01/%' ESCAPE '\\'" in sql
    assert "t.text IN ('intro', 'credits')" in sql
    for word in ("INSERT", "UPDATE", "DELETE", "CREATE", "ATTACH", "PRAGMA", "REPLACE", "VACUUM"):
        assert word not in sql.upper()


def test_parse_scale_dump_lists_every_part_and_reads_the_final_flag():
    markers = [
        {"file": "/m/A/A.mkv", "type": "credits", "start": 5_000, "end": 6_000, "extra": '{"pv:version":"4"}'},
        {"file": "/m/A/A.mkv", "type": "credits", "start": 7_000, "end": 9_000, "extra": '{"pv:final":"1"}'},
    ]
    parts = [{"file": "/m/A/A.mkv", "duration": 9_000}, {"file": "/m/B/B.mkv", "duration": 8_000}]
    assert parse_scale_dump(markers, parts) == {
        "/m/A/A.mkv": [PlexMarker("credits", 5_000, 6_000, False), PlexMarker("credits", 7_000, 9_000, True)],
        "/m/B/B.mkv": [],
    }


def test_server_candidates_match_the_apps_plex_reader():
    markers = [PlexMarker("intro", 1_000, 30_000, False), PlexMarker("credits", 90_000, 100_000, True)]
    assert server_candidates(markers) == [
        Candidate(MarkerType.INTRO, 1_000, 30_000, Source.SERVER_MARKERS, origin="plex"),
        Candidate(MarkerType.CREDITS, 90_000, None, Source.SERVER_MARKERS, origin="plex"),  # final runs to the end
    ]
    assert server_candidates(markers, MarkerType.INTRO) == server_candidates(markers)[:1]
