"""Loudness health reports unusable local writers independently of markers."""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.servers.loudness_readiness import loudness_readiness_section

from .test_settings import _cfg


@pytest.mark.parametrize("block", [{}, {"enabled": False}])
def test_disabled_loudness_never_probes_database(block):
    with patch("media_preview_generator.loudness.guard.loudness_capability") as capability:
        assert loudness_readiness_section(MagicMock(), _cfg(block)) is None
    capability.assert_not_called()


@pytest.mark.parametrize("enabled", ["false", "true", "", 0, 1, {}, [], None])
def test_invalid_enable_is_reported_without_probing_the_database(enabled):
    with patch("media_preview_generator.loudness.guard.loudness_capability") as capability:
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": enabled}))
    capability.assert_not_called()
    assert not section["ok"]
    assert section["checks"][0]["reason"] == "loudness.enabled must be a boolean"


@pytest.mark.parametrize("ready", [True, False])
def test_health_reports_capability_without_marker_consent(ready):
    config = _cfg({"enabled": True}, markers={"enabled": False})
    server = MagicMock()
    with patch(
        "media_preview_generator.loudness.guard.loudness_capability",
        return_value=MagicMock(ready=ready, message="Wrong server"),
    ) as capability:
        section = loudness_readiness_section(server, config)
    capability.assert_called_once_with(server, config)
    assert section["ok"] is ready
    check = section["checks"][0]
    assert check["reason"] == (None if ready else "Wrong server")
    assert check["severity"] == ("recommended" if ready else "critical")
    assert check["actions"] == {}


def test_failed_probe_is_unhealthy_with_actionable_message():
    with patch("media_preview_generator.loudness.guard.loudness_capability", side_effect=RuntimeError("offline")):
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": True}))
    assert not section["ok"]
    assert "Check the Plex connection" in section["checks"][0]["reason"]


def test_stored_unsupported_music_selection_is_not_reported_as_ready():
    with patch("media_preview_generator.loudness.guard.loudness_capability") as capability:
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": True, "library_ids": ["3"]}))
    capability.assert_not_called()
    assert not section["ok"]
    assert "movie and TV" in section["checks"][0]["reason"]


@pytest.mark.parametrize("mode", ["never", "scheduled", "asap", None, "new-value", []])
def test_native_schedule_is_recommended_off(mode):
    prefs = {"LoudnessAnalysisBehavior": mode}
    server = MagicMock()
    cfg = _cfg({"enabled": True})
    with patch(
        "media_preview_generator.loudness.guard.loudness_capability", return_value=MagicMock(ready=True, message="")
    ) as capability:
        section = loudness_readiness_section(server, cfg, prefs)
    capability.assert_called_once_with(server, cfg)
    rows = {check["id"]: check for check in section["checks"]}
    if mode not in ("never", "scheduled", "asap"):
        assert "loudness_plex_analysis" not in rows
        assert section["ok"] is True
        return
    row = rows["loudness_plex_analysis"]
    assert row["severity"] == "recommended" and "informational" not in row
    assert row["recommended"] == "Never"
    assert row["bulk"] is False
    assert section["ok"] is (mode == "never")
    if mode == "never":
        assert row["ok"] is True and row["reason"] is None and row["actions"] == {}
    else:
        assert row["ok"] is False
        assert "repeating this app's work" in row["reason"]
        assert row["fix_action"] == "disable" and row["fix_label"] == "Set to Never"
        action = row["actions"]["disable"]
        assert action["action"] == "set_plex_loudness_never"
        assert all(term in action["confirm"]["body"] for term in ["music", "unselected", "Existing measurements"])


@pytest.mark.parametrize(
    "library_ids,ready,action",
    [
        (None, True, True),
        (["1"], True, True),
        ([], True, False),
        (["missing"], True, False),
        (None, False, False),
        ([], False, False),
    ],
)
def test_optional_control_requires_current_writer_and_nonempty_video_selection(library_ids, ready, action):
    cfg = _cfg({"enabled": True, "library_ids": library_ids})
    server = MagicMock()
    with patch(
        "media_preview_generator.loudness.guard.loudness_capability",
        return_value=MagicMock(ready=ready, message="Writer not ready"),
    ) as capability:
        section = loudness_readiness_section(server, cfg, {"LoudnessAnalysisBehavior": "scheduled"})
    capability.assert_called_once_with(server, cfg)
    row = next(row for row in section["checks"] if row["id"] == "loudness_plex_analysis")
    assert section["ok"] is False
    assert bool(row["actions"]) is action
    assert row["ok"] is False and row["severity"] == "recommended"
    assert "repeating this app's work" in row["reason"]
    if not action:
        assert "Set to Never" in row["reason"]


def test_native_information_remains_visible_when_probe_fails():
    with patch("media_preview_generator.loudness.guard.loudness_capability", side_effect=RuntimeError("offline")):
        section = loudness_readiness_section(
            MagicMock(), _cfg({"enabled": True}), {"LoudnessAnalysisBehavior": "scheduled"}
        )
    assert section["ok"] is False and section["severity"] == "critical"
    row = section["checks"][1]
    assert row["id"] == "loudness_plex_analysis" and row["actions"] == {}
    assert row["ok"] is False and "unavailable until this app can store" in row["reason"]
    assert loudness_readiness_section(MagicMock(), _cfg({"enabled": False})) is None


def _plex_state(switches, music, extra_sections=()):
    """Patch what the section reads from Plex: per-library switches, whether a music library exists, and any other
    library sections beyond this app's movie library 1 and TV library 2."""
    sections = [MagicMock(key="1", type="movie"), MagicMock(key="2", type="show")]
    if music:
        sections.append(MagicMock(key="9", type="artist"))
    sections.extend(MagicMock(key=key, type=kind) for key, kind in extra_sections)
    server = MagicMock()
    server._connect.return_value.library.sections.return_value = sections
    patcher = patch("media_preview_generator.servers.plex_scheduled_jobs.read_library_prefs")
    return server, patcher, switches


def _row(server, patcher, switches, mode="scheduled"):
    ready = MagicMock(ready=True, message="")
    with (
        patch("media_preview_generator.loudness.guard.loudness_capability", return_value=ready),
        patcher as read,
    ):
        if isinstance(switches, Exception):
            read.side_effect = switches
        else:
            read.return_value = switches
        section = loudness_readiness_section(server, _cfg({"enabled": True}), {"LoudnessAnalysisBehavior": mode})
    return section, next(row for row in section["checks"] if row["id"] == "loudness_plex_analysis")


def test_scheduled_server_passes_when_every_selected_library_is_off():
    section, row = _row(
        *_plex_state({"1": {"enableLoudnessAnalysis": False}, "2": {"enableLoudnessAnalysis": False}}, True)
    )
    assert row["ok"] is True and section["ok"] is True
    assert row["label"] == "Plex's own loudness analysis is off in this app's libraries"
    assert row["actions"] == {} and row["reason"] is None


def test_music_library_gets_a_per_library_fix_for_only_the_libraries_still_on():
    section, row = _row(
        *_plex_state({"1": {"enableLoudnessAnalysis": False}, "2": {"enableLoudnessAnalysis": True}}, True)
    )
    assert row["ok"] is False and section["ok"] is False
    assert row["label"] == "Plex also analyses loudness"
    assert "TV" in row["reason"] and "Filmer" not in row["reason"]
    assert row["current"] == "As a scheduled task; on in TV"
    assert row["recommended"] == "Off in Filmer, TV"
    assert row["fix_label"] == "Turn off in this app's libraries"
    action = row["actions"]["disable"]
    assert action["action"] == "set_plex_loudness_library_off"
    assert action["args"] == {"library_ids": ["2"]}
    assert "music" in action["confirm"]["body"] and "TV" in action["confirm"]["body"]
    assert row["bulk"] is False


def test_no_music_library_keeps_the_server_wide_never_fix():
    _, row = _row(*_plex_state({"1": {"enableLoudnessAnalysis": True}}, False))
    assert row["ok"] is False
    assert row["actions"]["disable"]["action"] == "set_plex_loudness_never"
    assert row["fix_label"] == "Set to Never" and row["recommended"] == "Never"


def test_a_movie_or_tv_library_this_app_does_not_measure_gets_the_per_library_fix():
    _, row = _row(*_plex_state({"1": {"enableLoudnessAnalysis": True}}, False, extra_sections=[("5", "movie")]))
    assert row["actions"]["disable"]["action"] == "set_plex_loudness_library_off"
    assert row["fix_label"] == "Turn off in this app's libraries"


def test_a_photo_library_does_not_count_as_another_library():
    _, row = _row(*_plex_state({"1": {"enableLoudnessAnalysis": True}}, False, extra_sections=[("7", "photo")]))
    assert row["actions"]["disable"]["action"] == "set_plex_loudness_never"


def test_unreadable_library_list_offers_no_action():
    server, patcher, switches = _plex_state({"1": {"enableLoudnessAnalysis": True}}, False)
    server._connect.return_value.library.sections.side_effect = RuntimeError("down")
    section, row = _row(server, patcher, switches)
    assert row["ok"] is False and section["ok"] is False
    assert row["actions"] == {}
    assert "Couldn't read Plex's libraries. Try Setup Health again." in row["reason"]


def test_libraries_are_listed_once():
    server, patcher, switches = _plex_state({"1": {"enableLoudnessAnalysis": True}}, True)
    _row(server, patcher, switches)
    server._connect.return_value.library.sections.assert_called_once()


@pytest.mark.parametrize("switches", [RuntimeError("down"), {}, {"1": {}, "2": {}}])
def test_a_library_switch_that_cant_be_read_counts_as_still_on(switches):
    section, row = _row(*_plex_state(switches, True))
    assert row["ok"] is False and section["ok"] is False
    assert row["actions"]["disable"]["args"] == {"library_ids": ["1", "2"]}


def test_server_wide_never_passes_without_reading_libraries():
    section, row = _row(*_plex_state({}, True), mode="never")
    assert row["ok"] is True and section["ok"] is True
    assert row["label"] == "Plex's own loudness analysis is off"
