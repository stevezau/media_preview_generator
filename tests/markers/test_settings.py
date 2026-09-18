import pytest

from media_preview_generator.markers import settings as ms

# Characters a pasted key can carry that the TheIntroDB client can't send (spelled out: they are invisible).
ZERO_WIDTH_SPACE, NO_BREAK_SPACE = chr(0x200B), chr(0xA0)
LEFT_QUOTE, RIGHT_QUOTE, E_ACUTE = chr(0x201C), chr(0x201D), chr(0xE9)


class TestValidateGlobal:
    def test_defaults_when_raw_is_empty_dict(self):
        block, err = ms.validate_global({}, None)
        assert err == ""
        assert block == ms.DEFAULT_GLOBAL_MARKERS

    def test_rejects_non_object(self):
        block, err = ms.validate_global("nope", None)
        assert block is None
        assert "object" in err

    @pytest.mark.parametrize("value", ["high", "medium"])
    def test_accepts_publish_when(self, value):
        block, err = ms.validate_global({"publish_when": value}, None)
        assert err == "" and block["publish_when"] == value

    def test_rejects_unknown_publish_when(self):
        block, err = ms.validate_global({"publish_when": "low"}, None)
        assert block is None and "publish_when" in err

    def test_rejects_unknown_source_id(self):
        block, err = ms.validate_global({"sources": [{"id": "bogus", "enabled": True}]}, None)
        assert block is None and "bogus" in err

    def test_rejects_duplicate_source_id(self):
        raw = {"sources": [{"id": "chapters", "enabled": True}, {"id": "chapters", "enabled": False}]}
        block, err = ms.validate_global(raw, None)
        assert block is None and "duplicate" in err

    def test_keeps_user_order_and_appends_missing_sources_in_default_order(self):
        raw = {"sources": [{"id": "skipdb", "enabled": False}, {"id": "chapters", "enabled": True}]}
        block, err = ms.validate_global(raw, None)
        assert err == ""
        assert [s["id"] for s in block["sources"]] == [
            "skipdb",
            "chapters",
            "theintrodb",
            "introdb",
            "season_audio",
            "credits_text",
            "server_markers",
        ]
        assert block["sources"][0]["enabled"] is False

    def test_masked_api_key_keeps_existing_key(self):
        existing = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "real-key-123"}]}
        raw = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": ms.SECRET_MASK}]}
        block, _ = ms.validate_global(raw, existing)
        tidb = next(s for s in block["sources"] if s["id"] == "theintrodb")
        assert tidb["api_key"] == "real-key-123"

    def test_new_api_key_is_stripped_and_stored(self):
        raw = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "  new-key  "}]}
        block, _ = ms.validate_global(raw, None)
        assert next(s for s in block["sources"] if s["id"] == "theintrodb")["api_key"] == "new-key"

    def test_rejects_api_key_with_whitespace_inside(self):
        raw = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "a b"}]}
        block, err = ms.validate_global(raw, None)
        assert block is None and "api_key" in err

    BAD_KEY_ERROR = "markers.sources: theintrodb api_key must be up to 200 printable ASCII characters with no spaces"

    @pytest.mark.parametrize(
        "key",
        [
            f"abc{ZERO_WIDTH_SPACE}def",
            f"{LEFT_QUOTE}abc{RIGHT_QUOTE}",
            f"cl{E_ACUTE}-123",
            "abc\x00def",
            "abc\x7fdef",
            f"a{NO_BREAK_SPACE}b",
            "k" * 201,
        ],
        ids=["zero-width-space", "curly-quotes", "non-ascii-letter", "nul", "del", "no-break-space", "too-long"],
    )
    def test_rejects_an_api_key_the_client_would_refuse_on_every_lookup(self, key):
        # The TheIntroDB client only sends printable ASCII without whitespace; anything else would be saved and then
        # fail every lookup with "API key contains invalid characters".
        raw = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": key}]}
        assert ms.validate_global(raw, None) == (None, self.BAD_KEY_ERROR)

    def test_rejects_a_new_bad_key_posted_over_a_valid_stored_one(self):
        stored = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "real-key-123"}]}
        posted = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": f"real-key-123{ZERO_WIDTH_SPACE}"}]}
        assert ms.validate_global(posted, stored) == (None, self.BAD_KEY_ERROR)

    @pytest.mark.parametrize("key", ["tidb_AbC-123.xyz==", "k" * 200])
    def test_accepts_a_printable_ascii_key_with_punctuation(self, key):
        raw = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": key}]}
        block, err = ms.validate_global(raw, None)
        assert err == "" and next(s for s in block["sources"] if s["id"] == "theintrodb")["api_key"] == key

    def test_a_stored_key_the_client_would_refuse_never_resets_the_other_settings(self):
        # Saved before this check existed: loading it (and saving with the masked key) keeps everything else.
        stored = {
            "publish_when": "medium",
            "sources": [{"id": "theintrodb", "enabled": True, "api_key": f"abc{ZERO_WIDTH_SPACE}def"}],
        }
        assert ms.load_global(stored).publish_when == "medium"
        posted = {"publish_when": "high", "sources": [{"id": "theintrodb", "enabled": True, "api_key": ms.SECRET_MASK}]}
        block, err = ms.validate_global(posted, stored)
        assert err == "" and block["publish_when"] == "high"

    def test_api_key_only_on_theintrodb(self):
        raw = {"sources": [{"id": "introdb", "enabled": True, "api_key": "x"}]}
        block, _ = ms.validate_global(raw, None)
        assert "api_key" not in next(s for s in block["sources"] if s["id"] == "introdb")

    def test_sources_omitted_entirely_keeps_existing_theintrodb_key(self):
        existing = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "real-key-123"}]}
        block, err = ms.validate_global({}, existing)
        assert err == ""
        tidb = next(s for s in block["sources"] if s["id"] == "theintrodb")
        assert tidb["api_key"] == "real-key-123"

    def test_theintrodb_omitted_from_posted_sources_keeps_existing_key(self):
        existing = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "real-key-123"}]}
        raw = {"sources": [{"id": "chapters", "enabled": True}]}
        block, err = ms.validate_global(raw, existing)
        assert err == ""
        tidb = next(s for s in block["sources"] if s["id"] == "theintrodb")
        assert tidb["api_key"] == "real-key-123"

    def test_theintrodb_entry_without_api_key_field_keeps_existing_key(self):
        existing = {"sources": [{"id": "theintrodb", "enabled": True, "api_key": "real-key-123"}]}
        raw = {"sources": [{"id": "theintrodb", "enabled": False}]}
        block, err = ms.validate_global(raw, existing)
        assert err == ""
        tidb = next(s for s in block["sources"] if s["id"] == "theintrodb")
        assert tidb["api_key"] == "real-key-123"
        assert tidb["enabled"] is False


class TestMaskGlobal:
    def test_masks_set_key(self):
        block = ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True, "api_key": "k"}]}, None)[0]
        masked = ms.mask_global(block)
        assert next(s for s in masked["sources"] if s["id"] == "theintrodb")["api_key"] == ms.SECRET_MASK
        assert next(s for s in block["sources"] if s["id"] == "theintrodb")["api_key"] == "k"  # input untouched

    def test_empty_key_stays_empty(self):
        masked = ms.mask_global(ms.DEFAULT_GLOBAL_MARKERS)
        assert next(s for s in masked["sources"] if s["id"] == "theintrodb")["api_key"] == ""


class TestValidateServer:
    @pytest.mark.parametrize("server_type", ["plex", "emby", "jellyfin"])
    def test_none_gives_defaults(self, server_type):
        block, err = ms.validate_server(None, server_type)
        assert err == ""
        assert block == ms.default_server_markers(server_type)
        assert ("plex" in block) is (server_type == "plex")
        assert ("emby" in block) is (server_type == "emby")

    def test_plex_enable_requires_confirmation(self):
        block, err = ms.validate_server({"enabled": True}, "plex")
        assert block is None and "confirm" in err.lower()

    def test_plex_enable_with_confirmation(self):
        raw = {"enabled": True, "plex": {"db_write_confirmed_at": "2026-09-13T10:00:00+00:00"}}
        block, err = ms.validate_server(raw, "plex")
        assert err == "" and block["enabled"] is True
        assert block["plex"] == {"db_write_confirmed_at": "2026-09-13T10:00:00+00:00", "on_plex_redetect": "restore"}

    @pytest.mark.parametrize(
        ("server_type", "expected"),
        [
            ("emby", {"enabled": True, "library_ids": None, "emby": {"on_emby_redetect": "restore"}}),
            ("jellyfin", {"enabled": True, "library_ids": None}),
        ],
    )
    def test_non_plex_enable_needs_no_confirmation_and_drops_plex_block(self, server_type, expected):
        raw = {"enabled": True, "plex": {"db_write_confirmed_at": "x"}}
        block, err = ms.validate_server(raw, server_type)
        assert err == "" and block == expected

    @pytest.mark.parametrize(("server_type", "has_emby_block"), [("plex", False), ("emby", True), ("jellyfin", False)])
    def test_emby_block_is_kept_only_for_emby(self, server_type, has_emby_block):
        raw = {"enabled": False, "emby": {"on_emby_redetect": "keep_emby"}}
        if server_type == "plex":
            raw["plex"] = {"db_write_confirmed_at": None}
        block, err = ms.validate_server(raw, server_type)
        assert err == ""
        assert block.get("emby") == ({"on_emby_redetect": "keep_emby"} if has_emby_block else None)
        settings = ms.load_server(raw, server_type)
        assert settings.on_emby_redetect == ("keep_emby" if has_emby_block else "restore")
        assert settings.keeps_server_markers is has_emby_block

    @pytest.mark.parametrize("raw", [{"emby": {"on_emby_redetect": "keep_plex"}}, {"emby": "keep_emby"}])
    def test_rejects_bad_emby_block(self, raw):
        block, err = ms.validate_server(raw, "emby")
        assert block is None and "markers.emby" in err

    @pytest.mark.parametrize(
        ("server_type", "raw", "keeps"),
        [
            ("plex", {"plex": {"db_write_confirmed_at": "t", "on_plex_redetect": "keep_plex"}}, True),
            ("plex", {"plex": {"db_write_confirmed_at": "t", "on_plex_redetect": "restore"}}, False),
            ("emby", {"emby": {"on_emby_redetect": "keep_emby"}}, True),
            ("emby", {"emby": {"on_emby_redetect": "restore"}}, False),
            ("emby", {}, False),
            ("jellyfin", {}, False),
        ],
    )
    def test_keeps_server_markers_follows_each_vendors_setting(self, server_type, raw, keeps):
        assert ms.load_server({"enabled": True, **raw}, server_type).keeps_server_markers is keeps

    def test_rejects_bad_on_plex_redetect(self):
        raw = {"plex": {"on_plex_redetect": "sometimes"}}
        block, err = ms.validate_server(raw, "plex")
        assert block is None and "on_plex_redetect" in err

    def test_library_ids_normalised_to_unique_strings(self):
        block, err = ms.validate_server({"library_ids": [1, "2", "2"]}, "jellyfin")
        assert err == "" and block["library_ids"] == ["1", "2"]

    def test_rejects_library_ids_non_list(self):
        block, err = ms.validate_server({"library_ids": "1"}, "emby")
        assert block is None and "library_ids" in err

    @pytest.mark.parametrize("raw", [[], "on", 1])
    def test_rejects_a_block_that_is_not_an_object_and_loads_it_as_off(self, raw):
        assert ms.validate_server(raw, "jellyfin") == (None, "markers must be an object")
        assert ms.load_server(raw, "jellyfin").enabled is False

    def test_load_server_treats_unconfirmed_plex_as_disabled(self):
        s = ms.load_server({"enabled": True, "plex": {"db_write_confirmed_at": None}}, "plex")
        assert s.enabled is False

    def test_load_server_confirmed_plex_enabled(self):
        s = ms.load_server({"enabled": True, "plex": {"db_write_confirmed_at": "t"}}, "plex")
        assert s.enabled is True and s.db_write_confirmed_at == "t" and s.on_plex_redetect == "restore"

    def test_accepts_keep_plex_on_plex_redetect(self):
        raw = {"plex": {"on_plex_redetect": "keep_plex"}}
        block, err = ms.validate_server(raw, "plex")
        assert err == ""
        assert block["plex"]["on_plex_redetect"] == "keep_plex"

    def test_load_server_loads_keep_plex_on_plex_redetect(self):
        s = ms.load_server({"plex": {"on_plex_redetect": "keep_plex"}}, "plex")
        assert s.on_plex_redetect == "keep_plex"

    def test_load_server_invalid_dict_logs_warning_and_falls_back_to_disabled(self, loguru_caplog):
        # enabled without confirmation is a validation error, not a valid "disabled" shape.
        s = ms.load_server({"enabled": True}, "plex")
        assert s.enabled is False
        assert "Ignoring invalid per-server Intro & Credits settings" in loguru_caplog.text


class TestIsSportsLibrary:
    @pytest.mark.parametrize(
        ("name", "kind", "expected"),
        [
            ("Movies", "movie", False),
            ("Anything", "sport", True),
            ("Anything", "sports", True),
            ("Transport Docs", None, False),  # name-based guard: "sport(s)" must be a whole word
        ],
    )
    def test_matrix(self, name, kind, expected):
        assert ms.is_sports_library(name, kind) is expected


class TestLibraryAllowed:
    @pytest.mark.parametrize(
        ("library_ids", "lib_id", "name", "expected"),
        [
            (None, "1", "Movies", True),
            (None, "2", "Sports", False),
            (None, "3", "NFL Sport", False),
            (None, "4", "Transport Docs", True),
            (("1",), "1", "Movies", True),
            (("1",), "2", "TV Shows", False),
            (("2",), "2", "Sports", True),  # explicit choice beats the sports default
        ],
    )
    def test_matrix(self, library_ids, lib_id, name, expected):
        s = ms.ServerMarkersSettings(True, library_ids, None, "restore")
        assert ms.library_allowed(s, library_id=lib_id, library_name=name, kind=None) is expected

    def test_unknown_library_id_uses_default_rule(self):
        s = ms.ServerMarkersSettings(True, None, None, "restore")
        assert ms.library_allowed(s, library_id=None, library_name="", kind=None) is True


class TestLoadGlobal:
    def test_garbage_falls_back_to_defaults(self):
        g = ms.load_global(["x"])
        assert g.publish_when == "high" and g.detect_intro and not g.detect_recap
        assert g.ordered_enabled_sources() == (
            "chapters",
            "introdb",
            "skipdb",
            "season_audio",
            "credits_text",
            "server_markers",
        )

    def test_fingerprint_ignores_api_key_but_tracks_enabled(self):
        a = ms.load_global(
            ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True, "api_key": "1"}]}, None)[0]
        )
        b = ms.load_global(
            ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True, "api_key": "2"}]}, None)[0]
        )
        # Same key *presence* as `a` (a real, non-empty key either way) so this row isolates
        # `enabled` — without this, `c` differing from `a` in both `enabled` and key-presence would
        # let a regression that drops `enabled` from the hash pass unnoticed.
        c = ms.load_global(
            ms.validate_global({"sources": [{"id": "theintrodb", "enabled": False, "api_key": "1"}]}, None)[0]
        )
        assert a.detection_fingerprint() == b.detection_fingerprint() != c.detection_fingerprint()

    def test_fingerprint_tracks_whether_theintrodb_has_a_key_configured(self):
        with_key = ms.load_global(
            ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True, "api_key": "abc"}]}, None)[0]
        )
        without_key = ms.load_global(ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True}]}, None)[0])
        other_key = ms.load_global(
            ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True, "api_key": "xyz"}]}, None)[0]
        )
        assert with_key.detection_fingerprint() != without_key.detection_fingerprint()
        assert with_key.detection_fingerprint() == other_key.detection_fingerprint()

    def test_repr_does_not_include_api_key(self):
        g = ms.load_global(
            ms.validate_global({"sources": [{"id": "theintrodb", "enabled": True, "api_key": "real-key-999"}]}, None)[0]
        )
        assert "real-key-999" not in repr(g)

    def test_invalid_dict_logs_warning_and_falls_back_to_defaults(self, loguru_caplog):
        g = ms.load_global({"publish_when": "sometimes"})
        assert g.publish_when == "high"
        assert "Ignoring invalid Intro & Credits settings" in loguru_caplog.text
