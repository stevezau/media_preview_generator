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

One directory per test module, one file per test, named `Class.test_name.yaml`
(parametrized tests add `[param]`):

```
tests/cassettes/
├── test_servers_plex_vcr/
├── test_servers_emby_vcr/
├── test_servers_jellyfin_vcr/
├── test_servers_markers_vcr/          # Plex marker read, Jellyfin bridge markers
└── test_servers_emby_markers_vcr/     # Emby bridge markers, versions, Premiere
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

## Markers and lab-recorded cassettes

`test_servers_markers_vcr`, `test_servers_emby_markers_vcr` and a few `test_servers_jellyfin_vcr` classes
(`TestJellyfinItemMissingContract`, `TestJellyfinTrickplayRegistrationContract`) are recorded against throwaway lab
servers (see `docs/design/lab-servers.md`), not the Docker-Compose test stack. They need:

- the Media Preview Bridge plugins installed on the Emby and Jellyfin lab servers;
- a synthetic "Synth Chapters (2021)" show (S01E01, S01E01 - Extended, S01E02) indexed on every server, with nothing
  stored on those episodes (the tests remove what they post);
- Jellyfin 12.0 for `test_an_alternate_version_is_not_missing` (12.0's API-key `/Items?Ids=` leaves owned alternate
  versions out while `/MediaSegments/<id>` answers) and an Emby without the plugin for `TestEmbyWithoutTheBridgeContract`
  (Ping 404, markers route 404).

Record with the server URLs and credentials exported (`PLEX_URL`/`PLEX_TOKEN`, `JELLYFIN_URL`/`JELLYFIN_TOKEN`,
`EMBY_URL`/`EMBY_USER_ID`), for example:

```bash
JELLYFIN_URL=http://<lab-jellyfin>:8096 JELLYFIN_TOKEN=<token> \
  python -m pytest --no-cov -n 0 tests/test_servers_jellyfin_vcr.py -k 'ItemMissing' --record-mode=once
grep -rlF -e "<token>" tests/cassettes/test_servers_jellyfin_vcr/ && echo "LEAK" || echo "clean"
```

Always run the leak grep for every token and user id you used. `EMBY_USER_ID` must be exported for Emby runs: the
cassette scrubber reads it to replace the recording user's id with `FAKE_USER_ID` (recorded as `/Users/FAKE_USER_ID/`).

Contracts worth knowing:

- `TestEmbyItemMissingContract` pins how Emby answers an item id it doesn't have (per user: 404; API key: an empty
  `Items` list), which Check servers uses to tell a deleted item from a failed read.
- An empty `/Items?Ids=<id>` answer is asked again by id (`/MediaSegments/<id>`), so
  `TestJellyfinItemMissingContract.test_an_id_jellyfin_doesnt_have_is_missing.yaml` holds that 404 twice. Its third
  entry is a hand copy of the first; a re-record replaces it with a real answer.
- Jellyfin answers `/Items?Ids=<id>` without `Path` or `MediaSources`, so for exactly that request the scrubber keeps
  each item's `Id` and `Type` only instead of emptying the list.

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
