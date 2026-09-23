# Lab proof: `turn_off_library_marker_detection` — Turn off + restore

Ran `plex_detection_turnoff_proof.py` against the throwaway lab Plex (`mlab-plex`, `docker ps`), never the
production `plex` host. Token/URL came from the local-only `evidence/lab/env` file, never printed.

The script builds a real `PlexServer` the way `tests/test_servers_plex.py`'s `plex_server_under_test`
fixture does, and calls the real `turn_off_library_marker_detection` method — the same one
`POST /api/servers/<id>/plex-marker-detection` calls — against two lab sections:

- Section `2` (a movie library, "movies"): turn off `enableCreditsMarkerGeneration`.
- Section `1` (a show library, "tv"): turn off `enableIntroMarkerGeneration` and
  `enableCreditsMarkerGeneration`.

## Read-back values

| Section | Pref | Before | After turn-off | After restore |
| --- | --- | --- | --- | --- |
| 2 (movie) | `enableCreditsMarkerGeneration` | `true` | `false` | `true` |
| 1 (show) | `enableIntroMarkerGeneration` | `true` | `false` | `true` |
| 1 (show) | `enableCreditsMarkerGeneration` | `true` | `false` | `true` |

`false` is the same wire value `GET /library/sections/<id>/prefs` returns for a pref Plex's own
Edit library -> Advanced tab shows unticked — confirming the API-level turn-off is exactly what a
user would see toggled off in Plex's UI.

Restore used a direct `PUT .../prefs` (not the method under test), so the restore step doesn't depend on
the code being proven. `enableAdMarkerGeneration` (outside the two allowed prefs) was read before and after
on both sections and never changed (`1` throughout) — confirming the write touches only the prefs it's
told to.

## Run

```
LAB_ENV_FILE=/path/to/evidence/lab/env python docs/design/intro-credits/evidence/lab/plex_detection_turnoff_proof.py
```

Script exits non-zero if the turn-off didn't take or the restore didn't land. This run: `OK: turn-off
confirmed, restore confirmed.`
