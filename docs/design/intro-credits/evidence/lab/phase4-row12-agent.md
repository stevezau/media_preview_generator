# Phase 4 lab row 12 — the Plex marker agent, two containers

The plan's row 12: *"the app container **without** the Plex config volume + the agent container **with** it; markers
published through it; a wrong token refused; a version mismatch refused; the agent stopped → Plex read-only with a
clear message."* Run on the storage lab against the claimed lab Plex 1.43.4 (`mlab-plex`); nothing on the `plex` host
was touched.

## The two containers

| Container | Image | What it can see |
|---|---|---|
| `mlab-app-remote` | `media_preview_generator:p4-agent` (this branch) | the lab media, and a `/plexcfg` volume **of its own** — `Media/localhost` and nothing else. `ls "…/Plug-in Support/Databases"` inside it fails. |
| `mlab-plex-agent` | `plex-marker-agent:lab` (this branch, 217 MB) | `mlab_plex_config` — Plex's real config volume — and nothing else. Runs as 1000:1000, the user that owns Plex's database. |

`phase4_row12_up.sh` creates both; `phase4_row12_agent.py configure` adds the lab Plex to the app and points it at
`http://mlab-plex-agent:9494` with a shared key from `./env`. The phase 1–3 `mlab-app` is untouched and keeps its
`/plexcfg` mount.

Why the app still has *a* Plex config folder: previews write BIF files into one, and the app's own config validation
requires it. A user whose Plex is on another machine has one too (usually a network mount). What they never have is a
database file they can lock — and that is the only thing the agent is for.

## Steps and results (`results/phase4-row-12.json`)

| Step | What it proves | Result |
|---|---|---|
| `isolated` | The app container cannot open Plex's database: the `Databases` folder isn't there, `mlab_plex_config` isn't mounted, and with the agent switched **off** the server reads `misconfigured — Plex database not found at …`. With it on: `ready`, showing the **agent's** path (`/plexcfg/…` inside the agent), `fs_type ext4`, `lock_holder true`. | PASS |
| `published` | An Intro & Credits job publishes through the agent: `markers_published`, `markers_written`, `taggings` rows `intro 10000–40000` and `credits 98000–120000` (the stored credits start is the served one − 2 s), and Plex's own API serves `intro 10000–40000`, `credits 100000–120000`. | PASS |
| `read_back` | Check servers reads the item back through the agent: `markers_up_to_date`, the Inspector's per-server plan `up_to_date`, rows unchanged. | PASS |
| `locked` | Server set to **Keep Plex's**, Plex's own intro row on the item (`990–29306`). The Inspector's save (`POST /api/markers/item/markers`, intro 20 s–44 s) publishes through the agent: rows become `intro 20000–44000`, the row says *"Replaced Plex's own marker. This server is set to keep Plex's, but a marker you adjust always wins."*, `replaced_own: ["intro"]`. | PASS |
| `wrong_key` | The app pointed at the agent with a key that doesn't match: `agent_unavailable`, *"refused this app's key. Set the same shared key on both sides."*, agent state `rejected`, job row `markers_skipped`, **rows unchanged**. | PASS |
| `skew` | A second agent from the same image, patched at start-up to report version `0.3.0` and protocol `99`: `agent_unavailable`, *"is version 0.3.0; this app needs 1.0.0 or newer. Update the agent container."*, agent state `incompatible`, job row `markers_skipped`, **rows unchanged**. | PASS |
| `wrong_plex` | An agent beside a *different* Plex (its own `Preferences.xml`, another `ProcessedMachineIdentifier`): `agent_unavailable`, *"is next to a different Plex server than …"*, **rows unchanged** — markers never reach the wrong database. | PASS |
| `stopped` | `docker stop mlab-plex-agent` mid-run: `agent_unavailable`, *"Can't reach the Plex marker agent at … Markers wait here until it answers again; nothing is lost."*, job row `markers_skipped`, no rows written. Start it again and the next run publishes them. | PASS |
| `cleaned_up` | The lock dropped and detection switched off, then a forced run: the rows this app left are taken off again through the agent (`taggings` empty, the part's `extra_data` back to `{"url":""}` — what Plex's own rollback leaves). | PASS |

Every refusal shows the same shape a local failure has today: a `PublishError` with a capability state, the file
reported `markers_skipped` with that message, and Plex's database untouched.

## Reset

```bash
./phase4_row12_up.sh down          # remove both containers (volumes kept)
docker volume rm mlab_app_remote_config mlab_app_remote_plexcfg
```

The lab Plex keeps whatever the last step left on the synth episode; `cleaned_up` leaves it with no marker rows.

## What this row does not cover

- **Only Plex.** Jellyfin and Emby publish over their own plugin APIs and need no agent.
- **One machine.** Both containers run on `storage` over a Docker bridge network; a real deployment crosses a LAN.
  Nothing in the path depends on the latency, and the timeouts are bounded either way (`CONNECT_TIMEOUT_S`,
  the per-request deadline, `READ_BACK_BUDGET_S`).
- **A write whose answer is lost after the agent committed.** The remote publisher declares itself non-atomic for
  exactly this case, and the unit tests pin it; reproducing it in the lab would mean killing the agent inside its
  own transaction.
- **The Edit tab's screenshots.** The agent block's wording is in `tests/`; the visible check is Task 2's pack.
