# Plan: no more "Needs review" (2026-10-02)

## Start here

Owner-approved on 2026-10-02 (items 1–8 below, item 4 as "re-check only the affected files, automatically").
Base: `dev` at `7a120c3`. Nothing is committed, pushed or deployed without the owner's OK.

Why: the owner's bar is "decide automatically like Plex, never ask the user". Measured on sflix 2026-10-02: the
file's own GPU/CPU read had already run on every Needs-review file; review only happened because that read and an
online answer didn't line up, or only an online answer existed. A file in review gets nothing written, same as
"No markers found", so review was a label, not a safety net. Discussion #332 user: 1,138 files in Needs review.

## What changes

1. **Remove Needs review.** `DecisionStatus.NEEDS_REVIEW` goes. Every type ends DECIDED (write) or NO_EVIDENCE
   (write nothing). Anything DECIDED today stays decided exactly the same way.
2. **The file's own read wins when online isn't sure.** Source order is unchanged (online first; the local reads
   run only while a type is undecided). "File read" = `SEASON_AUDIO`, `SEASON_AUDIO_PREVIOUS`, `CREDITS_TEXT`
   (pipeline.py ~1362). Chapters are not a file read for this rule.
3. **Plex items with several versions stop waiting.** When versions don't agree within 2 s, publish the decision
   of the lowest `media_item_id` version that has a marker of that type (deterministic, no new data). No waiting on
   an undecided sibling either. Bump `WAITING_VERSIONS_VERSION` so stuck items are re-run once.
4. **Existing libraries: re-check only the affected files, automatically.** Files with a legacy `needs_review`
   decision (raw status string) plus Plex items stuck waiting on versions. Reuse the existing decide-again job
   (`triggers.submit_decide_again`, `upgrade.py` v16–18 pattern) via a new upgrade step. Do NOT bump
   `DECIDE_RULES_VERSION` (that re-runs the whole library). Legacy `needs_review` rows that never get re-decided
   (e.g. files gone from disk) load as NO_EVIDENCE.
5. **UI and docs.** Remove the Needs review chip, the job file-list filter option, Inspector/search badges and
   docs mentions. The Inspector stays for manual edits. Regenerate `docs/llms-full.txt`.
6. **A failed local read shows "Failed", not "No markers found".** Only after the worker's normal GPU→CPU
   fallback also failed. If another type was written, the file stays "Markers written" with the failure in its
   message. It is re-read on the next run (existing `_detector_due`); no new retry machinery.
7. **GPU keeps failing → bell notification.** Per GPU, in memory, across jobs: 5 files in a row that needed the
   CPU fallback (whole-file rerun or a step-level fallback, previews and Intro & Credits) raise a notification in
   the existing bell (`web/notifications.py`). Cleared by the next file that GPU finishes without fallback.
   Title "GPU keeps failing: files are running on the CPU". Body "<GPU name> couldn't process the last 5 files, so
   they ran on the CPU (slower). Last reason: <reason>. Check the GPU driver in Settings → GPU." Job summary gains
   a line "N files ran on the CPU because the GPU failed" when N > 0.
8. **Fix the wrong log wording.** `processing/generator.py` ~2076, ~2112, ~2127, ~2133 say "handing off to a CPU
   worker"; the same GPU worker retries on the CPU in place (`jobs/worker.py:629-653`, test
   `tests/test_worker.py:1071` with `cpu_workers=0`).

## Item 2: every former review cell (decide.py at 7a120c3)

Note 2026-10-03 (measured rule, OWNER DECISIONS below): where a row says a **file read** wins, read **credit text**; season audio never wins a disagreement, so those intro cells are NO_EVIDENCE.

| Site | Today | New |
|---|---|---|
| :722 chapter vs contradicting cluster(s) | review | credit text agrees with the chapter → DECIDED `chapter_marker`; otherwise NO_EVIDENCE (the `_audio_over_chapter`/`_text_over_chapter` guards stay the only way a file read beats a chapter — Family Guy S14, :895) |
| :724 / :726 waiting for credit text / season audio | review (transient) | NO_EVIDENCE, same reason text; the pipeline keeps asking because status is not DECIDED |
| :737 long intro chapter, servers only | review | NO_EVIDENCE |
| :748 chapter + groups, composed other edge insane | review | DECIDED `chapter_marker` (keep the marker, skip the adjustment) |
| :962 cliques conflict | review | the clique holding credit text → DECIDED its composed marker; none or both → NO_EVIDENCE |
| :967 / :1033 composed marker insane | review | credit text in the group with a sane own marker → DECIDED that; else NO_EVIDENCE |
| :1028 source disagrees with itself | review | NO_EVIDENCE |
| :1043 "sources disagree" | review | credit text among the sources → DECIDED `_own_marker(proposal)`; else NO_EVIDENCE |
| :1043 `SEASON_AUDIO_WITH_SERVER_REASON` | review | DECIDED if the file read may decide alone (`_may_decide_alone`), else NO_EVIDENCE |
| :1043 `_lone_answer_reason` (online/server alone) | review | NO_EVIDENCE |
| :1226 agreeing sources contradict the result | review | result rests on credit text → keep DECIDED; else NO_EVIDENCE |
| :1275 server-shortened start insane | review | keep the unshortened DECIDED marker |
| :1384 `_demote` overlaps | review | NO_EVIDENCE for the demoted type |

Markers we already published must not be pulled just because a former review cell now says NO_EVIDENCE: keep the
existing `keep_published` / `_keep_published_before_rule_change` behaviour for those cells.

## Proof (done = all of these)

- Decide replay, before (7a120c3) vs after, no media reads: `evidence/decide-rules/measure_final.sh`
  (intro lab 118 / held-out 175 / chapter set, credits 80+205, online 43, prod replay). Bar per set: wrong ≤ before
  and wrong ≤ Plex; report useful/wrong/missed.
- Publisher tests (`tests/markers/test_publisher_contract.py`, `test_plex_db_publisher.py`) and lab rows that
  assert multi-version behaviour (phase1 row 8, phase2 row 11) updated and passing.
- Full `pytest`, `ruff check`, `ruff format --check`, docs `--check`, `tests/test_docs_site.py`.
- Architecture Review agent (Plex writes, worker concurrency, upgrade migration): no HIGH.
- After an owner-approved deploy: live sweep of the sflix markers DB — no `needs_review` rows for on-disk files,
  the 163 former review rows each DECIDED or NO_EVIDENCE as the table says.

## Lanes

- A — backend items 1, 2, 4, 6 (decide, store, pipeline, outcomes, job_runner, triggers, upgrade, inspect,
  job_log, api_markers, missing, inspector/statuses, unit tests).
- B — item 3 (publishers/base.py, plex_db.py, the versions bits of pipeline/outcomes/versions, tests).
- C — item 5 (JS, templates, docs, e2e tests).
- D — items 7, 8 (worker, notifications, job summary, generator wording, tests).

## Status

- [x] A  - [x] B  - [x] C  - [x] D — merged uncommitted into the main checkout (dev) on 2026-10-02; one conflict
  (tests/markers/test_pipeline.py, kept lane B's versions rule). Lane worktrees under `.claude/worktrees/agent-*`.
- Lane calls to know: a user-locked version beats the lowest-id rule (base.py); the item-2 own-marker fallback is
  gated on `_may_decide_alone` (decide.py ~505); the Inspector "Needs your check" panel was removed (Adjust stays).
- [ ] post-merge cleanup (guides.md row, v20 upgrade message, dead VERSIONS_UNCHECKED retry plumbing)
- Baseline replay saved: `evidence/decide-rules/local/no-review-before.txt` + `local/final.no-review-before/`
  (credits `--chapters` sets can't run: 3 files replaced on disk since 2026-09-25).
- [x] replay after (file read wins everywhere): intros worse (lab118 wrong 4→12, 8 of 10 new intros wrong —
  IntroDB was right on Simpsons S03E01/E04, Big Door Prize S02E03); credits better (movie_credit_truth
  103/11/18/72 → 122/43/34/5 useful/late/wrong/missed, Plex 124/11/61/8). Saved `local/no-review-after.txt`.
- Owner picked "split by type" (credits: file read wins; intros: nothing) on 2026-10-03, then asked to work out
  what is actually most accurate per disagreement. PENDING: per-cell measurement — disagree (NOTHING / FILE /
  ONLINE) × lone online (NOTHING / ONLINE), per type, plus head-to-head by source pair; outputs
  `local/final.policy-*/`. Criterion: wrong ≤ Plex on every set, then most useful+late, NOTHING when within 2
  files on held-out.
- [x] full suite: 5 failures — README sync (fixed) + 4 `tests/markers_eval` count shifts (update after the policy
  is settled)  - [x] architecture review: no HIGH; 2 MED + 3 LOW all fixed (10,207 passed)
- [x] Per-cell measurement done (`local/final.policy-D*-L*/`, `local/policy-measure.log`, scripts `local/cells.py`,
  `local/h2h.py`, `local/run_policies.sh`; temp switch lived only in worktree agent-ac44fe99befe65386).
  Intro disagree: both sides wrong 6/10 → NOTHING. Intro lone online: IntroDB ~1 in 4 wrong → NOTHING (rule 6
  stays). Credits disagree: text right-or-late 45/60 vs Plex/SkipDB wrong 2:1 (early, skips story) → FILE.
  Credits lone online: 3 cases → NOTHING (follow-up: lone IntroDB/TheIntroDB credits 24/24 on online-43).
- **OWNER DECISIONS 2026-10-03:** measured rule (only credit text wins a disagreement; everything else NOTHING,
  so intros must replay identical to 7a120c3 and credits identical to `no-review-after`); ship = lab rows
  (phase1 row 8, phase2 row 11) → sflix + live sweep → PR into dev (ask before merge); reply on #332 after it's on
  dev, text shown to owner first; a locked (hand-set) version beats the lowest-id rule.
- [x] implement the measured rule (`decide.py` `_WINS_A_DISAGREEMENT = {CREDITS_TEXT}`) + spec + markers_eval
- [x] replay proof (`local/no-review-final.txt`): every intro set identical to 7a120c3, every credits set identical
  to `no-review-after`; prod replay differs from after only on Westworld S03E07 intro (back to nothing, intended)
- [x] full suite 16882 passed, 1 failed: `test_scheduler.py::…cron_schedule_fires` — date-dependent, the next
  02:00 Sydney fire lands on the 2026-10-04 DST gap; unrelated to this change
- [x] lab row 8 on plex-previews:no-review-20261003: Plex wrote the original's markers without waiting, served
  correct per version; marked "fail" only by A2/B2 "Jellyfin 12.0 written for the second version" — harness flake:
  an Emby not-indexed-yet retry job's rows (JF "Already up to date") supersede the job's own rows in
  `/api/jobs/<id>/files` (web/jobs.py:2752-2754); the job log shows JF12 "Added". FOLLOW-UP: make that check read
  the job's own log line or accept written|up_to_date plus served-per-source. Row 11 not run.
- Owner switched to the fast path (2026-10-03): commit → PR → CI → merge → upgrade sflix → live sweep.
- [x] PR #349 all green, squash-merged into dev as 4617b74; sflix upgraded 2026-10-03 07:12 UTC, live sweep: 112
  on-disk review rows → 18 credits decided by credit text, intros nothing; 0 versions waiting (134 multi-version
  items written); 11 Animal Control S04 intros re-decided by IntroDB+season audio (legit); 6 Failed = Legends of
  Tomorrow S03 truncated files; no GPU warning. Found 79 rows still raw `needs_review` → fixed in PR #350
  (b7f98f7), sflix upgraded 07:4x UTC, 79 → 0. Backups: `/config/plex-generate-previews.pre-pr349.tgz`,
  `.pre-pr350.tgz`; old containers `-pre-pr349`, `-pre-pr350` stopped.
- [x] #332 reply text given to the owner to post (2026-10-03)
- [x] lab check follow-up: `phase1_matrix.py` `_version_case` A2/B2 accepts written|up_to_date and requires the
  served segments per media source (branch fix/legacy-review-rows)
- [x] sflix live bug: 79 raw `needs_review` rows survived decide-again (`_decisions_changed` read them as
  NO_EVIDENCE = unchanged); `DecisionRow.legacy` now forces the rewrite (branch fix/legacy-review-rows)
- [ ] owner OK to commit  - [ ] deploy + live sweep
- [x] Lab phase 2 row 11 (Plex versions drifting) on the published dev image b7f98f7: PASS — Plex served
  intro 25–55 s / credits 100–120 s at every step, no flapping, no versions-waiting lines (run 1 failed on setup
  only: a fresh DB had never decided E03). Result `evidence/lab/results/p2-row-11.json`.
- [x] DST-dependent scheduler test made deterministic (test bug, PEP 495 inter-zone compare).
- Follow-ups, not part of this chunk: PR #341 (credit text v9, captions) matters more now that credit text wins
  credits disagreements — rebase, re-measure, ship next; bigger test of lone IntroDB/TheIntroDB credits (24/24).
