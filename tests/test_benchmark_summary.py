"""The benchmark's summary: the only place a speed number on the site may come from.

summarize() turns docs/benchmark/results.csv into docs/benchmark/summary.json. It must refuse to
produce a Plex-vs-app ratio whenever the Plex side wasn't timed in every run, there are too few runs,
or the runs disagree too much to call one number representative (spec §8: drop the number rather
than estimate).
"""

from __future__ import annotations

from scripts.benchmark_previews import MIN_RUNS, summarize


def _rows(tool: str, seconds: list[float], status: str = "timed") -> list[dict[str, str]]:
    return [{"tool": tool, "run": str(i), "status": status, "wall_seconds": str(s)} for i, s in enumerate(seconds, 1)]


class TestSummarize:
    def test_ratio_is_median_plex_time_over_median_gpu_time(self) -> None:
        rows = (
            _rows("plex-builtin", [600, 620, 610])
            + _rows("app-gpu", [100, 98, 102])
            + _rows("app-cpu", [300, 310, 305])
        )
        summary = summarize(rows)
        assert summary["tools"]["plex-builtin"]["median_seconds"] == 610
        assert summary["tools"]["app-gpu"]["median_seconds"] == 100
        assert summary["ratio_plex_over_app_gpu"] == 6.1
        assert summary["ratio_display"] == "6.1"

    def test_no_ratio_when_any_plex_run_was_untimed(self) -> None:
        rows = (
            _rows("plex-builtin", [600, 620])
            + _rows("plex-builtin", [0], status="untimed")
            + _rows("app-gpu", [100, 98, 102])
        )
        summary = summarize(rows)
        assert summary["ratio_plex_over_app_gpu"] is None
        assert summary["ratio_display"] is None
        assert "Plex" in summary["reason"]

    def test_no_ratio_with_too_few_runs(self) -> None:
        rows = _rows("plex-builtin", [600] * (MIN_RUNS - 1)) + _rows("app-gpu", [100] * MIN_RUNS)
        assert summarize(rows)["ratio_display"] is None

    def test_no_ratio_when_runs_disagree_by_more_than_a_quarter(self) -> None:
        rows = _rows("plex-builtin", [400, 600, 800]) + _rows("app-gpu", [100, 98, 102])
        summary = summarize(rows)
        assert summary["ratio_display"] is None
        assert "varied" in summary["reason"]

    def test_display_rounds_to_one_decimal(self) -> None:
        rows = _rows("plex-builtin", [1000, 1000, 1000]) + _rows("app-gpu", [300, 300, 300])
        assert summarize(rows)["ratio_display"] == "3.3"
