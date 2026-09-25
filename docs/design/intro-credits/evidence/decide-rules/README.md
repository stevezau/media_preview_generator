# Decision rules after the production audit (2026-09-25)

Five rules proposed from the 2026-09-25 production audit, measured at decide level before any was built: spec §14
2026-09-25 "Decision rules" has the outcome and numbers; §5.5 rules 2, 3, 6 and 14 the rules as built. Owner's bar:
decisions automatic (Needs review is not a fix), useful ≥ Plex and wrong ≤ Plex on every measured set.

| Rule | Outcome | Where |
|---|---|---|
| (a) a lone SkipDB answer can't decide | adopted (any type) | `decide._AGREEMENT_ONLY` |
| (b) chapter vs SkipDB (+ a server marker) → run credit text | adopted: credit text checks a credits chapter SkipDB disagrees with; clusters with credit text outvote it | `decide._decide_from_chapters`, `_text_over_chapter`, `_text_should_check_chapter`; `pipeline._decide` source order |
| (c) clamp an online credits end past the file | adopted for IntroDB/TheIntroDB, 5 s | `decide.ONLINE_END_PAST_FILE_MS` |
| (d) a source that can't decide alone doesn't block one that can | rejected | `variants.py` `d`, `dw` |
| (e) same-length shifted IntroDB → season audio decides | adopted with ≥ 30 s and > 15 s | `decide._without_another_releases_intro` |

## Sets

All at decide level from stored answers; no media is read except the frame sheets.

- **Intros** (`intro_sets.py`): lab 118, held-out 175, Accused, the #310 library chapter set, Bones S05–S08, from
  `../intro-end/evidence_*.json` and the Bones inputs; `--skipdb` adds SkipDB's answer from the ODbL dump
  (`../online/skipdb-dump.json`, nearest duration, exact ≤ 2 s, shifted ≤ 15 s).
- **Credits** (`credits_sets.py`): movies40, tv40 (the 80 hand-checked) and the 205 movies, credit text answers from
  the phase-3 harness run (`local/ae3_gpu.json`), Plex's markers from the lab scale run, SkipDB from the dump;
  `--chapters`, `--no-plex`.
- **Online 43** (`online_set.py`, credit text from `online_text.py`).
- **Production replay** (`prod_replay.py`): every present file of the audit's `markers.db` copy (`local/post.db`,
  after #312) decided the way `pipeline._decide` builds its context; `--text` adds credit text where a run now reads
  it (`prod_text.py`, `run_prod_text.sh`), `--next` Plex's own markers as the current reader stores them
  (`plex_next.py`, from the audit's Plex pull `local/rows.json`).
- `variants.py` holds every rule as a monkeypatch on the base tree (`run_variants.sh`, `run_b.sh`, `run_noplex.sh`,
  `run_online.sh`); `compare.py`, `compare_prod.py`, `diff_sets.py` list what moved. Data studies: `skipdb_vs_sa.py`,
  `ct_vs_skipdb.py`, `rule_e_data.py`, `patterns.py`, `lone.py`, `got_overshoot.py`.
- Frame sheets (`verify_sheets.py`, `verify_a1.py`, `dd_sheets.py`, `sheet.py`: nice 19, 2 threads, read-only) of
  every changed production decision and the two online-set credits scored against late chapter truth:
  `local/verify/`.
- `prop_debug2.py` with `local/prop_case.pkl`: the rule 4 composition gap the property test found (same result on
  40311c3; spec §0).

## Re-run

```bash
cd docs/design/intro-credits/evidence/decide-rules
./measure_final.sh            # base (local/base = dev 40311c3) and work (this checkout); writes local/final/
python compare_prod.py local/final/prod_text_base.json local/final/prod_text_work.json
python diff_sets.py local/final/intro_skipdb_base.json local/final/intro_skipdb_work.json
```

Credit text answers need `MEDIA_PREVIEW_TEXTDET_MODEL` pointing at the phase-3 model
(`../credits/bench/textdet-model/ch_PP-OCRv4_det_infer.onnx`) only for `online_text.py` and `prod_text.py`; everything
else reads caches and stored answers. `imdb_map.py` reads the Sonarr key from `~/.variables.yml` at run time and never
prints or writes it.

**Local-only** (`local/`, gitignored, storage only): the base tree, the audit's `post.db` and `rows.json`, every JSON
input and result, the Bones inputs, frame sheets, and the property case.
