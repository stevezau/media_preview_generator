"""Credits text harness rows on synthetic files: the spec tally, the Plex comparison, the gate, the cache."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.markers.credits.detector import CREDITS_TEXT_VERSION, CreditsTextResult
from media_preview_generator.markers.credits.frames import GpuDecodeError
from media_preview_generator.markers.probe import Chapter, MediaProbe
from tools.markers_eval import credits_text as ct
from tools.markers_eval.decode_cache import DecodeCache
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
# plumbing the detector reports through, and the Vulkan probe, which only picks the helper's device (the self-test
# keeps a GPU whose box counts differ from the CPU's out).
NOT_ANSWER_CODE = {"markers.models", "markers.locks", "markers.pipeline", "markers.store", "processing.generator",
                   "gpu.vulkan_probe"}  # fmt: skip


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

    def __init__(self, tmp_path, monkeypatch, *, missing=(), spec_within_10s=3, failing_path=None, cache_error=None):
        self.cache_calls, self.closed, self.seen = [], 0, {}
        evidence = tmp_path / "evidence"
        (evidence / "credits").mkdir(parents=True)
        for name, rows in {**GATE_FILES, "adjudicated": {}}.items():
            (evidence / f"credits/{name}.json").write_text(json.dumps(rows))
        run = self
        self.counted = []

        def count_boxes(planes):
            run.counted.append(planes.shape)
            return [0] * len(planes)

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
                return {"start_s": start, "end_s": None, "key": [], "fine": [], "end": []}

        def close():
            run.closed += 1

        def counter(decode, gpu_device):
            run.seen["counter"] = (decode, gpu_device)
            return ct.TextDetection(count_boxes, backend, close)

        def hdr_kind(path, *, ffprobe):
            run.seen.setdefault("hdr_ffprobe", set()).add(ffprobe)
            return "hdr10" if "M1" in path else "sdr"

        monkeypatch.setattr(ct, "evidence_dir", lambda: evidence)
        monkeypatch.setattr(ct, "ProbeCache", FakeProbes)
        monkeypatch.setattr(ct, "CreditsTextCache", FakeCache)
        monkeypatch.setattr(ct, "_counter", counter)
        monkeypatch.setattr(ct, "load_baseline", lambda path: run.seen.update(baseline=path) or {p: [PlexMarker("credits", int(s * 1000), DUR, True)] for p, s in GATE_PLEX.items()})  # fmt: skip
        monkeypatch.setattr(ct, "hdr_kind", hdr_kind)
        monkeypatch.setattr(ct, "SPEC_WITHIN_10S", spec_within_10s)
        self.tmp_path, self.count_boxes = tmp_path, count_boxes

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
def test_the_counter_reports_the_backend_the_pool_actually_used(monkeypatch, decode, verdict, backend):
    # This string is the decode cache's key: a GPU run whose self-test chose the CPU must share a CPU run's rows.
    from media_preview_generator.markers.credits import textdet_helper

    asked = []

    class Pool:
        def backend_of(self, gpu, gpu_device_path):
            asked.append((gpu, gpu_device_path))
            return verdict

        def count_boxes(self, planes, *, gpu, gpu_device_path):
            asked.append(("count", gpu, gpu_device_path))
            return [0] * len(planes)

        def close_all(self):
            asked.append("closed")

    monkeypatch.setattr(textdet_helper, "get_textdet_pool", Pool)
    detection = ct._counter(decode, "cuda:1")
    assert detection.backend() == backend
    gpu, device = ("NVIDIA", "cuda:1") if decode == "gpu" else (None, None)
    detection.count_boxes([object()])
    detection.close()
    assert asked == [(gpu, device), ("count", gpu, device), "closed"]


@pytest.mark.parametrize(("decode", "gpu_device"), [("gpu", "cuda:1"), ("cpu", "cuda:0")])
def test_the_run_hands_its_decode_path_and_tools_to_every_part(tmp_path, monkeypatch, decode, gpu_device):
    # A --decode cpu run that measured the GPU path (or the wrong card) would still report "decode": "cpu".
    run = _GateRun(tmp_path, monkeypatch)
    summary, _, _ = run(decode=decode, gpu_device=gpu_device)
    assert run.seen["counter"] == (decode, gpu_device)
    root, kwargs = run.seen["cache"]
    assert root == tmp_path / "cache"
    assert kwargs.pop("probe").__self__.__class__.__name__ == "FakeProbes"
    decodes = kwargs.pop("decodes")
    assert isinstance(decodes, DecodeCache) and decodes.digest == ct.decode_digest()
    # The helper is started on one blank frame before the first file, so the cache is told a backend from the start.
    assert run.counted[0] == (1, 180, 320) and decodes._backend() == "webgpu cuda:0"
    assert summary["text_detection"] == "webgpu cuda:0"
    assert kwargs == {"ffmpeg": "/ff", "decode": decode, "gpu_device": gpu_device, "count_boxes": run.count_boxes}
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
    monkeypatch.setattr(ct, "sheet_reasons", lambda detail, key_rows: ["late >30 s"])
    summary, _, _ = ct.run_credits_text(decode="gpu", gpu_device="cuda:0", sets=("80", "205"), online=False,
                                        cache_root=tmp_path / "cache", ffmpeg="/ff", ffprobe="/ffp",
                                        baseline_path=tmp_path / "b.json", sheets_dir=tmp_path / "sheets",
                                        before=before)  # fmt: skip
    assert [(c["set"], c["name"], c["moved"]) for c in summary["changed"]] == [
        ("movie_credit_truth", "X2 (2011)", ["start"])
    ]
    assert summary["changed"][0]["start_minus_truth"] == [-20.0, 1.0]
    assert [(path, around) for path, around, _ in written] == [("/m/X2 (2011)/X2.mkv", 5501.0)]


def test_only_the_chosen_set_is_run_and_judged(tmp_path, monkeypatch):
    run = _GateRun(tmp_path, monkeypatch, missing=("/m/M2 (2002)/M2.mkv",))
    summary, _, passed = run(sets=("205",))
    assert set(summary["gate"]) == {"205"} and "rule_j_80" not in summary
    assert {path for path, _ in run.cache_calls} == {"/m/X1 (2010)/X1.mkv", "/m/X2 (2011)/X2.mkv"}
    assert passed is True


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
