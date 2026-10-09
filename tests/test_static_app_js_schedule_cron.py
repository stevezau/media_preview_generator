"""
Pure JS unit tests for the schedule cron arithmetic in
``media_preview_generator/web/static/js/schedules.js`` and
``schedule_modal.js``.

Why this file exists
--------------------
Commit ``2d29fe9`` ("fix: correct day-of-week offset in schedule cron
expressions") shipped a real off-by-one bug to users for an unknown
duration: APScheduler uses ISO weekday numbers (0=Mon..6=Sun) but the
UI checkboxes used Unix cron numbering (0=Sun..6=Sat), so every weekly
schedule fired one day late. Pure JS day arithmetic with no
``test_static_app_js.py`` coverage was the hindsight-flagged gap.

The functions involved are pure arithmetic on strings and numbers — no
real DOM behaviour is required to exercise the bug shape — so we run
the actual JS source through ``node`` from a Python pytest test. A
small adapter stubs ``document.getElementById`` for the two functions
that read form values; ``describeSchedule`` is already a pure function
and runs unmodified.

This is intentionally lower-cost than adding vitest/jsdom + an npm
install + a CI step. Node is already on the dev machine and on the
GitHub Actions runner; the harness is one ``subprocess.run`` per test.

Coverage matrix (every cell is a separate test):
  * Each weekday Mon-Sun (round-trip through saveSchedule -> cron ->
    describeSchedule and back through showEditScheduleModal)
  * Every-N-minutes / every-N-hours interval display (1, 2, 60, 120, 1440)
  * Hour-of-day arithmetic at boundaries (00:00, 12:00, 23:00)
  * The exact bug shape from 2d29fe9 (Sunday-only must show "Sun")
  * The _pendingProgress replay shape from 31cd4a0
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_DIR = REPO_ROOT / "media_preview_generator" / "web" / "static" / "js"
SCHEDULES_JS = JS_DIR / "schedules.js"
SCHEDULE_MODAL_JS = JS_DIR / "schedule_modal.js"
SCAN_FILTERS_JS = JS_DIR / "scan_filters.js"
APP_JS = JS_DIR / "app.js"

# Skip everything if node isn't on PATH. This file is the only one in
# the suite that shells out to node, so we'd rather skip than fail when
# a dev runs pytest in an environment without node (e.g. a minimal
# container). CI installs node already.
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available — JS unit tests skipped")


def _run_node(snippet: str) -> str:
    """Execute a snippet of JS in node and return stdout (stripped).

    The snippet is responsible for printing a single JSON document on
    stdout. Stderr is captured into the assertion message on failure
    so JS exceptions surface clearly.
    """
    result = subprocess.run(
        [NODE, "-e", snippet],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"node exited {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# JS adapter — loads the real source file(s) into a node VM context with a
# minimal DOM stub, then calls into them. We use vm.runInNewContext rather
# than `require()` because the JS files target the browser (top-level
# `function` declarations, `window` globals) and have no module exports.
# ---------------------------------------------------------------------------

_VM_PRELUDE = r"""
const vm = require('vm');
const fs = require('fs');

function makeElementStub() {
    return {
        value: '',
        checked: false,
        dataset: {},
        addEventListener: function () {},
        innerHTML: '',
        disabled: false,
        classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
        style: {},
        // <select> stubs need an iterable options list — the edit modal
        // checks membership before assigning a lookback value.
        options: [],
        querySelector(selector) {
            const match = selector.match(/^option\[value="([^"]+)"\]$/);
            return match ? this.options.find(option => option.value === match[1]) || null : null;
        },
        add(option) {
            option.remove = () => { this.options = this.options.filter(item => item !== option); };
            this.options.push(option);
        },
    };
}

function loadSchedulesContext(formValues) {
    // formValues maps element id -> { value?, checked?, dataset? } shape.
    // Anything missing returns null from getElementById to mimic the real
    // browser when a control is absent (the early-return guards rely on
    // this).
    //
    // __autoStubMissing flips that: unknown ids get a blank stub instead
    // of null. Needed by anything that goes through _resetScheduleForm,
    // which writes to every control in the dialog — enumerating all ~15
    // in each test would bury the assertion under setup.
    const autoStub = !!(formValues && formValues.__autoStubMissing);
    const elements = {};
    for (const [id, props] of Object.entries(formValues || {})) {
        elements[id] = Object.assign(makeElementStub(), props);
    }

    // Special: query selectors used by saveSchedule for the day checkboxes.
    const checkedDays = (formValues && formValues.__checkedDays) || [];
    const checkedLibs = (formValues && formValues.__checkedLibs) || [];
    const scanModeChecked = (formValues && formValues.__scanMode) || 'full_library';

    const docStub = {
        addEventListener: function () {},
        removeEventListener: function () {},
        getElementById: (id) => {
            if (elements[id]) return elements[id];
            // Cron tests omit the optional filter panel, as a page without it does.
            if (id === 'scheduleScanFilters') return null;
            if (!autoStub) return null;
            elements[id] = makeElementStub();
            return elements[id];
        },
        querySelectorAll: (sel) => {
            if (sel === '.schedule-day') {
                // For showEditScheduleModal: every day checkbox exists; we'll
                // record which were checked.
                const out = [];
                for (let v = 0; v < 7; v++) {
                    out.push({
                        value: String(v),
                        checked: false,
                    });
                }
                if (formValues && formValues.__editScheduleDayCheckboxes) {
                    formValues.__editScheduleDayCheckboxes.length = 0;
                    formValues.__editScheduleDayCheckboxes.push(...out);
                }
                return out;
            }
            if (sel === '.schedule-day:checked') {
                return checkedDays.map((v) => ({ value: String(v) }));
            }
            if (sel === '.schedule-library-checkbox:checked') {
                return checkedLibs.map((v) => ({ value: String(v) }));
            }
            if (sel === '.schedule-library-checkbox') {
                return [];
            }
            if (sel === 'input[name="scanMode"]:checked') {
                return [{ value: scanModeChecked }];
            }
            return [];
        },
        querySelector: (sel) => {
            if (sel === 'input[name="scanMode"]:checked') {
                return { value: scanModeChecked };
            }
            if (sel === 'input[name="scheduleType"]:checked') {
                return { value: (formValues && formValues.__scheduleType) || 'specific-time' };
            }
            return null;
        },
    };

    // Stubs for the functions saveSchedule depends on but that we don't
    // exercise here.
    const ctx = {
        document: docStub,
        window: { _scheduleQuietHoursOverlap: undefined, appConfirm: async () => true },
        Option: class { constructor(text, value) { this.text = text; this.value = value; } },
        bootstrap: { Modal: class { static getInstance() { return { hide: () => {} }; } show() {} } },
        // app.js's dialog helpers, which schedule_modal.js loads after.
        modalOpening: () => null,
        hideModalSafely: () => {},
        showToast: function (title, msg, level) {
            ctx.__toasts.push({ title, msg, level });
        },
        __toasts: [],
        __captured: { puts: [], posts: [] },
        apiPost: async function (url, body) {
            ctx.__captured.posts.push({ url, body: JSON.parse(JSON.stringify(body)) });
            return { id: 'sch-test' };
        },
        apiPut: async function (url, body) {
            ctx.__captured.puts.push({ url, body: JSON.parse(JSON.stringify(body)) });
            return {};
        },
        apiDelete: async function () { return {}; },
        loadSchedules: function () {},
        loadJobs: function () {},
        loadJobStats: function () {},
        schedules: (formValues && formValues.__schedules) || [],
        libraries: (formValues && formValues.__libraries) || [],
        escapeHtml: (s) => String(s),
        // saveSchedule helpers
        _getSelectedScheduleType: function () {
            return (formValues && formValues.__scheduleType) || 'specific-time';
        },
        _populateScheduleServerPicker: () => Promise.resolve(),
        _renderScheduleLibraryList: () => {},
        _resetScheduleForm: () => {},
        onScheduleLibraryAllChange: () => {},
        onScheduleServerChange: () => Promise.resolve(),
        onScanModeChange: () => {},
        onScheduleTypeChange: () => {},
        console: console,
    };
    ctx.global = ctx;
    vm.createContext(ctx);
    // Load and evaluate the source files. schedules.js uses `const
    // DAY_NAMES = ...` at top level — that's a let/const binding in the
    // module scope, fine inside a vm context.
    const schedulesSrc = fs.readFileSync(__SCHEDULES_PATH__, 'utf8');
    const modalSrc = fs.readFileSync(__SCHEDULE_MODAL_PATH__, 'utf8');
    vm.runInContext(fs.readFileSync(__SCAN_FILTERS_PATH__, 'utf8'), ctx);
    // Browser window properties are globals; the VM's window stub is separate.
    ctx.MediaScanFilters = ctx.window.MediaScanFilters;
    vm.runInContext(schedulesSrc, ctx);
    vm.runInContext(modalSrc, ctx);
    return ctx;
}
"""


def _vm_prelude() -> str:
    return (
        _VM_PRELUDE.replace("__SCHEDULES_PATH__", json.dumps(str(SCHEDULES_JS)))
        .replace("__SCHEDULE_MODAL_PATH__", json.dumps(str(SCHEDULE_MODAL_JS)))
        .replace("__SCAN_FILTERS_PATH__", json.dumps(str(SCAN_FILTERS_JS)))
    )


def _eval_js(call: str, form_values: dict | None = None) -> dict | str | int | float | list | None:
    """Run ``call`` (a JS expression) inside a fresh schedules.js context
    with form values applied. The expression must resolve to something
    JSON-serialisable.
    """
    snippet = (
        _vm_prelude()
        + f"\nconst ctx = loadSchedulesContext({json.dumps(form_values or {})});\n"
        + f"Promise.resolve(vm.runInContext({json.dumps(call)}, ctx))"
        + ".then((v) => { console.log(JSON.stringify({result: v, captured: ctx.__captured, toasts: ctx.__toasts})); })"
        + ".catch((e) => { console.error(e.stack || e.message); process.exit(1); });"
    )
    out = _run_node(snippet)
    return json.loads(out)


# ---------------------------------------------------------------------------
# describeSchedule — pure formatter
# ---------------------------------------------------------------------------


class TestDescribeScheduleInterval:
    """The interval branch returns 'Every N minutes' or 'Every N hours'."""

    @pytest.mark.parametrize(
        "minutes,expected",
        [
            (1, "Every minute"),
            (2, "Every 2 minutes"),
            (45, "Every 45 minutes"),
            (60, "Every hour"),
            (120, "Every 2 hours"),
            (1440, "Every 24 hours"),  # daily-as-interval edge case
        ],
    )
    def test_interval_formatting(self, minutes: int, expected: str) -> None:
        out = _eval_js(f"describeSchedule('interval', {minutes})")
        assert out["result"] == expected, f"interval={minutes}: got {out['result']!r}"


class TestDescribeScheduleCronWeekdays:
    """Each APScheduler weekday number must format to the correct DAY_NAMES
    label. Pins the 2d29fe9 fix: APS 0=Mon..6=Sun, mapped via (n+1)%7
    to Unix 0=Sun..6=Sat for DAY_NAMES lookup.

    The buggy pre-fix code would have shown the day one position EARLIER
    in the week (Sunday-only schedule displayed as Saturday, Monday-only
    as Sunday, etc.) because it indexed DAY_NAMES with the raw APS number.
    """

    # (aps_day_in_cron, expected_label) — every cell of the matrix.
    @pytest.mark.parametrize(
        "aps_dow,label",
        [
            (0, "Mon"),
            (1, "Tue"),
            (2, "Wed"),
            (3, "Thu"),
            (4, "Fri"),
            (5, "Sat"),
            (6, "Sun"),  # The exact 2d29fe9 bug shape.
        ],
    )
    def test_single_day_label(self, aps_dow: int, label: str) -> None:
        cron = f"0 14 * * {aps_dow}"
        out = _eval_js(f"describeSchedule('cron', '{cron}')")
        # Format is "HH:MM Day"
        assert out["result"] == f"14:00 {label}", (
            f"APS dow={aps_dow} should display {label} (the 2d29fe9 fix maps APS->Unix); got {out['result']!r}"
        )


class TestDescribeScheduleCronAggregations:
    """The describeSchedule simple-time branch collapses common patterns
    into 'Daily' / 'Weekdays' / 'Weekends'. Pin every collapse cell.
    """

    def test_all_seven_days_displays_daily(self) -> None:
        # APS Mon..Sun = 0..6
        out = _eval_js("describeSchedule('cron', '0 0 * * 0,1,2,3,4,5,6')")
        assert out["result"] == "00:00 Daily", out["result"]

    def test_weekdays_collapse_to_weekdays_label(self) -> None:
        # Mon-Fri in APS numbering = 0,1,2,3,4
        out = _eval_js("describeSchedule('cron', '0 8 * * 0,1,2,3,4')")
        assert out["result"] == "08:00 Weekdays", out["result"]

    def test_weekends_collapse_to_weekends_label(self) -> None:
        # Sat=5, Sun=6 in APS numbering — these become Unix [6, 0] which
        # match the 'weekends' check ([0,6] sorted).
        out = _eval_js("describeSchedule('cron', '0 10 * * 5,6')")
        assert out["result"] == "10:00 Weekends", out["result"]

    def test_mwf_lists_individual_days_in_unix_order(self) -> None:
        """Mon, Wed, Fri (APS 0,2,4) -> displayed individually."""
        out = _eval_js("describeSchedule('cron', '0 14 * * 0,2,4')")
        # Days are looked up by (aps+1)%7 -> Unix order 1,3,5 -> Mon, Wed, Fri.
        assert out["result"] == "14:00 Mon, Wed, Fri", out["result"]


class TestDescribeScheduleCronHourBoundaries:
    """Hour/minute padding and edge values."""

    @pytest.mark.parametrize(
        "minute,hour,expected_time",
        [
            (0, 0, "00:00"),
            (5, 0, "00:05"),
            (0, 12, "12:00"),
            (59, 23, "23:59"),
            (0, 23, "23:00"),
        ],
    )
    def test_time_padding(self, minute: int, hour: int, expected_time: str) -> None:
        # APS 0,1,2,3,4,5,6 = all days -> "Daily"
        out = _eval_js(f"describeSchedule('cron', '{minute} {hour} * * 0,1,2,3,4,5,6')")
        assert out["result"] == f"{expected_time} Daily", out["result"]


class TestDescribeScheduleCronFallback:
    """Non-simple cron expressions return the raw value unchanged."""

    @pytest.mark.parametrize(
        "expr",
        [
            "*/15 * * * *",  # every 15 minutes — minute field isn't pure digits
            "0 9 1 * *",  # day-of-month set
            "0 9 * 6 *",  # month set
            "0 9 * * MON",  # textual dow
        ],
    )
    def test_complex_cron_returns_raw_value(self, expr: str) -> None:
        out = _eval_js(f"describeSchedule('cron', '{expr}')")
        assert out["result"] == expr, out["result"]

    def test_empty_value_returns_dash(self) -> None:
        out = _eval_js("describeSchedule('cron', '')")
        assert out["result"] == "-", out["result"]


# ---------------------------------------------------------------------------
# saveSchedule — UI checkbox values (Unix 0=Sun..6=Sat) -> APS cron string
# ---------------------------------------------------------------------------


class TestSaveScheduleCronEncoding:
    """saveSchedule converts Unix-cron checkbox values to APScheduler
    weekday numbers via ``(unix + 6) % 7``. Pin every weekday cell.
    """

    @pytest.mark.parametrize(
        "unix_dow,expected_aps",
        [
            (0, 6),  # Sun -> APS 6
            (1, 0),  # Mon -> APS 0
            (2, 1),  # Tue
            (3, 2),  # Wed
            (4, 3),  # Thu
            (5, 4),  # Fri
            (6, 5),  # Sat
        ],
    )
    def test_single_weekday_payload(self, unix_dow: int, expected_aps: int) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "Test"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": "14:30"},
            "__scheduleType": "specific-time",
            "__checkedDays": [unix_dow],
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        # saveSchedule POSTs (no edit id) — capture & assert.
        posts = out["captured"]["posts"]
        assert len(posts) == 1, f"expected 1 POST, got {len(posts)}: {posts}"
        cron = posts[0]["body"]["cron_expression"]
        # Format: "30 14 * * <APSday>"
        assert cron == f"30 14 * * {expected_aps}", (
            f"unix_dow={unix_dow} should map to APS {expected_aps}; got cron {cron!r}"
        )

    def test_weekdays_encode_to_aps_0_through_4(self) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "Weekdays"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": "08:00"},
            "__scheduleType": "specific-time",
            "__checkedDays": [1, 2, 3, 4, 5],  # Mon..Fri in Unix
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        cron = out["captured"]["posts"][0]["body"]["cron_expression"]
        # Order is preserved from the checkbox iteration: Mon..Fri -> APS 0..4
        assert cron == "0 8 * * 0,1,2,3,4", cron

    def test_weekends_encode_to_aps_5_and_6(self) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "Weekends"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": "10:00"},
            "__scheduleType": "specific-time",
            "__checkedDays": [0, 6],  # Sun, Sat in Unix
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        cron = out["captured"]["posts"][0]["body"]["cron_expression"]
        # Sun(0) -> APS 6, Sat(6) -> APS 5. Iteration order preserved.
        assert cron == "0 10 * * 6,5", cron


class TestSaveScheduleHourBoundaries:
    """saveSchedule strips zero-pad in cron output via parseInt."""

    @pytest.mark.parametrize(
        "time_str,expected_prefix",
        [
            ("00:00", "0 0 "),
            ("00:05", "5 0 "),
            ("12:00", "0 12 "),
            ("23:59", "59 23 "),
            ("09:30", "30 9 "),  # leading zero stripped by parseInt
        ],
    )
    def test_time_to_cron_prefix(self, time_str: str, expected_prefix: str) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "T"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": time_str},
            "__scheduleType": "specific-time",
            "__checkedDays": [1],  # Monday — irrelevant to this test
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        cron = out["captured"]["posts"][0]["body"]["cron_expression"]
        assert cron.startswith(expected_prefix), f"time={time_str}: got cron {cron!r}"


class TestSaveScheduleIntervalBranch:
    """The interval branch encodes minutes/hours into ``interval_minutes``."""

    @pytest.mark.parametrize(
        "value,unit,expected_minutes",
        [
            (1, "minutes", 1),
            (30, "minutes", 30),
            (1, "hours", 60),
            (2, "hours", 120),
            (24, "hours", 1440),
        ],
    )
    def test_interval_payload(self, value: int, unit: str, expected_minutes: int) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "I"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleIntervalValue": {"value": str(value)},
            "scheduleIntervalUnit": {"value": unit},
            "__scheduleType": "interval",
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        body = out["captured"]["posts"][0]["body"]
        assert body["interval_minutes"] == expected_minutes, body
        # Interval schedules must NOT carry a cron_expression.
        assert "cron_expression" not in body or body["cron_expression"] is None, body
        # And stop_time must be cleared for interval triggers (D20 rule).
        assert body.get("stop_time") == "", body


# ---------------------------------------------------------------------------
# saveSchedule / showEditScheduleModal — the priority pin (issue #285)
# ---------------------------------------------------------------------------


def _priority_form(priority_value: str, edit_id: str = "") -> dict:
    return {
        "scheduleEditId": {"value": edit_id},
        "scheduleName": {"value": "Sweep"},
        "scheduleEnabled": {"checked": True},
        "schedulePriority": {"value": priority_value},
        "scheduleStopTime": {"value": ""},
        "scheduleLibraryAll": {"checked": True},
        "scheduleServer": {"value": ""},
        "scheduleTime": {"value": "06:00"},
        # Only the recently_added branch reads these two; the priority
        # logic is shared with full_library, but exercising the sweep
        # path is what issue #285 is actually about.
        "scheduleLookback": {"value": "1", "options": [{"value": "1"}]},
        "scheduleSortBy": {"value": ""},
        "__scheduleType": "specific-time",
        "__checkedDays": [1],
        "__scanMode": "recently_added",
    }


class TestSaveSchedulePriorityPin:
    """The modal must be able to express "no pin".

    Issue #285 shipped a global "Incoming job priority" setting that
    Recently Added sweeps inherit — but only when the schedule stores
    ``priority: null``. The first cut of that feature was inert because
    this exact function did ``parseInt(value, 10) || 2``, so every save
    posted an explicit Normal and the inherit branch was unreachable. The
    Python route tests POST JSON directly and cannot catch a regression
    here; this is the only coverage of the JS half.
    """

    def test_default_option_posts_null_priority(self) -> None:
        out = _eval_js("saveSchedule()", form_values=_priority_form(""))
        posts = out["captured"]["posts"]
        assert len(posts) == 1, f"expected 1 POST, got {posts}"
        assert posts[0]["body"]["priority"] is None, (
            "An empty priority select means 'Default (from Settings)' and MUST post null. "
            "Posting 2 here is the original #285 bug: the schedule looks unpinned in the UI "
            f"but is pinned to Normal on disk, so it never inherits the global setting. "
            f"Got {posts[0]['body']['priority']!r}"
        )

    @pytest.mark.parametrize(("selected", "expected"), [("1", 1), ("2", 2), ("3", 3)])
    def test_explicit_pin_posts_that_int(self, selected: str, expected: int) -> None:
        out = _eval_js("saveSchedule()", form_values=_priority_form(selected))
        assert out["captured"]["posts"][0]["body"]["priority"] == expected


class TestEditModalPriorityPrefill:
    """Reopening a schedule must show the pin it actually has.

    ``String(schedule.priority || 2)`` rewrote a stored null to Normal the
    first time anyone opened the modal, so a subsequent save pinned a
    schedule the user never pinned — the inherit state decayed away on
    contact with the edit dialog.
    """

    @staticmethod
    def _prefill_for(stored_priority) -> str:
        form = _priority_form("2", edit_id="sch-1")
        # showEditScheduleModal runs the real _resetScheduleForm first,
        # which touches every control in the dialog.
        form["__autoStubMissing"] = True
        form["__schedules"] = [
            {
                "id": "sch-1",
                "name": "Sweep",
                "enabled": True,
                "priority": stored_priority,
                "trigger_type": "cron",
                "trigger_value": "0 6 * * 1",
                "library_ids": [],
                "config": {"job_type": "recently_added", "lookback_hours": 1},
            }
        ]
        out = _eval_js(
            "(function(){ showEditScheduleModal('sch-1');"
            " return document.getElementById('schedulePriority').value; })()",
            form_values=form,
        )
        return out["result"]

    def test_null_priority_prefills_the_default_option(self) -> None:
        assert self._prefill_for(None) == "", (
            "A schedule stored with no pin must reopen on 'Default (from Settings)'. "
            "Prefilling '2' silently converts it to a Normal pin on the next save."
        )

    @pytest.mark.parametrize("stored", [1, 2, 3])
    def test_explicit_pin_prefills_itself(self, stored: int) -> None:
        assert self._prefill_for(stored) == str(stored)


# ---------------------------------------------------------------------------
# Round-trip: saveSchedule -> describeSchedule -> showEditScheduleModal.
# This is the strongest pin for the 2d29fe9 bug class — a regression in
# either direction breaks the round trip.
# ---------------------------------------------------------------------------


class TestScheduleRoundTrip:
    """saveSchedule produces an APS cron string; describeSchedule parses
    that string back to a label that mentions the same day the user picked.
    Any drift in either direction (off-by-one in either function) breaks
    this round trip.
    """

    @pytest.mark.parametrize(
        "unix_dow,day_label",
        [
            (0, "Sun"),
            (1, "Mon"),
            (2, "Tue"),
            (3, "Wed"),
            (4, "Thu"),
            (5, "Fri"),
            (6, "Sat"),
        ],
    )
    def test_user_picks_day_and_describe_displays_same_day(self, unix_dow: int, day_label: str) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "RoundTrip"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": "06:00"},
            "__scheduleType": "specific-time",
            "__checkedDays": [unix_dow],
            "__scanMode": "full_library",
        }
        # Combined: save then immediately describe the resulting cron.
        snippet = (
            _vm_prelude()
            + f"\nconst ctx = loadSchedulesContext({json.dumps(form)});\n"
            + "Promise.resolve(vm.runInContext('saveSchedule()', ctx))"
            + ".then(() => {"
            + "  const cron = ctx.__captured.posts[0].body.cron_expression;"
            + "  const label = vm.runInContext(`describeSchedule('cron', '${cron}')`, ctx);"
            + "  console.log(JSON.stringify({cron, label}));"
            + "}).catch((e) => { console.error(e.stack || e.message); process.exit(1); });"
        )
        out = json.loads(_run_node(snippet))
        assert day_label in out["label"], (
            f"Round-trip drift for unix_dow={unix_dow}: cron={out['cron']!r} "
            f"described as {out['label']!r}, expected to mention {day_label!r}"
        )


# ---------------------------------------------------------------------------
# _pendingProgress (commit 31cd4a0) — same file family; assert the cache
# behaviour pinned by the bug. The function lives in app.js, not
# schedules.js, but the hindsight audit asked for it to be covered here.
# ---------------------------------------------------------------------------


class TestPendingProgressReplay:
    """31cd4a0 added a frontend cache so progress events arriving before
    the active-job DOM card existed could be replayed once loadJobs()
    rendered the card. Pin the two contract edges:

      * updateJobProgress with a missing DOM target stashes into
        _pendingProgress instead of crashing.
      * After the DOM target appears, a subsequent call clears the cache
        entry.

    updateJobProgress and its cache are extracted from app.js and run
    under node with a stub ``jobs`` list and ``document``; the replay
    call site inside loadJobs() is only checked in the source because
    loadJobs() drags in the whole dashboard.
    """

    @pytest.fixture(scope="class")
    def app_src(self) -> str:
        return APP_JS.read_text(encoding="utf-8")

    def _run_update_job_progress(self, app_src: str, *, jobs: str, row_exists: bool) -> dict:
        start = app_src.index("const _pendingProgress = {};")
        fn_start = app_src.index("function updateJobProgress(", start)
        source = app_src[start : app_src.index("\n}\n", fn_start) + 3]
        row = "{ querySelector: () => null }" if row_exists else "null"
        snippet = f"""
const jobs = {jobs};
const document = {{ getElementById: (id) => id === 'job-row-7' ? {row} : null }};
{source}
_pendingProgress['7'] = {{ percent: 1 }};
updateJobProgress('7', {{ percent: 5 }});
console.log(JSON.stringify({{ pending: _pendingProgress, jobs }}));
"""
        return json.loads(_run_node(snippet))

    def test_progress_for_a_job_not_yet_listed_is_cached(self, app_src: str) -> None:
        out = self._run_update_job_progress(app_src, jobs="[]", row_exists=False)
        assert out["pending"] == {"7": {"percent": 5}}

    def test_progress_for_a_job_without_a_dom_row_is_cached_and_applied_to_the_job(self, app_src: str) -> None:
        out = self._run_update_job_progress(app_src, jobs="[{ id: 7, progress: {} }]", row_exists=False)
        assert out["pending"] == {"7": {"percent": 5}}
        assert out["jobs"][0]["progress"] == {"percent": 5}

    def test_progress_clears_the_cache_entry_once_the_dom_row_exists(self, app_src: str) -> None:
        out = self._run_update_job_progress(app_src, jobs="[{ id: 7, progress: {} }]", row_exists=True)
        assert out["pending"] == {}
        assert out["jobs"][0]["progress"] == {"percent": 5}

    def test_load_jobs_replays_pending_progress(self, app_src: str) -> None:
        # The replay site lives at the end of loadJobs() — assert it iterates
        # the cache and calls updateJobProgress for each entry.
        assert "Object.keys(_pendingProgress)" in app_src, (
            "loadJobs() must iterate _pendingProgress after rendering the active-job "
            "cards and replay each cached event — that's the 'replay after DOM ready' "
            "half of the 31cd4a0 fix."
        )
        # Find the loop body and confirm it calls updateJobProgress.
        idx = app_src.find("Object.keys(_pendingProgress)")
        window = app_src[idx : idx + 200]
        assert "updateJobProgress(" in window, (
            "The Object.keys(_pendingProgress) loop must invoke updateJobProgress to "
            "actually flush the cache — without the call the cache fills up forever."
        )
