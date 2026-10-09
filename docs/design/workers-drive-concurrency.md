# Workers drive concurrency (drop `max_concurrent_jobs`)

Branch: `feat/workers-drive-concurrency` (from dev `195ac2a`).

## Problem

`JobGate` (`web/job_gate.py`) holds a slot for a job's whole run. With `max_concurrent_jobs=5`, one slot reserved
for high priority and a per-kind ceiling of `cap-2`, five one-file webhook jobs fill the gate while CPU loudness
workers sit idle (seen live on sflix 2026-10-09: "4 of 5 busy, 1 reserved for high priority", loudness at its
3-slot ceiling, 4 CPU loudness workers mostly idle). The dispatcher already orders files by priority across all
jobs and only hands a file to a worker whose group runs that job type, so the job cap only throttles throughput.

## Decision

1. **The gate only bounds start-up.** A job takes a slot before setup (config load, server queries, building its
   file list), and gives it back as soon as its files are submitted to the dispatcher. The size is a fixed
   internal constant `STARTUP_SLOTS = 3`, not a setting. Its only job is to stop a webhook burst from hammering
   Plex/Emby/Jellyfin with dozens of simultaneous enumerations.
2. **Gate rules that existed only because slots were held for hours go away:** the high-priority reserved slot
   (`HIGH_PRIORITY_RESERVED_SLOTS`, `effective_cap`, `_active_high`), the per-kind ordinary ceiling, and the
   "one slot per waiting kind first" rule. Kept: priority then FIFO order, `register_request` readiness policy
   (dependencies, retry deadlines, pause), and "a kind with zero open workers waits" (`admission_capacity == 0`).
3. **Pause no longer hands a slot back.** A job past start-up holds no slot, so the pause-by-hand release /
   re-acquire machinery is deleted (`_wait_releasing_slot_while_paused`'s release/acquire half, the markers and
   loudness equivalents, `JobDispatcher.release_when_idle`, `mark_slot_reacquired`, `JobTracker.slot_released`,
   `_all_slots_released`). A paused job simply gets no new files picked (`tracker.is_paused()` already filters).
   The two start-up steps that can wait out a pause or closed worker hours (loudness resume validation, the
   Check servers listing) give their start-up slot back before waiting and don't take it again.
4. **Dispatcher rotates between jobs of the same priority.** Pick order becomes
   `(priority, last_picked, submission_order)`, where `last_picked` is stamped from a dispatcher-wide counter each
   time one of the job's files is handed to a worker (0 for a job never picked). A 5,000-file Normal scan no
   longer makes a later Normal webhook wait behind all its files. Applies to both the item pick
   (`_assign_tasks`) and the check pick.
5. **Status text.** A job whose files are submitted but none started shows `Running` with current item
   "Waiting for a free worker" until its first file starts (owner chose this over keeping it Queued). Gate wait
   text becomes "Queued — waiting to start (X of 3 jobs starting up)"; `app.js` shortens it to "Waiting to start".
6. **Setting removed.** `max_concurrent_jobs` leaves the API (GET/POST lists, validation), the Settings page, and
   docs. A POST that still sends it is ignored, not rejected. Schema migration v23 deletes the key from
   `settings.json` and adds a user-facing note.

## Risk / review

Concurrency + settings schema → Architecture Review required before commit (CLAUDE.md). Points to check:
- No path still releases a slot it no longer holds (double release) or never releases one (leak) — every
  runner's `finally` must release only while `held`.
- Post-run work (outcome, publisher rows, follow-up/retry jobs) runs without a slot; follow-ups take their own.
- Parking (`jobs/parking.py`) and restart revival still work without slot hand-back.

## Tests

- Dispatcher round-robin: job A (5 items) then job B (1 item), same priority, one worker → B's item is picked
  2nd, not 6th. Higher priority still wins outright.
- Start-up slot released after submit: with 1 start-up slot, job 1 submitted and still running does not block
  job 2's start-up.
- Gate: no settings read; zero-open-worker kind waits; priority/FIFO order kept.
- Migration v23 drops `max_concurrent_jobs`; POST with the key is accepted and ignored.
