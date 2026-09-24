# Plex markers made for an earlier file — evidence

Plex keeps an item's markers when its file is replaced, so they can describe the old file (production, 2026-09-24:
Bones' markers were detected 2025-06-17 on the Blu-ray files Sonarr replaced with 25 fps files on 2026-09-23, leaving
intros 9–17 s late and credits past the end). The rule (`publishers/plex_db._types_not_made_for_file`): a type's rows
are stale when no live part carries Plex's own `pv:intros`/`pv:credits` record of those times, and every row was
tagged more than 60 s before every live part last changed. Stale markers are stored flagged (`Candidate.stale`),
dropped as `decide` starts, and don't stop our detection; "Keep Plex's" still keeps one when we find nothing.

The scripts ran from the session scratchpad on read-only exports of prod Plex's database and a markers.db snapshot;
their paths still point there. The raw exports (`parts.tsv`, `taggings.tsv`, `chapters.tsv`, `parts_raw.json`,
`taggings_raw.json`, tens to hundreds of MB) and the frame sheets were not kept; results are local-only.

## Headline numbers

- Validation (`validation/`): against Sonarr's own import history (`sonarr_imports.jsonl`), no false positive among
  5,493 files Plex re-detected after their replacement.
- Live check on production (`live-check/`, frame-checked): 52 stale Plex markers can't be right for the file at all
  (already refused by the impossible-marker rule, `decide.unusable_server_marker`); of the 29 disagreements where Plex's stale marker could fit, ours right 14, a tie 4, Plex
  right 7, both wrong 4 (ours wrong 11 against Plex's 18). Deciding again changes 2 answers (credits: Needs review →
  decided from credit text) and leaves every other answer as it was.
- Bones S05–S08 (`bones/`, and `../speed/integration-proof/`): the rule flags Plex's intro on all 41 25 fps files
  (and S06E19, whose file changed after its markers); decided from season audio and IntroDB with those markers
  flagged: **82 / 0 / 0**.

## Files

| Folder | What |
|---|---|
| `validation/` | `load.py` (reads the exports), `classify.py`, `pertype.py` (does the `pv:` key hold the rows' times; are the rows older), `evalq3.py`, `q5.py`, `chapeval.py`; `sonarr_imports.jsonl` (local-only) |
| `live-check/` | `live_check.py` (the rule on a fresh prod export + markers.db snapshot), `disagree.py`, `adjudicate.py` + `frames.py` (frame sheets around both starts), `tally.py` (verdicts), `before_after.py`, `export2.sql`; `patch_lc*.py`/`peek*.py`/`paths.py` are one-off helpers |
| `bones/` | `bones_q.sql` → `bones_plex.tsv` (local-only): Bones' parts, their update times and Plex's marker rows with their tag dates |
