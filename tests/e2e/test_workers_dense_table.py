"""E2E tests for the dense Workers table (any enabled member with 5+ workers) and its per-member row cap."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults

LONG_DEVICE = "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770] (rev 04)"
ROW_CAP = 8
ROWS = "#workerGroupLiveRows .worker-slot:visible"
JOB = "abcd1234-0000-4000-8000-0000000000{:02d}"


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _member(member_id: str, count: int, resource: str = "cpu", types: tuple[str, ...] = ("previews",)) -> dict:
    return {
        "id": member_id,
        "resource": resource,
        "device": None if resource == "cpu" else "/dev/dri/renderD128",
        "count": count,
        "job_types": list(types),
    }


def _multi_group(group_id: str, name: str, members: list[dict]) -> dict:
    return {
        "id": group_id,
        "name": name,
        "enabled": True,
        "members": members,
        "availability": {"mode": "always", "windows": []},
    }


def _group(group_id: str, name: str, count: int, resource: str = "cpu", types: tuple[str, ...] = ("previews",)) -> dict:
    return _multi_group(group_id, name, [_member("m1", count, resource, types)])


def _worker(group: dict, number: int, *, busy: bool, kind: str = "previews", member: int = 0, **extra) -> dict:
    member_row = group["members"][member]
    return {
        "worker_id": number,
        "worker_type": "CPU" if member_row["resource"] == "cpu" else "GPU",
        "worker_name": f"{group['name']} {number}",
        "group_id": group["id"],
        "member_id": member_row["id"],
        "group_name": group["name"],
        "status": "processing" if busy else "idle",
        "progress_percent": 10 + number if busy else 0,
        "current_title": f"Episode {number} of a series with a rather long descriptive title" if busy else "",
        "current_file": f"/data/tv/Episode {number}.mkv" if busy else "",
        "job_id": JOB.format(number) if busy else None,
        "job_kind": kind,
        "library_name": "TV Shows",
        "ffmpeg_started": busy and kind == "previews",
        "speed": "3.2x",
        "eta": "4m 10s",
        **extra,
    }


def _open(page: Page, app_url: str, groups: list[dict], workers: list[dict], width: int = 1440) -> None:
    mock_dashboard_defaults(page)
    state = {
        "groups": groups,
        "revision": 1,
        "timezone": "UTC",
        "limits": {"cpu": 32, "gpu": 32, "members": 8},
        "hardware": [{"device": "/dev/dri/renderD128", "name": LONG_DEVICE, "type": "intel", "status": "ok"}],
        "capacity": {
            "groups": [
                {
                    "id": g["id"],
                    "state": "busy",
                    "busy": sum(1 for w in workers if w["group_id"] == g["id"] and w["status"] == "processing"),
                    "members": [
                        {
                            "id": m["id"],
                            "resource": m["resource"],
                            "device": m["device"],
                            "state": "busy",
                            "busy": sum(
                                1
                                for w in workers
                                if w["group_id"] == g["id"]
                                and w["member_id"] == m["id"]
                                and w["status"] == "processing"
                            ),
                        }
                        for m in g["members"]
                    ],
                }
                for g in groups
            ]
        },
        "warnings": [],
    }
    page.route("**/api/worker-groups**", lambda r: _fulfill_json(r, state))
    page.route("**/api/jobs/workers", lambda r: _fulfill_json(r, {"workers": workers}))
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(app_url + "/")
    page.evaluate("localStorage.removeItem('workerGroupsExpanded')")
    page.reload()
    page.wait_for_selector("#workerGroupLiveRows [data-group-id]")


def _loudness_example() -> tuple[list[dict], list[dict]]:
    nvidia = _group("nv", "NVIDIA", 2, "gpu")
    intel = _group("intel", "UHD Graphics 770", 2, "gpu")
    cpu = _group("cpu", "CPU loudness", 7, types=("loudness",))
    workers = [_worker(nvidia, n, busy=False) for n in (1, 2)]
    workers += [_worker(intel, n, busy=False) for n in (3, 4)]
    workers += [
        _worker(cpu, n, busy=True, kind="loudness", current_phase="Loudness 1/1", ffmpeg_started=False)
        for n in range(5, 12)
    ]
    return [nvidia, intel, cpu], workers


@pytest.mark.e2e
class TestLayoutSwitch:
    def test_cards_layout_is_kept_when_every_member_has_four_or_fewer_workers(
        self, authed_page: Page, app_url: str
    ) -> None:
        cpu = _group("cpu", "CPU workers", 4)
        _open(authed_page, app_url, [cpu], [_worker(cpu, n, busy=n < 3) for n in range(1, 5)])
        expect(authed_page.locator("#workerGroupLiveRows.wg-dense")).to_have_count(0)
        expect(authed_page.locator("#workerGroupCols")).to_be_hidden()
        expect(authed_page.locator("#workerGroupLiveRows .worker-slot:visible")).to_have_count(4)
        expect(authed_page.locator("#workerGroupLiveRows .wg-idle-row:visible")).to_have_count(0)
        expect(authed_page.locator("#workerGroupLiveRows .wg-more:visible")).to_have_count(0)
        display = authed_page.locator("#workerGroupLiveRows").evaluate("e => getComputedStyle(e).display")
        assert display == "grid"
        idle_card = authed_page.locator('[data-worker-key="CPU_3"] [data-card]')
        expect(idle_card).to_have_class("card workers-panel-card wk idle")

    @pytest.mark.parametrize("count", [5, 7, 20])
    def test_table_layout_is_used_when_a_member_has_five_or_more_workers(
        self, authed_page: Page, app_url: str, count: int
    ) -> None:
        cpu = _group("cpu", "CPU workers", count)
        busy = 3
        _open(authed_page, app_url, [cpu], [_worker(cpu, n, busy=n <= busy) for n in range(1, count + 1)])
        expect(authed_page.locator("#workerGroupLiveRows.wg-dense")).to_have_count(1)
        rows = authed_page.locator(ROWS)
        expect(rows).to_have_count(busy)
        for n in range(1, busy + 1):
            row = authed_page.locator(f'[data-worker-key="CPU_{n}"]')
            expect(row.locator("[data-worker-id]")).to_have_text(f"#{n}")
            title = f"Episode {n} of a series with a rather long descriptive title"
            expect(row.locator("[data-title]")).to_have_text(title)
            expect(row.locator("[data-title]")).to_have_attribute("title", title)
            expect(row.locator("[data-kind-chip]")).to_be_visible()
            expect(row.locator("[data-progress-wrap]")).to_be_visible()
            expect(row.locator("[data-worker-job]")).to_have_text(JOB.format(n)[:8])
            expect(row.locator("[data-worker-logs]")).to_be_visible()
        idle = authed_page.locator("[data-group-idle='cpu:m1']")
        expect(idle).to_have_text(re.compile(rf"{count - busy} workers idle"))
        for n in range(busy + 1, count + 1):
            expect(idle.locator(".wg-idle-n", has_text=f"#{n}")).to_have_count(1)
        expect(authed_page.locator("[data-group-id='cpu'] .occ-chip")).to_contain_text(f"{busy}/ {count}")

    def test_three_plus_three_group_stays_cards_and_a_five_worker_member_turns_the_table_on(
        self, authed_page: Page, app_url: str
    ) -> None:
        group = _multi_group("g", "Mixed", [_member("m-gpu", 3, "gpu"), _member("m-cpu", 3)])
        workers = [_worker(group, n, busy=True, member=0) for n in (1, 2, 3)]
        workers += [_worker(group, n, busy=True, member=1) for n in (4, 5, 6)]
        _open(authed_page, app_url, [group], workers)
        expect(authed_page.locator("#workerGroupLiveRows.wg-dense")).to_have_count(0)
        expect(authed_page.locator("#workerGroupLiveRows .wg-mem")).to_have_count(2)
        group["members"][1]["count"] = 5
        workers += [_worker(group, n, busy=False, member=1) for n in (7, 8)]
        _open(authed_page, app_url, [group], workers)
        expect(authed_page.locator("#workerGroupLiveRows.wg-dense")).to_have_count(1)

    def test_panel_is_compact_for_the_seven_worker_loudness_example(self, authed_page: Page, app_url: str) -> None:
        groups, workers = _loudness_example()
        _open(authed_page, app_url, groups, workers)
        height = authed_page.locator("#workerStatusContainer").evaluate("e => e.getBoundingClientRect().height")
        # 600 before member rows existed; each of the three groups now carries one ~38 px device row.
        assert height < 700, height
        expect(authed_page.locator(ROWS)).to_have_count(7)
        expect(authed_page.locator("[data-group-idle='nv:m1']")).to_contain_text("2 workers idle")
        expect(authed_page.locator("[data-group-idle='intel:m1']")).to_contain_text("2 workers idle")
        expect(authed_page.locator("[data-group-idle='cpu:m1']")).to_be_hidden()


@pytest.mark.e2e
class TestNoOverflow:
    @pytest.mark.parametrize("width", [1920, 1600, 1440, 1280, 1100, 768, 390])
    def test_table_does_not_overflow_or_overlap_when_hardware_name_is_long(
        self, authed_page: Page, app_url: str, width: int
    ) -> None:
        groups, workers = _loudness_example()
        groups[1]["name"] = LONG_DEVICE
        _open(authed_page, app_url, groups, workers, width)
        problems = authed_page.evaluate(
            """() => {
                const out = [];
                if (document.documentElement.scrollWidth > innerWidth) out.push('page overflows');
                const panel = document.getElementById('workerStatusContainer').getBoundingClientRect();
                const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
                for (const row of document.querySelectorAll('.worker-slot')) {
                    if (!visible(row)) continue;
                    const cells = ['[data-worker-id]', '[data-file]', '[data-kind-chip]', '[data-metrics]',
                                   '[data-worker-job]', '[data-worker-logs]']
                        .map(s => row.querySelector(s)).filter(e => e && visible(e)).map(e => [e, e.getBoundingClientRect()]);
                    for (const [e, r] of cells) if (r.right > panel.right + 0.5 || r.left < panel.left - 0.5) out.push('outside ' + e.className);
                    for (let i = 0; i < cells.length; i++) for (let j = i + 1; j < cells.length; j++) {
                        const a = cells[i][1], b = cells[j][1];
                        if (a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5)
                            out.push('overlap ' + cells[i][0].className + ' / ' + cells[j][0].className);
                    }
                }
                for (const header of document.querySelectorAll('.worker-group-dashboard-header')) {
                    const r = header.getBoundingClientRect();
                    if (r.right > panel.right + 0.5) out.push('header outside');
                    for (const part of header.querySelectorAll('.g-name, .hw, .g-tools')) {
                        const p = part.getBoundingClientRect();
                        if (p.right > r.right + 0.5) out.push('header part outside ' + part.className);
                    }
                }
                return out;
            }"""
        )
        assert problems == []

    @pytest.mark.parametrize("width", [1440, 390])
    def test_expanded_twenty_worker_group_does_not_overflow(self, authed_page: Page, app_url: str, width: int) -> None:
        cpu = _group("cpu", "CPU workers", 20)
        _open(authed_page, app_url, [cpu], [_worker(cpu, n, busy=True) for n in range(1, 21)], width)
        authed_page.get_by_role("button", name="Show 12 more").click()
        expect(authed_page.locator(ROWS)).to_have_count(20)
        assert authed_page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.e2e
class TestRowCap:
    @pytest.mark.parametrize(("count", "label"), [(8, None), (9, "Show 1 more"), (20, "Show 12 more")])
    def test_expander_appears_only_when_rows_exceed_the_cap(
        self, authed_page: Page, app_url: str, count: int, label: str | None
    ) -> None:
        cpu = _group("cpu", "CPU workers", count)
        _open(authed_page, app_url, [cpu], [_worker(cpu, n, busy=True) for n in range(1, count + 1)])
        expect(authed_page.locator(ROWS)).to_have_count(min(count, ROW_CAP))
        more = authed_page.locator(".wg-more").first
        if label is None:
            expect(more).to_be_hidden()
        else:
            expect(more).to_contain_text(label)
            expect(more).to_have_attribute("aria-expanded", "false")

    def test_busy_workers_are_listed_before_idle_and_idle_are_never_rows(self, authed_page: Page, app_url: str) -> None:
        cpu = _group("cpu", "CPU workers", 12)
        workers = [_worker(cpu, n, busy=n in (10, 11, 12)) for n in range(1, 13)]
        _open(authed_page, app_url, [cpu], workers)
        keys = authed_page.locator(ROWS).evaluate_all(
            "els => els.sort((a, b) => Number(getComputedStyle(a).order) - Number(getComputedStyle(b).order))"
            ".map(e => e.dataset.workerKey)"
        )
        assert keys == ["CPU_10", "CPU_11", "CPU_12"]

    def test_running_rows_come_first_and_hidden_running_rows_are_counted_in_the_label(
        self, authed_page: Page, app_url: str
    ) -> None:
        cpu = _group("cpu", "CPU workers", 12)
        workers = [_worker(cpu, n, busy=True) for n in range(1, 13)]
        _open(authed_page, app_url, [cpu], workers)
        more = authed_page.locator(".wg-more").first
        expect(more).to_have_text("Show 4 more · 4 running")

    def test_fallback_row_is_shown_even_beyond_the_cap(self, authed_page: Page, app_url: str) -> None:
        cpu = _group("cpu", "CPU workers", 12)
        workers = [_worker(cpu, n, busy=True) for n in range(1, 13)]
        workers[11]["fallback_active"] = True
        workers[11]["fallback_reason"] = "GPU processing failed"
        _open(authed_page, app_url, [cpu], workers)
        expect(authed_page.locator('[data-worker-key="CPU_12"]')).to_be_visible()
        expect(authed_page.locator(ROWS)).to_have_count(ROW_CAP)
        expect(authed_page.locator(".wg-more").first).to_have_text("Show 4 more · 4 running")

    def test_cap_and_show_more_are_per_member_and_expansion_is_remembered_per_group_and_member(
        self, authed_page: Page, app_url: str
    ) -> None:
        group = _multi_group("g", "Mixed", [_member("m-gpu", 10, "gpu"), _member("m-cpu", 12)])
        workers = [_worker(group, n, busy=True, member=0) for n in range(1, 11)]
        workers += [_worker(group, n, busy=True, member=1) for n in range(11, 23)]
        _open(authed_page, app_url, [group], workers)
        gpu_more = authed_page.locator("[data-member-block='g:m-gpu'] .wg-more")
        cpu_more = authed_page.locator("[data-member-block='g:m-cpu'] .wg-more")
        expect(gpu_more).to_have_text("Show 2 more · 2 running")
        expect(cpu_more).to_have_text("Show 4 more · 4 running")
        expect(authed_page.locator("[data-group-workers='g:m-gpu'] .worker-slot:visible")).to_have_count(ROW_CAP)
        cpu_more.click()
        expect(authed_page.locator("[data-group-workers='g:m-cpu'] .worker-slot:visible")).to_have_count(12)
        expect(authed_page.locator("[data-group-workers='g:m-gpu'] .worker-slot:visible")).to_have_count(ROW_CAP)
        assert authed_page.evaluate("JSON.parse(localStorage.getItem('workerGroupsExpanded'))") == ["g:m-cpu"]
        authed_page.reload()
        authed_page.wait_for_selector("#workerGroupLiveRows [data-group-id]")
        expect(authed_page.locator("[data-group-workers='g:m-cpu'] .worker-slot:visible")).to_have_count(12)
        expect(authed_page.locator("[data-group-workers='g:m-gpu'] .worker-slot:visible")).to_have_count(ROW_CAP)

    def test_expanding_is_in_place_labelled_show_less_and_persists_across_reload(
        self, authed_page: Page, app_url: str
    ) -> None:
        cpu = _group("cpu", "CPU workers", 20)
        other = _group("other", "Second", 6)
        workers = [_worker(cpu, n, busy=True) for n in range(1, 21)] + [
            _worker(other, n + 100, busy=False) for n in range(6)
        ]
        _open(authed_page, app_url, [cpu, other], workers)
        more = authed_page.locator("[data-worker-group-shell='cpu'] .wg-more")
        section_top = authed_page.locator("[data-worker-group-shell='cpu']").evaluate(
            "e => e.getBoundingClientRect().top + scrollY"
        )
        more.click()
        expect(more).to_have_text("Show less")
        expect(more).to_have_attribute("aria-expanded", "true")
        expect(authed_page.locator("[data-worker-group-shell='cpu'] .worker-slot:visible")).to_have_count(20)
        assert (
            authed_page.locator("[data-worker-group-shell='cpu']").evaluate(
                "e => e.getBoundingClientRect().top + scrollY"
            )
            == section_top
        )
        authed_page.reload()
        authed_page.wait_for_selector("#workerGroupLiveRows [data-group-id]")
        expect(authed_page.locator("[data-worker-group-shell='cpu'] .worker-slot:visible")).to_have_count(20)
        authed_page.locator(".wg-more").first.click()
        expect(authed_page.locator("[data-worker-group-shell='cpu'] .worker-slot:visible")).to_have_count(ROW_CAP)
        assert authed_page.evaluate("JSON.parse(localStorage.getItem('workerGroupsExpanded'))") == []

    def test_expander_is_keyboard_operable_with_a_controlled_region(self, authed_page: Page, app_url: str) -> None:
        cpu = _group("cpu", "CPU workers", 10)
        _open(authed_page, app_url, [cpu], [_worker(cpu, n, busy=True) for n in range(1, 11)])
        more = authed_page.locator(".wg-more").first
        more.focus()
        authed_page.keyboard.press("Enter")
        expect(more).to_have_attribute("aria-expanded", "true")
        controls = more.get_attribute("aria-controls")
        assert authed_page.evaluate(
            "id => document.getElementById(id).classList.contains('worker-group-workers')", controls
        )

    def test_table_still_renders_when_storage_is_unavailable(self, authed_page: Page, app_url: str) -> None:
        cpu = _group("cpu", "CPU workers", 10)
        authed_page.add_init_script(
            "for (const m of ['getItem', 'setItem']) { const f = Storage.prototype[m];"
            " Storage.prototype[m] = function (k, ...a) {"
            " if (k === 'workerGroupsExpanded') throw new Error('denied'); return f.call(this, k, ...a); }; }"
        )
        _open(authed_page, app_url, [cpu], [_worker(cpu, n, busy=True) for n in range(1, 11)])
        authed_page.locator(".wg-more").first.click()
        expect(authed_page.locator(ROWS)).to_have_count(10)


@pytest.mark.e2e
class TestInteractions:
    def test_logs_button_opens_the_job_logs_like_the_card_button(self, authed_page: Page, app_url: str) -> None:
        cpu = _group("cpu", "CPU workers", 5)
        _open(authed_page, app_url, [cpu], [_worker(cpu, 1, busy=True)])
        authed_page.evaluate("window.__opened = []; window.openJobDetails = (...args) => window.__opened.push(args)")
        button = authed_page.get_by_role("button", name=f"View logs for job {JOB.format(1)}")
        expect(button).to_be_visible()
        authed_page.evaluate("window.__opened.length = 0")
        button.click()
        assert authed_page.evaluate("window.__opened") == [[JOB.format(1), "logs"]]

    def test_live_update_patches_the_row_in_place_without_duplicates(self, authed_page: Page, app_url: str) -> None:
        cpu = _group("cpu", "CPU workers", 5)
        workers = [_worker(cpu, n, busy=True) for n in range(1, 4)] + [_worker(cpu, n, busy=False) for n in (4, 5)]
        _open(authed_page, app_url, [cpu], workers)
        row = authed_page.locator('[data-worker-key="CPU_1"]')
        row.evaluate("e => { e.dataset.sentinel = '1'; }")
        updated = [dict(w) for w in workers]
        updated[0]["progress_percent"] = 77
        authed_page.evaluate("w => window.updateWorkerStatuses(w)", updated)
        expect(row.locator("[data-percent]")).to_have_text("77.0%")
        expect(row).to_have_attribute("data-sentinel", "1")
        expect(authed_page.locator(ROWS)).to_have_count(3)
        expect(authed_page.locator('[data-worker-key="CPU_1"]')).to_have_count(1)

    def test_worker_finishing_moves_to_the_idle_line(self, authed_page: Page, app_url: str) -> None:
        cpu = _group("cpu", "CPU workers", 5)
        workers = [_worker(cpu, n, busy=n <= 2) for n in range(1, 6)]
        _open(authed_page, app_url, [cpu], workers)
        workers[0] = _worker(cpu, 1, busy=False)
        authed_page.evaluate("w => window.updateWorkerStatuses(w)", workers)
        expect(authed_page.locator(ROWS)).to_have_count(1)
        expect(authed_page.locator("[data-group-idle='cpu:m1']")).to_contain_text("4 workers idle")


@pytest.mark.e2e
class TestContrast:
    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_row_text_meets_aa_contrast_when_theme_is_set(self, authed_page: Page, app_url: str, theme: str) -> None:
        groups, workers = _loudness_example()
        workers[4]["current_phase"] = "Loudness 1/1"
        _open(authed_page, app_url, groups, workers)
        authed_page.evaluate("t => document.documentElement.setAttribute('data-bs-theme', t)", theme)
        failures = authed_page.evaluate(
            """() => {
                const parse = c => c.match(/[\\d.]+/g).map(Number);
                const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
                const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
                const backdrop = el => {
                    let layers = [];
                    for (let e = el; e; e = e.parentElement) {
                        const c = parse(getComputedStyle(e).backgroundColor);
                        if (c.length === 4 && c[3] === 0) continue;
                        layers.push(c);
                        if (c.length === 3 || c[3] === 1) break;
                    }
                    let base = [255, 255, 255];
                    for (const c of layers.reverse()) { const a = c.length === 4 ? c[3] : 1; base = base.map((v, i) => v * (1 - a) + c[i] * a); }
                    return base;
                };
                const out = [];
                const sel = '.worker-slot [data-title], .worker-slot [data-library], .worker-slot [data-percent], .worker-slot [data-kind-chip],'
                    + ' .worker-slot [data-worker-job], .worker-slot [data-worker-logs], .worker-slot [data-worker-id], .g-name, .devname .nm, .wg-idle-row';
                for (const el of document.querySelectorAll(sel)) {
                    if (!el.getClientRects().length || !el.textContent.trim()) continue;
                    const fg = parse(getComputedStyle(el).color).slice(0, 3);
                    const bg = backdrop(el);
                    const a = lum(fg) + 0.05, b = lum(bg) + 0.05;
                    const ratio = Math.max(a, b) / Math.min(a, b);
                    if (ratio < 4.5) out.push(el.className + ' ' + ratio.toFixed(2));
                }
                return out;
            }"""
        )
        assert failures == []
