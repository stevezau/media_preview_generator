# Plex marker agent

A small container you run **next to Plex**, so Media Preview Generator can send intro and credits markers to a Plex
that runs on a different machine.

You only need it in that one case. If the app and Plex already run on the same machine, don't run this — the app
writes the markers itself.

## Why it has to exist

Plex has no API for intro and credits markers. Markers live in Plex's own library database, and the app writes them
straight into it — the same rows Plex writes. SQLite's shared-file locking (WAL) only works between processes on the
same machine: over NFS, SMB or a Docker Desktop share it is not safe, and the app refuses to try
("Plex's database is on a network share… Plex stays read-only").

So the app needs something *on the Plex machine* to do the write. That is this. It runs the app's own writer
(`media_preview_generator/markers/publishers/plex_db.py`) on the Plex host, and the app asks it to.

**It is the only supported way to write markers to a Plex on another machine.** Without it, that Plex stays
read-only for markers, with the message it has today.

## What it may touch, and what it will not

It touches exactly one file: `Plug-in Support/Databases/com.plexapp.plugins.library.db` inside the Plex config
folder you mount into it. Within that file it only ever writes the marker rows of one item at a time
(`taggings` on Plex's marker tag, and `pv:intros` / `pv:credits` in that item's `media_parts.extra_data`).

It also reads `Preferences.xml` in that folder, for one string: Plex's own machine identifier. The app compares
it with the identifier its Plex reports, so an agent set up beside the *wrong* Plex is refused before anything is
written.

It will not:

- take a database path from the app — the path comes from this container's own `PLEX_CONFIG_DIR`;
- run any SQL the caller supplies, or answer anything else about your library;
- create Plex's marker tag row (if Plex hasn't made one yet, the write is refused and the app tells you to run
  Plex's own detection once);
- write a database that isn't on a local disk **on this machine**, or one whose schema it doesn't recognise;
- do anything at all without the shared key.

It refuses exactly what the in-process writer refuses, because it *is* the in-process writer.

## Running it

`docker-compose.yml` is in this folder; everything that has to match your machine lives in `.env` beside it.
`AGENT_TOKEN` and `PLEX_CONFIG` have no default — leave either out and compose stops with a message naming it,
instead of starting an agent with nothing mounted. `PUID`/`PGID` do fall back to `1000`, so set them if Plex runs
as anyone else: an agent running as the wrong user starts and reports healthy, then writes no markers at all.

```bash
{
  printf 'AGENT_TOKEN=%s\n' "$(openssl rand -hex 24)"
  printf 'PLEX_CONFIG=%s\n' /path/to/plex/config
  printf 'PUID=%s\nPGID=%s\n' 1000 1000
} > .env
chmod 600 .env
docker compose up -d
```

Then paste the address (`http://<this-machine>:9494`) and the same key into the app:
**Servers → your Plex → Edit → Intro & Credits → Plex marker agent**.

Without compose — note `--env-file .env`, so the key comes out of the file you just wrote rather than an empty
shell variable (an empty `AGENT_TOKEN` makes the agent exit on start and the container restart in a loop):

```bash
# --user takes no .env value here; put the uid:gid that owns Plex's database in it yourself.
docker run -d --name plex-marker-agent --restart unless-stopped \
  --user 1000:1000 \
  --env-file .env \
  -e PLEX_CONFIG_DIR="/plex/Library/Application Support/Plex Media Server" \
  -v /path/to/plex/config:/plex \
  -p 9494:9494 \
  --security-opt no-new-privileges:true --cap-drop ALL \
  ghcr.io/stevezau/plex-marker-agent:1.0.0
```

| Setting | What it is |
|---|---|
| `AGENT_TOKEN` | The key both sides share. Required — the agent won't start without one. Never logged. |
| `PLEX_CONFIG` | Plex's config folder **on this host** (compose only; the `-v` source above). Required. |
| `PLEX_CONFIG_DIR` | Plex's config folder as **this container** sees it. The database path is derived from it. |
| `AGENT_PORT` | Port to listen on (default 9494). It moves what the agent binds *inside* the container, so the published port has to move with it: compose derives the mapping from this one value, but a hand-written `docker run` needs its `-p` changed to match. |
| `AGENT_BIND` | Host address compose publishes on (default `0.0.0.0`, all interfaces). Set it to the one LAN address the app reaches this machine on if this host has several. |
| `PUID` / `PGID` | The user that owns Plex's database (the same values Plex's own container runs as). Defaults to `1000`. The agent is non-root and can't write the database as anyone else. |

Three things to keep in mind:

- **Same machine as Plex, and the same path.** The agent proves Plex has the database open through the very file it
  sees; a copy, a snapshot or a different mount of the same folder is refused, not written.
- **One container per Plex**, and one worker inside it (the image is built that way). Two writers would break the
  "one writer at a time" guarantee the app relies on.
- **The key is all that stands between a caller and Plex's database.** Keep the port on your LAN; don't publish it
  to the internet.

## Building it

From the repository root (it needs the app's source, which is what it runs):

```bash
docker build -f plex-marker-agent/Dockerfile -t plex-marker-agent:dev .
```

The image carries no ffmpeg, no GPU drivers and none of the app's media libraries: the app's wheel is installed with
`--no-deps`, and `requirements.txt` holds the handful of libraries it really needs — each line copied from the app's
`pyproject.toml`, with a test that fails if one drifts.

`requirements.txt` is the hand-written input; `requirements.lock.txt` is what the image actually installs. It pins
the whole closure (the transitive libraries too) at one version each with `--generate-hashes`, and the Dockerfile
installs it with `--require-hashes`, so two builds of one tag are the same image rather than whatever PyPI happened
to serve that day. The base image is pinned by digest for the same reason. Regenerate the lock after editing
`requirements.txt` — from the repository root:

```bash
docker run --rm -v "$PWD/plex-marker-agent:/w" -w /w python:3.12-slim-bookworm \
  sh -c 'pip install pip-tools && pip-compile --generate-hashes --strip-extras --no-header \
           --output-file=requirements.lock.txt requirements.txt'
```

CI builds this image, boots it and calls it on every pull request (`marker-agent-image` in `.github/workflows/ci.yml`),
so a new module-scope import under `markers/publishers/` that the `--no-deps` install can't satisfy fails a PR
instead of a release. Releases are cut by pushing a `marker-agent-v<version>` tag, which runs
`.github/workflows/marker-agent.yml`.

## Versions

The agent has its own version (`AGENT_VERSION`, the image tag) and implements one or more numbered **protocols** —
the shape of the requests and answers. The app sends the protocol it speaks on every request and reads the agent's
version and protocols off every answer, so a mismatch is caught on the first call, in either direction:

- the agent is older than the app's `MIN_AGENT_VERSION` → refused, naming both versions and saying to update the
  agent ("the agent is 0.3.0, this app needs 1.0.0 or newer");
- the agent has moved past the protocol the app speaks → refused, naming both protocols and saying to update the
  **app** (updating the agent again would send you in a circle);
- either way the app shows **Update needed** on the Edit tab and **writes nothing**. Markers wait for the next run.

A new protocol number is only ever *added* to the agent, never swapped, so an agent can serve an older app while you
update them one at a time. Update the agent first: a newer agent still speaks the older app's protocol.

An optional field added to a request doesn't change the protocol: an older agent ignores it and behaves as before.
**Needs the next agent release:** the file's limits a write now carries (`limits`, the file's duration). With them
"Keep Plex's" drops a Plex marker that can't be right for the file, such as credits that start after it ends, and
writes ours. Agent 1.0.0 ignores them and keeps such markers until it is updated.

**Needs the next agent release too:** the item read now answers `stale_types` (the types whose markers Plex made for
an earlier file at the path, from `taggings.created_at`, `media_parts.updated_at` and the parts' `pv:` records) and
each part's `updated_at`. Agent 1.0.0 leaves both out, which the app reads as "can't tell": every Plex marker counts
as before, and its "Keep Plex's" keeps stale markers even where the app has an answer of its own.

`AGENT_VERSION` in `plex_marker_agent.py` is the one place that version is decided. The image tag in
`docker-compose.yml` and in the `docker run` above are copies of it that a human has to read, so a test
(`TestTheImageStaysInStepWithTheApp`) fails when either drifts from it, and the release workflow refuses a
`marker-agent-v<version>` tag whose version isn't the one the code reports.

## The contract

Everything is `POST` with a JSON body, except `/v1/health` and `/v1/ping`. Every request carries
`Authorization: Bearer <AGENT_TOKEN>` and `X-Marker-Agent-Protocol: 1`; every answer carries
`{"agent": {"version", "protocols"}}`.

| Endpoint | What it does | Answers |
|---|---|---|
| `GET /v1/health` | Liveness for the container's health check. No key, nothing about your library. | `{"ok": true, "agent": …}` |
| `GET /v1/ping` | A shell check with curl: the version, which Plex this agent serves, and whether the database file is there. Needs the key but no protocol header, so it still answers when the two sides disagree. The app doesn't call it — "Check again" runs the two checks below. | `result.db_present`, `result.machine_identifier` |
| `POST /v1/checks/file` | Is the database found, on a local disk, writable, and open in Plex's process? Also which Plex this is. | `result.report` (a capability report), `result.machine_identifier` |
| `POST /v1/checks/db` | Is it the tested schema, is its marker data readable, is Plex's marker tag row there? | `result.report` |
| `POST /v1/item/read` | One item's live parts (schema and tag row checked first). | `result.item` |
| `POST /v1/item/write` | Write one item's markers, in one transaction. | `result.write` |
| `POST /v1/items/shown` | What items show of what the app left on them (a read-back). | `result.batch` |
| `POST /v1/item/exists` | Does this Plex still have that item? | `result.exists` |

Status codes: `200` a result, `400` a body the agent can't read, `401` no key or the wrong key (and nothing else —
an unauthenticated caller learns nothing about this Plex), `409` a refusal the app re-raises as its own error (a
network filesystem, an unknown schema, a missing marker tag row, Plex busy) or a protocol it doesn't speak, `413` a
body over 2 MB.

The payload shapes live in `media_preview_generator/markers/publishers/plex_remote.py` — one codec, used by both
sides, so they can't disagree about a field.

## When something is wrong

| What you see | What it means |
|---|---|
| **Can't reach it** | The agent isn't running, or the address or port is wrong. Markers wait; nothing is lost. |
| **Key refused** | The keys don't match. Set the same `AGENT_TOKEN` on both sides. |
| **Update needed** | The app and the agent are different versions. The message says which side to update. |
| "is next to a different Plex server" | The address points at the agent of another Plex. Both sides compare Plex's own machine identifier, so markers never go into the wrong database. |
| "Plex's database is on a network share" | The *agent's* mount is a network share. It has to be a local disk on the Plex machine. |
| "Plex doesn't have its database open through this folder right now" | Plex is stopped, or the agent has a different path to the file than Plex does. |
| "Plex hasn't created its marker tag yet" | Run Plex's own intro or credits detection once on any item. The agent never creates that row. |

`docker logs plex-marker-agent` shows what it did — one line per item written. It never logs the key.
