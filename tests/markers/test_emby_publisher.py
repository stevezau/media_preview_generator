"""EmbyMarkerPublisher: capability matrix, credits note, write/confirm/cleanup, Keep Emby's, versions, read-back."""

from __future__ import annotations

from unittest.mock import MagicMock, create_autospec

import pytest
import requests

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers.base import Capability, ItemNotFoundError, PublishError, Shown
from media_preview_generator.markers.publishers.emby import CREDITS_BEFORE_END_NOTE, EmbyMarkerPublisher
from media_preview_generator.markers.settings import load_server
from media_preview_generator.servers import EmbyServer
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import server_config

T = MarkerType
DUR = 1_321_472
INTRO = Marker(T.INTRO, 126_771, 157_068, ("chapters",))
CREDITS_TO_END = Marker(T.CREDITS, 1_295_324, DUR, ("chapters",))
CREDITS_EARLY = Marker(T.CREDITS, 1_250_000, 1_290_000, ("theintrodb", "server_markers"))
INTRO_GROUP = ("IntroStart", "IntroEnd")
OLD_INTRO = Marker(T.INTRO, 5_000, 35_000, ("chapters",))  # an intro this app published before a re-decision
# Rows Emby's own intro detection (or another plugin) leaves: the plugin never wrote them.
EMBY_INTRO = [("IntroStart", 600_000_000, "Intro"), ("IntroEnd", 900_000_000, "Intro End")]


def _ok(**extra):
    return MagicMock(status_code=200, **{"json.return_value": {"Found": True, "Stale": False, **extra}})


def _rows_for(stored):
    """The chapter rows the plugin writes for stored ticks (``MarkerChapters.RowsFor``)."""
    if stored is None:
        return []
    intro_start, intro_end, credits_start = stored
    rows = []
    if intro_start is not None:
        rows += [("IntroStart", intro_start, "Intro"), ("IntroEnd", intro_end, "Intro End")]
    if credits_start is not None:
        rows.append(("CreditsStart", credits_start, "Credits"))
    return rows


def _ready_server():
    server = create_autospec(EmbyServer, instance=True)
    server.get_bridge_info.return_value = {"installed": True, "version": "1.0.0.0", "features": ["markers"]}
    server.get_bridge_markers_access.return_value = "ok"
    server.bridge_catalog_listed.return_value = False
    server.get_media_source_durations.return_value = [DUR]
    return server


class FakeEmby:
    """An autospec'd EmbyServer over a model of the Bridge plugin: one stored marker set per item and the item's chapter
    rows, merged the way ``MarkerChapters.Apply`` does (only the plugin's own rows are removed; a type that has someone
    else's rows keeps them unless ``ReplaceOwn``). ``versions`` is what Emby lists as the item's MediaSources: ``(path,
    version item id)``."""

    def __init__(self, media_path, *, server=None, item_id="42"):
        self.path = media_path
        self.versions = [(media_path, item_id)]
        self.stored = None  # (intro_start_ticks, intro_end_ticks, credits_start_ticks)
        self.size = None
        self.rows = [("Chapter", 0, "Chapter 1")]  # (MarkerType, ticks, name)
        self.stale = False  # the file on disk has another size than the one posted
        self.show_nothing = False  # Emby's item answer doesn't list the plugin's rows
        self.replace_own = []  # ReplaceOwn of every POST
        self.server = server
        if server is None:  # one item: the server answers every call from this model
            self.server = s = _ready_server()
            s.get_chapters_and_versions.side_effect = lambda item_id: (self._chapters(item_id), list(self.versions))
            s.put_emby_markers.side_effect = self._put
            s.delete_emby_markers.side_effect = self._delete
            s.get_chapter_markers.side_effect = self._chapters
            s.get_emby_marker_state.side_effect = self._state

    def _apply(self, wanted, replace_own):
        ours = _rows_for(self.stored)
        rows = [r for r in self.rows if r not in ours]
        written = 0
        for group in (INTRO_GROUP, ("CreditsStart",)):
            add = [r for r in _rows_for(wanted) if r[0] in group]
            if not add:
                continue
            if any(r[0] in group for r in rows):
                if not replace_own:
                    continue
                rows = [r for r in rows if r[0] not in group]
            rows += add
            written += len(add)
        self.rows = sorted(rows, key=lambda r: r[1])
        return written

    def _put(self, item_id, *, intro_start_ticks, intro_end_ticks, credits_start_ticks, file_size, replace_own):
        self.replace_own.append(replace_own)
        wanted = (intro_start_ticks, intro_end_ticks, credits_start_ticks)
        written = self._apply(None if self.stale else wanted, replace_own)
        self.stored, self.size = wanted, file_size
        return _ok(Stale=self.stale, Stored=written)

    def _delete(self, item_id):
        self._apply(None, False)
        self.stored, self.size = None, None
        return _ok(Stored=0)

    def _chapters(self, item_id):
        ours = _rows_for(self.stored)
        rows = [r for r in self.rows if not (self.show_nothing and r in ours)]
        return [{"marker_type": t, "start_ms": ticks // 10_000, "name": name} for t, ticks, name in rows]

    def _state(self, item_id):
        m = self.stored or (None, None, None)
        return {"intro_start_ticks": m[0], "intro_end_ticks": m[1], "credits_start_ticks": m[2],
                "file_size": self.size, "stale": self.stale}  # fmt: skip

    def markers_shown(self):
        return [(t, ticks // 10_000) for t, ticks, _name in self.rows if t != "Chapter"]


class FakeEmbyItems:
    """Emby versions of one video: each its own item (own plugin store and chapters) behind one autospec'd EmbyServer.

    Every item lists all versions as its MediaSources in the order given (Emby's: the widest video first), so an item's
    own file isn't always first.
    """

    def __init__(self, paths_by_item: dict[str, str]):
        self.server = s = _ready_server()
        self.items = {item_id: FakeEmby(path, server=s, item_id=item_id) for item_id, path in paths_by_item.items()}
        for item in self.items.values():
            item.versions = [(path, item_id) for item_id, path in paths_by_item.items()]
        s.get_chapters_and_versions.side_effect = lambda item_id: (
            self.items[item_id]._chapters(item_id),
            list(self.items[item_id].versions),
        )
        s.put_emby_markers.side_effect = lambda item_id, **kw: self.items[item_id]._put(item_id, **kw)
        s.delete_emby_markers.side_effect = lambda item_id: self.items[item_id]._delete(item_id)
        s.get_chapter_markers.side_effect = lambda item_id: self.items[item_id]._chapters(item_id)
        s.get_emby_marker_state.side_effect = lambda item_id: self.items[item_id]._state(item_id)


def _publisher(fake, setting="restore", **kwargs):
    cfg = server_config("emby-1", ServerType.EMBY, root="/")
    cfg.markers["emby"] = {"on_emby_redetect": setting}
    return EmbyMarkerPublisher(fake.server, cfg, load_server(cfg.markers, "emby"), **kwargs)


@pytest.fixture
def emby(tmp_path):
    media = tmp_path / "S01E01.mkv"
    media.write_bytes(b"x" * 321)
    fake = FakeEmby(str(media))
    fake.publisher = _publisher(fake)
    return fake


def _write(emby, markers, previous=(), publisher=None, **kw):
    return (publisher or emby.publisher).write(
        "42",
        list(markers),
        previous=None if previous is None else list(previous),
        duration_ms=kw.get("duration_ms", DUR),
        canonical_path=emby.path,
        kept_types=frozenset(kw.get("kept_types", ())),
    )


class TestCapability:
    @pytest.mark.parametrize(
        ("setup", "state"),
        [
            (lambda s: setattr(s.get_bridge_info, "return_value", None), Capability.UNREACHABLE),
            (lambda s: setattr(s.get_bridge_info, "return_value", {"installed": False, "version": None, "features": []}), Capability.NEEDS_PLUGIN),
            (lambda s: setattr(s.get_bridge_info, "return_value", {"installed": True, "version": "0.9", "features": []}), Capability.PLUGIN_OUTDATED),
            (lambda s: setattr(s.get_bridge_markers_access, "return_value", "unauthorized"), Capability.MISCONFIGURED),
            (lambda s: setattr(s.get_bridge_markers_access, "return_value", "forbidden"), Capability.MISCONFIGURED),
            (lambda s: setattr(s.get_bridge_markers_access, "return_value", None), Capability.UNREACHABLE),
            (lambda s: None, Capability.READY),
        ],
    )  # fmt: skip
    def test_matrix(self, emby, setup, state):
        setup(emby.server)
        report = emby.publisher.capability()
        assert report.state is state
        if state is Capability.READY:
            assert report.details == {"plugin_version": "1.0.0.0", "can_show": ["intro", "credits"]}
        if state is Capability.NEEDS_PLUGIN:
            assert report.message == "Install the Media Preview Bridge for Emby plugin"
            assert report.details == {"catalog_listed": False}
        if state is Capability.PLUGIN_OUTDATED:
            assert report.details == {"plugin_version": "0.9"}

    def test_switched_off_is_disabled_without_asking_emby(self):
        server = create_autospec(EmbyServer, instance=True)
        cfg = server_config("emby-1", ServerType.EMBY, markers={"enabled": False, "library_ids": None})
        report = EmbyMarkerPublisher(server, cfg, load_server(cfg.markers, "emby")).capability()
        assert report.state is Capability.DISABLED and server.get_bridge_info.call_count == 0


class TestCreditsNote:
    @pytest.mark.parametrize(
        ("markers", "duration", "note"),
        [
            ([INTRO, CREDITS_TO_END], DUR, ""),
            ([Marker(T.CREDITS, 1_295_000, DUR - 2_000, ())], DUR, ""),
            ([Marker(T.CREDITS, 1_295_000, DUR - 2_001, ())], DUR, CREDITS_BEFORE_END_NOTE),
            ([INTRO, CREDITS_EARLY], DUR, CREDITS_BEFORE_END_NOTE),
            ([CREDITS_EARLY], None, ""),  # unknown duration: can't tell
            ([INTRO], DUR, ""),
        ],
    )  # fmt: skip
    def test_note_names_credits_that_end_before_the_file(self, emby, markers, duration, note):
        assert emby.publisher.projection_note(markers, duration_ms=duration) == note

    def test_every_decided_credits_marker_is_sent(self, emby):
        # Owner decision 2026-09-14: Emby always gets the credits start, even when a scene follows the credits.
        assert emby.publisher.project([CREDITS_EARLY, INTRO, Marker(T.RECAP, 0, 30_000, ())]) == [INTRO, CREDITS_EARLY]
        assert CREDITS_BEFORE_END_NOTE == "Emby skips to the end of the file"


class TestWrite:
    def test_posts_ticks_with_the_file_size_and_returns_ours(self, emby):
        assert _write(emby, [INTRO, CREDITS_TO_END]) == [INTRO, CREDITS_TO_END]
        call = emby.server.put_emby_markers.call_args
        assert call.args == ("42",)
        assert call.kwargs == {"intro_start_ticks": 1_267_710_000, "intro_end_ticks": 1_570_680_000,
                               "credits_start_ticks": 12_953_240_000, "file_size": 321, "replace_own": True}  # fmt: skip
        assert emby.markers_shown() == [("IntroStart", 126_771), ("IntroEnd", 157_068), ("CreditsStart", 1_295_324)]
        assert emby.publisher.last_write_changed is True
        assert emby.publisher.last_kept_types == frozenset() and emby.publisher.last_item_files is None

    def test_a_set_not_recorded_as_ours_is_posted_without_reading_the_plugin_first(self, emby):
        # A bulk first publish costs one item read, one POST and one chapter read per item: the plugin's store is read
        # only when previous says this very set is already ours.
        _write(emby, [INTRO])
        emby.server.reset_mock()
        assert _write(emby, [INTRO], previous=[]) == [INTRO]
        emby.server.get_emby_marker_state.assert_not_called()
        assert emby.server.get_chapters_and_versions.call_count == 1
        assert emby.server.put_emby_markers.call_count == emby.server.get_chapter_markers.call_count == 1

    def test_credits_that_end_before_the_file_are_sent_too(self, emby):
        assert _write(emby, [INTRO, CREDITS_EARLY]) == [INTRO, CREDITS_EARLY]
        assert emby.server.put_emby_markers.call_args.kwargs["credits_start_ticks"] == 12_500_000_000

    def test_unchanged_set_for_this_file_sends_nothing(self, emby):
        _write(emby, [INTRO, CREDITS_TO_END])
        emby.server.reset_mock()
        assert _write(emby, [INTRO, CREDITS_TO_END], previous=[INTRO, CREDITS_TO_END]) == [INTRO, CREDITS_TO_END]
        emby.server.put_emby_markers.assert_not_called()
        assert emby.publisher.last_write_changed is False
        # One item read (chapters and versions together) and one plugin store read.
        assert emby.server.get_chapters_and_versions.call_count == emby.server.get_emby_marker_state.call_count == 1
        emby.server.get_chapter_markers.assert_not_called()

    @pytest.mark.parametrize(
        "change",
        [
            lambda e: setattr(e, "size", 1),  # the plugin holds another file's size
            lambda e: setattr(e, "stored", (1, 2, None)),  # the plugin holds other markers
            lambda e: setattr(e, "rows", [r for r in e.rows if r[0] == "Chapter"]),  # a refresh dropped the rows
        ],
        ids=["other-file-size", "other-markers-stored", "rows-gone"],
    )
    def test_unchanged_set_is_posted_again_when_emby_or_the_plugin_differ(self, emby, change):
        _write(emby, [INTRO])
        change(emby)
        emby.server.put_emby_markers.reset_mock()
        assert _write(emby, [INTRO], previous=[INTRO]) == [INTRO]
        assert emby.server.put_emby_markers.call_args.kwargs["file_size"] == 321
        assert emby.publisher.last_write_changed is True

    @pytest.mark.parametrize(
        ("previous", "kept", "deleted"),
        [(None, (), True), ([INTRO], (), True), ([], (), False), ([], (T.INTRO,), True)],
        ids=["unknown", "ours-there", "nothing-ours", "only-kept-types"],
    )
    def test_nothing_wanted_deletes_what_may_be_ours(self, emby, previous, kept, deleted):
        assert _write(emby, [], previous=previous, kept_types=kept) == []
        assert emby.server.delete_emby_markers.called is deleted
        assert emby.publisher.last_write_changed is deleted
        assert emby.publisher.last_kept_types == frozenset()

    def test_stale_answer_is_removed_and_fails(self, emby):
        emby.stale = True
        with pytest.raises(PublishError, match="different file size"):
            _write(emby, [INTRO])
        emby.server.delete_emby_markers.assert_called_once_with("42")
        assert emby.stored is None

    def test_chapters_not_showing_ours_are_removed_and_fail(self, emby):
        emby.show_nothing = True
        with pytest.raises(PublishError, match="chapters don't show"):
            _write(emby, [INTRO])
        emby.server.delete_emby_markers.assert_called_once_with("42")

    def test_unreadable_chapters_after_the_post_are_removed_and_fail(self, emby):
        emby.server.get_chapter_markers.side_effect = None
        emby.server.get_chapter_markers.return_value = None
        with pytest.raises(PublishError, match="couldn't confirm") as caught:
            _write(emby, [INTRO])
        assert caught.value.state is Capability.UNREACHABLE
        emby.server.delete_emby_markers.assert_called_once_with("42")

    @pytest.mark.parametrize(
        ("answer", "error", "state"),
        [
            (MagicMock(status_code=200, **{"json.return_value": {"Found": False, "Error": "item not found"}}), ItemNotFoundError, None),
            (MagicMock(status_code=200, **{"json.return_value": {"Found": True, "Error": "item is not a video"}}), PublishError, None),
            (MagicMock(status_code=200, **{"json.side_effect": ValueError}), PublishError, None),
            (MagicMock(status_code=404, **{"json.side_effect": ValueError}), PublishError, Capability.NEEDS_PLUGIN),
            (MagicMock(status_code=401), PublishError, None),
            (MagicMock(status_code=403), PublishError, None),
            (MagicMock(status_code=500, **{"json.return_value": {"Found": True, "Error": "couldn't write the item's chapters (IOException)"}}), PublishError, None),
            (MagicMock(status_code=503), PublishError, Capability.UNREACHABLE),
        ],
        ids=["not-found", "refused", "unreadable", "no-route", "401", "403", "500", "503"],
    )  # fmt: skip
    def test_answers_map_to_errors(self, emby, answer, error, state):
        emby.server.put_emby_markers.side_effect = None
        emby.server.put_emby_markers.return_value = answer
        with pytest.raises(error) as caught:
            _write(emby, [INTRO])
        assert type(caught.value) is error and caught.value.state is state

    def test_the_plugins_error_text_is_named(self, emby):
        body = {"Found": True, "Error": "couldn't write the item's chapters (IOException)"}
        emby.server.put_emby_markers.side_effect = None
        emby.server.put_emby_markers.return_value = MagicMock(status_code=500, **{"json.return_value": body})
        with pytest.raises(PublishError, match=r"HTTP 500: couldn't write the item's chapters \(IOException\)"):
            _write(emby, [INTRO])

    @pytest.mark.parametrize(
        ("status", "words"), [(401, "rejected this server's credentials"), (403, "needs administrator rights")]
    )
    def test_auth_answers_say_what_to_fix(self, emby, status, words):
        emby.server.put_emby_markers.side_effect = None
        emby.server.put_emby_markers.return_value = MagicMock(status_code=status)
        with pytest.raises(PublishError, match=words):
            _write(emby, [INTRO])

    def test_a_stale_store_is_posted_again_even_when_its_ticks_match(self, emby):
        _write(emby, [INTRO])
        state = emby.server.get_emby_marker_state.side_effect
        emby.server.get_emby_marker_state.side_effect = lambda item_id: {**state(item_id), "stale": True}
        emby.server.put_emby_markers.reset_mock()
        assert _write(emby, [INTRO], previous=[INTRO]) == [INTRO]
        assert emby.server.put_emby_markers.call_count == 1

    def test_transport_failure_is_unreachable(self, emby):
        emby.server.put_emby_markers.side_effect = requests.ConnectionError("down")
        with pytest.raises(PublishError) as caught:
            _write(emby, [INTRO])
        assert caught.value.state is Capability.UNREACHABLE


class TestKeepEmbys:
    """ "When Emby has its own markers": Use ours (restore) sends ReplaceOwn; Keep Emby's leaves Emby's rows of a type."""

    @pytest.mark.parametrize("setting", ["restore", "keep_emby"])
    def test_emby_rows_on_a_first_publish_follow_the_setting(self, emby, setting):
        emby.rows += EMBY_INTRO
        publisher = _publisher(emby, setting)
        ours = _write(emby, [INTRO, CREDITS_TO_END], publisher=publisher)
        assert emby.replace_own == [setting == "restore"]
        if setting == "keep_emby":
            assert ours == [CREDITS_TO_END] and publisher.last_kept_types == frozenset({T.INTRO})
            assert emby.markers_shown() == [("IntroStart", 60_000), ("IntroEnd", 90_000), ("CreditsStart", 1_295_324)]
        else:
            assert ours == [INTRO, CREDITS_TO_END] and publisher.last_kept_types == frozenset()
            assert emby.markers_shown() == [("IntroStart", 126_771), ("IntroEnd", 157_068), ("CreditsStart", 1_295_324)]

    def test_kept_type_with_emby_rows_still_there_sends_nothing(self, emby):
        emby.rows += EMBY_INTRO
        publisher = _publisher(emby, "keep_emby")
        _write(emby, [INTRO, CREDITS_TO_END], publisher=publisher)
        emby.server.put_emby_markers.reset_mock()
        ours = _write(
            emby, [INTRO, CREDITS_TO_END], previous=[CREDITS_TO_END], kept_types={T.INTRO}, publisher=publisher
        )
        assert ours == [CREDITS_TO_END] and publisher.last_kept_types == frozenset({T.INTRO})
        emby.server.put_emby_markers.assert_not_called()
        assert publisher.last_write_changed is False
        assert publisher.shows("42", ours, kept_types=frozenset({T.INTRO})) is Shown.OURS

    def test_kept_type_emby_dropped_is_missing_and_ours_go_back(self, emby):
        emby.rows += EMBY_INTRO
        publisher = _publisher(emby, "keep_emby")
        _write(emby, [INTRO, CREDITS_TO_END], publisher=publisher)
        emby.rows = [r for r in emby.rows if r not in EMBY_INTRO]  # Emby removed its intro
        assert publisher.shows("42", [CREDITS_TO_END], kept_types=frozenset({T.INTRO})) is Shown.MISSING
        ours = _write(
            emby, [INTRO, CREDITS_TO_END], previous=[CREDITS_TO_END], kept_types={T.INTRO}, publisher=publisher
        )
        assert ours == [INTRO, CREDITS_TO_END] and publisher.last_kept_types == frozenset()

    def test_switching_to_use_ours_replaces_a_kept_type(self, emby):
        emby.rows += EMBY_INTRO
        _write(emby, [INTRO, CREDITS_TO_END], publisher=_publisher(emby, "keep_emby"))
        publisher = _publisher(emby, "restore")
        ours = _write(
            emby, [INTRO, CREDITS_TO_END], previous=[CREDITS_TO_END], kept_types={T.INTRO}, publisher=publisher
        )
        assert ours == [INTRO, CREDITS_TO_END] and publisher.last_kept_types == frozenset()
        assert emby.replace_own == [False, True]
        assert ("IntroStart", 60_000) not in emby.markers_shown()

    def test_the_saved_setting_is_read_at_write_time(self, emby):
        emby.rows += EMBY_INTRO
        live = {"setting": "restore"}

        def saved():
            return load_server(
                {"enabled": True, "library_ids": None, "emby": {"on_emby_redetect": live["setting"]}}, "emby"
            )

        publisher = _publisher(emby, "restore", settings_provider=saved)
        live["setting"] = "keep_emby"
        assert _write(emby, [INTRO], publisher=publisher) == []
        assert emby.replace_own == [False] and publisher.last_kept_types == frozenset({T.INTRO})

    def test_ours_written_back_for_a_kept_type_after_a_refresh_are_ours_again(self, emby):
        emby.rows += EMBY_INTRO
        publisher = _publisher(emby, "keep_emby")
        assert _write(emby, [INTRO, CREDITS_TO_END], publisher=publisher) == [CREDITS_TO_END]
        # "Replace all metadata" deletes every marker row; the plugin writes back what it stores, the kept intro too.
        emby.rows = [("Chapter", 0, "Chapter 1"), *_rows_for(emby.stored)]
        assert publisher.shows("42", [CREDITS_TO_END], kept_types=frozenset({T.INTRO})) is Shown.MISSING
        emby.server.put_emby_markers.reset_mock()
        ours = _write(
            emby, [INTRO, CREDITS_TO_END], previous=[CREDITS_TO_END], kept_types={T.INTRO}, publisher=publisher
        )
        assert ours == [INTRO, CREDITS_TO_END] and publisher.last_kept_types == frozenset()
        emby.server.put_emby_markers.assert_not_called()  # nothing to change on Emby, only the record
        assert publisher.shows("42", ours) is Shown.OURS

    def test_a_kept_intro_with_only_a_start_row_is_still_embys(self, emby):
        emby.rows += EMBY_INTRO[:1]  # another writer left an IntroStart without an IntroEnd
        publisher = _publisher(emby, "keep_emby")
        assert _write(emby, [INTRO, CREDITS_TO_END], publisher=publisher) == [CREDITS_TO_END]
        assert publisher.last_kept_types == frozenset({T.INTRO})
        assert publisher.shows("42", [CREDITS_TO_END], kept_types=frozenset({T.INTRO})) is Shown.OURS

    @pytest.mark.parametrize(("previous", "changed"), [([], False), ([OLD_INTRO], True)])
    def test_a_write_that_keeps_every_type_changes_nothing_unless_ours_were_there(self, emby, previous, changed):
        emby.rows += [*EMBY_INTRO, ("CreditsStart", 11_000_000_000, "Credits")]
        publisher = _publisher(emby, "keep_emby")
        assert _write(emby, [INTRO, CREDITS_TO_END], previous=previous, publisher=publisher) == []
        assert publisher.last_kept_types == frozenset({T.INTRO, T.CREDITS})
        assert publisher.last_write_changed is changed

    def test_rows_this_app_left_that_stay_are_never_taken_for_embys(self, emby):
        publisher = _publisher(emby, "keep_emby")
        _write(emby, [OLD_INTRO], publisher=publisher)
        emby.stored, emby.size = None, None
        real_apply = emby._apply
        emby._apply = lambda wanted, replace_own: real_apply(wanted, False)  # Emby ignores ReplaceOwn
        with pytest.raises(PublishError, match="chapters don't show"):
            _write(emby, [INTRO], previous=[OLD_INTRO], publisher=publisher)

    def test_rows_this_app_left_are_replaced_when_the_plugin_lost_its_store(self, emby):
        publisher = _publisher(emby, "keep_emby")
        _write(emby, [OLD_INTRO, CREDITS_TO_END], publisher=publisher)
        emby.stored, emby.size = None, None  # the plugin's store folder was removed; our rows stay on the item
        ours = _write(emby, [INTRO, CREDITS_TO_END], previous=[OLD_INTRO, CREDITS_TO_END], publisher=publisher)
        assert ours == [INTRO, CREDITS_TO_END] and publisher.last_kept_types == frozenset()
        assert emby.replace_own == [False, False, True, False]
        replaced = emby.server.put_emby_markers.call_args_list[2].kwargs
        assert (replaced["intro_start_ticks"], replaced["credits_start_ticks"]) == (1_267_710_000, None)
        assert emby.markers_shown() == [("IntroStart", 126_771), ("IntroEnd", 157_068), ("CreditsStart", 1_295_324)]
        assert emby.stored == (1_267_710_000, 1_570_680_000, 12_953_240_000)


OTHER_INTRO = Marker(T.INTRO, 127_900, 158_100, ("chapters",))  # within 2 s of INTRO
OTHER_CREDITS_EARLIER = Marker(T.CREDITS, 1_276_324, DUR + 10_000, ("chapters",))  # 19 s earlier: disagrees


@pytest.fixture
def versions(tmp_path):
    """Item 42 = S01E01 (321 bytes) and item 43 = its Extended cut (654 bytes), each listing both versions, 43 first."""
    mine, other = tmp_path / "S01E01.mkv", tmp_path / "S01E01 - Extended.mkv"
    mine.write_bytes(b"x" * 321)
    other.write_bytes(b"y" * 654)
    fake = FakeEmbyItems({"43": str(other), "42": str(mine)})
    fake.publisher = _publisher(fake)
    fake.mine, fake.other = str(mine), str(other)
    return fake


def _write_item(fake, item_id, markers, previous=(), path=None):
    return fake.publisher.write(item_id, list(markers), previous=None if previous is None else list(previous),
                                duration_ms=DUR, canonical_path=path or fake.items[item_id].path)  # fmt: skip


class TestVersions:
    """Emby's player shows the chapters of the version playing: each version is an item published on its own."""

    def test_each_version_gets_its_own_decision_with_its_own_file_size(self, versions):
        assert versions.items["42"].versions[0] == (versions.other, "43")  # the item's own file isn't listed first
        assert _write_item(versions, "42", [INTRO, CREDITS_TO_END]) == [INTRO, CREDITS_TO_END]
        assert versions.server.put_emby_markers.call_args.args == ("42",)
        assert versions.server.put_emby_markers.call_args.kwargs["file_size"] == 321
        assert _write_item(versions, "43", [OTHER_INTRO, OTHER_CREDITS_EARLIER]) == [OTHER_INTRO, OTHER_CREDITS_EARLIER]
        assert versions.server.put_emby_markers.call_args.args == ("43",)
        assert versions.server.put_emby_markers.call_args.kwargs["file_size"] == 654
        assert versions.items["42"].markers_shown() == [
            ("IntroStart", 126_771), ("IntroEnd", 157_068), ("CreditsStart", 1_295_324),
        ]  # fmt: skip
        assert versions.items["43"].markers_shown() == [
            ("IntroStart", 127_900), ("IntroEnd", 158_100), ("CreditsStart", 1_276_324),
        ]  # fmt: skip
        assert versions.publisher.last_item_files is None

    def test_a_version_never_decided_doesnt_hold_back_the_other(self, versions):
        assert _write_item(versions, "42", [INTRO], previous=[]) == [INTRO]
        versions.server.delete_emby_markers.assert_not_called()
        assert versions.items["43"].markers_shown() == []

    def test_a_file_emby_lists_as_another_versions_item_is_refused(self, versions):
        # The job found item 42 for the Extended file: its markers belong on item 43, whose chapters play with it.
        with pytest.raises(PublishError, match="This file is Emby item 43, another version of item 42") as caught:
            _write_item(versions, "42", [INTRO], path=versions.other)
        assert caught.value.state is None
        versions.server.put_emby_markers.assert_not_called()

    @pytest.mark.parametrize(
        "own_version",
        [
            pytest.param(("/elsewhere/a.mkv", "42"), id="no-version-is-this-file"),
            # Emby lists this file but names no item for it, so it can't be confirmed as item 42's own version.
            pytest.param(("MINE", None), id="this-file-without-an-item-id"),
        ],
    )
    def test_a_file_that_is_none_of_the_items_versions_is_not_in_the_library(self, versions, own_version):
        path, version_id = own_version
        versions.items["42"].versions = [
            (versions.items["42"].path if path == "MINE" else path, version_id),
            ("/elsewhere/b.mkv", "43"),
        ]
        with pytest.raises(ItemNotFoundError) as caught:
            _write_item(versions, "42", [INTRO])
        assert str(caught.value) == (
            "Emby doesn't show which of this item's versions is this file yet; if the file is already in Emby's "
            "library, check this server's path mappings"
        )
        versions.server.put_emby_markers.assert_not_called()

    @pytest.mark.parametrize("version_id", ["42", None], ids=["own-id", "no-item-id"])
    def test_one_file_is_the_item_the_job_found_even_when_its_path_doesnt_map_back(self, emby, version_id):
        emby.versions = [("/server/view/S01E01.mkv", version_id)]
        assert _write(emby, [INTRO]) == [INTRO]
        assert emby.server.put_emby_markers.call_args.kwargs["file_size"] == 321

    def test_an_unreadable_item_fails_without_writing(self, versions):
        versions.server.get_chapters_and_versions.side_effect = lambda item_id: None
        with pytest.raises(PublishError, match="Couldn't read this Emby item") as caught:
            _write_item(versions, "42", [INTRO])
        assert caught.value.state is None
        versions.server.put_emby_markers.assert_not_called()

    def test_read_back_ignores_item_files(self, versions):
        ours = _write_item(versions, "42", [INTRO])
        versions.server.reset_mock()
        assert versions.publisher.shows("42", ours, item_files=("/elsewhere/a.mkv",)) is Shown.OURS
        versions.server.get_chapters_and_versions.assert_not_called()
        assert versions.server.get_chapter_markers.call_count == 1


class TestShows:
    def test_ours_missing_and_replaced(self, emby):
        _write(emby, [INTRO, CREDITS_TO_END])
        assert emby.publisher.shows("42", [INTRO, CREDITS_TO_END]) is Shown.OURS
        emby.rows = [("Chapter", 0, "Chapter 1"), *EMBY_INTRO, ("CreditsStart", 12_953_240_000, "Credits")]
        assert emby.publisher.shows("42", [INTRO, CREDITS_TO_END]) is Shown.REPLACED
        emby.rows = [("Chapter", 0, "Chapter 1")]
        assert emby.publisher.shows("42", [INTRO]) is Shown.MISSING

    def test_credits_are_compared_by_their_start(self, emby):
        _write(emby, [CREDITS_EARLY])
        assert emby.publisher.shows("42", [CREDITS_EARLY]) is Shown.OURS
        assert emby.publisher.shows("42", [Marker(T.CREDITS, 1_250_001, DUR, ())]) is Shown.REPLACED

    def test_a_second_intro_beside_ours_is_not_ours(self, emby):
        _write(emby, [INTRO])
        emby.rows = sorted([*emby.rows, *EMBY_INTRO], key=lambda r: r[1])
        assert emby.publisher.shows("42", [INTRO]) is Shown.MISSING

    def test_kept_types_whose_plugin_store_cant_be_read_are_unknown(self, emby):
        emby.rows += EMBY_INTRO
        publisher = _publisher(emby, "keep_emby")
        _write(emby, [INTRO], publisher=publisher)
        emby.server.get_emby_marker_state.side_effect = lambda item_id: None
        assert publisher.shows("42", [], kept_types=frozenset({T.INTRO})) is None

    def test_unreadable_chapters_are_unknown(self, emby):
        emby.server.get_chapter_markers.side_effect = None
        emby.server.get_chapter_markers.return_value = None
        assert emby.publisher.shows("42", [INTRO]) is None
