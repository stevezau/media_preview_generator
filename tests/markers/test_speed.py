"""Playback speed families: a 25 fps release of a film-rate show plays 25 / 23.976 faster (a PAL speed-up)."""

from __future__ import annotations

import pytest

from media_preview_generator.markers import speed
from media_preview_generator.markers.speed import FILM_FPS, PAL_FPS


class TestPlaybackSpeed:
    @pytest.mark.parametrize(
        ("frame_rate", "expected"),
        [
            (24000 / 1001, FILM_FPS),
            (23.98, FILM_FPS),
            (24.0, FILM_FPS),
            (25.0, PAL_FPS),
            (25.02, PAL_FPS),
        ],
        ids=["23.976", "23.98", "24", "25", "25.02"],
    )
    def test_film_and_pal_rates_name_their_family(self, frame_rate, expected):
        assert speed.playback_speed(frame_rate) == expected

    @pytest.mark.parametrize(
        "frame_rate",
        [None, 0.0, 23.5, 29.97, 30.0, 50.0, 59.94],
        ids=["unknown", "zero", "23.5", "29.97", "30", "50", "59.94"],
    )
    def test_any_other_rate_has_no_family(self, frame_rate):
        assert speed.playback_speed(frame_rate) is None


class TestMatchSpeed:
    def test_a_group_at_one_speed_is_matched_as_it_is(self):
        assert speed.match_speed([FILM_FPS, FILM_FPS, FILM_FPS]) is None
        assert speed.match_speed([PAL_FPS, PAL_FPS]) is None

    def test_files_of_unknown_speed_never_make_a_group_mixed(self):
        assert speed.match_speed([FILM_FPS, None, FILM_FPS, None]) is None
        assert speed.match_speed([None, None]) is None

    def test_a_mixed_group_is_matched_at_the_speed_most_of_its_files_play_at(self):
        assert speed.match_speed([PAL_FPS] * 12 + [FILM_FPS]) == PAL_FPS
        assert speed.match_speed([PAL_FPS] * 5 + [FILM_FPS] * 17 + [None]) == FILM_FPS

    def test_a_tie_is_matched_at_film_speed(self):
        assert speed.match_speed([PAL_FPS, FILM_FPS]) == FILM_FPS
        assert speed.match_speed([FILM_FPS, PAL_FPS, None]) == FILM_FPS


class TestRetimeFactor:
    def test_a_pal_file_in_a_film_group_runs_23_976_own_seconds_per_25_of_the_group(self):
        assert speed.retime_factor(PAL_FPS, FILM_FPS) == pytest.approx(0.95904, abs=1e-5)

    def test_a_film_file_in_a_pal_group_runs_25_own_seconds_per_23_976_of_the_group(self):
        assert speed.retime_factor(FILM_FPS, PAL_FPS) == pytest.approx(1.04271, abs=1e-5)

    @pytest.mark.parametrize(
        ("own", "clock"),
        [(FILM_FPS, FILM_FPS), (PAL_FPS, PAL_FPS), (None, FILM_FPS), (PAL_FPS, None), (None, None)],
        ids=["film-in-film", "pal-in-pal", "unknown-file", "one-speed-group", "neither"],
    )
    def test_a_file_at_the_groups_speed_or_of_unknown_speed_is_not_retimed(self, own, clock):
        assert speed.retime_factor(own, clock) is None


class TestOnlineTimeScale:
    def test_on_a_25_fps_file_film_rate_times_run_4_percent_late(self):
        # Bones S07E01 (25 fps): IntroDB 324-354 s from a 23.976 release; the file's own theme plays 310-338 s.
        scale = speed.online_time_scale(25.0)
        assert scale == pytest.approx(FILM_FPS / PAL_FPS)
        assert (324 * scale, 354 * scale) == (pytest.approx(310.7, abs=0.1), pytest.approx(339.5, abs=0.1))

    @pytest.mark.parametrize("frame_rate", [24000 / 1001, 24.0], ids=["23.976", "24"])
    def test_on_a_film_rate_file_pal_times_run_4_percent_early(self, frame_rate):
        assert speed.online_time_scale(frame_rate) == pytest.approx(PAL_FPS / FILM_FPS)

    @pytest.mark.parametrize("frame_rate", [None, 29.97, 30.0, 50.0], ids=["unknown", "29.97", "30", "50"])
    def test_any_other_rate_is_never_scaled(self, frame_rate):
        assert speed.online_time_scale(frame_rate) is None
