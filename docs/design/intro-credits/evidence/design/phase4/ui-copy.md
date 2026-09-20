# Phase 4 — UI copy

Every string phase 4 puts on screen, in the order of the plan's **"UI copy and mockups"** section
(`plan-phase4.md`). Tasks 5, 6, 9 and 10 lift these verbatim — if a string here and a string in the code
disagree, this file is wrong and gets fixed, not paraphrased.

Rendered, in the app's own theme: [`index.html`](index.html) · screenshots in [`shots/`](shots/).

**Status:** awaiting owner sign-off (checkpoint 6). Nothing below is built yet.

Wording rules applied: plain English, present tense, no internal setting names in the first sentence,
tooltips ≤ 120 characters.

---

## 1. The editor open

Entry points (Inspector → Intro & Credits, header row, beside the existing `Re-detect`):

| Element | String |
|---|---|
| Button | `Adjust` (icon `bi-pencil`) |
| Button ⓘ (**revised** — see §9) | `Drag the intro or credits to where they really are, or add one that wasn't found. Your times are kept from then on.` |
| Button | `Lock` (icon `bi-lock`) |
| Button ⓘ | `Keep these times exactly as they are. Later checks won't change them.` |
| Button (when locked) | `Unlock` (icon `bi-unlock`) |
| Button ⓘ (when locked) | `Let later checks set these times again.` |

While editing:

| Element | String |
|---|---|
| Banner | `Adjusting this episode. Nothing changes on your servers until you save.` |
| Banner (movie) | `Adjusting this movie. Nothing changes on your servers until you save.` |
| Per-type strip, type name | `Intro` / `Credits` / `Recap` / `Preview` |
| Field labels | `Start` · `End` |
| Length readout | `27 seconds long` (`{n} seconds long`; over 2 minutes: `2 minutes 6 seconds long`) |
| Keyboard hint | `Arrow keys move it 1 second · hold Shift for 10 seconds` |
| Keyboard hint (phone) | `Arrow keys 1 second · Shift 10 seconds` |
| Keyboard hint ⓘ | `Tab to a handle, then use the arrow keys. You can also type a time straight into the boxes.` |
| Credits/preview switch | `Runs to the end of the file` |
| That switch's ⓘ | `Turn this off when something plays after the credits — a last scene, or a preview of the next episode.` |
| Handle screen-reader label | `Intro start, 0 minutes 14 seconds` / `Intro end, 0 minutes 41 seconds` |
| Window ⓘ (unchanged) | `Zoomed to the start or end of the file, so a difference of a second is visible.` |

Action bar (one for the whole edit, under both zoom windows):

| Element | String |
|---|---|
| Prefix | `Adjusting` |
| Pending list | `Intro 0:14–0:41 · Credits 40:55–43:30` |
| Note | `Saving keeps your times — later checks won't change them.` |
| Button | `Save and publish to 3 servers` (`Save and publish to {n} server{s}`) |
| Button, no server on | `Save` |
| Button, while saving | `Saving…` |
| Button | `Cancel` |

Warnings (P-R2 — the editor warns, it does not refuse):

| Case | String |
|---|---|
| Intro under 3 s | `That's shorter than most intros. This will still be saved.` |
| Intro over 5 minutes | `That's longer than most intros. This will still be saved.` |
| Intro/recap after the first third | `That's later in the file than intros usually are. This will still be saved.` |
| Credits/preview in the first three quarters | `That's earlier than credits usually start. This will still be saved.` |
| Credits that stop before the file does | `Credits usually run to the end of the file. This will still be saved.` |

Refusals (the only two — Save stays disabled until they're fixed):

| Case | String |
|---|---|
| End at or before start | `The end has to come after the start.` |
| Past the file's length | `That's past the end of the file (44:12).` |

Type this server can't show, while editing:

| Case | String |
|---|---|
| Recap or preview, some servers can show it (editable) | `Only Jellyfin shows recaps. Plex and Emby have no recap marker, so this one won't reach them.` |
| Recap or preview, no server can show it (refused, fields disabled) | `Recaps can't be adjusted here: neither Plex nor Emby has a recap marker, and no other server has this file.` |

**Open (behaviour, not only wording).** The plan's D8 and Task 5 Step 4 say the editor *refuses* recap and preview
for Plex and Emby. Read strictly, the first row above would not exist: a recap could never be adjusted while a Plex
or Emby server has the file, even with a Jellyfin that would show it. The pack proposes editable-with-a-note for
that case and keeps the refusal for "no server can show it". If the owner wants the strict reading, D8 stands and
the first row goes; if they want the pack's, D8 and Task 5 Step 4 need the sentence rewriting (Task 4/11).

## 2. Lock and Unlock

| Element | String |
|---|---|
| Chip (Inspector, matches the Season view's existing chip) | `🔒 Locked by you` |
| Decision lane lock ⓘ | `You set these times. Later checks won't change them.` |
| Toast after Lock | `Locked — these times stay until you unlock them.` |
| Unlock dialog title | `Unlock these markers?` |
| Unlock dialog body, line 1 | `Your times stay on your servers for now:` |
| Unlock dialog body, line 2 | `The next time this episode is checked, the sources decide again and may move them. Re-detect does that straight away.` |
| Unlock dialog, keep button | `Keep them locked` |
| Unlock dialog, confirm button | `Unlock` |
| Toast after Unlock | `Unlocked — the next check decides these times again.` |

## 3. The publish result

Per-server card, while saving:

| Badge | Meaning |
|---|---|
| `Sending…` | this server is being written now |
| `Waiting` | queued behind another server |

Action bar while saving: `Your times are saved. Sending them to your servers…`

Per-server card, after saving:

| Badge | Line under it |
|---|---|
| `Updated` | `Intro 0:14–0:41 · Credits 40:55–43:30` |
| `Couldn't reach it` | `Your times are saved. This server gets them at the next Check servers run.` |
| `Intro & Credits off` (the badge the tab already uses for this server state) | `Turn on Intro & Credits for this server to send markers here.` |
| `Recap not sent` | `Emby has no recap marker. Its intro and credits were updated.` |
| `Nothing sent` | `Plex has no recap or preview marker, and nothing else changed.` |

Emby's credits end (D8 — accepted, published start-only, per-field note):

> `Your credits end wasn't sent. Emby skips to the end of the file, past any scene after the credits.`

Kept from today, unchanged: `All versions of this item share one set of markers`.

Confirmation under the cards: `Saved and locked. Your times stay until you unlock them.`

## 4. The Q1 answer in words

Recommended (option a — a marker you adjust beats the server's "keep its own" choice):

- Plex: `Replaced Plex's own marker. This server is set to keep Plex's, but a marker you adjust always wins.`
- Emby: `Replaced Emby's own marker. This server is set to keep Emby's, but a marker you adjust always wins.`
- On a locked marker before publishing (the plan lane, not the result):
  `This server is set to keep Plex's own markers. Your locked marker replaces them anyway.`

If the owner picks option b instead, the badge stays `Keeps Plex's` and the line becomes:

- `Plex keeps its own marker here, so your locked times aren't sent to this server.`

## 5. Season view

| Element | String |
|---|---|
| Row action | `Edit` |
| Row action ⓘ (**revised** — see §9) | `Adjust this episode's intro and credits, or add one that wasn't found.` |
| Existing row action, unchanged | `Review` |
| Toast, Edit on a row with no marker to drag (added in Task 6 — the mockup has no surface for it) | `Nothing to adjust on this episode yet — Re-detect checks the file now.` |

**Answered (Task 6, 2026-09-20): dropped.** `Published` was only printed when every enabled server's dot was
already green, so it repeated the dots while saying less than they do — and in an action column a status word
moves the buttons around. The reason is recorded in `plan-phase4.md` Task 6's notes.

## 6. Settings → Intro & Credits

Only one string changes (D5). The setting and its label stay as they are.

- Label, unchanged: `Never overwrite my edits`
- ⓘ **before**: `Markers you lock in the Inspector always win and are never replaced by detection. Editing and locking markers in the Inspector comes in a later update.`
- ⓘ **after**: `Markers you adjust or lock in the Inspector always win and are never replaced by detection.`

## 7. Setup Health

New section title on all three vendors: `Intro & Credits`.
A row with no button carries the shipped badge `Change in <vendor> UI` — there is no Manual chip
(`servers.js:2302`). Every `recommended`-severity row also carries the shipped `Dismiss` link
(`servers.js:2425`), passing rows included — nothing new is written for it.

### Plex

| Label | Severity | Currently → Recommended | Reason line | ⓘ |
|---|---|---|---|---|
| `Skip buttons need Plex Pass` | critical | `not active` → `active` | `Markers are still written, but nobody sees a skip button.` | `Plex only shows Skip Intro and Skip Credits to viewers on a server with Plex Pass.` |
| `Plex hasn't made its marker list yet` | critical | `missing` → `present` | `Turn on Plex's own intro detection for one library and play a file, then check again.` | `Plex builds this list the first time it finds a marker itself. This app never creates it.` |
| `Plex's library database isn't on this machine` | critical | `another machine` → `this machine` | `Run the Plex marker helper next to Plex, or run this app on the same machine as Plex.` | `Markers go straight into Plex's database, so this app must run on the Plex machine, or reach it through the helper.` |
| `Plex's own detection can replace your markers` | recommended | `On` → `Off` | `Plex settings → Library → Generate intro and credits video markers.` | `When Plex finds markers itself it overwrites ours. The next Intro & Credits run puts them back.` |

### Jellyfin

| Label | Severity | Currently → Recommended | Reason line | ⓘ |
|---|---|---|---|---|
| `Media Preview Bridge plugin` (existing row, escalated) | critical when markers are on | `not installed` → `installed` | `Without it, nothing can send markers to this server.` | `Jellyfin takes intro and credits markers only through this plugin. Installing it restarts Jellyfin.` |
| `The plugin is too old for intro and credits markers` | recommended | `1.2.0` → `1.4.0` | `Previews still work. Markers wait until it's updated.` | `This version of the plugin can't take intro and credits markers. Updating installs the newest version.` |

### Emby

Same two rows, with `Media Preview Bridge for Emby plugin` as the first label. When Emby's own catalogue
doesn't list the plugin there is no button, so the row carries `Change in Emby UI` and this reason:

> `This Emby's plugin catalogue doesn't list it. Install it by hand — the guide is in the Intro & Credits tab.`

### The one row a server gets when Intro & Credits is off (P-R6 — `severity: "recommended"`, `ok: true`)

| Label | Reason line | ⓘ |
|---|---|---|
| `Intro & Credits is off for this server` | `Nothing here is checked until you switch it on.` | `Turn it on in the Intro & Credits tab to send intro and credits markers to this server.` |

No `current` / `recommended` pair on this row: the label already says the state, and `_renderValueDiff`
drops a value the label repeats.

**Open:** because P-R6 makes this row `recommended`, the shipped card gives it a `Dismiss` link and a user can
silence it. `info` is not an option (`_partitionChecks` drops it). Leave it dismissible, or is this row one that
should always show?

## 8. Servers → Edit → Intro & Credits — the Plex marker helper

| Element | String |
|---|---|
| Row label | `Plex marker helper` |
| Row ⓘ | `A small container you run next to Plex. It is the only supported way to write markers to a Plex on another machine.` |
| Not set up | badge `Not set up` · button `Set up helper` |
| Not set up, warning | `Plex is on another machine, so markers can't be written from here.` + `Run the Plex marker helper next to Plex, or run this app on the same machine as Plex.` |
| Connected | badge `✓ Connected` · `http://plex-host.lan:9494` · `version 0.4.1` · button `Check again` |
| Unreachable | badge `Can't reach it` · button `Check again` |
| Unreachable, warning | `Markers wait here until the helper answers again. Nothing is lost.` |
| Too old | badge `Update needed` · `the helper is 0.3.0, this app needs 0.4.1` |
| Key row label | `Shared key` |
| Key row ⓘ | `The same key is set on both sides. It is never written to the logs.` |
| `How markers get here`, with a helper | `Written into this Plex server's database, through the helper next to Plex` |
| That row's ⓘ, with a helper | `The helper does the database write on the Plex machine. This app never touches the file itself.` |

Unchanged when there is no helper: `Written into this Plex server's database` and its existing ⓘ.

---

## 9. Adding a marker by hand (added 2026-09-21 — surfaces 14–18)

Today the editor can only move a marker detection already put on the timeline. When nothing was found for a type
there is nothing to drag for it, and when nothing was found **at all** `Adjust` is disabled and the Season view's
`Edit` toasts that there is nothing to adjust. Both of those are whole-file, not per-type.
These strings cover making one by hand. Everything else about it — save = lock, publish now, §5.5 rule 1, P-R2's two
bounds, a type no server can show — is the adjusted marker's behaviour unchanged, and needs no new words.

### The way in

| Element | String |
|---|---|
| Button (unchanged) | `Adjust` (icon `bi-pencil`) |
| Button ⓘ **before** | `Drag the intro or credits to where they really are, then save. Your times are kept from then on.` |
| Button ⓘ **after** | `Drag the intro or credits to where they really are, or add one that wasn't found. Your times are kept from then on.` |

Behaviour, not wording: `Adjust` stops being disabled on a file where nothing was found. It is still disabled on a
file no job has looked at, and on one whose length isn't known — neither has a timeline to put a marker on.

### The Add affordance, in the empty Decision lane

| Element | String |
|---|---|
| Button | `Add intro` / `Add credits` / `Add recap` / `Add preview` (icon `bi-plus-lg`) |
| Button ⓘ | `Puts a marker on the timeline at a starting time. Drag it to where it really is, then save.` |
| Action bar's pending list, before anything is added | `Nothing to save yet` |
| Save button, before anything is added | `Save` (disabled — the shipped "no server" label, reused) |

### The just-added marker

| Element | String |
|---|---|
| Strip note, while its times are still the starting ones | `Starting times, not something we found — drag them to where they really are.` |
| Button | `Remove` |
| Button ⓘ | `Take this one back out. Nothing has been saved yet.` |

The note goes as soon as an edge moves — it is only true while the times are untouched. `Remove` is offered only for
a marker added in this edit; a marker that came from detection is dropped with `Unlock` or `Re-detect`, as today.
Everything else in the strip is the shipped editor's: `Start`, `End`, `{n} seconds long`, the keyboard hint, the
`Runs to the end of the file` switch, the warnings and the two refusals.

### Starting times

| Type | Start | End | `Runs to the end of the file` |
|---|---|---|---|
| Intro | `0:00` | `0:30` | n/a |
| Recap | `0:00` | `0:30` | n/a |
| Credits | `max(0, length − 1 minute)` | the end of the file | on |
| Preview | `max(0, length − 30 seconds)` | the end of the file | on |

Round by design, so they can't be read as an answer: the audio and text detectors practically never produce a whole
30 seconds from 0:00, or exactly the last minute — every real answer they have written has odd seconds in it. (A
file whose *chapter* marks happen to be round is the one way the same shape turns up honestly; `sources/chapters.py`
passes the container's timestamps through as they are.) On a file of ordinary episode or film length these sit
inside the usual bounds, so the editor opens with no warning already showing. Nothing is taken from the season, the
neighbouring episodes or the sources.

The `max(0, …)` is load-bearing, not decoration: without it a file shorter than the seed gets a negative start, which
the editor's own `refusalFor()` does not catch (it checks `start >= duration`, not `start < 0`) and the API then
refuses with a 400. With the clamp, a short file gets a legal marker and the shipped warning
`That's earlier than credits usually start. This will still be saved.` — which fires below four minutes for the
credits seed and below two for the preview seed, and is what the editor shows for any credits or preview starting
before the last quarter, added or adjusted. Intro and recap need no clamp: `0:00`–`0:30` is legal on any file of at
least 30 seconds, and a shorter one is refused by the "inside the file" bound like any other marker.

### A type no server with Intro & Credits on can show

The shipped sentence with one word changed, because nothing has been put there to adjust yet:

| Case | String |
|---|---|
| Nothing found for it, and no server can show it (the Add button is on screen, disabled) | `Recaps can't be added here: neither Plex nor Emby has a recap marker, and no other server has this file.` |
| Found, and no server can show it (shipped, unchanged) | `Recaps can't be adjusted here: neither Plex nor Emby has a recap marker, and no other server has this file.` |
| Added, and some server can show it (shipped, unchanged) | `Only Jellyfin shows recaps. Plex and Emby have no recap marker, so this one won't reach them.` |

The same generated sentence with the same vendor list; only `adjusted` / `added` differs, chosen by whether that type
has a marker yet. The "no server with Intro & Credits on has this file" variant is unchanged too.

### Season view

| Element | String |
|---|---|
| Row action ⓘ **before** | `Adjust this episode's intro and credits.` |
| Row action ⓘ **after** | `Adjust this episode's intro and credits, or add one that wasn't found.` |
| Toast, unchanged | `Nothing to adjust on this episode yet — Re-detect checks the file now.` |

The toast stops firing for a row with no marker — that row opens the editor now — and survives for the row it is
still true of: a file no Intro & Credits job has looked at.

### Unchanged, and deliberately so

`Adjusting this episode. / this movie.`, `Adjusting` in the action bar, `Saving keeps your times — later checks won't
change them.`, `Save and publish to {n} servers`, every per-server result, and `Saved and locked. Your times stay
until you unlock them.` An added marker is saved, locked and published exactly like an adjusted one, so it reads
exactly like one from the moment it is on the timeline.

---

## Open wording questions for the owner

1. **Q1's sentence** (§4 above) — the recommended wording says a marker you adjust overrides a server's
   "keep its own" setting. Surface 7 of the pack shows both options side by side.
2. **`Change in Plex UI` on the database row** (§7) — that badge is right for three of the four Plex rows and
   wrong for `Plex's library database isn't on this machine`, which you fix by moving this app or running the
   helper. Leave it, or give that row a `Set up helper` button (which replaces the badge) once Task 10 lands?
3. ~~**`Published` in the Season view's action column** (§5) — keep the word beside `Edit`, or let the dots say
   it?~~ **Answered (Task 6):** dropped; the dots say it. See §5.
4. **Recap and preview on Plex and Emby** (§1) — refuse the edit outright, as D8 words it, or let it be
   adjusted and saved when some other server can show it, as the pack draws it?
5. **The P-R6 "Intro & Credits is off" row is dismissible** (§7) — fine, or should it always show?
6. **`Save and publish to 3 servers`** — the count includes only servers with Intro & Credits on that can show
   at least one type you changed. Is that the count you'd expect to read there?

### Added 2026-09-21, with §9

7. **One way in, or two** (surface 14) — the Add buttons appear only once `Adjust` has opened the editor, so there is
   one entry point and one label. Option B also puts `+ Add credits` in the read-only lane, next to
   `Credits: no markers found`, which opens the editor straight onto that type. More discoverable, one more control.
   The pack recommends one way in.
8. **`Add preview` on a movie** (surface 16) — a movie's one window covers credits and preview, so the button is
   offered there too. Almost no movie has a preview. Leave it, or leave preview out of movies?
9. **A type whose detection is switched off** — `Add preview` still appears for it, because spec §5.5 rule 1 says a
   locked user marker wins "even for a type whose detection is off". The chip above still reads
   `Preview: Detection off` (surfaces 14 and 16 both draw that pair). Right, or should a switched-off type have
   nothing to add?
