"""The Plex marker agent's wire protocol and the app's side of it (plan phase 4 Task 10).

The agent's own side lives in ``tests/test_plex_marker_agent.py``, including the two containers' round trip.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.markers.decide import FileLimits
from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers import plex_remote
from media_preview_generator.markers.publishers.base import (
    Capability,
    CapabilityReport,
    DatabaseBusyError,
    ItemNotFoundError,
    PublishError,
    Shown,
)
from media_preview_generator.markers.publishers.plex_db import (
    ItemRead,
    LocalPlexDb,
    PlexMarkerPublisher,
    ShownAnswer,
    ShownAsk,
    ShownBatch,
    WriteRequest,
    WriteResult,
    _Part,
)
from media_preview_generator.markers.publishers.plex_remote import AgentError, RemotePlexDb, plex_database
from media_preview_generator.markers.settings import ServerMarkersSettings
from media_preview_generator.servers.base import ServerConfig, ServerType

T = MarkerType
TOKEN = "s3cret-key-nobody-should-see"
URL = "http://plex-host.lan:9494"
INTRO = Marker(T.INTRO, 11_000, 37_000, ("chapters",))
CREDITS = Marker(T.CREDITS, 1_299_000, 1_320_000, ("chapters", "user"), locked=True)
PART = _Part(1, 1, "/plexmedia/tv/S01E01.mkv", '{"pv:intros":"x","url":"y"}', None)


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """A ``requests.Session`` that answers from a script and records what it was sent."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        answer = self.answers.pop(0) if self.answers else FakeResponse(200, {})
        if isinstance(answer, Exception):
            raise answer
        return answer


def _envelope(result: dict, *, version: str = "1.0.0", protocols=(1,)) -> FakeResponse:
    return FakeResponse(
        200, {"ok": True, "agent": {"version": version, "protocols": list(protocols)}, "result": result}
    )


def _refusal(error: dict, *, version: str = "1.0.0", protocols=(1,)) -> FakeResponse:
    return FakeResponse(409, {"ok": False, "agent": {"version": version, "protocols": list(protocols)}, "error": error})


def _remote(*answers) -> tuple[RemotePlexDb, FakeSession]:
    session = FakeSession(*answers)
    return RemotePlexDb(URL, TOKEN, session=session), session


class TestCodec:
    """Every payload the two sides exchange survives the round trip unchanged."""

    @pytest.mark.parametrize("marker", [INTRO, CREDITS, Marker(T.CREDITS, 0, 1, ())])
    def test_a_marker_round_trips(self, marker):
        assert plex_remote.marker_from_json(plex_remote.marker_to_json(marker)) == marker

    @pytest.mark.parametrize(
        "part",
        [PART, _Part(3, 4, "/x.mkv", None, 42), _Part(5, 6, "/y.mkv", "pv%3Aintros=%7B%7D", None)],
        ids=["json-extra", "optimized-copy", "url-form-extra"],
    )
    def test_a_part_round_trips(self, part):
        assert plex_remote.part_from_json(plex_remote.part_to_json(part)) == part

    def test_a_write_request_round_trips_every_field(self):
        request = WriteRequest(
            rating_key=7,
            parts=[PART],
            wanted=[INTRO, CREDITS],
            prior=[INTRO],
            duration_ms=1_320_000,
            own_prior=[CREDITS],
            calling_part_ids=(1, 2),
            kept_types=frozenset({T.CREDITS}),
            keep_plex=True,
        )
        assert plex_remote.write_request_from_json(plex_remote.write_request_to_json(request)) == request

    def test_a_write_request_with_nothing_set_round_trips(self):
        request = WriteRequest(7, [], [], [], None, [], (), frozenset(), False)
        assert plex_remote.write_request_from_json(plex_remote.write_request_to_json(request)) == request

    def test_the_files_limits_travel_with_a_write(self):
        # The agent runs "Keep Plex's": a kept Plex marker must fit the same limits the app checks.
        request = WriteRequest(7, [PART], [INTRO], [], 2_498_304, [], (1,), frozenset(), True, FileLimits(2_498_304))

        body = plex_remote.write_request_to_json(request)

        assert body["limits"] == {"duration_ms": 2_498_304}
        assert plex_remote.write_request_from_json(body) == request

    def test_a_write_from_an_app_without_limits_checks_nothing(self):
        body = plex_remote.write_request_to_json(WriteRequest(7, [PART], [INTRO], [], 1, [], (1,), frozenset(), True))
        body.pop("limits", None)
        assert plex_remote.write_request_from_json(body).limits is None

    @pytest.mark.parametrize(
        "limits",
        ["text", {"duration_ms": "1"}, {}, {"duration_ms": True}],
        ids=["not-an-object", "text-number", "no-duration", "bool-for-int"],
    )
    def test_limits_that_arent_limits_are_refused(self, limits):
        body = plex_remote.write_request_to_json(WriteRequest(7, [PART], [INTRO], [], 1, [], (1,), frozenset(), True))
        with pytest.raises(ValueError):
            plex_remote.write_request_from_json({**body, "limits": limits})

    def test_a_write_result_round_trips(self):
        result = WriteResult(True, [INTRO], frozenset({T.INTRO}), frozenset({T.CREDITS}))
        assert plex_remote.write_result_from_json(plex_remote.write_result_to_json(result)) == result

    def test_an_item_read_round_trips(self):
        item = ItemRead(True, [PART])
        assert plex_remote.item_read_from_json(plex_remote.item_read_to_json(item)) == item

    @pytest.mark.parametrize("stale", [frozenset(), frozenset({T.INTRO}), frozenset({T.INTRO, T.CREDITS})])
    def test_an_item_reads_stale_types_and_file_times(self, stale):
        item = ItemRead(True, [PART._replace(updated_at=1_790_207_382)], stale)
        body = plex_remote.item_read_to_json(item)
        assert body["stale_types"] == sorted(t.value for t in stale)
        assert body["parts"][0]["updated_at"] == 1_790_207_382
        assert plex_remote.item_read_from_json(body) == item

    @pytest.mark.parametrize("updated_at", ["2026-09-23", True, 1.5, {}], ids=["text", "bool", "float", "object"])
    def test_a_file_time_that_isnt_a_number_reads_as_unknown(self, updated_at):
        body = plex_remote.part_to_json(PART)
        body["updated_at"] = updated_at
        assert plex_remote.part_from_json(body).updated_at is None

    def test_a_write_result_says_which_stale_types_it_replaced(self):
        result = WriteResult(True, [INTRO], frozenset(), frozenset(), frozenset({T.INTRO}))
        body = plex_remote.write_result_to_json(result)
        assert body["replaced_stale"] == ["intro"]
        assert plex_remote.write_result_from_json(body) == result
        body.pop("replaced_stale")  # an agent older than this field
        assert plex_remote.write_result_from_json(body).replaced_stale == frozenset()

    def test_an_older_agents_item_read_says_nothing_about_staleness(self):
        # An agent from before this field: unknown, so the pipeline and Keep Plex's behave as before.
        body = plex_remote.item_read_to_json(ItemRead(True, [PART], frozenset({T.INTRO})))
        body.pop("stale_types")
        for part in body["parts"]:
            part.pop("updated_at")
        assert plex_remote.item_read_from_json(body) == ItemRead(True, [PART], None)

    @pytest.mark.parametrize("files", [None, (), ("/a.mkv", "/b.mkv")], ids=["not-recorded", "empty", "two"])
    def test_a_read_back_ask_round_trips(self, files):
        ask = ShownAsk(7, [INTRO], frozenset({T.CREDITS}), files)
        assert plex_remote.shown_ask_from_json(plex_remote.shown_ask_to_json(ask)) == ask

    @pytest.mark.parametrize("shown", list(Shown) + [None])
    def test_a_read_back_batch_round_trips_every_answer(self, shown):
        batch = ShownBatch([ShownAnswer(shown, ("/a.mkv",)), ShownAnswer(None, ())], True)
        assert plex_remote.shown_batch_from_json(plex_remote.shown_batch_to_json(batch)) == batch

    @pytest.mark.parametrize("state", list(Capability))
    def test_a_capability_report_round_trips_every_state(self, state):
        report = CapabilityReport(state, "message", {"db_path": "/p/db", "lock_holder": True})
        assert plex_remote.report_from_json(plex_remote.report_to_json(report)) == report

    @pytest.mark.parametrize(
        ("error", "kind", "state"),
        [
            (PublishError("busy", state=Capability.UNREACHABLE), PublishError, Capability.UNREACHABLE),
            (ItemNotFoundError("gone"), ItemNotFoundError, None),
            (
                PublishError("odd schema", state=Capability.UNSUPPORTED_SCHEMA),
                PublishError,
                Capability.UNSUPPORTED_SCHEMA,
            ),
            (PublishError("no state"), PublishError, None),
            # The agent's write gave up on Plex's busy database: the app's job retries it minutes later.
            (
                DatabaseBusyError("Plex's database was busy (held by another program) for 121 s; trying again on the next run",
                                  state=Capability.UNREACHABLE),
                DatabaseBusyError,
                Capability.UNREACHABLE,
            ),
        ],
        ids=["publish-with-state", "item-not-found", "schema", "stateless", "plex-db-busy"],
    )  # fmt: skip
    def test_a_refusal_comes_back_as_the_very_same_exception(self, error, kind, state):
        rebuilt = plex_remote.error_from_json(plex_remote.error_to_json(error))
        assert type(rebuilt) is kind
        assert str(rebuilt) == str(error)
        assert rebuilt.state is state

    @pytest.mark.parametrize(
        "raw",
        [None, "text", {"type": "nope", "start_ms": 1, "end_ms": 2}, {"type": "intro", "start_ms": "1", "end_ms": 2},
         {"type": "intro", "start_ms": True, "end_ms": 2}],
        ids=["none", "text", "unknown-type", "text-number", "bool-for-int"],
    )  # fmt: skip
    def test_a_marker_that_isnt_one_is_refused(self, raw):
        with pytest.raises(ValueError):
            plex_remote.marker_from_json(raw)

    @pytest.mark.parametrize("raw", [None, [], {"rating_key": 7}, {"rating_key": "7", "parts": [], "wanted": []}])
    def test_a_write_request_that_isnt_one_is_refused(self, raw):
        with pytest.raises(ValueError):
            plex_remote.write_request_from_json(raw)


class TestVersionSkew:
    """An agent and an app that don't match refuse each other by name, and never write."""

    @pytest.mark.parametrize(
        ("version", "too_old"),
        [("0.9.0", True), ("1.0.0", False), ("1.0.1", False), ("2.0.0", False), ("", False), ("dev", False)],
    )
    def test_only_a_lower_readable_version_counts_as_too_old(self, version, too_old):
        assert plex_remote.agent_too_old(version) is too_old

    def test_an_agent_ahead_of_this_app_says_to_update_the_app(self):
        remote, _ = _remote(FakeResponse(409, {"agent": {"version": "2.0.0", "protocols": [2]}, "error": {}}))
        with pytest.raises(AgentError) as ei:
            remote.read_item(7, deadline=_deadline())
        message = str(ei.value)
        assert "2.0.0" in message and "protocol 2" in message and f"speaks {plex_remote.AGENT_PROTOCOL}" in message
        assert "Update this app" in message and "Update the agent" not in message
        assert ei.value.state is Capability.AGENT_UNAVAILABLE

    @pytest.mark.parametrize(
        ("version", "protocols"),
        # An agent that names no protocols at all is given the benefit of the doubt (its own answer decides), so
        # there is no skew refusal to word in that case.
        [("2.0.0", [2]), ("", [2]), ("dev", [2])],
        ids=["newer", "no-version", "unreadable-version"],
    )
    def test_an_agent_that_isnt_provably_old_never_says_update_the_agent(self, version, protocols):
        # Telling the user to update an agent that has moved PAST this app sends them in a circle.
        remote, _ = _remote(FakeResponse(409, {"agent": {"version": version, "protocols": protocols}, "error": {}}))
        with pytest.raises(AgentError) as ei:
            remote.read_item(7, deadline=_deadline())
        assert "Update the agent" not in str(ei.value)
        assert "Update this app" in str(ei.value)

    def test_an_agent_behind_this_app_says_to_update_the_agent(self):
        remote, _ = _remote(FakeResponse(409, {"agent": {"version": "0.3.0", "protocols": [1]}, "error": {}}))
        with pytest.raises(AgentError) as ei:
            remote.read_item(7, deadline=_deadline())
        assert "0.3.0" in str(ei.value) and plex_remote.MIN_AGENT_VERSION in str(ei.value)
        assert "Update the agent container" in str(ei.value)

    def test_an_agent_older_than_this_app_is_refused_even_when_it_answers(self):
        remote, _ = _remote(_envelope({"item": {"exists": True, "parts": []}}, version="0.3.0"))
        with pytest.raises(AgentError) as ei:
            remote.read_item(7, deadline=_deadline())
        assert "0.3.0" in str(ei.value) and "1.0.0" in str(ei.value)

    def test_every_request_says_which_protocol_it_speaks(self):
        remote, session = _remote(_envelope({"item": {"exists": True, "parts": []}}))
        remote.read_item(7, deadline=_deadline())
        assert session.calls[0]["headers"][plex_remote.PROTOCOL_HEADER] == str(plex_remote.AGENT_PROTOCOL)

    def test_a_skewed_agent_reads_as_a_capability_problem_not_a_crash(self):
        remote, _ = _remote(FakeResponse(409, {"agent": {"version": "0.1.0", "protocols": [1]}, "error": {}}))
        report = remote.file_checks(deadline=_deadline())
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert report.details["agent"]["state"] == "incompatible"


def _deadline(seconds: float = 8.0) -> float:
    import time

    return time.monotonic() + seconds


class TestFailuresLookLikeTodays:
    """Whatever goes wrong, the app sees the same exception shape it sees from a local database."""

    def test_a_refusal_from_the_agent_is_raised_unchanged(self):
        remote, _ = _remote(
            _refusal({"kind": "publish", "message": "Plex is busy writing its database", "state": "unreachable"})
        )
        with pytest.raises(PublishError) as ei:
            remote.write_item(
                WriteRequest(7, [PART], [INTRO], [], 1, [], (1,), frozenset(), False), deadline=_deadline()
            )
        assert str(ei.value) == "Plex is busy writing its database"
        assert ei.value.state is Capability.UNREACHABLE and type(ei.value) is PublishError

    def test_an_item_the_agent_cant_find_stays_an_item_not_found(self):
        remote, _ = _remote(_refusal({"kind": "item_not_found", "message": "Plex item 7 not found", "state": None}))
        with pytest.raises(ItemNotFoundError):
            remote.read_item(7, deadline=_deadline())

    def test_an_unreachable_agent_is_a_capability_answer_with_the_wait_here_wording(self):
        remote, _ = _remote(requests.ConnectionError("connection refused"))
        report = remote.file_checks(deadline=_deadline())
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert "Can't reach the Plex marker agent" in report.message and "nothing is lost" in report.message
        assert report.details["agent"] == {
            "url": URL,
            "version": "",
            "state": "unreachable",
            "machine_identifier": "",
        }

    def test_an_agent_that_goes_away_mid_publish_fails_the_write_and_says_so(self):
        remote, _ = _remote(requests.ReadTimeout("timed out"))
        with pytest.raises(AgentError) as ei:
            remote.write_item(
                WriteRequest(7, [PART], [INTRO], [], 1, [], (1,), frozenset(), False), deadline=_deadline()
            )
        assert ei.value.state is Capability.AGENT_UNAVAILABLE and URL in str(ei.value)

    def test_a_wrong_key_is_refused_and_named(self):
        remote, _ = _remote(FakeResponse(401, {"agent": {"version": "1.0.0", "protocols": [1]}, "error": {}}))
        report = remote.file_checks(deadline=_deadline())
        assert report.details["agent"]["state"] == "rejected"
        assert "refused this app's key" in report.message

    @pytest.mark.parametrize(
        "answer",
        [FakeResponse(500, {}), FakeResponse(200, {"ok": True}), FakeResponse(200, ValueError("not json")),
         FakeResponse(200, [1, 2])],
        ids=["server-error", "no-result", "not-json", "not-an-object"],
    )  # fmt: skip
    def test_an_answer_this_app_cant_read_never_looks_like_success(self, answer):
        remote, _ = _remote(answer)
        with pytest.raises(PublishError):
            remote.read_item(7, deadline=_deadline())

    @pytest.mark.parametrize(
        "answer",
        [requests.ConnectionError("refused"), FakeResponse(401, {}), FakeResponse(500, {}),
         _refusal({"kind": "publish", "message": "busy", "state": "unreachable"})],
        ids=["unreachable", "wrong-key", "server-error", "refusal"],
    )  # fmt: skip
    def test_the_shared_key_is_never_in_an_error_message(self, answer):
        remote, _ = _remote(answer)
        with pytest.raises(PublishError) as ei:
            remote.read_item(7, deadline=_deadline())
        assert TOKEN not in str(ei.value)

    def test_the_key_goes_in_the_header_and_never_in_the_body(self):
        remote, session = _remote(_envelope({"item": {"exists": True, "parts": []}}))
        remote.read_item(7, deadline=_deadline())
        call = session.calls[0]
        assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(call["json"])

    def test_no_address_is_a_refusal_rather_than_a_request(self):
        remote = RemotePlexDb("", TOKEN, session=FakeSession())
        report = remote.file_checks(deadline=_deadline())
        assert report.state is Capability.AGENT_UNAVAILABLE


class TestWhatIsSent:
    """The agent is told what to do and nothing else — no path, and a bounded deadline."""

    def test_the_write_carries_the_decision_and_no_database_path(self):
        remote, session = _remote(
            _envelope({"write": {"changed": True, "ours": [], "kept_types": [], "replaced_own": []}})
        )
        request = WriteRequest(7, [PART], [INTRO], [CREDITS], 1_320_000, [], (1,), frozenset({T.CREDITS}), True)
        remote.write_item(request, deadline=_deadline())
        body = session.calls[0]["json"]
        assert plex_remote.write_request_from_json(body["write"]) == request
        assert body["deadline_s"] > 0
        assert "db_path" not in str(body) and "plex_config_folder" not in str(body)

    def test_the_deadline_the_app_is_holding_is_what_the_agent_is_given(self):
        remote, session = _remote(_envelope({"report": {"state": "ready", "message": "", "details": {}}}))
        remote.file_checks(deadline=_deadline(5.0))
        assert 4.0 < session.calls[0]["json"]["deadline_s"] <= 5.0
        # The HTTP wait is that deadline plus the grace the agent needs to answer after it.
        assert session.calls[0]["timeout"][1] == pytest.approx(session.calls[0]["json"]["deadline_s"] + 3.0, abs=0.5)

    def test_a_read_back_is_chunked_and_says_how_long_each_item_may_wait(self):
        answers = [
            _envelope({"batch": {"answers": [{"shown": "ours", "version_files": []}] * 50, "unreadable": False}}),
            _envelope({"batch": {"answers": [{"shown": "ours", "version_files": []}] * 5, "unreadable": False}}),
        ]
        remote, session = _remote(*answers)
        batch = remote.shown_many([ShownAsk(i, [INTRO], frozenset(), None) for i in range(55)], timeout_s=30.0)
        assert len(batch.answers) == 55 and batch.unreadable is False
        assert [len(c["json"]["items"]) for c in session.calls] == [50, 5]
        assert session.calls[0]["json"]["timeout_s"] == 30.0
        assert session.calls[0]["json"]["budget_s"] == plex_remote.READ_BACK_BUDGET_S

    def test_a_cancel_between_chunks_stops_asking_and_leaves_the_rest_unread(self):
        answers = [
            _envelope({"batch": {"answers": [{"shown": "ours", "version_files": []}] * 50, "unreadable": False}})
        ]
        remote, session = _remote(*answers)
        asked = []
        batch = remote.shown_many(
            [ShownAsk(i, [INTRO], frozenset(), None) for i in range(120)],
            timeout_s=30.0,
            cancel_check=lambda: bool(asked) or asked.append(1),
        )
        assert len(session.calls) == 1 and len(batch.answers) == 50 and batch.unreadable is False

    def test_an_agent_that_stops_mid_read_back_marks_the_rest_unread(self):
        remote, _ = _remote(
            _envelope({"batch": {"answers": [{"shown": "ours", "version_files": []}] * 50, "unreadable": False}}),
            requests.ConnectionError("gone"),
        )
        asks = [ShownAsk(i, [INTRO], frozenset(), None) for i in range(60)]
        batch = remote.shown_many(asks, timeout_s=30.0)
        assert len(batch.answers) == 50 and batch.unreadable is True

    def test_an_agent_that_answers_fewer_items_than_asked_leaves_them_unread_without_unreadable(self):
        # Its own budget ran out: the items left are simply not read, exactly as a cancelled local read-back leaves them.
        remote, _ = _remote(
            _envelope({"batch": {"answers": [{"shown": "ours", "version_files": []}] * 3, "unreadable": False}})
        )
        batch = remote.shown_many([ShownAsk(i, [INTRO], frozenset(), None) for i in range(10)], timeout_s=30.0)
        assert len(batch.answers) == 3 and batch.unreadable is False


class FakeDatabase:
    """Records what the publisher asks the database half to do."""

    atomic_writes = True

    def __init__(self, *, parts=(PART,), exists=True) -> None:
        self.item = ItemRead(exists, list(parts))
        self.writes: list[WriteRequest] = []
        self.asks: list[ShownAsk] = []
        self.timeouts: list[float] = []
        self.result = WriteResult(True, [INTRO], frozenset(), frozenset())
        self.batch = ShownBatch([ShownAnswer(Shown.OURS, ("/plexmedia/tv/S01E01.mkv",))], False)

    file_report = CapabilityReport(Capability.READY, "", {"db_path": "/agent/db", "lock_holder": True})

    def file_checks(self, *, deadline):
        return self.file_report

    def db_checks(self, *, deadline):
        return CapabilityReport(Capability.READY, "")

    def read_item(self, rating_key, *, deadline):
        return self.item

    def write_item(self, request, *, deadline):
        self.writes.append(request)
        return self.result

    def shown_many(self, items, *, timeout_s, cancel_check=None):
        self.asks.extend(items)
        self.timeouts.append(timeout_s)
        return self.batch

    def item_exists(self, rating_key, *, deadline):
        return True


def _publisher(database, *, keep_plex=False, mappings=None, identity=None) -> PlexMarkerPublisher:
    config = ServerConfig(
        id="plex-1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://p",
        auth={},
        output={"plex_config_folder": "/local/plex"},
        path_mappings=mappings or [{"plex_prefix": "/plexmedia", "local_prefix": "/data"}],
        server_identity=identity,
    )
    settings = ServerMarkersSettings(True, None, "2026-09-13T00:00:00+00:00", "keep_plex" if keep_plex else "restore")
    server = MagicMock()
    server.get_server_status.return_value = {"plex_pass": True, "version": "1.43.4"}
    server.get_marker_detection_prefs.return_value = {"intro": "never", "credits": "never"}
    return PlexMarkerPublisher(server, config, settings, db=database)


class TestThePublisherTalksToWhicheverDatabaseItWasGiven:
    """The seam itself: what the publisher decides is what the database half is asked for."""

    def test_the_write_carries_every_decided_value(self):
        database = FakeDatabase()
        publisher = _publisher(database, keep_plex=True)
        publisher.write(
            "7",
            [INTRO, CREDITS],
            previous=[INTRO],
            duration_ms=1_320_000,
            canonical_path="/data/tv/S01E01.mkv",
            own_previous=[CREDITS],
            kept_types=frozenset({T.CREDITS}),
        )
        request = database.writes[0]
        assert request.rating_key == 7
        assert request.wanted == [INTRO, CREDITS]
        assert request.prior == [INTRO]
        assert request.own_prior == [CREDITS]
        assert request.duration_ms == 1_320_000
        assert request.calling_part_ids == (1,)
        assert request.kept_types == frozenset({T.CREDITS})
        assert request.keep_plex is True
        assert request.parts == [PART]

    def test_use_ours_is_forwarded_as_keep_plex_false(self):
        database = FakeDatabase()
        _publisher(database).write("7", [INTRO], previous=None, duration_ms=1, canonical_path="/data/tv/S01E01.mkv")
        assert database.writes[0].keep_plex is False
        assert database.writes[0].prior == []  # previous=None removes nothing

    def test_what_the_write_answered_is_what_the_publisher_reports(self):
        database = FakeDatabase()
        database.result = WriteResult(False, [CREDITS], frozenset({T.INTRO}), frozenset({T.CREDITS}))
        publisher = _publisher(database)
        ours = publisher.write("7", [INTRO], previous=None, duration_ms=1, canonical_path="/data/tv/S01E01.mkv")
        assert ours == [CREDITS]
        assert publisher.last_write_changed is False
        assert publisher.last_kept_types == frozenset({T.INTRO})
        assert publisher.last_replaced_own_types == frozenset({T.CREDITS})

    def test_an_item_the_database_half_doesnt_have_is_not_found(self):
        database = FakeDatabase(exists=False, parts=())
        with pytest.raises(ItemNotFoundError):
            _publisher(database).write("7", [INTRO], previous=None, duration_ms=1, canonical_path="/data/tv/S01E01.mkv")

    def test_the_read_back_asks_with_projected_markers_and_maps_the_files_back(self):
        database = FakeDatabase()
        publisher = _publisher(database)
        recap = Marker(T.RECAP, 0, 1_000, ("chapters",))
        out = publisher.shows_many([("7", [INTRO, recap], frozenset({T.CREDITS}), ("/plexmedia/tv/S01E01.mkv",))])
        assert out == {"7": Shown.OURS}
        ask = database.asks[0]
        assert ask.rating_key == 7
        assert ask.ours == [INTRO]  # Plex shows no recap: projected away before the database is asked
        assert ask.kept_types == frozenset({T.CREDITS})
        assert ask.item_files == ("/plexmedia/tv/S01E01.mkv",)
        assert publisher.live_files("7") == ("/data/tv/S01E01.mkv",)

    def test_the_publish_now_deadline_reaches_the_database_half(self):
        database = FakeDatabase()
        publisher = _publisher(database)
        publisher._db_timeout_s = 8.0
        publisher.shows_many([("7", [INTRO], frozenset(), None)])
        assert database.timeouts == [8.0]

    def test_the_agents_own_report_is_what_the_capability_says(self):
        database = FakeDatabase()
        report = _publisher(database).capability()
        assert report.state is Capability.READY
        assert report.details["db_path"] == "/agent/db"  # the agent's path, not this app's


class TestWhichDatabaseHalfAServerGets:
    """Every cell of "is there an agent": the local file unless one is switched on with an address."""

    @pytest.mark.parametrize(
        ("enabled", "url", "expected"),
        [
            (False, "", LocalPlexDb),
            (False, URL, LocalPlexDb),
            (True, "", LocalPlexDb),
            (True, URL, RemotePlexDb),
        ],
        ids=["off-no-address", "off-with-address", "on-no-address", "on-with-address"],
    )
    def test_the_agent_is_used_only_when_it_is_on_and_has_an_address(self, enabled, url, expected):
        settings = ServerMarkersSettings(
            True, None, "x", "restore", agent_enabled=enabled, agent_url=url, agent_token=TOKEN
        )
        database = plex_database(settings, path_provider=lambda: "/local/db", label="Plex")
        assert isinstance(database, expected)

    def test_the_factory_gives_a_plex_server_with_an_agent_the_remote_half(self):
        from media_preview_generator.markers.publishers.factory import publisher_for

        config = ServerConfig(
            id="plex-1",
            type=ServerType.PLEX,
            name="Plex",
            enabled=True,
            url="http://p",
            auth={},
            output={"plex_config_folder": "/local/plex"},
            markers={
                "enabled": True,
                "library_ids": None,
                "plex": {
                    "db_write_confirmed_at": "2026-09-13T00:00:00+00:00",
                    "on_plex_redetect": "restore",
                    "agent": {"enabled": True, "url": URL, "token": TOKEN},
                },
            },
        )
        publisher = publisher_for(MagicMock(), config)
        assert isinstance(publisher._db, RemotePlexDb)

    def test_the_factory_gives_a_plex_server_without_one_the_local_half(self):
        from media_preview_generator.markers.publishers.factory import publisher_for

        config = ServerConfig(
            id="plex-1",
            type=ServerType.PLEX,
            name="Plex",
            enabled=True,
            url="http://p",
            auth={},
            output={"plex_config_folder": "/local/plex"},
            markers={"enabled": False, "library_ids": None},
        )
        publisher = publisher_for(MagicMock(), config)
        assert isinstance(publisher._db, LocalPlexDb)
        assert publisher.db_path() == "/local/plex/Plug-in Support/Databases/com.plexapp.plugins.library.db"


class TestTheAgentMustBeNextToThisPlex:
    """A pasted address that points at another Plex's agent would write the wrong database."""

    def _publisher_seeing(self, *, agent_identity, server_identity):
        database = FakeDatabase()
        database.file_report = CapabilityReport(
            Capability.READY,
            "",
            {"db_path": "/agent/db", "lock_holder": True, "agent": {"url": URL, "machine_identifier": agent_identity}},
        )
        return _publisher(database, identity=server_identity), database

    @pytest.mark.parametrize(
        ("agent_identity", "server_identity", "ready"),
        [
            ("plex-aaa", "plex-aaa", True),
            ("plex-bbb", "plex-aaa", False),
            ("", "plex-aaa", True),
            ("plex-bbb", None, True),
            ("", None, True),
        ],
        ids=["same-plex", "another-plex", "agent-doesnt-say", "app-doesnt-know", "neither-says"],
    )
    def test_only_a_proven_mismatch_refuses(self, agent_identity, server_identity, ready):
        publisher, _ = self._publisher_seeing(agent_identity=agent_identity, server_identity=server_identity)
        report = publisher.capability()
        assert report.ready is ready
        if not ready:
            assert report.state is Capability.AGENT_UNAVAILABLE
            assert "different Plex server" in report.message

    def test_the_refusal_flags_the_agent_block_so_the_health_card_can_tell_it_apart(self):
        """The Setup Health row can't infer this from "connected yet refused": an agent whose answer simply
        couldn't be decoded is also connected-and-refused, and would get told to change a correct address."""
        publisher, _ = self._publisher_seeing(agent_identity="plex-bbb", server_identity="plex-aaa")
        assert publisher.capability().details["agent"]["wrong_plex"] is True

    def test_an_agent_next_to_the_right_plex_is_never_flagged(self):
        publisher, _ = self._publisher_seeing(agent_identity="plex-aaa", server_identity="plex-aaa")
        assert "wrong_plex" not in publisher.capability().details["agent"]

    def test_the_wrong_plex_is_said_so_even_when_that_plex_is_stopped(self):
        # Otherwise the "same host path" advice below it would tell the user to fix paths, not the address.
        database = FakeDatabase()
        database.file_report = CapabilityReport(
            Capability.UNREACHABLE,
            "Plex doesn't have its database open through this folder right now",
            {"db_path": "/agent/db", "lock_holder": False, "agent": {"url": URL, "machine_identifier": "plex-bbb"}},
        )
        report = _publisher(database, identity="plex-aaa").capability()
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert "different Plex server" in report.message

    def test_a_write_to_another_plexs_agent_never_reaches_the_database(self):
        publisher, database = self._publisher_seeing(agent_identity="plex-bbb", server_identity="plex-aaa")
        with pytest.raises(PublishError) as ei:
            publisher.write("7", [INTRO], previous=[], duration_ms=1, canonical_path="/data/tv/S01E01.mkv")
        assert ei.value.state is Capability.AGENT_UNAVAILABLE
        assert database.writes == []


class TestARefusalNamesTheContainerItIsAbout:
    """Spec §6.3: with an agent the path, the filesystem check and the lock proof are the AGENT's.

    ``LocalPlexDb.file_checks`` runs on whichever side holds the database but words its refusals from this app's
    side, so through an agent they told the user to move the app — the one thing the agent exists to avoid.
    """

    @staticmethod
    def _publisher_told(report: CapabilityReport) -> PlexMarkerPublisher:
        database = FakeDatabase()
        database.file_report = report
        return _publisher(database)

    @staticmethod
    def _with_agent(details: dict) -> dict:
        return {**details, "agent": {"url": URL, "state": "connected", "machine_identifier": ""}}

    NETWORK = {"db_path": "/agent/db", "fs_type": "nfs4"}
    UNKNOWN_FS = {"db_path": "/agent/db", "fs_type": "fuse.weird"}
    UNWRITABLE = {
        "db_path": "/agent/db",
        "fs_type": "ext4",
        "unwritable": ["/agent/db", "/agent/db-wal"],
        "writer_uid": "1000",
        "db_owner_uid": "998",
    }

    @pytest.mark.parametrize(
        ("state", "details", "app_wording"),
        [
            (Capability.NEEDS_LOCAL_DB, NETWORK, "The app must run on the same machine as Plex"),
            (Capability.NEEDS_LOCAL_DB, UNKNOWN_FS, "this app doesn't recognise"),
            (Capability.MISCONFIGURED, UNWRITABLE, "This app (user 1000) can't write"),
        ],
        ids=["network-share", "unrecognised-filesystem", "unwritable-paths"],
    )
    def test_without_an_agent_the_wording_is_still_about_this_app(self, state, details, app_wording):
        report = self._publisher_told(
            CapabilityReport(state, f"Plex's database … {app_wording} …", details)
        ).capability()
        assert report.state is state
        assert app_wording in report.message
        assert "agent" not in report.message

    def test_a_network_share_on_the_agents_side_says_to_mount_it_there(self):
        report = self._publisher_told(
            CapabilityReport(
                Capability.NEEDS_LOCAL_DB,
                "Plex's database is on a network share (nfs4). The app must run on the same machine as Plex to "
                "write markers; Plex stays read-only.",
                self._with_agent(self.NETWORK),
            )
        ).capability()
        assert report.state is Capability.NEEDS_LOCAL_DB
        assert report.message == (
            "Plex's database is on a network share (nfs4) where the Plex marker agent runs. Mount Plex's config "
            "folder into the agent's container from a local disk of the Plex machine; Plex stays read-only."
        )
        # The refusal the agent exists to make unnecessary must not be the advice it gives.
        assert "The app must run on the same machine as Plex" not in report.message

    def test_a_filesystem_the_agent_cant_place_names_the_agent_too(self):
        report = self._publisher_told(
            CapabilityReport(
                Capability.NEEDS_LOCAL_DB,
                "Plex's database is on a filesystem this app doesn't recognise as a local disk (fuse.weird); "
                "Plex stays read-only.",
                self._with_agent(self.UNKNOWN_FS),
            )
        ).capability()
        assert report.message == (
            "Plex's database is on a filesystem the Plex marker agent doesn't recognise as a local disk "
            "(fuse.weird). Mount Plex's config folder into the agent's container from a local disk of the Plex "
            "machine; Plex stays read-only."
        )
        assert "this app doesn't recognise" not in report.message

    def test_paths_the_agent_cant_write_send_the_user_to_the_agents_container(self):
        report = self._publisher_told(
            CapabilityReport(
                Capability.MISCONFIGURED,
                "This app (user 1000) can't write /agent/db, /agent/db-wal. Run it with the user that owns Plex's "
                "database (user 998).",
                self._with_agent(self.UNWRITABLE),
            )
        ).capability()
        assert report.state is Capability.MISCONFIGURED
        assert report.message == (
            "The Plex marker agent (user 1000) can't write /agent/db, /agent/db-wal. Run the agent's container "
            "with the user that owns Plex's database (user 998)."
        )
        assert "This app (user" not in report.message

    def test_the_facts_the_wording_is_rebuilt_from_survive_the_rewrite(self):
        """The Edit tab reads ``details``, so a reworded refusal must keep every fact the original carried."""
        details = self._with_agent(self.UNWRITABLE)
        report = self._publisher_told(CapabilityReport(Capability.MISCONFIGURED, "old", details)).capability()
        assert report.details["unwritable"] == ["/agent/db", "/agent/db-wal"]
        assert report.details["db_path"] == "/agent/db"
        assert report.details["agent"]["url"] == URL

    def test_a_misconfigured_answer_that_is_not_about_writing_is_left_alone(self):
        """Only the two refusals that name a container are reworded; a missing database file already reads right."""
        message = "Plex database not found at /agent/db"
        report = self._publisher_told(
            CapabilityReport(Capability.MISCONFIGURED, message, self._with_agent({"db_path": "/agent/db"}))
        ).capability()
        assert report.message == message

    def test_the_lock_holder_branch_still_says_the_agent_sees(self):
        """The one branch that was already agent-aware must keep its wording (and not be reworded twice)."""
        report = self._publisher_told(
            CapabilityReport(
                Capability.UNREACHABLE,
                "Plex doesn't have its database open through this folder right now",
                self._with_agent({"db_path": "/agent/db", "lock_holder": False, "fs_type": "ext4"}),
            )
        ).capability()
        assert report.state is Capability.NEEDS_LOCAL_DB
        assert "the database file the agent sees at /agent/db" in report.message


class TestWhatAFailedWriteLeavesBehind:
    """A remote write can fail with its answer lost, so what is ours afterwards is unknown."""

    def test_the_local_half_is_atomic_and_the_remote_half_is_not(self):
        local = ServerMarkersSettings(True, None, "x", "restore")
        remote = ServerMarkersSettings(True, None, "x", "restore", agent_enabled=True, agent_url=URL, agent_token=TOKEN)
        assert plex_database(local, path_provider=lambda: "/db", label="Plex").atomic_writes is True
        assert plex_database(remote, path_provider=lambda: "/db", label="Plex").atomic_writes is False

    def test_the_publisher_takes_that_from_the_half_it_was_given(self):
        assert _publisher(FakeDatabase()).atomic_writes is True
        assert _publisher(RemotePlexDb(URL, TOKEN, session=FakeSession())).atomic_writes is False
