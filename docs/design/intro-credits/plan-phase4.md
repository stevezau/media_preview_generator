# Intro & Credits — Phase 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The leftovers a user has to fix by hand become fixable in the app. The Inspector gains an Adjust / Lock
editor that saves, locks and publishes to every owning server at once; anime credits stop being missed because the
chapter is called "Ending"; Setup Health says why a server can't take markers before the user finds out from a job; Plex on another machine gets a
helper container that does the database write; the user docs stop saying "comes in a later update"; and the evidence
folder is trimmed to what a release should carry.

**Architecture:** No new package. The lock data model, the "a lock always wins" rule, the `user` evidence lane and the
Season view's 🔒 chip already exist (see "What the branch already does"); phase 4 adds the missing write path
(`POST /api/markers/item/markers` → `MarkerStore.lock_marker` → the existing per-server publish), the editor UI in
`markers_inspector.js`, an anime-scoped chapter-title rule in `markers/sources/chapters.py`, marker sections in
each vendor's `previews_readiness()` (the Setup Health tab renders them with no JS change), and a small separate
container that exposes the Plex database write over HTTP so `publishers/plex_db.py` can run it on the Plex host.

**Tech Stack:** unchanged — Python 3.12 (image) / 3.14 (dev venv), Flask, SQLite, requests, Bootstrap 5 + vanilla JS,
pytest, Playwright, Docker. The helper container is the same stack (its shape is owner question Q3).

**Spec:** `docs/design/intro-credits/spec.md` (Revision 3) — read §0 first, then §2 goal 3, §3.1 (Plex Pass, tag row,
DB location, what wipes markers), §4 (the AniSkip row — answered, see parked items), §5.1 (chapter titles and the
cold-open rule), §5.5 rules 1, 2, 7, 8, §6.2 steps 5–8,
§6.3 (`Capability`, the Plex/Emby kept-type rules), §7 items 1, 3, 4, 6, §8, §10.2, §10.3, §11, §12 phase 4, §13 items
1, 4, 5, 17, 18, §14 (2026-09-13 "helper container on the Plex host later", 2026-09-14 R1/R5, 2026-09-16 Q4's gate
shape, 2026-09-16 importer groups, 2026-09-19 Plex ownership). Roadmap and its Global Constraints:
`plan-roadmap.md` ("Phase 4 — Polish"). Owner-approved design artifact: `evidence/design/index.html` (the Inspector
header already shows `✎ Adjust  🔒 Lock  ↻ Re-detect  Publish to 3 servers`; the Season view already shows
`🔒 Locked by you`). Phase-3 plan and ledger for the working pattern: `plan-phase3.md`,
`.superpowers/sdd/plan-phase3/progress.md`.

## Global Constraints

Every task implicitly includes these. The roadmap's Global Constraints still bind; the lines that matter in phase 4 are
repeated so an implementer who sees only one task has them.

**Carried over (owner rules, spec §0, §1, §5.6 and the roadmap):**
- **Precision over coverage:** a missing marker is acceptable; a wrong one is not. A new source publishes nothing on
  its own until it is measured against the chapter truth, in the gate style phases 2 and 3 used. A user's own marker
  is the one exception — it is the user's call.
- Feature is **off until turned on per server**. Nothing is detected for a file with no enabled owner, and the editor
  publishes only to owners whose switch is on.
- File identity = canonical path + size + mtime. A change drops evidence and **unlocked** markers; locked markers
  survive (`store.py` `DELETE … WHERE locked=0`). All marker times are integer **milliseconds**.
- **Lab on storage only** (`evidence/lab/`: `mlab-plex`, `mlab-jellyfin`, `mlab-jf12`, `mlab-emby`, `mlab-emby49`,
  `mlab-app`), **never the prod Plex on `plex`**. Prod Plex DB: read-only (`sqlite3 "file:<db>?mode=ro"`). **Phase 4
  needs nothing on the `plex` host** — the helper container is proven with two containers on storage (Task 13 row 12).
  If a task ever looks like it needs `plex`, ask the owner and quote this rule in the question.
- **Never delete or write files under `/data*`** (the owner's library, both hosts). Lab mounts are `:ro`. The anime chapter
  measurements only `stat`/`ffprobe`-read real files.
- **No real library paths, file names or release groups in committed files.** Anime chapter truth sets and per-file
  JSON stay local (git-ignored), as in phases 2 and 3.
- **Secrets:** TheIntroDB's key stays masked as `****` and never logged. Whatever credential the helper container
  uses gets the same treatment (masked in every API response, never logged, round-trips unchanged when `****` is
  posted back). Never commit `evidence/lab/env`.
- **Resource rule:** storage is shared. `nice -n 19` for harness and lab work. Marker work stays on the existing
  WorkerPool. The editor's publish is not job work and must not take a worker slot or a job-gate slot.
- Tests follow `.claude/rules/testing.md`: mock HTTP/filesystem/servers in unit tests, assert the kwargs, SQL and
  request bodies the code controls (not call counts), cover **every cell** of a branchy matrix (server type × capability
  × kept types × locked/unlocked is this phase's biggest matrix). Tests needing real servers or real media are
  `integration`/`gpu` and never run in default CI.
- Code style: ruff (line 120), type hints everywhere, Google docstrings on public APIs, `from loguru import logger`,
  comments explain *why* only. Implementers run `pre-commit run --files <staged files>` before reporting.
- **UI checkpoint (owner, non-negotiable):** new visible wording or layout → mockup or screenshot to the owner
  **before** building it across files. Task 2 collects every phase-4 surface into one pack so this is one checkpoint,
  not five. Every non-obvious control gets an ⓘ tooltip (`.info-icon` pattern, `_initBootstrapTooltips`).
- Git: branch `feat/markers-detection` (PR #241 → `dev`). Conventional Commits. **Before every commit run the
  `Architecture Review` agent** on the staged diff; HIGH blocks, MED is discussed. Commit with
  `PATH="/home/data/.venv/bin:$PATH" git commit`. Never commit to `dev`/`main`. **Nothing merges into `dev` until the
  owner says it is fully tested.** Releases only on the owner's explicit word "release".
- Update the spec (not just code) whenever a decision changes; add a dated line to spec §14.
- Test commands (storage): `nice -n 19 /home/data/.venv/bin/python -m pytest --no-cov <files>` for a task;
  `nice -n 19 /home/data/.venv/bin/python -m pytest` (full) before each push; `… -m e2e -n 8 --no-cov` for UI tasks
  (never `-n auto`). In a worktree lane: `PYTHONPATH=<worktree>`, `-n 4`.
- **Stable local paths, never a session scratchpad.** Shared venv `/home/data/.venv`. Local-only phase-4 artifacts go
  in the main checkout's git-ignored `docs/design/intro-credits/evidence/online/phase4/` (Task 1 adds the ignore rule).
  Owner-bound screenshots and the PR body draft go to `.superpowers/sdd/plan-phase4/` (git excluded).

**Phase-4 additions:**
- **A save is a publish.** Spec §6.2 step 8: "no job — save, lock, publish to every owner immediately". So one HTTP
  request writes a Plex database and calls two plugins. It must be bounded: see ruling **P-R1**.
- **Nothing new blocks the web worker.** The app runs one gunicorn worker with threads; a save that waits on an
  unreachable server for 30 s blocks a thread. Per-server deadline, per-server result, no retries inside the request.
- **No new runtime dependency** unless Task 1 proves the MAL id needs one (owner question Q2b). A new pinned
  dependency needs the owner's OK and an image-size number, as in phase 3.
- **AniSkip does not ship** — measured in Task 1 and not taken (parked items; `evidence/eval/aniskip-facts.md`).
  Spec §5.5 rule 8's interim ruling, which files an AniSkip importer's copies with IntroDB + TheIntroDB, becomes
  **permanent** on that measurement (Task 11).
- **The helper container is the only way markers reach a Plex on another host.** Without it, Plex stays read-only with
  the message it has today (`Capability.NEEDS_LOCAL_DB`). The helper never becomes a general remote-control API.

## What the branch already does (checked 2026-09-20, `6f098b1`)

Reading the code before planning turned up more finished work than the roadmap line suggests. None of this is built
again.

- **The lock model is complete end to end, minus a caller.** `markers` table has `locked` (`store.py:84`);
  `MarkerStore.lock_marker()` (`store.py:855`) exists with the docstring "the Inspector editor calls this in phase 4"
  and **no route calls it**; `get_locked()` (`:851`); `save_decisions` refuses to overwrite a locked row (`:796–822`);
  a file-identity change deletes only `locked=0` rows (`:515`); `decide()` takes `locked` and emits
  `"locked by user"` (`decide.py:645–665`) and skips locked markers in the overlap checks (`:629–673`);
  `source_counts.py:37` reports a locked marker's source as `user`; `inspect.py:684` already publishes
  `"locked": marker.locked` in the payload.
- **The UI already renders locks.** `markers_inspector.js:36` has the `user` → "Your marker" lane;
  `markers_season.js:98–99` renders the `🔒 Locked by you` chip with `.mk-chip-locked` (`markers_inspector.css:262`).
- **`markers.respect_locks` exists** as a setting, a UI switch (`settings.html:521`) and a typed accessor — and is
  deliberately **excluded from `detection_fingerprint()`** (`settings.py:106`). Don't "fix" that without reading why;
  Task 3 Step 3 writes the reason into the docstring.
- **Setup Health is fully data-driven.** The envelope every vendor returns is specified in
  `servers/base.py:693–745`; `servers.js renderReadiness()` (`:1976`) renders it with no vendor branching,
  `_partitionChecks` (`:2122`) buckets Must fix / Recommended / All good and `_renderValueDiff` (`:2844`) draws
  current → recommended. A new check needs **no JS** — only an entry in a vendor's `previews_readiness()` following
  that envelope. Two constraints the JS imposes on the backend: an `info` row is dropped (`:2138`, `:2262`), and the
  plugin install controls key on `section.id === 'plugin'` with the first check's `current` (`:1929`, `:2669`).
- **The facts the Setup Health checks need are already computed** for the server Edit tab: `capability()` returns
  `NEEDS_PASS`, `NEEDS_LOCAL_DB`, `NEEDS_PLEX_DETECTION_ONCE`, `NEEDS_PLUGIN`, `PLUGIN_OUTDATED` (spec §6.3), and
  `GET /api/markers/servers/<id>/status` already renders them. Task 9 reuses that, it does not re-probe.
- **AniSkip is only ever someone else's importer, and that is now the permanent answer.**
  `sources/server_markers.py:32–38` matches an AniSkip importer plugin by name and `decide.py:45–49` files its copies
  in the IntroDB group. There is no AniSkip client and no `Source.ANISKIP` — and none is coming (parked items).
  Measured: no MAL id exists anywhere in this library (0 of 4,651 anime episodes; every TV part carries a tvdb token
  and nothing else), and AniSkip matches IntroDB to ≤ 44 ms on 21 % of intros where the two intro databases already
  counted as one source match on 10 %.
- **`markers/job_runner.py:60`** already treats `manual`, `inspector` and `inspector_season` as user-picked job
  sources (`_USER_PICKED_SOURCES`, reused at `:71`).
- **The helper container does not exist** in any form. Two words in the codebase look like it and are not: `sidecar`
  in `processing/multi_server.py` means an Emby BIF file on disk, and `helper` in `markers/credits/` means a text
  detection subprocess. **Pick a distinct name** — this plan uses **Plex marker agent** (`plex-marker-agent/`).

## Measured and closed (Task 1, 2026-09-20 — `evidence/eval/aniskip-facts.md`, `c53d8c6`)

All eight facts this plan listed for AniSkip were measured against the live service and the owner's real library, and
the answer is that **AniSkip does not ship** (parked items). The API itself was never the problem: anonymous, one GET,
seconds not milliseconds, 404 for no data, and it does check the file's cut — to ±20 s, ranked by votes rather than by
closeness. What stopped it:

1. **No MAL id exists anywhere in this library** — 0 of 4,651 anime episodes resolve without a mapping file, and
   neither candidate map (Anime-Lists/anime-lists, Fribb/anime-lists) carries a LICENSE.
2. **It fails the owner's own Q4 gate** — 5.1 % of anime intros wrong by more than 15 s against the 2 % Medium cap,
   and tightening the episode-length band to ≤ 2 s doesn't help (5.2 %).
3. **It isn't a coverage win** — on 120 resolved anime episodes it is the only source with an answer for 2 intros and
   0 credits; IntroDB covers anime better.
4. **It probably isn't independent** — it matches IntroDB to ≤ 44 ms on 21 % of intros, where the two intro databases
   already counted as one source match on 10 %, and not through chapters.

Two findings from the same work **are** worth taking, and became Task 15: the chapter classifier ignores `Ending`,
which on anime is the credits (238 files), and 581 of 1,080 anime intro chapters are a lone generic `Intro` where
§5.1's cold-open rule can't fire.

## Open questions for the owner (Q2 and Q2b answered 2026-09-20; the rest still open)

Each question states what is at stake and the plan's recommended default. **No task that depends on one starts before
it is answered.**

- **Q1 · How does a locked marker meet "Keep Plex's" / "Keep Emby's"?** *(the roadmap's named open question)*
  Today, spec §6.2 step 6 says `keep_plex` / `keep_emby` keep the server's own markers **over** a locked one, "until
  the phase-4 lock editor settles how the two meet", while §5.5 rule 1 says a locked marker "wins, always". Both
  sentences are in the spec; they contradict each other, and nothing creates a lock yet so nothing has hit it.
  Three options:
  - **(a) A locked type bypasses the kept-type rule.** The user's marker is written to the server even under "Keep
    Plex's" / "Keep Emby's"; on Emby that means posting with `ReplaceOwn`. *For:* "I fixed this by hand" is the
    strongest signal in the system, and the user who set "Keep Plex's" was choosing between *detections*, not
    overruling themselves. It is also the only option where the Inspector's "Publish to N servers" does what its label
    says. *Against:* one setting silently stops applying to one type; a user who set "Keep Plex's" to protect a
    curated Plex library can have it overwritten by an edit made for a different reason (e.g. to fix Emby).
  - **(b) Kept types still win; the lock applies to every other server.** Today's behaviour, made permanent.
    *For:* the per-server setting means exactly what it says, always. *Against:* the user edits a marker, the server
    they were looking at doesn't change, and the only feedback is a row message. This is the shape most likely to be
    reported as a bug.
  - **(c) Per-edit choice.** The save dialog asks "also replace Plex's own marker on <server>?" when a kept type is
    involved. *For:* no rule is silent; the user decides with the facts in front of them. *Against:* a modal on a
    two-second action, another cell in every publish matrix, and a third state to store (`markers` would need a
    per-server override or a "replace kept" flag).
  **Plan's recommendation: (a)**, with the Inspector saying plainly on the server card that it is overriding the
  server's "Keep Plex's" / "Keep Emby's" setting for that type, and the row message naming it. Cost if wrong: a
  reversal is a rule change in `publishers/plex_db.py` and `emby.py` plus the matrix rows — cheap while nothing is
  locked in the wild, expensive after a release. **Whatever the answer, it rewrites spec §6.2 step 6 and §5.5 rule 1
  so only one of them survives** (Task 4).
- **Q2 · AniSkip's gate and its default — ANSWERED (2026-09-20): it does not ship.** Task 1's measurements
  (`evidence/eval/aniskip-facts.md`) fail the gate this question proposed: 5.1 % of anime intros wrong by more than
  15 s against a 2 % Medium cap, no coverage win, and probably not independent of IntroDB. **Blocks nothing.**
- **Q2b · Where the MAL id may come from — ANSWERED (2026-09-20) by the same measurement:** nowhere. 0 of 4,651
  anime episodes resolve from path tokens or from what Plex reports, and both candidate id maps are unlicensed.
  **Blocks nothing.** What would reopen both: a licensed id map **and** a gate pass (parked items).
- **Q3 · The Plex marker agent's shape.** Its own tiny image, or the same app image started in an agent mode? How is
  it authenticated (a token the user pastes into both sides)? Is it published to GHCR beside the app image, and under
  what tag? Does it ever do anything but the marker write? Recommended default: **its own small image**, one shared
  token, published beside the app image, and it exposes nothing but the marker read/write for one Plex database.
  Cost if wrong: a rebuild of the container and its docs, before anyone runs it.
- **Q4 · How long may a save take?** See ruling P-R1 — answer only if the recommended bound is wrong.
- **Q5 · Evidence trim.** What stays in the repo at release (spec §11 says trim before release). The lab bring-up
  script and tokens must move to a stable dev-tools location first (spec §10.3). Recommended default: keep the spec,
  the plans, the harness docs and the lab results; move `lab/` to a dev-tools folder; drop the prototypes, the raw
  measurement dumps and the design artifact source. **The owner decides what is deleted** — nothing is deleted on our
  own initiative.

## Plan rulings taken while planning (each with its cost if wrong)

- **P-R1 · A save publishes inline, with a deadline per server.** The request saves and locks first (that part must
  never be lost), then publishes to each enabled owner with a short bounded timeout, and returns a per-server result.
  A server that fails or times out is reported as such in the response and left to the next Check servers run — no
  retries inside the request, no background thread that outlives it. Cost if wrong: on a slow LAN the user sees
  "couldn't reach <server>" and the marker lands a run later, instead of the request hanging.
- **P-R2 · The editor clamps, it doesn't judge.** A user marker keeps exactly two of §5.5 rule 2's bounds, enforced
  server-side: it must be inside the file, and it must end after it starts. **Every other bound in rule 2** (the 3 s
  minimum, the 300 s intro cap, intro/recap in the first 35 %, credits/preview in the last 25 %, the 900 s movie
  credits limit, "never running to the end of the file") is **not** applied to a user's own marker — those exist to
  catch a source that is wrong, and a user marking a 2 s intro is not wrong. The UI warns; it does not refuse. Cost
  if wrong: a user can publish an unusual marker they asked for.
- **P-R3 · Adjusting locks.** There is no "edited but unlocked" state. Save = lock (the owner-approved artifact says
  "Markers you adjust in the Inspector are locked" and "A marker you adjusted always wins and is never overwritten").
  The Lock button alone locks the decided times unchanged; Unlock drops the lock and the next run re-decides that
  type. Cost if wrong: an extra state to add later.
- **P-R4 · Route names are the plan's choice**, not spec: `POST /api/markers/item/markers` (save + lock + publish),
  `DELETE /api/markers/item/markers` (unlock). They sit beside `/api/markers/item/redetect` and reuse
  `_library_file()` (`api_markers.py:189`) as the path-safety gate. Cost if wrong: a rename before release.
- **P-R5 · Setup Health keeps `previews_readiness()`.** The marker checks are a new **section** in the envelope the
  three vendors already return, not a new endpoint and not a rename (the method name is already wrong for half of
  what it reports; renaming it touches three vendors, the routes, the JS, the e2e tests and a docs page for no user-
  visible gain). Cost if wrong: a rename task later, mechanical.
- **P-R6 · The marker sections only appear when the server has Intro & Credits switched on**, except one row saying
  so. That row is emitted as **`severity: "recommended"`, `ok: true`** (it lands in "All good"), **never `info`**:
  `servers.js _partitionChecks` drops every `info` row on purpose (`:2138`, repeated at `:2262` — "must be emitted by
  the backend as severity='recommended' or 'critical' — not 'info'"), so an `info` row would simply never render.
  Cost if wrong: a Setup Health tab that shouts about Plex Pass at users who never asked for markers.

## Spec contradictions and drift found while planning

- **D1 · §5.5 rule 1 vs §6.2 step 6** — "a locked marker wins, always" vs `keep_plex`/`keep_emby` keeping the
  server's markers over a locked one. Q1 resolves it; Task 4 rewrites whichever loses.
- **D2 · §6.2 step 8** ("no job — save, lock, publish to every owner immediately") sets no bound on a request that
  writes a database and calls two plugins. P-R1 bounds it; Task 11 writes the bound into §6.2.
- **D3 · (closed) §8's settings block has no AniSkip entry** — and never gains one: phase 4 adds no source, so
  `SOURCE_IDS`, `DEFAULT_GLOBAL_MARKERS` and `_CURRENT_SCHEMA_VERSION` (15) are all untouched.
- **D4 · §5.5 rule 8's interim ruling becomes permanent.** It files an AniSkip importer's copies with IntroDB +
  TheIntroDB "until phase 4 measures what they copy". Phase 4 measured it: AniSkip matches IntroDB to ≤ 44 ms on
  21 % of intros, against 10 % between the two intro databases already counted as one source, and not through
  chapters. Task 11 strikes "until phase 4 measures what they copy" from rule 8 and records the 21 % as the reason,
  in §5.5 and in a dated §14 line; `decide.py:45–49`'s comment loses its "isn't a source yet" wording and keeps the
  grouping.
- **D5 · The user docs promise this phase.** `docs/reference.md:290` ("The Inspector can't adjust or lock markers
  yet"), `docs/guides.md:624`, `:640`, `:681` ("comes in a later update") and `settings.html:518`'s
  `respect_locks` tooltip ("Editing and locking markers in the Inspector comes in a later update") all become false
  the moment Task 5 lands. Task 5 fixes the tooltip; Task 11 fixes the docs.
- **D6 · §7.6 lists Setup Health checks but §9's touchpoint table has no Setup Health row.** Task 11 adds it
  (`servers/base.py previews_readiness()`, `web/routes/api_servers.py`, `web/static/js/servers.js renderReadiness`).
- **D7 · §13 item 17 is still open** (markers this app wrote to Plex read as Plex's own after `markers.db` is lost).
  A lock makes the same loss worse: a lock lives only in `markers.db`, so losing it loses the user's own edit, not
  just its provenance. Task 11 records that under item 17; whether to write an ownership key into Plex's
  `extra_data` is the same pending owner decision, not re-opened here.
- **D8 · Emby and Plex can't show every type, and the two cases are different.** Plex and Emby take no recap or
  preview at all — `can_show` covers that, and the editor refuses those types for those servers. Emby's credits are
  **not** a refusal: spec §6.3 R1 says Emby always gets the decided credits **start**, even when the credits end
  before the file does, and the app already says so with `CREDITS_BEFORE_END_NOTE` ("Emby skips to the end of the
  file", `publishers/emby.py:44`), rendered today in `markers_server_tab.js:216`. So an edited credits **end** is
  accepted and published start-only on Emby, with that note. `can_show` is **type-level**, so it cannot carry this —
  the editor needs its own per-field note for the Emby credits end (Task 5 Step 4, Task 2's pack item 3).

## UI copy and mockups (Task 2 — one owner checkpoint for the whole phase)

Every visible surface phase 4 adds, drawn once, in the app's own theme, with real values, before any of it is built.
The approved design artifact already fixes the entry points (`✎ Adjust`, `🔒 Lock`, `↻ Re-detect`,
`Publish to N servers`, `🔒 Locked by you`) — the pack fills in what it never showed:

1. **The editor open.** What a marker bar looks like while it is being dragged (handles, the time readout, the
   snap/zoom behaviour), the keyboard hint, Save / Cancel, and what the two zoom windows do while editing.
2. **Lock and Unlock.** How a locked type reads in the decision lane, in the chips, and in the Season row; what
   Unlock says before it drops the user's marker.
3. **The publish result.** What the per-server cards show while saving and after: written / failed / not enabled /
   can't show this type (recap and preview on Plex and Emby), plus the per-field note for an edited credits end on
   Emby — accepted, published start-only, "Emby skips to the end of the file" (D8).
4. **The Q1 answer in words** — the sentence on the server card when an edit overrides that server's "Keep Plex's" /
   "Keep Emby's".
5. **Season view:** the per-row Edit affordance beside the existing Review button.
6. **Settings → Intro & Credits:** the corrected `respect_locks` tooltip. (No new source row — phase 4 adds no
   source.)
7. **Setup Health:** the marker rows for Plex (Pass missing, marker tag row absent, database not on this machine,
   Plex's own detection may overwrite) and for Jellyfin/Emby (plugin missing, plugin outdated), each with its
   current → recommended pair and its ⓘ. **No "Manual" chip** — it was deliberately removed
   (`servers.js:2302` "No separate Manual chip — the badge above already says it."); a row with no action uses the
   shipped `Change in <vendor> UI` badge instead.
8. **Servers → Edit → Intro & Credits:** the Plex marker agent block (address, connection state, version, the
   "Plex is on another machine" path).

Wording rules: plain English, present tense, no internal setting names in the first sentence (say what the user gets),
≤ 120 characters in a tooltip.

## Execution model

- **Lanes.** `[lane-parallel]` tasks share no files with the other tasks of their wave (see the conflict table) and run
  in git worktrees under `/home/data/workspace/p4-lanes/` with `PYTHONPATH=<worktree>`. The controller commits in the
  lane branch, cherry-picks onto `feat/markers-detection`, runs the full suite, pushes. `[sequential]` tasks start from
  the branch head after their dependencies land. **Verify every cherry-pick with `git cherry`** — a lane commit was
  silently skipped once before.
- **Reviews (speed mode, owner).** One combined review per task (spec compliance + Architecture Review shapes).
  `[high-risk]` tasks get a deep adversarial review (mutants, real data, lab) and a scoped re-review only after a
  HIGH. **Milestone audit** over the whole phase-4 diff after Tasks 5, 6, 9, 10 and 15 land, before the docs task —
  a gate, not a suggestion.
- **Architecture Review agent** runs on every staged diff before its commit, in parallel with the task review.
- **Waves.** W0: Task 1 (**done**, 2026-09-20) and Task 2 (Task 2 ends at the owner checkpoint). W1: 3, 9 and 15 in
  parallel, then 4 after 3 (**3 and 4 both touch `markers/pipeline.py`**; 9 and 15 share nothing with them or with
  each other). W2: 5 and 10. W3: 6. Gate: milestone audit. Then 11 (docs), 12 (evidence trim), 13 (lab), 14
  (close-out) in order. **Numbers 7 and 8 are retired** — the AniSkip source and its harness gate were measured and
  not taken (parked items); the surviving tasks keep the numbers they had.
- **High-risk tasks:** 3 (writes servers from an HTTP request), 4 (changes what reaches a server), 10 (writes a Plex
  database over a network). Task 15 changes what chapters mean, so it ships only what its measurement wins.

## Task order and dependencies

| # | Task | Depends on | Wave · lane | Risk | Visible UI | Files (C = create, M = modify) |
|---|---|---|---|---|---|---|
| 1 | AniSkip and anime-id measurements — **done 2026-09-20** (`c53d8c6`) | — | 0 · lane-parallel | | no | C `evidence/eval/aniskip-facts.md`, `evidence/online/phase4/*.py`; M `.gitignore` |
| 2 | Mockup pack + UI copy → **owner checkpoint 6** | — | 0 · sequential | | **yes (all of it)** | C `evidence/design/phase4.html`, `.superpowers/sdd/plan-phase4/shots/` |
| 3 | Marker write API + publish-now path | 2 (copy only), Q4 (P-R1's default applies unless the owner objects) | 1 · lane-parallel | high-risk | no | C `tests/markers/test_api_markers_edit.py`, `tests/markers/test_publish_now.py`; M `web/routes/api_markers.py`, `markers/pipeline.py`, `markers/store.py`, `markers/inspect.py` |
| 4 | Locked markers vs "Keep Plex's" / "Keep Emby's" (**Q1**) | Q1, 3 | 1 · sequential | high-risk | no | C `tests/markers/test_locks_vs_kept.py`; M `markers/publishers/plex_db.py`, `markers/publishers/emby.py`, `markers/pipeline.py`, `markers/decide.py` (comments), `docs/design/intro-credits/spec.md` (§5.5 rule 1, §6.2 step 6, §14) |
| 5 | Inspector Adjust / Lock editor | 2 (approved), 3, 4 | 2 · lane-parallel | | **yes** | M `web/static/js/markers_inspector.js`, `web/static/css/pages/markers_inspector.css`, `web/templates/bif_viewer.html`, `web/templates/settings.html` (D5 tooltip), `tests/e2e/test_intro_credits_inspector.py` |
| 6 | Season view: Edit per row | 5 | 3 · sequential | | **yes** | M `web/static/js/markers_season.js`, `tests/e2e/test_intro_credits_season.py` |
| 15 | Anime chapter titles: `Ending`, and the lone generic `Intro` | 1 | 1 · lane-parallel | | no | C `tests/markers/test_chapters_anime.py`, `evidence/eval/phase4-chapters.md`; M `markers/sources/chapters.py`, `tests/markers/test_chapters.py`, `tools/markers_eval/` (a before/after chapter run) |
| 9 | Setup Health: Intro & Credits checks | 2 (approved) | 1 · lane-parallel | | **yes** | C `tests/markers/test_readiness_markers.py`; M `servers/plex.py`, `servers/jellyfin.py`, `servers/emby.py`, `servers/base.py` (docstring), `tests/test_servers_plex.py`, `tests/test_servers_jellyfin.py`, `tests/test_servers_emby.py`, `tests/test_api_servers.py`, `tests/e2e/test_intro_credits_server_tab.py` |
| 10 | Plex marker agent (helper container) | Q3, 3 | 2 · lane-parallel | high-risk | **yes (Edit tab block)** | C `plex-marker-agent/` (app, Dockerfile, README), `markers/publishers/plex_remote.py`, `tests/markers/test_plex_remote.py`, `tests/test_plex_marker_agent.py`; M `markers/publishers/plex_db.py`, `markers/publishers/base.py`, `markers/settings.py`, `web/templates/servers.html`, `web/static/js/markers_server_tab.js`, `web/routes/api_markers.py` |
| — | **Milestone audit** (phase-4 diff) | 5, 6, 9, 10, 15 | gate | | | fixes in the owning task's files |
| 11 | Docs | 3–6, 9, 10, 15, audit | 4 · sequential | | | M `README.md`, `docs/reference.md`, `docs/guides.md`, `docs/guides/previews-readiness.md`, `spec.md`, `plan-roadmap.md`, `evidence/README.md` |
| 12 | Evidence trim + a stable home for the lab (**Q5**) | 11, Q5 | 5 · sequential | | | M/delete under `docs/design/intro-credits/evidence/`; C the lab's new home |
| 13 | Phase-4 lab matrix (+ rows in the phase 1–3 matrices) | 10, 11 | 6 · sequential | high-risk | | C `evidence/lab/phase4_matrix.py`, `evidence/lab/phase4-results.md`; M `evidence/lab/phase1_matrix.py`, `phase2_matrix.py`, `phase3_matrix.py`, `evidence/lab/up.sh`, `evidence/lab/app.sh` |
| 14 | PR, image, close-out | 13 | 7 · sequential | | | M `spec.md` §0/§12/§14, `plan-roadmap.md`, `evidence/lab/phase4-results.md`, `.superpowers/sdd/plan-phase4/progress.md`; PR body (REST — `gh pr edit` is broken here) |

All paths under `markers/`, `servers/`, `processing/`, `web/` are inside `media_preview_generator/`; `evidence/` is
`docs/design/intro-credits/evidence/`.

## Pre-flight conflict table

| Tasks | Shared file | Resolution |
|---|---|---|
| 3, 4 | `markers/pipeline.py` | 3 adds the publish-now path; 4 changes the kept-type rule inside the publishers. 4 starts after 3 lands (sequential in wave 1). |
| 3, 10 | `web/routes/api_markers.py` | 3 adds the write routes; 10 adds the agent status only. 10 starts after 3 lands (wave 2). |
| 4, 10 | `markers/publishers/plex_db.py` | 4 (wave 1) changes the kept-type write path; 10 (wave 2) moves the write behind a transport. Sequential. |
| 5, 6 | `web/static/js/markers_season.js` | Only 6 edits it (5 leaves the Season view alone). |
| 15, any | `markers/sources/chapters.py`, `tools/markers_eval/` | No other phase-4 task touches either — 15 is the only chapter-source and harness change, so it runs beside 3 and 9 with no shared file. |
| 9, 10 | `web/templates/servers.html` | 9 adds nothing to the template (checks render themselves); 10 adds the agent block. No overlap. |
| 4, 11, 14, 15 | `docs/design/intro-credits/spec.md` | 4 writes §5.5 rule 1 / §6.2 step 6; 15 writes §5.1's chapter-title rule; 11 the rest (including §5.5 rule 8 and §4's AniSkip row); 14 §0/§12. Sequential by wave. |
| 12, 13 | `evidence/` | 12 trims, 13 adds the lab results. **13 runs after 12** so nothing is written into a folder that is about to move. |
| any, `.gitignore` | `.gitignore` | Only Task 1 edits it. |

## File map (phase 4)

```
media_preview_generator/
  markers/
    sources/chapters.py             # T15 anime `Ending` = credits, and the lone generic `Intro`
    settings.py                     # T10 per-server agent block
    decide.py                       # T4  comments only (rule 8's grouping is unchanged, now permanent)
    pipeline.py                     # T3  publish-now path;  T4 kept types
    store.py                        # T3  unlock + the lock's own timestamp
    inspect.py                      # T3  the payload the editor needs
    publishers/plex_db.py           # T4  locked vs kept;  T10 the write behind a transport
    publishers/plex_remote.py       # T10 the agent transport
    publishers/emby.py              # T4  locked vs kept (ReplaceOwn)
  servers/{plex,jellyfin,emby}.py   # T9  marker sections in previews_readiness()
  web/routes/api_markers.py         # T3  save/lock/unlock + publish;  T10 agent status
  web/static/js/markers_inspector.js# T5  the editor
  web/static/js/markers_season.js   # T6  Edit per row
  web/static/js/markers_server_tab.js # T10 the agent block
  web/templates/settings.html       # T5  respect_locks tooltip
  web/templates/servers.html        # T10 the agent block
plex-marker-agent/                  # T10 the helper container (image, README, contract)
tools/markers_eval/                 # T15 the before/after chapter run (no new subcommand unless it needs one)
docs/design/intro-credits/evidence/
  eval/aniskip-facts.md             # T1  the measured service facts (done, c53d8c6)
  online/phase4/                    # T1  the scripts behind those numbers
  design/phase4.html                # T2  the mockup pack
  eval/phase4-chapters.md           # T15 the chapter rule's before/after numbers
  lab/phase4_matrix.py, lab/phase4-results.md   # T13
```

## Lab rows (storage lab only: Plex, Emby 4.10 + 4.9, Jellyfin 10.11 + 12.0)

New `evidence/lab/phase4_matrix.py` (same shape as `phase3_matrix.py`: `configure` / `run` / `rows`, one JSON per row
under `results/`, tokens scrubbed):

| Row | What it proves | Servers |
|---|---|---|
| 1 | Adjust + Save in the Inspector: every enabled owner shows the new times before the request returns; `markers.db` has `locked=1` | Plex, JF 10.11, JF 12.0, Emby 4.10 |
| 2 | Lock alone (no time change) publishes nothing new and still survives a re-detect | all |
| 3 | A locked marker survives a forced re-detect **and** a Check servers run | all |
| 4 | Unlock: the detected answer (or Needs review) comes back on the next run, on every server | all |
| 5 | Q1's answer on Plex: locked type × `keep_plex` × Plex's own markers present, after a forced Plex detection | Plex |
| 6 | Q1's answer on Emby: locked type × `keep_emby` × Emby's own rows, 4.10 and 4.9 (`ReplaceOwn` per Q1) | Emby 4.10, 4.9 |
| 7 | Save with one server stopped: saved and locked, that server reported failed in the response, the next Check servers publishes it | JF 10.11 stopped |
| 8 | The two D8 shapes, told apart: a recap on Emby and on Plex is **refused** in the UI (`can_show`), while an edited credits **end** on Emby is **accepted and published start-only** with "Emby skips to the end of the file" shown — never silently dropped | Emby 4.10, Plex |
| 9 | Task 15's chapter rule end to end on a real anime episode whose credits chapter is `Ending`: credits decided from chapters and published, and an episode with only a lone generic `Intro` behaves as Task 15 shipped | Plex + JF 10.11 |
| 10 | Setup Health, Plex: Pass missing, marker tag row absent, database not on this machine, Plex's own detection on | Plex (+ a no-Pass Plex, see below) |
| 11 | Setup Health, plugins: missing, then an older build installed → outdated, then current → All good | JF 10.11, JF 12.0, Emby 4.10, 4.9 |
| 12 | The Plex marker agent: the app container **without** the Plex config volume + the agent container **with** it; markers published through it; a wrong token refused; a version mismatch refused; the agent stopped → Plex read-only with a clear message | Plex |
| 13 | Phase 1–3 matrices re-run on the phase-4 image (regression) | all |

**Row 10 needs a Plex without Plex Pass, and the lab Plex is claimed** — rows 1, 3, 5 and 12 all need it claimed
(an unclaimed Plex serves no markers at all, spec §3.1). Two ways, decided in Task 13 Step 1 and recorded in the
results file: a **second, unclaimed throwaway Plex on storage** on its own port (the lab already runs five servers;
this one needs no library beyond one synthetic file), or a **stubbed `capability()`** for that row alone. The
throwaway server is the honest proof and is preferred; the stub is the fallback if the claim state can't be kept
apart. Unclaiming the lab Plex is not an option — four other rows depend on it.

Rows added to the earlier matrices (the publisher behaviour they extend lives there):

| Matrix | Row | What it proves |
|---|---|---|
| `phase1_matrix.py` | **20** | A locked marker through the Plex publisher end to end — extends row 16 (`keep_plex`) with Q1's answer and a forced detection afterwards |
| `phase1_matrix.py` | **21** | A Plex database the app can't reach locally (row 11's network-filesystem case) now publishes through the agent instead of going read-only |
| `phase2_matrix.py` | **25** | A locked marker through the Emby publisher, 4.10 and 4.9, with and without `ReplaceOwn` per Q1 — extends the `keep_emby` rows |
| `phase2_matrix.py` | **26** | Season view → Edit a row → save → the Season view and the per-server dots update — extends row 16 (Season view) |
| `phase3_matrix.py` | **17** | A locked credits marker whose answer came from credit text: a forced run still detects, still doesn't replace, and the Inspector shows both |

---

## Task 1: AniSkip and anime-id measurements

`[lane-parallel]` — spec §4 (the AniSkip row and how the app paces from headers), §5.2, §5.5 rule 8, §13 item 1.
**No product code in this task.**

**DONE 2026-09-20** (`c53d8c6`, branch `aniskip-facts`): `docs/design/intro-credits/evidence/eval/aniskip-facts.md`.
The ruling it produced — **AniSkip does not ship**, and the two anime chapter findings that became Task 15 — is in
"Measured and closed" above and in the parked items. The steps below are kept as the record of what was asked.

**Files:**
- Create: `docs/design/intro-credits/evidence/eval/aniskip-facts.md`,
  `docs/design/intro-credits/evidence/online/phase4/` (scripts; the truth set and per-file JSON stay git-ignored)
- Modify: `.gitignore` (the phase-4 local folder)

**Steps**
- [x] **Step 1: The service.** Call AniSkip for a handful of known anime episodes and record, with the date and the
  exact command: base URL, version, path, required and optional parameters, whether a key is needed, the segment
  types, the response envelope and units, and every rate-limit header it returns. Record the 429 behaviour. Record its
  terms of use and any attribution requirement. **Write down what could not be established**, rather than guessing.
- [x] **Step 2: Does it check the file's cut?** Ask with a deliberately wrong `episodeLength` and record what changes.
  This decides whether AniSkip could ever qualify under §5.5 rule 6 (a lone decider at Medium) or never.
- [x] **Step 3: Ids.** Count, over the owner's anime folders: episodes with a MAL/AniDB/AniList token in the path;
  episodes whose Plex, Jellyfin and Emby items report an anime id; and the overlap. Report coverage as a percentage of
  anime episodes, beside the phase-1 coverage table's style.
- [x] **Step 4: Numbering.** For the seasons that do resolve, check whether AniSkip's episode numbering matches the
  file's season/episode. Record every mismatch shape found (absolute vs seasonal, split cours, specials).
- [x] **Step 5: Truth set.** Assemble anime episodes with trustworthy truth (studio chapters, or frame-checked) —
  aim for the scale of the existing online set (43 cases) or better. List it locally; commit only counts.
- [x] **Step 6: Overlap with the sources we have.** For episodes both cover, compare AniSkip's times with
  TheIntroDB / IntroDB / SkipDB the way §5.5 rule 8's ≤ 44 ms check was done. This is the evidence for whether AniSkip
  is an independent source or a copy.
- [x] **Step 7: Write it up** in `aniskip-facts.md`, marking each fact "measured" with its command, or "not
  established".

**Proves:** the file itself; no tests.
**Done when:** every one of the eight facts is either measured with its command and date, or explicitly recorded as
not established, and the owner has the coverage numbers in front of them. **Met.**

---

## Task 2: Mockup pack and UI copy — **owner checkpoint 6**

`[sequential]` — spec §0 ("Visible UI changes: show a mockup and get the look + wording confirmed before building"),
§7 items 1, 3, 4, 6; memory `feedback_checkpoint_ux_before_building`, `feedback_info_icons_everywhere`,
`feedback_setup_health_ux_pattern`. Blocks Tasks 5, 6, 9 and 10.

**Files:**
- Created (2026-09-20): `docs/design/intro-credits/evidence/design/phase4/` — `index.html` (the pack, built from the
  app's own `style.css` + `markers_inspector.css` by relative path, Bootstrap vendored so it opens with no network),
  `ui-copy.md` (every new string, in this section's order, for Tasks 5/6/9/10 to lift verbatim), `README.md`,
  `shot.py`, `shots/*.png` (each surface dark and light, plus the editor at 390 px). A folder rather than the single
  `phase4.html` this line first named, because the copy file and the shots belong beside the page; the shots are
  committed under `docs/` (the repo's ignore rules keep an explicit exception for `docs/**/*.png`) instead of the
  excluded `.superpowers/` path, so the owner still has them once the lane worktree is gone.

**Steps**
- [x] **Step 1:** Draw the eight surfaces listed under "UI copy and mockups" above. Use real values from the lab or
  the owner's library, anonymised in anything committed. — done. The pack uses invented data throughout (a show
  called *Northern Lights*, three servers) rather than anonymising a real title. **One deviation from D8 is drawn,
  not assumed:** D8 and Step 4 of Task 5 word recap/preview on Plex and Emby as a flat refusal; read strictly that
  means a recap can never be adjusted while a Plex or Emby server has the file, even with a Jellyfin that would show
  it. The pack draws editable-with-a-note for that case and keeps the refusal for "no server can show it", and puts
  both to the owner. Whichever they pick, D8, Task 5 Step 4 and lab row 8 say the same thing afterwards.
- [x] **Step 2:** Write every new string, including the ⓘ tooltips and the Setup Health `current` / `recommended`
  values, in plain English, present tense, no internal setting names in the first sentence. — `ui-copy.md`.
- [x] **Step 3:** For Setup Health, show the rows inside the existing Must fix / Recommended / All good buckets with
  the current → recommended pair, and the shipped `Change in <vendor> UI` badge where there is no button (there is no
  "Manual" chip any more — `servers.js:2302`) — the pattern is fixed, the wording is not. — done. **The shipped badge
  does not fit one row**: "Plex's library database isn't on this machine" is fixed by moving the app or running the
  helper, not in Plex's UI. Carried as an open wording question in `ui-copy.md`.
- [ ] **Step 4:** Send the pack to the owner as one checkpoint, naming the Q1 wording (option (a)'s override
  sentence, or whichever option they pick) as the part most worth a hard look.
- [ ] **Step 5:** Record the owner's answer, including any wording change, in the pack and in spec §14.

**Proves:** the owner's reply.
**Done when:** the owner has confirmed the look and the wording of every surface phase 4 adds, and no task that builds
one has started before that reply.

---

## Task 3: Marker write API + publish-now path

`[lane-parallel]` `[high-risk]` — spec §6.2 step 8, §6.3 (`write()` and its `kept_types` / `previous` arguments),
§5.5 rule 1, §7 item 3; rulings P-R1, P-R2, P-R3, P-R4. High-risk because an HTTP request writes a Plex database and
two plugins, and because a half-saved edit is worse than a refused one.

**Files:**
- Create: `tests/markers/test_api_markers_edit.py`, `tests/markers/test_publish_now.py`
- Modify: `media_preview_generator/web/routes/api_markers.py` (the new routes beside `marker_item_redetect` ~321),
  `media_preview_generator/markers/pipeline.py` (a publish-only entry point reusing `_publish_to` ~1240),
  `media_preview_generator/markers/store.py` (unlock; the lock's own timestamp; `DecisionRow.decided_by`;
  `SCHEMA_VERSION` and `_MIGRATIONS`),
  `media_preview_generator/markers/inspect.py` (whatever the editor needs that the payload lacks)

**Steps**
- [ ] **Step 1: Failing API tests first.** The request shape (path or `server_id`+`item_id`, per type: start, end or
  "runs to the end of the file", lock), the refusals (unknown path, a path outside the library roots via
  `_library_file()`, end ≤ start, outside the file, a type no enabled owner can show), and the response shape (the
  stored markers + one row per owner: written / unchanged / failed / not enabled / can't show this type).
- [ ] **Step 2: Migrate `markers.db` before adding a column.** This task adds two columns to an existing database
  (the lock's own timestamp on `markers`, and `decided_by` on `decisions` — the phase-2 parked item L100). `_SCHEMA`
  is `CREATE TABLE IF NOT EXISTS` only, so a column added there **never reaches an existing install**:
  `SCHEMA_VERSION` is 1 (`store.py:27`) and `_MIGRATIONS` is empty (`:241`), and `_open_schema` (`:366–400`) runs
  `_MIGRATIONS[current]` in order inside one transaction before the idempotent `_SCHEMA` statements. So: bump
  `SCHEMA_VERSION` to 2 **and** append the two `ALTER TABLE` statements as `_MIGRATIONS[1]`. If the columns turn out
  not to be needed, drop both and record why in parked items — do not add a column without the migration.
- [ ] **Step 3: Save and lock.** `MarkerStore.lock_marker()` already exists; add unlock and make sure a save is
  atomic and lands **before** any server is contacted (P-R1). A save that publishes nothing still returns 200 with the
  stored marker. While in this file, write the reason `respect_locks` stays out of `detection_fingerprint()`
  (`settings.py:106`) into that docstring, so the next reader doesn't "fix" it.
- [ ] **Step 4: Publish now.** Reuse the pipeline's existing per-server publish rather than a second implementation —
  the same `previous`, `duration_ms`, `canonical_path`, `own_previous` and `kept_types` arguments a job passes, so the
  publish state and the row messages stay identical to a job's. Per-server bounded deadline; failures recorded in
  `publish_state` exactly as a job's would be, so Check servers picks them up.
- [ ] **Step 5: The matrix.** Server type (Plex / Jellyfin / Emby) × capability (ready / needs plugin / needs
  confirmation / unreachable) × type (intro / recap / credits / preview) × kept-types state. Assert the request body,
  the SQL parameters and the kwargs the code controls — never a call count. Include the D8 cells: recap and preview
  refused for Plex and Emby; an edited credits **end** on Emby accepted, published start-only, with
  `CREDITS_BEFORE_END_NOTE` in the row.
- [ ] **Step 6: Nothing outlives the request.** A test proves no thread, no job and no worker slot is created, and
  that an unreachable server can't hold the request past the deadline.
- [ ] **Step 7:** Unlock: the lock row goes, the next run re-decides, and a type that now has no answer becomes
  Needs review rather than staying on the servers silently.
- [ ] **Step 8:** Full markers suite, `ruff`, Architecture Review, commit, push.

**Proves:** `tests/markers/test_api_markers_edit.py`, `tests/markers/test_publish_now.py`, a migration test in
`tests/markers/test_store.py` that opens a **schema-1** `markers.db` written without the new columns, upgrades it and
reads a locked marker and a decision back (it must fail before the `_MIGRATIONS[1]` entry exists), and the existing
`tests/markers/test_pipeline.py` / `test_plex_db_publisher.py` / `test_emby_publisher.py` passing unchanged.
**Done when:** a save stores a locked marker, publishes it to every enabled owner inside one bounded request, returns
a per-server result the UI can render, and a following detection run leaves it alone — with every cell of the
server × capability × type matrix covered, and an existing schema-1 `markers.db` upgrading cleanly (or no column
added at all).

---

## Task 4: Locked markers vs "Keep Plex's" / "Keep Emby's" (**owner Q1**)

`[sequential]` `[high-risk]` — spec §5.5 rule 1, §6.2 step 6, §6.3 (Plex kept types, Emby `ReplaceOwn`), D1.
**Does not start before Q1 is answered.**

**Files:**
- Create: `tests/markers/test_locks_vs_kept.py`
- Modify: `media_preview_generator/markers/publishers/plex_db.py`,
  `media_preview_generator/markers/publishers/emby.py`, `media_preview_generator/markers/pipeline.py`,
  `media_preview_generator/markers/decide.py` (comments only),
  `docs/design/intro-credits/spec.md` (§5.5 rule 1, §6.2 step 6, §14 dated line)

**Steps**
- [ ] **Step 1:** Write the owner's answer into spec §5.5 rule 1 and §6.2 step 6 **first**, so only one of the two
  sentences survives, with a dated §14 line naming the option and its reason.
- [ ] **Step 2: Failing cells.** Locked × `restore` / `keep_plex` × Plex has its own rows of that type / has none ×
  the item record says ours / says nothing. The same for Emby with `keep_emby` and `ReplaceOwn`. Jellyfin has no kept-
  type setting — one cell records that and why.
- [ ] **Step 3:** Implement the answer in the two publishers. If the answer is (a), the override must be visible: the
  row message says the server's setting was overridden for that type, and the Inspector's server card says it before
  the user saves.
- [ ] **Step 4:** Check servers (`reconcile.py`) re-asserts under the new rule — spec §6.2 step 6's "Locked markers
  re-assert, except that `keep_plex` / `keep_emby` …" clause is rewritten or deleted here.
- [ ] **Step 5:** Full suite, Architecture Review, commit, push.

**Proves:** `tests/markers/test_locks_vs_kept.py` (every cell), plus lab rows 5 and 6.
**Done when:** the spec holds exactly one rule for this, the publishers implement it, every cell of the matrix has a
test, and the UI says what it is doing on the affected servers.

---

## Task 5: Inspector Adjust / Lock editor

`[lane-parallel]` **[visible UI — Task 2 must be approved first]** — spec §7 item 3, §6.2 step 8; P-R2, P-R3; D5.

**Files:**
- Modify: `media_preview_generator/web/static/js/markers_inspector.js` (`bar()` ~149, `lane()` ~164,
  `decisionLane()` ~213, `renderServers()` ~364, `render()` ~412, `wireRedetect()` ~539 — the module has **no**
  pointer or keyboard handlers today), `media_preview_generator/web/static/css/pages/markers_inspector.css`,
  `media_preview_generator/web/templates/bif_viewer.html` (the header buttons beside `#markersRedetectBtn` ~175),
  `media_preview_generator/web/templates/settings.html` (the `respect_locks` tooltip ~518 — D5),
  `tests/e2e/test_intro_credits_inspector.py`

**Steps**
- [ ] **Step 1:** Editor state in the existing render path: open, drag a handle, nudge, save, cancel. Reuse
  `clock()`, `laneRange()`, `TOLERANCE_MS`, `END_OF_FILE_MS` and the two zoom windows rather than a new timeline.
- [ ] **Step 2: Keyboard first.** Every drag has a keyboard equivalent (arrow = small step, shift+arrow = larger,
  a stated step size), the handles are focusable, and the current time is announced in text, not only by position.
  The e2e test does the whole edit **without a mouse** in one case.
- [ ] **Step 3: Clamp, don't judge** (P-R2): inside the file, end after start, "runs to the end of the file" as an
  explicit choice. A marker outside the usual bounds warns and saves.
- [ ] **Step 4: Save = lock** (P-R3). Show the per-server result the API returns — written / failed / not enabled /
  can't show this type — and the Q1 override sentence where it applies. **D8 has two different shapes:** recap and
  preview are refused for Plex and Emby (`can_show`, type-level), while an edited credits **end** on Emby is accepted
  and published start-only — that one needs its own per-field note, "Emby skips to the end of the file", the same
  sentence `markers_server_tab.js:216` already shows.
- [ ] **Step 5: Unlock** with a confirmation that says what comes back.
- [ ] **Step 6:** The `respect_locks` tooltip and any other copy that says editing "comes in a later update".
- [ ] **Step 7:** e2e: drag, nudge, save, reload and see the lock; a failing server; a movie (no opening window); a
  file with no decision. `-m e2e -n 8 --no-cov`.
- [ ] **Step 8:** Screenshot the built editor to the owner beside the approved mockup before the commit.

**Proves:** `tests/e2e/test_intro_credits_inspector.py`.
**Done when:** a user can adjust and lock a marker with the mouse or the keyboard alone, sees per-server results
without leaving the tab, and the built screen matches the approved mockup (any deviation went to the owner first).

---

## Task 6: Season view — Edit per row

`[sequential]` **[visible UI — from Task 2's pack]** — spec §7 item 4 ("Review opens that episode; editing is phase
4").

**Files:**
- Modify: `media_preview_generator/web/static/js/markers_season.js` (`actionCell()` ~119, `chipsCell()` ~92 which
  already renders the 🔒 chip), `tests/e2e/test_intro_credits_season.py`

**Steps**
- [ ] **Step 1:** An Edit action per row that opens that episode in the editor (the view switch already exists in
  `openEpisode()` ~257), beside the existing Review button.
- [ ] **Step 2:** After a save, the Season row's times, chips and per-server dots reflect it without a reload.
- [ ] **Step 3:** e2e: edit from the Season view, come back, see 🔒 and the updated dots.

**Proves:** `tests/e2e/test_intro_credits_season.py`.
**Done when:** every episode in the Season view can be opened straight into the editor, and a save is visible in the
season table without a page reload.

---

## Task 15: Anime chapter titles — `Ending`, and the lone generic `Intro`

`[lane-parallel]` — spec §5.1 (chapter titles, the cold-open rule), §5.5 rules 2 and 3, §10.2; Task 1's measurements
(`evidence/eval/aniskip-facts.md` §5). Wave 1, beside Tasks 3 and 9: it touches the chapter source and the harness,
which no other phase-4 task goes near. **Measure first, ship only what wins** — the same discipline rule J got.

Two findings from Task 1, both on the owner's real library:
- **`Ending` is the credits on anime, and the classifier ignores it.** `chapters.py:23` deliberately excludes
  `End`/`Ending` because they are common final-scene names in **movies**. 238 anime files carry one; on the 149 where
  AniSkip also has an `ed`, its start is within 5 s of that chapter in 127 (85 %), within 1 s in 78, and more than
  15 s away in 9 — median |delta| 1.0 s. That is up to 238 files of credits coverage the app throws away today.
- **581 of 1,080 anime intro chapters are a lone generic `Intro`** with no specific opening chapter in the file, so
  §5.1's cold-open rule can't fire and the app takes the chapter at face value. On anime that chapter is often the
  cold open, not the theme — so this one may *cost* accuracy rather than gain it. Chapter conventions are uniform
  within a show, so it is checkable per show (112 shows), not per file.

**Files:**
- Create: `tests/markers/test_chapters_anime.py`,
  `docs/design/intro-credits/evidence/eval/phase4-chapters.md`
- Modify: `media_preview_generator/markers/sources/chapters.py`, `tests/markers/test_chapters.py`,
  `tools/markers_eval/` (whatever the before/after chapter run needs — no new subcommand unless it earns one)

**Steps**
- [ ] **Step 1: Baseline.** Score today's classifier against the existing chapter truth (the harness already scores
  chapters) on three populations: the anime set, the non-anime TV set and the movie sets. Record useful / wrong /
  missed per population. Nothing is changed yet.
- [ ] **Step 2: The `Ending` rule, scoped.** The rule is global today and was written for movies, so an anime-only
  scope is the premise: decide what "anime" means here from what the code already has (library kind, the season
  group, a per-show chapter convention — **not** a new metadata source), and say plainly in the write-up which files
  the scope catches and which it misses.
- [ ] **Step 3: Measure it.** Re-score all three populations with the rule on. State the gain (anime credits) **and**
  the cost (any movie or non-anime file whose final scene is now read as credits). A single new wrong answer on
  movies is worth more than ten anime gains — precision over coverage.
- [ ] **Step 4: The lone generic `Intro`.** Measure what the app decides for those 581 files today, per show, against
  the chapter truth. If they are mostly cold opens, the honest change is to **not** decide an intro from a lone
  generic `Intro` on anime (a coverage loss that removes wrong answers); if they are mostly themes, change nothing.
  Either way the write-up carries the numbers.
- [ ] **Step 5: Bump `CHAPTER_RULES_VERSION`** (`chapters.py:21`, currently 1) for whatever ships. Its own comment
  says it exists for exactly this: a change to the candidates a file's chapters give means already-probed files must
  be read again. Without the bump the new rule never reaches a file the app has already seen — the chapter-source
  twin of Task 3's migration trap. A test pins that a stored answer at the old version is re-probed.
- [ ] **Step 6: Ship only what wins.** Each change survives on its own measurement, or is dropped and recorded as
  measured-and-not-taken. No tuning to the truth set, no rule that wins on anime by losing on movies.
- [ ] **Step 7:** Unit cells in `tests/markers/test_chapters_anime.py` for every title shape the rule reads
  (`Ending`, `End`, `End Credits`, `Ending Song`, the non-anime cases the rule must still refuse) and for the scope
  boundary; the existing `tests/markers/test_chapters.py` passes unchanged, or every changed row is explained.
- [ ] **Step 8:** Write `evidence/eval/phase4-chapters.md`: the three populations, before and after, per change, plus
  what was dropped and why. Counts only, no library paths.
- [ ] **Step 9:** Spec §5.1 gains the shipped rule and its scope; a dated §14 line records the measurement. Full
  markers suite, `ruff`, Architecture Review, commit, push.

**Proves:** `tests/markers/test_chapters_anime.py` and the unchanged `tests/markers/test_chapters.py`, plus the
harness before/after run in `evidence/eval/phase4-chapters.md` — the numbers are the gate, not the tests.
**Lab:** phase-4 row 9 (a real anime episode whose credits chapter is `Ending` gets credits published to lab Plex and
Jellyfin; a lone-generic-`Intro` episode behaves as shipped). A row is warranted because this changes what the app
publishes from a file it already reads, on a population the lab can mount read-only.
**Done when:** each of the two findings is either shipped with a measurement that shows it wins on anime and costs
nothing on movies and non-anime TV, or dropped with the numbers that say so — and spec §5.1 matches the code.

---

## Task 9: Setup Health — Intro & Credits checks

`[lane-parallel]` **[visible UI — from Task 2's pack]** — spec §7 item 6, §6.3 (`Capability`), §3.1 (Pass, tag row,
DB location, forced detection), §13 items 4 and 5; P-R5, P-R6; memory `feedback_setup_health_ux_pattern`.

**Files:**
- Create: `tests/markers/test_readiness_markers.py`
- Modify: `media_preview_generator/servers/plex.py` (`previews_readiness()` 922–1463),
  `media_preview_generator/servers/jellyfin.py` (1617–2499), `media_preview_generator/servers/emby.py`
  (838–1231), `media_preview_generator/servers/base.py` (the envelope docstring 693–745),
  `tests/test_servers_plex.py`, `tests/test_servers_jellyfin.py`, `tests/test_servers_emby.py`,
  `tests/test_api_servers.py`, `tests/e2e/test_intro_credits_server_tab.py`

**Steps**
- [ ] **Step 1:** One new section per vendor (`markers`), built from the **existing** capability report and server
  status rather than new probes — the Edit tab already shows these facts, and two sources of truth would drift.
- [ ] **Step 2: The checks the roadmap names** — five §7 bullets, six checks, and **two of them are not new rows**.
  - Plex (all new): Plex Pass missing (critical), marker tag row absent (critical — and the app must never create
    it), database not on this machine (critical, with the agent from Task 10 as the recommended fix once it
    exists), Plex's own detection may overwrite ours (recommended).
  - Jellyfin: the `plugin` section and its `plugin_installed` check already exist
    (`servers/jellyfin.py:1856–1902`) **and the install controls are keyed on `section.id === 'plugin'`**
    (`servers.js:1929`, `:2669`). So "plugin missing" **escalates that check's severity** when markers are on; it
    does not add a second row. Only "plugin outdated" is new for Jellyfin (recommended, installed → required as the
    value pair).
  - Emby: `emby.py` has no plugin section at all, so **both** rows are new there, and they must use the same
    `section.id` (`plugin`) and first-check `current` convention (`"not installed"` / a version) that the JS already
    reads.
- [ ] **Step 3: Every row carries `current` and `recommended`**, an ⓘ, a `docs_anchor`, and an action only where one
  genuinely exists (install/update the plugin). A row with no action uses the shipped `Change in <vendor> UI` badge —
  there is no "Manual" chip any more (`servers.js:2302`).
- [ ] **Step 4: P-R6** — the section appears only when that server has Intro & Credits on, plus one row saying so when
  it is off, emitted as `severity: "recommended"`, `ok: true` so it lands in "All good". **Never `info`**:
  `_partitionChecks` drops `info` rows (`servers.js:2138`, `:2262`).
- [ ] **Step 5:** Unit cells per vendor × check × ok/not-ok, asserting the emitted dict (id, severity, current,
  recommended, actions), not a count — including a cell proving the off-server row is `recommended`+`ok`, and a cell
  proving Jellyfin still emits exactly one `plugin` section with the install controls' `current` convention intact.
  e2e: the rows land in the right bucket with the right badge.
- [ ] **Step 6:** Screenshot to the owner beside Task 2's mockup before the commit.

**Proves:** `tests/markers/test_readiness_markers.py`, `tests/test_servers_plex.py`,
`tests/test_servers_jellyfin.py`, `tests/test_servers_emby.py`, `tests/test_api_servers.py`,
`tests/e2e/test_intro_credits_server_tab.py`.
**Done when:** each of the six checks fires on a server that has the problem and stays quiet on one that doesn't,
each shows current → recommended, Jellyfin's existing plugin section and its install controls still work unchanged,
and no JS changed (if any did, say why).

---

## Task 10: Plex marker agent — the helper container

`[lane-parallel]` `[high-risk]` **[visible UI — the Edit tab block, from Task 2's pack]** — spec §3.1 ("The app must
run on the same host as Plex to write"), §6.3 (`NeedsLocalDb`), §14 2026-09-13 ("helper container on the Plex host
later"); Q3. High-risk: it writes a Plex database from another machine.

**Files:**
- Create: `plex-marker-agent/` (the service, its Dockerfile, its README with the contract),
  `media_preview_generator/markers/publishers/plex_remote.py`, `tests/markers/test_plex_remote.py`,
  `tests/test_plex_marker_agent.py`
- Modify: `markers/publishers/plex_db.py` (the write behind a transport; the local path unchanged),
  `markers/publishers/base.py` (a capability for "agent configured but unreachable / wrong version"),
  `markers/settings.py` (the per-server agent block), `web/templates/servers.html`,
  `web/static/js/markers_server_tab.js`, `web/routes/api_markers.py` (agent status in the server status payload)

**Steps**
- [ ] **Step 1:** Separate today's Plex write into "decide what to write" and "run it against the database", with the
  local path proven unchanged by the existing publisher tests (the phase-3 golden-args lesson: prove the old path is
  byte-identical before moving it).
- [ ] **Step 2:** The agent: one endpoint to read what an item shows, one to write a marker set, one ping carrying its
  version and the Plex schema it sees. Same rules as the in-process writer — never create a `tags` row, stop on an
  unknown schema, the `taggings` + `media_parts.extra_data` pair, the ±2 s serving shifts — because it is the same
  code, not a second implementation.
- [ ] **Step 3: Refusals.** No token or a wrong token → refused; a version the app doesn't know → refused with a
  message naming both versions; a database on a network filesystem **on the agent's side** → the same `NeedsLocalDb`
  answer, not a write.
- [ ] **Step 4: Secrets.** The token is masked in every API response, never logged, and round-trips unchanged when
  `****` is posted back — the same rule TheIntroDB's key has.
- [ ] **Step 5: UI.** The Edit tab block: address, connection state, agent version, and the sentence that replaces
  "the app must run on the same machine as Plex" when an agent is configured.
- [ ] **Step 6: Docs** live with the container (`plex-marker-agent/README.md`): what it is, what it may touch, how to
  run it beside Plex, and that it is the only supported way to write a Plex on another host.
- [ ] **Step 7:** Deep review before the lab row: this is the one part of phase 4 that can corrupt a user's Plex
  database from another machine.

**Proves:** `tests/markers/test_plex_remote.py`, `tests/test_plex_marker_agent.py`, the existing
`tests/markers/test_plex_db_publisher.py` unchanged, and lab row 12.
**Done when:** an app container with no access to the Plex config volume publishes markers to lab Plex through an
agent container that has it; a wrong token, a version mismatch and a stopped agent each fail with a message a user can
act on; and the local (same-host) path is provably unchanged.

---

## Milestone audit (gate, after Tasks 5, 6, 9, 10, 15)

Whole phase-4 diff, the eight production bug shapes plus this phase's own risks: an HTTP request that writes servers,
a rule change that reaches every publisher, a new source that can publish wrong times, a container that writes a
database over a network, and UI copy that contradicts the code. Fixes land in the owning task's files. This is a task
gate, not a suggestion.

---

## Task 11: Docs

`[sequential]` — `.claude/rules/docs.md`; D3, D4, D5, D6, D7.

**Files:** `README.md`, `docs/reference.md`, `docs/guides.md`, `docs/guides/previews-readiness.md`,
`docs/design/intro-credits/spec.md`, `docs/design/intro-credits/plan-roadmap.md`,
`docs/design/intro-credits/evidence/README.md`

**Steps**
- [ ] **Step 1:** Delete every "comes in a later update" (`reference.md:290`, `guides.md:624`, `:640`, `:681`) and
  write how adjusting, locking and unlocking actually work, including what happens on a server that keeps its own
  markers (Q1's answer) and on Emby's missing types.
- [ ] **Step 2:** `reference.md`: the two new endpoints and their responses; the per-server agent block. No source
  list change — phase 4 adds no source.
- [ ] **Step 3:** `guides.md`: a "Plex on another machine" section pointing at the agent, and — if Task 15 shipped
  anything — one line on what the app now reads from an anime `Ending` chapter.
- [ ] **Step 4:** `guides/previews-readiness.md`: the new marker checks, one entry each, with what to do about them.
- [ ] **Step 5:** Spec: §4's AniSkip row becomes "measured 2026-09-20, not taken" with a pointer to
  `evidence/eval/aniskip-facts.md`; **§5.5 rule 8 loses "until phase 4 measures what they copy"** — an AniSkip
  importer's copies stay in the IntroDB + TheIntroDB group **permanently**, with the 21 % ≤ 44 ms match (against 10 %
  between the two intro databases) as the recorded evidence (D4); §5.1 keeps whatever Task 15 shipped; §6.2 step 8's
  bound (D2), §7 items 3, 4 and 6, §9's Setup Health row (D6), §13 item 17's lock note (D7), and a dated §14 line per
  decision taken in this phase, the AniSkip ruling included.
- [ ] **Step 6:** Roadmap: tick the phase-4 bullets, record what was done and what was not.

**Proves:** a docs read-through against the built UI; the e2e copy tests that assert shipped strings.
**Done when:** no user-facing document claims editing is unavailable, every new setting, endpoint and check is
documented, and the spec matches the code.

---

## Task 12: Evidence trim and a stable home for the lab (**owner Q5**)

`[sequential]` — spec §10.3 ("Before the evidence folder is trimmed, move `lab/up.sh` + `lab/env` to a stable dev-tools
location"), §11 ("Trim or remove before release"); memory `lab-servers-on-storage` ("keep the lab containers").

**Steps**
- [ ] **Step 1:** List what `evidence/` holds today, by folder, with sizes and what each is for. Send it with a
  keep / move / delete recommendation. **The owner decides.**
- [ ] **Step 2:** Move the lab bring-up scripts and their env to the agreed dev-tools location, with the tokens file
  still git-ignored, and update every reference to the old path (spec §10.3, the matrices, the READMEs).
- [ ] **Step 3:** Delete only what the owner approved, in its own commit, with the list in the commit message.
- [ ] **Step 4:** Check nothing still referenced is gone: the matrices, the harness, the spec and the plans all still
  resolve their links.

**Proves:** a link check over everything that survives, run before and after the delete commit and pasted into the
task report — every relative link in `docs/**/*.md` resolves on disk, e.g.
`grep -rhoE '\]\(([^)#]+\.(md|py|sh|json|html))' docs | …` piped into a `test -e` loop from the repo root — plus
`nice -n 19 /home/data/.venv/bin/python -m pytest --no-cov tests/markers_eval tests/markers` (the harness and store
tests read evidence paths) and `evidence/lab/up.sh` bringing the lab up from its new home.
**Done when:** the lab still comes up from its new home, the repo carries only what the owner agreed a release should
carry, the link check is clean, and the delete list is in the commit message.

---

## Task 13: Phase-4 lab matrix

`[sequential]` `[high-risk]` — spec §10.3, §3; the 13 rows and the five added rows in "Lab rows" above. Storage lab
only; **nothing runs on `plex`**.

**Files:**
- Create: `evidence/lab/phase4_matrix.py`, `evidence/lab/phase4-results.md`
- Modify: `evidence/lab/phase1_matrix.py` (rows 20, 21), `phase2_matrix.py` (rows 25, 26), `phase3_matrix.py`
  (row 17), `evidence/lab/up.sh`, `evidence/lab/app.sh` (whatever the agent container needs)

**Steps**
- [ ] **Step 1:** Build the rows on the phase-4 image, per-row JSON under `results/` (git-ignored), tokens scrubbed.
  Decide and record how row 10 gets a Plex without Pass (the second throwaway Plex, or the stub) before writing it.
- [ ] **Step 2:** Run every phase-4 row, then re-run the phase 1, 2 and 3 matrices on the same image.
- [ ] **Step 3:** Write `phase4-results.md` in the phase-3 style: a row table, then a paragraph per row that needed
  one, with counts and no library paths.
- [ ] **Step 4:** Any failure is fixed in the owning task's files and the row re-run — never explained away.

**Proves:** `evidence/lab/phase4_matrix.py run` (every phase-4 row), `phase1_matrix.py`, `phase2_matrix.py` and
`phase3_matrix.py` re-run on the same image, with the per-row JSON under `results/` and the counts in
`phase4-results.md`; plus the full unit suite (`nice -n 19 /home/data/.venv/bin/python -m pytest`) on the commit the
image was built from.
**Done when:** every phase-4 row passes on the image that will ship, the phase 1–3 matrices still pass on it, and the
results file records the image digest.

---

## Task 14: PR, image, close-out

`[sequential]`

**Steps**
- [ ] **Step 1:** Re-run the phase-4 rows on the **published** `pr-241` image (not a local build), as phases 2 and 3
  did, and record its digest.
- [ ] **Step 2:** Spec §0 status, §12 phase 4's "done when", §14's dated lines; the roadmap's phase-4 block;
  `.superpowers/sdd/plan-phase4/progress.md`.
- [ ] **Step 3:** PR body via `gh api` REST (`gh pr edit` fails on this repo).
- [ ] **Step 4:** Tell the owner what is left: the Emby catalog submission (checkpoint 4) and the first
  `emby-plugin-v*` release, which spec §13 item 18 says must be cut at or after `fb32a88`. **Do not post the catalog
  submission; the owner does that at the end.**
- [ ] **Step 5:** Nothing merges into `dev`; the PR stays draft until the owner says the feature is fully tested.
  **No version bump, no release notes, no tag** — those need the owner's explicit word "release".

**Proves:** the selected phase-4 rows re-run on the pulled `pr-241` image with its digest recorded (as
`phase3-results.md` "PR image check" does), the full unit and e2e suites green on the head commit, and CI green on
the pushed branch.
**Done when:** the published image has re-run the matrix, the spec and roadmap say phase 4 is done and what it left
open, and the owner has the close-out.

---

## Owner checkpoints (ask, don't assume)

Continuing the roadmap's list; 1–3 are done, 4 and 5 are still open.

4. *(open, from phase 2)* Emby catalog forum thread / developer id — plus the first `emby-plugin-v*` release, which
   §13 item 18 wants cut at or after `fb32a88`.
5. *(open)* Enabling the prod Plex DB write for a real server — only when the owner says so.
6. **Phase-4 mockup pack** (Task 2) — the editor, Season Edit, Setup Health rows, the agent block. Blocks Tasks 5,
   6, 9 and 10.
7. **Q1 — a locked marker vs "Keep Plex's" / "Keep Emby's."** Blocks Task 4, and Task 5's copy.
8. **Q2 — AniSkip's default and its gate. ANSWERED 2026-09-20 by Task 1's measurement: it does not ship.** Blocks
   nothing.
9. **Q2b — where the MAL id may come from. ANSWERED 2026-09-20: nowhere** (0 of 4,651, both id maps unlicensed).
   Blocks nothing.
10. **Q3 — the Plex marker agent's shape, auth and distribution.** Blocks Task 10.
11. **Q4 — the save-and-publish bound** (only if P-R1's recommendation is wrong).
12. **Q5 — the evidence trim**: what stays, and where the lab scripts move to. Blocks Task 12.
13. **Release timing.** Nothing is tagged, versioned or released without the explicit word "release".

## Parked items carried in

- **AniSkip as a source of ours — measured and not taken (Task 1, 2026-09-20,
  `docs/design/intro-credits/evidence/eval/aniskip-facts.md`, `c53d8c6`).** The API was never the problem (anonymous,
  one GET, seconds not ms, 404 = no data, and it does check the file's cut to ±20 s). Four measured reasons:
  1. **No MAL id exists anywhere in this library** — 0 of 4,651 anime episodes resolve without a mapping file, and
     neither candidate map (Anime-Lists/anime-lists, Fribb/anime-lists) carries a LICENSE.
  2. **It fails the owner's own Q4 gate** — 5.1 % of anime intros wrong by more than 15 s against the 2 % Medium cap;
     tightening the episode-length band to ≤ 2 s doesn't help (5.2 %).
  3. **It isn't a coverage win** — on 120 resolved anime episodes it is the only source with an answer for 2 intros
     and 0 credits; IntroDB covers anime better.
  4. **It probably isn't independent** — it matches IntroDB to ≤ 44 ms on 21 % of intros, where the two intro
     databases already counted as one source match on 10 %, and not through chapters.
  **What would change the answer: a licensed id map *and* a gate pass** — both, not either. Until then spec §5.5
  rule 8's grouping of an AniSkip importer's copies with IntroDB + TheIntroDB is permanent (Task 11), and the
  retired plan numbers 7 (the source) and 8 (its harness gate) are not reused.
- **Phase-2 plan L100** — `DecisionRow` drops a proposed marker's `decided_by`, "not in phase 2 because only the
  phase-4 editor needs it". Task 3 needs it: the editor shows what a proposal was based on before the user overrides
  it. Landing it there is the cheapest place — **with the `decisions` migration in Task 3 Step 2**, or not at all
  (an added column that skips `_MIGRATIONS` never reaches an existing install).
- **Phase-2 plan L165 / phase-3 parked item** — anime "Ending" chapters aren't counted as credits, and files with two
  "End Credits" chapters take the last one. **Now scheduled: Task 15**, with Task 1's measurement behind it (238
  files, AniSkip's `ed` within 5 s of the chapter on 85 % of the 149 it could check) and the same measure-then-gate
  discipline the credit-text rules got. The second half of L165 (two "End Credits" chapters, the rules take the last)
  stays parked — no measurement asked for it.
- **`pipeline.py:672`** — "what `respect_locks=False` should change is for the phase 4 editor". Task 3 decides it and
  writes the answer into the tooltip and `docs/reference.md`; `respect_locks` stays outside
  `detection_fingerprint()` (`settings.py:106`), and Task 3 Step 3 writes that reason into the docstring.
- **Spec §13 item 17** — Plex markers of ours read as Plex's own after `markers.db` is lost. A lock makes the loss
  worse (the user's own edit goes, not just its provenance). Task 11 records that; the pending owner decision on an
  ownership key in `extra_data` is unchanged.
