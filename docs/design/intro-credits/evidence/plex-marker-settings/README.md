# Plex's own marker settings and "Keep Plex's" — evidence

Setup Health's Plex marker rows (`markers/readiness.py`): while a library's own "Intro markers" / "Credits markers"
setting is off, Plex hides every skip marker in that library, ours included, so the fix is **Turn on** per library;
the server-wide "Never" (stop Plex's own detection) is its own button and never part of a bulk fix. The Plex reader
answers None for a type a library hides (`servers/plex.py`), so a hidden type is never read as "none there".

## Files

| File | What |
|---|---|
| `plexprefs.sh` | Prod Plex's server-wide `Generate*MarkerBehavior` and each library's `enable*MarkerGeneration` (`show`, `keys`, `markers <ratingKey>…`; `apply`/`movies` write them and need the owner's go-ahead). The token is read at run time over ssh and only ever sent as a header; it is never printed |
| `probe_libs.py`, `probe_root.py`, `probe_item.py`, `probe_raw.py` | What the app's Plex reader returns for libraries and items (read-only; no token printed) |
| `sweep.sql`, `sweep2.sql`, `kept.sql` | markers.db queries: Plex answers stored as "none there", and files kept as Plex's own (read-only) |
| `local/` (local-only) | The outputs: `lab_features.txt`, `prod_features.txt`, `lib_samples.txt`, `sweep*_out.txt`, `kept_out.txt` |

The scripts ran from the session scratchpad against the prod app's settings (read-only); `server_id` in them is the
app's own id for the prod Plex server.
