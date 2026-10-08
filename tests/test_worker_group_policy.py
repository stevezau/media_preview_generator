"""Policy boundaries, including overnight time and overlapping capacity."""

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from media_preview_generator.worker_groups import (
    configured_group_totals,
    effective_worker_groups,
    group_is_available,
    group_resource_key,
    groups_from_legacy,
    legacy_group_view,
    member_policies,
    next_group_opening,
    supports_job,
    validate_worker_groups,
)


def cpu_group(**changes):
    return {
        "id": "cpu-a",
        "name": "Loudness",
        "enabled": True,
        "resource": "cpu",
        "device": None,
        "count": 2,
        "job_types": ["loudness"],
        "availability": {"mode": "always", "windows": []},
        **changes,
    }


def window(days, start="23:00", end="07:00"):
    return {"mode": "scheduled", "windows": [{"days": days, "start": start, "end": end}]}


@pytest.mark.parametrize(
    ("timestamp", "eligible"),
    [
        ("2026-10-05T01:00:00", False),
        ("2026-10-05T22:59:59", False),
        ("2026-10-05T23:00:00", True),
        ("2026-10-06T06:59:59", True),
        ("2026-10-06T07:00:00", False),
    ],
)
def test_overnight_belongs_to_start_day(timestamp, eligible):
    group = cpu_group(availability=window([0]))
    assert group_is_available(group, datetime.fromisoformat(timestamp)) is eligible


def test_sunday_window_wraps_into_monday():
    group = cpu_group(availability=window([6]))
    assert group_is_available(group, datetime(2026, 10, 5, 1))
    assert not group_is_available(group, datetime(2026, 10, 6, 1))


def test_same_group_windows_union_but_different_groups_add():
    group = cpu_group(count=40, availability=window([0], "08:00", "12:00"))
    group["availability"]["windows"].append({"days": [0], "start": "10:00", "end": "14:00"})
    assert configured_group_totals(validate_worker_groups([group])) == (0, 40)
    other = {**group, "id": "cpu-b"}
    with pytest.raises(ValueError, match="Overlapping"):
        validate_worker_groups([group, other])


def test_nonoverlapping_groups_share_capacity_limit():
    groups = [
        cpu_group(count=64, availability=window([0], "08:00", "12:00")),
        cpu_group(id="cpu-b", count=64, availability=window([0], "12:00", "16:00")),
    ]
    assert configured_group_totals(validate_worker_groups(groups)) == (0, 64)


def test_gpu_family_limit_applies_across_devices():
    groups = [
        cpu_group(id=f"gpu-{n}", resource="gpu", device=f"gpu:{n}", count=40, job_types=["previews"]) for n in range(2)
    ]
    with pytest.raises(ValueError, match="GPU 80/64"):
        validate_worker_groups(groups)


def test_cpu_loudness_requirement_cannot_be_overridden_by_policy():
    group = cpu_group(resource="gpu", device="gpu:0")
    assert not supports_job(group, "loudness")
    with pytest.raises(ValueError, match="requires CPU"):
        validate_worker_groups([group])


def test_policy_allows_only_selected_job_kinds():
    group = cpu_group()
    assert supports_job(group, "loudness")
    assert not supports_job(group, "previews")
    assert not supports_job(group, "intro_credits")


@pytest.mark.parametrize(
    "change",
    [
        {"count": True},
        {"count": 0},
        {"count": 65},
        {"job_types": []},
        {"job_types": ["chapters"]},
        {"enabled": "false"},
        {"availability": window([], "01:00", "02:00")},
        {"availability": window([0], "01:00", "01:00")},
        {"availability": window([0], "25:00", "02:00")},
    ],
)
def test_invalid_policy_is_rejected(change):
    with pytest.raises(ValueError):
        validate_worker_groups([cpu_group(**change)])


def test_explicit_empty_policy_does_not_restore_legacy_counts():
    assert effective_worker_groups({"worker_groups": [], "cpu_threads": 8}) == []
    assert effective_worker_groups({"cpu_threads": 0, "gpu_config": []}) == []
    assert effective_worker_groups({"cpu_threads": 2})[0]["members"][0]["count"] == 2


def test_disabled_group_has_no_opening_and_does_not_use_budget():
    group = cpu_group(enabled=False, count=32)
    assert not group_is_available(group)
    assert next_group_opening(group) is None
    assert configured_group_totals([group]) == (0, 0)


def test_spring_gap_returns_first_real_open_minute():
    zone = ZoneInfo("Australia/Sydney")
    group = cpu_group(availability=window([6], "02:30", "04:00"))
    opening = next_group_opening(group, datetime(2026, 10, 4, 1, 59, tzinfo=zone))
    assert opening == datetime(2026, 10, 4, 3, 0, tzinfo=zone)


def test_repeated_hour_is_available_in_both_folds():
    zone = ZoneInfo("Australia/Sydney")
    group = cpu_group(availability=window([6], "02:00", "03:00"))
    assert group_is_available(group, datetime(2026, 4, 5, 2, 30, tzinfo=zone, fold=0))
    assert group_is_available(group, datetime(2026, 4, 5, 2, 30, tzinfo=zone, fold=1))


def test_validation_does_not_mutate_draft():
    group = cpu_group()
    original = copy.deepcopy(group)
    clean = validate_worker_groups([group])
    clean[0]["members"][0]["job_types"].append("previews")
    assert group == original


def test_group_revision_and_persistence_failure_preserve_saved_policy(tmp_path, monkeypatch):
    from media_preview_generator.web.settings_manager import SettingsManager, WorkerGroupsConflict

    settings = SettingsManager(str(tmp_path))
    assert settings.update_worker_groups([cpu_group()], expected_revision=0) == 1
    with pytest.raises(WorkerGroupsConflict):
        settings.update_worker_groups([], expected_revision=0)
    assert settings.worker_groups[0]["members"][0]["count"] == 2

    def fail_save():
        raise OSError("Disk full")

    monkeypatch.setattr(settings, "_save", fail_save)
    with pytest.raises(OSError, match="Disk full"):
        settings.update_worker_groups([], expected_revision=1)
    assert settings.worker_groups_revision == 1
    assert settings.worker_groups[0]["members"][0]["count"] == 2


def test_global_pause_owners_do_not_clear_each_other(tmp_path):
    from media_preview_generator.web.settings_manager import SettingsManager

    settings = SettingsManager(str(tmp_path))
    settings.processing_paused = True
    settings.set_processing_pause_reason("quiet_hours", True)
    settings.set_processing_pause_reason("quiet_hours", False)
    assert settings.processing_paused
    assert settings.processing_pause_reasons == ["manual"]
    settings.set_processing_pause_reason("quiet_hours", True)
    settings.processing_paused = False
    assert settings.processing_paused
    assert settings.processing_pause_reasons == ["quiet_hours"]


def test_fully_skipped_dst_window_opens_the_following_week():
    zone = ZoneInfo("Australia/Sydney")
    group = cpu_group(availability=window([6], "02:15", "02:30"))
    # September 27 has already closed, and October 4's entire window does not
    # exist when clocks advance. The next real window is two Sundays away.
    opening = next_group_opening(group, datetime(2026, 9, 27, 3, 0, tzinfo=zone))
    assert opening == datetime(2026, 10, 11, 2, 15, tzinfo=zone)


@pytest.fixture
def mounted_sydney_localtime(monkeypatch):
    """Model a Sydney bind mount over an image's Etc/UTC symlink target."""
    import io
    from pathlib import Path
    from types import SimpleNamespace

    from media_preview_generator import worker_groups as policy

    sydney_bytes = Path("/usr/share/zoneinfo/Australia/Sydney").read_bytes()
    monkeypatch.setattr(policy, "open", lambda *args, **kwargs: io.BytesIO(sydney_bytes), raising=False)
    monkeypatch.setattr(
        policy, "Path", lambda _: SimpleNamespace(resolve=lambda: Path("/usr/share/zoneinfo/Etc/UTC")), raising=False
    )
    policy._timezone_for_name.cache_clear()
    yield policy
    policy._timezone_for_name.cache_clear()


@pytest.mark.parametrize("tz_value", [None, "", "Not/A_Real_Zone"])
def test_mounted_localtime_bytes_override_misleading_symlink_name(mounted_sydney_localtime, monkeypatch, tz_value):
    from datetime import timedelta

    if tz_value is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", tz_value)
    zone = mounted_sydney_localtime.application_timezone()
    assert datetime(2026, 10, 5, 12, tzinfo=zone).utcoffset() == timedelta(hours=11)
    assert datetime(2026, 7, 5, 12, tzinfo=zone).utcoffset() == timedelta(hours=10)
    assert str(zone) == "Local time"
    group = cpu_group(availability=window([6], "02:15", "02:30"))
    assert next_group_opening(group, datetime(2026, 9, 27, 3, tzinfo=zone)).isoformat() == "2026-10-11T02:15:00+11:00"


@pytest.mark.parametrize(
    ("tz_value", "key", "offset"), [("UTC", "UTC", 0), (":Australia/Sydney", "Australia/Sydney", 11)]
)
def test_explicit_timezone_overrides_mounted_localtime(mounted_sydney_localtime, monkeypatch, tz_value, key, offset):
    from datetime import timedelta

    monkeypatch.setenv("TZ", tz_value)
    zone = mounted_sydney_localtime.application_timezone()
    assert str(zone) == key
    assert datetime(2026, 10, 5, 12, tzinfo=zone).utcoffset() == timedelta(hours=offset)


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_unreadable_localtime_falls_back_to_utc(monkeypatch, failure):
    import io
    from datetime import UTC
    from types import SimpleNamespace

    from media_preview_generator import worker_groups as policy

    def read_localtime(*args, **kwargs):
        if failure == "missing":
            raise OSError("No localtime")
        return io.BytesIO(b"not a timezone file")

    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(policy, "open", read_localtime, raising=False)
    monkeypatch.setattr(policy, "Path", lambda _: SimpleNamespace(resolve=lambda: "/etc/localtime"), raising=False)
    policy._timezone_for_name.cache_clear()
    try:
        assert policy.application_timezone() is UTC
    finally:
        policy._timezone_for_name.cache_clear()


def member(member_id="m1", resource="cpu", device=None, count=2, job_types=("loudness",)):
    return {"id": member_id, "resource": resource, "device": device, "count": count, "job_types": list(job_types)}


def new_group(group_id="g1", members=None, **changes):
    return {
        "id": group_id,
        "name": "Off-hours",
        "enabled": True,
        "availability": {"mode": "always", "windows": []},
        "members": [member()] if members is None else members,
        **changes,
    }


def test_member_shape_is_accepted_and_normalized_in_fixed_key_order():
    raw = new_group(
        members=[
            member("a", "gpu", "cuda:0", 3, ["intro_credits", "previews"]),
            member("b", "cpu", None, 10, ["loudness", "previews"]),
        ]
    )
    clean = validate_worker_groups([raw])
    assert list(clean[0]) == ["id", "name", "enabled", "availability", "members"]
    assert list(clean[0]["members"][0]) == ["id", "resource", "device", "count", "job_types"]
    assert clean[0]["members"][0]["job_types"] == ["previews", "intro_credits"]
    assert clean[0]["members"][1]["job_types"] == ["previews", "loudness"]
    assert validate_worker_groups(clean) == clean


def test_legacy_flat_group_becomes_one_member_m1():
    clean = validate_worker_groups([cpu_group()])
    assert clean == [
        {
            "id": "cpu-a",
            "name": "Loudness",
            "enabled": True,
            "availability": {"mode": "always", "windows": []},
            "members": [member("m1", "cpu", None, 2, ["loudness"])],
        }
    ]


def test_top_level_echo_equal_to_the_only_member_is_accepted_and_dropped():
    raw = new_group(**legacy_group_view(new_group()))
    assert raw["count"] == 2
    clean = validate_worker_groups([raw])
    assert clean[0]["members"] == [member()]
    assert "count" not in clean[0] and "resource" not in clean[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [("resource", "gpu"), ("device", "cuda:9"), ("count", 99), ("job_types", ["previews"])],
)
def test_top_level_field_that_differs_from_the_only_member_is_refused(field, value):
    raw = new_group(**{**legacy_group_view(new_group()), field: value})
    with pytest.raises(ValueError, match=rf"edit members\[0\] instead of the group's top-level {field}"):
        validate_worker_groups([raw])


def test_top_level_fields_are_not_checked_against_a_multi_member_group():
    raw = new_group(members=[member("a"), member("b", "gpu", "cuda:0", 1, ["previews"])], count=99)
    assert len(validate_worker_groups([raw])[0]["members"]) == 2


def test_gpu_device_with_surrounding_whitespace_is_refused():
    with pytest.raises(ValueError, match="choose a GPU device"):
        validate_worker_groups([new_group(members=[member("g", "gpu", " cuda:0", 1, ["previews"])])])


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([], "Off-hours: add at least one device"),
        ([member(str(n), "gpu", f"cuda:{n}", 1, ["previews"]) for n in range(9)], "Off-hours: use at most 8 devices"),
        ([member("a"), member("a", "gpu", "cuda:0", 1, ["previews"])], "each device needs a unique valid ID"),
        ([member("bad id")], "each device needs a unique valid ID"),
        ([member(resource="tpu")], "choose CPU or GPU for each device"),
        ([member(resource="gpu", job_types=["previews"])], "choose a GPU device"),
        ([member(device="cuda:0")], "a CPU member cannot select a GPU device"),
        ([member("a"), member("b")], "Off-hours: CPU appears twice; use one row per device"),
        (
            [member("a", "gpu", "cuda:0", 1, ["previews"]), member("b", "gpu", "cuda:0", 1, ["intro_credits"])],
            "Off-hours: cuda:0 appears twice; use one row per device",
        ),
        ([member(count=0)], "Off-hours (CPU): worker count must be between 1 and 64; remove the device to use zero"),
        ([member(count=65)], "Off-hours (CPU): worker count must be between 1 and 64; remove the device to use zero"),
        ([member(count=True)], "worker count must be between 1 and 64"),
        ([member(job_types=[])], "Off-hours (CPU): select at least one supported job type"),
        ([member(job_types=["chapters"])], "select at least one supported job type"),
        (
            [member("g", "gpu", "cuda:0", 1, ["previews", "loudness"])],
            "Off-hours (cuda:0): Plex loudness requires CPU workers",
        ),
        (["cpu"], "each device must be an object"),
    ],
)
def test_bad_member_shapes_are_refused_with_exact_messages(members, message):
    with pytest.raises(ValueError) as caught:
        validate_worker_groups([new_group(members=members)])
    assert message in str(caught.value)


def test_group_without_members_or_resource_is_refused():
    raw = new_group()
    del raw["members"]
    with pytest.raises(ValueError, match="Off-hours: add at least one device"):
        validate_worker_groups([raw])


@pytest.mark.parametrize("count", [1, 64])
def test_member_count_edges_are_accepted(count):
    assert validate_worker_groups([new_group(members=[member(count=count)])])[0]["members"][0]["count"] == count


def test_same_device_may_appear_in_different_groups_and_ids_may_repeat_across_groups():
    groups = [
        new_group("g1", [member("m1", "gpu", "cuda:0", 1, ["previews"])]),
        new_group("g2", [member("m1", "gpu", "cuda:0", 1, ["intro_credits"])]),
    ]
    assert configured_group_totals(validate_worker_groups(groups)) == (2, 0)


def test_group_limits_still_apply():
    with pytest.raises(ValueError, match="at most 64 groups"):
        validate_worker_groups([new_group(f"g{n}") for n in range(65)])
    with pytest.raises(ValueError, match="unique valid ID"):
        validate_worker_groups([new_group("g1"), new_group("g1")])


def test_peak_overflow_is_summed_across_members_of_one_group():
    members = [member("a", "cpu", None, 40), member("b", "gpu", "cuda:0", 40, ["previews"])]
    assert configured_group_totals(validate_worker_groups([new_group(members=members)])) == (40, 40)
    with pytest.raises(ValueError, match=r"peak CPU 80/64, GPU 0/64"):
        validate_worker_groups([new_group("g1", [member(count=40)]), new_group("g2", [member(count=40)])])


def test_gpu_peak_overflow_across_members_of_different_groups():
    groups = [
        new_group("g1", [member("a", "gpu", "cuda:0", 40, ["previews"])]),
        new_group("g2", [member("a", "gpu", "cuda:1", 40, ["previews"])]),
    ]
    with pytest.raises(ValueError, match=r"GPU 80/64"):
        validate_worker_groups(groups)


def test_exactly_64_across_members_is_accepted():
    groups = [new_group("g1", [member(count=32)]), new_group("g2", [member(count=32)])]
    assert configured_group_totals(validate_worker_groups(groups)) == (0, 64)


def test_member_policies_copy_group_and_member_fields_under_a_composite_id():
    group = new_group(
        "g1",
        [member("a", "gpu", "cuda:0", 3, ["previews"]), member("b", "cpu", None, 10, ["loudness"])],
        enabled=False,
        availability=window([0], "01:00", "07:00"),
    )
    policies = member_policies([group])
    assert policies == [
        {
            "id": "g1:a",
            "group_id": "g1",
            "member_id": "a",
            "name": "Off-hours",
            "enabled": False,
            "availability": group["availability"],
            "resource": "gpu",
            "device": "cuda:0",
            "count": 3,
            "job_types": ["previews"],
        },
        {
            "id": "g1:b",
            "group_id": "g1",
            "member_id": "b",
            "name": "Off-hours",
            "enabled": False,
            "availability": group["availability"],
            "resource": "cpu",
            "device": None,
            "count": 10,
            "job_types": ["loudness"],
        },
    ]
    assert member_policies(policies) == policies


def test_member_policies_treat_a_legacy_flat_group_as_member_m1():
    policy = member_policies([cpu_group()])[0]
    assert (policy["id"], policy["group_id"], policy["member_id"]) == ("cpu-a:m1", "cpu-a", "m1")
    assert policy["count"] == 2


def test_helpers_work_on_a_saved_group_with_members():
    group = new_group(members=[member("a", "gpu", "cuda:0", 3, ["previews"]), member("b", job_types=["loudness"])])
    assert supports_job(group, "previews")
    assert supports_job(group, "loudness")
    assert not supports_job(group, "intro_credits")
    assert group_is_available(group)
    assert group_resource_key(new_group(members=[member("a", "gpu", "cuda:0", 1, ["previews"])])) == "gpu:cuda:0"
    with pytest.raises(ValueError, match="several devices"):
        group_resource_key(group)


def test_legacy_group_view_echoes_only_single_member_groups():
    single = new_group(members=[member("a", "gpu", "cuda:0", 3, ["previews"])])
    assert legacy_group_view(single) == {
        "resource": "gpu",
        "device": "cuda:0",
        "count": 3,
        "job_types": ["previews"],
    }
    assert legacy_group_view(new_group(members=[member("a"), member("b", "gpu", "cuda:0", 1, ["previews"])])) == {}


def test_groups_from_legacy_emits_member_shape_with_m1():
    groups = groups_from_legacy(
        {"cpu_threads": 4, "gpu_config": [{"device": "cuda:0", "name": "RTX", "workers": 2, "enabled": False}]}
    )
    assert [g["id"] for g in groups][0] == "legacy-cpu"
    assert groups[0]["members"] == [member("m1", "cpu", None, 4, ["previews", "intro_credits", "loudness"])]
    assert groups[1]["name"] == "RTX"
    assert groups[1]["enabled"] is False
    assert groups[1]["members"] == [member("m1", "gpu", "cuda:0", 2, ["previews", "intro_credits"])]
