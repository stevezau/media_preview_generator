"""Only Plex's raw control characters in a title get a lossless compatibility read."""

import json

import pytest

from media_preview_generator.markers.publishers.base import Capability, PublishError
from media_preview_generator.markers.publishers.plex_db import decode_extra_data, encode_extra_data


def native_title(control="\x16"):
    fields = {
        "ma:title": f'Commentary with Person A {control}and Person B; "quoted", \\path and café',
        "ma:audioChannelLayout": "stereo",
        "ma:samplingRate": "48000",
        "ma:streamIdentifier": "2",
        "unknown:preserve": "A&B = + % / 😀",
    }
    canonical = encode_extra_data(fields)
    # Plex wrote the literal character in JSON, but the URL mirror is properly escaped.
    escaped = json.dumps(control)[1:-1]
    return canonical.replace(escaped, control), json.loads(canonical)


@pytest.mark.parametrize("control", [chr(code) for code in range(32)])
def test_raw_title_controls_preserve_every_field_through_strict_roundtrip(control):
    raw, expected = native_title(control)
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)

    fields, url_form = decode_extra_data(raw)

    assert url_form is False
    assert fields == expected
    rewritten = encode_extra_data(fields)
    assert json.loads(rewritten) == expected
    assert control not in rewritten
    assert decode_extra_data(rewritten) == (expected, False)


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "escaped_duplicate",
        "nontext",
        "empty_key",
        "control_key",
        "surrogate_key",
        "mirror_mismatch",
        "mirror_missing",
        "mirror_duplicate",
        "other_value",
        "outside",
        "trailing_comma",
        "invalid_escape",
        "truncated",
        "unquoted_key",
    ],
)
def test_ambiguous_or_other_malformed_json_is_not_repaired(damage):
    raw, expected = native_title()
    if damage == "duplicate":
        raw = raw.replace("{", '{"ma:title":"other",', 1)
    elif damage == "escaped_duplicate":
        raw = raw.replace("{", '{"ma:\\u0074itle":"other",', 1)
    elif damage == "nontext":
        raw = raw.replace('"ma:samplingRate":"48000"', '"ma:samplingRate":48000')
    elif damage == "empty_key":
        raw = raw.replace('"unknown:preserve"', '""')
    elif damage == "control_key":
        raw = raw.replace('"unknown:preserve"', '"unknown:\\u0016preserve"')
    elif damage == "surrogate_key":
        raw = raw.replace('"unknown:preserve"', '"unknown:\\ud800preserve"')
    elif damage == "mirror_mismatch":
        raw = raw.replace("ma%3AsamplingRate=48000", "ma%3AsamplingRate=44100")
    elif damage == "mirror_missing":
        raw = raw.replace(',"url":' + json.dumps(expected["url"], ensure_ascii=False), "")
    elif damage == "mirror_duplicate":
        raw = raw.replace('"url":"', '"url":"ma%3AsamplingRate=48000&', 1)
    elif damage == "other_value":
        raw = raw.replace('"stereo"', '"ste\x16reo"')
    elif damage == "outside":
        raw = raw[:-1] + "\x16}"
    elif damage == "trailing_comma":
        raw = raw[:-1] + ",}"
    elif damage == "invalid_escape":
        raw = raw.replace('"stereo"', '"ste\\qreo"')
    elif damage == "truncated":
        raw = raw[:-1]
    else:
        raw = raw.replace('"ma:samplingRate"', "ma:samplingRate")
    with pytest.raises(PublishError) as caught:
        decode_extra_data(raw)
    assert caught.value.state is Capability.UNSUPPORTED_SCHEMA


@pytest.mark.parametrize("second", ['"ma:title"', '"ma:\\u0074itle"'])
def test_strict_valid_json_with_duplicate_fields_is_refused(second):
    with pytest.raises(PublishError):
        decode_extra_data('{"ma:title":"first",' + second + ':"second"}')


def test_strict_valid_json_and_legacy_url_forms_keep_their_supported_contract():
    fields = {"ma:title": "ordinary", "unknown:preserve": "all text"}
    assert decode_extra_data(json.dumps(fields)) == (fields, False)
    url = encode_extra_data(fields, url_form=True)
    assert decode_extra_data(url) == (fields, True)


def test_title_with_literal_backslash_before_control_remains_lossless():
    canonical = encode_extra_data({"ma:title": "Commentary \\\x16 "})
    raw = canonical.replace("\\u0016", "\x16")
    assert decode_extra_data(raw) == (json.loads(canonical), False)


def test_control_character_cannot_repair_an_invalid_backslash_escape():
    raw, _ = native_title()
    raw = raw.replace("\x16", "\\\x16")
    with pytest.raises(PublishError):
        decode_extra_data(raw)
