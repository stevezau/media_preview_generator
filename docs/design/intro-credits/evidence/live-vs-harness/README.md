# Live against the harness after #320–#325 (2026-09-28)

sflix ran `dev` 82dc2bc (#320, #323, #324, #325) and its re-check after the version bumps finished at 06:31. The
audit that followed (`local/after/work`, fresh sample seed 20260929) contradicted what the harnesses said the fixes do.
This lane found why for each named file, fixed what was wrong in the code and in the harness, and measured the fixes on
the baseline answer key (the 2026-09-27 audit) and on the fresh sample. Spec: §6.2 step 3 ("A detector's new version"),
§5.5 rule 15, §5.3 (end picture), §14 2026-09-28 "Live against the harness".

Local-only (gitignored, library paths): `local/` (see `common.py`): the audit's folder and its `markers.db` copies (now,
before #323, before #320), the replays, the shipped tree's export, contact sheets. On `storage`, in the main checkout's
copy of this folder.

## Why live wasn't what the harness showed

- **10 Things I Hate About You** (and A Beautiful Imperfection). Never read with credit text version 7: its stored
  answer is version 6 (`evidence_versions`), read on 2026-09-27 at 03:10 UTC when #320's decide-rules pass ran every
  file; no log line names the file after the restart. The version re-run (`versions.files_to_read_again`) lists a
  file for a detector only when an unlocked decided type **rests on** that detector's older answer; both films'
  credits rest on their chapter (credit text only kept it), so #324's credit-text-only bump listed 0 of the 161 such
  files. Not the device (the file never reached v7), not a lock or a keep. Read on sflix's own Intel UHD 770 in a
  throwaway container (`run_intel.sh`: VAAPI decode, text detection on the iGPU), version 7 says "the frames move the
  credits chapter at 5529232 ms to 5573568 ms", the same as storage's P5000, so the harness's decode matches the app's.
  A Beautiful Imperfection stays wrong once read: version 7 keeps its chapter (the epilogue shape #324 measured).
- **The harness** (`../credits-accuracy/replay.py`) re-decided every file with the tree's credit text, from the
  audit's `markers.db` of 2026-09-27, taken *before* #320. That database needed #320's decide-rules pass, which runs
  every file, so v7 reached 10 Things there; on sflix #320 had already run and #324 reached only the files resting on
  credit text. It also counted three types every server keeps Plex's own marker for (3 Days to Kill, 3 Women, 5 to 7),
  which the app neither reads again nor publishes. Fixed: the replay now asks the tree's own re-run listing on the copy
  it starts from and gives the tree's answers only to the files it lists (`--every-file` for the old behaviour), takes
  `--db` (the live copy after the previous release), models the carry-over, and marks kept-own rows, which
  `moves.py`/`diff_replay.py` leave out. Started from the pre-#323 copy, the shipped tree now lists 1133 files (the
  live log said 1133), moves exactly the three published credits live moved (Doc S02E14, The Vampire Diaries S05E09,
  CIA S01E02) and leaves 10 Things at the chapter; on the files the harness read it agrees with live on 375 of 376
  credits (the other is an online-only Big Bang Theory row left from older rules).
- **Season audio's harness** (#323's `v10/lib.py`) matches the app: run on the regressed episodes it gives the live
  answers exactly. Its sets and split-season sample just didn't hold these seasons.

## Each named file

- Homicide Hunter S06E01/E03 (lost): #323's grouping. The two files sat alone in `/data_16tb2`'s season folder and
  matched each other (1/1); the season on every disk has 20 episodes, and their opening is matched by 5 and 4 of the
  19 others (Kenda's voice-over over the titles breaks the pairs; `season_members.py`), under the 50 % quorum. The
  second-opening quorum doesn't take it: one supporter (E12) sits among the other opening's episodes. No change: the
  group is right, and no rule found reaches them without lowering the quorum.
- Game of Thrones S08E06 (lost, then held as "only IntroDB"): #323's grouping moved the cluster's median end 0.43 s
  (111.58 → 111.15) and the end-picture check failed there: "Directed by" on near-black at σ 4.4–4.8 against the
  partners' 2.6–3.3 (a correlation of 1.00 with E04) was "not alike" because one side sat under `FLAT_STD`, so both
  partners shared 0.5 (`endpic_probe.py`). Fixed (check version 4).
- Somebody Somewhere S03E03 (held): #320's rule "a lone online answer kept through a rule change goes once a detector
  that reads the file found nothing to agree" (season audio found nothing). The same rule took off S03E07's lone SkipDB
  intro, which skipped 9 s of dialogue. By design; no change.
- Westworld S04E05/E06, Stargate Atlantis S01E10, Stuart Fails S01E01 (held): not new. The baseline audit's own
  `markers.db` has the same holds, decided 2026-09-25/26 (season audio against SkipDB 11 s apart on the end; IntroDB
  11 s late on a 23.976 fps release; IntroDB alone). The fresh sample found them.
- The Floor S05E06 (harmful): not a regression. The season's first run was after the deploy, and the pre-#323 tree
  gives the same answer on the same stored data: a per-round sting found by 6 of 11 episodes at different times meets
  the quorum, the real opening only 4 of 11. Needs a "plays once per episode" signal; not done here.
- Killer Cases S03E04 (harmful): not new: credit text's 41:52 on the courtroom sign is the same in versions 5, 6 and 7
  (read 2026-09-25, 27, 27).
- Small Prophets S01E05 (and E06): the carry-over brought back season audio version 9's 0–12 s logo stretch after
  version 10, which passes it over, found nothing on the same files: the path's earlier identity had decided it. Fixed
  (decide rules version 3).

## Fixes

1. **Credit text and season audio list every unlocked decided type beside their older answer**
   (`AnswerVersion.checks_others`): they check what other sources decided (§5.5 rules 3, 4, 16). On today's sflix
   copy: 161 more credit text files, 10 Things and A Beautiful Imperfection among them.
2. **A marker only content detectors decided isn't carried** to a same-length replacement a **newer** version of them
   read and found nothing in (`carry_over`'s `read_by`, `ReadNow.newer_than`). The same version finding nothing is
   rule 15's own case (another encode) and still carries. The deciding versions are kept aside with the snapshot
   (`replaced_versions`, matched on `seen_at`, no schema bump) or read from a replaced file gone from disk; a snapshot
   kept aside by a build without them counts as decided at most at season audio 2010 and credit text 8 (#327's
   version, in case it ships first)
   (`VERSIONS_BEFORE_THEY_WERE_KEPT`), which is how Small Prophets' (season audio v9, re-read at 3010) still drop.
   The verdict counts only while it isn't due again (`pipeline._read_now`, shared with rule 16): a "nothing" whose
   read again failed after the season changed is from before. Decide rules version 3; the re-run lists files holding
   a carried marker too (a carried marker has no answer of its type).
3. **A flat frame beside one that isn't is compared by correlation** (`end_picture.frames_alike`, check version 4),
   as the spec already said; a frame of one grey level still matches no picture.

## Measured

Replays of today's sflix copy, the shipped tree against this branch, each as the live app runs them (`replay.py`,
v7 credit text from `../credits-remaining` plus `vtext_paths.py` for the 10 listed files it lacked, season audio from
`season_repro.py` after `reshare_db.py`): 5 published markers change library-wide, nothing else moves by 1 s.

| | Shipped | This branch |
|---|---|---|
| 10 Things I Hate About You credits | 1:32:09.2 (43 s early) | 1:32:53.6 (truth 1:32:37–1:32:52) |
| Game of Thrones S08E06 intro | Needs review | 0:06–1:51.2 (IntroDB + season audio) |
| House of the Dragon S02E03 intro | Needs review | 0:06–1:46.6 (SkipDB + season audio; frame-checked) |
| Small Prophets S01E05, E06 intro | 0:00–0:12 (logos) | none |

Baseline answer key (`../credits-accuracy/metrics.py`, split 70/30 by file with seed 20260927): verdict credits wrong
10/88 → 9/88 (skips story 3 → 2; tuning 6 → 5, held out 4 → 4), Plex comparison 10/81 → 9/81 (1 → 0), intros 5/51 →
5/51. Fresh sample (`proof_fresh.py`, 70/30 by file with seed 20260929): published wrong 11/78 → 10/77 (tuning 9 → 8,
held out 2 → 2), the lost-right Game of Thrones S08E06 (held out) back, Homicide Hunter S06E03 still lost.

Season audio's four truth sets (#323's harness, 570 files) with check version 4: no answer changes. Every one of their
605 shares measured again (`share_variants.py`, decoded once, each variant on the same frames): check 3 gives the
cached value for all 605, check 4 raises 4 (none enough to change an answer). On sflix's 731 stored shares (94 not
readable on storage: one show ffprobe rejects here), check 3 again equals what sflix stored for every one, and check 4
raises 22, turning 15 passing on the four files above; two answers change (the table).

Not changed after review: the end card needing a picture on **both** sides (`--card-both` in `share_variants.py`).
It changes no share of the 605 and one of sflix's: Game of Thrones S08E06 against E02 at the old folder group's end
(111.58 s), a right pass (1.0) that would fail (0.67), the card fainter in one release. Its current answer (111.15 s:
E04 1.0, E02 0.5) is the same either way, but the rule only ever removed a right pass, so it stays out.

After review of #330 (HIGH: the carry-over dropped a marker when the same version found nothing; MED: a due answer
whose read failed counted as a verdict): the replay of today's copy and both samples give the same numbers as above;
the re-check lists the same 1410 files. Merged with dev's credit text 8 (#327), which re-reads every credit text
answer, sflix's re-check lists 1410 files: decide rules 1373, season audio 1107, credit text 1332 (863 resting on credit
text, 346 decided by other sources, which this branch adds, and 123 undecided).

## Scripts

| Script | What |
|---|---|
| `season_repro.py`, `season_members.py` | The app's season step for one episode from a `markers.db` copy: candidates, support, guards; each partner's runs |
| `endpic_probe.py` | Per-instant end-picture comparison (σ, mean, correlation) for one pair |
| `reshare_db.py` | Every stored share under the pass line measured again with the tree's check, into a copy |
| `share_variants.py` | Shares decoded once and compared under check 3, check 4 and the end card needing both sides |
| `due_diff.py` | The tree's version re-run listing on a copy |
| `vtext_paths.py` | The tree's credit text through the harness caches for a `{path: is_episode}` file |
| `intel_probe.py`, `run_intel.sh` | Credit text on sflix's own GPU in a throwaway container (read-only mounts) |
| `states.py`, `dump.py` | A file's rows across the three snapshots; one file's rows |
| `proof_fresh.py` | The fresh sample before/after from two replays |
