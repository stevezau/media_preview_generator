"""Which GPUs and how many CPU helpers the saved worker groups select."""

from types import SimpleNamespace

from media_preview_generator.markers.credits.textdet_helper import _configured_cpu_workers
from media_preview_generator.web.routes.job_runner import _build_selected_gpus

ALWAYS = {"mode": "always", "windows": []}


def member(mid, resource, count, kinds, device=None):
    return {"id": mid, "resource": resource, "device": device, "count": count, "job_types": kinds}


def group(gid, members, enabled=True):
    return {"id": gid, "name": gid, "enabled": enabled, "availability": ALWAYS, "members": members}


def fake_settings(groups, gpu_config=()):
    values = {"worker_groups": groups}
    return SimpleNamespace(
        gpu_config=list(gpu_config),
        worker_groups=groups,
        cpu_threads=9,
        get=lambda key, default=None: values.get(key, default),
    )


DETECTED = [
    {"type": "nvidia", "device": "cuda:0", "name": "A"},
    {"type": "nvidia", "device": "cuda:1", "name": "B"},
]


class TestBuildSelectedGpus:
    def test_gpu_member_in_a_disabled_group_is_excluded(self):
        groups = [
            group("on", [member("m1", "gpu", 2, ["previews"], "cuda:0")]),
            group("off", [member("m1", "gpu", 3, ["previews"], "cuda:1")], enabled=False),
        ]

        selected = _build_selected_gpus(fake_settings(groups), detected=DETECTED)

        assert [device for _, device, _ in selected] == ["cuda:0"]
        assert selected[0][2]["workers"] == 2

    def test_workers_for_one_device_are_summed_across_groups(self):
        groups = [
            group("a", [member("m1", "gpu", 2, ["previews"], "cuda:0")]),
            group(
                "b",
                [member("m1", "gpu", 3, ["intro_credits"], "cuda:0"), member("m2", "gpu", 1, ["previews"], "cuda:1")],
            ),
        ]

        selected = {
            device: info["workers"]
            for _, device, info in _build_selected_gpus(fake_settings(groups), detected=DETECTED)
        }

        assert selected == {"cuda:0": 5, "cuda:1": 1}

    def test_failed_gpu_is_excluded(self):
        groups = [group("a", [member("m1", "gpu", 2, ["previews"], "cuda:0")])]
        detected = [{**DETECTED[0], "status": "failed"}]

        assert _build_selected_gpus(fake_settings(groups), detected=detected) == []


class TestConfiguredCpuWorkers:
    def test_counts_only_cpu_members_that_can_run_intro_credits(self, monkeypatch):
        groups = [
            group(
                "mixed",
                [
                    member("m1", "cpu", 4, ["previews", "intro_credits"]),
                    member("m2", "gpu", 7, ["previews", "intro_credits"], "cuda:0"),
                    member("m3", "cpu", 5, ["loudness"]),
                ],
            ),
            group("other", [member("m1", "cpu", 2, ["intro_credits"])]),
        ]
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.peek_settings_manager", lambda: fake_settings(groups)
        )

        assert _configured_cpu_workers() == 6
