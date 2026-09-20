# Phase 4 mockup pack — owner checkpoint 6

Everything phase 4 puts on screen, drawn once, before any of it is built
(`plan-phase4.md` → Task 2; spec §0 "show a mockup and get the look + wording confirmed before building").

| File | What it is |
|---|---|
| `index.html` | the pack — open it in a browser, no network needed |
| `ui-copy.md` | every new string, in the plan's UI-copy order, for Tasks 5/6/9/10 to lift verbatim |
| `shots/` | PNGs of every surface, dark and light, plus the editor at phone width |
| `shot.py` | re-shoots `shots/` — `/home/data/.venv/bin/python shot.py` |
| `vendor/` | Bootstrap 5.3.2 + Bootstrap Icons 1.11.1, the versions `base.html` loads, vendored so the pack works offline |

The pack is **not** part of the app and has no route. It pulls the app's own
`static/css/style.css` and `static/css/pages/markers_inspector.css` in by relative path, so it can't drift
from the shipped look — it does mean the pack has to stay inside the repo to render.

The data is invented: a show called *Northern Lights*, three servers, made-up times and paths.
Nothing here is wired up; buttons don't do anything and the times don't move.

## The surfaces

| # | Surface | Screenshot |
|---|---|---|
| 1 | Inspector header — `Adjust` and `Lock` beside `Re-detect` | `01-buttons.png` |
| 2 | The editor open, opening window — handles, times, keyboard | `02-editor-opening.png` |
| 3 | The editor, ending window — "runs to the end", warnings, both refusals, a type a server can't show | `03-editor-ending.png` |
| 4 | The editor at 390 px | `04-editor-phone.png` |
| 5 | Saving | `05-saving.png` |
| 6 | What each server did — updated, unreachable, off, can't show this type, Emby's credits end, the Q1 sentence | `06-server-results.png` |
| 7 | A locked marker at rest, and the Q1 fork side by side | `07-locked.png` |
| 8 | The Unlock dialog | `08-unlock.png` |
| 9 | Season view — `Edit` on every row | `09-season-edit.png` |
| 10 | Settings — the one tooltip that stops being true | `10-settings-tooltip.png` |
| 11 | Setup Health, Plex — four new rows in the existing buckets | `11-health-plex.png` |
| 11b | Setup Health on a server that isn't sending markers | `11b-health-markers-off.png` |
| 12 | Setup Health, Jellyfin and Emby — plugin missing, plugin too old | `12-health-jellyfin-emby.png` |
| 13 | Servers → Edit → Intro & Credits — the Plex marker helper, four states | `13-plex-helper.png` |

Light-theme versions carry a `-light` suffix. `00-whole-pack.png` is the whole page in one image.

Four wording questions are listed at the end of `ui-copy.md`.
