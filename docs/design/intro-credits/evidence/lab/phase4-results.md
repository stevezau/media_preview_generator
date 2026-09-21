# Phase 4 lab results

The marker editor, locks and Setup Health rows against the lab servers on `storage` (`up.sh`), real library mounted
`:ro`. Tokens in `env`, scrubbed from every result file. Rows and their evidence: `phase4_matrix.py`; per-row JSON in
`results/p4-row-NN.json` (git-ignored); the added rows write `row-20.json`, `row-21.json`, `p2-row-25.json`,
`p2-row-26.json` and `p3-row-17.json`. Nothing ran on `plex`; nothing under `/data*` was written.

## Run on the final image (2026-09-21)

**Every phase-4 row passes on the image below, and so do the five rows added to the earlier matrices.**

| | |
|---|---|
| Commit the image was built from | `97fa74e` (`fix(markers): a save a server missed is reported failed and Check servers finds it`), on `513efd3` |
| App image | `plex-previews:phase4-lab`, `sha256:9e388619861d4c52f1945d5735bf284cff5f929425217d698523304dd88381bf` |
| Agent image | `plex-marker-agent:phase4-lab`, `sha256:357e917c049943b07291efdd5119dbabf0ddf916e0f2e06ac631e26604229417` |
| Containers on it | `mlab-app` (`MLAB_APP_GPU=nvidia`), `mlab-app-remote` and `mlab-app-health` (app), `mlab-plex-agent` (agent); row 12's premise checks the last two |
| Servers | Plex 1.43.4 (claimed), Jellyfin 10.11 and 12.0, Emby 4.10.0.40 and 4.9.1.90; every plugin the current build |
| Unit suite | `tests/markers` 6,982 passed on this commit; the full default suite passed 12,098 on the commit before the last review fix |

The rows ran in one pass (`phase4_matrix.py run 1 … 11`, then `phase1_matrix.py run 20 21`, `phase2_matrix.py run 25 26`,
`phase3_matrix.py run 17`, `phase4_matrix.py run 12`), 01:00–01:20 UTC, all against the same containers. Row 25 was
run once more alone afterwards, when the wait after Emby's wipe was fixed (see Harness notes). An earlier pass over the
same image (00:26–00:47 UTC) had passed every row too, with row 20's last check reworded after its first run. The phase
1–3 matrices themselves were **not** re-run here: that is a later step on the final image.

| Row | Result | Checks | What it proved |
|---|---|---|---|
| 1 Adjust + Save | pass | 21 / 21 | A save (intro 12.500–41.250 s, credits 103.250–118.500 s) is a 200 in under the 20 s bound, and **before it returned** all five servers served exactly those times, read from each server itself. Plex's rows are the edit (credits start stored 2 s early, as for ours always). markers.db holds both rows `locked=1`, `decided_by ["user"]`. The same save again reports `unchanged` on all five. Five refused bodies (end before start, past the end of the file, start at the end, a non-integer time, a path outside every library) are 400s that changed nothing. |
| 2 Lock alone | pass | 12 / 12 | Saving the decided times unchanged: every server `unchanged`; Plex's row ids, Jellyfin's store files (name, mtime, size) and Emby's plugin state are identical before and after; markers.db `locked=1` at the decided times; a forced re-detect finds every server up to date, changes nothing, and the rows stay locked. |
| 3 Forced re-detect and Check servers | pass | 12 / 12 | Locked 27–52 s / 102–116 s over chapters of 25–55 s / 100–120 s. A forced run re-reads the chapters (evidence newer, still 25–55 s / 100–120 s), leaves the decision and all five servers alone; a locked type carries no proposal, the evidence rows are where the detected answer shows. With every server's markers dropped by hand, Check servers writes the **locked** times back on all five (not the chapters'), and a second run lists nothing for the file. |
| 4 Unlock | pass | 14 / 14 | Unlock publishes nothing (every server still serves the edit; markers.db rows unlocked; the reason reads "unlocked; the next run decides this type again"). The next run writes the chapters' answer on all five. A file detection has nothing for (Needs review): an edit reaches all five, unlock plus a run brings the intro back to Needs review, and the edit is gone from every server, back to exactly what they served before. |
| 5 Q1 on Plex | pass | 38 / 38 | Five cells, exact times on Plex and the other four, rows and markers.db each. See below. |
| 6 Q1 on Emby 4.10 and 4.9 | pass | 41 / 41 | Five cells on each Emby. See below. |
| 7 Save with a server stopped | pass | 10 / 10 | Jellyfin 10.11 stopped: the save is a 200 in 8.3 s (its 8 s publish deadline, not a hang), Jellyfin's row is `failed` ("Can't reach this Jellyfin server"), the other four `written` and serving the edit, markers.db locked. After Jellyfin starts it still serves the old answer; the next Check servers run writes the edit to it (`markers_written`) and all five serve it. **This row failed on the first image and found a product bug; see below.** |
| 8 The two D8 shapes | pass | 32 / 32 | A recap, a preview, and an intro plus a recap are each refused with 400 ("No server with Intro & Credits turned on for this file can show a recap marker", and the same for preview), with only Plex, only Emby 4.10, or Plex and both Embys on; nothing stored, every server unchanged. With Jellyfin on, the recap is accepted: both Jellyfins serve it at 5–9 s beside the detected intro and credits, Plex and both Embys name it in `cant_show` and are unchanged. An edited credits **end** (103.250–110 s) is accepted and written on all five; Plex and both Jellyfins serve the whole span, both Embys the start only, each Emby row carries the note "Emby skips to the end of the file", the Inspector says it too, and markers.db keeps the full end. |
| 9 Chapter titles | pass | 12 / 12 | A staged episode with chapters `Intro` and `Ending`: credits are read from `Ending` (evidence label Ending, 100–120 s) and published on all five. One with only a lone `Intro` chapter and no credits chapter: the intro is decided and published on all five, no credits. One whose last chapter is `End`: no credits decided or served (a scene name), intro published. |
| 10 Setup Health, Plex | pass | 11 / 11 | Lab Plex reads good on all four rows; own detection on: only that row fails; marker list hidden: only that row fails; good again after. An unclaimed second Plex: Plex Pass row fails (critical, `not active` → `active`), database row passes, the rows past Pass aren't read so aren't shown. A copy of the lab database: the database row fails (critical, `another machine` → `this machine`), Plex Pass still reads active. See below. |
| 11 Setup Health, plugins | pass | 41 / 41 | On each of Jellyfin 10.11, 12.0, Emby 4.10, 4.9: current build reads ready; plugin removed: install row critical, `not installed`, no outdated row, Edit tab `needs_plugin`; an older build: install row passes with the older version, the outdated row fails (recommended, `<older version>` → `newest`), Edit tab `plugin_outdated`; current build back: ready, no outdated row. See below. |
| 12 The Plex marker agent | pass | 9 / 9 | All nine steps of `phase4_row12_agent.py` on this image: the app container can't open Plex's database and publishes through the agent (intro 10–40 s, credits 98–120 s stored, served 100–120 s), reads back, a locked save replaces Plex's own intro through it, a wrong key, a version mismatch and an agent beside another Plex are each refused with rows unchanged, the agent stopped gives `agent_unavailable` with the "Markers wait here…" message and no rows, and the rows are taken off again. Premise: both containers run the images above. |
| P1 20 Locked credits on Plex | pass | 13 / 13 | Keep Plex's on, credits taken off Plex's item and Plex's own forced detection writing them (1297.324–1321.472 s). A save at 1277.324–1317.472 s replaces them (`replaced_own ["credits"]`, message "Replaced Plex's own marker. This server is set to keep Plex's, but a marker you adjust always wins."). Plex's forced detection puts its own back over the lock; a normal job writes the lock again, a forced job leaves it, markers.db stays `locked=1`. Unlock and Restore: Plex serves what the run decided, unlocked. |
| P1 21 Database the app can't reach | pass | 7 / 7 | Row 11's network-share case (a database file the app can't open) publishes through the agent: the app container has no `Databases` folder, without the agent its capability isn't ready, with it `ready`, and Plex serves intro 10–40 s and credits 100–120 s. |
| P2 25 Locked marker through the Emby publisher | pass | 19 / 19 | On both Embys under Keep Emby's with Emby's own intro 20–50 s and credits 110 s in `library.db`: the save replaces the intro alone (credits still Emby's 110 s) and says it replaced Emby's own intro; after Emby's own "Replace all metadata" wipe the locked intro is written back from the plugin's copy; Check servers leaves it and a second run has nothing to do; unlock plus a Restore run hand both types back to detection; locking both replaces both of Emby's types. |
| P2 26 Season view Edit | pass | 11 / 11 | In the real page (Playwright): Season view, **Edit** on E02, intro typed 0:20–0:44, **Save**. The save is a 200; the row now reads 0:20 – 0:44 with "🔒 Locked by you"; the Season data holds the locked intro; all five dots are green; markers.db and every server serve the edit (the editor also locks the credits at their detected start). |
| P3 17 Locked credits from credit text | pass | 9 / 9 | At Medium credit text alone decided the credits near 540 s. Locked at 560–640 s; a forced run detects again (credit-text evidence newer, still near 540 s), leaves the lock (`decided_by ["user"]`, reason "locked by user"), and every server still serves the locked times. Unlock and a High run leave none of them anywhere. |

## Row 7 found a product bug (Cause / Fix / Proof)

**On the first image (`513efd3`) row 7 failed 2 of its 9 checks**: Jellyfin's row came back `not_enabled`, and the next
Check servers run published nothing to it (Jellyfin still served the old answer afterwards).

- **Cause:** two things. `api_markers._EDITOR_RESULTS` turned every `markers_skipped` publish row into `not_enabled`,
  but a job also calls "skipped" a server that has Intro & Credits **on** and couldn't take the markers just then (down,
  plugin missing). The editor badge read "Intro & Credits off" with "Turn on Intro & Credits for this server". And the
  editor's promise that such a server "gets them at the next Check servers run" was never kept: Jellyfin shows what this
  app last left there, so it hasn't drifted, and a skipped publish isn't a failed item, so the backoff that retries
  failed items never reached it either. Nothing listed the file until another job happened to run it.
- **Fix:** `api_markers.py` `_editor_server_row` reports a skipped row of a server with Intro & Credits on as `failed`
  (a server with it off stays `not_enabled`); `store.py` `files_with_undelivered_locks` and `reconcile.py`
  `files_of_undelivered_locks` (wired into `check_servers_listing` ahead of the failed-item retries) list every file with a
  locked marker whose last publish to an owning server was `failed` or `skipped`, on every run with no backoff, at most
  `UNDELIVERED_LOCKS_MAX` (100) files a run. Spec §6.2 step 6 and a dated §14 line, `guides.md` and `reference.md` say so.
- **Proof:** tests written first and failing for these reasons (`tests/markers/test_api_markers_edit.py`,
  `test_reconcile.py`, `test_store.py`), then passing; row 7 on the final image: 10 / 10, Jellyfin `failed`, then
  `markers_written` by Check servers, all five serving the edit.

The Architecture Review found no HIGH and two MED on this diff, both about that list: a server that stays not ready
(plugin missing) keeps its files listed on every run, and an uncapped list could crowd out the other two. The cap and its
test (`test_locked_edits_a_server_missed_are_capped…`) are the answer; the cost of a listed file whose server stays not
ready is one cached capability check and no write.

## Row 5: Q1 on Plex (locked type × Keep Plex's × Plex's own markers)

Synth episode 2 (chapters 17–47 s / 100–120 s), Plex's own rows written into the lab Plex's database as its detection
leaves them (intro 2–22 s, credits stored 108–120 s, served 110–120 s). Plex serves a credits start 2 s after the stored
one, ours and its own alike. Locks: intro 12.500–41.250 s, credits 103.250–118.500 s.

| Cell | Plex serves | Row says it replaced Plex's own |
|---|---|---|
| A Keep, lock intro, Plex has intro and credits | intro = edit, credits = **Plex's own** (kept) | `intro` |
| B Keep, lock both, Plex has both | both = edit | `credits`, `intro` |
| C Keep, lock intro, Plex has credits only | intro = edit, credits = Plex's own | nothing |
| D Keep, lock intro, Plex has none of its own | intro = edit, credits = detected | nothing |
| E Restore, lock intro, Plex has both | intro = edit, credits = detected (Restore replaces Plex's) | nothing |

Every cell also asserts Plex's database rows, that the other four servers serve the lock plus the detected answer (Keep
Plex's is Plex-only), and that markers.db locks exactly the saved types. After cell A a forced run leaves the lock and
the kept credits, and an unlock plus a run gives the detected intro back while Keep Plex's still holds Plex's credits.

## Row 6: Q1 on Emby (locked type × Keep Emby's × Emby's own rows)

Both Embys, Emby's own rows added to `library.db` with the container stopped (its detection is an Emby Premiere feature
the lab lacks; the same method as `emby_plugin_check.py` check 17): intro 20–50 s, credits start 110 s. Cells A–E are the
same as Plex's, and for each Emby: what it serves (a locked type is ours, an unlocked type is Emby's own under Keep
Emby's and ours under Restore), `replaced_own`, and that Plex and both Jellyfins serve the lock plus the detected
answer. All ten server-cells pass.

## Row 9: chapter titles, and why the episodes are synthetic

The plan asks for a real anime episode. The lab mounts none: a real season would mean recreating the servers with a
new mount. The rule reads chapter titles only, so `synth_chapters.sh` now stages three 120 s episodes (`Ending`, a lone
`Intro`, a final `End`) that the row copies next to Synth Chapters 1–3, scans on all five servers, runs, and removes
again. This gives exact truth and all five servers instead of Plex and Jellyfin 10.11 only. What it does not give is a
real release's chapter names; the shapes the rule reads are unit-tested in `tests/markers/test_chapters_anime.py`.

## Row 10: a Plex without Plex Pass

The plan preferred a second unclaimed throwaway Plex over a stubbed `capability()`, and that is what ran
(`phase4_health_up.sh`): `mlab-plex-nopass` never claimed, with its own config volume, and `mlab-app-health`, an app
container that sees that volume at `/plexnp` and a **copy** of the lab Plex's database at `/plexcopy`. The lab Plex is
untouched (four other rows need it claimed).

- An unclaimed Plex refuses **every** token and answers only a tokenless request from its own loopback, while the app
  insists on a token. `mlab-plex-nopass-proxy` (nginx, in that Plex's network namespace) re-sends each request to
  `127.0.0.1:32400` without the token header and with a loopback `Host`. Plex itself is real and unmodified: its
  `myPlexSubscription` is false and its database is the file the app reads.
- "Database not on this machine" is a database file that is not the one the running Plex holds. The copy is that: the
  capability check reports `needs_local_db` and the row fails. An empty volume would read `misconfigured` (no file) and
  show no such row.
- "Marker list absent" hides the lab Plex's marker tag row for the cell (`update tags set tag_type=912`, put back in
  `finally`), because the unclaimed Plex stops at Plex Pass before it reads the list, so it can't show that row.
- "Own detection on" turns the lab Plex's two detection settings on for the cell and back to `never`.

## Row 11: the older plugin builds

"Outdated" means the installed plugin answers Ping but doesn't list the `markers` feature. Only Jellyfin 10.11 has a
released build from before markers (`plugin-v10.11.0.3`, downloaded as published; it reports 10.11.0.3). Jellyfin 12.0
and the Embys never shipped one, so `phase4_old_plugins.sh` builds the current source with that one feature left out of
Ping (12.0.0.3 and 0.9.0.0), the way CI builds it. Each stage moves the current plugin to `/config/plugins-held`,
restarts the server, installs the older build, restarts, then puts the current build back; `finally` restores all four.
Jellyfin's install row keeps its existing id (`plugin_installed`), Emby's is `markers_plugin_installed`; the outdated
row is `markers_plugin_outdated` on both.

## What did not run, or ran differently

- **Row 13** (phase 1–3 regression on this image): not run, as instructed; the later step does it.
- **A real anime episode** for row 9: synthetic, as above.
- **The Inspector's page for the D8 refusal (row 8):** the API refusals and the `can_show` the Inspector reads are
  asserted; no browser check of the editor's disabled control beyond the e2e tests in `tests/e2e`.
- **Row 12's "written result lost after the agent committed"** case is unit-tested only, as `phase4-row12-agent.md` says.
- **The first pass ran on the image built from `513efd3`** (`sha256:5260de14…` app, `sha256:c57aced1…` agent): rows 1–6
  and 8–12 passed there and row 7 failed as described. Everything above is the pass on the final image.

## Harness notes

- Row 20's last check first compared what Plex serves after an unlock to the answer read before the lock. Detection is
  decided afresh after an unlock, and TheIntroDB had started answering in between (1298.000 s instead of 1295.000 s), so
  the row now compares with what markers.db holds after the run, unlocked, and not the locked times.
- `settle()` puts a synth episode back to its chapters' answer between cells. Plex keeps our earlier rows when they agree
  with the decision within its version tolerance, so an edit only milliseconds off (the Season view's credits end, 8 ms
  past the chapters') survives a plain re-run. It then moves the episode far away with a save first, so the run has to
  write the chapters' answer exactly.
- Every row's cleanup runs through `run_cleanup` (`phase1_matrix.py`): each step runs whatever the others do, and the
  row's last check says whether the lab went back. A raise inside a `finally` would have skipped the steps after it and
  replaced the row's own error. Row 11's restore does nothing when the plugin is already back, so a failure after a
  restore can't delete the plugin that was just put back.
- Row 25 first waited on the intro alone after Emby's "Replace all metadata", which was already there before the wipe,
  so the read raced the wipe and Check servers then did the write-back. It now waits for Emby's own credits to be gone
  and the plugin's copy (the locked intro and our credits) to be back, and asserts that exact state.
- `phase4_row12_agent.py` read its agent image from a hard-coded tag in the skew and wrong-Plex steps; it now reads
  `MLAB_AGENT_IMAGE` like `phase4_row12_up.sh`.

## Reset

```bash
./phase4_health_up.sh down                 # the unclaimed Plex, its proxy and mlab-app-health (volumes kept)
./phase4_row12_up.sh down                  # mlab-app-remote and mlab-plex-agent
docker volume rm mlab_plex_nopass_config mlab_plex_nopass_transcode mlab_app_health_config mlab_p4h_plexcopy
```

The rows put the synth episodes back to their chapters' answer on every server (`settle`), remove the staged episodes
they added, switch Keep Plex's, Keep Emby's and Plex's detection back off, and turn every server's Intro & Credits back
on. The one real-library file row 20 uses ends with the decision the last run made.
