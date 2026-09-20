# AniSkip — measured facts (phase 4, Task 1), 2026-09-20

Everything the phase-4 plan lists under "Facts that need checking against the real service", measured against the live
service and the owner's real library on 2026-09-20. No product code was changed and nothing was published. Each fact is
marked **measured** with the command that produced it, or **not established**.

**Short version.** The API works, it is anonymous, it has no terms of use at all, and it *does* check the file's cut —
but only to ±20 s and it breaks ties by votes, not by closeness. The API is not the problem. Three other things are:

1. **Nothing in this library carries a MAL id** — not the paths, not Plex. AniSkip cannot be called at all without a
   new id map, and the two maps that would supply it have no licence.
2. **AniSkip's numbering is per-MAL-entry**, which the library's season/episode numbers do not match for a third of
   the anime on disk — and a wrong choice does not fail, it returns another episode's times (§2 has a case where it
   calls the last 90 s of an episode the opening).
3. **It is not a coverage win and it is probably not independent.** On a random 120 resolved anime episodes it is the
   only source with an answer for **2 intros and 0 credits**, and *on intros* it matches IntroDB to the millisecond
   twice as often as TheIntroDB and IntroDB match each other.

The full decision is in §6.

---

## 1. The API

**Measured.** Base URL `https://api.aniskip.com`, OpenAPI 3.0.0, `info.version` `2.0.576`, served by nginx +
NestJS. The spec is public and was read, not guessed:

```bash
curl -A "<identifying UA>" https://api.aniskip.com/api-docs-json
```

### 1.1 The one endpoint we would call

```
GET /v2/skip-times/{animeId}/{episodeNumber}?types[]=<type>&episodeLength=<seconds>
```

| Part | Required | Meaning (quoting the spec's own `description`) |
|---|---|---|
| `animeId` (path) | yes, `>= 1` | "MAL id of the anime to get" — integer, **MyAnimeList id only** |
| `episodeNumber` (path) | yes, `>= 0`, `double` | "Episode number to get" — a *number*, so `.5` episodes exist |
| `types[]` (query) | **yes** | "Type of skip time to get"; enum `op`, `ed`, `mixed-op`, `mixed-ed`, `recap`. Repeatable |
| `episodeLength` (query) | **yes**, `>= 0`, `double` | "Approximate episode length to search for. **If the input is 0, it will return all episodes**" |

Both query parameters are **required** — omitting either is a 400, measured:

```bash
# no episodeLength
{"statusCode":400,"message":["episodeLength must not be less than 0",
 "episodeLength must be a number conforming to the specified constraints"],"error":"Bad Request"}
# no types[]
{"statusCode":400,"message":["each value in types must be one of the following values: op, ed, mixed-op, mixed-ed, recap",
 "types must be an array","each value in the array must be unique"],"error":"Bad Request"}
```

### 1.2 What identifies an episode

MAL id + episode number + `episodeLength`. There is no title, no season, no other id — and **no season parameter at
all**, which is the whole of §2 below.

### 1.3 Response shape (200)

```json
{"found":true,
 "results":[{"interval":{"startTime":310.571,"endTime":400.571},
             "skipType":"op",
             "skipId":"d4f34b6d-0547-4438-ac93-b25b577eddd5",
             "episodeLength":1443.984}],
 "message":"Successfully found skip times","statusCode":200}
```

- **Units: seconds, floating point**, for `startTime`, `endTime` and `episodeLength`. Not milliseconds. (Our store is
  integer ms — `round(x * 1000)`.)
- `endTime` is **never absent**: the schema marks `interval` as a plain object with both fields, and all 3,887
  segments received in the 3,166-episode sweep below carried both. There is no "runs to the end of the file"
  sentinel the way TheIntroDB's `credits.end_ms: null` is.
- `episodeLength` is **the length of the cut the submitter had**, not the length we asked for. It is the only field
  that tells us how close the match is (see §1.6).
- `skipId` is a UUID naming that one crowd submission — the handle for the vote endpoint. Worth storing in
  `meta_json`; it is the only stable identity an answer has.
- **There is no vote count, no confidence and no submitter in the response.** Votes exist server-side and order the
  results (below); the client cannot see them.
- At most **one segment per requested type** comes back: the service asks for the top 10 by votes per type and returns
  `skipTimes[0]` (`src/skip-times/skip-times.service.v2.ts`). So `types[]=op&types[]=ed` yields at most 2 results.

### 1.4 The segment types, and how they map to our `MarkerType`

| `skipType` | What it is | Our type |
|---|---|---|
| `op` | the opening theme | `INTRO` |
| `mixed-op` | an opening that runs over story ("mixed") | `INTRO` |
| `ed` | the ending theme | `CREDITS` |
| `mixed-ed` | an ending that runs over story | `CREDITS` |
| `recap` | "previously on" | `RECAP` |

**There is no preview type**, so AniSkip never feeds our `PREVIEW`. `MarkerType.PREVIEW` stays the business of
chapters and the other sources.

**"Ending" is our credits, not our preview — measured, not assumed.** Of the anime files that carry *both* a credits
chapter and a preview chapter of their own, AniSkip's `ed` start is nearer the credits chapter in **319 of 320**, within
5 s of it in **279**, and within 5 s of the preview chapter in **0**.

> **Re-measured 2026-09-21.** The numbers above were 236 of 236 / 211 / 0 when this page was written, because
> `analyze.py` asked `chapter_candidates()` without the episode kind and so never saw a bare `Ending` chapter as
> credits. Every file here is an anime episode, so the app itself has read them as credits since phase 4, Task 15.
> With the kind passed, 84 more files have both chapter kinds; one of them is nearer its preview chapter, and none of
> the 320 is within 5 s of it.

Two cautions for whoever writes the client:

- **`op` and `mixed-op` are different answers, not two names for one.** Where an episode has both, their starts differ
  by more than 5 s in **62 of the 65** cases in the sweep (`ed`/`mixed-ed`: 9 of 11). A client must pick one — `op`,
  falling back to `mixed-op` — and must never treat the pair as two sources agreeing.
- **An `ed` is the ending *song*, about 90 s, and it stops well before the end of the file** (the next-episode preview
  usually follows). It is not Plex's "credits run to the end of the file". That is the right thing to publish for
  anime, but it means an AniSkip credits marker and a Plex credits marker describe different spans on purpose.

### 1.5 Error shapes

| Case | Status | Body |
|---|---|---|
| Nothing matches (unknown MAL id, unknown episode, `episodeLength` too far off) | **404** | `{"found":false,"results":[],"message":"No skip times found","statusCode":404}` |
| Bad parameter | 400 | `{"statusCode":400,"message":[ …list of strings… ],"error":"Bad Request"}` |
| `animeId` 0 | 400 | `{"statusCode":400,"message":["animeId must not be less than 1"],"error":"Bad Request"}` |
| Unknown path | 404 | `{"statusCode":404,"message":"Cannot GET /v2/","error":"Not Found"}` |

**A 404 is the normal "no data" answer, not a failure.** A client that treats 404 as `unavailable` would retry for
ever; it must be read as `no_data`. Note the two different 404 bodies — the "no skip times" one always carries
`"found":false`, the routing one carries `"error":"Not Found"`. Telling them apart matters.

`v1` still answers (`/v1/skip-times/{id}/{ep}?types=op`) with snake_case (`start_time`, `skip_type`, `episode_length`)
and **no `episodeLength` filter at all** — so v1 answers about whatever cut it likes. Do not use v1.

### 1.6 Does it check the file's cut? — **yes, to ±20 s, ranked by votes**

This is the §5.5 rule 6 question, and it is the one fact that is both measured *and* confirmed in the service's own
source. The query is:

```sql
-- src/repositories/skip-times.repository.ts, findSkipTimes()
WHERE anime_id = $1 AND episode_number = $2 AND skip_type = $3
  AND votes > -2
  AND ($4::real = 0 OR ABS(episode_length - $4::real) <= 20)
ORDER BY votes DESC LIMIT 10
```

Measured end to end, asking MAL 21 episode 1 for `op` at falling `episodeLength` (a stored cut is 1443.984 s):

| `episodeLength` | HTTP | `startTime` | matched `episodeLength` |
|---|---|---|---|
| 1443 … 1425 | 200 | 310.571 | 1443.984 |
| 1420 | 200 | 22.313 | 1432.1 |
| 1410, 1400 | 200 | 98.073 | 1418.061 |
| 600 | **404** | — | — |

At 1425 the answer is 19.0 s away while a 7.1 s-away entry existed — because the further one has more votes. So:

- **It rejects a clearly different cut** (600 s → 404). That is a real file-cut check, unlike IntroDB and TheIntroDB,
  which answer whatever length you send (spec §4, §5.5 rule 6).
- **But ±20 s is loose and it does not prefer the closest match.** SkipDB labels its matches `exact` (≤ 2 s) /
  `shifted` (≤ 15 s) and we only accept those two. AniSkip gives no label — but `results[].episodeLength` is in the
  response, so **the client can compute `|file duration − entry episodeLength|` and enforce its own tighter band**.
  That is the recommendation: apply our own ≤ 2 s band and treat 2–20 s as "another cut, agreement only".
- Different types on the same request can come back from **different cuts** — one request for MAL 21 ep 1 at
  `episodeLength=1440` returned an `op` from a 1443.984 s cut and an `ed` from a 1434.985 s cut. Each type is matched
  independently.
- `episodeLength=0` **disables the check entirely** and returns an arbitrary cut. The client must never send 0 and must
  refuse to look up a file with no known duration.

### 1.7 Rate limits and 429

**Measured, with a caveat.** The GET route is annotated `@Throttle(120, 60)` — "Maximum 120 times in 1 minute" — per
IP (`src/skip-times/skip-times.controller.v2.ts`). Every response carries:

```
x-ratelimit-limit: 120
x-ratelimit-remaining: 119
x-ratelimit-reset: 1
```

**These headers do not work.** 20 GETs back to back over one keep-alive connection all reported
`remaining: 119, reset: 1` — the counter never moves:

```bash
curl -A "<UA>" $(for i in $(seq 1 20); do printf -- '--next -sS -o /dev/null -D - "%s" ' "$URL"; done)
```

**And the limit itself was not enforced.** One controlled boundary test — the only time anything here went over the
documented rate — sent **140 requests in 39.2 s (214/min)** and got **140 × HTTP 200, no 429, no `Retry-After`**, with
the headers still reading `119` and `1`. The test stopped there; it was not pushed further.

**This is not an exception to how the app paces — it is the ordinary case.** `sources/ratelimit.py:378` already
hard-codes a per-window floor for every source (`_MIN_INTERVALS = {"theintrodb": …, "introdb": 0.5, "skipdb": 0.5}`)
under the comment "Per-window floors only …; daily budgets always come from headers". Spec §4's "paces from headers"
is about the **daily budget**, and AniSkip publishes none. So AniSkip needs one more entry in that dict —
`"aniskip": 0.67` gives 90/min against the documented 120/min — and no daily-budget reader at all. Nothing new is
required. The 3,166-request sweep below ran just under that (0.70 s sleep, ~60/min measured) and was never throttled.

The vote and create routes are `@Throttle(4, 60*60)` and `@Throttle(10, 60*60*24)` — we call neither.

### 1.8 Auth

**Measured: none.** No key, no header, no account. `Authorization` is not in the OpenAPI spec and every call above
succeeded anonymously. There is nothing to store, mask or leak — unlike TheIntroDB.

### 1.9 Terms of use — **there are none**

**Measured (by looking for them).**

- `aniskip/aniskip-api` ships an **MIT LICENSE** (`Copyright (c) 2021 Lexes Jan Mantiquilla`) — that licenses *the
  server software*, which is also published as a Docker image for self-hosting. It says nothing about the hosted
  service or the data.
- `aniskip/aniskip-extension` is likewise MIT.
- The API README covers deployment only. There is **no terms-of-service page, no acceptable-use policy, no attribution
  requirement and no stated licence on the skip-time data** — on the repos, on `api.aniskip.com` (`/` and `/v2/` are
  bare 404s) or anywhere a search turns up.

**Compared with the TheIntroDB risk the owner already accepted (spec §13 item 1), this is a different shape, not a
smaller one.** TheIntroDB's terms say "solely for client-side, non-commercial, end-user applications" and prohibit
server calls — we knowingly breach a written rule. AniSkip has no written rule to breach: nothing forbids a server
calling it, and nothing permits it either. Practically that means no licence to rely on, no notice period, and the
service can add terms, require a key or block us at any time. **The owner should decide again**, with the same
mitigations the TheIntroDB decision used: ship it **off by default**, and make sure everything works with the source
disabled.

Two facts for that decision: the service is **self-hostable** (Docker image published) — but the skip-time **database
is not published**, so a self-hosted instance starts empty and is not a fallback. And there is no key to revoke,
because there is no key.

### 1.10 `relation-rules` — the API's own answer to the numbering problem

**Measured.** A second endpoint exists that the plan did not know about:

```
GET /v2/relation-rules/{animeId}
```

```bash
curl ".../v2/relation-rules/50265"
{"statusCode":200,"message":"Successfully found rules for animeId '50265'","found":true,
 "rules":[{"from":{"start":13,"end":25},"to":{"malId":50602,"start":1,"end":13}}]}
```

That reads: "episodes 13–25 asked against MAL 50265 are really episodes 1–13 of MAL 50602". It is AniSkip's own
redirection table for split cours. MAL 35760 returns three rules, including `38–49 → 35760 1–12`, i.e. it also handles
*absolute* numbers asked against a season entry. A MAL id with no rules answers 404 with `"found":false`.

This is useful and free, but it **does not solve §2**: it is keyed by MAL id, so you already need the right MAL id
before you can ask.

---

## 2. Episode numbering — per-MAL-entry, and the library does not match it

**Measured.** AniSkip's numbering is **neither absolute nor seasonal**: it is *per MAL entry*, and MAL splits a show
into a separate entry per cour or per season. There is no season parameter, so the season has to be resolved into the
MAL id before the call.

The owner's library numbers files the TVDB way (`SxxEyy`, from Sonarr). Those two disagree constantly.

### A real example from the owner's library

**Attack on Titan (2013), TVDB season 3.** TVDB has 22 episodes in season 3; MAL has two entries — 35760 (season 3
part 1, 12 episodes) and 38524 (season 3 part 2, 10 episodes). The file `S03E13` is episode **1** of MAL 38524. Asking
the way the season number suggests — MAL 35760 (the season's "the" MAL id), episode 13 — is asking for an episode that
entry does not have, and AniSkip **still answers**, because the crowd also submitted absolute-numbered rows:

| File | Correct: MAL 38524 ep 1 | Naive: MAL 35760 ep 13 |
|---|---|---|
| S03E13 | op 111.3 → 204.8, ed 1330.2 → 1420.2 | *no op*, ed 1348 → 1436 |
| S03E14 | op 0 → 92.7, ed 1330.3 → 1420.3 | **404** |
| S03E16 | op 130.8 → 222.6, ed 1330.2 → 1420.5 | *no op*, ed 1348 → 1436 |

**SPY x FAMILY (2022) season 1** is the one that shows how bad a wrong choice gets. TVDB season 1 is 25 files; MAL
50265 is part 1 (12 episodes) and MAL 50602 is part 2. For `S01E16`:

| | intro (`op`) | credits (`ed`) |
|---|---|---|
| Correct — MAL 50602 ep 4 | 107.7 → 197.7 | 1350.1 → 1440.1 |
| Naive — MAL 50265 ep 16 | **1352.7 → 1442.7** | 1352.7 → 1450.1 |

The naive lookup calls **the last 90 seconds of a 24-minute episode an opening**. Published, that is a skip-intro
button that jumps the viewer to the end credits, and an intro marker sitting on top of the credits. This is exactly the
failure the phase-4 plan says the feature must never have — and note that it is **not** caught by the sanity checks
alone (the times are inside the file and 90 s long; only the "intro starts in the first 35%" check catches this one,
and the S03E13 case above passes every check while being another episode's ending).

Seventeen (show, TVDB season) pairs in the owner's library have a TVDB season that spans more than one MAL entry:
86 Eighty Six S1, Attack on Titan S3 and S4, Dr. STONE S3 and S4, Fire Force S3, Food Wars! S3, Initial D S1,
Mobile Suit Gundam The Witch from Mercury S1, Mushoku Tensei S1 and S2, Pokémon Concierge S1, Re:ZERO S2,
SAKAMOTO DAYS S1, SPY x FAMILY S1, That Time I Got Reincarnated as a Slime S2, The Ancient Magus' Bride S2.
Fifty of the library's anime shows map to more than one MAL entry (My Hero Academia: 8; Attack on Titan, Initial D and
Dr. STONE: 7 each).

### The other shape: absolute numbering

Three shows are marked `defaulttvdbseason="a"` by the AniDB↔TVDB list — the show is one MAL/AniDB entry numbered
absolutely, while TVDB (and the owner's files) split it into seasons:

| Show | Files | TVDB seasons | What AniSkip wants |
|---|---|---|---|
| One Piece (1999) | 1,177 | 23 (S21 alone has 194) | MAL 21, absolute episode 1…1,100+ |
| Dragon Ball Z (1989) | 291 | 9 | one entry, absolute |
| Dragon Ball Super (2015) | 131 | 5 | one entry, absolute |

`S21E194` is **not** MAL 21 episode 194 — the 20 earlier seasons hold 890 files, so it is absolute episode 1,084.
Getting this wrong publishes the opening of an episode **890 episodes away**. MAL 21 has no `relation-rules`, so the
API cannot help here either.

The owner's files *sometimes* carry the absolute number — Sonarr's anime naming puts it after the `SxxEyy` token
(`… - S01E02 - 002 - <title> …`). **333 of One Piece's 1,177 files** have it; **0 of 422** Dragon Ball files do. So the
path gives the absolute number for 333 of the 1,599 absolute-numbered files (21%), and the rest would need a TVDB
episode list (i.e. a TVDB API key) to compute it.

**Specials (`S00`) resolve to nothing** and should simply be skipped — though this library happens to have none
in its anime folders (§4).

---

## 3. Where a MAL id could come from

### What the app has today

**Measured.** `MediaIds` carries tmdb/imdb/tvdb only (`markers/models.py`), `external_ids.py`'s token regex is
`[\{\[](tmdb|tvdb|imdb)(?:id)?[-=]((?:tt)?\d+)[\}\]]`, and `servers/_embyish.py:777` restricts episodes to
`("tmdb", "imdb", "tvdb")` from Jellyfin/Emby `ProviderIds` — so even a server that knows an AniDB id has it dropped.

**From the path: zero.** Over all 114,632 TV episode parts in the read-only prod dump, the id tokens on the show
folder are `tvdb` for **114,632** parts and nothing else; a search of the full path for `{anidb-…}`, `{mal-…}`,
`{myanimelist-…}` or `{anilist-…}` finds **0 files**.

**From Plex: zero.** The phase-1 coverage sample's `guids` column (200 random TV episodes) holds exactly `tmdb`,
`imdb` and `tvdb` — 200/200 for each. Plex's TV agent stores no anime id. (The prod Plex server was not queried for
this task; the numbers come from the phase-1/phase-2 read-only dumps.)

**From Jellyfin/Emby: not established.** The owner has no Jellyfin or Emby holding this library, and the lab servers
hold synthetic media, so there was nothing to measure. What can be said: Jellyfin/Emby only report `AniDB`/`AniList`
`ProviderIds` when a Shoko or AniDB metadata plugin is installed, and the app currently drops those keys anyway.

So **Q2b's recommended default — "path tokens plus whatever the server already reports, nothing new" — yields a MAL id
for 0 of the owner's 4,651 anime episodes.** Without a mapping, AniSkip cannot be called at all.

### The mapping sources

| Source | What it gives | Licence | Cadence | Size |
|---|---|---|---|---|
| **`Anime-Lists/anime-lists`** (`anime-list-master.xml`) | anidb ↔ tvdb with `defaulttvdbseason` + `episodeoffset` — the season/offset table, i.e. the only source that answers §2 | **none — no LICENSE file in the repo** | Automated "Generate lists" commits plus contributor PRs, several a week (latest 2026-09-17) | 3.4 MB XML |
| **`Fribb/anime-lists`** (`anime-list-full.json`) | anidb ↔ **mal** ↔ tvdb ↔ tmdb ↔ imdb ↔ anilist, 39,304 entries | **none — no LICENSE file in the repo**. Derived from `cedya77/anime-offline-database` (a fork made after manami-project archived, per its README, 2026-09-09) + `Anime-Lists/anime-lists` | Weekly, "automated list update" (latest 2026-09-15) | 7.1 MB JSON (1.2 MB for `anime-lists-reduced.json`) |
| `manami-project/anime-offline-database` | the id spine Fribb reduces | **ODbL 1.0 + DbCL 1.0** (clean) | archived upstream; the live fork is `cedya77/…` | ~6 MB |
| AniSkip `/v2/relation-rules/{malId}` | cour splits and absolute-vs-entry ranges **from a MAL id we already have** | same as the API: none | live | one request |
| A TVDB API key | absolute episode numbers for `defaulttvdbseason="a"` shows | commercial terms, per-user key | live | — |

**What each would cost us:**

- **Ship a reduced map in the image.** Built and measured: a `tvdb → [(defaulttvdbseason, episodeoffset, mal_id), …]`
  index covering 4,318 TVDB ids and 7,305 rules is **142 KB raw, 38 KB gzipped**. No runtime dependency, no network,
  works offline. Costs: a build step that pulls two unlicensed repos, a map that goes stale between releases (new shows
  and new cours land weekly), and an image-size line the owner has to approve — 142 KB, which is noise beside the
  image.
- **Fetch it at runtime.** Always current, nothing in the image. Costs: a new network dependency on a personal GitHub
  repo, a multi-MB download to cache and refresh, a new failure mode, and the same licence question.
- **Neither.** AniSkip stays off. Costs: nothing; anime keeps whatever coverage chapters, season audio and the other
  online sources already give it.

### Recommendation

**Ship a reduced map in the image, built from `Anime-Lists/anime-lists` + `Fribb/anime-lists`, refreshed per release —
but only after the owner clears the licence question, because neither repo has a licence.** The reasons:

1. It is the only option that answers §2. The season/offset table is what turns `S03E13` into "MAL 38524 episode 1".
   Without it the feature cannot be built safely at all, and with it the *wrong-marker* risk of §2 disappears for the
   seasonal shows.
2. It is data, not code — no new Python dependency, no import, no CVE surface. A generated JSON file the build pulls.
3. Staleness is bounded and visible: a show missing from the map resolves to nothing and is skipped with a reason,
   which is the safe direction. It never produces a *wrong* id, only a missing one.
4. Runtime fetching buys currency we do not need — a marker for a show that aired last week is worth far less than not
   shipping another episode's intro.

Add AniSkip's own `relation-rules` on top (one cached request per MAL id) so a cour split that landed after the map was
built still resolves. Do **not** take a TVDB key for the absolute-numbered shows: that is three shows, it needs a
per-user credential, and the safe answer for them is to skip.

**If the owner will not accept an unlicensed data file in the image, the honest answer is that AniSkip does not ship.**

---

## 4. Coverage on the owner's real library (read-only, nothing published)

### How the anime was found — the way the app would

The app has no idea what "anime" is, and **the owner's Plex has no anime library**: the TV section holds every show.
So anime was found by id, not by folder:

1. The phase-2 read-only dump of prod Plex's parts (`prod_plex_parts.json`, 125,956 parts) gives every file's path,
   duration and library section. Section 2 is TV: **114,632 episode parts in 4,911 show folders**.
2. The show's id comes from the `{tvdb-…}` token on its folder and the episode's from `SxxEyy` in the file name —
   exactly what `external_ids.py` parses. 114,519 parts yield both.
3. A show counts as anime when its TVDB id appears in the AniDB↔TVDB lists: **112 show folders, 4,651 episode files**
   (4.1% of the TV library).

**Cross-checked against Sonarr**, which tags anime itself (`seriesType: "anime"`): 109 series, 4,435 episode files.
The 112 folders carry 112 distinct TVDB ids (no folder shares one). **101 ids are in both lists; 8 are Sonarr-only and
11 are folder-only.** The two methods agree to within about 5%, and the app cannot use Sonarr's answer anyway, so the
id-based number is the one used below.

### How many resolve to a MAL id

| | files | of the anime |
|---|---:|---:|
| anime episode files | 4,651 | 100% |
| **resolved to a MAL id + episode number** | **3,166** | **68.1%** |
|  · via the AniDB↔TVDB season/offset table | 2,833 | 60.9% |
|  · via the absolute number in the file name (`… - SxxEyy - NNN - …`) | 333 | 7.2% |
| not resolved — absolute-numbered show, no absolute number in the name | 1,266 | 27.2% |
| not resolved — the AniDB list has no rule for that TVDB season | 211 | 4.5% |
| not resolved — the AniDB id has no MAL id | 8 | 0.2% |

The 211 with no season rule are two shows: Teenage Mutant Ninja Turtles (1987) — 193 files, in the AniDB lists but not
anime anyone would call anime — and Star Wars Visions (2021), 18 files, an anthology TVDB and AniDB split differently.
**The library has no anime specials at all**: 0 anime files sit in TVDB season 0, so the `S00` case never arises here
(a client still has to handle it).

### What AniSkip returns for them

One request per episode, all five types, 0.70 s sleep plus a ~0.3 s round trip — **about 60 requests/minute
measured**, against the ≤ 90/min pace §1.7 recommends. **3,166 requests, no 429, no network error**; every answer was
200 or 404.

| | episodes | of the 3,166 asked | of all 4,651 anime files |
|---|---:|---:|---:|
| HTTP 200 with at least one segment | 2,225 | 70.3% | 47.8% |
| HTTP 404 "No skip times found" | 941 | 29.7% | — |
| an intro (`op` or `mixed-op`) | 2,049 | 64.7% | 44.1% |
| credits (`ed` or `mixed-ed`) | 1,626 | 51.4% | 35.0% |
| a recap | 136 | 4.3% | 2.9% |

3,887 segments came back. **2,721 (70%) matched a stored cut within 2 s of the file's own duration**; the rest matched
a cut 2–20 s away, which is the §1.6 band the client should tighten.

**87 of the 3,887 segments (2.2%) fail the app's own sanity checks** (spec §5.5 rule 2) — 54 end past the end of the
file, 15 are intros or recaps starting after the first 35%, 18 are credits starting before the last 25%. The checks
catch them, but they show the database carries real rubbish: a "recap" at 1256 s in a 1432 s episode, an `ed` at 113 s.

### Against the app's other sources, on the same files

A random 120 of the 3,166 resolved anime episodes, each asked of the app's **own** clients (`theintrodb`, `introdb`,
`skipdb`), its own `chapter_candidates()`, and Plex's own markers from the read-only dump. Where a source returns
several segments of a type, the earliest is taken — checked, and no source did so on this sample.

**Every table in this section comes from that one run**, re-derived together so the coverage, the overlap and the
"what does it add" numbers cannot disagree with each other.

| Source | has an intro | has credits |
|---|---:|---:|
| IntroDB.app | **94 (78%)** | **77 (64%)** |
| Plex's own markers | 55 (46%) | 71 (59%) |
| TheIntroDB (no key) | 80 (67%) | 47 (39%) |
| **AniSkip** | 78 (65%) | 52 (43%) |
| Chapters in the file | 40 (33%) | 27 (22%) |
| SkipDB | 6 (5%) | 7 (6%) |
| any source at all | 110 (92%) | 115 (96%) |

**TheIntroDB's row has a shelf life.** Without a key its budget is 500 lookups a day (spec §4), and a second 120-episode
run the same day comes back almost entirely `unavailable`. The row above is the first run of the day; a re-run needs a
fresh day or the owner's key.

> **The chapter rows here and in the next table are out of date and were not re-run (2026-09-21).** They were
> produced by `compare.py` asking
> `chapter_candidates()` without the episode kind, so a bare `Ending` chapter was not credits to it, though it has
> been credits to the app since phase 4, Task 15. `compare.py` has been fixed, but re-running it spends about 120
> lookups of TheIntroDB's anonymous daily allowance, so it was not re-run for this correction and **the five other
> rows are the 2026-09-20 run's**. What *is* measured, offline on the same 120 files with today's classifier: passing
> the kind adds a credits chapter to **8 more of the 120** and moves no intro. Neither "any source at all" nor the
> "only source" counts moves with it — every one of the 8 already has a credits answer from another source — so §6's
> decision reads the same. The published 40 / 27 cannot be reproduced offline (today's classifier reads 37 / 21 on
> the same files without the kind, 37 / 29 with it), which is why the rows are flagged rather than rewritten:
> something other than Task 15 has moved the chapter classifier since, and finding it is not this correction's
> business. The two figures that decide anything here — "AniSkip is the only source with an answer for 2 intros and
> 0 credits" and "4 intros and 10 credits against the three online sources" — are unmoved either way.

**The sobering result: AniSkip is the only source with an answer for 2 of 120 intros and 0 of 120 credits.** Against
the three online sources alone it is the only answer for 4 intros (3%) and 10 credits (8%). On this library AniSkip is
not a coverage win — IntroDB already answers more anime episodes than it does. Its value would be as a source that
*agrees*, turning a single answer into a published one.

### Is it independent? (spec §5.5 rule 8, the ≤ 44 ms test)

Comparing the edge each rule compares — intro **end**, credits **start** — on the episodes both sources answer:

| Pair | kind | both answer | identical ≤ 44 ms | ≤ 1 s | ≤ 5 s |
|---|---|---:|---:|---:|---:|
| **AniSkip vs IntroDB** | intro | 68 | **14 (21%)** | 36 | 51 |
| **AniSkip vs IntroDB** | credits | 38 | 6 (16%) | 23 | 29 |
| AniSkip vs TheIntroDB | intro | 60 | 3 (5%) | 22 | 42 |
| AniSkip vs TheIntroDB | credits | 24 | 2 (8%) | 11 | 18 |
| *TheIntroDB vs IntroDB* (already counted as one source) | intro | 72 | 7 (10%) | 35 | 52 |
| *TheIntroDB vs IntroDB* (already counted as one source) | credits | 33 | 11 (33%) | 29 | 30 |
| AniSkip vs chapters | intro | 28 | 6 (21%) | 12 | 21 |
| AniSkip vs Plex's own | intro | 39 | 0 | 2 | 27 |
| AniSkip vs Plex's own | credits | 33 | 0 | 1 | 12 |

(All rows come from the same random 120 episodes, so they are comparable.)

**On intros AniSkip matches IntroDB to the millisecond twice as often as TheIntroDB and IntroDB match each other** —
and those two are already counted as one source. It is not the chapters doing it: of the 14 millisecond-identical
AniSkip/IntroDB intro pairs, **10 are on files with no intro chapter at all**. Plex's own detection, by contrast, never
matches AniSkip to the millisecond in 72 comparisons — which is what a genuinely independent source looks like.

**So spec §5.5 rule 8's interim ruling is right and should be made permanent: AniSkip joins the IntroDB + TheIntroDB
group.** Which removes most of the remaining value — on these files AniSkip mostly agrees with a source it is not
allowed to agree *with*.

### How well does it do when it does answer?

Scored against the files' own OP/ED chapters (§5), over all 3,166 asked — intro judged on its **end**, credits on
their **start**:

| | n (files with that chapter) | useful ≤ 5 s | off 5–15 s | wrong > 15 s | no answer |
|---|---:|---:|---:|---:|---:|
| intro | 1,080 | 610 (56%) | 93 | **55 (5.1%)** | 322 |
| intro, cut matched within 2 s | 580 | 499 (86%) | 51 | **30 (5.2%)** | — |
| credits | 1,087 | 510 (47%) | 52 | **34 (3.1%)** | 491 |
| credits, cut matched within 2 s | 487 | 441 (91%) | 23 | **23 (4.7%)** | — |

Median |delta| is 1.1 s for intros and 1.0 s for credits — when it is right it is very right.

> **The two credits rows were re-measured on 2026-09-21** and are the only rows on this page that moved. They read
> 849 / 383 (45%) / 39 / 25 (2.9%) / 402 and 367 / 333 (91%) / 18 / 16 (4.4%) when the page was written, because
> `analyze.py` asked `chapter_candidates()` without the episode kind: a bare `Ending` chapter was not credits to it,
> though it has been credits to the app since phase 4, Task 15, and every file scored here is an anime episode. The
> 238 `Ending`/`End` files join the credits truth. The intro rows are untouched — the kind decides no intro name —
> and the wrong rate the gate reads goes **up**, 2.9% → 3.1%, so nothing below changes direction.

**But the wrong rate is the number that matters, and it does not pass the Q4 gate shape** (≤ 2% of files wrong at
Medium, ≤ 1% at High). 5.1% of anime intros with a chapter get an answer more than 15 s from that chapter, and
tightening the cut band to ≤ 2 s does **not** help (5.2%); credits fail both caps too, at 3.1%. Some of that is
chapter-truth noise — 581 of the 1,080 intro chapters are a lone generic "Intro" which may be the cold open (§5) —
but restricting to files with a *specific* opening chapter still leaves 23 wrong of 408 answered (5.6%).

**Every one of these numbers is a lower bound on the risk**, because they are measured *after* the mapping resolved the
right MAL id. §2 shows what the same source returns when it does not.

---
## 5. The truth question — what would we score AniSkip against, and what does that cost?

**Score it against the files' own OP/ED chapters, with frame checks for the disagreements and for a sample of the
files that have none.** Chapters are what phases 1–3 used (`online/build_cases.py`: "chapters", "3-source agreement",
a few visual confirms), and anime is the best-chaptered population in the library.

**Measured, over the 3,166 resolved anime files, read-only, with the app's own `chapter_candidates()` — not an ad-hoc
regex:**

| | files |
|---|---:|
| asked for (the resolved set) | 3,166 |
| **read successfully** | **3,117** |
| gone from disk since the parts dump was taken | 49 |
| no chapters at all | 1,270 (41% of those read) |
| an intro chapter the app would accept | 1,080 |
| a credits chapter the app would accept | 1,087 (849 before Task 15 — see the side finding below) |
| both | 950 |
| a recap chapter | 40 |
| a preview chapter | 549 |

That is already **25× the 43-case online set**, for the cost of one ffprobe pass: 3,166 files in about 25 minutes at
`nice -n 19` with 6 threads, reading container headers only.

**Two caveats the gate has to respect.**

1. **581 of the 1,080 intro chapters are a lone generic "Intro"** — no specific opening chapter ("OP", "Opening",
   "Title Sequence") anywhere in the file, so spec §5.1's cold-open rule cannot fire and the app takes the "Intro"
   chapter at face value. On anime that chapter is often the cold open, not the theme. Chapter conventions are uniform
   within a show, so this is checkable per show (112 shows), not per file.
2. **Chapters are also one of the app's sources.** A chapter-scored gate measures "does AniSkip agree with the
   chapter", which is the right question for precision but says nothing about the 41% of anime files with no chapter —
   exactly the files AniSkip would be *for*. Those need frame checks.

**A side finding worth the owner's attention, now measured — and since acted on.** `Ending` is the third most common
credits-ish chapter name in this library's anime (230 chapters, after `End Credits` 337 and `Credits` 275). **On anime
that chapter is the ED.** 238 anime files carry an `Ending`/`End` chapter; on the 149 where AniSkip also has an `ed`,
its start is within 5 s of the chapter in **127 (85%)**, within 1 s in 78, and more than 15 s away in 9 — median
|delta| **1.0 s**.

That is 238 files of free credits coverage, and it needed an episode-only scope, because the rule is global and a
film's "Ending" is its last scene.

> **Shipped 2026-09-20 (phase 4, Task 15), so this page's "the app's classifier deliberately does not treat it as
> credits" is out of date.** `chapters.py` now reads a bare `Ending` as credits **on an episode**, judged by the kind
> the file's own path gives (`ids_from_path`); `End` alone is still a scene name everywhere. What the page quoted —
> "'End'/'Ending' alone are common final-scene names in movies, so they are deliberately not credits" — is not in the
> file any more. The measurement behind Task 15 is `evidence/eval/phase4-chapters.md`: 282 of 4,346 anime episodes
> carry a bare `Ending`, against 1 of 9,904 movies.
>
> Every credits number on this page was re-derived on 2026-09-21 with the kind passed, because `analyze.py` and
> `compare.py` were still asking `chapter_candidates()` without it. §1.4 and §4 carry the new rows; the "credits
> chapter the app would accept" count above is 1,087, not 849. **The §6 decision does not move**: chapters getting
> better makes AniSkip worth less, and AniSkip's own credits wrong rate against the wider truth is 3.1%, still over
> both Q4 caps.

**Cost to build the truth set:**

| Step | Machine | Review |
|---|---|---|
| Chapter truth for 3,166 files | **done** — the ffprobe pass above, cached | — |
| Confirm the "Intro" convention per show (112 shows, one contact sheet each) | ~30 min | ~30 min |
| Frame-check a 60–80 file sample of the no-chapter population, phase-1 style | ~30 min | 1–2 h |
| Frame-check every AniSkip-vs-chapter disagreement | ~15 s/file (phase-3 harness rate) | scales with the count |

**Total: about one hour of machine time and two to four hours of review** for a truth set of roughly 1,100 anime
intros and 1,100 credits (850 before Task 15) — far past the "aim for the scale of the existing online set (43
cases) or better" the plan asks for. None of it needs the `plex` host and none of it writes to the library.

---

## 6. What this means for phase 4 — and what would block shipping

**Nothing here blocks Task 7 technically.** The client is small and every shape it needs is measured: one GET, seconds
to ms, five segment types, 404 = `no_data`, a self-paced ≤ 90/min against a documented 120/min, no key. What blocks
it is the value and the dependency:

| Blocker | Severity | What it costs to clear |
|---|---|---|
| **No MAL id anywhere in the library.** Q2b's recommended default ("path tokens plus whatever the server reports") yields **0 of 4,651** anime episodes. | **Blocks the feature entirely** | The owner accepting a ~142 KB unlicensed id map in the image (§3). There is no other option that works. |
| **Neither id-map repo has a licence.** | **Owner decision** | An owner ruling, like the TheIntroDB one in spec §13 item 1. |
| **AniSkip has no terms of use of any kind** — MIT on the code, nothing on the service or the data. | **Owner decision** | A second ruling. Mitigations are the same as TheIntroDB's: off by default, degrade cleanly. |
| **It is not independent of IntroDB** (§4). | Design | None — keep spec §5.5 rule 8's interim ruling and make it permanent. But it is what removes most of the remaining value. |
| **It fails the Q4 gate shape on the chapter truth**: 5.1% of anime intros and 3.1% of anime credits with a chapter get an answer more than 15 s away, against caps of 2% (Medium) and 1% (High) — and tightening the cut band to ≤ 2 s does not help. | **Blocks publishing** | Task 8's gate would have to reject it at both settings on this evidence. It could still be useful as an *agreeing* source that never decides. |
| **1,599 files (34% of the anime) are absolute-numbered** and only 333 carry the absolute number in the file name. | Scope | Skip them, and say so. A TVDB key is not worth three shows. |

**If the owner wants one sentence: AniSkip is buildable, but on this library it would add a marker to about 2% of
anime episodes that nothing else covers, at the price of a new unlicensed data file in the image and a source whose
answers are wrong more than 5% of the time.** The recommendation is to put that choice in front of the owner (Q2/Q2b)
before Task 7 starts, rather than build it and let Task 8's gate reject it.

Two things worth doing regardless of the AniSkip decision, both found on the way here:

- ~~The app's chapter classifier does not treat `Ending` as credits.~~ **Done, 2026-09-20 (phase 4, Task 15):** a
  bare `Ending` is credits on an episode, judged by the kind the file's path gives. Measured here first: on anime it
  is the ED (§5) — 238 files, AniSkip's `ed` within 5 s of it in 85%.
- 581 of 1,080 anime intro chapters are a lone generic `Intro` with no specific opening chapter in the file, so spec
  §5.1's cold-open rule cannot fire and the app takes them at face value. Worth a look before anything publishes from
  an anime intro chapter.

---

## 7. What was NOT established

- **The 429 body could not be provoked.** 140 requests in 39.2 s (§1.7) all answered 200. The shape a 429 would take
  is NestJS's `ThrottlerGuard` default — `429 {"statusCode":429,"message":"ThrottlerException: Too Many Requests"}` —
  which is **from the library, not measured here**. `Retry-After` was never seen on any response. A client still needs
  429 handling; it just cannot be tested against the live service today.
- **Whether `x-ratelimit-remaining` ever decrements.** It did not in any request made for this page.
  Whether that is a broken Redis throttle store or a load balancer in front is not knowable from outside. (~3,300
  requests in total: the 3,166 sweep, the 140-request boundary test and the header probes.)
- **What Jellyfin's and Emby's `ProviderIds` report for this library.** The owner has no Jellyfin or Emby holding it,
  and the lab servers hold synthetic media.
- **Whether the prod Plex has any anime id beyond tmdb/imdb/tvdb.** The prod Plex database was not queried for this
  task; the guid evidence is the phase-1 200-episode sample dump.
- **The skip-time data's licence.** There isn't one published. This is an absence, not a permission.

## 8. How to reproduce

**The scripts are committed** in `docs/design/intro-credits/evidence/online/phase4/` and produced every number on
this page. Their data — the id maps, the 3,166-row sweep, the chapter probe, the per-file comparison — is ignored by
the repo (`evidence/**/*.json`, `evidence/**/*.jsonl`, plus the one `.xml` map) because it carries real library paths.
This page carries only counts, and titles rather than paths.

```bash
E=docs/design/intro-credits/evidence/online/phase4
PY=python                                        # a Python that can import media_preview_generator
bash           $E/fetch_maps.sh                  # the two anime id maps
nice -n 19 $PY $E/resolve.py                     # library -> anime -> MAL id + episode number
nice -n 19 $PY $E/sweep.py                       # ask AniSkip for each (paced; stops on 429)
nice -n 19 $PY $E/chapters.py                    # ffprobe chapters for the same files (read-only)
nice -n 19 $PY $E/analyze.py                     # §1.4, §4 and §5 in one run
CONFIG_DIR=$(mktemp -d) nice -n 19 $PY $E/compare.py 120   # the other sources on a random sample
nice -n 19 $PY $E/sonarr_anime.py                # the anime cross-check
nice -n 19 $PY $E/limit_test.py                  # the one rate-limit boundary test
```

`$E/README.md` has the prerequisites: the Sonarr export, and `MARKERS_PARTS_DUMP` / `MARKERS_PLEX_DUMP` when running
from a worktree, since the phase-2 dumps are local-only and live in the main checkout.

Everything runs on `storage`, with no `plex` host and nothing written under `/data*`. `chapters.py` is the only
thing that opens a library file, and `ffprobe -show_chapters` reads the container header only.

**One exception to "read-only":** `compare.py` calls the app's real source clients, so it spends about 120 lookups of
TheIntroDB's anonymous daily allowance and records source usage in a `markers.db`. Give it a throwaway `CONFIG_DIR`
as above so it never writes to the app's own database.

The two facts taken from the service's own source (MIT) rather than measured over the wire:

```
src/skip-times/skip-times.controller.v2.ts   @Throttle(120, 60)
src/repositories/skip-times.repository.ts    ABS(episode_length - $4) <= 20, ORDER BY votes DESC LIMIT 10
```

And the three one-off calls behind §1 and §1.10:

```bash
UA="MediaPreviewGenerator-research/0.1 (intro-credits source evaluation)"
curl -A "$UA" 'https://api.aniskip.com/v2/skip-times/21/1?types[]=op&types[]=ed&episodeLength=1440'
curl -A "$UA"  https://api.aniskip.com/api-docs-json            # the OpenAPI spec
curl -A "$UA"  https://api.aniskip.com/v2/relation-rules/50265  # the cour-split table
```

---

## 9. Where this file lives

The phase-4 plan's Task 1 names `evidence/online/phase4-aniskip.md`. This page was written to
`evidence/eval/aniskip-facts.md` instead, on the owner's instruction. **Tasks 7 and 8 should read this file** wherever
the plan says `phase4-aniskip.md`; the scripts are where the plan expects them, in `evidence/online/phase4/`.
