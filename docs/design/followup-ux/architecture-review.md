# Follow-up UX architecture review

Reviewed on 2026-10-05 against `.claude/agents/architecture-review.md`, including all eight recorded bug shapes. This independent review covers another agent's setup, operational pages, and documentation analytics changes; it does not claim independent review of the reviewer's Servers or job-results implementation.

## Resolved findings

| Severity | Finding | Resolution and regression evidence |
| --- | --- | --- |
| HIGH | Switching a resumed Emby/Jellyfin setup to Plex retained the previous server ID and could save paths to that server. | Changing vendor clears the selected server identity. Browser regression saves Plex paths and asserts the previous server receives GET requests only, with no PATCH or PUT. |
| MED | A failed Emby/Jellyfin path load left the next Plex attempt disabled; stale asynchronous restore responses could also replace a newer vendor choice. | Each current path request controls its own navigation state. Sequence guards reject stale restore/load responses, including failure handlers. Vendor-switch regression passes. |
| MED | Applying log filters erased an older-history fetch failure. | History failure state persists separately from transient filter feedback. Regression verifies the error survives filtering. |
| LOW | A relative input could become absolute through a mapping, weakening the real-file check's input contract. | Validate the original absolute POSIX, Windows-drive, or UNC path before mapping; relative and drive-relative inputs have explicit regressions. |

Verification also exposed two concrete UI defects, both fixed: raw mode repeated expanded structured details, and `Load older` stayed hidden because clearing its inline display did not override the stylesheet. The latter caused the earlier aggregate browser timeout/worker termination; it was an application visibility bug, not evidence of worker-resource instability. Both show sites now explicitly display the button.

## Security and behavior checks

- The new `POST /api/setup/preview-file-path` follows existing setup-or-auth gating and CSRF protection. Completed setup requires authentication; initial setup retains its existing access policy.
- Original and mapped paths are validated, including type, length, null bytes, parent traversal, mapping limits, and symlink escape. Real paths remain within `MEDIA_ROOT`; its existing default `/` policy is unchanged. The endpoint checks file metadata/readability without reading contents, writing files, or dispatching work.
- Resume storage contains setup selections and server identity, not access tokens. No credential persistence in local storage or new plaintext credential export was added.
- Structured logs escape content. Raw/download output preserves literal records, and filters retain error feedback. No new processing dispatch, shared lazy initialization, settings migration, or worker concurrency path was introduced.

## Verification evidence

- Initial independent backend verification: **32 passed**.
- Expanded backend and operational browser review run: **43 passed**, comprising **34 backend cases and 9 browser cases**; the remaining older-log case timed out on the hidden button described above.
- After correction, independent serial regressions for older-log failure persistence and Emby/Jellyfin-to-Plex save isolation: **2 passed in 5.77 seconds**.
- The operational owner and whole-branch verifier run the broader final matrices separately; these counts describe this review's direct test evidence, not a combined all-pass claim for the earlier aggregate run.

## Documentation analytics review

The production collector is only `https://mediapreviewgenerator.goatcounter.com/count`. Build and runtime gates restrict collection to the public production documentation origin, exclude 404 pages and frames, and use generated page paths rather than URL queries. Referrers are reduced to permitted origins without user information, path, query, or fragment. Requests omit credentials and the HTTP referrer. Only fixed installation events are collected; there is no external analytics script or client identifier.

The request parameters match GoatCounter's documented [pixel endpoint](https://www.goatcounter.com/help/pixel) and [event contract](https://www.goatcounter.com/help/events). The footer describes connection information consistently with the service's [privacy documentation](https://www.goatcounter.com/help/privacy). DNT, GPC, persistent opt-out, storage failure and origin gates are exercised by the analytics author's **10 browser tests**; real Jekyll builds verify production/nonproduction inclusion. Headless, framed-context and prerender gates were inspected in source but are not separately exercised by those tests. This reviewer inspected the controls and tests without sending production analytics events.

The parent agent separately verified private site **111927**, with Sessions and Referrer collection selected. That account configuration is parent-supplied evidence, not a setting changed by this reviewer.

## Final disposition

✅ No findings. Diff is clean against the eight bug shapes.
