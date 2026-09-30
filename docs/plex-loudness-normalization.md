---
title: Plex loudness analysis on your workers, for Normalize Loudness
heading: Plex loudness
description: Runs Plex's own loudness analysis of each audio track on this app's workers and stores it in Plex, for
  Normalize Loudness.
---

Plex's **Normalize Loudness** needs each audio track analysed first. Plex Media Server does that itself, one track at
a time, in the background: on a big library it can take months to reach every file. This app can run the same analysis
on its workers, alongside previews and Intro & Credits, and store the answer where Plex keeps it. Plex then treats those
tracks as done.

It is for Plex only, and it stays off until you turn it on for a server.

## What it runs

For every audio track that Plex has no loudness for yet, the app runs the command Plex Media Server 1.43 runs:

```
ffmpeg -i FILE -map 0:TRACK -af loudnorm=I=-16:TP=-1:LRA=9:print_format=json -f null -
```

and stores loudnorm's measurements in the track's `extra_data`, under the names Plex uses: `ln:loudness`, `ln:peak`,
`ln:lra`, `ln:threshold`, `ln:gainOffset` and `ln:loudnessAnalysisVersion`. The values are the ones loudnorm prints,
as Plex stores them, and the rest of the track's data is kept. Decoding audio gains nothing from a GPU, so the
analysis runs on the CPU even on a GPU worker.

Once every audio track of a movie or episode (all its versions) has loudness, the app also marks the item itself, as
Plex does: `ln:loudnessAnalysisVersion` in the item's `extra_data` (`metadata_items`). Plex checks that mark, not the
tracks, before it analyses an item, so without it Plex would analyse the item again. Only that field is written; Plex's
title-search triggers on the table don't fire on it, and the app refuses to write if a trigger could.

EAC3 (Dolby Digital Plus) tracks are the one exception. Plex decodes them with its own Dolby decoder, which only runs
inside Plex and applies no dynamic range compression, so the app adds `-drc_scale 0` for them. Their loudness and gain
match Plex's; the true peak can differ by a few tenths of a dB.

Tracks Plex (or an earlier job) already analysed are skipped: a file whose tracks and item are all done never reaches
a worker, and one whose tracks are done but whose item isn't marked yet is only marked.
The same file listed twice in Plex is analysed once and written to both.

## Turning it on

1. Confirm the Plex database write on the server's **Intro & Credits** tab (Servers → Edit). Loudness writes to the same
   database, under the same rules: the app must see Plex's config folder on the same machine, and Plex must be running.
   It can't go through the Plex marker agent.
2. On the server's **Loudness** tab, switch on **Analyse loudness for this server**.
3. On the **Libraries** tab, choose the libraries in the **Loudness** column. Movie and TV libraries start on; music
   libraries are chosen by hand, because Plex keeps more loudness data for music (album gain, fade ramps) than this
   writes.

New files get it after their previews (and their Intro & Credits, when that's on), when Sonarr, Radarr or Plex sends
a webhook. For the files you already have,
start a **Plex loudness** job from the dashboard (**New job**), for chosen libraries or all of them. A file Plex
hasn't added to its library yet, or met while Plex was restarting or its database busy, is checked again later, up to
three times. Jobs share the workers, priorities, pause and cancel of every other job.

Plex keeps analysing on its own schedule too: whichever gets to a track first stores it, and the other skips it.

## Undoing it

Every write is recorded in `loudness-writes.jsonl` in the app's config folder, with the track's (or item's) data before
and after. To put the tracks and items back as they were, stop Plex and run, in the app's container:

```
docker exec -it media-preview-generator python -m media_preview_generator.loudness.undo \
  --db "/path/to/com.plexapp.plugins.library.db" --log /config/loudness-writes.jsonl
```

`--db` is Plex's database as the container sees it. Add `--dry-run` to count what would be restored first.

A track or item still holding exactly what the app wrote gets its old data back. One Plex changed since keeps
Plex's change and loses only the fields the app added, and is left alone if Plex changed those fields too. The
`updated_at` time the write set on a track isn't put back.
