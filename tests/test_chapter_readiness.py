"""Chapter health distinguishes a usable writer from unknown or competing settings."""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.chapter_readiness import CHAPTER_PREF, chapter_readiness_section


def _config(output):
    return ServerConfig(
        id="plex-chapters",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://plex:32400",
        auth={},
        output=output,
    )


@pytest.mark.parametrize("output", [{}, {"chapter_thumbnails": False}, {"chapter_thumbnails": "true"}])
def test_disabled_chapters_do_not_probe_database_or_helper(output):
    server = MagicMock()
    with patch("media_preview_generator.servers.plex_chapters.chapter_capability") as capability:
        assert chapter_readiness_section(server, _config(output), {}) is None
    capability.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "expected_ok", "expected_label"),
    [
        ("never", True, "Plex's own chapter generation is off"),
        ("scheduled", False, "Plex also generates chapter thumbnails"),
        ("asap", False, "Plex also generates chapter thumbnails"),
        (None, False, "Could not check Plex's chapter setting"),
        ("new-value", False, "Could not check Plex's chapter setting"),
        (False, False, "Could not check Plex's chapter setting"),
        ({}, False, "Could not check Plex's chapter setting"),
    ],
)
def test_native_setting_is_independent_of_writer_readiness(mode, expected_ok, expected_label):
    server = MagicMock()
    config = _config({"chapter_thumbnails": True})
    with patch(
        "media_preview_generator.servers.plex_chapters.chapter_capability",
        return_value=MagicMock(ready=True, message="Ready"),
    ) as capability:
        section = chapter_readiness_section(server, config, {CHAPTER_PREF: mode})

    capability.assert_called_once_with(server, config)
    assert section["ok"] is expected_ok
    registration, native = section["checks"]
    assert registration["ok"] is True
    assert native["ok"] is expected_ok
    assert native["label"] == expected_label
    assert native["severity"] == "recommended"  # Unknown must not disappear into the UI's info filter.
    assert native["actions"] == {}


def test_refused_writer_is_actionable_without_enabling_intro_credits():
    with patch(
        "media_preview_generator.servers.plex_chapters.chapter_capability",
        return_value=MagicMock(ready=False, message="The Plex helper belongs to another server."),
    ):
        section = chapter_readiness_section(MagicMock(), _config({"chapter_thumbnails": True}), {CHAPTER_PREF: "never"})

    assert section["ok"] is False
    assert section["severity"] == "critical"
    registration = section["checks"][0]
    assert registration["reason"] == "The Plex helper belongs to another server."
    assert registration["note"]["configure_plex_helper"] is True
    assert "without enabling Intro & Credits" in registration["note"]["text"]
    assert registration["actions"] == {}


def test_unavailable_writer_probe_is_not_healthy():
    with patch("media_preview_generator.servers.plex_chapters.chapter_capability", side_effect=RuntimeError("offline")):
        section = chapter_readiness_section(MagicMock(), _config({"chapter_thumbnails": True}), {})

    assert section["ok"] is False
    assert all(check["ok"] is False for check in section["checks"])
    assert "Could not check chapter registration" in section["checks"][0]["reason"]
