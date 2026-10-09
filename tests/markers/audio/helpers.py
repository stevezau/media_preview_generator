"""Shared builders for the season audio tests: planted fingerprints, an ``Audio`` that fakes ffmpeg and ffprobe, and
the matcher helpers the guard and opening tests share."""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from unittest.mock import patch

import numpy as np

from media_preview_generator.markers.audio import POINT_S, fingerprint, season
from media_preview_generator.markers.audio.matcher import IntroSegment, file_hits, intro_for
from media_preview_generator.markers.probe import MediaProbe

DUR = 1_321_472
# Why an intro only IntroDB answered for is left undecided (``decide._lone_answer_reason``).
IDB_ALONE = "only IntroDB has the intro; an online answer needs a check against the file"
N_POINTS = int(fingerprint.window_s(DUR) / POINT_S)
INTRO = np.random.default_rng(42).integers(0, 2**32, size=240, dtype=np.uint64).astype("<u4")
OFFSETS = {
    "S01E01": 300,
    "S01E02": 520,
    "S01E03": 710,
    "S01E04": 90,
    "S01E05": 600,
    "S02E01": 400,
    "S02E02": 900,
    "S02E03": 150,
}
SEASON_RAW = {"sources": [{"id": "theintrodb", "enabled": False}], "detect": {"intro": True, "credits": False}}
SIL = season.SILENCE_POINT


def fake_points(path: str) -> np.ndarray:
    key = re.search(r"S\d\dE\d\d", path).group(0)
    rng = np.random.default_rng(zlib.crc32(key.encode()))
    body = rng.integers(0, 2**32, size=N_POINTS, dtype=np.uint64).astype("<u4")
    body[OFFSETS[key] : OFFSETS[key] + 240] = INTRO
    return body


def planted_ms(path: str) -> tuple[int, int]:
    at = OFFSETS[re.search(r"S\d\dE\d\d", path).group(0)]
    return round(at * POINT_S * 1000), round((at + 239) * POINT_S * 1000)


def noise(seed: int, size: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 2**32, size=size, dtype=np.uint64).astype("<u4")


class Audio:
    """Patches ffmpeg (fingerprints), ffprobe of other episodes, the chromaprint check and the end-picture decode (every
    pair of pictures alike unless ``share`` says otherwise).

    ``rates`` gives files a video frame rate (the rest have none); ``retimed(path, retime)`` is a file's fingerprint
    with its audio retimed to another speed, and ``fail_retimed`` the files whose retimed fingerprint ffmpeg fails on.
    """

    def __init__(self, fail: set[str] | None = None, points=fake_points, share=lambda *_args: 1.0, rates=None,
                 retimed=None, fail_retimed: set[str] | None = None):  # fmt: skip
        self.computed: list[str] = []
        self.retimes: list[tuple[str, float]] = []
        self.fail = fail or set()
        self.fail_retimed = fail_retimed or set()
        self.points = points
        self.retimed = retimed
        self.rates = rates or {}
        self.share = share
        self.compared: list[tuple] = []
        self.worker: list[dict] = []  # each ffmpeg run's pause check and threads
        self.progress: list = []  # each ffmpeg run's progress factory

    def _share(self, reader, target, partner, start_s, end_s, offset_s):
        self.compared.append((target, partner, start_s, end_s, offset_s))
        return self.share(target, partner, start_s, end_s, offset_s)

    def compute(self, path, duration_ms, *, ffmpeg, cancel_check=None, retime=None, pause_check=None,
                ffmpeg_threads=None, progress=None):  # fmt: skip
        self.worker.append({"pause_check": pause_check, "ffmpeg_threads": ffmpeg_threads})
        self.progress.append(progress)
        if retime is not None:
            self.retimes.append((path, retime))
            if path in self.fail_retimed:
                raise fingerprint.FingerprintError("ffmpeg exited 1")
            return self.retimed(path, retime)
        self.computed.append(path)
        if path in self.fail:
            raise fingerprint.FingerprintError("ffmpeg exited 1")
        return self.points(path)

    def probe(self, path, **_kwargs):
        """ffprobe of a file: its duration and frame rate, no chapters."""
        return MediaProbe(DUR, (), frame_rate=self.rates.get(path))

    def __enter__(self):
        self._patches = [
            patch.object(fingerprint, "compute_fingerprint", side_effect=self.compute),
            patch.object(season, "probe_media", side_effect=self.probe),
            patch.object(season, "chromaprint_ffmpeg", return_value="/usr/lib/jellyfin-ffmpeg/ffmpeg"),
            patch.object(season.end_picture.Reader, "share", autospec=True, side_effect=self._share),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def season_spec():
    with patch.object(season, "chromaprint_ffmpeg", return_value="/usr/lib/jellyfin-ffmpeg/ffmpeg"):
        return season.season_audio_spec("/usr/lib/jellyfin-ffmpeg/ffmpeg")


def pts(seconds: float) -> int:
    """Fingerprint points in ``seconds``."""
    return round(seconds / POINT_S)


@dataclass(frozen=True)
class Plant:
    """Shared audio planted in the episodes: at one start for all, or one start per episode (None: not in it). With
    ``every`` k only one point in k is shared (music under dialogue), and ``hole`` blanks points (start, count). A
    plant that runs past the end of an episode's fingerprint is cut there."""

    seed: int
    at: float | tuple[float | None, ...]
    length_s: float
    every: int = 1
    hole: tuple[int, int] | None = None

    def into(self, body: np.ndarray, episode: int) -> None:
        start = self.at if isinstance(self.at, float) else self.at[episode]
        if start is None:
            return
        shared = np.arange(0, pts(self.length_s), self.every)
        if self.hole:
            shared = shared[(shared < self.hole[0]) | (shared >= self.hole[0] + self.hole[1])]
        target = pts(start) + shared
        inside = target < len(body)
        body[target[inside]] = noise(self.seed, pts(self.length_s))[shared[inside]]


def runs(points):
    """``runs_between(a, b)`` over planted fingerprints, each pair computed once."""
    cache = {}

    def runs_between(a, b):
        if (a, b) not in cache:
            cache[(a, b)] = season.season_pair_runs(points[a], points[b])
        return cache[(a, b)]

    return runs_between


def alike(_candidate) -> bool:
    """An end-picture check whose pictures always match."""
    return True


def answers(files, points, pictures=alike):
    """What the matcher alone answers for each file, and what the guarded season step answers."""
    runs_between = runs(points)
    matcher_answers = [intro_for(file_hits(f, files, runs_between), len(files) - 1) for f in files]
    step_answers = [season.season_intro(f, files, points, runs_between, end_picture_passes=pictures) for f in files]
    return matcher_answers, step_answers


def near(segment: IntroSegment | None, start: float, end: float) -> bool:
    """Whether ``segment`` starts and ends within a second of ``start`` and ``end``."""
    return segment is not None and abs(segment.start_s - start) < 1.0 and abs(segment.end_s - end) < 1.0
