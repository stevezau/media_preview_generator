"""Credits text harness rows on synthetic files: the spec tally, the Plex comparison, the gate, the cache."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.markers.credits.detector import CREDITS_TEXT_VERSION, CreditsTextResult
from media_preview_generator.markers.credits.frames import GpuDecodeError
from media_preview_generator.markers.probe import Chapter, MediaProbe
from tools.markers_eval import credits_text as ct
from tools.markers_eval.plex import PlexMarker

DUR = 6_000_000
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


def _dark(t, boxes=0):
    return (float(t), boxes, 10.0)


STORY = [(float(t), 0, 120.0) for t in range(0, 90, 2)]


@pytest.mark.parametrize(
    ("rows", "start", "expected"),
    [
        ([*STORY, *[_dark(t, 3) for t in range(90, 160, 2)]], 90.0, False),                      # a plain roll
        ([*STORY, *[_dark(t, 1) for t in range(88, 100, 2)], *[_dark(t, 3) for t in range(100, 160, 2)]], 88.0, True),  # 12 s of cards
        ([*STORY, *[_dark(t, 1) for t in range(90, 100, 2)], *[_dark(t, 3) for t in range(100, 160, 2)]], 90.0, False),  # 10 s: not more
        ([*STORY, *[_dark(t, 2) for t in range(90, 100, 2)], *[_dark(t) for t in range(100, 130, 2)], *[_dark(t, 2) for t in range(130, 190, 2)]], 90.0, True),  # black gap
        ([*STORY, *[(float(t), 0, 120.0) for t in range(90, 100, 2)]], None, False),              # no answer
    ],
)  # fmt: skip
def test_epilogue_like(rows, start, expected):
    assert ct.epilogue_like(rows, start) is expected


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
    monkeypatch.setattr(ct, "epilogue_like", lambda rows, start: epilogue)
    assert ct.sheet_reasons({"text": text, "text_end": end, "truth": truth}, []) == reasons


def test_the_pipeline_reads_credit_text_only_for_online_cases_left_undecided():
    verdicts = [
        {"key": "a", "type": "credits", "verdict": "useful"},
        {"key": "b", "type": "credits", "verdict": "missed"},
        {"key": "b", "type": "intro", "verdict": "useful"},
        {"key": "c", "type": "intro", "verdict": "missed"},
        {"key": "d", "type": "credits", "verdict": "wrong"},
    ]
    assert ct.undecided_credits(verdicts) == {"b"}


@pytest.mark.parametrize(
    ("stream", "kind"),
    [
        ({"color_transfer": "bt709"}, "sdr"),
        ({"color_transfer": "smpte2084"}, "hdr10"),
        ({"color_transfer": "arib-std-b67"}, "hdr10"),
        ({"color_transfer": "smpte2084", "side_data_list": [{"dv_profile": 8}]}, "dv_other"),
        ({"side_data_list": [{"dv_profile": 5}]}, "dv5"),
    ],
)
def test_hdr_kind(stream, kind):
    import json

    with patch.object(ct.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps({"streams": [stream]}), returncode=0)) as run:  # fmt: skip
        assert ct.hdr_kind("/m/A.mkv", ffprobe="/ff/ffprobe") == kind
    assert run.call_args.args[0][:2] == ["/ff/ffprobe", "-v"]


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
        return CreditsTextResult(5702.0, 5890.0, ((5700.0, 2, 12.0),), ((5701.0, 2, 12.0),), ((5890.0, 2, 12.0),))

    monkeypatch.setattr(ct, "find_credits", find)
    cache = ct.CreditsTextCache(tmp_path / "cache", ffmpeg="/ff", decode="gpu", gpu_device="cuda:0",
                                count_boxes=lambda p: [0] * len(p), probe=lambda p: MediaProbe(DUR, ()))  # fmt: skip
    first = cache.result(str(media), is_episode=False)
    second = cache.result(str(media), is_episode=False)
    assert first == second == {"start_s": 5702.0, "end_s": 5890.0, "key": [[5700.0, 2, 12.0]], "fine": [[5701.0, 2, 12.0]],
                               "end": [[5890.0, 2, 12.0]]}  # fmt: skip
    assert len(calls) == 1
    assert (calls[0]["duration_ms"], calls[0]["gpu"], calls[0]["gpu_device_path"], calls[0]["is_episode"]) == (DUR, "NVIDIA", "cuda:0", False)  # fmt: skip
    monkeypatch.setattr(ct, "CREDITS_TEXT_VERSION", CREDITS_TEXT_VERSION + 1)
    cache.result(str(media), is_episode=False)
    assert len(calls) == 2


def _cache_for(tmp_path, find, decode="gpu"):
    return ct.CreditsTextCache(tmp_path / "cache", ffmpeg="/ff", decode=decode, gpu_device="cuda:0",
                               count_boxes=lambda p: [0] * len(p), probe=lambda p: MediaProbe(DUR, ()))  # fmt: skip


def test_a_file_the_gpu_cannot_decode_is_read_on_the_cpu_as_the_worker_does(tmp_path, monkeypatch):
    media = tmp_path / "AV1.mkv"
    media.write_bytes(b"x")
    calls = []

    def find(path, **kwargs):
        calls.append(kwargs)
        if kwargs["gpu"] is not None:
            raise GpuDecodeError("ffmpeg exited 69 on the GPU")
        return CreditsTextResult(5702.0, None, ((5700.0, 2, 12.0),), (), ())

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

    monkeypatch.setattr(ct, "_counter", never)
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
                            count_boxes=lambda p: [], probe=lambda p: MediaProbe(DUR, ()))  # fmt: skip
