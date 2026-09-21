#!/usr/bin/env python3
"""Phase 4 lab matrix for Intro & Credits (plan-phase4 Task 13): the marker editor, locks and Setup Health rows.

    ./phase4_matrix.py configure              phase 3 configure (five servers, every synth library)
    ./phase4_matrix.py run 1 2 3 ...          run rows in the given order
    ./phase4_matrix.py rows                   list the rows

Rows drive the real HTTP API (`POST` / `DELETE /api/markers/item/markers`) and then read every server's own state
independently: Plex's API and its database (read-only queries), Jellyfin's `MediaSegments`, Emby's chapters, and
markers.db read straight from the file. Every assertion compares exact times. Each row writes `results/p4-row-NN.json`
(git-ignored) with its premise, checks and evidence; credentials are scrubbed. A row whose premise doesn't hold fails
whatever its checks say, and a row that raises is recorded as a failure with the error. Rows put lab state back in
`finally`. Row 12 is `phase4_row12_agent.py`, run in full. MLAB_DIR sets the lab folder holding env, synth/ and
results/; MLAB_SHOTS where screenshots go; MLAB_APP_IMAGE the app image; MLAB_AGENT_IMAGE the agent image.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import phase1_matrix as p1
import phase2_matrix as p2
import phase3_matrix as p3  # importing it adds the Synth Credits library to phase 2's configure
from phase1_matrix import app_ok, now_iso, say, scrub, sh, wait_until
from plex_inject import part_extra

HERE = Path(__file__).resolve().parent
RESULTS = p1.RESULTS
SERVERS = p2.ALL_MARKER_SERVERS
JELLYFINS = tuple(p1.JELLYFINS)
EMBYS = p2.EMBY_SERVERS
TICKS_PER_MS = p1.TICKS_PER_MS
JELLYFIN_TYPES = {"Intro": "intro", "Outro": "credits", "Recap": "recap", "Preview": "preview"}
UNLOCK_ALL = ["intro", "credits", "recap", "preview"]
# A user's edits, chosen to be off every whole second and different from every synth episode's chapters (10-40 s,
# 17-47 s, 25-55 s; credits 100-120 s), so a server showing the chapter answer can't pass for the edit.
INTRO_EDIT = (12_500, 41_250)
CREDITS_EDIT = (103_250, 118_500)
# What Plex's database stores for our credits: the served start minus 2 s (phase4-row12-agent.md, `published`).
PLEX_CREDITS_STORED_OFFSET_MS = 2_000
FAR_INTRO, FAR_CREDITS = (1_000, 2_000), (110_000, 115_000)  # far from every synth chapter answer
SAVE_BOUND_S = 20  # one server down costs its 8 s publish deadline (pipeline.PUBLISH_NOW_SERVER_TIMEOUT_S)

Time = tuple[int, int | None]
Served = dict[str, list[tuple[str, int, int | None]]]


class PremiseError(RuntimeError):
    """The lab wasn't in the state a row needs, so what the row checks wouldn't mean anything."""


# ------------------------------------------------------------------------------------------------------ results


def write_result(row: int, title: str, result: str, evidence: dict, notes: list[str] | None = None) -> dict:
    """Write one row's result file and say how it went.

    Args:
        row: The row number.
        title: The row's title.
        result: ``pass``, ``fail`` or ``fail (premise)``.
        evidence: What the row read, merged into the file.
        notes: Lines printed under the result.

    Returns:
        The written body.
    """
    RESULTS.mkdir(exist_ok=True)
    body = scrub({"row": row, "title": title, "result": result, "at": now_iso(), "notes": notes or [], **evidence})
    (RESULTS / f"p4-row-{row:02d}.json").write_text(json.dumps(body, indent=2, default=str) + "\n")
    say(f"p4 row {row}: {result} — {title}")
    for note in notes or []:
        say(f"  - {note}")
    return body


def checks_result(
    row: int,
    title: str,
    premise: dict[str, bool],
    checks: dict[str, bool],
    evidence: dict,
    notes: list[str] | None = None,
) -> dict:
    """Pass when the premise and every check hold; a failed premise is "fail (premise)", never a pass.

    Args:
        row: The row number.
        title: The row's title.
        premise: What had to be true for the checks to mean anything.
        checks: The row's own checks.
        evidence: What the row read.
        notes: Extra lines for the result file.

    Returns:
        The written body.
    """
    lines = [f"premise {k}: {v}" for k, v in premise.items()] + [f"{k}: {v}" for k, v in checks.items()] + (notes or [])
    if not all(premise.values()):
        result = "fail (premise)"
    else:
        result = "pass" if all(checks.values()) else "fail"
    return write_result(row, title, result, {"premise": premise, "checks": checks, **evidence}, lines)


ROWS: dict[int, Callable[[], dict]] = {}


def row(number: int) -> Callable:
    """Register a row. An exception inside it becomes a recorded failure instead of ending the run."""

    def register(fn: Callable[[], dict]) -> Callable[[], dict]:
        def guarded() -> dict:
            try:
                return fn()
            except Exception as exc:
                trace = "".join(traceback.format_exception(exc))[-3000:]
                return write_result(number, (fn.__doc__ or fn.__name__).strip().splitlines()[0], "fail",
                                    {"error": trace}, [f"raised {type(exc).__name__}: {scrub(str(exc))[:300]}"])  # fmt: skip

        guarded.__doc__ = fn.__doc__
        ROWS[number] = guarded
        return guarded

    return register


# ------------------------------------------------------------------------------------------------------ servers


_IDS: dict[tuple[str, str], str] = {}


def item_id(server_id: str, path: str) -> str:
    """One server's own id for the item behind a file (cached: ids don't change while a row runs).

    Args:
        server_id: A lab server id.
        path: The file as the app sees it.

    Returns:
        The id Plex, Jellyfin or Emby uses for the item.
    """
    key = (server_id, path)
    if key not in _IDS:
        if server_id == "mlab-plex":
            _IDS[key] = p2.plex_item(path)
        elif server_id in JELLYFINS:
            _IDS[key] = p1.jf_items(server_id)[path]["id"]
        else:
            _IDS[key] = p2.emby_items(server_id)[path]["id"]
    return _IDS[key]


def served_on(server_id: str, path: str) -> list[tuple[str, int, int | None]]:
    """What one server itself serves for a file, as ``(type, start_ms, end_ms)``, Emby's credits with no end.

    Args:
        server_id: A lab server id.
        path: The file as the app sees it.

    Returns:
        The markers, sorted.
    """
    ident = item_id(server_id, path)
    if server_id == "mlab-plex":
        return sorted((m["type"], m["start"], m["end"]) for m in p1.plex_served(ident))
    if server_id in JELLYFINS:
        return sorted(
            (JELLYFIN_TYPES[s["type"]], s["start_ticks"] // TICKS_PER_MS, s["end_ticks"] // TICKS_PER_MS)
            for s in p1.jf_segments(server_id, ident)
        )
    _, data = p2.emby_call(server_id, "GET", f"/Items?Ids={ident}&Fields=Chapters")
    marks = {m["type"]: m["ms"] for m in p2.emby_marks(data["Items"][0].get("Chapters") or [])}
    out: list[tuple[str, int, int | None]] = []
    if "IntroStart" in marks and "IntroEnd" in marks:
        out.append(("intro", marks["IntroStart"], marks["IntroEnd"]))
    if "CreditsStart" in marks:
        out.append(("credits", marks["CreditsStart"], None))
    return sorted(out)


def served(path: str) -> Served:
    """What every server itself serves for a file (each read from that server, not from the app).

    Args:
        path: The file as the app sees it.

    Returns:
        ``server id -> markers``.
    """
    return {sid: served_on(sid, path) for sid in SERVERS}


def expected_on(server_id: str, intro: Time | None = None, credits: Time | None = None) -> list[tuple]:
    """What one server should serve for an intro and a credits marker: Emby's credits have no end (D8).

    Args:
        server_id: A lab server id.
        intro: ``(start_ms, end_ms)`` or None.
        credits: ``(start_ms, end_ms)`` or None.

    Returns:
        The markers, sorted, in ``served_on``'s shape.
    """
    out: list[tuple[str, int, int | None]] = []
    if intro is not None:
        out.append(("intro", intro[0], intro[1]))
    if credits is not None:
        out.append(("credits", credits[0], None if server_id in EMBYS else credits[1]))
    return sorted(out)


def expected(intro: Time | None = None, credits: Time | None = None) -> Served:
    """``expected_on`` for every server.

    Args:
        intro: ``(start_ms, end_ms)`` or None.
        credits: ``(start_ms, end_ms)`` or None.

    Returns:
        ``server id -> markers``.
    """
    return {sid: expected_on(sid, intro, credits) for sid in SERVERS}


def plex_stored_rows(path: str) -> list[tuple[str, int, int]]:
    """Plex's own ``taggings`` rows for the file's item (read-only query on the lab Plex's database).

    Args:
        path: The file as the app sees it.

    Returns:
        ``(text, start, end)`` per row, sorted.
    """
    item = item_id("mlab-plex", path)
    return sorted((r["text"], int(r["start"]), int(r["end"])) for r in p1.plex_marker_rows() if r["item"] == item)


def stored_times(path: str) -> dict[str, tuple[int, int, int]]:
    """markers.db's ``(start_ms, end_ms, locked)`` per type for the file.

    Args:
        path: The file as the app sees it.

    Returns:
        ``type -> (start_ms, end_ms, locked)``.
    """
    return {t: (r["start_ms"], r["end_ms"], r["locked"]) for t, r in p1.stored_markers(path).items()}


def synth(episode: int) -> str:
    """A Synth Chapters episode's path."""
    return p1.synth_path(episode)


def truth(episode: int) -> tuple[Time, Time]:
    """What synth_chapters.sh wrote as an episode's chapters: ``(intro, credits)``."""
    i_start, i_end, c_start, c_end = p1.SYNTH_TRUTH[episode]
    return (i_start, i_end), (c_start, c_end)


def run_job(path: str, label: str, *, force: bool = False) -> tuple[dict, list[dict]]:
    """A markers job over one file, waited for.

    Args:
        path: The file as the app sees it.
        label: Goes in the job's name.
        force: Force re-detection.

    Returns:
        The finished job and its Files rows.
    """
    return p2.run_job({"file_paths": [path], "library_name": f"Phase 4 {label}", "force": force})


def server_status(files: list[dict], path: str, server_id: str) -> str:
    """One server's status in a job's Files list for a file (``""`` when absent)."""
    return p2.server_row(files, path.rsplit("/", 1)[-1], server_id).get("status", "")


def save_result(body: dict | None, server_id: str) -> dict:
    """One server's row in a save's response (``{}`` when the response has none)."""
    return next((s for s in (body or {}).get("servers", []) if s["server_id"] == server_id), {})


def marker(mtype: str, span: Time | None) -> dict:
    """One entry of a save body."""
    return {"type": mtype, "start_ms": span[0], "end_ms": span[1]}


def save(path: str, intro: Time | None = None, credits: Time | None = None) -> tuple[int, dict, float]:
    """The Inspector's Save with an intro and/or a credits marker.

    Args:
        path: The file as the app sees it.
        intro: ``(start_ms, end_ms)`` or None to leave the intro out.
        credits: ``(start_ms, end_ms)`` or None to leave the credits out.

    Returns:
        The status, the body and how many seconds the request took.
    """
    entries = [marker(t, span) for t, span in (("intro", intro), ("credits", credits)) if span is not None]
    return p1.save_markers(path, entries)


def all_results(body: dict | None) -> dict[str, str]:
    """``server id -> result`` of a save's response."""
    return {s["server_id"]: s["result"] for s in (body or {}).get("servers", [])}


def set_settings() -> None:
    """The settings every row starts from: publish at High, Keep Plex's and Keep Emby's off, Plex's detection off."""
    p2.set_publish_when("high")
    p1.set_redetect("restore")
    for server_id in EMBYS:
        set_keep_emby(server_id, keep=False)


def set_keep_emby(server_id: str, *, keep: bool) -> None:
    """Switch an Emby server's "Keep Emby's" on or off.

    Args:
        server_id: An Emby lab server id.
        keep: True for ``keep_emby``, False for ``restore``.
    """
    mode = "keep_emby" if keep else "restore"
    app_ok("PUT", f"/api/servers/{server_id}", {"markers": {"emby": {"on_emby_redetect": mode}}})
    stored = app_ok("GET", f"/api/markers/servers/{server_id}/status")["settings"]
    if stored["emby"]["on_emby_redetect"] != mode:
        raise PremiseError(f"{server_id} kept on_emby_redetect {stored['emby']['on_emby_redetect']!r}, wanted {mode!r}")


def settle(path: str, episode: int) -> None:
    """Put a synth episode back to the answer its chapters give, on every server, and check that it took.

    Plex keeps the markers this app already left on an item when they agree with the decision within its version
    tolerance, so a run over rows only milliseconds off the chapters' answer would leave them. When that happens the
    episode is first moved far away with a save, so the run that follows has to write the chapters' answer exactly.

    Args:
        path: The file as the app sees it.
        episode: Its number in Synth Chapters.

    Raises:
        PremiseError: A server still serves something else afterwards.
    """
    intro, credits = truth(episode)
    want = expected(intro, credits)

    def rerun() -> Served:
        p1.unlock_markers(path, UNLOCK_ALL)
        drop_markers(path)
        run_job(path, "settle", force=True)
        return served(path)

    got = rerun()
    if got != want:
        save(path, FAR_INTRO, FAR_CREDITS)
        got = rerun()
    if got != want:
        raise PremiseError(f"episode {episode} did not settle: served {got}, wanted {want}")


# ------------------------------------------------------------------------------------------------------- rows


@row(1)
def row_01_save_publishes_and_locks() -> dict:
    """Adjust + Save: every owner shows the new times before the request returns; markers.db has locked=1."""
    ep = synth(1)
    set_settings()
    settle(ep, 1)
    before = served(ep)
    try:
        status, body, seconds = save(ep, INTRO_EDIT, CREDITS_EDIT)
        after = served(ep)  # read right after the response: nothing waited for
        plex_rows = plex_stored_rows(ep)
        stored = stored_times(ep)
        payload = p1.item_payload(ep)
        again_status, again, _ = save(ep, INTRO_EDIT, CREDITS_EDIT)
        rejected = {
            "end before start": save(ep, (40_000, 30_000), None),
            "past the end of the file": save(ep, (10_000, 130_000), None),
            "starts at the end of the file": save(ep, None, (120_008, 120_009)),
            "not a whole number": p1.app("POST", "/api/markers/item/markers", {"path": ep, "markers": [
                {"type": "intro", "start_ms": "12.5", "end_ms": 41_250}]}),
            "outside every library": p1.save_markers("/etc/passwd", [marker("intro", INTRO_EDIT)]),
        }  # fmt: skip
        after_rejected = {"served": served(ep), "stored": stored_times(ep)}
    finally:
        cleanup_errors = p1.run_cleanup(lambda: settle(ep, 1))
    premise = {"before the save every server served the chapters' answer": before == expected(*truth(1))}
    checks = {
        "the save is a 200": status == 200,
        "it answered inside the bound": seconds < SAVE_BOUND_S,
        "every server reports written": all_results(body) == dict.fromkeys(SERVERS, "written"),
        "no server reports a replaced marker of its own": all(s["replaced_own"] == [] for s in body["servers"]),
        "the response lists both markers locked at the edited times": {
            t: (m["start_ms"], m["end_ms"], m["locked"]) for t, m in body["markers"].items()
        }
        == {"intro": (*INTRO_EDIT, True), "credits": (*CREDITS_EDIT, True)},
        **{f"{sid} serves the edit before the request returned": after[sid] == expected_on(sid, INTRO_EDIT, CREDITS_EDIT)
           for sid in SERVERS},
        "Plex's database rows are the edit (credits start is the served start minus 2 s)": plex_rows
        == [
            ("credits", CREDITS_EDIT[0] - PLEX_CREDITS_STORED_OFFSET_MS, CREDITS_EDIT[1]),
            ("intro", *INTRO_EDIT),
        ],
        "markers.db holds both rows with locked=1 at the edited times": stored
        == {"intro": (*INTRO_EDIT, 1), "credits": (*CREDITS_EDIT, 1)},
        "the Inspector says locked by the user": all(
            payload["decisions"][t]["marker"]["locked"] is True and payload["decisions"][t]["reason"] == "locked by user"
            for t in ("intro", "credits")
        ),
        "the same save again is a 200 and changes nothing anywhere": again_status == 200
        and all_results(again) == dict.fromkeys(SERVERS, "unchanged"),
        "an end before the start is refused with 400": rejected["end before start"][0] == 400
        and "has to end after it starts" in rejected["end before start"][1]["error"],
        "a marker past the end of the file is refused with 400": rejected["past the end of the file"][0] == 400
        and "has to be inside the file" in rejected["past the end of the file"][1]["error"],
        "a start at the end of the file is refused with 400": rejected["starts at the end of the file"][0] == 400,
        "a non-integer time is refused with 400": rejected["not a whole number"][0] == 400
        and "whole number" in rejected["not a whole number"][1]["error"],
        "a path outside every library is refused with 400": rejected["outside every library"][0] == 400,
        "the refused saves changed nothing (servers and markers.db)": after_rejected["served"] == after
        and after_rejected["stored"] == stored,
    }  # fmt: skip
    evidence = {"seconds": round(seconds, 2), "response": body, "served": after, "plex_rows": plex_rows,
                "stored": stored, "rejected": {k: v[:2] for k, v in rejected.items()}}  # fmt: skip
    checks["the lab was put back"] = not cleanup_errors
    return checks_result(
        1, "Adjust + Save publishes to every owner before it returns, locked", premise, checks, evidence
    )


def write_fingerprint(path: str) -> dict:
    """Everything a rewrite of the file's markers would change on a server: Plex's row ids, Jellyfin's store files
    (name, mtime, size) and Emby's plugin state. Two equal fingerprints mean nothing was written between them.

    Args:
        path: The file as the app sees it.

    Returns:
        The fingerprint, JSON-friendly.
    """
    item = item_id("mlab-plex", path)
    return {
        "plex_rows": [
            (r["id"], r["text"], r["start"], r["end"], r["extra_data"])
            for r in p1.plex_marker_rows()
            if r["item"] == item
        ],
        "jellyfin_stores": {sid: p1.plugin_marker_files(sid) for sid in JELLYFINS},
        "emby_plugin": {sid: p2.emby_bridge(sid, "GET", item_id(sid, path))[1] for sid in EMBYS},
    }


def job_statuses(files: list[dict], path: str) -> dict[str, str]:
    """``server id -> status`` of a job's Files row for one file."""
    return {sid: server_status(files, path, sid) for sid in SERVERS}


def decision_view(payload: dict, mtype: str) -> tuple[int | None, int | None, bool | None]:
    """A decision's ``(start_ms, end_ms, locked)`` in an Inspector payload."""
    marker_row = payload["decisions"][mtype]["marker"] or {}
    return marker_row.get("start_ms"), marker_row.get("end_ms"), marker_row.get("locked")


def says_locked_by_user(payload: dict) -> bool:
    """Whether the Inspector shows both types locked by the user, with the reason the run writes for a lock."""
    return all(
        decision_view(payload, t)[2] is True and payload["decisions"][t]["reason"] == "locked by user"
        for t in ("intro", "credits")
    )


@row(2)
def row_02_lock_alone_publishes_nothing() -> dict:
    """Lock alone (the decided times, unchanged) publishes nothing new and still survives a forced re-detect."""
    ep = synth(2)
    intro, credits = truth(2)
    set_settings()
    settle(ep, 2)
    try:
        before = {"served": served(ep), "fingerprint": write_fingerprint(ep), "stored": stored_times(ep)}
        status, body, _ = save(ep, intro, credits)  # the Lock button sends the decided times as they are
        after_lock = {"served": served(ep), "fingerprint": write_fingerprint(ep), "stored": stored_times(ep)}
        payload = p1.item_payload(ep)
        forced_job, forced_files = run_job(ep, "row 2 forced re-detect", force=True)
        after_forced = {"served": served(ep), "fingerprint": write_fingerprint(ep), "stored": stored_times(ep)}
        forced_payload = p1.item_payload(ep)
    finally:
        cleanup_errors = p1.run_cleanup(lambda: settle(ep, 2))
    locked = {"intro": (*intro, 1), "credits": (*credits, 1)}
    premise = {
        "before the lock every server served the chapters' answer": before["served"] == expected(intro, credits),
        "nothing was locked before": all(v[2] == 0 for v in before["stored"].values()),
    }
    checks = {
        "the lock is a 200": status == 200,
        "every server reports unchanged": all_results(body) == dict.fromkeys(SERVERS, "unchanged"),
        "no server serves anything different": after_lock["served"] == before["served"],
        "nothing was rewritten (Plex row ids, Jellyfin store files, Emby plugin state)": after_lock["fingerprint"]
        == before["fingerprint"],
        "markers.db now holds both rows locked at the decided times": after_lock["stored"] == locked,
        "the Inspector says locked by the user": says_locked_by_user(payload),
        "a forced re-detect completed": forced_job["status"] == "completed",
        "it found every server up to date": job_statuses(forced_files, ep)
        == dict.fromkeys(SERVERS, "markers_up_to_date"),
        "it changed nothing on any server": after_forced["served"] == before["served"]
        and after_forced["fingerprint"] == before["fingerprint"],
        "the rows are still locked at the decided times": after_forced["stored"] == locked,
        "the Inspector still says locked by the user": says_locked_by_user(forced_payload),
    }
    evidence = {
        "before": before,
        "after_lock": after_lock,
        "after_forced": after_forced,
        "response": body,
        "forced_files": forced_files,
        "decisions": forced_payload["decisions"],
    }
    checks["the lab was put back"] = not cleanup_errors
    return checks_result(2, "Lock alone publishes nothing new and survives a re-detect", premise, checks, evidence)


def drop_markers(path: str) -> None:
    """Take our markers off every server the way each one loses them (Plex rows, plugin DELETE), without the app."""
    p2.plex_delete_our_rows(item_id("mlab-plex", path))
    for sid in JELLYFINS:
        p1.jf(sid, "DELETE", f"/MediaPreviewBridge/Markers/{item_id(sid, path)}")
    for sid in EMBYS:
        p2.emby_bridge(sid, "DELETE", item_id(sid, path))


def chapters_evidence(payload: dict) -> list[tuple]:
    """The chapters source's evidence rows in an Inspector payload, as ``(type, start_ms, end_ms)``."""
    return sorted((e["type"], e["start_ms"], e["end_ms"]) for e in payload["evidence"] if e["source"] == "chapters")


def chapters_fetched_at(payload: dict) -> str:
    """When the chapters source's evidence was last read."""
    return max(e["fetched_at"] for e in payload["evidence"] if e["source"] == "chapters")


@row(3)
def row_03_lock_survives_redetect_and_check_servers() -> dict:
    """A locked marker survives a forced re-detect and a Check servers run, which writes the locked times back."""
    ep = synth(3)
    chapters_intro, chapters_credits = truth(3)
    edit_intro, edit_credits = (27_000, 52_000), (102_000, 116_000)
    set_settings()
    settle(ep, 3)
    try:
        save_status, _, _ = save(ep, edit_intro, edit_credits)
        fetched_before = chapters_fetched_at(p1.item_payload(ep))
        forced_job, forced_files = run_job(ep, "row 3 forced re-detect", force=True)
        forced_payload = p1.item_payload(ep)
        after_forced = {"served": served(ep), "stored": stored_times(ep)}
        drop_markers(ep)
        dropped = served(ep)
        check_job, check_files, _ = p2.reconcile_job()
        after_check = {"served": served(ep), "stored": stored_times(ep)}
        second_job, second_files, _ = p2.reconcile_job()
    finally:
        cleanup_errors = p1.run_cleanup(lambda: settle(ep, 3))
    locked = {"intro": (*edit_intro, 1), "credits": (*edit_credits, 1)}
    want = expected(edit_intro, edit_credits)
    premise = {
        "the save took (every server serves the edit)": save_status == 200 and after_forced["served"] == want,
        "every server lost its markers before Check servers ran": all(v == [] for v in dropped.values()),
    }
    checks = {
        "the forced re-detect completed with every server up to date": forced_job["status"] == "completed"
        and job_statuses(forced_files, ep) == dict.fromkeys(SERVERS, "markers_up_to_date"),
        "it detected again (the chapters' evidence is newer)": chapters_fetched_at(forced_payload) > fetched_before,
        "the chapters' answer is still what detection finds": chapters_evidence(forced_payload)
        == [("credits", *chapters_credits), ("intro", *chapters_intro)],
        "the decision is still the locked edit, not the chapters'": {
            t: decision_view(forced_payload, t) for t in ("intro", "credits")
        }
        == {"intro": (*edit_intro, True), "credits": (*edit_credits, True)},
        "a locked type carries no proposal: the evidence rows are where the detected answer shows": all(
            forced_payload["decisions"][t]["proposed"] is None for t in ("intro", "credits")
        ),
        "servers and markers.db are unchanged after the forced re-detect": after_forced["served"] == want
        and after_forced["stored"] == locked,
        "Check servers completed and listed the file": check_job["status"] == "completed"
        and ep in [f["file"] for f in check_files],
        "it wrote the locked times back on every server": job_statuses(check_files, ep)
        == dict.fromkeys(SERVERS, "markers_written"),
        "every server serves the locked edit again, not the chapters' answer": after_check["served"] == want,
        "markers.db still holds the locked rows": after_check["stored"] == locked,
        "a second Check servers run lists nothing for the file": second_job["status"] == "completed"
        and ep not in [f["file"] for f in second_files],
    }
    evidence = {
        "forced_files": forced_files,
        "dropped": dropped,
        "check_files": check_files,
        "after_check": after_check,
        "decisions": forced_payload["decisions"],
        "second_files": second_files,
    }
    checks["the lab was put back"] = not cleanup_errors
    return checks_result(
        3, "A locked marker survives a forced re-detect and a Check servers run", premise, checks, evidence
    )


@row(4)
def row_04_unlock_brings_the_detected_answer_back() -> dict:
    """Unlock: nothing is published at once; the next run brings the detected answer back on every server, or removes
    the marker when detection has nothing (Needs review)."""
    ep = synth(1)
    audio = p2.audio_path(2, 1)  # no season answer: detection decides nothing for it
    intro, credits = truth(1)
    audio_edit = (48_000, 72_000)
    set_settings()
    settle(ep, 1)
    try:
        save(ep, INTRO_EDIT, CREDITS_EDIT)
        edit_state = served(ep)
        unlock_status, unlocked = p1.unlock_markers(ep, ["intro", "credits"])
        after_unlock = {"served": served(ep), "stored": stored_times(ep)}
        job, files = run_job(ep, "row 4 next run")
        after_run = {"served": served(ep), "stored": stored_times(ep)}

        run_job(audio, "row 4 audio analyse", force=True)
        audio_before = {"served": served(audio), "decisions": p1.item_payload(audio)["decisions"]}
        audio_save = save(audio, audio_edit, None)
        audio_saved = served(audio)
        audio_unlock = p1.unlock_markers(audio, ["intro"])
        audio_job, audio_files = run_job(audio, "row 4 audio next run")
        audio_after = {
            "served": served(audio),
            "stored": stored_times(audio),
            "decisions": p1.item_payload(audio)["decisions"],
        }
    finally:
        cleanup_errors = p1.run_cleanup(
            lambda: p1.unlock_markers(audio, UNLOCK_ALL),
            lambda: run_job(audio, "row 4 audio cleanup", force=True),
            lambda: settle(ep, 1),
        )
    premise = {
        "the edit reached every server before the unlock": edit_state == expected(INTRO_EDIT, CREDITS_EDIT),
        "detection decided no intro for the audio file, and no server serves one": audio_before["decisions"]["intro"][
            "status"
        ]
        != "decided"
        and all(v == [] for v in audio_before["served"].values()),
    }
    checks = {
        "unlock is a 200 naming both types": unlock_status == 200 and unlocked["unlocked"] == ["intro", "credits"],
        "the response says the types wait for the next run": all(
            unlocked["decisions"][t]["reason"] == "unlocked; the next run decides this type again"
            for t in ("intro", "credits")
        ),
        "unlock published nothing: every server still serves the edit": after_unlock["served"] == edit_state,
        "the rows are no longer locked": all(v[2] == 0 for v in after_unlock["stored"].values()),
        "the next run completed and wrote on every server": job["status"] == "completed"
        and job_statuses(files, ep) == dict.fromkeys(SERVERS, "markers_written"),
        "every server serves the detected chapters' answer again": after_run["served"] == expected(intro, credits),
        "markers.db holds the detected times, unlocked": after_run["stored"]
        == {"intro": (*intro, 0), "credits": (*credits, 0)},
        "audio: the edit reached every server": audio_save[0] == 200 and audio_saved == expected(audio_edit, None),
        "audio: unlock is a 200": audio_unlock[0] == 200 and audio_unlock[1]["unlocked"] == ["intro"],
        "audio: the next run completed": audio_job["status"] == "completed",
        "audio: detection has nothing, so the intro is Needs review again": audio_after["decisions"]["intro"]["status"]
        == "needs_review",
        "audio: our edit is gone from every server (back to what they served before)": audio_after["served"]
        == audio_before["served"],
        "audio: no intro row is left in markers.db": "intro" not in audio_after["stored"],
    }
    evidence = {
        "after_unlock": after_unlock,
        "files": files,
        "after_run": after_run,
        "audio_files": audio_files,
        "audio_before": audio_before,
        "audio_after": audio_after,
    }
    checks["the lab was put back"] = not cleanup_errors
    return checks_result(
        4, "Unlock brings the detected answer (or Needs review) back on the next run", premise, checks, evidence
    )


PLEX_OWN_INTRO = (2_000, 22_000)  # Plex's own detection: off every edit, every chapter time and the synth truth
PLEX_OWN_CREDITS = (108_000, 120_000)  # as stored; Plex serves a credits start 2 s later, ours and its own alike
PLEX_OWN_CREDITS_SERVED = (PLEX_OWN_CREDITS[0] + PLEX_CREDITS_STORED_OFFSET_MS, PLEX_OWN_CREDITS[1])
PLEX_OWN_EXTRA = {
    "intro": '{"pv:version":"5","url":"pv%3Aversion=5"}',
    "credits": '{"pv:final":"1","pv:version":"4","url":"pv%3Afinal=1&pv%3Aversion=4"}',
}


def plex_set_own_rows(path: str, intro: Time | None = None, credits: Time | None = None) -> None:
    """Make an item's Plex rows what Plex's own detection leaves: ``taggings`` rows and the parts' ``extra_data``.

    Our rows go first, so whatever the item shows afterwards is not ours (written on the lab Plex's database, as
    phase 1 row 5 and phase 2 row 3 do for their fixtures).

    Args:
        path: The file as the app sees it.
        intro: ``(start_ms, end_ms)`` of Plex's own intro, or None for none.
        credits: ``(start_ms, end_ms)`` of Plex's own credits, or None for none.
    """
    part = p1.plex_parts()[path]
    tag = p1.plex_db("select id from tags where tag_type=12 and tag='' order by id limit 1")[0][0]
    statements = [f"delete from taggings where metadata_item_id={part['item']} and text in ('intro','credits')"]
    for index, (text, span) in enumerate((("intro", intro), ("credits", credits))):
        if span is not None:
            statements.append(
                "insert into taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, "
                f"thumb_url, created_at, extra_data) values ({part['item']}, {tag}, {index}, '{text}', {span[0]}, "
                f"{span[1]}, '', strftime('%s','now'), '{PLEX_OWN_EXTRA[text]}')"
            )
    extra = part_extra(part["extra_data"], [list(intro)] if intro else [], [list(credits)] if credits else [])
    statements.append(
        f"update media_parts set extra_data='{extra.replace(chr(39), chr(39) * 2)}' where id={part['part']}"
    )
    sh(str(HERE / "plexdb.sh"), "; ".join(statements))


def plex_rows_expected(intro: Time | None, credits: Time | None) -> list[tuple]:
    """The ``taggings`` rows Plex should hold for what it serves: the credits start is stored 2 s early."""
    rows = []
    if credits is not None:
        rows.append(("credits", credits[0] - PLEX_CREDITS_STORED_OFFSET_MS, credits[1]))
    if intro is not None:
        rows.append(("intro", *intro))
    return sorted(rows)


def set_keep_plex(*, keep: bool) -> None:
    """Switch the lab Plex's "Keep Plex's" on or off."""
    p1.set_redetect("keep_plex" if keep else "restore")


def locked_cell(episode: int, *, locked: tuple[str, ...], plexs_own: tuple[str, ...], keep: bool) -> dict:
    """One cell of Q1 on Plex: the state, the save and everything Plex, the others and markers.db show afterwards.

    Args:
        episode: The Synth Chapters episode.
        locked: The types the user saves (``intro`` and/or ``credits``).
        plexs_own: The types Plex has rows of its own for before the save.
        keep: Whether "Keep Plex's" is on.

    Returns:
        What the cell read (served per server, Plex's rows, markers.db, the save response, the plan).
    """
    ep = synth(episode)
    chapters_intro, chapters_credits = truth(episode)
    own = {"intro": PLEX_OWN_INTRO if "intro" in plexs_own else None,
           "credits": PLEX_OWN_CREDITS if "credits" in plexs_own else None}  # fmt: skip
    edits = {
        "intro": INTRO_EDIT if "intro" in locked else None,
        "credits": CREDITS_EDIT if "credits" in locked else None,
    }
    set_keep_plex(keep=False)  # restore mode clears whatever the last cell left of Plex's own
    settle(ep, episode)
    set_keep_plex(keep=keep)
    plex_set_own_rows(ep, own["intro"], own["credits"])
    planted = served_on("mlab-plex", ep)
    status, body, seconds = save(ep, edits["intro"], edits["credits"])
    plex_row = save_result(body, "mlab-plex")
    return {
        "planted": planted,
        "status": status,
        "seconds": round(seconds, 2),
        "plex_result": plex_row.get("result"),
        "plex_message": plex_row.get("message"),
        "replaced_own": sorted(plex_row.get("replaced_own", [])),
        "served": served(ep),
        "plex_rows": plex_stored_rows(ep),
        "stored": stored_times(ep),
        "chapters": (chapters_intro, chapters_credits),
        "edits": edits,
        "own": own,
    }


# (name, "Keep Plex's" on, the types the user locks, the types Plex has rows of its own for)
PLEX_CELLS = (
    ("A keep, lock intro, Plex has its own intro and credits", True, ("intro",), ("intro", "credits")),
    ("B keep, lock both, Plex has its own intro and credits", True, ("intro", "credits"), ("intro", "credits")),
    ("C keep, lock intro, Plex has its own credits only", True, ("intro",), ("credits",)),
    ("D keep, lock intro, Plex has none of its own", True, ("intro",), ()),
    ("E restore, lock intro, Plex has its own intro and credits", False, ("intro",), ("intro", "credits")),
)


def plex_cell_checks(name: str, cell: dict, keep: bool, locked: tuple[str, ...], plexs_own: tuple[str, ...]) -> dict:
    """The exact expectations of one Plex cell (Q1: a locked type replaces Plex's own, an unlocked one obeys the setting).

    Args:
        name: The cell's label.
        cell: What ``locked_cell`` read.
        keep: Whether "Keep Plex's" was on.
        locked: The locked types.
        plexs_own: The types Plex had its own rows of.

    Returns:
        ``check text -> held``.
    """
    chapters_intro, chapters_credits = cell["chapters"]
    intro = INTRO_EDIT if "intro" in locked else PLEX_OWN_INTRO if keep and "intro" in plexs_own else chapters_intro
    credits_kept = keep and "credits" not in locked and "credits" in plexs_own
    credits = CREDITS_EDIT if "credits" in locked else PLEX_OWN_CREDITS_SERVED if credits_kept else chapters_credits
    plex_serves = [("credits", *credits), ("intro", *intro)]
    others_intro = INTRO_EDIT if "intro" in locked else chapters_intro
    others_credits = CREDITS_EDIT if "credits" in locked else chapters_credits
    replaced = sorted(t for t in locked if t in plexs_own) if keep else []
    return {
        f"{name}: the save is a 200 and Plex's row is written": cell["status"] == 200
        and cell["plex_result"] == "written",
        f"{name}: Plex serves {intro} / {credits}": [(t, s, e) for t, s, e in cell["served"]["mlab-plex"]]
        == sorted(plex_serves),
        f"{name}: Plex's database rows say the same": cell["plex_rows"] == plex_rows_expected(intro, credits),
        f"{name}: the row says it replaced Plex's own {replaced or 'nothing'}": cell["replaced_own"] == replaced,
        f"{name}: the message says the setting was overridden": (
            "a marker you adjust always wins" in (cell["plex_message"] or "")
        )
        == bool(replaced),
        f"{name}: every other server serves the lock and the detected answer for the rest": all(
            cell["served"][sid] == expected_on(sid, others_intro, others_credits)
            for sid in SERVERS
            if sid != "mlab-plex"
        ),
        f"{name}: markers.db locks exactly the saved types": {t: v[2] for t, v in cell["stored"].items() if v[2]}
        == dict.fromkeys(locked, 1),
    }


@row(5)
def row_05_locked_marker_meets_keep_plex() -> dict:
    """Q1 on Plex: a locked type replaces Plex's own markers even under Keep Plex's; the unlocked type still obeys it."""
    episode = 2
    ep = synth(episode)
    set_settings()
    cells: dict[str, dict] = {}
    followed_up: dict[str, Any] = {}
    try:
        for name, keep, locked, plexs_own in PLEX_CELLS:
            cells[name] = locked_cell(episode, locked=locked, plexs_own=plexs_own, keep=keep)
            if name.startswith("A "):
                # After a lock replaced Plex's intro: a forced run leaves it; unlocking hands the intro back to
                # detection while "Keep Plex's" still holds the credits Plex found itself.
                forced_job, _ = run_job(ep, "row 5 A forced", force=True)
                followed_up["forced"] = {"status": forced_job["status"], "served": served_on("mlab-plex", ep)}
                p1.unlock_markers(ep, ["intro"])
                run_job(ep, "row 5 A after unlock")
                followed_up["unlocked"] = {"served": served_on("mlab-plex", ep), "stored": stored_times(ep)}
    finally:
        cleanup_errors = p1.run_cleanup(lambda: set_keep_plex(keep=False), lambda: settle(ep, episode))
    premise = {
        f"{name}: Plex showed the rows planted as its own before the save": cells[name]["planted"]
        == sorted(
            ("credits", *PLEX_OWN_CREDITS_SERVED) if t == "credits" else (t, *span)
            for t, span in cells[name]["own"].items()
            if span is not None
        )
        for name, *_ in PLEX_CELLS
    }
    checks: dict[str, bool] = {}
    for name, keep, locked, plexs_own in PLEX_CELLS:
        checks.update(plex_cell_checks(name, cells[name], keep, locked, plexs_own))
    chapters_intro, _ = truth(episode)
    checks.update({
        "A: a forced run leaves the lock on Plex and the credits Plex found itself": followed_up["forced"]["status"]
        == "completed"
        and followed_up["forced"]["served"] == sorted([("credits", *PLEX_OWN_CREDITS_SERVED), ("intro", *INTRO_EDIT)]),
        "A: after unlock, detection's intro is back and Keep Plex's still holds Plex's credits": followed_up["unlocked"][
            "served"
        ]
        == sorted([("credits", *PLEX_OWN_CREDITS_SERVED), ("intro", *chapters_intro)]),
    })  # fmt: skip
    checks["the lab was put back"] = not cleanup_errors
    return checks_result(
        5, "A locked marker meets Keep Plex's (Q1)", premise, checks, {"cells": cells, "followed_up": followed_up}
    )


EMBY_OWN_INTRO = (20_000, 50_000)
EMBY_OWN_CREDITS_START = 110_000
EMBY_CELLS = (
    ("A keep, lock intro, Emby has its own intro and credits", True, ("intro",), ("intro", "credits")),
    ("B keep, lock both, Emby has its own intro and credits", True, ("intro", "credits"), ("intro", "credits")),
    ("C keep, lock intro, Emby has its own credits only", True, ("intro",), ("credits",)),
    ("D keep, lock intro, Emby has none of its own", True, ("intro",), ()),
    ("E restore, lock intro, Emby has its own intro and credits", False, ("intro",), ("intro", "credits")),
)


def emby_own_marks(plexs_own: tuple[str, ...]) -> list[tuple[str, int]]:
    """The marker rows an Emby's own detection would leave for the types given."""
    marks: list[tuple[str, int]] = []
    if "intro" in plexs_own:
        marks += [("IntroStart", EMBY_OWN_INTRO[0]), ("IntroEnd", EMBY_OWN_INTRO[1])]
    if "credits" in plexs_own:
        marks.append(("CreditsStart", EMBY_OWN_CREDITS_START))
    return marks


def emby_expected(
    server_id: str, chapters: tuple[Time, Time], keep: bool, locked: tuple[str, ...], emby_own: tuple[str, ...]
) -> tuple[list[tuple], list[str]]:
    """What one Emby serves after a save (Q1) and which of its own types the lock replaced (spec §5.5 rule 1, §6.3).

    Args:
        server_id: The Emby's id (its credits carry no end).
        chapters: The episode's ``(intro, credits)`` chapters.
        keep: Whether this Emby is set to "Keep Emby's".
        locked: The locked types.
        emby_own: The types Emby had its own rows of.

    Returns:
        The served markers and the sorted ``replaced_own`` types.
    """
    intro = INTRO_EDIT if "intro" in locked else EMBY_OWN_INTRO if keep and "intro" in emby_own else chapters[0]
    if "credits" in locked:
        credits_start = CREDITS_EDIT[0]
    elif keep and "credits" in emby_own:
        credits_start = EMBY_OWN_CREDITS_START
    else:
        credits_start = chapters[1][0]
    served_rows = expected_on(server_id, intro, (credits_start, chapters[1][1]))
    return served_rows, sorted(t for t in locked if t in emby_own) if keep else []


@row(6)
def row_06_locked_marker_meets_keep_emby() -> dict:
    """Q1 on Emby 4.10 and 4.9: a locked type replaces Emby's own rows even under Keep Emby's (ReplaceOwn for that
    type alone); the unlocked type obeys the setting."""
    episode = 2
    ep = synth(episode)
    chapters = truth(episode)
    set_settings()
    cells: dict[str, dict] = {}
    try:
        for name, keep, locked, emby_own in EMBY_CELLS:
            for sid in EMBYS:
                set_keep_emby(sid, keep=False)  # restore mode clears the last cell's own rows
            settle(ep, episode)
            for sid in EMBYS:
                set_keep_emby(sid, keep=keep)
                p2.emby_set_own_marker_rows(sid, ep, emby_own_marks(emby_own))
            planted = {sid: served_on(sid, ep) for sid in EMBYS}
            status, body, seconds = save(ep, INTRO_EDIT if "intro" in locked else None,
                                         CREDITS_EDIT if "credits" in locked else None)  # fmt: skip
            cells[name] = {
                "planted": planted,
                "status": status,
                "seconds": round(seconds, 2),
                "rows": {sid: save_result(body, sid) for sid in EMBYS},
                "served": served(ep),
                "stored": stored_times(ep),
            }
    finally:
        cleanup_errors = p1.run_cleanup(
            *[lambda sid=sid: set_keep_emby(sid, keep=False) for sid in EMBYS],
            *[lambda sid=sid: p2.emby_set_own_marker_rows(sid, ep, []) for sid in EMBYS],
            lambda: settle(ep, episode),
        )
    premise = {
        f"{name}: {sid} showed the rows planted as Emby's own before the save": cells[name]["planted"][sid]
        == sorted(
            expected_on(
                sid,
                EMBY_OWN_INTRO if "intro" in own else None,
                (EMBY_OWN_CREDITS_START, 0) if "credits" in own else None,
            )  # fmt: skip
        )
        for name, _, _, own in EMBY_CELLS
        for sid in EMBYS
    }
    checks: dict[str, bool] = {}
    for name, keep, locked, emby_own in EMBY_CELLS:
        cell = cells[name]
        for sid in EMBYS:
            want, replaced = emby_expected(sid, chapters, keep, locked, emby_own)
            checks.update({
                f"{name} [{sid}]: the save is a 200 and the row is written": cell["status"] == 200
                and cell["rows"][sid].get("result") == "written",
                f"{name} [{sid}]: Emby serves {want}": cell["served"][sid] == want,
                f"{name} [{sid}]: the row says it replaced Emby's own {replaced or 'nothing'}": sorted(
                    cell["rows"][sid].get("replaced_own", [])
                ) == replaced,
            })  # fmt: skip
        others_intro = INTRO_EDIT if "intro" in locked else chapters[0]
        others_credits = CREDITS_EDIT if "credits" in locked else chapters[1]
        checks[f"{name}: Plex and both Jellyfins serve the lock and the detected answer for the rest"] = all(
            cell["served"][sid] == expected_on(sid, others_intro, others_credits) for sid in ("mlab-plex", *JELLYFINS)
        )
        checks[f"{name}: markers.db locks exactly the saved types"] = {
            t: v[2] for t, v in cell["stored"].items() if v[2]
        } == dict.fromkeys(locked, 1)
    checks["the lab was put back"] = not cleanup_errors
    return checks_result(6, "A locked marker meets Keep Emby's (Q1)", premise, checks, {"cells": cells})


@row(7)
def row_07_save_with_a_server_stopped() -> dict:
    """Save with Jellyfin 10.11 stopped: saved and locked, that server reported failed, the next Check servers
    publishes it."""
    ep = synth(1)
    stopped = "mlab-jellyfin"
    chapters_intro, chapters_credits = truth(1)
    set_settings()
    settle(ep, 1)
    before = served(ep)
    started = False
    try:
        sh("docker", "stop", stopped, timeout=120)
        status, body, seconds = save(ep, INTRO_EDIT, CREDITS_EDIT)
        while_stopped = {sid: served_on(sid, ep) for sid in SERVERS if sid != stopped}
        stored = stored_times(ep)
        sh("docker", "start", stopped)
        started = True
        p1.jf_wait_healthy(stopped)
        after_start = served_on(stopped, ep)
        check_job, check_files, _ = p2.reconcile_job()
        after_check = served(ep)
    finally:

        def start_again() -> None:
            if not started:
                sh("docker", "start", stopped)
                p1.jf_wait_healthy(stopped)

        cleanup_errors = p1.run_cleanup(start_again, lambda: settle(ep, 1))
    failed_row = save_result(body, stopped)
    checks = {
        "the save is a 200 (the edit is kept although one server failed)": status == 200,
        "it came back inside the bound, not after a hang": seconds < SAVE_BOUND_S,
        "Jellyfin 10.11 is reported failed with a message": failed_row.get("result") == "failed"
        and bool(failed_row.get("message")),
        "the four others are reported written": {sid: r for sid, r in all_results(body).items() if sid != stopped}
        == dict.fromkeys([s for s in SERVERS if s != stopped], "written"),
        "the four others serve the edit": all(
            while_stopped[sid] == expected_on(sid, INTRO_EDIT, CREDITS_EDIT) for sid in while_stopped
        ),
        "the edit is saved and locked in markers.db": stored
        == {"intro": (*INTRO_EDIT, 1), "credits": (*CREDITS_EDIT, 1)},
        "Jellyfin still serves the old answer after it comes back": after_start
        == expected_on(stopped, chapters_intro, chapters_credits),
        "Check servers publishes the edit to it": check_job["status"] == "completed"
        and server_status(check_files, ep, stopped) == "markers_written",
        "every server serves the edit afterwards": after_check == expected(INTRO_EDIT, CREDITS_EDIT),
    }
    evidence = {"seconds": round(seconds, 2), "response": body, "while_stopped": while_stopped,
                "after_start": after_start, "check_files": check_files, "after_check": after_check}  # fmt: skip
    checks["the lab was put back"] = not cleanup_errors
    premise = {"before the save every server served the chapters' answer": before == expected(*truth(1))}
    return checks_result(
        7, "Save with one server stopped, published by the next Check servers", premise, checks, evidence
    )


@contextlib.contextmanager
def only_enabled(*keep: str) -> Iterator[None]:
    """Intro & Credits on for the servers named and off for the rest, for a block; all on again after.

    Args:
        keep: Server ids that stay on.
    """
    try:
        for sid in SERVERS:
            app_ok("PUT", f"/api/servers/{sid}", {"markers": {"enabled": sid in keep}})
        yield
    finally:
        errors = p1.run_cleanup(
            *[lambda sid=sid: app_ok("PUT", f"/api/servers/{sid}", {"markers": {"enabled": True}}) for sid in SERVERS]
        )
        if errors:  # every server was tried; a lab left half off would spoil the rows after this one
            raise RuntimeError(f"couldn't turn Intro & Credits back on everywhere: {errors}")


REFUSAL = "No server with Intro & Credits turned on for this file can show a {} marker"
REFUSED_SAVES = (
    ("recap", [marker("recap", (5_000, 9_000))], "recap"),
    ("preview", [marker("preview", (110_000, 115_000))], "preview"),
    ("intro and recap", [marker("intro", INTRO_EDIT), marker("recap", (5_000, 9_000))], "recap"),
)
OWNER_SETS = (("Plex only", ("mlab-plex",)), ("Emby 4.10 only", ("mlab-emby",)),
              ("Plex and both Embys", ("mlab-plex", *EMBYS)))  # fmt: skip
EARLY_CREDITS = (103_250, 110_000)


@row(8)
def row_08_the_two_d8_shapes() -> dict:
    """D8: a recap is refused when only Plex or Emby owns the file (can_show); an edited credits end on Emby is
    accepted and published start-only, with the note, never silently dropped."""
    ep = synth(1)
    set_settings()
    settle(ep, 1)
    before = served(ep)
    refused: dict[str, dict] = {}
    try:
        for owners_name, owners in OWNER_SETS:
            with only_enabled(*owners):
                for what, body, missing in REFUSED_SAVES:
                    status, answer = p1.app("POST", "/api/markers/item/markers", {"path": ep, "markers": body})
                    refused[f"{owners_name}: {what}"] = {
                        "status": status, "error": (answer or {}).get("error"), "missing": missing,
                        "stored": stored_times(ep), "served": served(ep),
                    }  # fmt: skip
        payload = p1.item_payload(ep)
        can_show = {s["server_id"]: s["can_show"] for s in payload["servers"]}
        recap_status, recap_body, _ = p1.save_markers(ep, [marker("recap", (5_000, 9_000))])
        recap_served = served(ep)
        p1.unlock_markers(ep, ["recap"])
        settle(ep, 1)

        early_status, early_body, _ = save(ep, None, EARLY_CREDITS)
        early_served = served(ep)
        early_payload = p1.item_payload(ep)
        early_stored = stored_times(ep)
    finally:
        cleanup_errors = p1.run_cleanup(lambda: settle(ep, 1))
    chapters_intro, _ = truth(1)
    baseline = expected(*truth(1))
    checks: dict[str, bool] = {}
    for key, seen in refused.items():
        checks[f"{key}: refused with 400 naming the type"] = seen["status"] == 400 and seen["error"] == REFUSAL.format(
            seen["missing"]
        )
        checks[f"{key}: nothing stored and every server unchanged"] = (
            "recap" not in seen["stored"]
            and seen["served"] == baseline
            and all(v[2] == 0 for v in seen["stored"].values())
        )
    checks.update({
        "the Inspector says Plex and both Embys can show intro and credits only": all(
            can_show[sid] == ["intro", "credits"] for sid in ("mlab-plex", *EMBYS)
        ),
        "the Inspector says both Jellyfins can also show recap and preview": all(
            can_show[sid] == ["intro", "credits", "recap", "preview"] for sid in JELLYFINS
        ),
        "with Jellyfin on, a recap is accepted (200)": recap_status == 200,
        "Plex and both Embys list it as one they can't show": all(
            save_result(recap_body, sid)["cant_show"] == ["recap"] for sid in ("mlab-plex", *EMBYS)
        ),
        "both Jellyfins serve the recap at the saved times next to the detected intro and credits": all(
            recap_served[sid] == sorted([*expected_on(sid, *truth(1)), ("recap", 5_000, 9_000)]) for sid in JELLYFINS
        ),
        "Plex and both Embys are unchanged by it": all(
            recap_served[sid] == baseline[sid] for sid in ("mlab-plex", *EMBYS)
        ),
        "an edited credits end is accepted (200) and written everywhere": early_status == 200
        and all_results(early_body) == dict.fromkeys(SERVERS, "written"),
        "Plex and both Jellyfins publish the whole span": all(
            early_served[sid] == expected_on(sid, chapters_intro, EARLY_CREDITS) for sid in ("mlab-plex", *JELLYFINS)
        ),
        "both Embys publish the start only, at the edited start": all(
            early_served[sid] == expected_on(sid, chapters_intro, EARLY_CREDITS)
            and ("credits", EARLY_CREDITS[0], None) in early_served[sid]
            for sid in EMBYS
        ),
        "both Embys say Emby skips to the end of the file (a note on the credits end)": all(
            save_result(early_body, sid)["notes"]
            == [{"type": "credits", "field": "end", "note": "Emby skips to the end of the file"}]
            for sid in EMBYS
        ),
        "no other server carries that note": all(
            save_result(early_body, sid)["notes"] == [] for sid in ("mlab-plex", *JELLYFINS)
        ),
        "the Inspector says it on both Embys too": all(
            "Emby skips to the end of the file" in next(s for s in early_payload["servers"] if s["server_id"] == sid)["plan_reason"]
            for sid in EMBYS
        ),
        "the full edited end is kept in markers.db, not dropped": early_stored["credits"] == (*EARLY_CREDITS, 1),
    })  # fmt: skip
    evidence = {"refused": refused, "can_show": can_show, "recap": {"served": recap_served, "response": recap_body},
                "early": {"served": early_served, "response": early_body}}  # fmt: skip
    checks["the lab was put back"] = not cleanup_errors
    premise = {"before the saves every server served the chapters' answer": before == baseline}
    return checks_result(
        8, "The two D8 shapes: recap refused, Emby credits end published start-only", premise, checks, evidence
    )


STAGED_EPISODES = {
    episode: p2.SYNTH / "_staging" / f"Synth Chapters (2021) - S01E{episode:02d}.webm" for episode in (4, 5, 6)
}


def chapter_evidence(payload: dict, mtype: str) -> list[tuple]:
    """The chapters source's evidence for one type, as ``(label, start_ms, end_ms)``."""
    return [
        (e["label"], e["start_ms"], e["end_ms"])
        for e in payload["evidence"]
        if e["source"] == "chapters" and e["type"] == mtype
    ]


@row(9)
def row_09_chapter_titles_ending_and_lone_intro() -> dict:
    """Task 15's chapter rule end to end: an episode whose credits chapter is `Ending` gets credits from it on every
    server; a lone generic `Intro` still decides the intro; a final chapter called `End` is not credits."""
    set_settings()
    targets = {episode: p1.SYNTH_HOST_SEASON / staged.name for episode, staged in STAGED_EPISODES.items()}
    paths = {episode: synth(episode) for episode in STAGED_EPISODES}
    for episode, staged in STAGED_EPISODES.items():
        shutil.copyfile(staged, targets[episode])
    seen: dict[int, dict] = {}
    try:
        listed = p2.rescan_until("Synth Chapters", list(paths.values()), present=True)
        _IDS.clear()
        job, files = p2.run_job(
            {"file_paths": list(paths.values()), "library_name": "Phase 4 row 9 chapter titles", "force": True}
        )
        for episode, path in paths.items():
            seen[episode] = {
                "served": served(path),
                "payload": p1.item_payload(path),
                "stored": stored_times(path),
                "statuses": job_statuses(files, path),
            }
    finally:
        removed: dict = {}

        def take_staged_episodes_out() -> None:
            for target in targets.values():
                target.unlink(missing_ok=True)
            removed.update(p2._rescan_best_effort("Synth Chapters", list(paths.values())))

        cleanup_errors = p1.run_cleanup(take_staged_episodes_out, _IDS.clear)
    ending, lone_intro, end_only = seen[4], seen[5], seen[6]
    every_server = list(SERVERS)
    premise = {
        "every server listed all three staged episodes": all(all(v.values()) for v in listed.values()),
    }
    checks = {
        "the job completed": job["status"] == "completed",
        "Ending: the chapters source reads it as the credits (labelled Ending, 1:40-2:00)": chapter_evidence(
            ending["payload"], "credits"
        )
        == [("Ending", 100_000, 120_000)],
        "Ending: credits are decided and published on every server": ending["served"]
        == expected((10_000, 40_000), (100_000, 120_000)),
        "Ending: markers.db holds both, unlocked": ending["stored"]
        == {"intro": (10_000, 40_000, 0), "credits": (100_000, 120_000, 0)},
        "Ending: every server row is written": ending["statuses"] == dict.fromkeys(every_server, "markers_written"),
        "lone Intro: the chapter is the intro, as shipped (measured, kept)": chapter_evidence(
            lone_intro["payload"], "intro"
        )
        == [("Intro", 20_000, 50_000)],
        "lone Intro: the intro is published on every server": lone_intro["served"] == expected((20_000, 50_000), None),
        "lone Intro: no credits are decided or served": chapter_evidence(lone_intro["payload"], "credits") == []
        and "credits" not in lone_intro["stored"],
        "End: a final chapter called End is a scene, so no credits come from it": chapter_evidence(
            end_only["payload"], "credits"
        )
        == [],
        "End: the intro is published and no server serves credits": end_only["served"]
        == expected((10_000, 40_000), None),
        "End: credits are not decided": end_only["payload"]["decisions"]["credits"]["status"] != "decided",
    }
    evidence = {"listed": listed, "removed": removed, "files": files, "seen": seen}
    checks["the lab was put back"] = not cleanup_errors
    return checks_result(
        9, "Chapter titles: Ending is credits, a lone Intro is kept, End is a scene", premise, checks, evidence
    )


# ---------------------------------------------------------------------------------------------- Setup Health

HEALTH_APP = "http://127.0.0.1:18082"
NOPASS_PLEX = "http://127.0.0.1:32403"
NOPASS_CONFIG = "/plexnp/Library/Application Support/Plex Media Server"
COPY_CONFIG = "/plexcopy/Library/Application Support/Plex Media Server"


def health_call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """A call to the row 10 app (`mlab-app-health`, `phase4_health_up.sh`)."""
    return p1.http(method, f"{HEALTH_APP}{path}", headers={"X-Auth-Token": p1.ENV["MLAB_APP_HEALTH_TOKEN"]}, body=body)


def health_ok(method: str, path: str, body: Any = None) -> Any:
    """``health_call`` that raises on a failure status."""
    status, data = health_call(method, path, body)
    if status >= 300:
        raise RuntimeError(f"health app {method} {path} -> {status}: {scrub(data)}")
    return data


def plex_entry(server_id: str, name: str, url: str, token: str, config_folder: str) -> dict:
    """A Plex server for the row 10 app, Intro & Credits on with the database write confirmed."""
    return {
        "id": server_id,
        "type": "plex",
        "name": name,
        "url": url,
        "auth": {"method": "token", "token": token},
        "output": {"plex_config_folder": config_folder},
        "markers": {
            "enabled": True,
            "library_ids": None,
            "plex": {"db_write_confirmed_at": now_iso(), "on_plex_redetect": "restore"},
        },
    }


def configure_health_app() -> None:
    """The unclaimed Plex and the copied database as servers of the row 10 app (created once, updated after)."""
    health_ok("POST", "/api/setup/complete")
    _, existing = health_call("GET", "/api/servers")
    ids = {s["id"] for s in (existing.get("servers", existing) if isinstance(existing, dict) else existing) or []}
    entries = (
        plex_entry("p4-nopass", "Unclaimed Plex", "http://mlab-plex-nopass:32499", "unclaimed", NOPASS_CONFIG),
        plex_entry("p4-copy", "Copied database", "http://mlab-plex:32400", p1.ENV["PLEX_TOKEN"], COPY_CONFIG),
    )
    for entry in entries:
        if entry["id"] in ids:
            health_ok("PUT", f"/api/servers/{entry['id']}", {k: v for k, v in entry.items() if k != "id"})
        else:
            health_ok("POST", "/api/servers", entry)


def marker_checks(payload: dict) -> dict[str, dict]:
    """The Intro & Credits rows of a readiness envelope, by id (empty when the server has no such section)."""
    section = next((s for s in payload.get("sections", []) if s["id"] == "markers"), None)
    return {c["id"]: c for c in (section or {}).get("checks", [])}


def readiness_of(call: Callable, server_id: str) -> dict:
    """A server's Setup Health envelope (``GET /api/servers/<id>/previews-readiness``)."""
    status, payload = call("GET", f"/api/servers/{server_id}/previews-readiness")
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"previews-readiness of {server_id} -> {status}: {scrub(payload)}")
    return payload


def readiness_until(call: Callable, server_id: str, holds: Callable[[dict], bool], what: str) -> dict:
    """The envelope once ``holds`` says so (the capability check is cached for a few seconds, so a fresh change of the
    server shows up within its lifetime).

    Args:
        call: ``p1.app`` or ``health_call``.
        server_id: The server.
        holds: Whether the envelope shows what the cell waits for.
        what: For the timeout message.

    Returns:
        The envelope that satisfied ``holds``.
    """

    def envelope() -> dict | None:
        current = readiness_of(call, server_id)
        return current if holds(current) else None

    return wait_until(what, envelope, timeout=120, every=4)


def row_shape(check: dict | None) -> tuple | None:
    """A Setup Health row reduced to what the user sees: ``(ok, severity, current, recommended, label)``."""
    if check is None:
        return None
    return check["ok"], check["severity"], check["current"], check["recommended"], check["label"]


PASS_ROW = "markers_plex_pass"
TAG_ROW = "markers_plex_tag_row"
DB_ROW = "markers_plex_db_local"
DETECTION_ROW = "markers_plex_detection"
GOOD = {
    PASS_ROW: (True, "critical", "active", "active", "Plex Pass is active"),
    TAG_ROW: (True, "critical", "present", "present", "Plex's marker list is ready"),
    DB_ROW: (True, "critical", "this machine", "this machine", "Plex's library database is on this machine"),
    DETECTION_ROW: (True, "recommended", "Off", "Off", "Plex's own detection is off"),
}
NO_PASS = (False, "critical", "not active", "active", "Skip buttons need Plex Pass")
NO_TAG = (False, "critical", "missing", "present", "Plex hasn't made its marker list yet")
NOT_LOCAL = (False, "critical", "another machine", "this machine", "Plex's library database isn't on this machine")
DETECTION_ON = (False, "recommended", "On", "Off", "Plex's own detection can replace your markers")


def shapes(payload: dict) -> dict[str, tuple | None]:
    """``row id -> row_shape`` for the four Plex rows."""
    checks = marker_checks(payload)
    return {row_id: row_shape(checks.get(row_id)) for row_id in GOOD}


def lab_plex_tag_ids() -> list[str]:
    """The ids of the lab Plex's marker tag rows (``tag_type`` 12)."""
    return [r[0] for r in p1.plex_db("select id from tags where tag_type=12")]


@row(10)
def row_10_setup_health_plex() -> dict:
    """Setup Health, Plex: Pass missing, marker list absent, database not on this machine, Plex's own detection on
    (and the same rows all good on the lab Plex)."""
    set_settings()
    configure_health_app()
    seen: dict[str, dict] = {}
    tag_ids = lab_plex_tag_ids()
    detection_before = p2.plex_prefs()
    try:
        seen["good"] = readiness_until(
            p1.app, "mlab-plex", lambda p: shapes(p) == GOOD, "the lab Plex's four rows to read all good"
        )
        with p2.PlexDetection():
            seen["detection on"] = readiness_until(
                p1.app,
                "mlab-plex",
                lambda p: shapes(p)[DETECTION_ROW] == DETECTION_ON,
                "Plex's own detection to show as on",
            )
        try:
            sh(str(HERE / "plexdb.sh"), f"update tags set tag_type=912 where id in ({','.join(tag_ids)})")
            seen["no marker list"] = readiness_until(
                p1.app, "mlab-plex", lambda p: shapes(p)[TAG_ROW] == NO_TAG, "the marker list to read as missing"
            )
        finally:
            sh(str(HERE / "plexdb.sh"), f"update tags set tag_type=12 where id in ({','.join(tag_ids)})")
        seen["restored"] = readiness_until(
            p1.app, "mlab-plex", lambda p: shapes(p) == GOOD, "the lab Plex's rows to read all good again"
        )
        seen["no pass"] = readiness_until(
            health_call, "p4-nopass", lambda p: shapes(p)[PASS_ROW] == NO_PASS, "the unclaimed Plex to read no Pass"
        )
        seen["not on this machine"] = readiness_until(
            health_call, "p4-copy", lambda p: shapes(p)[DB_ROW] == NOT_LOCAL, "the copied database to read as elsewhere"
        )
    finally:
        cleanup_errors = p1.run_cleanup(
            lambda: sh(str(HERE / "plexdb.sh"), f"update tags set tag_type=12 where id in ({','.join(tag_ids)})"),
            lambda: p1.plex_set_prefs(**detection_before),
        )
    got = {name: shapes(payload) for name, payload in seen.items()}
    nopass_section = next(s for s in seen["no pass"]["sections"] if s["id"] == "markers")
    copy_section = next(s for s in seen["not on this machine"]["sections"] if s["id"] == "markers")
    unclaimed_identity = p1.http("GET", f"{NOPASS_PLEX}/identity")[1]
    premise = {
        "the throwaway Plex is really unclaimed (its identity says claimed=0)": 'claimed="0"'
        in str(unclaimed_identity),
        "the lab Plex's marker tag rows exist before the cell that hides them": bool(tag_ids),
    }
    checks = {
        "lab Plex: pass, marker list, database and detection all read good": got["good"] == GOOD,
        "lab Plex with its own detection on: only the detection row fails (recommended, On -> Off)": got["detection on"]
        == {**GOOD, DETECTION_ROW: DETECTION_ON},
        "lab Plex with the marker list hidden: only the marker list row fails (critical, missing -> present)": got[
            "no marker list"
        ]
        == {**GOOD, TAG_ROW: NO_TAG},
        "lab Plex reads all good again once the list is back": got["restored"] == GOOD,
        "unclaimed Plex: Plex Pass row fails (critical, not active -> active), database row passes": got["no pass"][
            PASS_ROW
        ]
        == NO_PASS
        and got["no pass"][DB_ROW] == GOOD[DB_ROW],
        "unclaimed Plex: the rows past Plex Pass aren't read, so they aren't shown": got["no pass"][TAG_ROW] is None
        and got["no pass"][DETECTION_ROW] is None,
        "unclaimed Plex: the section is critical": nopass_section["ok"] is False
        and nopass_section["severity"] == "critical",
        "copied database: the database row fails (critical, another machine -> this machine)": got[
            "not on this machine"
        ][DB_ROW]
        == NOT_LOCAL,
        "copied database: Plex Pass still reads active, and the unreached rows aren't shown": got[
            "not on this machine"
        ][PASS_ROW]
        == GOOD[PASS_ROW]
        and got["not on this machine"][TAG_ROW] is None
        and got["not on this machine"][DETECTION_ROW] is None,
        "copied database: the section is critical": copy_section["ok"] is False
        and copy_section["severity"] == "critical",
    }
    checks["the lab was put back"] = not cleanup_errors
    return checks_result(10, "Setup Health for Plex: Pass, marker list, database location, own detection", premise, checks, {"envelopes": seen, "shapes": got})  # fmt: skip


PLUGIN_HOLD = "/config/plugins-held"
PLUGIN_SOURCES = LAB_PLUGINS = p1.LAB / "plugins-old"


class PluginCase:
    """How one lab server's Bridge plugin is taken out, replaced by an older build and put back."""

    def __init__(self, server_id: str, kind: str, installed: str, old_files: Path, old_name: str, old_version: str):
        self.server_id = server_id
        self.kind = kind  # "jellyfin" or "emby"
        self.installed = installed  # what is in /config/plugins now (a folder for Jellyfin, a dll for Emby)
        self.old_files = old_files
        self.old_name = old_name  # what the older build is called inside /config/plugins
        self.old_version = old_version

    def exec(self, *cmd: str) -> str:
        """A command in the server's container."""
        return sh("docker", "exec", self.server_id, *cmd)

    def hold_current(self) -> None:
        """Move the current plugin out of /config/plugins and restart, so the server loads without it."""
        self.exec("sh", "-c", f"mkdir -p {PLUGIN_HOLD} && mv /config/plugins/{self.installed} {PLUGIN_HOLD}/")
        self.restart()

    def install_old(self) -> None:
        """Put the older build in /config/plugins and restart."""
        if self.kind == "jellyfin":
            self.exec("mkdir", f"/config/plugins/{self.old_name}")
            source = next(self.old_files.iterdir())
            if source.suffix == ".zip":
                out = LAB_PLUGINS / "unzipped" / self.server_id
                shutil.rmtree(out, ignore_errors=True)
                out.mkdir(parents=True)
                sh("unzip", "-o", "-q", str(source), "-d", str(out))
                source = next(out.glob("*.dll"))
            sh("docker", "cp", str(source), f"{self.server_id}:/config/plugins/{self.old_name}/{source.name}")
        else:
            source = next(self.old_files.glob("*.dll"))
            sh("docker", "cp", str(source), f"{self.server_id}:/config/plugins/{self.old_name}")
        self.restart()

    def is_held(self) -> bool:
        """Whether the current plugin is in the hold folder (moved out and not yet put back)."""
        answer = self.exec("sh", "-c", f"test -e {PLUGIN_HOLD}/{self.installed} && echo held || echo not")
        return answer.strip() == "held"

    def restore_current(self) -> None:
        """Remove the older build, put the current one back and restart. The older build is only removed while the
        current one is still in the hold folder, so a cleanup after a failure can't delete the plugin that was just
        restored (Emby's older build has its name)."""
        if self.is_held():
            self.exec("sh", "-c", f"rm -rf /config/plugins/{self.old_name}")
            self.exec("sh", "-c", f"mv {PLUGIN_HOLD}/{self.installed} /config/plugins/{self.installed}")
        self.restart()  # also when it was already back: an earlier restart may be the step that failed

    def restart(self) -> None:
        """Restart the server and wait until it answers."""
        sh("docker", "restart", self.server_id, timeout=300)
        if self.kind == "jellyfin":
            p1.jf_wait_healthy(self.server_id)
        else:
            p2.emby_wait_healthy(self.server_id)


def wait_capability(server_id: str, state: str) -> dict:
    """A server's Intro & Credits capability once it reports ``state`` (asked fresh each time).

    Args:
        server_id: The server.
        state: The capability state to wait for.

    Returns:
        The capability report.
    """

    def report() -> dict | None:
        capability = app_ok("GET", f"/api/markers/servers/{server_id}/status?refresh=1")["capability"]
        return capability if capability["state"] == state else None

    return wait_until(f"{server_id} capability {state}", report, timeout=180, every=5)


def plugin_cases() -> list[PluginCase]:
    """The four servers' plugin cases, from what is installed in each container right now."""

    def listing(server_id: str) -> list[str]:
        return sh("docker", "exec", server_id, "ls", "/config/plugins").split()

    cases = []
    for server_id, version in (("mlab-jellyfin", "10.11.0.3"), ("mlab-jf12", "12.0.0.3")):
        folder = next(name for name in listing(server_id) if name.startswith("MediaPreviewBridge_"))
        old = PLUGIN_SOURCES / ("jf10.11" if server_id == "mlab-jellyfin" else "jf12.0")
        cases.append(PluginCase(server_id, "jellyfin", folder, old, f"MediaPreviewBridge_{version}", version))
    for server_id, abi in (("mlab-emby", "4.10"), ("mlab-emby49", "4.9")):
        cases.append(PluginCase(server_id, "emby", "MediaPreviewBridge.Emby.dll", PLUGIN_SOURCES / f"emby{abi}",
                                "MediaPreviewBridge.Emby.dll", "0.9.0.0"))  # fmt: skip
    return cases


def plugin_rows(payload: dict) -> dict[str, tuple | None]:
    """The plugin rows of a Setup Health envelope by id: the install row of either vendor and the outdated row."""
    section = next((s for s in payload.get("sections", []) if s["id"] == "plugin"), {})
    checks = {c["id"]: c for c in section.get("checks", [])}
    installed = checks.get("plugin_installed") or checks.get("markers_plugin_installed")
    outdated = checks.get("markers_plugin_outdated")
    return {
        "installed": None if installed is None else (installed["ok"], installed["severity"], installed["current"]),
        "outdated": None
        if outdated is None
        else (
            outdated["ok"],
            outdated["severity"],
            outdated["current"],
            outdated["recommended"],
            outdated["label"],
        ),  # fmt: skip
    }


@row(11)
def row_11_setup_health_plugins() -> dict:
    """Setup Health, plugins on Jellyfin 10.11, 12.0 and Emby 4.10, 4.9: missing, then an older build (outdated), then
    the current build (all good)."""
    set_settings()
    stages: dict[str, dict[str, dict]] = {}
    cases = plugin_cases()
    held: list[PluginCase] = []
    try:
        for case in cases:
            sid = case.server_id
            stages[sid] = {"before": {"envelope": readiness_of(p1.app, sid),
                                      "capability": app_ok("GET", f"/api/markers/servers/{sid}/status")["capability"]}}  # fmt: skip
            held.append(case)  # before the move, so a hold that fails half way is restored too
            case.hold_current()
            for name, act in (("missing", None), ("outdated", case.install_old), ("current", case.restore_current)):
                if act:
                    act()
                want = {"missing": "needs_plugin", "outdated": "plugin_outdated", "current": "ready"}[name]
                capability = wait_capability(sid, want)
                stages[sid][name] = {"envelope": readiness_of(p1.app, sid), "capability": capability}
            held.remove(case)
    finally:
        cleanup_errors = p1.run_cleanup(*[case.restore_current for case in held])
    checks: dict[str, bool] = {}
    for case in cases:
        sid, seen = case.server_id, stages[case.server_id]
        rows = {name: plugin_rows(seen[name]["envelope"]) for name in ("before", "missing", "outdated", "current")}
        # A missing install row is a failed check below, not a crash: compare against a row that can't match.
        installed = {name: rows[name]["installed"] or (None, None, "") for name in rows}
        checks.update({
            f"{sid}: before, the plugin is installed and current (row passes, nothing outdated)": installed["before"][0] is True
            and rows["before"]["outdated"] is None,
            f"{sid}: missing: the install row fails as critical and reads not installed": rows["missing"]["installed"]
            == (False, "critical", "not installed"),
            f"{sid}: missing: no outdated row (there is no plugin to be old)": rows["missing"]["outdated"] is None,
            f"{sid}: missing: the Edit tab says needs_plugin": seen["missing"]["capability"]["state"] == "needs_plugin",
            f"{sid}: outdated: the install row passes and reads the installed version": installed["outdated"][:2]
            == (True, "critical")
            and case.old_version in str(installed["outdated"][2]),
            f"{sid}: outdated: the outdated row fails as recommended, {case.old_version} -> newest": rows["outdated"]["outdated"]
            == (False, "recommended", case.old_version, "newest", "The plugin is too old for intro and credits markers"),
            f"{sid}: outdated: the Edit tab says plugin_outdated": seen["outdated"]["capability"]["state"] == "plugin_outdated",
            f"{sid}: current: the install row passes": installed["current"][0] is True,
            f"{sid}: current: no outdated row": rows["current"]["outdated"] is None,
            f"{sid}: current: the Edit tab says ready": seen["current"]["capability"]["state"] == "ready",
        })  # fmt: skip
    checks["the lab was put back"] = not cleanup_errors
    premise = {
        f"{case.server_id} had the current plugin installed and ready before the swap": stages[case.server_id][
            "before"
        ]["capability"]["state"]
        == "ready"
        for case in cases
    }
    return checks_result(
        11,
        "Setup Health for the plugins: missing, outdated, current, on four servers",
        premise,
        checks,
        {"stages": stages},
    )


ROW12_STEPS = (
    "isolated",
    "published",
    "read_back",
    "locked",
    "wrong_key",
    "skew",
    "wrong_plex",
    "stopped",
    "cleaned_up",
)
# Same variables, and the same defaults, as phase4_row12_up.sh and phase4_row12_agent.py read.
ROW12_CONTAINERS = {
    "mlab-app-remote": os.environ.get("MLAB_APP_IMAGE", "plex-previews:phase4-lab"),
    "mlab-plex-agent": os.environ.get("MLAB_AGENT_IMAGE", "plex-marker-agent:phase4-lab"),
}


def image_id(reference: str) -> str:
    """The id of a local image (a tag or a container's image id)."""
    return sh("docker", "image", "inspect", reference, "--format", "{{.Id}}").strip()


@row(12)
def row_12_plex_marker_agent() -> dict:
    """The Plex marker agent, two containers: publish through it, a wrong key and a version mismatch refused, the
    agent stopped leaves Plex read-only with a clear message (phase4_row12_agent.py, every step)."""
    running = {name: sh("docker", "inspect", name, "--format", "{{.Image}}").strip() for name in ROW12_CONTAINERS}
    # The skew and wrong-Plex steps start containers of their own from this tag: the one the premise checks.
    os.environ["MLAB_AGENT_IMAGE"] = ROW12_CONTAINERS["mlab-plex-agent"]
    recorded = p1.row12_steps(*ROW12_STEPS)
    premise = {
        f"{name} runs the image under test ({tag})": running[name] == image_id(tag)
        for name, tag in ROW12_CONTAINERS.items()
    }
    checks = {f"step {name}": recorded[name].get("ok") is True for name in ROW12_STEPS}
    evidence = {"images": running, "steps": recorded}
    return checks_result(12, "The Plex marker agent: publish, refusals, agent stopped", premise, checks, evidence)


def main(argv: list[str]) -> int:
    """Run the matrix.

    Args:
        argv: ``configure``, ``rows``, or ``run`` and row numbers.

    Returns:
        0 when every row asked for passed, 1 when one did not, 2 for a bad command line.
    """
    if not argv or argv[0] not in ("configure", "run", "rows"):
        print(__doc__)
        return 2
    if argv[0] == "configure":
        p3.configure()
        return 0
    if argv[0] == "rows":
        for number, fn in sorted(ROWS.items()):
            print(f"{number:>2}  {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0
    numbers = [int(a) for a in argv[1:]]
    unknown = [n for n in numbers if n not in ROWS]
    if unknown:
        print(f"no such row: {unknown}; rows: {sorted(ROWS)}")
        return 2
    failed = 0
    for number in numbers:
        failed += ROWS[number]()["result"] != "pass"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
