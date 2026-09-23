#!/usr/bin/env python3
"""Lab proof for the final scale run's findings 1 and 2 (spec §14 2026-09-19): Plex parts in Plex's URL-encoded
extra_data, and this app's own markers read back as a server's.

Runs against the lab from ``up.sh``/``app.sh`` with the app image under test. From a worktree, set ``MLAB_DIR`` to the
lab folder that holds ``env`` and ``results/``. Raw output goes to ``results/scale-bugs/`` (git-ignored).

    ./scale_bugs.py snapshot NAME       # the URL-form parts' extra_data, their items' taggings and what Plex serves
    ./scale_bugs.py job NAME [--force]  # an Intro & Credits job over those files; its per-file rows
    ./scale_bugs.py detect-intro on|off # the app's intro detection switch (a removal path for our intro rows)
    ./scale_bugs.py emby ITEM [PATH]    # the lab Emby reader with and without our rows, against the plugin store
    ./scale_bugs.py optimize            # Plex's own "Optimize database" (the task that ran the final migration)
    ./scale_bugs.py rerun-final-migration  # forget Plex's credits final migration so the next optimize runs it again
    ./scale_bugs.py verify BEFORE AFTER  # per part between two snapshots: form, keys changed, served = decided
    ./scale_bugs.py refresh-files       # also follow parts that are URL-encoded now
    ./scale_bugs.py parts-dump NAME     # every part's extra_data
    ./scale_bugs.py probe-key add|check|remove PART  # a key of our own in one part's extra_data
    ./scale_bugs.py plex analyze|refresh|credits ITEM  # Plex's own analyze, forced refresh, forced credits detection

``rerun-final-migration`` and ``probe-key`` write the LAB Plex's database (never a real server): the first deletes one
``schema_migrations`` row, so the deferred one-time migration ``202302020000`` runs again at the next optimize, as it
did on 2026-09-17.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase1_matrix as p1  # noqa: E402

OUT = p1.RESULTS / "scale-bugs"
FILES = OUT / "files.json"
CONTAINER_PYTHON = "python3"


def save(name: str, data: object) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(p1.scrub(data), indent=1, default=str, ensure_ascii=False) + "\n")
    p1.say(f"saved {OUT / (name + '.json')}")


def url_form_files(*, refresh: bool = False) -> list[dict]:
    """The parts found in URL-encoded extra_data (kept, so later snapshots follow the same; ``refresh`` adds the ones
    in that form now)."""
    known = json.loads(FILES.read_text()) if FILES.exists() else []
    if known and not refresh:
        return known
    rows = p1.plex_db(
        "select p.id, mi.metadata_item_id, p.file from media_parts p join media_items mi on mi.id=p.media_item_id "
        "where p.extra_data glob 'ma*' or p.extra_data glob 'pv*' order by p.id"
    )
    seen = {f["part"] for f in known}
    files = known + [{"part": int(r[0]), "item": int(r[1]), "file": r[2]} for r in rows if int(r[0]) not in seen]
    OUT.mkdir(parents=True, exist_ok=True)
    FILES.write_text(json.dumps(files, indent=1) + "\n")
    p1.say(f"{len(files)} parts followed ({len(files) - len(known)} new)")
    return files


def snapshot(name: str) -> dict:
    files = url_form_files()
    parts = ",".join(str(f["part"]) for f in files)
    items = ",".join(str(f["item"]) for f in files)
    extra = {int(r[0]): r[1] for r in p1.plex_db(f"select id, extra_data from media_parts where id in ({parts})")}
    tags = p1.plex_db(
        "select t.id, t.metadata_item_id, t.text, t.time_offset, t.end_time_offset, t.[index], t.created_at, "
        f"t.extra_data from taggings t join tags g on g.id=t.tag_id where g.tag_type=12 and t.metadata_item_id in ({items}) "
        "order by t.metadata_item_id, t.text"
    )
    out = {
        "at": p1.now_iso(),
        "files": [
            {
                **f,
                "form": "url" if not (extra.get(f["part"]) or "").startswith("{") else "json",
                "extra_data": extra.get(f["part"]),
                "taggings": [r for r in tags if int(r[1]) == f["item"]],
                "served": p1.plex_served(str(f["item"])),
            }
            for f in files
        ],
    }
    forms = [f["form"] for f in out["files"]]
    p1.say(f"{len(forms)} parts: {forms.count('url')} URL-encoded, {forms.count('json')} JSON")
    save(name, out)
    return out


def job(name: str, force: bool) -> dict:
    paths = [f["file"] for f in url_form_files()]
    created = p1.start_markers_job({"file_paths": paths, "priority": "normal", "force": force, "library_name": name})
    done = p1.wait_job(created["id"], timeout=3600)
    rows = p1.job_files(created["id"])
    plex_rows = []
    for row in rows:
        for server in row.get("servers") or []:
            if server.get("id") == "mlab-plex":
                plex_rows.append((server.get("status"), server.get("message")))
    summary = {}
    for status, _message in plex_rows:
        summary[status] = summary.get(status, 0) + 1
    p1.say(f"lab Plex rows: {summary}")
    for status, message in plex_rows:
        if status not in ("markers_written", "markers_up_to_date"):
            p1.say(f"  {status}: {message}")
    save(f"job-{name}", {"job": done, "files": rows, "plex_summary": summary})
    return summary


def detect_intro(state: str) -> None:
    markers = p1.app_ok("GET", "/api/settings")["markers"]
    detect = {**markers["detect"], "intro": state == "on"}
    p1.app_ok("POST", "/api/settings", {"markers": {"detect": detect}})
    p1.say("detect:", p1.app_ok("GET", "/api/settings")["markers"]["detect"])


_EMBY_READER = """
import json, sys
from media_preview_generator.markers.sources.server_markers import read_server_markers
from media_preview_generator.servers.registry import ServerRegistry
item, path = sys.argv[1], sys.argv[2]
with open("/config/settings.json") as fh:
    reg = ServerRegistry.from_settings(json.load(fh).get("media_servers") or [])
cfg = reg.get_config("mlab-emby")
server = reg.get(cfg.id)
def show(found):
    return None if found is None else [(c.type.value, c.start_ms, c.end_ms) for c in found]
print(json.dumps({
    "evidence": show(read_server_markers(server, cfg, item, canonical_path=path)),
    "what_clients_see": show(read_server_markers(server, cfg, item, include_ours=True)),
    "plugin_store": server.get_emby_marker_state(item),
}))
"""


def emby(item: str, path: str | None = None) -> dict:
    if path is None:
        # The lab Emby sees the media at the app's own paths (up.sh).
        path = p1.emby("GET", f"/Users/{p1.ENV['EMBY_UID']}/Items/{item}")[1]["Path"]
    out = subprocess.run(
        ["docker", "exec", "-u", "1000", "mlab-app", CONTAINER_PYTHON, "-c", _EMBY_READER, item, path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(p1.scrub(out.stderr[-2000:]))
    answer = json.loads(out.stdout.strip().splitlines()[-1])
    p1.say(json.dumps(answer))
    save(f"emby-{item}", answer)
    return answer


def optimize() -> None:
    before = log_lines()
    # The Butler's task, not PUT /library/optimize: only the Butler runs pending migrations after the optimize (lab
    # log: the manual optimize at 2026-09-19 11:32 UTC ran none).
    status, _ = p1.plex("POST", "/butler/OptimizeDatabase")
    p1.say(f"POST /butler/OptimizeDatabase -> {status}")
    p1.wait_until(
        "Plex to optimize", lambda: "Database optimization: complete" in log_tail(before), timeout=900, every=5
    )
    time.sleep(30)
    lines = [
        line
        for line in log_tail(before).splitlines()
        if "migration" in line.lower() or "CreditsFinalAttributeMigration" in line or "optimiz" in line.lower()
    ]
    for line in lines:
        p1.say(line[:220])
    save("optimize-log", {"lines": lines})


_LOG = "/config/Library/Application Support/Plex Media Server/Logs/Plex Media Server.log"


def log_lines() -> int:
    return int(p1.sh("docker", "exec", "mlab-plex", "wc", "-l", _LOG).split()[0])


def log_tail(after_line: int) -> str:
    return p1.sh("docker", "exec", "mlab-plex", "tail", "-n", f"+{after_line + 1}", _LOG)


def rerun_final_migration() -> None:
    before = p1.plex_db("select rowid, version from schema_migrations where version='202302020000'")
    p1.sh(str(HERE / "plexdb.sh"), "delete from schema_migrations where version='202302020000'")
    after = p1.plex_db("select rowid, version from schema_migrations where version='202302020000'")
    p1.say(f"lab Plex schema_migrations 202302020000: {before} -> {after}")


def verify(before_name: str, after_name: str) -> dict:
    """Per part: its form, which keys changed between two snapshots, and whether Plex serves the app's decision."""
    sys.path.insert(0, str(HERE.parents[4]))
    from media_preview_generator.markers.publishers.plex_db import decode_extra_data  # noqa: PLC0415

    before = {f["part"]: f for f in json.loads((OUT / f"{before_name}.json").read_text())["files"]}
    after = json.loads((OUT / f"{after_name}.json").read_text())["files"]
    rows, counts = [], {}
    for f in after:
        if f["part"] not in before:
            continue
        old_fields, old_url = decode_extra_data(before[f["part"]]["extra_data"])
        new_fields, new_url = decode_extra_data(f["extra_data"])
        changed = sorted(k for k in set(old_fields) | set(new_fields) if old_fields.get(k) != new_fields.get(k))
        decided = p1.item_payload(f["file"])
        wanted = sorted(
            (d["marker"]["type"], d["marker"]["start_ms"], d["marker"]["end_ms"])
            for d in (decided.get("decisions") or {}).values()
            if d.get("status") == "decided" and d.get("marker") and d["marker"]["type"] in ("intro", "credits")
        )
        served = sorted((m["type"], m["start"], m["end"]) for m in f["served"])
        row = {
            "part": f["part"],
            "form": f"{'url' if old_url else 'json'} -> {'url' if new_url else 'json'}",
            "keys_changed": changed,
            "final_flag_removed": "%2C%22final%22%3A1" in (before[f["part"]]["extra_data"] or "")
            and "%2C%22final%22%3A1" not in (f["extra_data"] or ""),
            "served_is_decided": served == wanted,
            "served": served,
            "decided": wanted,
        }
        rows.append(row)
        for key in ("form", "served_is_decided", "final_flag_removed"):
            label = f"{key}={row[key]}"
            counts[label] = counts.get(label, 0) + 1
        counts[f"keys_changed={','.join(changed) or 'none'}"] = (
            counts.get(f"keys_changed={','.join(changed) or 'none'}", 0) + 1
        )
    p1.say(json.dumps(counts, indent=1))
    for row in rows:
        if not row["served_is_decided"]:
            p1.say(f"  part {row['part']}: served {row['served']} decided {row['decided']}")
    save(f"verify-{before_name}-{after_name}", {"counts": counts, "parts": rows})
    return counts


PROBE_KEY = "zz:probe"


def part_extra(part: int) -> str:
    return p1.plex_db(f"select extra_data from media_parts where id={int(part)}")[0][0]


def probe_key(action: str, part: int) -> None:
    """Add, check or remove a key of our own in one lab part's extra_data (the Plex option in spec §13 item 17)."""
    sys.path.insert(0, str(HERE.parents[4]))
    from media_preview_generator.markers.publishers.plex_db import decode_extra_data, encode_extra_data  # noqa: PLC0415

    fields, url_form = decode_extra_data(part_extra(part))
    if action == "check":
        p1.say(f"part {part}: {'URL-encoded' if url_form else 'JSON'}, {PROBE_KEY}={fields.get(PROBE_KEY)!r}, "
               f"keys {sorted(fields)}")  # fmt: skip
        return
    if action == "add":
        fields[PROBE_KEY] = "1"
    else:
        fields.pop(PROBE_KEY, None)
    new = encode_extra_data(fields, url_form=url_form).replace("'", "''")
    p1.sh(str(HERE / "plexdb.sh"), f"update media_parts set extra_data='{new}' where id={int(part)}")
    probe_key("check", part)


def plex_rewrite(what: str, item: str) -> None:
    """One of Plex's own operations on an item, then wait until Plex is idle."""
    if what == "analyze":
        p1.plex("PUT", f"/library/metadata/{item}/analyze")
    elif what == "refresh":
        p1.plex("PUT", f"/library/metadata/{item}/refresh", force=1)
    elif what == "credits":
        p1.plex("PUT", f"/library/metadata/{item}/credits", force=1)
    p1.plex_wait_idle(min_wait=10, timeout=900)
    p1.say(f"Plex {what} on item {item}: served {p1.plex_served(item)}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "snapshot":
        snapshot(rest[0])
    elif command == "job":
        job(rest[0], "--force" in rest)
    elif command == "detect-intro":
        detect_intro(rest[0])
    elif command == "emby":
        emby(rest[0], rest[1] if len(rest) > 1 else None)
    elif command == "optimize":
        optimize()
    elif command == "rerun-final-migration":
        rerun_final_migration()
    elif command == "parts-dump":
        rows = p1.plex_db("select id, extra_data from media_parts order by id")
        save(f"parts-{rest[0]}", {r[0]: (r[1] if len(r) > 1 else "") for r in rows})
    elif command == "refresh-files":
        url_form_files(refresh=True)
    elif command == "verify":
        verify(rest[0], rest[1])
    elif command == "probe-key":
        probe_key(rest[0], int(rest[1]))
    elif command == "plex":
        plex_rewrite(rest[0], rest[1])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
