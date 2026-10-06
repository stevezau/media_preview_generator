# Plex loudness analysis

## Scope and existing model

Loudness is a separate job kind in the existing dispatcher. It uses the shared
worker pool, admission limits, priorities, pause/cancel controls, persistence and
follow-up retries with the shared attempt limit. Audio analysis scans a whole track, so it should not
delay the completion of the short chapter-thumbnail work in a Previews job.

The chapter-thumbnail publisher is the model for safe Plex integration:
positive server identity, a tested Plex version, a source snapshot, narrowly
scoped database writes, and verification through Plex's own API. Loudness reuses
`LocalPlexDb` locking, filesystem checks, transaction handling and extra-data
encoding, plus the existing Plex source hash and fingerprint functions. It has
its own schema and stream operations; it does not call chapter registration or
introduce another generic processing framework.

Initial support is local Plex 1.43.4.x movie and TV libraries. The feature has its
own default-off switch and Setup Health checks, independently of Intro & Credits.
Music is excluded because complete native music analysis includes additional
album and fade data. The remote helper needs a separate loudness capability and
typed operations before it can support this feature.

## Inspector and automatic entry points

Inspector reads each owning Plex server's metadata for the exact item and part matching the selected file. It
reports per-stream measurements and normalization capability without requiring the app's analysis opt-in or a local
Plex database mount. This is a read-only view of Plex's reported data, not proof that a same-size replacement has
already been reanalysed. Source size and fingerprint mismatches are reported instead of presenting known-stale
measurements as current. Unsupported or incomplete data stays distinguishable from a server that cannot be reached.
Non-finite native sentinel values use valid JSON strings; malformed values never become JSON NaN or Infinity.

Automatic loudness follow-ups use the actual paths selected by preview processing for webhook, manual/library and
scheduled runs, including Recently Added. Eligibility is shared with library opt-in and path ownership; preview
retry children do not create another workflow. A loudness follow-up waits for its own preview and all relevant
Intro & Credits jobs using the existing first-pass dependency semantics. The single dependency field remains
compatible with persisted jobs, while the complete dependency list prevents a joined marker job from letting
loudness start before its own preview has finished. Chapter thumbnails remain in the Previews job.

An automatic follow-up keeps the selected files in one job, like a manual loudness job; the selected-file count
does not split the initial library scan into visible jobs. Follow-ups split only when their combined predecessor
list would exceed 500 dependency IDs, preserving every file's barriers. Repeated submission for the same preview
does not queue the same file again, including after a restart. Separate preview jobs retain their own follow-ups
so an earlier runnable job cannot bypass a later preview's dependency. Existing native results make a repeated
loudness check inexpensive.

Selections larger than 500 paths are saved once as immutable, content-addressed inputs under the app's config
directory. Job updates carry only their reference and count. Restarts and reruns read the exact saved selection;
a missing or corrupt input fails the job instead of expanding its scope to all libraries. Inputs remain available
until the last referencing job is deleted, including cloned reruns.

An interrupted job rechecks its selected files against their current source and Plex metadata before counting
them as finished. Native-complete files skip analysis; previous Files-panel rows alone cannot establish that a
file is still current. Each resumed attempt rebuilds its file and per-server totals. Deliberately parked jobs
retain their existing checkpoint accounting and unfinished work.

An absent selected path is retired as `skipped_source_gone` only when a saved Sonarr or Radarr import explicitly
reported that exact path deleted, after applying the selected server's path mappings. A path that exists now is
still checked normally. Other missing files retain their existing failure or retry behavior; filenames alone
cannot establish that a release was replaced.

## Detection and measurement

For each owning, enabled Plex server, resolve the canonical path using the
existing path mappings. Read live original media parts and their indexed audio
streams. Compare the source's size and calculated Plex hash with the indexed
part before accepting existing measurements or launching analysis.

- A complete supported native result is preserved, including Plex's observed
  silence and very-short-audio sentinel values.
- An absent result is analyzed with the existing full-track `loudnorm` filter
  and codec-specific decoder settings.
- Partial or unfamiliar native metadata is left unchanged with an actionable
  failure. Guessing at it must not overwrite Plex's work.
- Missing indexing, changed sources and temporarily unavailable Plex state wait
  for a bounded retry. Invalid configuration and unsupported formats fail clearly.

Capture the local file fingerprint and the indexed stream identity before
analysis. Capture the item's complete set of original audio sources as well, so
adding, removing or replacing another version cannot silently validate an old
completion decision. Recheck the local fingerprint and database snapshot when
publishing. The source hash uses the chapter feature's existing implementation,
including Plex's small-file hashing rules.

Measurement quality is established against native Plex measurements and actual
normalized playback on identical source bytes. Decoder and FFmpeg-version
rounding differences must be recorded rather than described as bit-identical
parity. The supported scope and live evidence are explicit; this design makes no
universal claim about untested media or future Plex versions.

## Publishing and rollback

`GuardedLoudnessDb` verifies live API identity against the local Preferences.xml
identity and checks the tested version. Every write also rechecks the local
filesystem, writability and live Plex database holder. Schema/trigger checks and
WAL requirements are independent of the marker feature's schema.

Under `BEGIN IMMEDIATE`, re-read the exact indexed source, check cancellation,
and preserve any complete result Plex published while analysis was running.
Change only the supported loudness fields and stream update timestamp. Mark the
item complete only after all relevant audio streams have complete measurements
and its source snapshot still matches. Read back committed data and require the
Plex API to expose the same values and normalization capability on the exact
part and stream. Recheck the local source fingerprint after that API response as well, so a replacement during
verification cannot be reported as successfully analysed.

The undo journal binds records to the exact database file and target identity.
Write a durable intent before committing SQLite, then a durable commit receipt.
Undo processes only confirmed intents whose current database, source identity
and values still match. Legacy unscoped records are refused; unconfirmed intents
are retained for recovery and never guessed to be committed. A failure to write
the intent rolls the transaction back. A receipt failure after commit is reported
explicitly as a committed write with unavailable undo confirmation.

Undo requires Plex to be stopped. It preserves unrelated newer metadata and
restores previous values of changed fields. Copied, replaced or relocated
databases fail closed rather than borrowing another database's journal entries.

## Retry and cancellation contracts

Keep failure reporting separate from retryability: one permanently failed stream
or server must not hide another stream or server waiting for a temporary lock.
Retries retain the originating server scope, priority and sender paths, and skip
completed streams. The global retry count and backoff settings apply. As with
Previews and Intro & Credits, hidden attempts belong to the original job's retry
chain. That original row stays Pending with its retry countdown, becomes Running
during an attempt, and shows the latest results across all files. There is no
human review or approval stage.

Only written or API-verified existing loudness results count as successful work.
An exhausted chain with no successful files fails; a partial result completes
with a warning. Permanent failures remain visible when other files recover.
Cancellation, Retry now and recovery after restart operate on the same chain.
Hidden attempts do not announce successful completion independently of it.

Each retry stores immutable aggregate counts and the previous outcomes of all
its unresolved files. Large selections and their accounting share the immutable input file;
job updates retain only its reference. Original sender paths keep that accounting stable when
a missing file later resolves through a different mount. Replaying the latest
attempt results replaces those previous counts, rather than incrementing totals,
so restart recovery is idempotent. The initial Files history remains capped;
retry results bypass that cap. Neither aggregate accuracy nor
the final per-server status depends on which initial history rows were retained.

Automatic loudness deduplication is scoped to the originating preview job and
covering server scope. It recognizes persisted follow-ups in every state so
replaying enumeration does not create duplicates. A separate preview retains
its own dependency and follow-up, even when an earlier job covers the same file.

Cancellation is checked before analysis and again inside each write transaction,
including an item that needs only its completion mark. Pause and process cleanup
use the existing freeze and worker mechanisms.

## Verification

Unit regressions cover two databases with overlapping IDs, changed sources and
versions, native-result preservation, partial metadata, write-lock retries,
mixed failures and pending work, journal intent/receipt failures, cancellation,
server-scope deduplication, and identity/version/API verification. Lifecycle
regressions use the actual job manager, dispatcher, worker pool and temporary
SQLite publication path to check retry parenting, terminal status, persisted
results and cancellation; external server and process boundaries are injected.

Browser tests exercise the separate job UI, independent opt-in, video-only
selection, settings persistence and compatibility with chapter settings. Live
proofs use the claimed Plex lab and dedicated test items, retaining native
baselines, codec comparisons, playback measurements and rollback evidence in
[`evidence/`](evidence/). The read-only comparison harness is
[`verify_plex_loudness.py`](../../../tests/integration/verify_plex_loudness.py).
The [Inspector and automatic follow-up proof](evidence/inspector-automation-live-2026-10-04.md)
records real Plex metadata, browser rendering, completion refresh and preview-skip coverage.
