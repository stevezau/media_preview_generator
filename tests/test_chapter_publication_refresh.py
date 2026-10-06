"""A timestamp-only retry keeps the real SQLite chapter transaction guarded."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

from media_preview_generator.markers.publishers import plex_chapters
from media_preview_generator.output.plex_hash import SourceFileChangedError, get_source_fingerprint
from tests.test_plex_chapters import MACHINE, VERSION, images, mutate, read, rows
from tests.test_plex_chapters import backend as _backend_fixture

backend = _backend_fixture


@pytest.fixture
def publication(backend, tmp_path, monkeypatch):
    target = read(backend)
    _, revisions = images(backend)
    source = tmp_path / "mounted-source.mkv"
    source.write_bytes(b"stable source bytes")
    fingerprint = get_source_fingerprint(source)

    def verify():
        if get_source_fingerprint(source) != fingerprint:
            raise SourceFileChangedError("Source changed during chapter publication")

    def query(path):
        if path == "/identity":
            return ET.fromstring(f'<MediaContainer machineIdentifier="{MACHINE}"/>')
        assert path == "/library/metadata/1?includeChapters=1"
        result = ET.Element("MediaContainer")
        for row in rows(backend):
            if row[2] == 9:
                ET.SubElement(result, "Chapter", index=str(row[3]), thumb=row[6])
        return result

    api = MagicMock()
    api.query.side_effect = query
    server = SimpleNamespace(_connect=lambda: api)
    monkeypatch.setattr(plex_chapters, "_context", lambda _server: (backend, MACHINE, VERSION, None))
    register = MagicMock(wraps=backend.register)
    monkeypatch.setattr(backend, "register", register)
    return SimpleNamespace(
        backend=backend,
        target=target,
        revisions=revisions,
        source=source,
        server=server,
        verify=MagicMock(side_effect=verify),
        register=register,
        api=api,
    )


def test_timestamp_only_refresh_registers_exact_images_and_verifies_native_api(publication):
    p = publication
    mutate(p.backend, "UPDATE media_parts SET updated_at=101")
    before = rows(p.backend)

    plex_chapters.register_chapters(p.server, p.target, p.revisions, verify_source=p.verify)

    assert p.register.call_count == 2
    for call, timestamp in zip(p.register.call_args_list, (100, 101), strict=True):
        assert call.args[0].source_updated_at == timestamp
        assert call.args[0].chapters == p.target.chapters
        assert call.args[1:] == (p.revisions, VERSION)
        assert call.kwargs["deadline"] > 0
    assert p.verify.call_count == 4
    assert [call.args[0] for call in p.api.query.call_args_list] == [
        "/identity",
        "/library/metadata/1?includeChapters=1",
    ]
    after = rows(p.backend)
    for old, new in zip(before, after, strict=True):
        assert old[:6] == new[:6] and old[7:] == new[7:]
    assert after[0][6] == plex_chapters.chapter_url(10, 1, p.revisions[1])
    assert after[1][6] == plex_chapters.chapter_url(10, 2, p.revisions[2])
    assert after[2] == before[2]


@pytest.mark.parametrize(
    "sql,field",
    [
        ("UPDATE taggings SET thumb_url='/new-native-owner' WHERE id=1", "chapters"),
        ("UPDATE taggings SET time_offset=1 WHERE id=1", "chapters"),
        ("UPDATE media_parts SET size=65537", "source_size"),
        ("UPDATE media_parts SET hash='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'", "bundle_hash"),
    ],
)
def test_timestamp_refresh_never_overwrites_a_real_concurrent_change(publication, sql, field):
    p = publication
    mutate(p.backend, "UPDATE media_parts SET updated_at=101")
    mutate(p.backend, sql)
    before = rows(p.backend)

    with pytest.raises(plex_chapters.ChapterError, match=field) as caught:
        plex_chapters.register_chapters(p.server, p.target, p.revisions, verify_source=p.verify)

    assert caught.value.code == "source_changed"
    assert rows(p.backend) == before
    assert p.register.call_count == 1
    p.api.query.assert_not_called()


def test_source_replacement_after_fresh_snapshot_prevents_retry_write(publication):
    p = publication
    mutate(p.backend, "UPDATE media_parts SET updated_at=101")
    before = rows(p.backend)
    original = p.verify.side_effect

    def replace_before_retry():
        if p.verify.call_count == 2:
            p.source.write_bytes(b"new source bytes")
        original()

    p.verify.side_effect = replace_before_retry
    with pytest.raises(SourceFileChangedError):
        plex_chapters.register_chapters(p.server, p.target, p.revisions, verify_source=p.verify)

    assert rows(p.backend) == before
    assert p.register.call_count == 1
    p.api.query.assert_not_called()


def test_second_concurrent_timestamp_change_is_bounded_and_keeps_guard(publication):
    p = publication
    mutate(p.backend, "UPDATE media_parts SET updated_at=101")
    before = rows(p.backend)
    original = p.register._mock_wraps

    def change_again(target, revisions, version, **kwargs):
        if p.register.call_count == 2:
            mutate(p.backend, "UPDATE media_parts SET updated_at=102")
        return original(target, revisions, version, **kwargs)

    p.register.side_effect = change_again
    with pytest.raises(plex_chapters.ChapterError) as caught:
        plex_chapters.register_chapters(p.server, p.target, p.revisions, verify_source=p.verify)

    assert caught.value.code == "source_changed"
    assert p.register.call_count == 2
    assert rows(p.backend) == before
    p.api.query.assert_not_called()


def test_timestamp_refresh_requires_source_verifier(publication):
    p = publication
    mutate(p.backend, "UPDATE media_parts SET updated_at=101")
    before = rows(p.backend)
    with pytest.raises(plex_chapters.ChapterError) as caught:
        plex_chapters.register_chapters(p.server, p.target, p.revisions)
    assert caught.value.code == "source_changed"
    assert rows(p.backend) == before
    assert p.register.call_count == 1


def test_committed_refresh_is_not_success_without_native_api_readback(publication):
    p = publication
    mutate(p.backend, "UPDATE media_parts SET updated_at=101")
    original = p.api.query.side_effect
    p.api.query.side_effect = lambda path: original(path) if path == "/identity" else ET.Element("MediaContainer")

    with pytest.raises(plex_chapters.ChapterError, match="verification") as caught:
        plex_chapters.register_chapters(p.server, p.target, p.revisions, verify_source=p.verify)

    assert caught.value.code == "registration"
    assert p.register.call_count == 2
    assert rows(p.backend)[0][6] == plex_chapters.chapter_url(10, 1, p.revisions[1])
