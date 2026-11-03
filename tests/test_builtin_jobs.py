"""Settings for the built-in daily jobs: tolerant reads, strict updates."""

import pytest

from media_preview_generator.web.builtin_jobs import (
    DEFAULT_BUILTIN_JOBS,
    parse_time,
    read_builtin_jobs,
    validate_builtin_update,
)


class TestReadBuiltinJobs:
    @pytest.mark.parametrize("raw", [None, {}, [], "x", 5, {"library_health": "x"}, {"check_servers": None}])
    def test_defaults_when_missing_or_not_the_expected_shape(self, raw):
        assert read_builtin_jobs(raw) == DEFAULT_BUILTIN_JOBS

    def test_saved_fields_are_kept(self):
        raw = {
            "library_health": {"enabled": False, "time": "02:15", "catch_up": False},
            "check_servers": {"enabled": False, "time": "05:00"},
        }

        assert read_builtin_jobs(raw) == raw

    def test_each_invalid_field_falls_back_on_its_own(self):
        raw = {"library_health": {"enabled": "yes", "time": "25:00", "catch_up": False}, "check_servers": {"time": 430}}

        result = read_builtin_jobs(raw)

        assert result["library_health"] == {"enabled": True, "time": "03:00", "catch_up": False}
        assert result["check_servers"] == {"enabled": True, "time": "04:30"}

    def test_unknown_fields_are_dropped(self):
        result = read_builtin_jobs({"check_servers": {"time": "05:00", "enabled": False}, "other": {}, "x": 1})

        assert result == {**DEFAULT_BUILTIN_JOBS, "check_servers": {"enabled": False, "time": "05:00"}}

    def test_the_defaults_are_not_shared_with_the_result(self):
        read_builtin_jobs(None)["library_health"]["time"] = "09:00"

        assert DEFAULT_BUILTIN_JOBS["library_health"]["time"] == "03:00"


class TestParseTime:
    def test_splits_hour_and_minute(self):
        assert parse_time("02:15") == (2, 15)

    @pytest.mark.parametrize("value", ["2:15", "24:00", "12:60", "12", "ab:cd", "", None, 215])
    def test_rejects_anything_but_zero_padded_hh_mm(self, value):
        with pytest.raises(ValueError, match="HH:MM"):
            parse_time(value)


class TestValidateBuiltinUpdate:
    def test_a_partial_update_is_returned_as_is(self):
        assert validate_builtin_update("library_health", {"catch_up": False}) == {"catch_up": False}

    def test_check_servers_accepts_time_and_enabled_but_not_catch_up(self):
        assert validate_builtin_update("check_servers", {"time": "05:00"}) == {"time": "05:00"}
        assert validate_builtin_update("check_servers", {"enabled": False}) == {"enabled": False}
        with pytest.raises(ValueError, match="Unknown field"):
            validate_builtin_update("check_servers", {"catch_up": False})

    def test_unknown_job_is_a_key_error(self):
        with pytest.raises(KeyError):
            validate_builtin_update("nope", {"time": "05:00"})

    @pytest.mark.parametrize("body", [None, [], {}, "x"])
    def test_body_must_be_a_non_empty_object(self, body):
        with pytest.raises(ValueError, match="JSON object"):
            validate_builtin_update("library_health", body)

    @pytest.mark.parametrize("body", [{"enabled": "yes"}, {"catch_up": 1}, {"time": "3pm"}, {"time": None}])
    def test_bad_values_are_rejected(self, body):
        with pytest.raises(ValueError):
            validate_builtin_update("library_health", body)

    def test_unknown_field_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown field"):
            validate_builtin_update("library_health", {"catch_up_max_files": 5})
