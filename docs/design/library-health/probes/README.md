# Library health probes (2026-10-10)

Read-only scripts that produced the measured numbers in `../spec.md`. Kept as `.py.txt`
so the repo's lint ignores them; run with `python3 -I <file>.py.txt`.

- `plex_health_probe.py.txt` — on the plex host: whole Plex count from the database + BIF stats (`16 --quick` skips the
  media-share check that turned out to be ~7 files/s).
- `nfs_sample.py.txt`, `nfs_dirs.py.txt` — on the plex host: media-share stat and folder-listing speed.
- `unflagged_age.py.txt` — on the plex host: BIFs Plex isn't flagged to show vs Plex's last part update.
- `embyish_probe.py.txt`, `embyish_markers.py.txt`, `jf_trickplay.py.txt` — on storage, from the repo root with the
  shared venv: lab Emby/Jellyfin listing, bulk chapters, per-item segments, bulk Trickplay field.
- `lab_unflagged.sh`, `lab_analyze.sh` — lab Plex: find an unflagged BIF, then prove Analyze sets the flag (the only
  script here that writes, and only to the lab Plex).
