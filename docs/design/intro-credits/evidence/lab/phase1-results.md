# Phase 1 lab results

Lab servers on storage (read-only calls): Jellyfin 10.11 `mlab-jellyfin`, Jellyfin 12 `mlab-jf12`, Emby `mlab-emby`,
Plex `mlab-plex`. Real library mounted `:ro`.

## Task 6 — `get_external_ids` (2026-09-14, staged code before fix round 1)

| Server | Item | Returned | Result |
|---|---|---|---|
| Jellyfin 10.11 (API key and user login) | Rick and Morty S01E01 | `{kind: episode, tmdb: 60625, imdb: tt2861424, tvdb: 275274, season: 1, episode: 1}` | pass |
| Jellyfin 10.11 (both) | Toy Story | `{kind: movie, tmdb: 862, imdb: tt0114709, tvdb: None}` | pass |
| Jellyfin 12 (both) | Rick and Morty S01E01 | same as 10.11 | pass |
| Emby (both) | Rick and Morty S01E01 | `{kind: episode, ids: None, season: 1, episode: 1}` | ids not confirmable: lab Emby has empty ProviderIds |
| Emby (both) | Toy Story | `{kind: movie, imdb: tt0114709}` | pass (Emby only has the imdb from the file name) |
| Plex | Rick and Morty S01E01 | `{kind: episode, tmdb: 60625, imdb: tt2861424, tvdb: 275274, season: 1, episode: 1}` via `/library/metadata/3?includeGuids=1` then the show | pass |
| Plex | Toy Story / Up | tmdb 862 / 14160, imdb ok, tvdb 318 / 315 | pass; tvdb movie ids dropped in fix round 1 |

Found by the same check: when the show lookup failed, episodes fell back to their own episode ids (Jellyfin:
tvdb 4711142; Plex: tmdb 973400) — fixed in Task 6 fix round 1 (episodes use show ids only).

## Task 6 — after fix round 1 (2026-09-14)

| Server | Item | Returned | Result |
|---|---|---|---|
| Jellyfin 10.11 and 12 (API key and user login) | Rick and Morty S01E01 | `{kind: episode, tmdb: 60625, imdb: tt2861424, tvdb: 275274, season: 1, episode: 1}` | pass |
| Jellyfin 10.11 (both) | Toy Story | `{kind: movie, tmdb: 862, imdb: tt0114709, tvdb: None}` | pass |
| Jellyfin, Plex | the show/series item itself | `{kind: unknown, all ids None}` | pass |
| Emby (both) | S01E01 / Toy Story | episode with no ids (lab Emby has no ProviderIds) / movie imdb tt0114709 | pass |
| Plex | S01E01 | `{kind: episode, tmdb: 60625, imdb: tt2861424, tvdb: 275274, season: 1, episode: 1}` | pass |
| Plex | Toy Story / Up | tmdb 862 / 14160, imdb ok, tvdb None | pass |
| Jellyfin 10.11 series fetch timing out; Plex empty show | S01E01 | `{kind: episode, ids None, season: 1, episode: 1}` (no fallback to episode ids) | pass |

Real library, read-only, 271,394 video files: TV episodes with ids 221,972 (4-digit seasons like `S2023Exx` now parse);
unknown 9,866 (9,638 extras + 228 date-named episodes left to the server). Movies with ids 19,102; the 16,627 that
became unknown are all `-trailer` files. No movie returns a tvdb id. All 4,911 TV show folders carry `{tvdb-…}` only.

## Task 7 — online sources live smoke (2026-09-14, storage egress, one request per source)

`pytest --no-cov -n 0 -s -m integration tests/markers/test_online_sources_live.py` — 3 passed, exactly 3 requests.
Item: Rick and Morty S01E01 (tmdb 60625, imdb tt2861424, tvdb 275274), file duration given to TheIntroDB.

| Source | HTTP | Candidates (ms) | Rate/usage headers |
|---|---|---|---|
| TheIntroDB (no key) | 200 | intro 127 894–156 824, credits 1 298 000–end | `x-ratelimit-limit 30`, `-remaining 29`, `-reset 10`; `x-usagelimit-limit 500`, `-remaining 173`, `-reset 0`; `x-usagelimit-specificmedia-limit 2000`, `-remaining 1997`, `-reset 0` |
| IntroDB.app | 200 | intro 128 000–160 000, credits 1 295 000–1 321 000 | none |
| SkipDB | 200 | intro 129 000–157 800 (conf 0.91), credits 1 296 000–1 320 000 (conf 0.9) | none |

Findings:
- TheIntroDB sends `x-usagelimit-reset: 0` mid-day, so trusting it as "seconds until reset" would refill the budget
  on every response. The limiter ends the daily budget at the UTC day change only
  (`test_live_theintrodb_headers_keep_the_budget_until_the_day_rolls` pins the captured header set).
- The keyless daily budget was already at 173/500 at 05:54 UTC on storage's IP; the pipeline shares that allowance.
- IntroDB.app and SkipDB send no rate headers; they are paced by the 0.5 s floor plus 429 handling.
- The three sources agree within 1.2 s on the intro start and 3 s on the credits start for this item.
- The TheIntroDB keyed limit (spec §13 item 9) is still unverified: no key available.
