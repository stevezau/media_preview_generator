"""The look-back before the credit text tail against the build before it, on synthetic files (spec §14 2026-09-23).

    cd /home/data/workspace/plex_generate_vid_previews   # or a worktree of it; needs git history
    PYTHONPATH=. nice -n 19 /home/data/.venv/bin/python docs/design/intro-credits/evidence/credits/lookback_sweep.py

``PYTHONPATH=.`` makes it read this checkout's package rather than the one the venv has installed.

The detector before the look-back is ``media_preview_generator/markers/credits/detector.py`` at ``BASE`` (read with
``git show``); the one after is the working tree's. Both run ``find_credits`` on the same synthetic files, decoded by
a fake that cuts one file's rows to each window asked for, so no media is read. A file is a story then a roll from
``roll_from`` to the end (or to a scene after it), over every combination of: keyframes every 2 s or 5 s, lit or dark
story, a channel bug boxed on one keyframe in four or none, a 60 s scene after the roll or none, dark or lit cards, for
roll starts every 7 s from 400 s before the tail on seven files -- episodes and movies, Automatic and a 300, 600 or
1800 s window.

It prints:

* every answer the old build gave, and whether the new build gives exactly the same result with exactly the same
  decodes (the claim is all of them);
* how many of the old build's misses now have an answer, and why the rest are still missed;
* how many old answers a first step narrowed to the earliest kept start (or to 30 s before it) would change -- why the
  first step is left as it always was.

Takes about ten minutes on storage, one core.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import itertools
import math
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from media_preview_generator.markers import decide
from media_preview_generator.markers.credits import detector as after
from media_preview_generator.markers.credits import frames

BASE = "acff9168"  # feat/markers-detection before the look-back (2026-09-23)
DETECTOR = "media_preview_generator/markers/credits/detector.py"
# (duration s, is an episode, the window the user chose or None for Automatic)
FILES = [(1320, True, None), (2640, True, None), (2640, True, 300.0), (6000, False, None), (6000, False, 600.0),
         (500, True, None), (3000, False, 1800.0)]  # fmt: skip
BUG = (4, 6, 46, 24)
CARDS = ((40, 24, 128, 44), (41, 55, 130, 75))


def _load_before() -> object:
    source = subprocess.run(["git", "show", f"{BASE}:{DETECTOR}"], capture_output=True, text=True, check=True).stdout
    path = Path(tempfile.mkdtemp()) / "detector_before.py"
    path.write_text(source)
    name = "media_preview_generator.markers.credits._detector_before"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _no_boxes(planes: object) -> list:
    return []


def _file(duration_s: int, roll_from: float, gop: int, story_luma: float, bug_every: int, scene_s: int, roll_lit: bool):
    roll_end = duration_s - scene_s

    def row(t: float) -> tuple:
        bug = bug_every and int(t) % (bug_every * gop) == 0
        if roll_from <= t < roll_end:
            boxes = (*CARDS, *((BUG,) if bug_every else ()))
            return (t, 3 if roll_lit else len(boxes), 90.0 if roll_lit else 12.0, boxes)
        boxes = (BUG,) if bug else ()
        return (t, len(boxes), story_luma, boxes)

    def decode(path: str, *, start_s: float, length_s: float | None, keyframes_only: bool, **kwargs: object) -> list:
        end_s = duration_s if length_s is None else start_s + length_s
        every = gop if keyframes_only else 1
        first = int(start_s // every) * every
        return [row(float(t)) for t in range(first, duration_s, every) if start_s <= t < end_s]

    return decode


def _run(module, decode, duration_s, is_episode, tail_s, earliest_s=None, first_step_floor_s=None):
    calls: list[dict] = []

    def recording(path: str, **kwargs: object) -> list:
        calls.append(kwargs)
        rows = decode(path, **kwargs)
        if first_step_floor_s is not None and kwargs["keyframes_only"] and sum(c["keyframes_only"] for c in calls) == 2:
            rows = [r for r in rows if r[0] >= first_step_floor_s]
        return rows

    module.frames.decode_rows = recording
    bound = {} if earliest_s is None else {"earliest_start_s": earliest_s}
    try:
        result = module.find_credits("/m/x.mkv", duration_ms=duration_s * 1000, is_episode=is_episode, tail_s=tail_s,
                                     ffmpeg="ff", detect_boxes=_no_boxes, gpu=None, gpu_device_path=None, **bound)  # fmt: skip
    except Exception as exc:  # noqa: BLE001 -- a raise is a result to compare too
        result = repr(exc)
    return result, calls


def _earliest_s(duration_s: int, is_episode: bool, tail_s: float | None) -> float:
    window_s = None if tail_s is None else int(tail_s)
    window_ms, cap_ms = decide.credits_limits_ms(
        is_episode=is_episode,
        tv_window_s=window_s if is_episode else None,
        movie_window_s=None if is_episode else window_s,
    )
    earliest_ms = decide.earliest_credits_start_ms(
        duration_s * 1000, is_movie=not is_episode, credits_window_ms=window_ms, movie_credits_max_from_end_ms=cap_ms
    )
    return earliest_ms / 1000.0


def _fields(result: object) -> object:
    return result if isinstance(result, str) else dataclasses.astuple(result)


def _answered(result: object) -> bool:
    return not isinstance(result, str) and result.start_s is not None


def main() -> int:
    before = _load_before()
    frames.container_start_s = lambda *args, **kwargs: 0.0
    frames.keyframe_thinning = lambda *args, **kwargs: frames.KeyframeThinning()
    cases = old_answers = identical = now_answered = refused = 0
    differences: list[tuple] = []
    missed: Counter = Counter()
    narrowed: Counter = Counter()
    for duration_s, is_episode, tail_s in FILES:
        tail_start = max(0.0, duration_s - (frames.tail_length_s(is_episode=is_episode) if tail_s is None else tail_s))
        earliest_s = _earliest_s(duration_s, is_episode, tail_s)
        for roll_from in range(int(max(0, tail_start - 400)), duration_s - 20, 7):
            shapes = itertools.product((2, 5), (120.0, 22.0), (0, 4), (0, 60), (False, True))
            for gop, story_luma, bug_every, scene_s, roll_lit in shapes:
                if roll_from >= duration_s - scene_s - 16:
                    continue
                decode = _file(duration_s, float(roll_from), gop, story_luma, bug_every, scene_s, roll_lit)
                old, old_calls = _run(before, decode, duration_s, is_episode, tail_s)
                new, new_calls = _run(after, decode, duration_s, is_episode, tail_s, earliest_s)
                cases += 1
                case = (duration_s, tail_s, roll_from, gop, story_luma, bug_every, scene_s, roll_lit)
                if _answered(old):
                    old_answers += 1
                    if _fields(old) == _fields(new) and old_calls == new_calls:
                        identical += 1
                    else:
                        differences.append(case)
                    refused += old.start_s < earliest_s
                    for label, floor_s in (
                        ("at the earliest kept start", earliest_s),
                        ("30 s before it", earliest_s - 30),
                    ):
                        narrowed_result, _ = _run(after, decode, duration_s, is_episode, tail_s, earliest_s, floor_s)
                        if _fields(narrowed_result) != _fields(old):
                            narrowed[label] += 1
                            narrowed[f"{label}, of them kept by the decision"] += old.start_s >= earliest_s
                    continue
                if _answered(new):
                    now_answered += 1
                    continue
                into_tail_s = math.ceil(roll_from / gop) * gop - math.ceil(tail_start / gop) * gop
                if roll_from < earliest_s:
                    missed["began before the earliest start the decision keeps"] += 1
                elif 0 <= into_tail_s < 30:
                    missed["first card 0-30 s into the tail (the tail-edge guard)"] += 1
                elif into_tail_s >= 30:
                    missed["30 s or more inside the tail (rule J's own miss)"] += 1
                else:
                    missed[f"before the tail: story luma {story_luma}, bug {bool(bug_every)}"] += 1
    print(f"files {cases}; the old build answered {old_answers}; identical with the same decodes {identical}")
    print(f"differences {len(differences)} {differences[:5]}")
    old_misses = cases - old_answers
    print(f"old misses {old_misses}: now answered {now_answered}, still missed {old_misses - now_answered}")
    for reason, count in sorted(missed.items()):
        print(f"  {count:6d}  {reason}")
    print(f"old answers the decision refuses anyway: {refused}")
    print("old answers a narrowed first step would change:", dict(narrowed))
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
