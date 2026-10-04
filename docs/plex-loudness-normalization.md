---
title: Plex loudness analysis on your workers, for Normalize Loudness
heading: Plex loudness
description: Analyses audio tracks on this app's workers and stores loudness measurements in Plex, for
  Normalize Loudness.
---

<nav class="task-links" aria-label="Plex loudness tasks">
  <a href="#turning-it-on"><strong>Turn it on</strong><span>Requirements, server switch and library selection</span></a>
  <a href="#checking-the-results"><strong>Check results</strong><span>Inspector status and native Plex analysis</span></a>
  <a href="#what-it-runs"><strong>Analysis details</strong><span>FFmpeg filter, stored fields and CPU processing</span></a>
  <a href="#undoing-it"><strong>Undo app writes</strong><span>Journal safeguards and restore command</span></a>
</nav>

Plex's **Normalize Loudness** needs each audio track analysed first. This app runs FFmpeg's loudness analysis on its
workers, alongside previews and Intro & Credits, and stores the measurements where Plex keeps them. Plex then treats
the completed tracks as analysed.

It is for Plex movie and TV libraries, and it stays off until you turn it on for a server. Music libraries are not
supported: Plex's music analysis includes album gain and fades that this video feature does not produce.

Loudness is off by default for each Plex server. When you enable it, all supported movie and TV libraries are selected
by default; you can narrow that selection on the **Libraries** tab.

Plex's playback requirements still apply: the player account needs Plex Pass, or membership of a Plex Home whose
admin has Plex Pass. Enabling Normalize Loudness causes audio transcoding; generating measurements here does not
unlock the playback feature. See Plex's [Audio Track Enhancements for Video](https://support.plex.tv/articles/audio-track-enhancements-for-video/).

## Turning it on

1. Run this app on the same machine as Plex and mount Plex's config folder locally. Plex must be running. This release
   supports Plex **1.43.4.x** and does not support loudness through the Plex helper or a network-mounted database.
2. On the server's **Loudness** tab, switch on **Analyse loudness for this server**.
3. On the **Libraries** tab, choose movie and TV libraries in the **Loudness** column. Both start on; music and other
   non-video libraries cannot be selected.

Loudness has its own opt-in; **Intro & Credits** can stay off, and its database-write confirmation is not required.
Run **Setup Health** to check the connection, server identity, database and supported Plex version before starting a
backfill. The app compares Plex's live identity with the mounted config, checks the database schema and WAL mode, and
matches the file's size and Plex bundle hash before analysis. Inside the write transaction it checks the file and
stream snapshot again. A changed source is left unwritten and checked again later.

Enabled files get a loudness follow-up after their previews and any relevant Intro & Credits jobs, including
webhooks, manual library runs and scheduled preview scans. Recently Added processing uses the same follow-up flow.
Chapter thumbnails remain part of the Previews job; Intro & Credits and loudness are separate jobs. Follow-ups can
appear in the queue together while waiting for the preceding jobs' first passes; they do not wait through every
retry of those jobs. For a loudness-only backfill,
start a **Plex loudness** job from the dashboard (**New job**), for chosen libraries or all of them. A file Plex
hasn't added to its library yet, or met while Plex was restarting or its database busy, is checked again according to
your retry settings. The same job row shows the countdown and attempt count; **Retry now** skips the wait.
After retries end, unresolved files leave a failure or a completion warning when other files succeeded. The Files
panel gives the reason. These retries are automatic and do not require review or approval.
Jobs share the workers, priorities, pause and cancel of every other job.

### Checking the results

Open a file in **Tools → Inspector**. Its **Loudness** section shows the measurements Plex reports for each audio
track and whether normalization is available. It also displays complete native Plex results when this app's
automatic loudness analysis is off. Availability means Plex can normalize the track; the player's **Normalize
Loudness** setting is separate.

The Inspector distinguishes missing measurements, incomplete results and a server it cannot reach. It reads metadata
without changing it or starting a job. The job banner identifies **Plex loudness** separately from Previews and Intro
& Credits, and the results refresh when the job finishes.

Plex keeps analysing on its own schedule too. The app preserves complete native measurements it finds before
publication. Once the item's completion mark exists, Plex's non-forced analysis skips the completed item.

Setup Health shows Plex's own **Analyze audio tracks for loudness** setting as information. Keeping native analysis
enabled is valid: Plex can serve music and video libraries this app does not cover. Native video analysis also
depends on each library's **Enable Loudness Analysis** setting; a server-wide schedule alone does not mean both
applications are analysing the same videos.

When this app has eligible video libraries selected and its writer is ready, **Set to Never** is an optional,
separately confirmed action. It changes Plex's server-wide schedule, including music and unselected video libraries;
it does not enable this app, extend its library selection or erase existing measurements. Keep native analysis on
if those other libraries need it. The app checks its selection and writer readiness again when the action runs.
The control is excluded from bulk fixes, and installing or upgrading the app never changes this Plex preference.

## What it runs

For every audio track that Plex has no loudness for yet, the app uses the loudnorm filter observed in Plex Media Server
1.43.4:

```
ffmpeg -i FILE -map 0:TRACK -af loudnorm=I=-16:TP=-1:LRA=9:print_format=json -f null -
```

and stores loudnorm's measurements in the track's `extra_data`, under the names Plex uses: `ln:loudness`, `ln:peak`,
`ln:lra`, `ln:threshold`, `ln:gainOffset` and `ln:loudnessAnalysisVersion`. The values are the ones loudnorm prints,
as Plex stores them, and the rest of the track's data is kept. Decoding audio gains nothing from a GPU, so the
analysis runs on the CPU even on a GPU worker. The entire audio track is measured; the source media is never rewritten.
Silent tracks and audio too short for an integrated measurement use Plex's observed `-inf` loudness and `inf` gain
values, with the remaining fields validated. Invalid or incomplete reports are not stored.

Once every original indexed audio track of a movie or episode (across its live versions) has loudness, the app also marks the item itself, as
Plex does: `ln:loudnessAnalysisVersion` in the item's `extra_data` (`metadata_items`). Plex checks that mark, not the
tracks, before it analyses an item, so without it Plex would analyse the item again. Only that field is written; Plex's
title-search triggers on the table don't fire on it, and the app refuses to write if a trigger could.

EAC3 (Dolby Digital Plus) tracks are the one exception. Plex decodes them with its own Dolby decoder, which only runs
inside Plex and applies no dynamic range compression, so the app adds `-drc_scale 0` for them. FFmpeg and Plex use
different decoder builds, so measurements need not be identical for every codec or source.

Tracks Plex (or an earlier job) already analysed are skipped: a file whose tracks and item are all done never reaches
a worker, and one whose tracks are done but whose item isn't marked yet is only marked.
The same file listed twice in Plex is analysed once and written to both.
Existing partial or unrecognised loudness metadata is also preserved. Such a track reports a failure with advice to
run Plex's native analysis, instead of overwriting data the app cannot verify.

## Undoing it

Before committing each write, the app saves an undo record in `loudness-writes.jsonl` in its config folder. The record
identifies the exact Plex database file and audio source as well as the track's (or item's) data before and after.
If this initial record cannot be saved, the database write is rolled back. After committing, the app adds a confirmation
to the journal. Undo only replays confirmed writes: a crash or rollback cannot make it undo a later native Plex result.
If saving the confirmation fails, the database write remains and the app warns; keep the journal for recovery, because
that unconfirmed record will not be replayed automatically.

To restore the affected tracks and items, stop Plex and run, in the app's container:

```
docker exec -it media-preview-generator python -m media_preview_generator.loudness.undo \
  --db "/path/to/com.plexapp.plugins.library.db" --log /config/loudness-writes.jsonl
```

`--db` is Plex's database as the container sees it. Add `--dry-run` to count what would be restored first.

Use the original database at the same mounted location: undo refuses a copied, replaced or different database. Logs
from older builds without database identity are refused. Records for other servers and changed audio sources are
left alone.

A track or item still holding exactly what the app wrote gets its old data back. One Plex changed since keeps
Plex's change and loses only the fields the app added, and is left alone if Plex changed those fields too. The
`updated_at` time the write set on a track isn't put back.
