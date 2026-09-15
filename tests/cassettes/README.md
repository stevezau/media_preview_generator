# Vendor-API Contract Cassettes

Recorded HTTP request/response pairs for Plex, Emby, and Jellyfin API
boundary functions. Captured once with [pytest-recording] / [vcrpy]
against a live server, replayed forever in CI.

## Why these exist

The production "Plex 500s when `type=` is omitted" bug shipped because the
unit test mocked `plex.fetchItems` and asserted on a substring of the URL
— it didn't catch the missing required parameter because the mock
returned items regardless. Cassettes capture the exact API contract
(URL shape, headers, status codes, response shape) so any future change
that strays from the recorded shape fails fast.

## Layout

```
tests/cassettes/
├── README.md                          # this file
├── test_servers_plex_vcr/             # one dir per test module
│   ├── test_resolve_one_path_movie.yaml
│   ├── test_resolve_one_path_episode.yaml
│   └── test_get_bundle_metadata.yaml
├── test_servers_emby_vcr/
│   └── …
└── test_servers_jellyfin_vcr/
    └── …
```

## Running

Replay (default, no live server needed):

```
pytest tests/test_servers_plex_vcr.py
```

Re-record (requires live server + creds in env):

```
PLEX_URL=https://plex.local:32400 \
PLEX_TOKEN=xxx \
pytest tests/test_servers_plex_vcr.py --record-mode=once
```

Other useful modes: `--record-mode=new_episodes` (record only missing
interactions, keep existing), `--record-mode=all` (overwrite everything).

## Scrubbing

Sensitive data is scrubbed at record time via the `vcr_config` fixture in
`tests/conftest.py`. Specifically:

- `X-Plex-Token`, `X-Emby-Token`, `Authorization`, `Cookie`, `Set-Cookie`
  headers → replaced with `FAKE_*` placeholders.
- `X-Plex-Token`, `api_key` query parameters → replaced.

If you add a new vendor or auth mechanism, **extend the `filter_headers`
and `filter_query_parameters` lists in `conftest.py` BEFORE recording**.
A leaked token in a committed cassette is a credentials disclosure.

## Don't commit cassettes that contain

- Real auth tokens (verify by `grep -E '(X-Plex-Token|X-Emby-Token):'
  tests/cassettes/**` — should only show `FAKE_*` strings).
- User-identifying paths if they reveal real media organisation. (Mostly
  fine for media metadata; concern is more around server identifiers.)

## Markers (lab)

`tests/test_servers_markers_vcr.py` pins Plex's marker read
(`includeMarkers=1`) and the Media Preview Bridge markers routes plus
Jellyfin's core `/MediaSegments`, recorded against the storage lab
(`mlab-plex`, `mlab-jellyfin`) rather than the Docker-Compose test stack.

Needs the phase-1 lab state in place first: plugins installed and the
synth chapters show published to both servers (see
`docs/design/intro-credits/evidence/lab/phase1-results.md`, run 2) —
`/media/synth-chapters/Synth Chapters (2021)/Season 01/Synth Chapters
(2021) - S01E01.webm` must be indexed on both.

Re-record:

```bash
cd /home/data/workspace/plex_generate_vid_previews
set -a; . docs/design/intro-credits/evidence/lab/env; set +a
PLEX_URL=http://127.0.0.1:32402 JELLYFIN_URL=http://127.0.0.1:18097 JELLYFIN_TOKEN="$JF_TOKEN" \
  /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_servers_markers_vcr.py --record-mode=once
grep -rlE "$PLEX_TOKEN|$JF_TOKEN" tests/cassettes/test_servers_markers_vcr/ && echo "TOKEN LEAK — delete and fix scrubbing" || echo "clean"
```

Expected: `4 passed`, `clean`.

`tests/test_servers_emby_markers_vcr.py` pins the Media Preview Bridge for Emby routes (Ping, the admin probe,
GET/POST/DELETE markers with `ReplaceOwn`), Emby's per-user item read (`Chapters`, `MediaSources`) and the read of a
grouped item's versions (`Chapters,MediaSources,AlternateMediaSources`, with an API key and per user; Emby groups
S01E01 with its "- Extended" cut), recorded against `mlab-emby` (Emby 4.10) with the plugin installed and nothing
stored on Synth Chapters S01E01, S01E01 - Extended and S01E02 (the test removes what it posts):

```bash
cd /home/data/workspace/plex_generate_vid_previews
set -a; . docs/design/intro-credits/evidence/lab/env; set +a
EMBY_URL=http://127.0.0.1:18096 EMBY_USER_ID="$EMBY_UID" \
  /home/data/.venv/bin/python -m pytest --no-cov -n 0 tests/test_servers_emby_markers_vcr.py --record-mode=once
grep -rlF -e "$EMBY_TOKEN" -e "$EMBY_UID" tests/cassettes/test_servers_emby_markers_vcr/ && echo "LEAK" || echo "clean"
```

Expected: `10 passed`, `clean`. The user id is recorded as `/Users/FAKE_USER_ID/` (`_scrub_request_uri`), which is what
the test sends on replay; the per-user route answers a single item, so the `Items`-list collapse above doesn't apply.
The API-key read answers an `Items` list whose item has no `Path` (it asks for other fields): the scrubber keeps it
when every one of its `MediaSources` paths is synthetic.

**Known gotcha — `PLEX_URL`/`PLEX_TOKEN` don't reach the test under pytest.**
`tests/conftest.py`'s session-scoped `_isolate_dotenv_from_tests` fixture
unconditionally pops `PLEX_URL`/`PLEX_TOKEN` from `os.environ` before any
test's fixtures run (it's cleaning up after `dotenv`, and doesn't check for
a `real_plex_server` opt-out the way the other autouse fixtures do). A
fixture that reads `os.environ.get("PLEX_URL", ...)` at test time — as
`plex_lab` in this file does — always sees the fallback, even when the
shell exported the real value, so `--record-mode=once` silently records
nothing for Plex (no cassette file, no error) instead of hitting the lab.
Jellyfin is unaffected — `JELLYFIN_URL`/`JELLYFIN_TOKEN` aren't in
`_ENV_MIGRATION_MAP`.

Workaround for the Plex leg only: temporarily comment out the pop (in
`_isolate_dotenv_from_tests`, exclude `"PLEX_URL"`/`"PLEX_TOKEN"` from
`keys_to_clean`), run the record command, then revert the conftest.py
edit before running anything else or committing — `git diff
tests/conftest.py` must be empty afterward. Don't commit a workaround
that weakens the leak guard for the rest of the suite.

**Cassette scrubbing needed a scrub-detector extension, not just a new
prefix.** The generic `_scrub_response_body` "synthetic test-stack data"
carve-out only recognised `/em-media/`, `/jf-media/`, `/media/Movies/Test
`/`/media/TV` — any other path (including `/media/synth-chapters/`) hit
the defensive fallback and collapsed `<Video>`/`<Directory>` elements and
JSON `Items` arrays to placeholders, silently destroying the very
`ratingKey` / segment data the cassette exists to pin (record-time
in-process assertions still passed against the real, unscrubbed response;
only the on-disk YAML was gutted). Fixed by adding
`/media/synth-chapters/` — with the same `(?:/|")` bare-root handling as
the existing `/media/Movies`/`/media/TV` entries — to both the XML
`xml_synthetic` regex and the JSON `_SYNTHETIC_PREFIXES` tuple, and by
adding a schema-based carve-out for Jellyfin's `/MediaSegments` `Items`
(`Id`/`ItemId`/`Type`/`StartTicks`/`EndTicks` — no path, title, or other
identifying field is even possible in that shape, so it's kept regardless
of the `Path`-prefix check). If a future marker cassette collapses to an
empty/placeholder body on replay while passing at record time, suspect
this same class of gap for whatever new path or response shape it uses.

## Re-recording when a vendor API changes

1. Delete the affected cassette file(s).
2. Run the test with `--record-mode=once` against a live server.
3. Verify the re-recorded YAML — should look similar in structure to the
   old one. Big differences (new headers, response-shape changes) are
   the API drift the cassette is designed to catch.
4. Commit the new cassette and the test changes together.

[pytest-recording]: https://pytest-recording.readthedocs.io/
[vcrpy]: https://vcrpy.readthedocs.io/
