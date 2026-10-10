"""Chapter registration proves identity, source, complete images and transaction boundaries."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.publishers.base import Capability
from media_preview_generator.markers.publishers.plex_chapters import (
    ChapterError,
    LocalChapters,
    _remote,
    chapter_url,
    target_from_json,
    target_to_json,
    verify_chapters,
)
from media_preview_generator.markers.publishers.plex_remote import error_from_json, error_to_json
from media_preview_generator.processing.chapters import _failure

VERSION = "1.43.4.10903-e5521bd8c"
MACHINE = "synthetic-server"
HASH = "a" * 40
SOURCE = "/media/synthetic.mkv"


@pytest.fixture
def backend(tmp_path, monkeypatch):
    """Actual SQLite and image files; only the external Plex lock holder is simulated."""
    folder = tmp_path / "Plex Media Server"
    db = Path(plex_db.plex_db_path(str(folder)))
    db.parent.mkdir(parents=True)
    (folder / "Preferences.xml").write_text(f'<Preferences ProcessedMachineIdentifier="{MACHINE}"/>')
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE tags(id INTEGER PRIMARY KEY,tag_type INTEGER);
            CREATE TABLE taggings(id INTEGER PRIMARY KEY,metadata_item_id INTEGER,tag_id INTEGER,
                "index" INTEGER,time_offset INTEGER,end_time_offset INTEGER,thumb_url TEXT,extra_data TEXT);
            CREATE TABLE media_items(id INTEGER PRIMARY KEY,metadata_item_id INTEGER,deleted_at INTEGER,proxy_type INTEGER);
            CREATE TABLE media_parts(id INTEGER PRIMARY KEY,media_item_id INTEGER,file TEXT,hash TEXT,size INTEGER,
                updated_at INTEGER,deleted_at INTEGER,extra_data TEXT);
            INSERT INTO tags VALUES(9,9);
            INSERT INTO tags VALUES(12,12);
            INSERT INTO media_items VALUES(10,1,NULL,NULL);
            INSERT INTO taggings VALUES(1,1,9,1,0,10000,'','chapter data');
            INSERT INTO taggings VALUES(2,1,9,2,10000,20000,'','chapter data');
            INSERT INTO taggings VALUES(3,1,12,0,500,5000,'','marker data');
        """)
        conn.execute(
            "INSERT INTO media_parts(id,media_item_id,file,hash,size,updated_at,deleted_at) VALUES(20,10,?,?,65536,100,NULL)",
            (SOURCE, HASH),
        )
        conn.commit()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("36 25 0:32 / / rw,relatime - ext4 /dev/sda1 rw\n")
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda *_a, **_k: True)
    return LocalChapters(str(folder), mountinfo_path=str(mountinfo))


def read(backend):
    return backend.read(SOURCE, MACHINE, VERSION, deadline=time.monotonic() + 3)


def images(backend):
    folder = backend.folder / "Media" / "localhost" / HASH[0] / f"{HASH[1:]}.bundle" / "Contents" / "Chapters"
    folder.mkdir(parents=True, exist_ok=True)
    revisions = {}
    for index, color in [(1, "red"), (2, "blue")]:
        path = folder / f"chapter{index}.jpg"
        Image.new("RGB", (32, 18), color).save(path, "JPEG")
        revisions[index] = hashlib.sha256(path.read_bytes()).hexdigest()
    return folder, revisions


def rows(backend):
    with contextlib.closing(sqlite3.connect(plex_db.plex_db_path(str(backend.folder)), isolation_level=None)) as conn:
        return conn.execute("SELECT * FROM taggings ORDER BY id").fetchall()


def mutate(backend, sql, args=()):
    with contextlib.closing(sqlite3.connect(plex_db.plex_db_path(str(backend.folder)), isolation_level=None)) as conn:
        conn.execute(sql, args)


def test_register_changes_only_existing_chapter_thumb_urls_and_is_idempotent(backend):
    target = read(backend)
    before = rows(backend)
    _, revisions = images(backend)
    backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)
    after = rows(backend)
    for old, new in zip(before, after, strict=True):
        assert old[:6] == new[:6] and old[7:] == new[7:]
    assert after[0][6] == chapter_url(10, 1, revisions[1])
    assert after[1][6] == chapter_url(10, 2, revisions[2])
    assert before[2] == after[2]
    backend.register(read(backend), revisions, VERSION, deadline=time.monotonic() + 3)
    assert rows(backend) == after


@pytest.mark.parametrize(
    "change, reason",
    [
        (
            "UPDATE taggings SET end_time_offset=9999 WHERE id=2",
            "Invalid chapter timing for chapter 2: start 10000ms, end 9999ms",
        ),
        (
            "UPDATE taggings SET time_offset=11000, end_time_offset=12000 WHERE id=1",
            "chapter 1 starts at 11000ms but chapter 2 starts at 10000ms",
        ),
    ],
)
def test_invalid_chapter_metadata_explains_the_exact_timing_without_writes(backend, change, reason):
    mutate(backend, change)
    before = rows(backend)

    with pytest.raises(ChapterError) as caught:
        read(backend)

    assert caught.value.code == "unsupported"
    assert reason in str(caught.value)
    assert rows(backend) == before


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE taggings SET end_time_offset=0 WHERE id=1",
        "UPDATE taggings SET end_time_offset=10000 WHERE id=2",
        "UPDATE taggings SET end_time_offset=15000 WHERE id=1",
        "UPDATE taggings SET time_offset=0 WHERE id=2",
    ],
)
def test_rounded_or_overlapping_chapters_register_images_without_changing_timing(backend, change):
    mutate(backend, change)
    target = read(backend)
    before = rows(backend)
    _, revisions = images(backend)

    backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)

    after = rows(backend)
    for old, new in zip(before, after, strict=True):
        assert old[:6] == new[:6] and old[7:] == new[7:]
    assert after[0][6] == chapter_url(10, 1, revisions[1])
    assert after[1][6] == chapter_url(10, 2, revisions[2])


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE media_parts SET hash='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'",
        "UPDATE media_parts SET size=65537",
        "UPDATE media_parts SET updated_at=101",
        "UPDATE taggings SET time_offset=1 WHERE id=1",
        "UPDATE taggings SET thumb_url='/other' WHERE id=1",
        "DELETE FROM taggings WHERE id=2",
    ],
)
def test_changed_snapshot_is_refused_without_any_writes(backend, change):
    target = read(backend)
    _, revisions = images(backend)
    mutate(backend, change)
    before = rows(backend)
    with pytest.raises(ChapterError, match="changed") as caught:
        backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)
    assert caught.value.code == "source_changed"
    assert rows(backend) == before


@pytest.mark.parametrize("bad_image", ["missing", "wrong_hash", "truncated", "tail_truncated", "png", "symlink"])
def test_every_image_must_be_valid_before_first_write(backend, tmp_path, bad_image):
    target = read(backend)
    folder, revisions = images(backend)
    path = folder / "chapter2.jpg"
    if bad_image == "missing":
        path.unlink()
    elif bad_image == "wrong_hash":
        revisions[2] = "0" * 64
    elif bad_image == "truncated":
        path.write_bytes(b"\xff\xd8broken")
        revisions[2] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif bad_image == "tail_truncated":
        path.write_bytes(path.read_bytes()[:-20])
        revisions[2] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif bad_image == "png":
        Image.new("RGB", (32, 18)).save(path, "PNG")
        revisions[2] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        outside = tmp_path / "outside.jpg"
        path.rename(outside)
        path.symlink_to(outside)
    before = rows(backend)
    with pytest.raises(ChapterError):
        backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)
    assert rows(backend) == before


def test_plex_media_symlink_can_target_a_separate_mounted_disk(backend, tmp_path):
    target = read(backend)
    _, revisions = images(backend)
    relocated = tmp_path / "bulk-plex-media"
    (backend.folder / "Media").rename(relocated)
    (backend.folder / "Media").symlink_to(relocated, target_is_directory=True)

    assert backend.capability(MACHINE, VERSION, deadline=time.monotonic() + 3).ready
    backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)
    assert rows(backend)[0][6] == chapter_url(10, 1, revisions[1])


def test_nested_chapter_directory_cannot_escape_relocated_media_root(backend, tmp_path):
    target = read(backend)
    folder, revisions = images(backend)
    relocated = tmp_path / "bulk-plex-media"
    (backend.folder / "Media").rename(relocated)
    (backend.folder / "Media").symlink_to(relocated, target_is_directory=True)
    outside = tmp_path / "outside-chapters"
    folder.rename(outside)
    folder.symlink_to(outside, target_is_directory=True)
    before = rows(backend)

    with pytest.raises(ChapterError, match="missing, invalid, or changed"):
        backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)
    assert rows(backend) == before


def test_readiness_explains_missing_media_symlink_mount(backend, tmp_path):
    (backend.folder / "Media").symlink_to(tmp_path / "unmounted-media", target_is_directory=True)
    report = backend.capability(MACHINE, VERSION, deadline=time.monotonic() + 3)
    assert report.state == Capability.MISCONFIGURED
    assert "Mount its target" in report.message


def pv_chapters(*starts, as_dict=False):
    """A part's extra_data as Plex writes it: pv:chapters is itself a JSON string."""
    chapter = [{"name": f"c{n}", "start": start, "end": start + 1.0} for n, start in enumerate(starts)]
    if not chapter:
        inner = {"Chapters": {}}
    else:
        inner = {"Chapters": {"Chapter": chapter[0] if as_dict else chapter}}
    return json.dumps({"pv:chapters": json.dumps(inner)})


OWN_CHAPTERS = pv_chapters(0.0, 10.004)


def add_second_version(backend, *, first=OWN_CHAPTERS, second=None):
    """Version 10 (the fixture's) gets ``first`` as its own chapters; version 11 gets ``second``."""
    mutate(backend, "UPDATE media_parts SET extra_data=? WHERE id=20", (first,))
    mutate(backend, "INSERT INTO media_items VALUES(11,1,NULL,NULL)")
    mutate(
        backend,
        "INSERT INTO media_parts(id,media_item_id,file,hash,size,updated_at,deleted_at,extra_data) "
        "VALUES(21,11,'/media/version2.mkv',?,65536,100,NULL,?)",
        ("b" * 40, second),
    )


def test_version_split_into_multiple_files_is_unsupported_source(backend):
    mutate(
        backend,
        "INSERT INTO media_parts(id,media_item_id,file,hash,size,updated_at,deleted_at) "
        "VALUES(21,10,'/media/part2.mkv','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',65536,100,NULL)",
    )
    with pytest.raises(ChapterError, match="split into multiple files") as caught:
        read(backend)
    assert caught.value.code == "unsupported_source"


def test_multi_version_without_chapters_is_no_chapters(backend):
    add_second_version(backend)
    mutate(backend, "DELETE FROM taggings")
    with pytest.raises(ChapterError) as caught:
        read(backend)
    assert caught.value.code == "no_chapters"


def test_multi_version_thumbs_from_other_live_version_is_other_version(backend):
    add_second_version(backend)
    mutate(backend, "UPDATE taggings SET thumb_url='/library/media/11/chapterImages/1?mpgChapter=x' WHERE id=1")
    with pytest.raises(ChapterError) as caught:
        read(backend)
    assert caught.value.code == "other_version"


def test_multi_version_with_empty_thumbs_and_matching_own_chapters_claims(backend):
    add_second_version(backend)
    target = read(backend)
    assert target.media_id == 10
    assert len(target.chapters) == 2


def test_multi_version_single_chapter_dict_form_matches(backend):
    mutate(backend, "DELETE FROM taggings WHERE id=2")
    add_second_version(backend, first=pv_chapters(0.0, as_dict=True))
    assert read(backend).media_id == 10


@pytest.mark.parametrize(
    "own",
    [
        pv_chapters(),
        pv_chapters(0.0, 20.0),
        pv_chapters(0.0, 10.0, 20.0),
        None,
        "not json",
        json.dumps({"pv:chapters": "not json"}),
        json.dumps({"pv:chapters": json.dumps({"Chapters": {"Chapter": [{"name": "x"}]}})}),
    ],
)
def test_multi_version_not_matching_shared_rows_is_other_version(backend, own):
    add_second_version(backend, first=own)
    with pytest.raises(ChapterError) as caught:
        read(backend)
    assert caught.value.code == "other_version"


def test_multi_version_thumbs_at_this_version_proceed_even_if_own_chapters_differ(backend):
    add_second_version(backend, first=pv_chapters(0.0, 99.0))
    mutate(backend, "UPDATE taggings SET thumb_url='/library/media/10/chapterImages/1?mpgChapter=x' WHERE id=1")
    assert read(backend).media_id == 10


def test_multi_version_thumbs_from_deleted_version_claims_when_chapters_match(backend):
    add_second_version(backend)
    mutate(backend, "INSERT INTO media_items VALUES(12,1,100,NULL)")
    mutate(backend, "UPDATE taggings SET thumb_url='/library/media/12/chapterImages/1?mpgChapter=x' WHERE id=1")
    assert read(backend).media_id == 10


def test_register_refuses_when_another_version_claimed_the_rows_meanwhile(backend):
    add_second_version(backend)
    target = read(backend)
    _folder, revisions = images(backend)
    mutate(backend, "UPDATE taggings SET thumb_url='/library/media/11/chapterImages/1?mpgChapter=x' WHERE id=1")
    mutate(backend, "UPDATE taggings SET thumb_url='/library/media/11/chapterImages/2?mpgChapter=x' WHERE id=2")
    before = rows(backend)
    with pytest.raises(ChapterError) as caught:
        backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)
    assert caught.value.code == "other_version"
    assert rows(backend) == before


@pytest.mark.parametrize("code", ["unsupported_source", "other_version", "no_chapters"])
def test_new_codes_survive_the_agent_error_codec(code):
    rebuilt = error_from_json(error_to_json(ChapterError("refused", code=code)))
    assert isinstance(rebuilt, ChapterError) and rebuilt.code == code
    assert _failure(rebuilt).status == {"no_chapters": "none"}.get(code, "skipped")


def test_chapterless_is_distinct_from_unindexed_and_requires_no_marker_tag(backend):
    mutate(backend, "DELETE FROM taggings")
    mutate(backend, "DELETE FROM tags")
    assert read(backend).chapters == ()
    assert backend.capability(MACHINE, VERSION, deadline=time.monotonic() + 3).ready
    with pytest.raises(ChapterError) as caught:
        backend.read("/media/missing.mkv", MACHINE, VERSION, deadline=time.monotonic() + 3)
    assert caught.value.code == "pending_index"


@pytest.mark.parametrize(
    "machine,version",
    [
        ("", VERSION),
        ("other", VERSION),
        (MACHINE, "1.44.0.12"),
        (MACHINE, "1.43.3.123"),
        (MACHINE, "1.43.4.invalid"),
        (MACHINE, ""),
    ],
)
def test_unknown_identity_or_version_refuses_capability(backend, machine, version):
    assert not backend.capability(machine, version, deadline=time.monotonic() + 3).ready


@pytest.mark.parametrize(
    "xml",
    [
        f'<Preferences ProcessedMachineIdentifier="{MACHINE}">',
        f'<!DOCTYPE Preferences><Preferences ProcessedMachineIdentifier="{MACHINE}"/>',
        f'<!DOCTYPE Preferences [<!ENTITY machine "{MACHINE}">]><Preferences ProcessedMachineIdentifier="&machine;"/>',
        '<!DOCTYPE Preferences SYSTEM "file:///not-a-preferences-file">'
        f'<Preferences ProcessedMachineIdentifier="{MACHINE}"/>',
    ],
)
def test_malformed_xml_and_entity_declarations_cannot_establish_identity(backend, xml):
    (backend.folder / "Preferences.xml").write_text(xml)
    report = backend.capability(MACHINE, VERSION, deadline=time.monotonic() + 3)
    assert report.state == Capability.MISCONFIGURED
    assert "Cannot prove" in report.message


def test_delete_journal_and_unknown_triggers_refuse_writes(backend):
    mutate(backend, "PRAGMA journal_mode=DELETE")
    assert backend.capability(MACHINE, VERSION, deadline=time.monotonic() + 3).state == Capability.NEEDS_LOCAL_DB
    mutate(backend, "PRAGMA journal_mode=WAL")
    mutate(backend, "CREATE TRIGGER unexpected AFTER UPDATE ON taggings BEGIN SELECT 1; END")
    assert backend.capability(MACHINE, VERSION, deadline=time.monotonic() + 3).state == Capability.UNSUPPORTED_SCHEMA


@pytest.mark.parametrize(
    "key,value", [("rating_key", True), ("bundle_hash", "../escape"), ("source_size", -1), ("machine_identifier", "")]
)
def test_snapshot_wire_rejects_unsafe_values(backend, key, value):
    raw = target_to_json(read(backend))
    raw[key] = value
    with pytest.raises(ValueError):
        target_from_json(raw)


@pytest.mark.parametrize("size", [0, 1, 123, 65535, 65536])
def test_bundle_hash_matches_plex_small_file_boundary(backend, size):
    raw = target_to_json(read(backend))
    raw.update(source_size=size, bundle_hash=(str(size) if size < 65536 else "") + "a" * 40)
    target = target_from_json(raw)
    assert target.source_size == size
    assert target.bundle_hash == raw["bundle_hash"]


@pytest.mark.parametrize(
    "size,bundle_hash",
    [
        (123, "124" + "a" * 40),
        (123, "0123" + "a" * 40),
        (123, "a" * 40),
        (65536, "65536" + "a" * 40),
        (123, "123../" + "a" * 40),
    ],
)
def test_wrong_size_prefix_or_path_in_bundle_hash_is_refused(backend, size, bundle_hash):
    raw = target_to_json(read(backend))
    raw.update(source_size=size, bundle_hash=bundle_hash)
    with pytest.raises(ValueError):
        target_from_json(raw)


def test_error_retry_classification_roundtrips():
    error = error_from_json(error_to_json(ChapterError("changed", code="source_changed")))
    assert isinstance(error, ChapterError) and error.code == "source_changed"


@pytest.mark.parametrize("capabilities,machine", [([], MACHINE), (["chapters_v1"], "other"), (["chapters_v1"], "")])
def test_remote_refuses_old_or_wrong_helper_before_sending_operation(capabilities, machine):
    class Client:
        def __init__(self):
            self.capabilities = capabilities

        def ping(self, *, timeout):
            assert timeout == 5
            return {"machine_identifier": machine}

        def post(self, *args, **kwargs):
            pytest.fail("An incompatible helper must never receive the operation")

    with pytest.raises(ChapterError):
        _remote(Client(), "register", MACHINE, VERSION, target={})


def test_second_update_failure_rolls_back_the_first(backend, monkeypatch):
    target = read(backend)
    _, revisions = images(backend)
    before = rows(backend)
    original = backend.database._connect

    class FailSecondUpdate:
        def __init__(self, connection):
            self.connection = connection
            self.updates = 0

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE taggings"):
                self.updates += 1
                if self.updates == 2:
                    raise sqlite3.OperationalError("injected write failure")
            return self.connection.execute(sql, *args)

    monkeypatch.setattr(backend.database, "_connect", lambda **kwargs: FailSecondUpdate(original(**kwargs)))
    from media_preview_generator.markers.publishers.base import PublishError

    with pytest.raises(PublishError):
        backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)
    assert rows(backend) == before


def test_expired_deadline_during_updates_rolls_back(backend, monkeypatch):
    target = read(backend)
    _, revisions = images(backend)
    before = rows(backend)
    calls = []

    def expires(deadline):
        calls.append(deadline)
        if len(calls) == 4:  # two JPEG checks, then the first and second updates
            raise ChapterError("Timed out", code="registration")

    monkeypatch.setattr(backend, "_within_deadline", expires)
    with pytest.raises(ChapterError, match="Timed out"):
        backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)
    assert len(calls) == 4
    assert rows(backend) == before


def test_expired_image_validation_does_not_read_files(backend):
    with pytest.raises(ChapterError, match="Timed out"):
        backend._images(read(backend), {1: "a" * 64, 2: "b" * 64}, deadline=time.monotonic() - 1)


def test_images_are_decoded_before_plexs_write_lock_is_taken(backend, monkeypatch):
    target = read(backend)
    _, revisions = images(backend)
    order = []
    real_images, real_begin = backend._images, backend.database._begin_write
    monkeypatch.setattr(backend, "_images", lambda *a, **k: order.append("images") or real_images(*a, **k))
    monkeypatch.setattr(backend.database, "_begin_write", lambda *a, **k: order.append("begin") or real_begin(*a, **k))

    backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)

    assert order == ["images", "begin"]


def test_an_image_replaced_after_validation_is_refused_inside_the_transaction(backend):
    target = read(backend)
    folder, revisions = images(backend)
    seen = backend._images(target, revisions, deadline=time.monotonic() + 3)
    before = rows(backend)

    (folder / "chapter1.jpg").write_bytes((folder / "chapter1.jpg").read_bytes() + b"\0")

    with pytest.raises(ChapterError, match="Chapter 1 image"):
        backend._images_unchanged(target, seen)
    assert rows(backend) == before


def test_agent_typed_requests_and_auth(backend, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plex-marker-agent"))
    import plex_marker_agent

    monkeypatch.setattr(plex_marker_agent, "LocalChapters", lambda *_a, **_kw: backend)
    client = plex_marker_agent.create_app(config_dir=str(backend.folder), token="synthetic-key").test_client()
    headers = {"Authorization": "Bearer synthetic-key", "X-Marker-Agent-Protocol": "1"}
    body = {"machine_identifier": MACHINE, "pms_version": VERSION, "deadline_s": 3, "source_path": SOURCE}
    assert client.post("/v1/chapters/read", json=body).status_code == 401
    response = client.post("/v1/chapters/read", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json["agent"]["capabilities"] == ["chapters_v1"]
    snapshot = response.json["result"]["target"]
    _, revisions = images(backend)
    body.update(target=snapshot, revisions=[{"index": i, "sha256": sha} for i, sha in revisions.items()])
    assert client.post("/v1/chapters/register", json=body, headers=headers).status_code == 200
    assert client.post("/v1/chapters/register", json=body, headers=headers).status_code == 409
    body["revisions"][1]["index"] = body["revisions"][0]["index"]
    assert client.post("/v1/chapters/register", json=body, headers=headers).status_code == 400


def test_register_subset_updates_only_revised_chapters(backend):
    target = read(backend)
    _, revisions = images(backend)
    before = rows(backend)
    backend.register(target, {1: revisions[1]}, VERSION, deadline=time.monotonic() + 3)
    after = rows(backend)
    assert after[0][6] == chapter_url(10, 1, revisions[1])
    assert after[1:] == before[1:]


@pytest.mark.parametrize("revisions", [{}, {3: "a" * 64}, {1: "a" * 64, 7: "b" * 64}])
def test_register_rejects_empty_or_unknown_chapter_indexes(backend, revisions):
    target = read(backend)
    images(backend)
    before = rows(backend)
    with pytest.raises(ChapterError) as caught:
        backend.register(target, revisions, VERSION, deadline=time.monotonic() + 3)
    assert caught.value.code == "registration"
    assert rows(backend) == before


def _plex_answering(observed):
    from xml.etree import ElementTree

    plex = MagicMock()
    chapters = "".join(f'<Chapter index="{i}" thumb="{thumb}"/>' for i, thumb in observed.items())
    plex.query.side_effect = lambda path: (
        {"machineIdentifier": MACHINE} if path == "/identity" else ElementTree.fromstring(f"<R>{chapters}</R>")
    )
    return SimpleNamespace(_connect=lambda: plex)


def test_verify_checks_only_the_revised_chapters(backend):
    target = read(backend)
    _, revisions = images(backend)
    only_first = {1: revisions[1]}
    server = _plex_answering({1: chapter_url(10, 1, revisions[1]), 2: ""})
    verify_chapters(server, target, only_first)
    with pytest.raises(ChapterError):
        verify_chapters(_plex_answering({1: "", 2: chapter_url(10, 2, revisions[2])}), target, only_first)


def test_register_clears_only_our_own_stale_reference_for_chapters_without_an_image(backend):
    target_before = read(backend)
    _, revisions = images(backend)
    mutate(backend, "UPDATE taggings SET thumb_url='/library/media/10/chapterImages/2?mpgChapter=old' WHERE id=2")
    backend.register(read(backend), {1: revisions[1]}, VERSION, deadline=time.monotonic() + 3)
    assert rows(backend)[1][6] == ""
    assert target_before.chapters[1].thumb_url == ""

    for native in (
        "/library/metadata/1/thumb/5",
        "/library/media/10/chapterImages/2",
        "/library/media/99/chapterImages/2?mpgChapter=x",
    ):
        mutate(backend, "UPDATE taggings SET thumb_url=? WHERE id=2", (native,))
        backend.register(read(backend), {1: revisions[1]}, VERSION, deadline=time.monotonic() + 3)
        assert rows(backend)[1][6] == native


def test_agent_validates_a_subset_of_chapter_revisions(backend, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plex-marker-agent"))
    import plex_marker_agent

    monkeypatch.setattr(plex_marker_agent, "LocalChapters", lambda *_a, **_kw: backend)
    client = plex_marker_agent.create_app(config_dir=str(backend.folder), token="synthetic-key").test_client()
    headers = {"Authorization": "Bearer synthetic-key", "X-Marker-Agent-Protocol": "1"}
    body = {"machine_identifier": MACHINE, "pms_version": VERSION, "deadline_s": 3, "source_path": SOURCE}
    snapshot = client.post("/v1/chapters/read", json=body, headers=headers).json["result"]["target"]
    _, revisions = images(backend)
    body["target"] = snapshot

    def register(revision_list):
        body["revisions"] = revision_list
        return client.post("/v1/chapters/register", json=body, headers=headers).status_code

    first = {"index": 1, "sha256": revisions[1]}
    assert register([]) == 400
    assert register([first, {"index": 2, "sha256": revisions[2]}, {"index": 3, "sha256": revisions[2]}]) == 400
    assert register([{"index": True, "sha256": revisions[1]}]) == 400
    assert register([{"index": 7, "sha256": revisions[1]}]) == 409
    assert register([first]) == 200
