# Chapter registration evidence

Observed on the disposable `pr287-chapter-spike` Plex container, PMS
`1.43.4.10903-e5521bd8c`, with a synthetic 30-second H.264 file containing three
10-second chapters. The native automatic chapter preference was `never` except
where an arm explicitly names `scheduled` or `asap`. No production library was used.

## Registration mechanism

`registration-matrix.json` distinguishes three independent observations: metadata
chapter references, JPEG bytes on disk, and direct image endpoint status. External
JPEGs alone already answer the predictable image endpoint with HTTP 200, but Plex
omits `Chapter.thumb` until its existing chapter rows have thumbnail references.
Thus a successful direct JPEG request alone is not proof of registration.

Files-only, partial scan, metadata refresh, analyze with native generation disabled,
and the per-item chapter API with native generation disabled left references empty.
The chapter API under `scheduled` also left them empty. Under `asap`, the chapter
API and analyze registered chapters but overwrote the sentinels. Manually starting
the scheduled Butler chapter task also overwrote them; its scanner invocation
included `--force`. These are observations of this version, not promises about
every Plex release or automatic maintenance execution.

Plex's local scanner without `--force` registered existing JPEGs unchanged:

```sh
docker exec --user plex \
  --env 'PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR=/config/Library/Application Support' \
  pr287-chapter-spike '/usr/lib/plexmediaserver/Plex Media Scanner' \
  --generate --chapter-thumbs-only --item 1
```

`source-absent-isolated.json` records an additional run with the source inaccessible
inside the container. References were read before restoring the source. Restoring
its directory later caused Plex to re-import local metadata, so that later state
is not used to claim persistence. A separate same-session database comparison with
the source unreadable by the scanner proves the minimal native delta:
`native-nonforce-db-diff.json` changes **only existing `taggings.thumb_url` values**.
The compared `metadata_items`, `media_items`, `media_parts`, and `tags` rows did not
change.

## Implemented writer and cache behavior

`backend-live.json` records the actual Python gateway and agent Flask server over
real HTTP, using the same live Plex database and no mocked database or Plex calls.
The helper key existed only in process memory. Intro & Credits stayed disabled.
The experiment began with three empty chapter references, registered red/green/blue
sentinels, checked all directly served SHA-256 hashes, and primed PhotoTranscoder.
Replacing red with yellow changed the URL's content revision; PhotoTranscoder then
returned yellow `(255, 255, 0)` without restarting Plex or purging its image cache.

The ordinary pipeline additionally generated actual source frames through FFmpeg,
published BIF and chapter artifacts, repaired a missing JPEG without regenerating
BIF, repaired missing references without decoding, and replayed idempotently.
The integration harness and its separately retained pipeline results document those
application-level checks.

`pipeline-live.json` includes a real Flask manual Previews job: a missing chapter
JPEG was discovered, decoded and registered through the normal worker, and the
persisted Files API reported chapters ready while the BIF bytes and modification
time stayed unchanged. The browser followed the chapter Setup Health link to an
unavailable helper's configuration with Intro & Credits still off and its masked
shared key preserved. Actual Plex Analyze cleared chapter references; a subsequent
pipeline run repaired them without any FFmpeg extraction.

The retained developer harness `tests/integration/verify_plex_chapters.py --run`
requires this exact disposable container, mounts, unclaimed server version and
synthetic movie. Its guards reject other targets before mutation. It does not
bootstrap the fixture or claim a Plex account. Its temporary app settings contain
only the fixture's randomly generated helper key and synthetic authentication;
they are deleted after the run.

`tests/integration/verify_chapter_extraction.py` generates its own SDR, PQ and HLG
clips and runs the real configured FFmpeg. `extraction-live.json` records valid
1280×720 JPEGs, a cancelled FFmpeg process that was reaped, and a deterministic
source replacement immediately after a real decode that published no JPEG or
freshness manifest. That last case injects a race at the extraction boundary; it
does not mock FFmpeg or registration success.

`persistence.json` uses the actual source-derived pipeline images, not sentinels.
All three references and directly served hashes survived a restart of the disposable
Plex container and a partial scan of its synthetic directory. Actual metadata
re-analysis can rebuild chapter rows; registration is therefore reconciled from a
fresh snapshot on a later run rather than inferred from existing JPEGs.

Only synthetic IDs, image digests, operation results, and the narrow native column
delta are retained here. Tokens, server preferences, full logs, and database copies
are intentionally excluded. API and transformed-image proof does not by itself
establish that a particular Plex client displays its chapter menu correctly.

## Small files and multiple versions

`tiny-live.json` records a 1,530-byte MKV with three chapters. Plex's bundle identifier
is 44 characters: the decimal size followed by the file's 40-character SHA-1 digest.
The common writer registered and served all three images with this real identifier.
The DTO validates the size-bound prefix for files smaller than 64 KiB and the plain
40-character identifier for larger files; it does not accept arbitrary path text.

`versions-live.json` records two actual versions of another synthetic item, merged
through Plex's own API. Their embedded chapter starts differ: `[0, 5000, 15000]`
and `[0, 10000, 20000]`. Requests with no selector, `mediaIndex=0`, and `mediaIndex=1`
all returned the first version's global chapter map. Running Plex's native scanner
then pointed those global chapter rows at the second version's image endpoints,
while retaining the first version's times. Thus adding `mediaIndex` to this metadata
request does not establish which version owns the chapter timings. Both sources are
explicitly refused by this implementation rather than given mismatched thumbnails
or left in an endless registration retry. Supporting them requires a deliberate
policy for Plex's shared chapter set, with further client and native-behavior proof.

## Client and packaged application

`plex-web-chapter-menu.png` is a crop of the actual Plex Web chapter drawer for the
task's synthetic item on the claimed lab server. It displays the registered red,
green, and blue sentinels at 0:00, 0:10, and 0:20. The existing account token stayed
in memory. This lab advertises an address inaccessible to the browser, so only that
server's connection was transparently forwarded to its localhost endpoint. All
metadata, playback, and image responses came from the real server; none were mocked.
The crop contains no account information, library sidebar, or unrelated media.

`packaged-extraction.json` records a successful build and normal startup of the root
application image, its `/api/health` HTTP 200, and checksums matching the frozen
checkout for nine changed runtime files. The packaged Jellyfin FFmpeg 8.1.3 produced
valid 1280×720 JPEGs from SDR, PQ, and HLG fixtures. Real cancellation reaped its
child process, and replacement of the source after real decoding prevented any
chapter image from being published. The integration extraction harness accepts
`CHAPTER_PROOF_FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg` to repeat that packaged-binary
test without replacing product code inside the image.
