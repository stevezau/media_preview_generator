"""Targeted credit text recheck: online timestamps guide OCR but do not decide a marker."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from media_preview_generator.markers.credits import detector, frames
from media_preview_generator.markers.models import MarkerType, Source
from media_preview_generator.nodes import remote_ops


def answer(start_s: float | None = None) -> detector.CreditsTextResult:
    return detector.CreditsTextResult(start_s, None, (), (), ())


@pytest.fixture
def no_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frames, "container_start_s", lambda *a, **k: 0.0)
    monkeypatch.setattr(frames, "keyframe_thinning", lambda *a, **k: frames.KeyframeThinning())


def find(*, online_hint_s: float | None = 1346.0, is_episode: bool = True):
    return detector.find_credits(
        "/video/S01E01.mkv",
        duration_ms=1_500_000,
        is_episode=is_episode,
        ffmpeg="ffmpeg",
        detect_boxes=lambda _frames: [],
        gpu=None,
        gpu_device_path=None,
        read_text=None,
        online_hint_s=online_hint_s,
    )


def test_targeted_retry_only_after_regular_failure(monkeypatch: pytest.MonkeyPatch, no_probes) -> None:
    calls: list[dict] = []

    def roll(_path, read):
        calls.append(read.keywords)
        return answer() if len(calls) == 1 else answer(1351.0)

    monkeypatch.setattr(detector, "_the_roll", roll)
    assert find().start_s == 1351.0
    assert len(calls) == 2
    assert calls[1]["sample_fps"] == 2
    assert calls[1]["tail_start"] == 1301.0  # extra 35 s of story before hint-10
    assert calls[1]["earliest_start_s"] == 1336.0
    assert calls[1]["window_end_s"] == 1426.0


def test_regular_answer_never_runs_targeted_retry(monkeypatch: pytest.MonkeyPatch, no_probes) -> None:
    roll = Mock(return_value=answer(1351.0))
    monkeypatch.setattr(detector, "_the_roll", roll)
    assert find().start_s == 1351.0
    assert roll.call_count == 1


@pytest.mark.parametrize("hint,episode", [(None, True), (-1.0, True), (0.0, True), (1500.0, True), (1346.0, False)])
def test_invalid_hints_and_movies_skip_retry(monkeypatch: pytest.MonkeyPatch, no_probes, hint, episode) -> None:
    roll = Mock(return_value=answer())
    monkeypatch.setattr(detector, "_the_roll", roll)
    assert find(online_hint_s=hint, is_episode=episode).start_s is None
    assert roll.call_count == 1


@pytest.mark.parametrize("start_s", [None, 1330.0, 1367.0, 1450.0])
def test_targeted_retry_requires_nearby_ocr_result(monkeypatch: pytest.MonkeyPatch, no_probes, start_s) -> None:
    answers = iter([answer(), answer(start_s)])
    monkeypatch.setattr(detector, "_the_roll", lambda *args: next(answers))
    assert find().start_s is None


def test_targeted_timeout_keeps_normal_retry_semantics(monkeypatch: pytest.MonkeyPatch, no_probes) -> None:
    roll = Mock(side_effect=[answer(), frames.DecodeTimeoutError("timed out")])
    monkeypatch.setattr(detector, "_the_roll", roll)
    with pytest.raises(frames.DecodeTimeoutError):
        find()


def test_online_hint_respects_consensus_and_enabled_sources() -> None:
    def evidence(start_ms, source):
        return SimpleNamespace(type=MarkerType.CREDITS, start_ms=start_ms, source=source)

    rec = SimpleNamespace(id=1, season_key="show-S1", duration_ms=1_500_000)
    rows = [evidence(1_346_000, Source.INTRODB), evidence(1_351_000, Source.SKIPDB)]
    ctx = SimpleNamespace(
        store=SimpleNamespace(get_evidence=lambda _id: rows),
        settings=SimpleNamespace(source_enabled=lambda _source: True),
    )
    assert detector._online_credits_hint(rec, ctx) == 1351.0
    rows[1] = evidence(1_360_000, Source.SKIPDB)
    assert detector._online_credits_hint(rec, ctx) is None
    ctx.settings.source_enabled = lambda _source: False
    assert detector._online_credits_hint(rec, ctx) is None


def test_remote_node_receives_online_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    def run(_op, _path, params, **_kwargs):
        sent.append(params)
        return {"result": {"start_s": None, "end_s": None, "key_rows": [], "fine_rows": [], "end_rows": []}}

    monkeypatch.setattr(remote_ops, "run_on_node", run)
    args = dict(duration_ms=1_500_000, is_episode=True, ffmpeg="ffmpeg", gpu=None, gpu_device_path=None)
    assert remote_ops.find_credits("/video/S01E01.mkv", online_hint_s=1346.0, **args).start_s is None
    assert sent[-1]["online_hint_s"] == 1346.0
    assert remote_ops.find_credits("/video/S01E01.mkv", **args).start_s is None
    assert "online_hint_s" not in sent[-1]
