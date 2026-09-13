from unittest.mock import MagicMock, create_autospec

import pytest
import requests

from media_preview_generator.markers.models import Candidate, MarkerType, MediaIds, Source
from media_preview_generator.markers.sources import online
from media_preview_generator.markers.sources.introdb import IntroDbClient
from media_preview_generator.markers.sources.ratelimit import Acquire, SourceLimiter
from media_preview_generator.markers.sources.skipdb import SkipDbClient
from media_preview_generator.markers.sources.theintrodb import TheIntroDbClient

T = MarkerType
RM_S01E01 = MediaIds("episode", tmdb="60625", imdb="tt2861424", tvdb="275274", season=1, episode=1)
TOY_STORY = MediaIds("movie", tmdb="862", imdb="tt0114709")
SECRET = "tidb-SECRET-9f8e7d6c"
# Echo fields every real 200 carries (evidence/online/online_results.json).
TIDB_EP = {"tmdb_id": 60625, "type": "tv", "season": 1, "episode": 1}
IDB_EP = {"imdb_id": "tt2861424", "season": 1, "episode": 1, "intro": None, "recap": None, "outro": None}


def _resp(status=200, body=None, headers=None):
    r = MagicMock(status_code=status, headers=headers or {})
    r.json.return_value = body
    return r


def _limiter(result=Acquire.ALLOWED):
    lim = create_autospec(SourceLimiter, instance=True)
    lim.acquire.return_value = result
    return lim


def _session(*responses, error=None):
    session = MagicMock()
    if error is not None:
        session.get.side_effect = error
    else:
        session.get.side_effect = list(responses)
    return session


class TestTheIntroDb:
    def test_episode_request_params_and_parsing(self):
        session = MagicMock()
        session.get.return_value = _resp(
            200,
            {
                "tmdb_id": 60625,
                "type": "tv",
                "season": 1,
                "episode": 1,
                "intro": [{"start_ms": 127894, "end_ms": 156824}],
                "recap": [],
                "preview": [],
                "credits": [{"start_ms": 1298000, "end_ms": None}],
            },
            {"x-usagelimit-remaining": "417"},
        )
        lim = _limiter()
        result = TheIntroDbClient("", limiter=lim, session=session).lookup(RM_S01E01, duration_ms=1_321_472, priority=2)
        args, kwargs = session.get.call_args
        assert args[0] == "https://api.theintrodb.org/v3/media"
        assert kwargs["params"] == {"tmdb_id": "60625", "season": 1, "episode": 1, "duration_ms": 1_321_472}
        assert "Authorization" not in kwargs["headers"] and kwargs["timeout"] == 15
        lim.acquire.assert_called_once()
        assert lim.acquire.call_args.kwargs["priority"] == 2
        lim.record.assert_called_once_with(200, {"x-usagelimit-remaining": "417"})
        assert result.status == "ok"
        assert result.candidates == (
            Candidate(T.INTRO, 127_894, 156_824, Source.THEINTRODB),
            Candidate(T.CREDITS, 1_298_000, None, Source.THEINTRODB),
        )

    def test_key_sent_as_bearer_and_null_start_is_zero(self):
        session = MagicMock()
        session.get.return_value = _resp(
            200, {**TIDB_EP, "intro": [{"start_ms": None, "end_ms": 30000}], "credits": []}
        )
        result = TheIntroDbClient("k-123", limiter=_limiter(), session=session).lookup(
            RM_S01E01, duration_ms=1_318_496, priority=3
        )
        assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer k-123"
        assert result.candidates == (Candidate(T.INTRO, 0, 30_000, Source.THEINTRODB),)

    def test_blank_key_is_not_sent(self):
        session = _session(_resp(200, {}))
        result = TheIntroDbClient("   ", limiter=_limiter(), session=session).lookup(
            RM_S01E01, duration_ms=1, priority=2
        )
        assert "Authorization" not in session.get.call_args.kwargs["headers"]
        assert result == online.LookupResult("unavailable", detail="TheIntroDB returned an unexpected response")

    @pytest.mark.parametrize(
        "key",
        [
            SECRET + "\u200b",  # zero-width space pasted from a web page
            "\u201c" + SECRET + "\u201d",  # curly quotes
            "tidb-S\u00e9CRET",  # Latin-1 but not ASCII
            "tidb SECRET",
            "tidb\tSECRET",
            "tidb-SECRET\n9f",
        ],
    )
    def test_key_with_invalid_characters_is_refused_before_any_request(self, key):
        session = MagicMock()
        lim = _limiter()
        result = TheIntroDbClient(key, limiter=lim, session=session).lookup(
            RM_S01E01, duration_ms=1_321_000, priority=2
        )
        assert result == online.LookupResult("unavailable", detail="TheIntroDB API key contains invalid characters")
        session.get.assert_not_called()
        lim.acquire.assert_not_called()

    def test_surrounding_whitespace_is_stripped_from_a_valid_key(self):
        session = _session(_resp(404, {"error": "media not found"}))
        TheIntroDbClient("  k-123\n", limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=1, priority=2)
        assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer k-123"

    @pytest.mark.parametrize(
        ("ids", "param"),
        [
            (MediaIds("episode", tvdb="275274", season=1, episode=1), {"tvdb_id": "275274"}),
            (MediaIds("episode", imdb="tt2861424", season=1, episode=1), {"imdb_id": "tt2861424"}),
            (MediaIds("episode", imdb="tt2861424", tvdb="275274", season=1, episode=1), {"tvdb_id": "275274"}),
            (TOY_STORY, {"tmdb_id": "862"}),
            (MediaIds("movie", imdb="tt0114709"), {"imdb_id": "tt0114709"}),
        ],
    )
    def test_id_preference(self, ids, param):
        session = MagicMock()
        session.get.return_value = _resp(404, {"error": "not found"})
        result = TheIntroDbClient(limiter=_limiter(), session=session).lookup(ids, duration_ms=1000, priority=2)
        params = session.get.call_args.kwargs["params"]
        assert {k: v for k, v in params.items() if k.endswith("_id")} == param
        assert ("season" in params) is ids.is_episode
        assert ("episode" in params) is ids.is_episode
        assert params["duration_ms"] == 1000
        assert result.status == "no_data"

    @pytest.mark.parametrize(
        "ids",
        [
            MediaIds("episode", season=1, episode=1),
            MediaIds("episode", tmdb="1"),
            MediaIds("episode", tmdb="1", season=1),
            MediaIds("episode", tmdb="1", episode=1),
            MediaIds("episode", tmdb="1", season=0, episode=1),
            MediaIds("episode", tmdb="1", season=1, episode=0),
            MediaIds("episode", imdb="tt12", season=1, episode=1),
            MediaIds("movie", tvdb="275274"),
            MediaIds("unknown", tmdb="862", imdb="tt0114709"),
            MediaIds(),
        ],
    )
    def test_not_applicable_without_ids_makes_no_request(self, ids):
        session = MagicMock()
        lim = _limiter()
        assert (
            TheIntroDbClient(limiter=lim, session=session).lookup(ids, duration_ms=1, priority=2).status
            == "not_applicable"
        )
        session.get.assert_not_called()
        lim.acquire.assert_not_called()

    @pytest.mark.parametrize("duration_ms", [None, 0, -5])
    def test_not_applicable_without_file_duration(self, duration_ms):
        session = MagicMock()
        lim = _limiter()
        result = TheIntroDbClient(limiter=lim, session=session).lookup(RM_S01E01, duration_ms=duration_ms, priority=2)
        assert result.status == "not_applicable" and "duration" in result.detail
        session.get.assert_not_called()
        lim.acquire.assert_not_called()

    @pytest.mark.parametrize(
        ("status", "detail"), [(401, "API key"), (403, "API key"), (500, "HTTP 500"), (429, "HTTP 429")]
    )
    def test_error_statuses_are_unavailable(self, status, detail):
        session = MagicMock()
        session.get.return_value = _resp(status, {})
        result = TheIntroDbClient("k", limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=1, priority=2)
        assert result.status == "unavailable" and detail in result.detail
        assert "k" not in result.detail.replace("key", "")  # never echo the key

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_refusal_without_a_key_says_a_key_is_needed(self, status):
        session = _session(_resp(status, {}))
        result = TheIntroDbClient(limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=1, priority=2)
        assert result.status == "unavailable"
        assert "requires an API key" in result.detail and str(status) in result.detail

    def test_network_error_records_failure(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("dns")
        lim = _limiter()
        result = TheIntroDbClient(limiter=lim, session=session).lookup(RM_S01E01, duration_ms=1, priority=2)
        assert result.status == "unavailable"
        lim.record.assert_called_once_with(None, None)

    @pytest.mark.parametrize("acq", [Acquire.BLOCKED, Acquire.BUDGET_EXHAUSTED, Acquire.CANCELLED])
    def test_limiter_refusal_is_unavailable_without_request(self, acq):
        session = MagicMock()
        result = TheIntroDbClient(limiter=_limiter(acq), session=session).lookup(RM_S01E01, duration_ms=1, priority=3)
        assert result.status == "unavailable" and acq.value in result.detail
        session.get.assert_not_called()

    def test_200_with_no_segments_is_no_data(self):
        session = MagicMock()
        session.get.return_value = _resp(200, {**TIDB_EP, "intro": [], "credits": [], "recap": [], "preview": []})
        assert (
            TheIntroDbClient(limiter=_limiter(), session=session).lookup(RM_S01E01, duration_ms=1, priority=2).status
            == "no_data"
        )

    def test_all_four_segment_types_in_order_with_several_per_type(self):
        body = {
            **TIDB_EP,
            "preview": [{"start_ms": 1_300_000, "end_ms": 1_320_000}],
            "credits": [{"start_ms": 1_250_000, "end_ms": 1_290_000}, {"start_ms": 1_295_000, "end_ms": None}],
            "recap": [{"start_ms": 0, "end_ms": 20_000, "confidence": 0.4}],
            "intro": [{"start_ms": 20_000, "end_ms": 50_000}],
        }
        result = TheIntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=1_321_000, priority=2
        )
        assert result.candidates == (
            Candidate(T.INTRO, 20_000, 50_000, Source.THEINTRODB),
            Candidate(T.RECAP, 0, 20_000, Source.THEINTRODB, confidence=0.4),
            Candidate(T.CREDITS, 1_250_000, 1_290_000, Source.THEINTRODB),
            Candidate(T.CREDITS, 1_295_000, None, Source.THEINTRODB),
            Candidate(T.PREVIEW, 1_300_000, 1_320_000, Source.THEINTRODB),
        )

    @pytest.mark.parametrize(
        "segment",
        [
            {"start_ms": None, "end_ms": None},  # "whole file" is never a skip
            {},
            {"start_ms": 50_000, "end_ms": 20_000},  # end before start
            {"start_ms": 20_000, "end_ms": 20_000},  # zero length
            {"start_ms": -1, "end_ms": 20_000},
            {"start_ms": 0, "end_ms": -20_000},
            {"start_ms": "12", "end_ms": 20_000},
            {"start_ms": True, "end_ms": 20_000},
            {"start_ms": 0, "end_ms": float("nan")},
            {"start_ms": float("inf"), "end_ms": None},
            "not-a-dict",
            None,
        ],
    )
    def test_malformed_segments_are_dropped(self, segment):
        body = {**TIDB_EP, "intro": [segment]}
        result = TheIntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=1_321_000, priority=2
        )
        assert result == online.LookupResult("no_data")

    def test_non_list_segment_collection_is_ignored(self):
        body = {
            **TIDB_EP,
            "intro": {"start_ms": 1, "end_ms": 5000},
            "credits": [{"start_ms": 1_300_000, "end_ms": None}],
        }
        result = TheIntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=1_321_000, priority=2
        )
        assert result.candidates == (Candidate(T.CREDITS, 1_300_000, None, Source.THEINTRODB),)

    def test_float_milliseconds_are_rounded_to_integers(self):
        body = {**TIDB_EP, "intro": [{"start_ms": 1000.6, "end_ms": 30000.4}]}
        result = TheIntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=1_321_000, priority=2
        )
        assert result.candidates == (Candidate(T.INTRO, 1001, 30000, Source.THEINTRODB),)
        assert all(isinstance(v, int) for v in (result.candidates[0].start_ms, result.candidates[0].end_ms))

    @pytest.mark.parametrize(
        ("ids", "body"),
        [
            (RM_S01E01, {**TIDB_EP, "type": "movie", "intro": [{"start_ms": 0, "end_ms": 30000}]}),
            (TOY_STORY, {"tmdb_id": 862, "type": "tv", "intro": [{"start_ms": 0, "end_ms": 30000}]}),
            (RM_S01E01, {**TIDB_EP, "season": 2, "intro": [{"start_ms": 0, "end_ms": 30000}]}),
            (RM_S01E01, {**TIDB_EP, "episode": 7, "intro": [{"start_ms": 0, "end_ms": 30000}]}),
            (RM_S01E01, {**TIDB_EP, "tmdb_id": 1399, "intro": [{"start_ms": 0, "end_ms": 30000}]}),
        ],
    )
    def test_answer_for_a_different_item_is_not_used(self, ids, body):
        result = TheIntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            ids, duration_ms=1_321_000, priority=2
        )
        assert result.status == "unavailable" and result.candidates == ()
        assert "different" in result.detail

    def test_matching_echo_fields_are_accepted_for_a_movie(self):
        body = {"tmdb_id": 862, "type": "movie", "season": None, "credits": [{"start_ms": 4_700_000, "end_ms": None}]}
        result = TheIntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            TOY_STORY, duration_ms=4_866_050, priority=2
        )
        assert result.candidates == (Candidate(T.CREDITS, 4_700_000, None, Source.THEINTRODB),)

    def test_tmdb_echo_is_not_compared_when_queried_by_another_id(self):
        body = {**TIDB_EP, "tmdb_id": 60625, "intro": [{"start_ms": 0, "end_ms": 30000}]}
        ids = MediaIds("episode", tvdb="275274", season=1, episode=1)
        result = TheIntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            ids, duration_ms=1_321_000, priority=2
        )
        assert result.status == "ok"

    @pytest.mark.parametrize(
        ("ids", "body"),
        [
            (RM_S01E01, {}),
            (RM_S01E01, {"error": "x"}),
            (TOY_STORY, {}),
            (TOY_STORY, {"error": "x", "credits": [{"start_ms": 4_700_000, "end_ms": None}]}),
            (RM_S01E01, {"tmdb_id": 60625, "season": 1, "episode": 1, "intro": [{"start_ms": 0, "end_ms": 30000}]}),
            (RM_S01E01, {"tmdb_id": 60625, "type": "tv", "episode": 1, "intro": [{"start_ms": 0, "end_ms": 30000}]}),
            (RM_S01E01, {"tmdb_id": 60625, "type": "tv", "season": 1, "intro": [{"start_ms": 0, "end_ms": 30000}]}),
            (RM_S01E01, {**TIDB_EP, "season": None, "intro": [{"start_ms": 0, "end_ms": 30000}]}),
        ],
    )
    def test_200_without_the_echo_fields_is_unexpected_not_no_data(self, ids, body):
        result = TheIntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            ids, duration_ms=1_321_000, priority=2
        )
        assert result == online.LookupResult("unavailable", detail="TheIntroDB returned an unexpected response")

    def test_secret_key_never_reaches_logs_details_or_params(self, loguru_caplog):
        outcomes = [
            _session(_resp(200, {**TIDB_EP, "intro": [{"start_ms": 0, "end_ms": 30000}]})),
            _session(_resp(200, {"type": "movie"})),
            _session(_resp(401, {"error": f"bad key {SECRET}"})),
            _session(_resp(403, {})),
            _session(_resp(500, {})),
            _session(_resp(404, {})),
            _session(error=requests.ConnectionError(f"boom Authorization: Bearer {SECRET}")),
            _session(error=UnicodeEncodeError("latin-1", f"Bearer {SECRET}", 7, 8, f"bad {SECRET}")),
            _session(error=ValueError(f"header Authorization: Bearer {SECRET}")),
        ]
        bad_json = _resp(200)
        bad_json.json.side_effect = ValueError(SECRET)
        outcomes.append(_session(bad_json))
        for session in outcomes:
            client = TheIntroDbClient(SECRET, limiter=_limiter(), session=session)
            result = client.lookup(RM_S01E01, duration_ms=1_321_000, priority=2)
            assert SECRET not in repr(result)
            assert SECRET not in repr(client)
            assert SECRET not in str(session.get.call_args.kwargs["params"])
            assert session.get.call_args.kwargs["headers"]["Authorization"] == f"Bearer {SECRET}"
        bad_key = SECRET + "\u200b"
        session = MagicMock()
        client = TheIntroDbClient(bad_key, limiter=_limiter(), session=session)
        result = client.lookup(RM_S01E01, duration_ms=1_321_000, priority=2)
        assert result.status == "unavailable"
        assert SECRET not in repr(result) and SECRET not in repr(client)
        assert SECRET not in str(vars(client))  # an unusable key isn't kept where a later change could send it
        session.get.assert_not_called()
        assert loguru_caplog.records, "lookups should log at debug level"
        assert all(SECRET not in r.getMessage() for r in loguru_caplog.records)
        assert SECRET not in loguru_caplog.text


class TestIntroDb:
    def test_request_and_parsing(self):
        session = MagicMock()
        session.get.return_value = _resp(
            200,
            {
                "imdb_id": "tt2861424",
                "season": 1,
                "episode": 1,
                "intro": {"start_ms": 128000, "end_ms": 160000, "confidence": 1},
                "recap": None,
                "outro": {"start_ms": 1295000, "end_ms": 1321000, "confidence": 0.5},
            },
        )
        lim = _limiter()
        result = IntroDbClient(limiter=lim, session=session).lookup(RM_S01E01, duration_ms=1_321_472, priority=2)
        assert session.get.call_args.args[0] == "https://api.introdb.app/segments"
        assert session.get.call_args.kwargs["params"] == {"imdb_id": "tt2861424", "season": 1, "episode": 1}
        assert session.get.call_args.kwargs["timeout"] == 15
        assert "Authorization" not in session.get.call_args.kwargs["headers"]
        assert lim.acquire.call_args.kwargs["priority"] == 2
        assert result.status == "ok"
        assert result.candidates == (
            Candidate(T.INTRO, 128_000, 160_000, Source.INTRODB, confidence=1.0),
            Candidate(T.CREDITS, 1_295_000, 1_321_000, Source.INTRODB, confidence=0.5),
        )

    @pytest.mark.parametrize(
        "ids",
        [
            TOY_STORY,
            MediaIds("episode", tmdb="60625", season=1, episode=1),
            MediaIds("episode", imdb="tt2861424", season=1),
            MediaIds("episode", imdb="tt2861424", episode=1),
            MediaIds("episode", imdb="tt2861424", season=0, episode=1),
            MediaIds("episode", imdb="tt2861424", season=1, episode=0),
            MediaIds("episode", imdb="2861424", season=1, episode=1),
            MediaIds("unknown", imdb="tt2861424", season=1, episode=1),
        ],
    )
    def test_movies_and_missing_imdb_not_applicable(self, ids):
        session = MagicMock()
        lim = _limiter()
        assert (
            IntroDbClient(limiter=lim, session=session).lookup(ids, duration_ms=1, priority=2).status
            == "not_applicable"
        )
        session.get.assert_not_called()
        lim.acquire.assert_not_called()

    def test_recap_maps_to_recap_and_missing_confidence_defaults_to_one(self):
        body = {**IDB_EP, "recap": {"start_ms": 0, "end_ms": 25_000, "confidence": None}}
        result = IntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=None, priority=2
        )
        assert result.candidates == (Candidate(T.RECAP, 0, 25_000, Source.INTRODB, confidence=1.0),)

    @pytest.mark.parametrize(("raw", "expected"), [(1.7, 1.0), (-0.2, 0.0), (0.0, 0.0), ("high", 1.0), (True, 1.0)])
    def test_confidence_is_clamped_to_zero_one(self, raw, expected):
        body = {**IDB_EP, "intro": {"start_ms": 10_000, "end_ms": 40_000, "confidence": raw}}
        result = IntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=None, priority=2
        )
        assert result.candidates[0].confidence == expected

    @pytest.mark.parametrize(
        "segment",
        [
            {"start_ms": 10_000},  # no end: IntroDB never documents "to the end of the file"
            {"start_ms": 10_000, "end_ms": None},
            {"end_ms": 40_000},
            {"start_ms": 40_000, "end_ms": 10_000},
            {"start_ms": 10_000, "end_ms": 10_000},
            {"start_sec": 10, "end_sec": 40},  # seconds only: not the documented shape
            [10_000, 40_000],
        ],
    )
    def test_incomplete_segments_are_dropped(self, segment):
        result = IntroDbClient(limiter=_limiter(), session=_session(_resp(200, {**IDB_EP, "intro": segment}))).lookup(
            RM_S01E01, duration_ms=None, priority=2
        )
        assert result == online.LookupResult("no_data")

    @pytest.mark.parametrize(
        "echo",
        [{"imdb_id": "tt0000001"}, {"season": 2}, {"episode": 9}],
    )
    def test_answer_for_a_different_episode_is_not_used(self, echo):
        body = {"imdb_id": "tt2861424", "season": 1, "episode": 1, "intro": {"start_ms": 0, "end_ms": 30_000}}
        body.update(echo)
        result = IntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=None, priority=2
        )
        assert result.status == "unavailable" and "different" in result.detail

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"error": "x"},
            {"season": 1, "episode": 1, "intro": {"start_ms": 0, "end_ms": 30_000}, "recap": None, "outro": None},
            {"imdb_id": "tt2861424", "episode": 1, "intro": {"start_ms": 0, "end_ms": 30_000}},
            {"imdb_id": "tt2861424", "season": 1, "intro": {"start_ms": 0, "end_ms": 30_000}},
            {"imdb_id": "tt2861424", "season": 1, "episode": 1},
            {"imdb_id": "tt2861424", "season": 1, "episode": 1, "error": "x"},
        ],
    )
    def test_200_without_the_documented_fields_is_unexpected_not_no_data(self, body):
        result = IntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=None, priority=2
        )
        assert result == online.LookupResult("unavailable", detail="IntroDB returned an unexpected response")

    def test_one_segment_key_is_enough_for_a_valid_answer(self):
        body = {"imdb_id": "tt2861424", "season": 1, "episode": 1, "outro": None}
        result = IntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=None, priority=2
        )
        assert result == online.LookupResult("no_data")

    def test_imdb_echo_is_case_insensitive(self):
        body = {"imdb_id": "TT2861424", "season": 1, "episode": 1, "intro": {"start_ms": 0, "end_ms": 30_000}}
        result = IntroDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=None, priority=2
        )
        assert result.status == "ok"


class TestSkipDb:
    def test_episode_request_params_and_match_filter(self):
        session = MagicMock()
        session.get.return_value = _resp(
            200,
            {
                "segments": {
                    "intro": {"start_ms": 129000, "end_ms": 157800, "match": "exact", "confidence": 0.93},
                    "recap": {"start_ms": 0, "end_ms": 20000, "match": "out-of-range", "confidence": 0.2},
                    "outro": {"start_ms": 1296000, "end_ms": 1320000, "match": "shifted", "confidence": 0.8},
                    "preview": None,
                }
            },
        )
        lim = _limiter()
        result = SkipDbClient(limiter=lim, session=session).lookup(RM_S01E01, duration_ms=1_321_472, priority=2)
        assert session.get.call_args.args[0] == "https://api.skipdb.tv/api/segments"
        assert session.get.call_args.kwargs["params"] == {
            "imdb_id": "tt2861424",
            "season": 1,
            "episode": 1,
            "duration": 1321.472,
            "adjust": "conservative",
        }
        assert session.get.call_args.kwargs["timeout"] == 15
        assert lim.acquire.call_args.kwargs["priority"] == 2
        assert result.candidates == (
            Candidate(T.INTRO, 129_000, 157_800, Source.SKIPDB, confidence=0.93),
            Candidate(T.CREDITS, 1_296_000, 1_320_000, Source.SKIPDB, confidence=0.8),
        )

    def test_movie_has_no_season_params(self):
        session = MagicMock()
        session.get.return_value = _resp(
            200, {"segments": {"intro": None, "recap": None, "outro": None, "preview": None}}
        )
        result = SkipDbClient(limiter=_limiter(), session=session).lookup(TOY_STORY, duration_ms=4_866_050, priority=2)
        assert session.get.call_args.kwargs["params"] == {
            "imdb_id": "tt0114709",
            "duration": 4866.05,
            "adjust": "conservative",
        }
        assert result.status == "no_data"

    @pytest.mark.parametrize("duration_ms", [None, 0, -1])
    def test_requires_duration(self, duration_ms):
        session = MagicMock()
        lim = _limiter()
        assert (
            SkipDbClient(limiter=lim, session=session).lookup(RM_S01E01, duration_ms=duration_ms, priority=2).status
            == "not_applicable"
        )
        session.get.assert_not_called()
        lim.acquire.assert_not_called()

    @pytest.mark.parametrize(
        "ids",
        [
            MediaIds("episode", tmdb="60625", tvdb="275274", season=1, episode=1),
            MediaIds("episode", imdb="tt2861424", season=1),
            MediaIds("episode", imdb="tt2861424", episode=1),
            MediaIds("episode", imdb="tt2861424", season=-1, episode=1),
            MediaIds("episode", imdb="tt28", season=1, episode=1),
            MediaIds("unknown", imdb="tt0114709"),
            MediaIds(),
        ],
    )
    def test_not_applicable_ids(self, ids):
        session = MagicMock()
        lim = _limiter()
        assert (
            SkipDbClient(limiter=lim, session=session).lookup(ids, duration_ms=1_000_000, priority=2).status
            == "not_applicable"
        )
        session.get.assert_not_called()
        lim.acquire.assert_not_called()

    def test_specials_season_zero_is_queried(self):
        session = _session(_resp(404, {}))
        ids = MediaIds("episode", imdb="tt2861424", season=0, episode=0)
        result = SkipDbClient(limiter=_limiter(), session=session).lookup(ids, duration_ms=600_000, priority=2)
        assert session.get.call_args.kwargs["params"]["season"] == 0
        assert session.get.call_args.kwargs["params"]["episode"] == 0
        assert result.status == "no_data"

    @pytest.mark.parametrize(("duration_ms", "seconds"), [(1_000, 1.0), (1_234_567, 1234.567), (1_320_001, 1320.001)])
    def test_duration_is_sent_in_seconds_to_the_millisecond(self, duration_ms, seconds):
        session = _session(_resp(404, {}))
        SkipDbClient(limiter=_limiter(), session=session).lookup(TOY_STORY, duration_ms=duration_ms, priority=2)
        assert session.get.call_args.kwargs["params"]["duration"] == seconds

    @pytest.mark.parametrize(
        "segment",
        [
            {"start_ms": 61000, "end_ms": 91000, "match": "agnostic", "confidence": 0.9},
            {"start_ms": 61000, "end_ms": 91000, "match": "out-of-range", "confidence": 0.9},
            {"start_ms": 61000, "end_ms": 91000, "confidence": 0.9},
            {"start_ms": 61000, "end_ms": 91000, "match": "EXACT", "confidence": 0.9},
            {"start_ms": 0, "end_ms": 0, "match": "exact"},  # confirmed "no segment of this type"
            {"start_ms": 91000, "end_ms": 61000, "match": "exact"},
            {"start_ms": 61000, "end_ms": None, "match": "exact"},
            {"start_ms": None, "end_ms": 91000, "match": "exact"},
            {"start_ms": -5, "end_ms": 91000, "match": "shifted"},
        ],
    )
    def test_unconfirmed_or_malformed_segments_are_dropped(self, segment):
        body = {"segments": {"intro": segment, "recap": None, "outro": None, "preview": None}}
        result = SkipDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=1_321_472, priority=2
        )
        assert result == online.LookupResult("no_data")

    def test_recap_and_preview_types(self):
        body = {
            "segments": {
                "intro": None,
                "recap": {"start_ms": 0, "end_ms": 21000, "match": "exact"},
                "outro": None,
                "preview": {"start_ms": 1_300_000, "end_ms": 1_320_000, "match": "shifted", "confidence": 0.7},
            }
        }
        result = SkipDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=1_321_472, priority=2
        )
        assert result.candidates == (
            Candidate(T.RECAP, 0, 21_000, Source.SKIPDB, confidence=1.0),
            Candidate(T.PREVIEW, 1_300_000, 1_320_000, Source.SKIPDB, confidence=0.7),
        )

    @pytest.mark.parametrize("body", [{}, {"segments": None}, {"segments": []}, {"error": "x"}])
    def test_unexpected_shape_is_unavailable_not_no_data(self, body):
        result = SkipDbClient(limiter=_limiter(), session=_session(_resp(200, body))).lookup(
            RM_S01E01, duration_ms=1_321_472, priority=2
        )
        assert result.status == "unavailable"


def _lookup(client_cls, lim, session):
    return client_cls(limiter=lim, session=session).lookup(RM_S01E01, duration_ms=1_321_472, priority=2)


ALL_CLIENTS = pytest.mark.parametrize(
    ("client_cls", "label"),
    [(TheIntroDbClient, "TheIntroDB"), (IntroDbClient, "IntroDB"), (SkipDbClient, "SkipDB")],
    ids=["theintrodb", "introdb", "skipdb"],
)


class TestSharedBehaviour:
    @ALL_CLIENTS
    @pytest.mark.parametrize("acq", [Acquire.BLOCKED, Acquire.BUDGET_EXHAUSTED, Acquire.CANCELLED])
    def test_limiter_refusal_sends_nothing(self, client_cls, label, acq):
        session = MagicMock()
        lim = _limiter(acq)
        cancel = MagicMock(return_value=False)
        result = client_cls(limiter=lim, session=session).lookup(
            RM_S01E01, duration_ms=1_321_472, priority=3, cancel_check=cancel
        )
        assert result == online.LookupResult("unavailable", detail=f"{label} {acq.value}")
        assert lim.acquire.call_args.kwargs == {"priority": 3, "cancel_check": cancel}
        session.get.assert_not_called()
        lim.record.assert_not_called()

    @ALL_CLIENTS
    @pytest.mark.parametrize("error", [requests.ConnectionError("x"), requests.Timeout("x"), requests.HTTPError()])
    def test_network_errors_are_recorded_as_failures(self, client_cls, label, error):
        lim = _limiter()
        result = _lookup(client_cls, lim, _session(error=error))
        assert result.status == "unavailable"
        assert result.detail == f"{label} network error: {type(error).__name__}"
        lim.record.assert_called_once_with(None, None)

    @ALL_CLIENTS
    @pytest.mark.parametrize("body", [{"error": "media not found"}, {}])
    def test_json_404_is_no_data(self, client_cls, label, body):
        lim = _limiter()
        headers = {"x-ratelimit-remaining": "5"}
        assert _lookup(client_cls, lim, _session(_resp(404, body, headers))) == online.LookupResult("no_data")
        lim.record.assert_called_once_with(404, headers)

    @ALL_CLIENTS
    @pytest.mark.parametrize("body", [None, [], "<html>Not Found</html>", "invalid-json"])
    def test_404_without_a_json_object_is_unavailable(self, client_cls, label, body):
        lim = _limiter()
        resp = _resp(404, body)
        if body == "invalid-json":
            resp.json.side_effect = requests.JSONDecodeError("bad", "doc", 0)
        assert _lookup(client_cls, lim, _session(resp)) == online.LookupResult(
            "unavailable", detail=f"{label} HTTP 404"
        )
        lim.record.assert_called_once_with(404, {})

    @ALL_CLIENTS
    @pytest.mark.parametrize(
        "error",
        [
            UnicodeEncodeError("latin-1", "Bearer k\u200b", 8, 9, "ordinal not in range(256)"),
            ValueError("bad header"),
            requests.exceptions.InvalidHeader("bad header"),
            requests.exceptions.InvalidURL("bad url"),
        ],
    )
    def test_request_that_cannot_be_built_refunds_the_slot_and_is_not_a_failure(self, client_cls, label, error):
        lim = _limiter()
        result = _lookup(client_cls, lim, _session(error=error))
        assert result == online.LookupResult(
            "unavailable", detail=f"{label} request could not be sent: {type(error).__name__}"
        )
        lim.refund.assert_called_once_with()
        lim.record.assert_not_called()

    @ALL_CLIENTS
    def test_refund_reaches_a_real_limiter(self, client_cls, label):
        lim = SourceLimiter(label.lower(), min_interval_s=0.0, utc_day=lambda: "2026-09-14")
        error = UnicodeEncodeError("latin-1", "Bearer k\u200b", 8, 9, "ordinal not in range(256)")
        _lookup(client_cls, lim, _session(error=error))
        assert lim.usage()["used"] == 0

    @ALL_CLIENTS
    def test_429_through_a_real_limiter_blocks_the_next_lookup(self, client_cls, label):
        clock = {"t": 1000.0}
        lim = SourceLimiter(
            label.lower(),
            min_interval_s=0.0,
            clock=lambda: clock["t"],
            sleep=lambda s: clock.update(t=clock["t"] + s),
            utc_day=lambda: "2026-09-14",
        )
        session = MagicMock()
        session.get.return_value = _resp(429, {}, {"Retry-After": "300"})
        first = _lookup(client_cls, lim, session)
        second = _lookup(client_cls, lim, session)
        assert first == online.LookupResult("unavailable", detail=f"{label} HTTP 429")
        assert second == online.LookupResult("unavailable", detail=f"{label} blocked")
        session.get.assert_called_once()
        assert lim.usage()["blocked_until_s"] == pytest.approx(300.0)

    @pytest.mark.parametrize(("client_cls", "label"), [(IntroDbClient, "IntroDB"), (SkipDbClient, "SkipDB")])
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_statuses_are_plain_http_errors_for_keyless_sources(self, client_cls, label, status):
        lim = _limiter()
        result = _lookup(client_cls, lim, _session(_resp(status, {})))
        assert result == online.LookupResult("unavailable", detail=f"{label} HTTP {status}")
        lim.record.assert_called_once_with(status, {})

    @ALL_CLIENTS
    @pytest.mark.parametrize("status", [400, 429, 500, 502, 503, 201, 204])
    def test_other_statuses_are_unavailable_and_recorded(self, client_cls, label, status):
        lim = _limiter()
        result = _lookup(client_cls, lim, _session(_resp(status, {})))
        assert result == online.LookupResult("unavailable", detail=f"{label} HTTP {status}")
        lim.record.assert_called_once_with(status, {})

    @ALL_CLIENTS
    def test_invalid_json_is_unavailable(self, client_cls, label):
        resp = _resp(200)
        resp.json.side_effect = requests.JSONDecodeError("bad", "doc", 0)
        result = _lookup(client_cls, _limiter(), _session(resp))
        assert result == online.LookupResult("unavailable", detail=f"{label} returned invalid JSON")

    @ALL_CLIENTS
    @pytest.mark.parametrize("body", [None, [], "text", 5])
    def test_non_object_json_is_unavailable(self, client_cls, label, body):
        result = _lookup(client_cls, _limiter(), _session(_resp(200, body)))
        assert result == online.LookupResult("unavailable", detail=f"{label} returned an unexpected response")

    @ALL_CLIENTS
    def test_accept_header_and_timeout(self, client_cls, label):
        session = _session(_resp(404, {}))
        _lookup(client_cls, _limiter(), session)
        assert session.get.call_args.kwargs["headers"]["Accept"] == "application/json"
        assert session.get.call_args.kwargs["timeout"] == online.REQUEST_TIMEOUT_S == 15

    @ALL_CLIENTS
    def test_default_limiter_and_session_are_the_shared_ones(self, client_cls, label):
        from media_preview_generator.markers.sources.ratelimit import get_limiter

        client = client_cls()
        assert client._limiter is get_limiter(label.lower())
        assert client._session is online.http_session()


class TestHttpSession:
    def test_shared_session_identifies_the_app(self):
        from media_preview_generator import __version__

        session = online.http_session()
        assert session is online.http_session()
        assert session.headers["User-Agent"].startswith(f"MediaPreviewGenerator/{__version__} ")
        assert "Authorization" not in session.headers

    def test_lookup_result_defaults(self):
        assert online.LookupResult("no_data") == online.LookupResult("no_data", (), "")
