"""TEST_AUDIT Phase 4 — Workers panel render-contract tests.

Closes UI bug classes from recent commits:
  * 75c8da8 — Workers panel rows persist + show stable Device #N labels
  * e46e73c — Workers panel jitter (in-place updates, not wholesale rebuild)
  * 933a26d / 58829b2 — worker card current_phase rendering instead of "0.0%"
  * 1f09c3a — fallback badge / current_phase during slow reverse-lookup

Production at app.js:2087-2148 builds the row container ONCE, then for
each subsequent poll patches text/class on per-worker cached cards by
``data-worker-key`` lookup. Workers that vanish are removed by key;
new ones are appended. The contract this file pins:

1. Re-rendering with same workers DOES NOT recreate the per-worker
   <div data-worker-key="..."> nodes (they're patched in place)
2. Worker with ``current_phase`` set + ``ffmpeg_started`` false shows
   the phase text (NOT "0.0%" or "Working…")
3. Worker with ``fallback_active=true`` shows the CPU-fallback badge
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import mock_dashboard_defaults, mock_worker_groups


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestWorkersPanelInPlaceUpdate:
    """Workers panel must do per-card in-place updates, not wholesale
    ``container.innerHTML = ...`` rebuild every poll. Without this, the
    user sees panel jitter (cards visibly recreated each second) and
    selection / hover state evaporates mid-interaction.
    """

    def test_re_render_with_same_workers_preserves_card_node_identity(self, authed_page: Page, app_url: str) -> None:
        """Render N workers → tag each card with a sentinel attribute →
        re-render with same workers (different status) → assert sentinel
        SURVIVES. If the panel rebuilt wholesale, the sentinel would be
        gone (innerHTML on the row container would replace all children).

        This is the core contract from app.js:2009-2014: "successive
        polls don't blow away DOM nodes the user might be hovering /
        selecting."
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                const container = document.getElementById('workerStatusContainer');
                if (!container) return {error: 'no workerStatusContainer'};

                // First render: 2 workers (one GPU, one CPU).
                const initial = [
                    {worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                     status: 'idle', progress_percent: 0},
                    {worker_id: 'cpu-0', worker_type: 'CPU', worker_name: 'CPU 0',
                     status: 'idle', progress_percent: 0},
                ];
                window.updateWorkerStatuses(initial);

                // Tag each freshly-rendered card with a sentinel.
                const cards = container.querySelectorAll('[data-worker-key]');
                if (cards.length !== 2) {
                    return {error: `expected 2 cards, got ${cards.length}`};
                }
                cards.forEach(c => c.setAttribute('data-test-sentinel', '1'));

                // Re-render with SAME worker ids but DIFFERENT status.
                // In-place update should patch text/class only — sentinel
                // attributes on the existing cards must survive.
                const updated = [
                    {worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                     status: 'processing', progress_percent: 42, current_title: 'Test',
                     ffmpeg_started: true, speed: '5.2x'},
                    {worker_id: 'cpu-0', worker_type: 'CPU', worker_name: 'CPU 0',
                     status: 'processing', progress_percent: 88, current_title: 'Test 2',
                     ffmpeg_started: true, speed: '1.1x'},
                ];
                window.updateWorkerStatuses(updated);

                // Count cards that STILL carry the sentinel.
                const survivingCards = container.querySelectorAll('[data-worker-key][data-test-sentinel="1"]');
                return {
                    initialCount: cards.length,
                    survivingCount: survivingCards.length,
                };
            }
            """
        )

        assert result.get("error") is None, f"Setup failed: {result.get('error')!r}"
        assert result["initialCount"] == 2, f"Initial render count wrong: {result['initialCount']}"
        assert result["survivingCount"] == 2, (
            f"In-place update must preserve card node identity; "
            f"got {result['survivingCount']} of {result['initialCount']} cards still tagged. "
            f"A regression that wholesale-rebuilds the container (innerHTML = ...) would "
            f"strip the sentinel, jitter the panel, and destroy any user selection / hover "
            f"state mid-interaction (commits e46e73c, 75c8da8)."
        )

    def test_vanished_worker_card_is_removed_by_key(self, authed_page: Page, app_url: str) -> None:
        """Worker that disappears from the snapshot (job ended, pool resize)
        is removed from the panel. App.js:2144-2148 walks row.children and
        removes any whose data-worker-key isn't in the current snapshot.

        Pin this so a regression that always-keeps-cards leaves stale rows
        forever; or one that always-rebuilds breaks the in-place contract
        above.
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                const container = document.getElementById('workerStatusContainer');
                if (!container) return {error: 'no workerStatusContainer'};

                window.updateWorkerStatuses([
                    {worker_id: 'a', worker_type: 'GPU', worker_name: 'A', status: 'idle', progress_percent: 0},
                    {worker_id: 'b', worker_type: 'GPU', worker_name: 'B', status: 'idle', progress_percent: 0},
                    {worker_id: 'c', worker_type: 'GPU', worker_name: 'C', status: 'idle', progress_percent: 0},
                ]);
                const beforeCount = container.querySelectorAll('[data-worker-key]').length;

                // Re-render with only 'a' and 'c' — 'b' must be removed.
                window.updateWorkerStatuses([
                    {worker_id: 'a', worker_type: 'GPU', worker_name: 'A', status: 'idle', progress_percent: 0},
                    {worker_id: 'c', worker_type: 'GPU', worker_name: 'C', status: 'idle', progress_percent: 0},
                ]);
                const afterCount = container.querySelectorAll('[data-worker-key]').length;
                const hasB = !!container.querySelector('[data-worker-key$="_b"]');
                return {beforeCount, afterCount, hasB};
            }
            """
        )

        assert result.get("error") is None
        assert result["beforeCount"] == 3, f"Initial render: expected 3 cards, got {result['beforeCount']}"
        assert result["afterCount"] == 2, (
            f"After removing worker 'b': expected 2 cards, got {result['afterCount']}. "
            f"Stale cards left behind would mislead the user about pool size."
        )
        assert result["hasB"] is False, "Worker 'b' card was not removed — vanished workers must be cleaned up"


@pytest.mark.e2e
class TestWorkerCardPhaseRendering:
    """Worker card with ``current_phase`` set + ``ffmpeg_started=false``
    must render the phase string (e.g. "Resolving item id on Jellyfin…")
    INSTEAD of the misleading "0.0% / 0.0x". Production at app.js:2263-2271
    swaps the percent label for the phase text in this state.

    Closes commit 933a26d (worker progress stayed at 0.0% during pre-FFmpeg
    phases) + 58829b2 (real sub-phase string in worker card).
    """

    def test_pre_ffmpeg_phase_renders_phase_text_not_zero_percent(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                window.updateWorkerStatuses([{
                    worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                    status: 'processing',
                    current_title: 'Some movie',
                    current_phase: 'Resolving item id on Jellyfin…',
                    ffmpeg_started: false,
                    progress_percent: 0,
                    speed: null,
                }]);
                const card = document.querySelector('[data-worker-key$="_gpu-0"]');
                if (!card) return {error: 'no card rendered'};
                const percent = card.querySelector('[data-percent]');
                const speed = card.querySelector('[data-speed]');
                return {
                    percentText: percent ? percent.textContent : null,
                    speedDisplay: speed ? speed.parentElement.style.display : null,
                };
            }
            """
        )

        assert result.get("error") is None, f"Setup failed: {result.get('error')!r}"
        assert result["percentText"] == "Resolving item id on Jellyfin…", (
            f"Pre-FFmpeg phase must render the phase text in place of the percent label; "
            f"got {result['percentText']!r}. The misleading '0.0%' is what the user reported "
            f"as 'worker is hung' (commit 933a26d) — this rendering distinguishes the two."
        )
        # Speed chip is hidden during pre-FFmpeg phase (no meaningful value).
        assert result["speedDisplay"] == "none", (
            f"Speed chip must be hidden during pre-FFmpeg phase; got display={result['speedDisplay']!r}"
        )

    def test_ffmpeg_started_phase_shows_percent_and_speed_normally(self, authed_page: Page, app_url: str) -> None:
        """Mirror test for the contract floor: when FFmpeg HAS started,
        percent + speed render normally (NOT phase text). Without this,
        a regression that always-shows-phase would mask real FFmpeg
        progress with stale phase text.
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                window.updateWorkerStatuses([{
                    worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                    status: 'processing',
                    current_title: 'Some movie',
                    current_phase: 'Resolving item id on Jellyfin…',  // would be stale
                    ffmpeg_started: true,
                    progress_percent: 42.5,
                    speed: '5.2x',
                }]);
                const card = document.querySelector('[data-worker-key$="_gpu-0"]');
                if (!card) return {error: 'no card rendered'};
                const percent = card.querySelector('[data-percent]');
                const speed = card.querySelector('[data-speed]');
                return {
                    percentText: percent ? percent.textContent : null,
                    speedText: speed ? speed.textContent : null,
                    speedDisplay: speed ? speed.parentElement.style.display : null,
                };
            }
            """
        )

        assert result.get("error") is None
        assert result["percentText"] == "42.5%", (
            f"FFmpeg-started worker must show percent (NOT phase); got {result['percentText']!r}"
        )
        assert result["speedText"] == "5.2x", f"FFmpeg-started worker must show speed; got {result['speedText']!r}"
        assert result["speedDisplay"] != "none", (
            f"Speed chip must be visible during FFmpeg phase; got display={result['speedDisplay']!r}"
        )

    def test_fallback_active_renders_cpu_fallback_badge(self, authed_page: Page, app_url: str) -> None:
        """Worker mid-CPU-fallback must show the warning badge so an op
        scanning the panel can spot it. App.js:2199-2208 toggles the
        d-none class on data-fallback-badge based on worker.fallback_active.
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                window.updateWorkerStatuses([{
                    worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                    status: 'processing',
                    current_title: 'HEVC movie',
                    fallback_active: true,
                    fallback_reason: 'GPU rejected HEVC; retrying on CPU',
                    ffmpeg_started: true,
                    progress_percent: 12,
                }]);
                const card = document.querySelector('[data-worker-key$="_gpu-0"]');
                if (!card) return {error: 'no card rendered'};
                const badge = card.querySelector('[data-fallback-badge]');
                const note = card.querySelector('[data-fallback-note]');
                return {
                    badgeHidden: badge ? badge.classList.contains('d-none') : null,
                    noteHidden: note ? note.classList.contains('d-none') : null,
                    noteText: note ? note.textContent.trim() : null,
                };
            }
            """
        )

        assert result.get("error") is None
        assert result["badgeHidden"] is False, (
            f"CPU-fallback badge must be visible (d-none REMOVED) when fallback_active=true; "
            f"got d-none={result['badgeHidden']!r}"
        )
        assert result["noteHidden"] is False, "Fallback reason note must be visible when present"
        assert result["noteText"] and "HEVC" in result["noteText"], (
            f"Fallback note must include the reason text so op can diagnose; got {result['noteText']!r}"
        )

    def test_fallback_toast_quotes_the_file_that_fell_back(self, authed_page: Page, app_url: str) -> None:
        """A short clip's CPU rerun finishes inside one poll, so the first poll
        that sees ``fallback_active`` finds the worker idle with ``current_title``
        blank. The toast must still name the file (live bug: ``for "this file"``).
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        reason = (
            "GPU processing failed (...) for /media/proof/Proof Movies/AV1 Clip 3 (2019)/AV1 Clip 3 (2019).mkv "
            "(exit code 255)"
        )
        result = authed_page.evaluate(
            """
            (reason) => {
                window.updateWorkerStatuses([{
                    worker_id: 1, worker_type: 'GPU', worker_name: 'GPU Worker 1 (Quadro P5000)',
                    status: 'idle',
                    current_title: '',
                    fallback_active: true,
                    fallback_reason: reason,
                    fallback_title: 'AV1 Clip 3 (2019)',
                    ffmpeg_started: false,
                    progress_percent: 0,
                }]);
                return {
                    title: document.getElementById('toastTitle').textContent,
                    body: document.getElementById('toastBody').textContent,
                };
            }
            """,
            reason,
        )

        assert result["title"] == "Switched to CPU"
        assert result["body"] == f'GPU Worker 1 (Quadro P5000) fell back to CPU for "AV1 Clip 3 (2019)" — {reason}'


@pytest.mark.e2e
class TestChapterWorkerProgress:
    @pytest.mark.parametrize("width", [390, 1440])
    @pytest.mark.parametrize("ffmpeg_started", [False, True])
    def test_chapter_counts_override_scrubber_progress_and_fit_card(
        self, authed_page: Page, app_url: str, width: int, ffmpeg_started: bool
    ) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.set_viewport_size({"width": width, "height": 900})
        authed_page.goto(f"{app_url}/")
        result = authed_page.evaluate(
            """(ffmpegStarted) => {
                const worker = {
                    worker_id: 'chapter-test', worker_type: 'GPU',
                    worker_name: 'GPU Worker 1 (NVIDIA TITAN RTX)', status: 'processing',
                    current_title: 'The Bourne Ultimatum', ffmpeg_started: ffmpegStarted,
                    progress_percent: 100, speed: '9.2x', eta: '0s',
                    chapter_progress: {stage: 'extracting', processed: 10, total: 40, ready: 9, failed: 1},
                };
                window.updateWorkerStatuses([worker]);
                const card = document.querySelector('[data-worker-key$="_chapter-test"]');
                const metrics = card.querySelector('[data-metrics]');
                const first = {
                    percent: card.querySelector('[data-percent]').textContent,
                    stage: card.querySelector('[data-chapter-stage]').textContent,
                    width: card.querySelector('[data-progress]').style.width,
                    accessibleValue: card.querySelector('[data-progress-wrap]').getAttribute('aria-valuenow'),
                    speedHidden: card.querySelector('[data-speed]').parentElement.style.display === 'none',
                    etaHidden: card.querySelector('[data-eta]').parentElement.style.display === 'none',
                    cpuIcon: card.querySelector('[data-icon]').classList.contains('bi-cpu'),
                    fits: metrics.scrollWidth <= metrics.clientWidth,
                };
                worker.chapter_progress = {stage: 'registering', processed: 40, total: 40, ready: 40, failed: 0};
                window.updateWorkerStatuses([worker]);
                const registering = card.querySelector('[data-chapter-stage]').textContent;
                worker.status = 'idle';
                window.updateWorkerStatuses([worker]);
                return {...first, registering,
                    idleHidden: card.querySelector('[data-metrics]').hidden,
                    idleChapterHidden: card.querySelector('[data-chapter-stage]').classList.contains('d-none'),
                    idleWidth: card.querySelector('[data-progress]').style.width,
                };
            }""",
            ffmpeg_started,
        )
        assert result == {
            "percent": "Chapters 10/40 · 25%",
            "stage": "Generating · 1 failed",
            "width": "25%",
            "accessibleValue": "25.0",
            "speedHidden": True,
            "etaHidden": True,
            "cpuIcon": True,
            "fits": True,
            "registering": "Registering with Plex",
            "idleHidden": True,
            "idleChapterHidden": True,
            "idleWidth": "0%",
        }

    @pytest.mark.parametrize("stage", [None, "preparing", "waiting"])
    def test_unknown_chapter_count_is_indeterminate_and_resets_for_video(
        self, authed_page: Page, app_url: str, stage: str | None
    ) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        result = authed_page.evaluate(
            """(stage) => {
                const worker = {
                    worker_id: 'chapter-test', worker_type: 'GPU', worker_name: 'GPU Worker 1',
                    status: 'processing', current_title: 'Movie', ffmpeg_started: true,
                    current_phase: 'Chapter thumbnails for Plex…', progress_percent: 100, speed: '7.0x',
                    chapter_progress: stage ? {stage, processed: 0, total: 0, ready: 0, failed: 0} : null,
                };
                window.updateWorkerStatuses([worker]);
                const card = document.querySelector('[data-worker-key$="_chapter-test"]');
                const first = {
                    animated: card.querySelector('[data-progress]').classList.contains('progress-bar-animated'),
                    value: card.querySelector('[data-progress-wrap]').getAttribute('aria-valuenow'),
                    percent: card.querySelector('[data-percent]').textContent,
                    stage: card.querySelector('[data-chapter-stage]').textContent,
                    speedHidden: card.querySelector('[data-speed]').parentElement.style.display === 'none',
                };
                Object.assign(worker, {chapter_progress: null, current_phase: '', progress_percent: 12.5});
                window.updateWorkerStatuses([worker]);
                return {...first,
                    videoPercent: card.querySelector('[data-percent]').textContent,
                    videoAnimated: card.querySelector('[data-progress]').classList.contains('progress-bar-animated'),
                    videoSpeedVisible: card.querySelector('[data-speed]').parentElement.style.display !== 'none',
                };
            }""",
            stage,
        )
        assert result["animated"] is True
        assert result["value"] is None
        assert "%" not in result["percent"]
        assert result["speedHidden"] is True
        if stage == "waiting":
            assert result["stage"] == "Waiting for chapter access"
        assert result["videoPercent"] == "12.5%"
        assert result["videoAnimated"] is False
        assert result["videoSpeedVisible"] is True


@pytest.mark.e2e
class TestMemberRows:
    """One card per group; each member is a sub-row with its own busy/count and stepper."""

    @staticmethod
    def _open(page: Page, app_url: str, *, extra_members: list[dict] | None = None) -> dict:
        mock_dashboard_defaults(page)
        api = mock_worker_groups(page, cpu_count=2)
        state = api["state"]
        group = state["groups"][0]
        group["members"] += extra_members or []
        state["capacity"]["groups"] = [
            {
                "id": "cpu",
                "state": "active",
                "busy": 1,
                "available": 1,
                "finishing": 0,
                "members": [
                    {"id": "m1", "resource": "cpu", "device": None, "state": "active", "busy": 1},
                    *[
                        {"id": m["id"], "resource": m["resource"], "device": m["device"], "state": "active", "busy": 0}
                        for m in (extra_members or [])
                    ],
                ],
            }
        ]
        page.goto(f"{app_url}/")
        page.wait_for_selector("#workerGroupLiveRows [data-group-id]")
        return api

    def test_members_render_under_one_group_card(self, authed_page: Page, app_url: str) -> None:
        gpu = {"id": "m-gpu", "resource": "gpu", "device": "/dev/nvidia0", "count": 3, "job_types": ["previews"]}
        self._open(authed_page, app_url, extra_members=[gpu])
        section = authed_page.locator("[data-worker-group-shell='cpu']")
        expect(authed_page.locator("[data-worker-group-shell='cpu']")).to_have_count(1)
        expect(section.locator(".wg-mem")).to_have_count(2)
        expect(section.locator("[data-group-indicator='configured']")).to_contain_text("1/ 5")
        expect(section.locator("[data-member-block='cpu:m-gpu'] .devname .nm")).to_have_text("GPU 0")
        expect(section.locator("[data-member-block='cpu:m-gpu'] [data-member-indicator]")).to_contain_text("0/ 3")

    def test_member_stepper_posts_to_the_member_scale_route(self, authed_page: Page, app_url: str) -> None:
        api = self._open(authed_page, app_url)
        block = authed_page.locator("[data-member-block='cpu:m1']")
        block.locator("[data-member-scale='1']").click()
        expect(block.locator("output")).to_have_text("3")
        write = api["writes"][-1]
        assert write["url"] == f"{app_url}/api/worker-groups/cpu/members/m1/scale"
        assert write["method"] == "POST"
        assert write["body"] == {"delta": 1}
        block.locator("[data-member-scale='-1']").click()
        assert api["writes"][-1]["body"] == {"delta": -1}
        assert api["state"]["groups"][0]["members"][0]["count"] == 2

    def test_stepper_minus_is_disabled_at_one_worker(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        mock_worker_groups(authed_page, cpu_count=1)
        authed_page.goto(f"{app_url}/")
        minus = authed_page.locator("[data-member-block='cpu:m1'] [data-member-scale='-1']")
        expect(minus).to_be_disabled()

    def test_missing_gpu_member_shows_not_detected_chip_and_group_keeps_working(
        self, authed_page: Page, app_url: str
    ) -> None:
        gone = {"id": "m-gone", "resource": "gpu", "device": "/dev/nvidia9", "count": 1, "job_types": ["previews"]}
        self._open(authed_page, app_url, extra_members=[gone])
        block = authed_page.locator("[data-member-block='cpu:m-gone']")
        expect(block.locator(".chip.warn")).to_contain_text("Not detected")
        expect(block).to_contain_text("Its jobs wait; other devices keep working.")
        expect(authed_page.locator("[data-member-block='cpu:m1'] .chip")).to_have_count(0)

    def test_removed_member_still_finishing_shows_row_without_stepper(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        api = mock_worker_groups(authed_page, cpu_count=2)
        api["state"]["capacity"]["groups"] = [
            {
                "id": "cpu",
                "state": "active",
                "busy": 1,
                "finishing": 1,
                "members": [
                    {"id": "m1", "resource": "cpu", "device": None, "state": "active", "busy": 0},
                    {"id": "m-old", "resource": "gpu", "device": "/dev/nvidia0", "state": "draining", "finishing": 1},
                ],
            }
        ]
        authed_page.goto(f"{app_url}/")
        block = authed_page.locator("[data-member-block='cpu:m-old']")
        expect(block).to_contain_text("Removed device")
        expect(block).to_contain_text("1 finishing")
        expect(block.locator("button:visible")).to_have_count(0)

    def test_disabled_group_shows_one_enable_button_and_no_member_steppers(
        self, authed_page: Page, app_url: str
    ) -> None:
        mock_dashboard_defaults(authed_page)
        api = mock_worker_groups(authed_page, cpu_count=0)
        authed_page.goto(f"{app_url}/")
        section = authed_page.locator("[data-worker-group-shell='cpu']")
        expect(section.locator("[data-member-scale]")).to_have_count(0)
        section.locator("[data-group-enable]").click()
        assert api["writes"][-1]["url"] == f"{app_url}/api/worker-groups/cpu/scale"
        assert api["writes"][-1]["body"] == {"enabled": True}

    def test_system_card_is_read_only_with_one_line_per_group(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        mock_worker_groups(authed_page, cpu_count=4)
        authed_page.goto(f"{app_url}/")
        line = authed_page.locator("[data-system-group='cpu']")
        expect(line.locator("small")).to_have_text("4 CPU")
        expect(authed_page.locator("#systemWorkerGroups .stepper")).to_have_count(0)
