"""Loudness operations refuse an unproven Plex identity, version or database."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree.ElementTree import Element

import pytest

from media_preview_generator.loudness.guard import create_loudness_db, loudness_capability
from media_preview_generator.markers.publishers.base import Capability, CapabilityReport, PublishError
from media_preview_generator.markers.publishers.plex_db import plex_db_path
from media_preview_generator.servers.base import ServerType


@pytest.fixture
def guarded(tmp_path):
    folder = tmp_path / "Plex Media Server"
    path = Path(plex_db_path(str(folder)))
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript((Path(__file__).parents[1] / "fixtures/markers/plex_schema_1_43.sql").read_text())
    conn.close()
    (folder / "Preferences.xml").write_text('<Preferences ProcessedMachineIdentifier="plex-a"/>')
    cfg = SimpleNamespace(type=ServerType.PLEX, markers={}, output={"plex_config_folder": str(folder)}, name="Plex A")
    server = MagicMock()
    server._connect.return_value.query.return_value = Element(
        "MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123"
    )
    return create_loudness_db(cfg, server), cfg, server


@pytest.mark.parametrize("read_only", [True, False])
def test_every_operation_proves_identity_and_preserves_wal(guarded, read_only, monkeypatch):
    db, _, server = guarded
    checks = MagicMock(return_value=CapabilityReport(Capability.READY, "Ready"))
    monkeypatch.setattr(db, "file_checks", checks)
    with db._database(read_only=read_only, deadline=1e12) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.machine_identifier == "plex-a"
    server._connect.return_value.query.assert_called_once_with("/identity")
    if read_only:
        checks.assert_not_called()
    else:
        checks.assert_called_once_with(deadline=1e12)


@pytest.mark.parametrize("state", [Capability.UNREACHABLE, Capability.NEEDS_LOCAL_DB, Capability.MISCONFIGURED])
def test_cached_readiness_cannot_authorize_a_later_unsafe_write(guarded, monkeypatch, state):
    db, _, _ = guarded
    checks = MagicMock(
        side_effect=[
            CapabilityReport(Capability.READY, "Ready"),
            CapabilityReport(state, "Database is no longer safe to write"),
        ]
    )
    monkeypatch.setattr(db, "file_checks", checks)
    with db._database(read_only=False, deadline=1e12) as conn:
        conn.execute("INSERT INTO metadata_items(id, metadata_type, title) VALUES(1, 1, 'Original')")
    with pytest.raises(PublishError, match="no longer safe") as caught:
        with db._database(read_only=False, deadline=1e12) as conn:
            conn.execute("UPDATE metadata_items SET title='Changed' WHERE id=1")
    assert caught.value.state is state
    with db._database(read_only=True, deadline=1e12) as conn:
        assert conn.execute("SELECT title FROM metadata_items WHERE id=1").fetchone()[0] == "Original"
    assert checks.call_count == 2


@pytest.mark.parametrize("machine", [None, "", "plex-b"])
def test_unknown_or_different_api_identity_is_refused(guarded, machine):
    db, _, server = guarded
    server._connect.return_value.query.return_value = {"machineIdentifier": machine, "version": "1.43.4.12345"}
    with pytest.raises(PublishError, match="Cannot prove"):
        with db._database(read_only=False, deadline=1e12):
            pytest.fail("Unproven database was opened")


@pytest.mark.parametrize("version", [None, "", "1.43.3.12345", "1.44.0.1", "1.43.4", "1.43.4.12345-unexpected!"])
def test_untested_versions_are_refused(guarded, version):
    db, _, server = guarded
    server._connect.return_value.query.return_value = {"machineIdentifier": "plex-a", "version": version}
    with pytest.raises(PublishError, match="only been verified"):
        with db._database(read_only=False, deadline=1e12):
            pytest.fail("Untested server was opened")


@pytest.mark.parametrize(
    "preferences",
    [
        None,
        "not xml",
        '<Preferences ProcessedMachineIdentifier="plex-b"/>',
        "<Preferences/>",
        '<!DOCTYPE Preferences [<!ENTITY x "plex-a">]><Preferences ProcessedMachineIdentifier="&x;"/>',
    ],
)
def test_preferences_must_safely_prove_the_same_server(guarded, preferences):
    db, _, _ = guarded
    path = db.folder / "Preferences.xml"
    if preferences is None:
        path.unlink()
    else:
        path.write_text(preferences)
    with pytest.raises(PublishError, match="Cannot prove"):
        with db._database(read_only=True, deadline=1e12):
            pytest.fail("Unproven preferences were accepted")


def test_identity_is_checked_again_after_an_earlier_success(guarded):
    db, _, server = guarded
    with db._database(read_only=True, deadline=1e12):
        pass
    server._connect.return_value.query.return_value.set("machineIdentifier", "plex-b")
    with pytest.raises(PublishError, match="Cannot prove"):
        with db._database(read_only=False, deadline=1e12):
            pytest.fail("Cached readiness allowed a write to another server")


def test_connection_errors_do_not_expose_tokens(guarded):
    db, _, server = guarded
    server._connect.side_effect = RuntimeError("http://plex/?X-Plex-Token=secret-token")
    with pytest.raises(PublishError, match="Cannot verify") as caught:
        with db._database(read_only=True, deadline=1e12):
            pass
    assert "secret-token" not in str(caught.value)


def test_non_wal_database_is_refused(guarded, monkeypatch):
    db, _, _ = guarded
    monkeypatch.setattr(db, "file_checks", lambda **kwargs: CapabilityReport(Capability.READY, "Ready"))
    with sqlite3.connect(db._path()) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    with pytest.raises(PublishError, match="live WAL"):
        with db._database(read_only=False, deadline=1e12):
            pytest.fail("Non-WAL database was accepted")


def test_capability_uses_own_schema_without_marker_consent(guarded, monkeypatch):
    _, cfg, server = guarded
    monkeypatch.setattr(
        "media_preview_generator.loudness.guard.LocalPlexDb.file_checks",
        lambda self, **kwargs: CapabilityReport(Capability.READY, ""),
    )
    assert loudness_capability(server, cfg).ready


def test_capability_preserves_file_readiness_failure(guarded, monkeypatch):
    _, cfg, server = guarded
    monkeypatch.setattr(
        "media_preview_generator.loudness.guard.LocalPlexDb.file_checks",
        lambda self, **kwargs: CapabilityReport(Capability.NEEDS_LOCAL_DB, "Network share"),
    )
    report = loudness_capability(server, cfg)
    assert report.state is Capability.NEEDS_LOCAL_DB and report.message == "Network share"
    server._connect.assert_not_called()


def test_capability_refuses_changed_schema(guarded, monkeypatch):
    db, cfg, server = guarded
    with sqlite3.connect(db._path()) as conn:
        conn.execute("DROP TABLE media_streams")
    monkeypatch.setattr(
        "media_preview_generator.loudness.guard.LocalPlexDb.file_checks",
        lambda self, **kwargs: CapabilityReport(Capability.READY, ""),
    )
    assert loudness_capability(server, cfg).state is Capability.UNSUPPORTED_SCHEMA


def test_helper_is_explicitly_unavailable(guarded):
    _, cfg, server = guarded
    cfg.markers = {"plex": {"agent": {"enabled": True}}}
    report = loudness_capability(server, cfg)
    assert report.state is Capability.MISCONFIGURED and "helper does not support" in report.message
    server._connect.assert_not_called()


def test_missing_folder_is_refused(guarded):
    _, cfg, server = guarded
    cfg.output = {}
    report = loudness_capability(server, cfg)
    assert report.state is Capability.MISCONFIGURED and "config folder" in report.message


def test_other_server_types_are_refused(guarded):
    _, cfg, server = guarded
    cfg.type = ServerType.JELLYFIN
    assert loudness_capability(server, cfg).state is Capability.MISCONFIGURED


@pytest.fixture
def served(guarded):
    from xml.etree.ElementTree import SubElement

    from media_preview_generator.loudness.plex_db import AudioStream
    from media_preview_generator.markers.publishers.plex_db import encode_extra_data

    from .test_plex_db import FIELDS

    db, _, server = guarded
    expected = AudioStream(
        11, 1, "aac", encode_extra_data(FIELDS), 5000, "/media/film.mkv", 7, True, 3, 2, "hash", 123, 1, 1, 2
    )
    metadata = Element("MediaContainer")
    part = SubElement(
        SubElement(SubElement(metadata, "Video", ratingKey="7"), "Media", id="2"), "Part", id="3", file=expected.file
    )
    audio = SubElement(
        part,
        "Stream",
        id="11",
        index="1",
        streamType="2",
        canNormalizeLoudness="1",
        **{key.removeprefix("ln:"): value for key, value in FIELDS.items()},
    )
    identity = server._connect.return_value.query.return_value
    server._connect.return_value.query.side_effect = lambda path: identity if path == "/identity" else metadata
    return db, server, expected, part, audio


def test_readback_compares_exact_audio_identity_and_numeric_measurements(served):
    db, server, expected, _, audio = served
    audio.set("lra", "7.3000")
    db.verify_streams([expected])
    assert [call.args[0] for call in server._connect.return_value.query.call_args_list] == [
        "/identity",
        "/library/metadata/7",
    ]


@pytest.mark.parametrize(
    "change",
    [
        ("part", "id", "99"),
        ("part", "file", "/media/other.mkv"),
        ("stream", "id", "99"),
        ("stream", "index", "2"),
        ("stream", "streamType", "1"),
        ("stream", "loudness", "-99"),
        ("stream", "peak", "nan"),
        ("stream", "loudnessAnalysisVersion", "0.03"),
        ("stream", "canNormalizeLoudness", "0"),
    ],
)
def test_unavailable_or_mismatched_api_analysis_is_retryable(served, change):
    db, _, expected, part, audio = served
    target, key, value = change
    (part if target == "part" else audio).set(key, value)
    with pytest.raises(PublishError) as caught:
        db.verify_streams([expected])
    assert caught.value.state is Capability.UNREACHABLE


def test_readback_requires_measurements_and_stream_to_be_present(served):
    db, _, expected, part, audio = served
    del audio.attrib["gainOffset"]
    with pytest.raises(PublishError):
        db.verify_streams([expected])
    part.remove(audio)
    with pytest.raises(PublishError):
        db.verify_streams([expected])


def test_readback_accepts_native_silence_values(served):
    from media_preview_generator.markers.publishers.plex_db import decode_extra_data, encode_extra_data

    db, _, expected, _, audio = served
    fields = decode_extra_data(expected.extra_data)[0]
    for key, value in (("loudness", "-inf"), ("peak", "-inf"), ("gainOffset", "inf")):
        audio.set(key, value)
        fields[f"ln:{key}"] = value
    native = expected._replace(extra_data=encode_extra_data(fields))
    db.verify_streams([native])

    audio.set("loudness", "-23.0")
    with pytest.raises(PublishError):
        db.verify_streams([native])


def test_readback_connection_failure_is_retryable_and_does_not_leak_tokens(served):
    db, server, expected, _, _ = served
    identity = server._connect.return_value.query("/identity")

    def query(path):
        if path == "/identity":
            return identity
        raise RuntimeError("http://plex/?X-Plex-Token=secret-token")

    server._connect.return_value.query.side_effect = query
    with pytest.raises(PublishError) as caught:
        db.verify_streams([expected])
    assert caught.value.state is Capability.UNREACHABLE
    assert "secret-token" not in str(caught.value)
