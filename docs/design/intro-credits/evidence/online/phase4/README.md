# Phase-4 AniSkip measurement scripts

These produced every number in `../../eval/aniskip-facts.md` (2026-09-20). They write their data beside themselves,
and that data is excluded from the repo (`evidence/**/*.json`, `evidence/**/*.jsonl`, plus `animelists.xml`) because
it holds real library paths.

Run them in this order, from the repo root. `PY` is any Python that can import `media_preview_generator`:

```bash
E=docs/design/intro-credits/evidence/online/phase4
PY=python                                        # a Python that can import media_preview_generator
bash           $E/fetch_maps.sh                  # the two anime id maps, cached here
nice -n 19 $PY $E/resolve.py                     # library -> anime -> MAL id + episode number
nice -n 19 $PY $E/sweep.py                       # ask AniSkip for each (paced; stops on 429)
nice -n 19 $PY $E/chapters.py                    # ffprobe chapters for the same files (read-only)
nice -n 19 $PY $E/analyze.py                     # what came back, and the chapter scoring
CONFIG_DIR=$(mktemp -d) nice -n 19 $PY $E/compare.py 120   # the other sources on a random sample
nice -n 19 $PY $E/sonarr_anime.py                # the anime cross-check
nice -n 19 $PY $E/limit_test.py                  # the one rate-limit boundary test
```

**`compare.py` is the one script that is not purely a reader.** It calls the app's real source clients, so it spends
about 120 lookups of TheIntroDB's anonymous daily allowance and records source usage in a `markers.db`. Give it a
throwaway `CONFIG_DIR` as above so it never touches the app's own database.

Running from a git worktree: the phase-2 Plex dumps are local-only and live in the main checkout, so set
`MARKERS_PARTS_DUMP` (for `resolve.py`) and `MARKERS_PLEX_DUMP` (for `compare.py`) to their paths there.

`resolve.py` reads the phase-2 read-only dump of prod Plex's parts
(`../../lab/results/scale/prod_plex_parts.json`) — it never queries a server. Nothing here writes under `/data*`;
`chapters.py` is the only script that opens a library file, and `ffprobe -show_chapters` reads the container header
only.

`compare.py` also needs imdb/tmdb ids for the anime shows — the same ids Plex's guids carry. It reads them from a
Sonarr export (`sonarr_series.json`), which `sonarr_anime.py` reuses. Produce it with credentials from
`~/.variables.yml`, never inline:

```bash
curl -s -H "X-Api-Key: $SONARR_KEY" "$SONARR_URL/api/v3/series" > $E/sonarr_series.json
```

**Be a polite API citizen.** Every script identifies itself in the User-Agent, paces below the documented limit and
stops on a 429. `limit_test.py` is the single deliberate exception and is capped at 140 requests in one 58 s window.
