"""EmbyServer's Media Preview Bridge calls: URLs, bodies and answer shapes (the plugin contract of Task 4)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from media_preview_generator.servers import EmbyServer, JellyfinServer, ServerConfig, ServerType
from media_preview_generator.servers import emby as emby_module


@pytest.fixture(autouse=True)
def _fresh_catalog_cache():
    emby_module.clear_catalog_cache()
    yield
    emby_module.clear_catalog_cache()


def _server(cls=EmbyServer, stype=ServerType.EMBY):
    return cls(ServerConfig(id="e1", type=stype, name="Emby", enabled=True, url="http://emby:8096",
                            auth={"method": "api_key", "api_key": "k"}, libraries=[]))  # fmt: skip


def _resp(status=200, body=None):
    r = MagicMock(status_code=status)
    r.json.return_value = body
    return r


class TestBridgeInfoShared:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ({"Ok": True, "Version": "1.0.0.0", "Features": ["markers"]}, {"installed": True, "version": "1.0.0.0", "features": ["markers"]}),
            ({"ok": True, "version": "10.11.1.0", "features": ["trickplay", "markers"]}, {"installed": True, "version": "10.11.1.0", "features": ["trickplay", "markers"]}),
        ],
    )  # fmt: skip
    def test_ping_is_read_in_emby_and_jellyfin_casing(self, body, expected):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, body)) as req:
            assert server.get_bridge_info() == expected
        assert req.call_args.args == ("GET", "/MediaPreviewBridge/Ping")

    def test_access_probe_uses_each_vendors_own_unused_id(self):
        emby, jellyfin = _server(), _server(JellyfinServer, ServerType.JELLYFIN)
        with patch.object(
            emby, "_request", return_value=_resp(200, {"Found": False, "Error": "item not found"})
        ) as req:
            assert emby.get_bridge_markers_access() == "ok"
        assert req.call_args.args == ("GET", "/MediaPreviewBridge/Markers/999999999999")
        with patch.object(jellyfin, "_request", return_value=_resp(404, {"error": "item not found"})) as req:
            assert jellyfin.get_bridge_markers_access() == "ok"
        assert req.call_args.args == ("GET", "/MediaPreviewBridge/Markers/ffffffffffffffffffffffffffffffff")

    @pytest.mark.parametrize(
        ("status", "access"), [(401, "unauthorized"), (403, "forbidden"), (404, None), (500, None)]
    )
    def test_emby_access_answers(self, status, access):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(status, None)):
            assert server.get_bridge_markers_access() == access


class TestEmbyMarkers:
    def test_state_is_read_from_the_plugin_answer(self):
        body = {"Id": "42", "Found": True, "Error": None, "IntroStartTicks": 100_000_000, "IntroEndTicks": 400_000_000,
                "CreditsStartTicks": None, "FileSize": 1234, "Stale": False, "Stored": 0}  # fmt: skip
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, body)) as req:
            state = server.get_emby_marker_state("42")
        assert req.call_args.args == ("GET", "/MediaPreviewBridge/Markers/42")
        assert state == {"intro_start_ticks": 100_000_000, "intro_end_ticks": 400_000_000,
                         "credits_start_ticks": None, "file_size": 1234, "stale": False}  # fmt: skip

    def test_fields_emby_leaves_out_read_as_none(self):
        # Emby omits null fields: an item with nothing stored answers only Id, Found, Stale and Stored.
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, {"Id": "42", "Found": True, "Stale": True})):
            state = server.get_emby_marker_state("42")
        assert state == {"intro_start_ticks": None, "intro_end_ticks": None, "credits_start_ticks": None,
                         "file_size": None, "stale": True}  # fmt: skip

    @pytest.mark.parametrize(
        "answer",
        [
            _resp(200, {"Found": False, "Error": "item not found"}),
            _resp(500, {"Found": True, "Error": "couldn't read the item's markers (IOException)"}),
            # The plugin's failure answer keeps HTTP 200 when Emby gives it no response object to set 500 on.
            _resp(200, {"Found": True, "Error": "couldn't read the item's markers (IOException)"}),
            _resp(404, None),
            _resp(200, ["not", "an", "object"]),
        ],
    )
    def test_unknown_answers_read_as_none(self, answer):
        server = _server()
        with patch.object(server, "_request", return_value=answer):
            assert server.get_emby_marker_state("42") is None

    def test_transport_errors_read_as_none(self):
        server = _server()
        with patch.object(server, "_request", side_effect=requests.ConnectionError("down")):
            assert server.get_emby_marker_state("42") is None

    @pytest.mark.parametrize("replace_own", [True, False])
    def test_put_sends_the_pascal_case_body(self, replace_own):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, {})) as req:
            server.put_emby_markers(
                "42", intro_start_ticks=1, intro_end_ticks=2, credits_start_ticks=None, file_size=9,
                replace_own=replace_own,
            )  # fmt: skip
        assert req.call_args.args == ("POST", "/MediaPreviewBridge/Markers/42")
        assert req.call_args.kwargs["json_body"] == {
            "IntroStartTicks": 1, "IntroEndTicks": 2, "CreditsStartTicks": None, "FileSize": 9, "ReplaceOwn": replace_own,
        }  # fmt: skip

    def test_ids_are_quoted_into_the_url(self):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, {})) as req:
            server.delete_emby_markers("../System/Restart")
        assert req.call_args.args == ("DELETE", "/MediaPreviewBridge/Markers/..%2FSystem%2FRestart")


# Emby 4.9 and 4.10 with an API key: item 53's own version, then another version of it (lab, 2026-09-15).
_GROUPED_ITEM = {
    "Id": "53",
    "Chapters": [
        {"StartPositionTicks": 0, "MarkerType": "Chapter", "Name": "Chapter 1"},
        {"StartPositionTicks": 100_000_000, "MarkerType": "IntroStart", "Name": "Intro"},
    ],
    "MediaSources": [
        {"Id": "mediasource_55", "ItemId": "55", "Path": "/media/S01E01 - Extended.webm"},
        {"Id": "mediasource_53", "ItemId": "53", "Path": "/media/S01E01.webm"},
        {"Id": "mediasource_x", "Path": ""},  # no file: left out
        {"Id": "mediasource_y", "Path": "/media/S01E01 - Copy.webm"},  # no ItemId
    ],
}


class TestChaptersAndVersions:
    @pytest.mark.parametrize("user_id", [None, "u1"], ids=["api-key", "per-user"])
    def test_one_read_answers_chapters_and_every_version_with_its_item(self, user_id):
        server = _server()
        server._config.auth["user_id"] = user_id
        answer = {"Items": [_GROUPED_ITEM]} if user_id is None else _GROUPED_ITEM
        with patch.object(server, "_request", return_value=_resp(200, answer)) as req:
            chapters, versions = server.get_chapters_and_versions("53")
        assert req.call_count == 1
        if user_id is None:
            assert req.call_args.args == ("GET", "/Items")
            assert req.call_args.kwargs["params"] == {
                "Ids": "53",
                "Fields": "Chapters,MediaSources,AlternateMediaSources",
            }
        else:
            assert req.call_args.args == ("GET", "/Users/u1/Items/53")
            assert req.call_args.kwargs["params"] == {"Fields": "Chapters,MediaSources,AlternateMediaSources"}
        assert chapters == [
            {"marker_type": "Chapter", "start_ms": 0, "name": "Chapter 1"},
            {"marker_type": "IntroStart", "start_ms": 10_000, "name": "Intro"},
        ]
        assert versions == [
            ("/media/S01E01 - Extended.webm", "55"),
            ("/media/S01E01.webm", "53"),
            ("/media/S01E01 - Copy.webm", None),
        ]

    def test_an_item_that_cant_be_read_is_none(self):
        server = _server()
        with patch.object(server, "_request", side_effect=requests.ConnectionError("down")):
            assert server.get_chapters_and_versions("53") is None


class TestCatalogCache:
    @staticmethod
    def _catalog(listed=True):
        return _resp(200, [{"Name": "Media Preview Bridge for Emby" if listed else "TimeMarkEdit"}])

    def test_the_answer_is_reused_for_an_hour_per_catalog_url(self, monkeypatch):
        now = [1_000.0]
        monkeypatch.setattr(emby_module, "_monotonic", lambda: now[0])
        server, other = _server(), _server()
        other._config = ServerConfig(id="e2", type=ServerType.EMBY, name="Emby 2", enabled=True,
                                     url="http://emby-2:8096/", auth={}, libraries=[])  # fmt: skip
        with patch.object(server, "_request", return_value=self._catalog(True)) as req:
            assert server.bridge_catalog_listed() is True
            now[0] += 3_599
            assert server.bridge_catalog_listed() is True
        assert req.call_count == 1
        with patch.object(other, "_request", return_value=self._catalog(False)) as other_req:
            assert other.bridge_catalog_listed() is False  # another Emby, another catalog URL
        assert other_req.call_count == 1
        with patch.object(server, "_request", return_value=self._catalog(False)) as req:
            now[0] += 2
            assert server.bridge_catalog_listed() is False  # an hour on: read again
        assert req.call_count == 1

    @pytest.mark.parametrize(("age_s", "answer"), [(3_601, True), (86_399, True), (86_401, None)])
    def test_a_failed_read_keeps_the_last_answer_for_a_day(self, monkeypatch, age_s, answer):
        now = [1_000.0]
        monkeypatch.setattr(emby_module, "_monotonic", lambda: now[0])
        server = _server()
        with patch.object(server, "_request", return_value=self._catalog(True)):
            assert server.bridge_catalog_listed() is True
        now[0] += age_s
        with patch.object(server, "_request", side_effect=requests.ConnectionError("catalog down")) as req:
            assert server.bridge_catalog_listed() is answer
        assert req.call_count == 1

    def test_a_failed_read_with_nothing_cached_is_unknown_and_not_cached(self, monkeypatch):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(500, None)):
            assert server.bridge_catalog_listed() is None
        with patch.object(server, "_request", return_value=self._catalog(True)) as req:
            assert server.bridge_catalog_listed() is True
        assert req.call_count == 1


class TestCatalogInstall:
    def test_not_in_the_catalog_says_install_by_hand(self):
        server = _server()
        with patch.object(server, "_request", return_value=_resp(200, [{"Name": "TimeMarkEdit"}])) as req:
            result = server.install_plugin()
        assert req.call_count == 1 and req.call_args.args == ("GET", "/Packages")
        assert result["ok"] is False and result["manual"] is True and "by hand" in result["error"]

    @pytest.mark.parametrize("answer", [_resp(500, None), _resp(200, {"Items": []})])
    def test_unreadable_catalog_is_an_error_not_a_manual_install(self, answer):
        server = _server()
        with patch.object(server, "_request", return_value=answer):
            result = server.install_plugin()
        assert result["ok"] is False and result["manual"] is False
        assert result["error"] == "Couldn't read Emby's plugin catalog"

    def test_listed_package_is_installed_and_emby_restarted(self):
        server = _server()
        answers = [_resp(200, [{"Name": "Media Preview Bridge for Emby"}]), _resp(204, None), _resp(204, None)]
        with patch.object(server, "_request", side_effect=answers) as req:
            result = server.install_plugin()
        assert [c.args for c in req.call_args_list] == [
            ("GET", "/Packages"),
            ("POST", "/Packages/Installed/Media%20Preview%20Bridge%20for%20Emby"),
            ("POST", "/System/Restart"),
        ]
        assert result["ok"] is True and result["manual"] is False
        assert [s["step"] for s in result["steps"]] == ["catalog", "queue_install", "restart"]

    def test_a_failed_install_step_stops_before_the_restart(self):
        server = _server()
        failing = _resp(500, None)
        failing.raise_for_status.side_effect = requests.HTTPError("500")
        answers = [_resp(200, [{"Name": "Media Preview Bridge for Emby"}]), failing]
        with patch.object(server, "_request", side_effect=answers) as req:
            result = server.install_plugin()
        assert req.call_count == 2
        assert result["ok"] is False and result["error"] == "queue_install failed: HTTPError"
