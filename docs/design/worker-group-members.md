# Design: worker groups with several devices ("members")

**Status:** Built in the working tree, uncommitted; see the §0 Status table. Written 2026-10-07 on branch `dev` at `0064b74`.
**Mockup:** `/home/data/design-archive/worker-members/index.html` (outside the repo; open it in a browser).
**Arch review:** required before merge. The change touches the `settings.json` schema, `upgrade.py`, the worker pool
and the job gate (`.claude/CLAUDE.md` → Architecture Review triggers).

---

## §0 Start here

Read this section, then the section for the lane you are building. Every file, function, test and command needed is
named in this document. The scratch prototypes mentioned in §7 are reproduced here in full; nothing outside the repo
is needed.

### Goal

Today a worker group is *one device + one count + job types + hours*. The owner wants a group to be *a name, an on/off
switch and hours* that holds **members**, each member being *one device + a count + its own job types*. Example:
**Off-hours**, daily 01:00–07:00, with **3 × NVIDIA (Video previews, Intro & Credits)** and **10 × CPU (Plex loudness
only)**. Existing installs migrate 1:1 with no reset. Every current function stays.

### Decisions (settled — do not reopen without the owner)

| # | Decision | Why (one line) |
|---|---|---|
| D1 | A group = `id`, `name`, `enabled`, `availability`, `members[]`. A member = `id`, `resource`, `device`, `count`, `job_types`. | Owner's model. Hours and on/off belong to the group; capability belongs to the device. |
| D2 | One member per device per group (one CPU member, one member per GPU). | Keeps the editor and the dashboard one-row-per-device; a second split of the same GPU is a second group. |
| D3 | No per-member enable switch. Member count is 1–32; remove the member to use zero; disable the group to stop all of it. | Same rule as today's group stepper ("never below 1"), no new state. |
| D4 | 1–8 members per group; up to 64 groups (unchanged). Weekly peak limits stay 32 CPU / 32 GPU across all members of all groups. | Devices per box are few; limits unchanged. |
| D5 | Migration v22: each v21 group → same `id`, `name`, `enabled`, `availability` + one member `{"id": "m1", resource, device, count, job_types}`. Revision +1. | 1:1, deterministic ids so worker ownership never churns; revision bump forces stale browser tabs to reload instead of overwriting. |
| D6 | The runtime keeps working on a flat list. `worker_groups.member_policies(groups)` flattens every member into a dict that looks exactly like a v21 group plus `group_id` and `member_id`. | Pool, capacity, peak and gate code keep their proven logic; only the edges (schema, API, UI) learn nesting. |
| D7 | `PUT /api/worker-groups` still accepts the flat v21 group shape (converted to one member `m1`). `GET` echoes `resource`/`device`/`count`/`job_types` on single-member groups (deprecated, read-only). `POST /worker-groups/<id>/scale {"delta"}` still works on single-member groups. New: `POST /worker-groups/<group>/members/<member>/scale`. | API-token clients and stale tabs keep working through the upgrade. |
| D8 | Dashboard: a group is one card; each member is a sub-row with its own busy/count and stepper; worker rows sit under their member. Dense table switches on when any enabled **member** has ≥ 5 workers; row cap (8) and "Show N more" are per member. | Owner's request; per-member trigger stops a 3 + 3 group flipping to the table. |
| D9 | Week graph: one lane per member, coloured by device (same device = same colour everywhere). Legend lists devices. | Owner's request. |
| D10 | Migration notice uses the existing one-shot "Settings migrated" notification card (`upgrade._USER_FACING_NOTES[22]`). | Zero new notice machinery; dismissal already persists. |
| D11 | Job gate fix (§7): **a job kind that holds no slot goes first.** Ships first, on its own PR, independent of members. | Proven starvation at High priority; the owner wants simple. |

### Open decisions for the owner

1. **System card steppers.** Today the quick-scale steppers live in the Dashboard **System** card. The owner asked for
   steppers on the Workers-panel member rows. Recommendation: steppers on member rows only; the System card shows one
   read-only line per group ("Off-hours · 3 GPU + 10 CPU"). Alternative: keep both (same endpoint, same renderer).
2. **Loudness slot when previews are High.** With the §7 fix, a burst of High webhook previews leaves one slot for
   waiting loudness/intro work. At cap 3 previews get 2 of 3 slots (today: 3 of 3). Previews still keep every GPU
   worker busy because one admitted job spreads its files across all of them. Confirm this trade is wanted.
3. **Static per-kind ceiling.** Today Normal/Low jobs of one kind may hold at most `cap − 2` slots even when nothing
   else is queued (`job_gate.py:338`). With the §7 rule it could be removed to use every slot when one kind is alone.
   Recommendation: keep it for now (shipped, tested, unrelated to the bug); revisit only if single-file jobs leave
   workers idle in practice.
4. **Same GPU twice in one group (D2).** Recommendation: refuse. Confirm nobody needs "2 workers previews-only +
   1 worker intro-only on the same GPU at the same hours" inside one group (it remains possible with two groups).

### Status

| Item | State |
|---|---|
| Spec (this file) | Written, uncommitted |
| Mockup | `/home/data/design-archive/worker-members/index.html` |
| §7 gate fix | Prototyped (not in repo): proposed test fails 4/9 on today's gate, passes 9/9 with the fix; 1,341 existing gate/priority/admission/runner/loudness/worker-group/dispatcher tests pass with it |
| Code | Built in the working tree (uncommitted): schema, validation, migration, API, runtime, Settings editor, Dashboard, week graph |
| Docs (Lane F) | Updated: `docs/reference.md`, `docs/guides.md`, FAQ, getting-started, GPU/slow/loudness/Sonarr pages, READMEs |
| Live verification | Pending |

### Files to change (owner lane in brackets; see §10)

Backend: `media_preview_generator/worker_groups.py` [A], `upgrade.py` [A], `config/__init__.py` [A],
`config/validation.py` [A], `web/settings_manager.py` [A], `jobs/worker.py` [B], `jobs/group_runtime.py` [B],
`jobs/dispatcher.py` [B], `jobs/parking.py` [B, verify only], `web/jobs.py` [B], `web/routes/job_runner.py` [B],
`markers/job_runner.py` [B], `markers/credits/textdet_helper.py` [B], `web/routes/api_worker_groups.py` [C],
`web/routes/api_jobs.py` [C], `web/routes/api_settings.py` [C], `web/routes/api_servers.py` [C, verify only],
`web/job_gate.py` [D].
Frontend: `web/static/js/worker_groups.js` [E], `web/static/js/app.js` [E], `web/static/js/week_graph.js` [E],
`web/static/css/worker_groups.css` [E], `web/static/css/pages/settings.css` [E], `web/static/css/dashboard.css` [E],
`web/templates/index.html` [E, tooltip copy only].
Docs: `docs/reference.md` [F], `docs/guides.md` [F], `docs/llms-full.txt` [F, regenerated].
Tests: listed in §8.

### How to run tests

```bash
cd /home/data/orca/workspaces/plex_generate_vid_previews/dashboard
PY=/home/data/.venv/bin/python                     # shared venv on storage (3.14, dev extras + Playwright)
$PY -m pytest                                      # full unit suite, parallel, ~100 s
$PY -m pytest --no-cov -n 0 tests/test_job_gate_kind_share.py tests/test_job_gate_resources.py tests/test_job_gate_ordering.py tests/test_priority.py
$PY -m pytest --no-cov -n 0 tests/test_worker_group_policy.py tests/test_worker_group_migration.py tests/test_worker_groups_runtime.py tests/test_worker_groups_api.py
$PY -m pytest -m e2e -n 8 --no-cov tests/e2e/test_worker_groups.py tests/e2e/test_workers_dense_table.py tests/e2e/test_ui_workers_panel.py   # never -n auto
$PY scripts/generate_llms_full.py && $PY -m pytest --no-cov -n 0 tests/test_docs_site.py   # after docs edits
ruff check . --fix && ruff format .
```

Fail fast: run the lane's own test files with `-x` first, the full suite once at the end, e2e last.

---

## 1. Today (v21), in one paragraph

`settings.json["worker_groups"]` is a list of flat groups validated by `worker_groups.validate_worker_groups`
(`worker_groups.py:152-235`). The pool (`jobs/worker.py:1081-1166 reconcile_groups`) keeps one set of `Worker`
slots per group, keyed by `group_id` and resource key (`cpu` or `gpu:<device>`), retires busy slots via
`_pending_removal`, and shares an assignment budget per resource (`_resource_targets`, `worker.py:1185-1186`).
`jobs/group_runtime.py` turns the saved policy into capacity (`capacity_for_groups`, `runtime_capacity`,
`admission_capacity`) for the job gate and the runners. `web/routes/api_worker_groups.py` serves `GET/PUT
/api/worker-groups` and `POST /worker-groups/<id>/scale`; `worker_groups.js` renders the Settings editor, the
Dashboard Workers panel and the System card steppers; `week_graph.js` draws lanes.

---

## 2. Data model

### 2.1 `settings.json` before and after

Before (schema v21):

```json
"worker_groups": [
  {"id": "legacy-gpu-1f3a…", "name": "NVIDIA RTX 4090", "enabled": true, "resource": "gpu", "device": "cuda:0",
   "count": 3, "job_types": ["previews", "intro_credits"], "availability": {"mode": "always", "windows": []}},
  {"id": "cpu-loudness", "name": "Overnight audio", "enabled": true, "resource": "cpu", "device": null,
   "count": 10, "job_types": ["loudness"],
   "availability": {"mode": "scheduled", "windows": [{"days": [0,1,2,3,4,5,6], "start": "01:00", "end": "07:00"}]}}
],
"worker_groups_revision": 6
```

After (schema v22) — the same two groups, migrated, unchanged in behaviour:

```json
"worker_groups": [
  {"id": "legacy-gpu-1f3a…", "name": "NVIDIA RTX 4090", "enabled": true,
   "availability": {"mode": "always", "windows": []},
   "members": [{"id": "m1", "resource": "gpu", "device": "cuda:0", "count": 3, "job_types": ["previews", "intro_credits"]}]},
  {"id": "cpu-loudness", "name": "Overnight audio", "enabled": true,
   "availability": {"mode": "scheduled", "windows": [{"days": [0,1,2,3,4,5,6], "start": "01:00", "end": "07:00"}]},
   "members": [{"id": "m1", "resource": "cpu", "device": null, "count": 10, "job_types": ["loudness"]}]}
],
"worker_groups_revision": 7
```

The owner's new **Off-hours** group, as saved by the editor:

```json
{"id": "6f0c…", "name": "Off-hours", "enabled": true,
 "availability": {"mode": "scheduled", "windows": [{"days": [0,1,2,3,4,5,6], "start": "01:00", "end": "07:00"}]},
 "members": [
   {"id": "2b9e…", "resource": "gpu", "device": "cuda:0", "count": 3, "job_types": ["previews", "intro_credits"]},
   {"id": "c71d…", "resource": "cpu", "device": null,     "count": 10, "job_types": ["loudness"]}]}
```

Key order on save is fixed (`id, name, enabled, availability, members`; member `id, resource, device, count,
job_types`) and `job_types` keeps `JOB_KINDS` order, as today, so "unchanged" compares equal.

### 2.2 Validation (`validate_worker_groups`)

Accepts both shapes per group: a dict with `members` is the v22 shape; a dict without `members` but with
`resource` is the v21 shape and becomes one member `m1` (D7). When `members` is present, any top-level
`resource`/`device`/`count`/`job_types` is ignored (they are the deprecated GET echo). Returns v22 only.

| Rule | Server message (exact; `{name}` = group name, `{dev}` = `CPU` or the device id) |
|---|---|
| list, ≤ 64 groups | `Worker groups must be a list of at most 64 groups` (unchanged) |
| group id: `_ID` regex, unique | `Each worker group needs a unique valid ID` (unchanged) |
| name 1–80 after strip | unchanged |
| `enabled` bool | unchanged |
| `members` list of 1–8 | `{name}: add at least one device` / `{name}: use at most 8 devices` |
| member id: `_ID` regex, unique in group | `{name}: each device needs a unique valid ID` |
| `resource` cpu/gpu | `{name}: choose CPU or GPU for each device` |
| GPU needs `device` (1–256 chars); CPU has `device` null/"" | `{name}: choose a GPU device` / `{name}: a CPU member cannot select a GPU device` |
| one member per device (CPU counts as one device) | `{name}: {dev} appears twice; use one row per device` |
| `count` int 1–32 | `{name} ({dev}): worker count must be between 1 and 32; remove the device to use zero` |
| `job_types` non-empty subset of `JOB_KINDS` | `{name} ({dev}): select at least one supported job type` |
| no `loudness` on GPU | `{name} ({dev}): Plex loudness requires CPU workers` |
| availability / windows | unchanged |
| weekly peak per family across all members of all groups ≤ 32 | `Overlapping groups exceed capacity: peak CPU {c}/32, GPU {g}/32. Reduce counts or use different hours.` (unchanged) |

### 2.3 Member policies — the flat runtime view (D6)

New in `worker_groups.py`:

```python
def member_policies(groups: list[dict]) -> list[dict]:
    """One flat policy per member, shaped like a v21 group plus its owners.

    Accepts validated v22 groups; a legacy flat group (no ``members``) counts as one member ``m1`` so a stale
    snapshot or a hand-edited file cannot crash the runtime. Pure: no settings, locks or I/O.

    Returns:
        Dicts with ``id`` (``"<group_id>:<member_id>"``), ``group_id``, ``member_id``, ``name`` (the group's),
        ``enabled``, ``availability`` (the group's), ``resource``, ``device``, ``count``, ``job_types``.
    """
```

`:` cannot occur in a group or member id (`_ID` is `[A-Za-z0-9][A-Za-z0-9_-]{0,79}`), so the policy id is
unambiguous. Every existing helper then takes a member policy where it took a group, unchanged:
`supports_job`, `group_resource_key`, `group_weekly_mask`, `group_is_available`, `next_group_opening`, `_peak`.
Functions that take the saved list flatten first: `configured_group_totals`, `future_capacity`,
`capacity_for_groups`, `WorkerPool.reconcile_groups`.

### 2.4 Migration and seeding

* `worker_groups.groups_from_legacy` (`worker_groups.py:238-273`): emit v22 directly — each legacy CPU/GPU allocation
  becomes a group with one member `m1`. Ids, names and `enabled` rules unchanged.
* `upgrade.py`: `_CURRENT_SCHEMA_VERSION = 22`; add `_run(22, _migrate_to_v22)` after v21 (`upgrade.py:527-528`);
  document v21 and v22 in the `_migrate_schema` docstring (it stops at v20 today — fix that drift too);
  `_USER_FACING_NOTES[22] = "Groups can now include several devices — your existing groups are unchanged."`

```python
def _migrate_to_v22(sm) -> list[str]:
    """Give every worker group a member list: each v21 group becomes one group with one member (owner, 2026-10-07).

    Ids, names, hours, counts and job types are kept, and each member is ``m1``, so live worker ownership and the
    dashboard are unchanged. The revision moves on so an editor left open across the upgrade reloads instead of
    saving the old shape over the new one.
    """
    from .worker_groups import validate_worker_groups

    stored = sm.get_all()
    if "worker_groups" not in stored:
        return []
    groups = validate_worker_groups(stored["worker_groups"])
    if groups == stored["worker_groups"]:
        return []
    sm.apply_changes(
        updates={"worker_groups": groups, "worker_groups_revision": int(stored.get("worker_groups_revision", 0)) + 1}
    )
    return ["v22: worker groups now hold devices; every existing group kept its device, count, jobs and hours"]
```

  A v20 → v22 jump runs v21 (which now writes v22 via `groups_from_legacy`), then v22 is a no-op and adds no second
  note. A stored list that fails validation raises exactly as v21 does today (`upgrade.py:1719`); the app could not
  run with it anyway, since `SettingsManager.worker_groups` validates on every read.
* `web/settings_manager.py`: `worker_groups`, `update_worker_groups`, `gpu_threads`, `cpu_threads` keep their
  signatures; the totals come from `configured_group_totals`, which now flattens. No other change.
* `config/__init__.py:603-608` and `config/validation.py:303-306`: unchanged calls; they get v22 from the validator.
* Downgrade: a v22 `settings.json` makes the v21 binary refuse to start (`SchemaDowngradeError`,
  `upgrade.py:454-465`). Rollback = restore the automatic `settings.json.<timestamp>.bak` written before migrating.

### 2.5 Backwards compatibility summary

| Caller | Behaviour after v22 |
|---|---|
| `PUT /api/worker-groups` with flat groups | Accepted; each becomes one member `m1`. |
| `GET /api/worker-groups` consumers reading `group.count` etc. | Single-member groups echo `resource`, `device`, `count`, `job_types`; multi-member groups do not. Documented as deprecated. |
| `POST /api/worker-groups/<id>/scale {"delta": ±1}` | Single-member group: scales that member (old semantics, including "−1 at 1 disables the group"). Multi-member: `409 {"error": "Choose a device to scale", "members": ["2b9e…", "c71d…"]}`. `{"enabled": bool}` unchanged for every group. |
| `POST /api/workers/add|remove` (`api_jobs.py:1446-1497 _scale_group_for_legacy_request`) | Chooses the unique member with that resource across all groups (optional `group_id`, new optional `member_id`); `409` with candidates when ambiguous. |
| `POST /api/settings` with `cpu_threads` / `gpu_threads` / `gpu_config[].workers` (`api_settings.py:844-918`) | Maps to the unique matching member; creates a one-member group when none exists; `ValueError` when several match (message unchanged). |
| Stale browser tab across the upgrade | Its next PUT gets `409` (revision bumped) and reloads; its quick-scale and GET polling keep working through the echo. |

---

## 3. API (`web/routes/api_worker_groups.py`)

### 3.1 `GET /api/worker-groups`

```json
{
  "groups": [{
    "id": "6f0c…", "name": "Off-hours", "enabled": true,
    "availability": {"mode": "scheduled", "windows": [{"days": [0,1,2,3,4,5,6], "start": "01:00", "end": "07:00"}]},
    "members": [
      {"id": "2b9e…", "resource": "gpu", "device": "cuda:0", "count": 3, "job_types": ["previews", "intro_credits"]},
      {"id": "c71d…", "resource": "cpu", "device": null, "count": 10, "job_types": ["loudness"]}]
  }],
  "revision": 8,
  "timezone": "Australia/Sydney", "timezone_label": "Australia/Sydney",
  "limits": {"cpu": 32, "gpu": 32, "members": 8},
  "hardware": [{"device": "cuda:0", "name": "NVIDIA GeForce RTX 4090", "type": "nvidia", "status": "ok"}],
  "capacity": {
    "groups": [{
      "id": "6f0c…", "name": "Off-hours", "desired": 13, "target": 13, "available": 2, "busy": 11, "finishing": 0,
      "state": "active", "next_available_at": null,
      "members": [
        {"id": "2b9e…", "resource": "gpu", "device": "cuda:0", "desired": 3, "target": 3, "available": 0,
         "busy": 3, "finishing": 0, "state": "active"},
        {"id": "c71d…", "resource": "cpu", "device": null, "desired": 10, "target": 10, "available": 2,
         "busy": 8, "finishing": 0, "state": "active"}]
    }],
    "current": {"cpu": 10, "gpu": 3}, "peak": {"cpu": 10, "gpu": 3}
  },
  "warnings": [
    {"code": "hardware_unavailable", "group_id": "…", "member_id": "…", "message": "Off-hours: GPU cuda:1 not detected. Its jobs wait; other devices keep working."},
    {"code": "no_eligible_workers", "job_type": "loudness", "message": "Plex loudness has no compatible worker hours outside global quiet hours. Add or enable a group and check its hours."}
  ],
  "processing_paused": false, "pause_reasons": []
}
```

Group row aggregation in `worker_group_payload` (`api_worker_groups.py:55-174`): `desired/target/available/busy/
finishing` are member sums. Group `state`: `disabled` if not enabled; else `draining` if no member has a target but
some are finishing; else `active` if open and at least one member's hardware is detected; else
`hardware_unavailable` if every member's hardware is missing; else `off_hours`. Member `state` uses the same words
per member (a missing GPU in an otherwise working group shows on that member only). Members removed from the policy
but still finishing appear as member rows with `desired: 0`, `state: "draining"`. Removed groups still finishing
appear as today (`api_worker_groups.py:114-126`), with their member rows.

### 3.2 `PUT /api/worker-groups`

Unchanged contract: `{"groups": [...], "revision": N}`; `400` invalid, `409` stale revision.

### 3.3 `POST /api/worker-groups/<group_id>/members/<member_id>/scale` (new)

Body exactly `{"delta": 1}` or `{"delta": -1}`. Under `settings.locked()`: load groups, find group (`404 "Worker group
no longer exists"`), find member (`404 "That device is no longer in this group"`), group disabled →
`409 "Enable the group first"`, `count + delta` outside 1–32 → `400 "A device needs 1–32 workers. Remove it in
Settings to use zero."`, then `settings.update_worker_groups(groups)` (revision +1; peak validation may `400`).
Outside the lock: `reconcile_group_settings(settings)`; respond with `worker_group_payload(settings)` plus
`success: true` and any `warning`. This is `scale_saved_group` (`api_worker_groups.py:206-223`) applied to a
member; both share one helper `_scale_saved_member(group_id, member_id, delta)`.

### 3.4 `POST /api/worker-groups/<group_id>/scale` (kept)

`{"enabled": bool}` toggles the group (all members). `{"delta": ±1}` delegates to the only member; multi-member
→ `409` as in §2.5.

### 3.5 Worker status rows (Socket.IO `worker_update` and `GET /api/jobs/workers`)

Add `member_id` beside `group_id`, `group_name`, `group_resource`, `retiring`. Producers:
`jobs/dispatcher.py:1402-1478 _build_worker_statuses` (read `worker.member_id` under the same locks as `group_id`),
`web/jobs.py:344-348 WorkerStatus` (new field `member_id: str | None = None`),
`web/routes/job_runner.py:978-986` and `markers/job_runner.py:1698-1706` (copy it), and
`web/routes/api_jobs.py:1943+ _build_idle_workers_from_config` (synthesised idle rows iterate member policies;
`worker_name` = `"{group name} {n}"` as today).

---

## 4. Runtime changes, function by function

| File | Function | Change |
|---|---|---|
| `worker_groups.py` | `validate_worker_groups` | v22 rules (§2.2); accepts v21 groups. |
| | `member_policies` (new) | §2.3. |
| | `supports_job`, `group_resource_key` | Unchanged; document that they take a member policy. |
| | `_peak`, `configured_group_totals` | Flatten first; peak sums member counts over group masks. |
| | `groups_from_legacy` | Emits v22 (one member `m1`). |
| | `future_capacity` | Flatten first. |
| | `legacy_group_view(group)` (new, small) | Returns the deprecated echo fields for a single-member group, `{}` otherwise. Used only by the API. |
| `jobs/worker.py` | `Worker.__init__` (`:185-189`) | Add `member_id: str | None` and `policy_id: str | None`. Ownership is by `policy_id`. |
| | `reconcile_groups` (`:1081-1166`) | `by_id = {p["id"]: p for p in member_policies(groups)}`. Ownership test `w.policy_id == pid and w.group_resource == resource`; set `group_id`, `member_id`, `policy_id`, `group_name` on new and kept slots. Retire rule unchanged: a slot whose policy disappeared or whose resource key changed is removed when idle or flagged `_pending_removal` when busy. Rename `_groups` → `_policies`, `_group_targets` → `_policy_targets` (internal; tests updated). `_resource_targets` unchanged (sum per resource key across all members of all groups). |
| | `_worker_supports_kind` (`:1168-1186`) | Read `self._policies[worker.policy_id]`. Logic unchanged. |
| | `capacity_for`, `has_capacity_for` (`:1188-1229`) | Iterate policies. |
| | `group_snapshots` (`:1231-1288`) → `member_snapshots` | One row per policy with `group_id`, `member_id`; draining policies (no longer saved) synthesised from their workers as today. |
| | `reconcile_gpu_workers`, `reconcile_cpu_workers` (`:1425+`, `:1561+`) | Unchanged calls into `reconcile_groups`. |
| `jobs/group_runtime.py` | `capacity_for_groups` (`:35-53`) | Flatten; "compatible/detected/opened" become member lists. `configured` and `open` are member-count sums. |
| | `current_group_policy`, `runtime_capacity`, `admission_capacity`, `wait_for_capacity`, `refresh_worker_groups` | Unchanged (they pass the saved list through). |
| `jobs/dispatcher.py` | `_build_worker_statuses` | Add `member_id` (§3.5). `_assign_tasks` unchanged. |
| `jobs/parking.py` | `park_if_unavailable` | No change; verify `capacity_for(kind)` now counts members of every group. |
| `web/routes/job_runner.py` | `_build_selected_gpus` (`:417-487`) | `grouped_devices` = devices of GPU member policies whose group is enabled; per-device `workers` from `configured_group_totals` over those policies. |
| `markers/credits/textdet_helper.py` | `_configured_cpu_workers` (`:1256-1275`) | Sum CPU member policies that support `intro_credits` and are available. |
| `web/routes/api_worker_groups.py` | `worker_group_payload`, `scale_saved_group`, new member route | §3. Hardware warnings per member. |
| `web/routes/api_jobs.py` | `_scale_group_for_legacy_request`, `_build_idle_workers_from_config` | §2.5, §3.5. |
| `web/routes/api_settings.py` | `_translate_legacy_worker_updates` (`:844-918`) | Match member policies; create one-member groups. |
| `web/job_gate.py` | `_next_eligible`, `_kind_can_admit`, `acquire` | §7.4. Independent lane. |

---

## 5. Concurrency and risk (architecture-review notes)

**Lock order — keep it.** settings `_lock` → pool `_workers_lock` (`reconcile_group_settings`,
`api_worker_groups.py:42-43`); dispatcher `_trackers_lock` → pool `_workers_lock` (`dispatcher.py:981`); gate
`_refresh_lock` → gate `_cond` (`job_gate.py:223-228`). Never pool → settings (`dispatcher.py:973-975` says why),
never pool → gate. `member_policies` is pure, so calling it inside `reconcile_groups` under `_workers_lock` adds no
edge. The gate today reads the cap provider (settings `_lock`) under `_cond` through
`_kind_can_admit → effective_cap(priority)` (`job_gate.py:338 → 289 → 271`); no path holds settings while taking the
gate, so it is safe, but §7.4 removes the edge by passing `cap` through.

**Member removal and device change.** A removed member, or a member whose device changed, is a policy whose id
vanished or whose resource key changed. `reconcile_groups` must flag busy slots `_pending_removal` and keep their
`group_id`/`member_id`, so the dashboard shows them under the group as "finishing" and the dispatcher still reaps
their completions (`dispatcher.py:863-875`). Their busy count keeps consuming the device's `_resource_targets` budget
(`worker.py:1185`), so a new member on the same device cannot oversubscribe it during the handover. Test both with a
busy worker (§8).

**Member id stability.** If ids changed on every save, every save would retire every worker. The validator never
generates ids; migration uses `m1`; the editor generates an id once when a device row is added and keeps it in the
draft; duplicate-group copies member ids unchanged (ids are only unique within a group). Test "save unchanged
groups → zero workers retired".

**Lazy-init races (bug shape 3).** No new singletons. `get_job_gate` and `get_dispatcher` keep their locked lazy
init. The settings migration runs before any job is accepted (`web/app.py` startup), so no runner ever sees a v21
list after boot. Dashboard JS creates group sections lazily (`worker_groups.js:303-335`); member hosts must be
created through the same keyed map (`group_id` → `member_id` → host) so a socket update that arrives before the
first GET cannot create a duplicate section.

**Revisions.** `reconcile_groups` ignores older revisions (`worker.py:1097-1100`); keep that. Member quick-scale
bumps the revision, so an open editor draft gets `409` on Apply, as group quick-scale does today. The v22 migration
bumps it once.

**Vestigial work.** `refresh_worker_groups` runs at most once a second from the dispatch loop
(`group_runtime.py:106-123`). Flattening is O(groups × members); no hardware detection on that path.

**Comments vs code.** Update the module docstrings of `worker_groups.py`, `job_gate.py`, the `_migrate_schema`
docstring, the Workers tooltip in `templates/index.html:169-171`, and the hint text in `worker_groups.js:567`
("Groups on the same resource add their worker counts" → "Devices shared by several groups add their worker counts").

---

## 6. UI behaviour (mockup sections in brackets)

### 6.1 Settings → Workers list [mockup §1]

* One card per group (as today). Icon: the device icon for one member, a stack icon for several.
* Title line: group name. Under it: one chip per member — device icon, short device name, `×count`, job-type icons
  (coloured when allowed). Then state chip + next opening + hours line (unchanged).
* Right side: total workers, **Edit**, enable switch (unchanged).
* Group strip (scheduled groups only): one lane per member, device colour (§6.6).

### 6.2 Editor [mockup §1]

Order: **Name** → **Devices in this group** → **Availability** (always / weekly windows, unchanged) → this group's
week (one lane per member) → actions (**Apply group changes**, **Duplicate group**, **Remove group**).

Each device row:

* Device picker: `CPU` plus each detected GPU. Devices already used by another row of this group are disabled with
  "(already in this group)". A saved GPU that is not detected shows "… — not detected".
* **Workers** stepper `−  n  +` (1–32; `−` disabled at 1 with tooltip "Remove the device to use zero"; `+` disabled
  at 32). Typing a number is allowed.
* Job chips: Video previews, Intro & Credits, Plex loudness. On a GPU the loudness chip is disabled with an (i)
  tooltip "Plex loudness runs on CPU workers only". Switching a row from CPU to GPU removes loudness and shows the
  one-line note "Loudness removed — it runs on CPU only."
* Remove-device button (trash, 44 px, visible, never hover-only). Removing the last device is refused inline:
  "A group needs at least one device. Remove the group instead."
* **Add device** adds the next unused device (first unused GPU, else CPU) with Video previews + Intro & Credits on a
  GPU and all three on CPU. Disabled with tooltip "Every detected device is already in this group" when none is left.

**Add group** creates "New worker group" with one CPU member (all job types, count 1, always available).
**Add CPU group for loudness** (shown only when loudness is wanted and uncovered) creates "CPU loudness" with one CPU
member, loudness only. **Duplicate group** copies members.

### 6.3 Validation messages (client, before Apply)

| Case | Inline message (row or editor foot) |
|---|---|
| Empty name | "Every group needs a name." |
| No devices | "Add at least one device." |
| Count outside 1–32 | "{device}: enter 1–32 workers. Remove the device to use zero." |
| No job type on a row | "{device}: choose at least one job type." |
| Same device twice | "{device} is already in this group." |
| Loudness on GPU | Prevented by the disabled chip; if it arrives from an import: "{device}: loudness runs on CPU only." |
| Weekly peak over 32 | Server message shown in the editor foot after Apply, e.g. "Overlapping groups exceed capacity: peak CPU 10/32, GPU 35/32. Reduce counts or use different hours." |
| GPU not detected | Warning on the row: "Not detected — its jobs wait; the other devices keep working." Saving is allowed. |

### 6.4 Dashboard → Workers panel [mockup §2]

* One section per group (unchanged placement, 1–3 columns in card layout).
* Header: name, hours clock (tooltip), union of job-type icons, `busy / total` chip (sum of members).
* Member rows directly under the header: device icon + name, job-type icons, `busy / count` chip, stepper
  (`POST …/members/<id>/scale`). Steppers are 28 px on desktop, 44 px under 576 px or on coarse pointers. A disabled
  group shows one **Enable** button instead of member steppers. A missing GPU shows a warning chip "Not detected" on
  its member row. A removed member still finishing shows "Removed device · n finishing" and no stepper.
* Worker cards/rows sit under their member row. Idle workers fold into one muted line per member
  ("7 workers idle #4 #5 …").
* Dense table: on when any enabled member has ≥ 5 workers (`DENSE_TABLE_MIN_WORKERS`, now per member). Row cap 8 per
  member, busy first, problem rows always shown, "Show N more · k running" per member, expansion remembered per
  `group_id:member_id` in `localStorage` (`workerGroupsExpanded`; old group-id keys are simply ignored).
* Filters/search (> 6 groups) unchanged; search text includes member device names.

### 6.5 Dashboard → System card

Per open decision 1. Default: one read-only line per group (`Off-hours · 3 GPU + 10 CPU`, state dot), **Enable** for
a disabled group.

### 6.6 Week graph [mockup §3]

* `week_graph.js`: add `deviceColor(member, hardware)` — CPU is `var(--ok)`; GPUs take `var(--run)`,
  `var(--t-intro)`, `var(--t-loud)`, `var(--accent)` in `hardware` order, cycling. Same device → same colour in every
  group, strip and legend.
* Week at a glance: one lane per enabled member, ordered by group then member; segment tooltip
  "Off-hours · RTX 4090 ×3 · Mon 01:00–07:00". Legend: one swatch per device in use, then Global pause and Now.
* A group's strip and the editor preview: one lane per member of that group.

### 6.7 Migration notice [mockup §4]

`_USER_FACING_NOTES[22]` → the existing "Settings migrated" card in the notification bell shows
"Groups can now include several devices — your existing groups are unchanged." once, until dismissed.

### 6.8 Empty states [mockup §5]

* Settings, no groups: dashed card "No worker groups yet. Jobs wait until a group can run them." with **Add group**
  and (when loudness is enabled somewhere) **Add CPU group for loudness**.
* Group with no devices cannot be saved (§6.3); the editor shows the device section empty state "Add a device to
  give this group workers." with **Add device**.
* Dashboard, no groups: "No worker groups configured. Jobs wait until a compatible group is available." with a
  **Manage groups** link (existing text).

### 6.9 Setup wizard

`setup.html:438` mounts the same editor (`#workerGroupSettings`); it inherits §6.1–6.3. Verify step 4 e2e.

---

## 7. §Queue — can previews start while 20 loudness jobs are pending?

Owner's scenario: 20 loudness jobs queued ahead of 10 preview jobs; 5 CPU workers allowed loudness only; 5 GPU
workers allowed previews only. Fear: previews wait for loudness to drain while GPU workers are free.

### 7.1 How admission works (cited)

1. Every pending job gets its own runner thread. On resume/restart, `resume_running_and_drain_pending` sorts pending
   jobs by `(priority, created_at)` (`web/routes/job_runner.py:536`), registers all of them with the gate first
   (`prepare_admissions`, `:539` → `jobs/admission.py:66-81`), then starts each (`:540-543`). Loudness and intro jobs
   go to their own runners (`job_runner.py:578-587`).
2. Before asking the gate, each runner waits **outside** it until its kind has open configured workers
   (`wait_for_capacity`: previews `job_runner.py:1109-1116`, loudness `loudness/job.py:458-465`, intro
   `markers/job_runner.py:1816-1824`; implementation `jobs/group_runtime.py:149-164`).
3. `JobGate.acquire` (`web/job_gate.py:365-438`; call sites `job_runner.py:1119`, `loudness/job.py:468`,
   `markers/job_runner.py:1827`) admits a waiter only when `_next_eligible(cap) is token` (`:409`).
4. `_next_eligible` (`:354-363`) takes the **minimum (priority, created order)** over waiters that are ready **and**
   pass `_can_admit` **and** `_kind_can_admit`. Blocked waiters are skipped, so an older blocked loudness job does
   not block a younger admissible preview job — no head-of-line blocking.
5. `_can_admit` (`:294-306`): total `active < cap`; non-High jobs also need `active − active_high <
   effective_cap = cap − 1` (one slot reserved for High, `:58`, `:275-292`).
6. `_kind_can_admit` (`:323-352`): for Normal/Low only, one kind may hold at most `ceiling = max(1,
   effective_cap − 1)` ordinary slots (`:329-344`) — 1 at cap 3, 3 at cap 5 — unless every held slot of that kind
   is lower priority (`higher_priority_escape`, `:339-342`). Then, at any priority, same-or-higher-priority
   admissions of the kind must stay below `limit` = the kind's **configured open** worker count
   (`:347-352`; provider `admission_capacity` → `runtime_capacity(kind)["open"]`, `group_runtime.py:72-76`,
   summed in `capacity_for_groups`, `:35-53`). **High priority skips the ceiling entirely** (`:329`).
7. A slot is a *job*, not a file. The admitted job keeps it through enumeration and dispatch until its runner
   finishes (`job_runner.py:2131-2138`, `loudness/job.py:752-757`). If its kind loses all open workers mid-run it
   parks and frees the slot (`jobs/parking.py:26-86`); after admission each runner re-checks capacity and releases
   if it vanished (`job_runner.py:1128-1131`, `loudness/job.py:481-488`, `markers/job_runner.py:1838-1847`). So a
   held slot always belongs to a kind with open workers.
8. Inside the dispatcher there is no cross-kind blocking either: `_assign_tasks` walks trackers by
   `(priority, submission_order)` and, for each, asks for a worker *of that tracker's kind*; when loudness has no
   free CPU worker it moves on to the preview tracker (`jobs/dispatcher.py:982-999`;
   `WorkerPool._find_available_worker`, `jobs/worker.py:1612-1637`; kind and group checks in
   `_worker_supports_kind`, `:1168-1186`, which also refuses loudness on GPU, `:1173`).
9. Defaults that matter: webhook and Recently Added jobs are **High** by default (`web/jobs.py:265-281`,
   `incoming_job_priority`); loudness follow-ups of a preview job are at most **Normal**
   (`markers/triggers.py:655`, `max(PRIORITY_NORMAL, preview.priority)`).

### 7.2 Results

Measured on the real `JobGate` (all 30 jobs registered before any runner asks, limits loudness 5 / previews 5):

| Case | Slots | Today (loudness, previews) | With §7.4 fix |
|---|---|---|---|
| (i) cap 3, all Normal | 3 (1 reserved for High) | 1, 1 | 1, 1 |
| (ii) cap 5, all Normal | 5 (1 reserved) | 3, 1 | 3, 1 |
| (iv) cap 3, loudness Normal / previews High | 3 | **0, 3** | 1, 2 |
| (iv) cap 5, loudness Normal / previews High | 5 | **0, 5** | 1, 4 |
| mirror cap 3, loudness High / previews Normal | 3 | **3, 0** | 2, 1 |
| mirror cap 5, loudness High / previews Normal | 5 | **5, 0** | 4, 1 |

(iii) "all Normal" is rows (i) and (ii).

Worked timelines (real gate, loudness queued first, then previews; one job finishes per step):

(i) cap 3, all Normal — previews never wait for loudness:

| Step | Event | Slot 1 | Slot 2 | Slot 3 (High only) |
|---|---|---|---|---|
| t0 | 20 L then 10 P queued | L1 | P1 | — |
| t1 | L1 done | L2 | P1 | — |
| t2 | P1 done | L2 | P2 | — |
| t3 | L2 done | L3 | P2 | — |

L2 is refused at t0 by the ceiling (1 Normal loudness at cap 3, `job_gate.py:338-344`); `_next_eligible` skips it
and admits P1 (`:354-363`).

(ii) cap 5, all Normal:

| Step | Event | Slots 1–4 (Normal) | Slot 5 (High only) |
|---|---|---|---|
| t0 | queued | L1 L2 L3 P1 | — |
| t1 | L1 done | L2 L3 L4 P1 | — |
| t2 | P1 done | L2 L3 L4 P2 | — |

(iv) cap 3, loudness Normal / previews High — **loudness starves, CPU workers idle**:

| Step | Event | Today | With fix |
|---|---|---|---|
| t0 | L1 got in before previews arrived | L1 P1 P2 | L1 P1 P2 |
| t1 | P1 done | L1 P2 P3 | L1 P2 P3 |
| t3 | L1 done | **P3 P4 P5** (no loudness until < 3 previews are left) | L2 P3 P4 |

Mirror, cap 3, loudness High / previews Normal — **the owner's fear, exactly**:

| Step | Event | Today | With fix |
|---|---|---|---|
| t0 | 3 High loudness took every slot | L1 L2 L3 | L1 L2 L3 |
| t1 | L1 done | **L2 L3 L4** (previews wait for ~18 more loudness jobs; 5 GPU workers idle) | L2 L3 P1 |

### 7.3 Verdict

* **No starvation in the owner's exact setup** (all Normal, cap 3 or 5): P1 starts at t0 while 19–17 loudness jobs
  are still pending. Proof: rows (i)/(ii) and the cited lines.
* **Starvation exists when one kind runs at High priority**, because the per-kind ceiling applies only to
  priority > High (`job_gate.py:329`) and `_can_admit` lets High fill every slot (`:302-305`). Then one kind holds
  every slot while the other kind's workers sit idle:
  * previews High (the default for webhooks, `web/jobs.py:265-281`) starve loudness/intro, which are Normal
    (`markers/triggers.py:655`) — CPU workers idle during a webhook burst;
  * loudness High (a manual/API job or a re-prioritised one) starves Normal previews — GPU workers idle until the
    loudness backlog drains. That is the owner's fear.
* Not causes: the per-kind `limit` (configured open workers, not free) never blocks another kind; slots are never
  held by a kind with no open workers (§7.1 item 7); the dispatcher never makes one kind's items wait for another
  kind's workers (§7.1 item 8).
* Separate from starvation: a slot is a job, so with single-file jobs a kind uses at most as many workers as it
  holds slots (e.g. cap 3, Normal: one preview job → one busy GPU worker of five). That is the global job limit
  doing its job; raise **Max concurrent jobs** to change it.

### 7.4 Fix — "a kind that holds no slot goes first" (Lane D)

While some job kind holds no slot and has a waiter that could start now, only that kind's waiters may be admitted.
Every kind with work and open workers therefore gets one slot before any kind takes another, at every priority.
Inside one kind, and once each kind has a slot, priority and FIFO decide as today. No preemption: running jobs are
never stopped; the next free slot goes to the waiting kind.

`web/job_gate.py`:

```python
    def _slots_by_kind(self) -> dict[str, int]:
        held: dict[str, int] = {}
        for (kind, _priority), count in self._kind_active.items():
            held[kind] = held.get(kind, 0) + count
        return held

    def _kind_can_admit(self, kind: str | None, priority: int, cap: int) -> bool:
        ...                                   # body unchanged except:
            ceiling = max(1, self.effective_cap(priority, cap) - 1)

    def _next_eligible(self, cap: int) -> object | None:
        readiness = {request.token: request.ready for request in self._requests.values()}
        eligible = [
            entry
            for entry in self._heap
            if readiness.get(entry[2], True)
            and self._can_admit(entry[0], cap)
            and self._kind_can_admit(self._waiter_kinds[entry[2]], entry[0], cap)
        ]
        # A kind holding no slot goes first, so one kind never fills every slot while another kind's workers idle.
        held = self._slots_by_kind()
        first_slot = [entry for entry in eligible if not held.get(self._waiter_kinds[entry[2]] or "", 0)]
        if any(self._waiter_kinds[entry[2]] is not None for entry in first_slot):
            eligible = first_slot
        return min(eligible, default=(0, 0, None))[2]
```

…and in `acquire` (`:425`) `resource_blocked = not self._kind_can_admit(kind, priority, cap)`. Kind-less legacy
waiters (tests only; every production caller passes a kind) stay eligible alongside first-slot kinds. Update the
module docstring with one bullet ("**One slot per waiting kind first** — …") and the `acquire` docstring.

Why it is safe:

* Never empties the candidate set and only filters waiters that are already admissible → no deadlock; a kind's
  first slot is never blocked by this rule → always progress.
* Only kinds that could start *now* count, so an off-hours or zero-capacity kind (`_kind_limits[kind] == 0`) never
  holds a slot back (test below).
* The High reservation and the static ceiling still apply (filter runs on top of `_can_admit`/`_kind_can_admit`).
* Lock order: everything read (`_heap`, `_requests`, `_waiter_kinds`, `_kind_active`, `_kind_limits`) is gate state
  already guarded by `_cond`, which both callers hold (`:403-409`, `:431-434`). No provider or settings call is
  added; passing `cap` removes the existing settings read under `_cond`.
* Wait text: a waiter held back by this rule shows the normal "Queued — waiting for active slot (X of Y busy)".

Evidence (scratch prototype, same logic): the test below fails 4 of 9 on today's gate and passes 9 of 9 with the
rule; `tests/test_job_gate_resources.py`, `test_job_gate_ordering.py`, `test_priority.py` (75) and the
`-k "gate or priority or admission or job_runner or loudness_job or worker_group or dispatcher or concurren"`
subset (1,341) pass with the rule applied.

### 7.5 Test that locks it in — `tests/test_job_gate_kind_share.py` (new)

```python
"""Every job kind with work and open workers gets a slot before any kind takes another, at every priority."""

import threading
import time

import pytest

from media_preview_generator.web.job_gate import JobGate
from media_preview_generator.web.jobs import PRIORITY_HIGH, PRIORITY_NORMAL

HIGH, NORMAL = PRIORITY_HIGH, PRIORITY_NORMAL


def _admitted_after_queueing(cap: int, limits: dict, jobs: list[tuple[str, str, int]]) -> list[str]:
    """Register every job first (as job_runner's restart drain does), then let each runner ask for a slot."""
    gate = JobGate(lambda: cap, kind_capacity_provider=lambda kind: limits[kind])
    gate._POLL_SECONDS = 0.005
    for index, (name, kind, priority) in enumerate(jobs):
        gate.register_request(
            name,
            created_at=f"2026-10-07T00:00:{index:02d}",
            priority=priority,
            kind=kind,
            policy=lambda _done, priority=priority: (priority, True),
        )
    admitted: list[str] = []
    stop = threading.Event()

    def run(name: str, kind: str, priority: int) -> None:
        if gate.acquire(priority, stop.is_set, kind=kind, request_id=name):
            admitted.append(name)

    threads = [threading.Thread(target=run, args=job, daemon=True) for job in jobs]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(admitted) < cap:
        time.sleep(0.01)
    time.sleep(0.1)  # nothing else may sneak in once the gate is full
    stop.set()
    for thread in threads:
        thread.join(1)
    return admitted


def _jobs(loudness: int, loud_priority: int, previews: int, preview_priority: int) -> list[tuple[str, str, int]]:
    return [(f"L{i}", "loudness", loud_priority) for i in range(1, loudness + 1)] + [
        (f"P{i}", "previews", preview_priority) for i in range(1, previews + 1)
    ]


@pytest.mark.parametrize(
    ("cap", "loud_priority", "preview_priority", "expected"),
    [
        # The owner's fear: urgent loudness must not hold every slot while GPU preview workers sit idle.
        (3, HIGH, NORMAL, {"loudness": 2, "previews": 1}),
        (5, HIGH, NORMAL, {"loudness": 4, "previews": 1}),
        # The production mix (webhook previews default to High): loudness keeps one slot for the CPU workers.
        (3, NORMAL, HIGH, {"loudness": 1, "previews": 2}),
        (5, NORMAL, HIGH, {"loudness": 1, "previews": 4}),
        # Equal priority already shared slots; the third/fifth slot stays reserved for high priority.
        (3, NORMAL, NORMAL, {"loudness": 1, "previews": 1}),
        (5, NORMAL, NORMAL, {"loudness": 3, "previews": 1}),
    ],
)
def test_waiting_kind_with_free_workers_gets_a_slot_when_twenty_loudness_jobs_are_ahead(
    cap, loud_priority, preview_priority, expected
):
    admitted = _admitted_after_queueing(
        cap, {"loudness": 5, "previews": 5}, _jobs(20, loud_priority, 10, preview_priority)
    )
    assert {kind: sum(name.startswith(kind[0].upper()) for name in admitted) for kind in expected} == expected


def test_one_kind_alone_still_uses_every_slot():
    admitted = _admitted_after_queueing(5, {"loudness": 5, "previews": 5}, _jobs(10, HIGH, 0, NORMAL))
    assert len(admitted) == 5


def test_kind_that_cannot_run_now_does_not_hold_a_slot_back():
    # Previews have no open workers (off-hours or no compatible member): loudness may take every slot.
    admitted = _admitted_after_queueing(5, {"loudness": 5, "previews": 0}, _jobs(10, HIGH, 3, HIGH))
    assert sorted(admitted) == ["L1", "L2", "L3", "L4", "L5"]


def test_high_priority_reservation_still_holds_back_the_last_slot_for_urgent_work():
    admitted = _admitted_after_queueing(3, {"loudness": 5, "previews": 5}, _jobs(5, NORMAL, 5, NORMAL))
    assert len(admitted) == 2  # one loudness, one preview; the third slot stays free for priority 1
```

Add one release-path case to `tests/test_job_gate_resources.py` (uses its `Waiter`/`setup_gate` helpers): cap 3,
three High loudness holders, one Normal preview waiting → `release(HIGH, kind="loudness")` → the preview is
admitted, the fourth loudness waits.

---

## 8. Test matrix (per `.claude/rules/testing.md`: every cell, assert the kwargs the SUT controls)

Branching variables: group shape (v21 flat / v22) · member resource (CPU / GPU) · job kind (previews / intro_credits /
loudness) · group (enabled / disabled; always / scheduled-open / scheduled-closed) · hardware (detected / missing) ·
count edges (0, 1, 32, 33) · weekly peak (≤ 32 / > 32) · member operation (add / remove / device change / count
change) × worker state (idle / busy) · endpoint (PUT / member scale / group scale single / group scale multi /
legacy workers add-remove / settings POST) · revision (current / stale) · gate (priority pair × cap 1, 2, 3, 5 ×
kinds held).

| File | Cases |
|---|---|
| `tests/test_worker_group_policy.py` | v22 valid; v21 flat → one member `m1`; members present → echo fields ignored; 0 and 9 members refused; duplicate member id; duplicate device (CPU twice, same GPU twice) refused with exact message; loudness on GPU member refused; count 0/1/32/33; peak across two groups' members > 32 refused, = 32 accepted; key order and `job_types` order stable; `member_policies` ids `g:m`, fields copied from group (name, enabled, availability) and member. |
| `tests/test_worker_group_migration.py`, `tests/test_upgrade.py` | v21 → v22 keeps id/name/enabled/availability/count/job_types/device, member `m1`, revision +1, one note with `_USER_FACING_NOTES[22]`; already-v22 list → no change, no note; v20 → v22 runs v21 then v22 no-op (one v21 note only); invalid stored list raises; `_CURRENT_SCHEMA_VERSION == 22`; backup written before migrating. |
| `tests/test_worker_groups_runtime.py` | reconcile creates `count` workers per member with `group_id`, `member_id`, `policy_id`, `allowed_job_types` (assert all four); GPU member gets that device's `ffmpeg_threads`; member count down with busy workers → `_pending_removal` on the busy ones, idle removed; member removed / device changed while busy → retiring, completions still reaped; two groups' members on one GPU share `_resource_targets` (no oversubscription during handover); unchanged save → zero workers retired; older revision ignored; scheduled group closed → targets 0 for all its members; missing GPU → only that member's target 0. |
| `tests/test_worker_group_parking_handoff.py` | job parks only when no open member of any group supports its kind; a second group's open member keeps it running. |
| `tests/test_worker_group_restart_policy.py`, `tests/test_worker_group_pause_lifecycle.py`, `tests/test_live_cpu_worker_reconcile.py` | fixtures to v22; behaviour unchanged. |
| `tests/test_worker_groups_api.py` | GET: groups with members, single-member echo present, multi-member echo absent, capacity group sums = member sums, member states incl. `hardware_unavailable` on one member while group `active`, draining removed member row, warnings carry `member_id`. PUT: v22 ok, v21 flat ok (→ `m1`), stale revision 409, invalid 400 with exact message. Member scale: +1/−1 (assert saved count **and** that only that member changed), at 1 → 400, at 32 → 400, disabled group → 409, unknown group/member → 404, peak → 400, revision +1, reconcile called. Group scale: single-member delta works (and −1 at 1 disables, as today), multi-member delta → 409 with member ids, `enabled` works for both. |
| `tests/test_routes.py` | `/api/workers/add|remove` with one matching member / several (409 lists `group_id` + `member_id`) / `member_id` given; settings POST `cpu_threads`, `gpu_config[].workers`, `gpu_config[].enabled` → right member, new one-member group when none. |
| `tests/test_dispatcher_worker_status_contract.py` | statuses carry `member_id` (busy, idle, retiring). |
| textdet helper (`tests/test_live_cpu_worker_reconcile.py` or a new small test) | `_configured_cpu_workers` sums CPU members allowing intro that are available; GPU members and loudness-only CPU members excluded. |
| `tests/test_job_gate_kind_share.py` (new) + `tests/test_job_gate_resources.py` | §7.5. |
| `tests/e2e/test_worker_groups.py` | editor: add device, device picker disables used devices, GPU row loudness chip disabled with tooltip, CPU→GPU switch removes loudness with note, remove device, last-device refusal, Apply sends exact payload (assert member ids preserved), server 400 peak message shown, 409 conflict flow, 390 px: no horizontal scroll and every control ≥ 44 px. |
| `tests/e2e/test_workers_dense_table.py` | 3 + 3 group stays cards; a 5-worker member turns the table on; cap 8 + "Show N more · k running" per member; expansion persists per `group:member`. |
| `tests/e2e/test_ui_workers_panel.py` | member rows render under one card; stepper posts to `/api/worker-groups/<gid>/members/<mid>/scale` with `{"delta": 1}` (assert URL and body); retiring member row; missing-GPU chip. |
| `tests/e2e/_mocks.py`, `tests/e2e/test_wizard_step4_processing.py`, `tests/e2e/test_dashboard*.py`, `tests/e2e/test_worker_loudness_button.py`, `tests/e2e/snapshots/readme_fixture.py` | payloads to v22; behaviour unchanged. |

---

## 9. Rollout and migration plan

1. **Phase 0 — gate fix alone** (Lane D): PR → CI (zero failing checks, run TZ tests under `TZ=UTC` too) → merge →
   sflix → live check: queue a High manual loudness job of many files plus a Normal preview scan; the preview job
   reaches Running within one release.
2. **Phases 1–3 — members** on one branch: arch review (block on HIGH) → PR → CI → merge to `dev` → sflix.
3. **Before deploying to sflix:** the migration writes `settings.json.<timestamp>.bak` automatically; also keep the
   previous container renamed as rollback (deploy recipe in memory). A rollback needs that `.bak`: the old binary
   refuses a v22 file.
4. **Live check on sflix:** `GET /api/worker-groups` → every existing group has one member `m1` with the old
   count/device/job types; revision +1; dashboard worker counts identical to before; bell shows the v22 note once;
   member stepper +1/−1 works; a preview job and a loudness job both run.
5. Release only when the owner says "release".

---

## 10. Build plan — lanes and file ownership

Each file has exactly one owning lane. Lanes start from the same base commit (fast-forward worktrees to it first).

| Phase | Lane | Owns (may edit) | Depends on |
|---|---|---|---|
| 0 | **D — gate fix** | `web/job_gate.py`, `tests/test_job_gate_kind_share.py`, `tests/test_job_gate_resources.py` | nothing (ship first) |
| 1 | **A — schema & migration** | `worker_groups.py`, `upgrade.py`, `web/settings_manager.py`, `config/__init__.py`, `config/validation.py`, `tests/test_worker_group_policy.py`, `tests/test_worker_group_migration.py`, `tests/test_upgrade.py` | nothing. Lands `member_policies`, `legacy_group_view`, validator first so B/C/E code against real functions. |
| 2 | **B — runtime** | `jobs/worker.py`, `jobs/group_runtime.py`, `jobs/dispatcher.py`, `jobs/parking.py`, `web/jobs.py`, `web/routes/job_runner.py`, `markers/job_runner.py`, `markers/credits/textdet_helper.py`, runtime tests in §8 | A |
| 2 | **C — API** | `web/routes/api_worker_groups.py`, `web/routes/api_jobs.py`, `web/routes/api_settings.py`, `web/routes/api_servers.py`, `tests/test_worker_groups_api.py`, `tests/test_routes.py` | A; uses `pool.member_snapshots()` from B (agree the row shape in §3.1 / §4) |
| 2 | **E — frontend** | `static/js/worker_groups.js`, `static/js/app.js`, `static/js/week_graph.js`, `static/css/worker_groups.css`, `static/css/pages/settings.css`, `static/css/dashboard.css`, `templates/index.html`, all `tests/e2e/*` listed in §8 | API contract §3 (build against `_mocks.py`) |
| 3 | **F — docs** | `docs/reference.md` (worker_groups table, endpoints, example), `docs/guides.md` §"Worker groups and availability", `docs/llms-full.txt` (regenerate) | C, E |
| 3 | Review | Architecture Review agent on the whole branch; one final whole-branch review | all |

Suggested models: D, A, B, C, E, F implement from this spec (`sonnet`); arch review on its own agent; the final
whole-branch review once.

---

## 11. Docs to update (Lane F)

* `docs/reference.md:139-158` — worker_groups field table → group fields + member fields; limits (8 members, one per
  device); deprecated echo note. `:1328-1345` — endpoint table adds the member scale route; legacy `delta` note;
  example JSON in v22.
* `docs/guides.md:252-275` — "A group has: Name, Devices (each with workers and allowed jobs), Availability";
  the example becomes the Off-hours group; steppers per device; "Devices shared by several groups add their worker
  counts".
* Check `docs/faq.md`, `plex-loudness-normalization.md`, `skip-intro-credits.md`, `plex-preview-thumbnails-gpu.md`,
  `plex-preview-thumbnails-slow.md`, `emby-bif-thumbnails-gpu.md`, `jellyfin-trickplay-gpu.md`,
  `sonarr-radarr-preview-thumbnails.md` for "a group runs on one device" wording; delete wrong specifics rather than
  re-explaining.
* `python scripts/generate_llms_full.py`, then `tests/test_docs_site.py`.
