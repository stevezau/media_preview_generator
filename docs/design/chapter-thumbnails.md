# Plex chapter thumbnails: implementation plan

Status: minimal live registration and cache-replacement proof passed; implementation in progress.

Replacement branch: `stevezau/plex-chapter-thumbnails`, based on `f7063a8`.
Original PR: #287, head `5a26236`, base `ef145a3`. Preserve its attribution and useful
research, but do not transplant its old dispatcher or bundle-hash assumptions.

## Current design decision

Use a common guarded chapter-reference writer, **locally on the Plex host or through the
existing Plex-side helper**. The investigator proved the minimal database update live: only
existing `taggings.thumb_url` values change; immediate API responses and image hashes match;
a content-hash query revision bypasses a primed PhotoTranscoder cache after replacement.
The default app feature remains off. A locally writable, positively identified Plex database
or a compatible configured helper beside that database is required. Do not require Intro & Credits generation, its
marker-specific database confirmation, or its Plex Pass check to use chapter thumbnails.
Earlier scanner/API exploration below is evidence, not a commitment to ship those transports.

The app keeps one chapter toggle with an adjacent explicit explanation that it updates
existing chapter image references in Plex's database and Intro & Credits can stay off.
Saving that toggle is the feature's opt-in. Do not add a second health toggle or silently
interpret existing marker consent as authorization for a different kind of database write.

The existing `markers.plex.agent` address/shared-key block already validates and persists
while `markers.enabled` is false. Reuse that single connection rather than duplicate secrets
or require enabling marker generation. Retain the existing controls under Intro & Credits,
preserving DOM wiring/masked-token
semantics and stored shape, and provide a direct Configure Plex helper link from chapter
Setup Health that selects the tab and scrolls to the shared connection. Both chapter and
marker users on the Plex host can use guarded local writes without enabling the helper.

### Proposed typed helper protocol (coordinate with the registration owner)

Reuse the existing bearer authentication, protocol header, response envelope and deadline
handling. Add a separately advertised `chapters_v1` capability; older helpers keep working
for markers and answer chapter capability unavailable. No arbitrary paths/SQL/thumb URLs.

- Read-only capability check: expected `machine_identifier`; response identifies the
  helper's Plex identity, chapter schema support and file/database readiness. It must not
  require Plex's marker tag or perform chapter writes. Missing or unequal identity is blocked.
- Chapter snapshot request: expected identity, positive `rating_key`, `media_id`, `part_id`,
  expected `bundle_hash`, bounded `deadline_s`. Response contains the verified same-version
  chapter rows (`tagging_id`, `index`, `start_ms`, `end_ms`, current reference), plus a stable
  snapshot digest. The helper discovers native chapter tags itself.
- Registration request: the same item/media/part/hash identity, expected snapshot digest,
  complete image list (`index`, `sha256`), bounded deadline. The helper derives fixed chapter
  paths and native references from its own config and verified rows. Under the shared database
  serialization boundary, recheck the snapshot/identity and image completeness, then update
  only the columns proven necessary. Response is changed/unchanged with verified row state;
  absent item, changed chapter map, busy DB, unsupported schema and missing images are typed
  failures, not success. The app verifies Plex chapter metadata/image serving afterward.

Endpoint names and final field types belong to the DB/helper owner once the live minimal
update proof is complete; agree them before parallel callers are implemented. A snapshot
digest must cover the full expected chapter map and old references, not just row count.

### Parallel file ownership after the proof

- Registration owner: chapter database implementation, helper capability/endpoints,
  helper/client transport and protocol types, helper image/version requirements.
- Main agent: per-item planning, shared-worker extraction/publication, BIF preservation,
  source/manifest freshness, result propagation and retry orchestration.
- Scout: server checkbox/shared-helper UI, strict API boolean validation, chapter readiness
  presentation and user/deployment documentation. Put chapter readiness in a focused module
  with a small call from `servers/plex.py`; coordinate the call site with metadata changes.
- Verifier: acceptance tests for contracts/integration, feature and regression suites,
  UI/settings checks, final verification. Coordinate test-file ownership before edits.

The DB proof passed before feature work began. Authentication and transactions remain
necessary infrastructure; live image-serving and cache-replacement evidence establishes
that the narrowly bounded update activates playback.

## Evidence and release gate

Sanitized live records: [registration matrix](chapter-thumbnails/evidence/registration-matrix.json),
[bounded backend and cache replacement](chapter-thumbnails/evidence/backend-live.json),
[native database delta](chapter-thumbnails/evidence/native-nonforce-db-diff.json),
[source-absent scanner](chapter-thumbnails/evidence/source-absent-isolated.json), and
[restart and partial-scan persistence](chapter-thumbnails/evidence/persistence.json).
The tested restart and partial scan preserved references and image hashes. Reanalysis can
replace chapter rows; the implementation reconciles references again rather than promising
that every Plex reanalysis preserves them.

- PR #287 writes `Contents/Chapters/chapter<N>.jpg` next to `Contents/Indexes/index-sd.bif`.
  The owner reported that the files alone did not activate thumbnails in Plex.
- The PR treats failed JPEG extraction as successful publication. A focused reproduction
  showed a stale existing JPEG receiving a new source journal, preventing later retries.
- The current baseline calculates Plex bundle hashes from the local media bytes and can
  publish a BIF before Plex indexes the file. Preserve that behavior.
- The registration investigator's Plex 1.43.4 probe returned no chapter elements from a
  default metadata request, and three with `includeChapters=1`. Any metadata helper must
  request chapters explicitly; tests must assert that query argument.
- Before feature implementation, prove a supported or deliberately bounded registration
  mechanism on the isolated lab: new images become usable through Plex's chapter metadata
  and served image URLs, survive restart, and do not require Plex to regenerate images.
  Record the server version, exact mechanism, before/after chapter response, and limitations.
- A scan request completing or a JPEG existing is not proof of chapter registration.
  Do not invent an upload API or substitute a Plex Analyze operation without verifying
  whether it reruns native extraction.
- Plex's native scanner in no-force mode registered a complete externally written image
  set unchanged, including with the source temporarily absent. Claimed and unclaimed
  `chapterThumbs` calls with `never` or `scheduled` returned HTTP 200 without registration;
  `asap` triggered generation but overwrote supplied images. Scheduled Butler work likewise
  used forced extraction. None is an adequate remote registration mechanism for this feature.
- Native before/after snapshots showed only existing `taggings.thumb_url` changed. A guarded
  minimal update reproduced visible chapter metadata and served correct bytes without restart
  or native extraction. Content-hash query revisions also replaced previously cached images.
  The initial writer supports only the tested Plex 1.43.4.x build family.
- Public request contract checked independently: the [Plex API documentation](https://developer.plex.tv/pms/)
  specifies `PUT /library/metadata/{ids}/chapterThumbs`, user-token authentication, optional
  integer `force=0|1`, and `200 OK`. The primary
  [plexjs implementation](https://github.com/LukasParke/plexjs/blob/main/src/funcs/libraryGenerateThumbs.ts)
  sends this path with no body; its
  [request schema](https://github.com/LukasParke/plexjs/blob/main/src/models/operations/generatethumbs.ts)
  uses comma-separated metadata IDs and defaults force to zero. No additional required
  selector or client header appears there. The public contract does not describe the
  conditions that suppress execution, nor promise that HTTP 200 means images were registered.

Lab operations follow `docs/design/lab-servers.md`. The registration investigator owns
the shared lab while proving this feature. Do not remove containers or start stopped
snapshots that share live volumes. Never copy tokens into evidence or tracked files.

## Rejected scanner deployment alternative

The existing default app cannot run Plex's native scanner: its image supplies FFmpeg, not
the Plex installation. A writable Plex config mount provides data, not the executable or its
runtime dependencies. Running the app on the same host as a separate Plex container does
not change that boundary. No production scanner-execution or SSH transport exists here.

The repository does have an optional Plex-host helper precedent: `plex-marker-agent/`.
It is a small Python image with typed per-item marker database operations, a shared key,
protocol/version negotiation, Plex machine-identity checks and Setup Health integration.
It deliberately contains no media tools, mounts only Plex's config directory and promises
not to be a remote-control API. Its current deployment cannot execute the scanner either.

Before the minimal database proof, scanner no-force registration would have required an explicit new
deployment choice: a narrowly scoped helper that can execute the matching Plex installation's
scanner on the Plex host/in its runtime. Reuse the agent's authenticated, versioned transport
patterns, but advertise a separate chapter-registration capability and documented runtime
requirements. A config-only marker agent must continue to report that capability unavailable.

Such an endpoint accepts a bounded typed item/media identity, not arbitrary shell commands,
paths or executable arguments. The helper owns its configured Plex runtime and validates
machine identity, media version/hash, complete existing JPEGs and no-force invocation. It
serializes its own scanner work, has a bounded process lifetime and verifies resulting
chapter URLs. Existing marker-only credentials/deployment must not silently acquire general
execution privileges. Generic SSH or Docker-socket access would be additional operational
scope, not an automatic fallback for this feature.

This scanner transport is not part of the implementation. The proven bounded reference update
below works in the existing writer runtime without adding Plex binaries or remote shell access.

### Selected mechanism: bounded chapter-reference updates

The existing marker agent supplies the smaller deployment option:
reuse its SQLite runtime and transport for a separately negotiated chapter-registration
capability, updating only preexisting chapter references whose native representation has
been proven. The live proof completed and this implementation path was authorized.

Existing reusable mechanics in `markers/publishers/plex_db.py` include SQLite `mode=rw`
(never creating a missing database), local-filesystem/WAL-lock validation, one process lock
per database, bounded busy waits, `BEGIN IMMEDIATE`, schema/trigger checks, rereading media
parts after obtaining the write lock, and rollback on every unsuccessful transaction.
`plex-marker-agent/plex_marker_agent.py` authenticates/version-checks typed per-item calls.
Its image already supplies SQLite; a pure reference update would not require Plex binaries.

The native before/after diff and immediate/cache-replacement proof underpin this mechanism.
Verify restart, Plex reanalysis and multiple media versions in acceptance testing. Existing
marker writes do not explicitly invalidate Plex caches, so chapter URLs include image revisions.

Keep the mutation narrowly defined: exact existing chapter row IDs, their native
tag classification, index/timestamps, item/media version and hash, expected previous values,
and complete valid image paths must all agree again under the transaction lock. Update only
the proven required columns; create no tags/chapters, change no chapter timing, and accept no
caller-supplied SQL or arbitrary database path. Treat conflicts as pending/retryable rather
than overwriting a concurrent Plex analysis result.

The chapter capability needs its own user-visible opt-in and database-write explanation;
the Intro & Credits confirmation and Plex Pass checks are feature-specific. The existing
marker agent rejects a known machine-ID mismatch but permits an unknown ID; the new chapter
operation should require a positive identity match. Negotiate capability/version explicitly,
preserve marker-only behavior, and share the database serialization boundary with markers.
An older marker agent remains usable for markers and reports chapters unsupported.

## User behavior

- Add a Plex-only **Generate chapter thumbnails** toggle under Servers -> Edit -> General,
  beside the Plex config folder. Default off; persist a JSON boolean at
  `media_servers[].output.chapter_thumbnails`.
- This is an optional output of the existing **Previews** job kind, not a third job kind.
  Existing library selection, exclusions, priority, schedules and webhooks remain applicable.
- Missing-only processing fills missing or stale enabled outputs. A current BIF remains
  untouched when only chapters need work. Regenerate-all rebuilds enabled preview outputs.
- Disabling chapter generation stops requesting chapter work and preserves existing files.
  Re-enabling checks the chapter manifest and current source rather than trusting existence.
- The existing Files panel should distinguish scrubber completion from chapter completion:
  for example, `Scrubber: ready; Chapters: 8/10, retrying`. Do not show complete success when
  optional enabled output failed. Preserve a successful BIF during chapter retries.

## Smallest integration boundaries

1. `servers/plex.py`: typed chapter lookup result distinguishing ready chapters, confirmed
   no chapters, pending index/analysis, and lookup failure. Use `includeChapters=1`, validate
   and preserve the server's actual index convention, and associate metadata with the correct
   media version/part. The proven registration mechanism belongs behind an explicit Plex
   method, not an unverified generic refresh call.
   Invoke registration only when every expected chapter JPEG is complete and the targeted
   item/media ID, version/part, and source bundle hash agree. Registering a partial set may
   cause Plex to extract missing images itself, defeating the load-control requirement.
   Partial extraction stays pending without invoking native registration. Once the trigger
   is proven, verify `includeChapters=1` thumb references and the served/cache image URLs;
   HTTP acceptance of the trigger alone is insufficient.
2. Add a focused chapter module under `processing/` for immutable chapter requests and a
   per-Plex-item work plan: required BIF work, chapter image work, registration work. Keep
   the existing BIF and non-Plex adapters intact; avoid a general output-framework rewrite.
3. `processing/multi_server.py`: build the plan before deciding whether interval frames
   are required. `check_only=True` remains FFmpeg-free. Chapter-only work enters the same
   capped worker path but bypasses interval extraction and BIF packing. Resolve Plex item
   IDs conditionally for chapters; BIF-only paths must retain the current no-lookup policy.
4. A timestamp-specific extraction helper shares `processing/ffmpeg_runner.py` process
   cancellation, pause, thread limits and color conversion behavior. Initial chapter grabs
   decode on CPU within the assigned worker slot; they do not promise GPU acceleration.
   Dolby Vision without a compatible base layer reports an explicit unsupported result;
   this path never initializes Vulkan or silently selects a GPU. The
   existing continuous interval generator should not decode the entire file solely for
   a handful of chapter images. PR's proposed profile is 1280px wide, source aspect ratio,
   JPEG quality 4. Do not upscale
   320px interval BIF frames into chapter thumbnails or substitute nearby interval times.
5. Chapter output publication stages validated JPEGs and atomically replaces their targets.
   Reuse the baseline `output/plex_hash.py` source fingerprint checks before publication;
   source changes during extraction invalidate the attempt. Use the locally calculated
   bundle hash, not stale prefetched Plex hashes.
6. Extend `PublisherResult` with optional typed artifact details (BIF and chapters), keeping
   existing consumers compatible. Carry completed/total chapter counts and pending/failed
   reasons through `jobs/orchestrator.py`, `jobs/worker.py`, `jobs/dispatcher.py`, persisted
   file-result rows and `web/static/js/job_modal.js`. Existing aggregate success is not
   sufficient to decide whether chapter work remains.
7. Reuse bounded retry-job orchestration in `web/routes/job_runner.py` and the shared pending
   status set in `processing/retry_queue.py`; update the API/UI consumers together. A chapter
   registration retry must not regenerate a current BIF or current chapter images. Distinguish
   transient errors from terminal failures. Preserve per-artifact state when retry history
   merges publisher rows, so an earlier BIF success cannot hide chapter failures.

### Before Plex indexing

Publish the BIF immediately using the current direct-file flow. If chapter metadata or its
registration target cannot yet be resolved, report chapters pending and schedule a bounded
retry. Never fail or defer the BIF because an optional chapter target is absent. A successful
metadata response for the correct analyzed media with no chapters is terminal `no chapters`;
an omitted chapter query flag, unavailable item, or failed API request is not that result.

The baseline `processing/plex_refresh.py` queue and journal pending tokens represent scan
notifications only. Preserve them; do not conflate notification delivery with chapter
registration. Reuse their infrastructure only if the proven chapter protocol actually fits.

## Freshness and atomicity

- Keep BIF journals and behavior compatible. Add chapter-specific metadata rather than
  changing the legacy freshness contract for all outputs.
- First enable deliberately rebuilds existing chapter JPEGs without an app manifest; an
  existing native reference or image alone does not prove source freshness. Document this
  one-time replacement. Reuse existing BIFs and change only references on the exact chapter
  rows validated for this item, never native timing, tags, or other marker types.
- A chapter manifest records the current source fingerprint, chapter identity/start offsets,
  output profile/version, successful image paths and registration state. The baseline source
  fingerprint includes device, inode, size, nanosecond mtime and ctime; reuse it.
- A metadata-only chapter timing change invalidates affected images even when the media
  bytes did not change. A source replacement invalidates old-source chapter completion.
- Write image-success metadata only after successful validated replacement. Never stamp a
  retained old image as current because another artifact succeeded.
- A registration failure leaves valid images reusable and registration pending. A failed
  extraction leaves prior images intact but not current for the new request.
- Do not blindly delete chapter files created by Plex or another process. Any stale-output
  cleanup needs app ownership and a verified current chapter map.

## Settings and health checks

Files: `web/templates/servers.html`, `web/static/js/servers.js`,
`web/routes/api_servers.py`, `servers/plex.py`; current generic settings persistence requires
no new top-level schema. Validate the setting is a boolean before the config-folder early
return in `_validate_plex_output`; do not use `bool("false")` coercion.

There is currently no native Plex chapter-generation health check. Existing readiness reads
`/:/prefs` for three library scan preferences and checks per-library `enableBIFGeneration`;
neither proves chapter generation is off. The disposable lab's `/:/prefs` exposes server-wide
`GenerateChapterThumbBehavior`, label `Generate chapter thumbnails`, type `text`, group
`library`, default `scheduled`, with these exact enum values and labels:

- `never`: `never`
- `scheduled`: `as a scheduled task`
- `asap`: `as a scheduled task and when media is added`

The registration investigator verified `never` is accepted. The disposable movie library's
`/library/sections/1/prefs` exposes no chapter-related element (all element tags checked), so
there is no evidence for a separate per-library chapter flag on that tested server/library.
Do not invent one or extrapolate this limited observation to every Plex version/library type.
Do not route this enum through `_plex_flag_actions`, which coerces values to booleans.
The chosen direct reference update works independently of Plex's native extraction trigger.
Recommend reviewing this server-wide setting and choosing `never` to avoid native generation
replacing supplied images. Do not change it automatically or add a one-click health action.

Show chapter-specific health rows only while the app's chapter toggle is enabled. There is
no separate health-check toggle. Verify both server-wide scheduling and any per-library
chapter-generation controls before describing a conflict; do not infer chapter state from
BIF settings or invent a `generateChapterThumbs` preference without observing it.

Candidate messages, conditional on verified facts:

- Native generation enabled: **Plex also generates chapter thumbnails.**
  `Both Plex and this app may process the same videos. Review Generate chapter thumbnails
  in Plex Settings -> Library.` Show the verified current value; do not change it on toggle.
- Probe unavailable: **Could not check Plex's chapter setting.**
  `Check Plex Settings -> Library -> Generate chapter thumbnails.` Do not report healthy.
- Registration unsupported: **Chapter thumbnails are unavailable on this Plex setup.**
  Give the actual tested requirement and remediation after the proof determines it.
  Continue BIF generation.

Health probes stay read-only. Do not claim writable folders or a reachable Plex server prove
chapter playback works. Reuse verified capability information; do not generate test images
or mutate a user's library merely to render Setup Health.

The existing health UI drops `severity="info"` rows and treats unknown `ok` values as All good.
An unavailable chapter probe therefore needs `ok=False`, `severity="recommended"`, a clear
unable-to-verify message and no setting-change action. A known non-conflicting state uses
`ok=True` with the same visible severity. Conflict is advisory unless the registration proof
establishes a genuine functional requirement. Read the chapter preference from the existing
`/:/prefs` response rather than issuing a duplicate server-wide request.

The smallest actionable first version links/instructs the user to review Plex's own setting.
If a one-click `Set Plex to Never` action is added after proving that it preserves externally
generated thumbnail visibility, construct an explicit enum action. The existing `apply_flag`
dispatcher and `apply_flag_values` support string values; no new generic health endpoint is
needed. The existing action-confirm dialog must state that the change is server-wide and
affects other libraries. Saving the app's chapter toggle never changes Plex preferences.

## Validation and documentation

Verifier owns test execution. Cover disabled/default behavior; strict boolean validation;
explicit chapter query flag; no chapters versus unavailable metadata; before-indexing BIF
success; chapter-only backfill without BIF extraction; forced regeneration; stale source;
metadata-only chapter changes; multi-version selection; CPU/GPU and HDR paths; cancellation;
partial extraction with old JPEG retained; registration-only retries; retry exhaustion;
toggle-off preservation; job/UI partial outcomes; same source owned by multiple servers.

Assert meaningful downstream arguments and state transitions, not just call counts or the
existence of a mocked JPEG. Run the isolated live registration acceptance case as well as
unit/integration tests; unit tests cannot prove Plex accepts the output.

Update established user docs and regenerate `docs/llms-full.txt` with
`python scripts/generate_llms_full.py` after docs changes; verify with `--check` and
`tests/test_docs_site.py`. This design directory is excluded from the published site.
Run relevant tests and Ruff, then the risk-triggered architecture review for Plex writes,
FFmpeg, settings and concurrency. Ask before committing; do not post or merge automatically.
