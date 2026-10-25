"""Setup Health for one node: the same envelope and buckets as a server's Setup Health card.

``{"overall_ok", "sections": [{"id", "title", "docs_anchor", "ok", "severity", "checks": [row...]}]}``. A row is
"must fix" (``critical``) when it stops the node doing work, "recommended" otherwise.

A failing row says where its fix is: ``fix_tab`` (``media`` / ``settings`` / ``overview``, the node dialog tab, with
``fix_label`` naming the button, ``fix_field`` the Settings field it focuses (``url`` / ``key``; else the tab's first)
and, on media rows, the ``suggestion`` {from, to, app_root} the button applies),
``fix_where: "node"`` (only the node's own machine can fix it), or ``help_url`` + ``help_label`` (a page of this app).
``docs_url`` + ``help_label`` point at the docs section with the steps.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from typing import Any

from ..markers.readiness import CRITICAL, RECOMMENDED
from ..markers.readiness import _row as readiness_row
from . import protocol
from .registry import (
    OFFLINE_NOT_A_NODE,
    OFFLINE_RATE_LIMITED,
    OFFLINE_RESTARTING,
    OFFLINE_STUCK,
    STATE_ACCESS_PROBLEM,
    STATE_CONNECTING,
    STATE_DISABLED,
    STATE_DUPLICATE,
    STATE_GPU_LOST,
    STATE_KEY_REFUSED,
    STATE_NO_FFMPEG,
    STATE_OFFLINE,
    STATE_TEMP_PROBLEM,
    STATE_UPDATE_NEEDED,
    NodeDetails,
    NodeView,
    no_id_reason,
)

DOCS_ANCHOR = "nodes"
PLEX_DB_CHECK_S = 10.0
FIX_ON_NODE = "node"
TAB_MEDIA = "media"
TAB_SETTINGS = "settings"
# The Settings fields a Fix button focuses (``fix_field``): the node's address and its key.
FIELD_URL = "url"
FIELD_KEY = "key"
# A row that is neither a problem nor a pass of the node (the Nodes page shows it as a note).
INFO = "info"
WORKERS_URL = "/settings#section-workers"
# Where this app's own path mappings and library checks live.
SERVERS_URL = "/servers"
# The docs section with the steps to update a node to this app's version.
UPDATE_DOCS_URL = "https://github.com/stevezau/media_preview_generator/blob/main/docs/guides.md#updating-a-node"
# The last Plex database check of each (address, key hash, saved node id), for the node list (cached_file_checks).
_last_reports: dict[tuple[str, str, str], Any] = {}
_last_reports_lock = threading.Lock()
# The Linux Plex database node's image, still published under the Plex marker agent's name (nodes/plex-db/README.md).
PLEX_DB_IMAGE = "ghcr.io/stevezau/plex-marker-agent"
# Node-only row keys; the shared row builder doesn't know them.
_NODE_KEYS = ("fix_tab", "fix_field", "suggestion", "help_url", "help_label", "docs_url")
# An offline node that answered at its address: (row label, where the fix is). A restart ends by itself, and a node
# asking this app to wait is held back by something else at this app's address, so neither has a fix on the node.
_ANSWERED_OFFLINE = {
    OFFLINE_RESTARTING: ("{name} is restarting", None),
    OFFLINE_STUCK: ("{name} accepts connections but doesn't answer", FIX_ON_NODE),
    OFFLINE_RATE_LIMITED: ("{name} asks this app to wait", None),
}


def _row(
    check_id: str, label: str, *, severity: str, ok: bool, explanation: str, tooltip: str | None = None, **extra: Any
) -> dict[str, Any]:
    node_keys = {k: extra.pop(k) for k in _NODE_KEYS if k in extra}
    row = readiness_row(
        check_id,
        label,
        severity=severity,
        ok=ok,
        tooltip=tooltip or explanation,
        explanation=explanation,
        docs_anchor=DOCS_ANCHOR,
        **extra,
    )
    # A passing row has nothing to fix, so it carries no fix pointer.
    if not ok:
        row.update({k: v for k, v in node_keys.items() if v is not None})
        if row.get("fix_where") is None:
            row.pop("fix_where", None)
    else:
        row.pop("fix_where", None)
        row.pop("fix_label", None)
        row.pop("fix_action", None)
    return row


def _section(section_id: str, title: str, checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not checks:
        return None
    failing_critical = any(c["ok"] is False and c["severity"] == CRITICAL for c in checks)
    return {
        "id": section_id,
        "title": title,
        "docs_anchor": DOCS_ANCHOR,
        "ok": all(c["ok"] for c in checks),
        "severity": CRITICAL if failing_critical else RECOMMENDED,
        "checks": checks,
    }


def _connection(
    details: NodeDetails, app_version: str, app_build: str = "", node_uid: str = ""
) -> list[dict[str, Any]]:
    view = details.view
    if view.state == STATE_DISABLED:
        row = _row(
            "node_disabled",
            "Turned off — no work is sent",
            severity=INFO,
            ok=False,
            explanation="Nothing is checked or sent to it until you turn it on.",
        )
        # Neither a problem nor a pass: one neutral note, not an "All good" bucket around "is turned off".
        row["informational"] = True
        return [row]
    reachable = view.state not in (STATE_OFFLINE, STATE_CONNECTING)
    answered = _ANSWERED_OFFLINE.get(view.cause) if view.state == STATE_OFFLINE else None
    if answered is not None:
        # It answered at this address, so the address is right: "Check the address" would send the user astray.
        label, fix_where = answered
        return [
            _row(
                "node_reachable",
                label.format(name=view.name),
                severity=CRITICAL,
                ok=False,
                explanation="The app checks every node every 30 seconds.",
                reason=view.reason,
                fix_where=fix_where,
            )
        ]
    if reachable:
        label = f"{view.name} answers"
    elif view.cause == OFFLINE_NOT_A_NODE:
        label = f"Something else answers at {view.name}'s address"
    else:
        label = f"{view.name} isn't answering"
    rows = [
        _row(
            "node_reachable",
            label,
            severity=CRITICAL,
            ok=reachable,
            explanation="The app checks every node every 30 seconds.",
            reason=None if reachable else view.reason,
            fix_tab=TAB_SETTINGS,
            fix_label="Check the address",
            fix_field=FIELD_URL,
        )
    ]
    if not reachable:
        return rows
    key_ok = view.state != STATE_KEY_REFUSED
    rows.append(
        _row(
            "node_key",
            "The node accepts this app's key" if key_ok else "The node refused this app's key",
            severity=CRITICAL,
            ok=key_ok,
            explanation="The node only works for an app that sends its key.",
            reason=None if key_ok else view.reason,
            fix_tab=TAB_SETTINGS,
            fix_label="Change the key",
            fix_field=FIELD_KEY,
        )
    )
    if not key_ok:
        return rows
    if view.state == STATE_DUPLICATE:
        # What answered here is the other node's machine, so no row below would be about this node.
        rows.append(
            _row(
                "node_duplicate",
                "Added twice — remove one",
                severity=CRITICAL,
                ok=False,
                explanation="The same machine is added at two addresses. This app sends this one nothing, or the "
                "machine would get twice the work.",
                reason=view.reason,
                fix_tab=TAB_SETTINGS,
                fix_label="Open Settings",
            )
        )
        return rows
    from ..markers.publishers.plex_remote import MIN_AGENT_VERSION, agent_older_than

    # A saved id means a node that gives one answered here before: an older release that gives none is refused.
    rolled_back = view.legacy and bool(node_uid)
    version_ok = view.state != STATE_UPDATE_NEEDED and not rolled_back
    # A worker node ahead of this app is matched by updating this app, so nothing points at the node's machine.
    app_behind = not version_ok and protocol.ROLE_WORKER in view.roles and agent_older_than(app_version, view.version)
    # The registry holds a worker to this app's exact version, a Plex database node only to a minimum one.
    worker = protocol.ROLE_WORKER in view.roles or not view.roles
    if worker:
        passing = "The node's version matches this app"
        explanation = "A worker node runs this app's own code, so it must be the same version."
    else:
        passing = "The node's version is new enough"
        explanation = (
            f"A Plex database node needs version {MIN_AGENT_VERSION} or newer of its Plex database role. It doesn't "
            "have to match this app's version."
        )
    shown = protocol.node_version(details.info) if details.info else ""
    current = shown or view.version or "unknown"
    recommended = app_version if protocol.ROLE_WORKER in view.roles and not app_behind else None
    node_block = (details.info or {}).get("node")
    node_build = str(node_block.get("build") or "") if isinstance(node_block, dict) else ""
    if recommended and view.version == app_version and not protocol.same_build(node_build, app_build):
        # The same version on both sides: only the commits tell the two builds apart.
        current, recommended = f"{view.version} ({node_build[:7]})", f"{app_version} ({app_build[:7]})"
    rows.append(
        _row(
            "node_version",
            passing if version_ok else "This app needs an update" if app_behind else "The node needs an update",
            severity=CRITICAL,
            ok=version_ok,
            explanation=explanation,
            current=current,
            recommended=recommended,
            reason=None if version_ok else no_id_reason(view.name) if rolled_back else view.reason,
            fix_where=None if app_behind else FIX_ON_NODE,
            docs_url=None if app_behind else UPDATE_DOCS_URL,
            help_label=None if app_behind else "How to update a node",
        )
    )
    if version_ok and protocol.ROLE_WORKER in view.roles:
        ffmpeg_ok = view.state != STATE_NO_FFMPEG
        rows.append(
            _row(
                "node_ffmpeg",
                "The node has FFmpeg" if ffmpeg_ok else "FFmpeg isn't installed on this node",
                severity=CRITICAL,
                ok=ffmpeg_ok,
                explanation="A worker node runs FFmpeg for every file it is sent.",
                reason=None if ffmpeg_ok else view.reason,
                fix_where=FIX_ON_NODE,
            )
        )
    if version_ok and protocol.ROLE_WORKER in view.roles and view.state != STATE_NO_FFMPEG:
        temp_ok = view.state != STATE_TEMP_PROBLEM
        rows.append(
            _row(
                "node_temp",
                "The node can use its temp folder" if temp_ok else "The node can't use its temp folder",
                severity=CRITICAL,
                ok=temp_ok,
                explanation="A worker node makes a folder there for every file it is sent (NODE_TEMP_DIR).",
                reason=None if temp_ok else view.reason,
                fix_where=FIX_ON_NODE,
            )
        )
    if view.legacy and not rolled_back:
        from .server.plex_db import AGENT_VERSION

        rows.append(
            _row(
                "node_legacy_helper",
                "This Plex database node is an older release",
                severity=RECOMMENDED,
                ok=False,
                explanation="It keeps working for Plex's database. Updating it adds health details and the newer "
                "Plex database features.",
                reason=f"Update its image to {PLEX_DB_IMAGE}:{AGENT_VERSION}.",
                fix_where=FIX_ON_NODE,
                docs_url=UPDATE_DOCS_URL,
                help_label="How to update a node",
            )
        )
    return rows


def _media_access(details: NodeDetails) -> list[dict[str, Any]]:
    view = details.view
    rows: list[dict[str, Any]] = []
    if view.state == STATE_ACCESS_PROBLEM:
        rows.append(
            _row(
                "node_access_problem",
                f"{view.name} couldn't see files it was sent",
                severity=CRITICAL,
                ok=False,
                explanation="Its workers are paused until a Test access, or the check every 5 minutes, passes.",
                reason=view.reason,
                fix_tab=TAB_MEDIA,
                fix_label="Test access",
                fix_action="test_access",
            )
        )
    result = details.access_result
    if not isinstance(result, dict):
        rows.append(
            _row(
                "node_access_untested",
                "Run Test access",
                severity=RECOMMENDED,
                ok=False,
                explanation="Test access checks a few files from every library through this node's folder mappings.",
                fix_tab=TAB_MEDIA,
                fix_label="Test access",
                fix_action="test_access",
            )
        )
        return rows
    skipped: list[str] = []
    for index, lib in enumerate(result.get("libraries") or []):
        if not isinstance(lib, dict):
            continue
        label = f"{lib.get('server')} · {lib.get('library')}"
        if not lib.get("checked") and lib.get("empty"):
            rows.append(
                _row(
                    f"node_access_{index}",
                    f"{label}: no video files in this library yet",
                    severity=RECOMMENDED,
                    ok=True,
                    explanation="Test access found no video near the top of this library's folder, so there was "
                    "nothing to ask the node about.",
                )
            )
            continue
        if not lib.get("checked"):
            # Nothing was sampled: this app can't open the folder itself or timed out reading it, so the node was
            # never asked. It isn't the node's problem, so all of them share one neutral line below. A result is one
            # folder, so a library with two folders is counted once.
            if label not in skipped:
                skipped.append(label)
            continue
        ok = bool(lib.get("ok"))
        missing = [f for f in lib.get("files") or [] if isinstance(f, dict) and not f.get("ok")]
        reason = None
        if not ok:
            reason = lib.get("error") or (missing[0].get("message") if missing else None)
        suggestion = lib.get("suggestion") if isinstance(lib.get("suggestion"), dict) else None
        rows.append(
            _row(
                f"node_access_{index}",
                f"{label}: the node sees its files" if ok else f"{label}: the node can't see its files",
                severity=CRITICAL,
                ok=ok,
                # The explanation is shown as HTML, so it stays literal; the folder, a media server's path that nobody
                # escapes, goes in the tooltip, which the page escapes.
                explanation="Test access checked a few files from this library through this node's folder mappings.",
                tooltip=f"Checked {lib.get('checked', 0)} file(s) under {lib.get('app_root')}.",
                current=suggestion.get("from") if suggestion else None,
                recommended=suggestion.get("to") if suggestion else None,
                reason=reason,
                fix_tab=TAB_MEDIA,
                fix_label="Use the suggested path" if suggestion else "Open Media",
                suggestion={**suggestion, "app_root": lib.get("app_root")} if suggestion else None,
            )
        )
    if skipped:
        count = len(skipped)
        row = _row(
            "node_access_skipped",
            f"{count} {'library' if count == 1 else 'libraries'} skipped: this app can't open "
            f"{'it' if count == 1 else 'them'}",
            severity=INFO,
            ok=False,
            explanation="Test access needs a file this app sees to ask the node for, so these weren't tested. That is "
            "about this app's own access to them, not the node: fix it on Media Servers.",
            reason=", ".join(skipped),
            help_url=SERVERS_URL,
            help_label="Fix on Media Servers",
        )
        # A note, not a problem of the node: shown on its own line, outside the Must fix / Recommended buckets.
        row["informational"] = True
        rows.append(row)
    return rows


def _members_using(node_id: str, groups: object, resource: str | None = None) -> int:
    if not isinstance(groups, list):
        return 0
    return sum(
        1
        for group in groups
        if isinstance(group, dict)
        for member in group.get("members") or []
        if isinstance(member, dict)
        and member.get("node") == node_id
        and (resource is None or member.get("resource") == resource)
    )


def _workers(details: NodeDetails, worker_groups: object) -> list[dict[str, Any]]:
    view = details.view
    used = _members_using(view.id, worker_groups) > 0
    rows = [
        _row(
            "node_in_group",
            "Used in a worker group" if used else "Not used in any worker group yet",
            severity=RECOMMENDED,
            ok=used,
            explanation="A node runs work only for the worker groups it is a member of.",
            reason=None if used else "Add this node's GPU or CPU to a worker group on the Workers page.",
            help_url=WORKERS_URL,
            help_label="Open Workers",
        )
    ]
    # The view drops each GPU's error; the details keep it, and it says what the test found.
    errors = {g.get("device"): g.get("error") for g in details.gpus}
    for gpu in view.gpus:
        ok = gpu.get("status") == "ok"
        name = gpu.get("name") or gpu.get("device") or "GPU"
        rows.append(
            _row(
                f"node_gpu_{gpu.get('device')}",
                f"{name} works" if ok else f"{name} failed its test",
                severity=CRITICAL,
                ok=ok,
                explanation="The node tests each GPU when it starts, every 5 minutes, and after a GPU error.",
                current=gpu.get("status") or "unknown",
                recommended="ok",
                reason=None if ok else _gpu_reason(errors.get(gpu.get("device"))),
                fix_where=FIX_ON_NODE,
            )
        )
    explained = any(gpu.get("status") != "ok" for gpu in view.gpus)
    if view.state == STATE_GPU_LOST and not explained:
        # The GPU is gone from the node's list, or no detection has worked yet: no GPU row above says why its GPU
        # workers are paused. A node used only for CPU work has none to pause.
        paused = _members_using(view.id, worker_groups, resource="gpu") > 0
        rows.append(
            _row(
                "node_gpu_lost",
                f"{view.name}'s GPU workers are paused" if paused else f"{view.name} can't use its GPU",
                severity=CRITICAL,
                ok=False,
                explanation="The node tests each GPU when it starts, every 5 minutes, and after a GPU error.",
                reason=view.reason,
                fix_where=FIX_ON_NODE,
            )
        )
    return rows


def _gpu_reason(error: object) -> str:
    what = str(error or "").strip().rstrip(".")
    found = f"{what}. " if what else ""
    return f"{found}Check the GPU driver on the node, then Rescan GPUs on its Overview tab."


def _report_key(url: str, key: str, node_uid: str) -> tuple[str, str, str]:
    return url, hashlib.sha256(key.encode()).hexdigest(), node_uid


def _default_file_checks(url: str, key: str, node_uid: str = "", name: str = "") -> Any:
    from ..markers.publishers.plex_remote import RemotePlexDb

    # Named like every request to the node: another machine at the address refuses, rather than answering with its
    # own Plex database as this node's.
    db = RemotePlexDb(url, key, name=name, node_uid=lambda: node_uid)
    report = db.file_checks(deadline=time.monotonic() + PLEX_DB_CHECK_S)
    with _last_reports_lock:
        _last_reports[_report_key(url, key, node_uid)] = report
    return report


def cached_file_checks(url: str, key: str, node_uid: str = "", name: str = "") -> Any:
    """The last Plex database check Setup Health ran on the node at ``url`` with ``key``, or None (never asks it).

    ``name`` is the node's, which the check's messages call it by; it isn't part of which check this is.

    The node list counts its rows from this, so a card stops saying Ready while its Setup Health has a must fix,
    without a call to every node on every poll.
    """
    with _last_reports_lock:
        return _last_reports.get(_report_key(url, key, node_uid))


def _plex_db_confirmed(details: NodeDetails) -> list[dict[str, Any]]:
    view = details.view
    if not view.plex_db_unconfirmed:
        return []
    return [
        _row(
            "plex_db_confirmed",
            f"{view.name} reports a new id",
            severity=CRITICAL,
            ok=False,
            explanation="Another machine given the same address and key would report a new id too, so this app sends "
            "it no Plex database work until you confirm it is the same Plex.",
            reason="Type the node's key again on its Settings tab to confirm it is the same Plex.",
            fix_tab=TAB_SETTINGS,
            fix_label="Type the key again",
            fix_field=FIELD_KEY,
        )
    ]


def _plex_db(
    view: NodeView, key: str, node_uid: str, file_checks: Callable[[str, str, str, str], Any]
) -> list[dict[str, Any]]:
    from ..markers.publishers.base import Capability
    from ..markers.publishers.plex_db import node_wording

    checked = file_checks(view.url, key, node_uid, view.name)
    if checked is None:
        return []  # Not checked yet (cached_file_checks): no rows rather than a guess.
    # The check ran on the node: its refusals are about the node, not this app.
    report = node_wording(checked)
    details = report.details or {}
    if report.state is Capability.AGENT_UNAVAILABLE:
        return [
            _row(
                "plex_db_reachable",
                "The Plex database role can't be used",
                severity=CRITICAL,
                ok=False,
                explanation="The node opens Plex's database on Plex's own machine.",
                # An older release refuses the saved id every request names: the version row's advice, not its
                # protocol number.
                reason=no_id_reason(view.name) if view.legacy and node_uid else report.message,
                fix_where=FIX_ON_NODE,
            )
        ]
    present = "db_path" in details
    rows = [
        _row(
            "plex_db_present",
            "Plex's database is found" if present else "Plex's database isn't found",
            severity=CRITICAL,
            ok=present,
            explanation="The node looks for it in the Plex folder it was started with.",
            current=details.get("db_path"),
            reason=None if present else report.message,
            fix_where=FIX_ON_NODE,
        )
    ]
    if not present:
        return rows
    local = report.state is not Capability.NEEDS_LOCAL_DB
    rows.append(
        _row(
            "plex_db_local",
            "It is on a local disk" if local else "It isn't on a local disk",
            severity=CRITICAL,
            ok=local,
            explanation="SQLite can only be written safely on the disk Plex itself uses.",
            current=details.get("fs_type"),
            reason=None if local else report.message,
            fix_where=FIX_ON_NODE,
        )
    )
    if not local:
        return rows
    writable = not details.get("unwritable")
    rows.append(
        _row(
            "plex_db_writable",
            "The node can write it" if writable else "The node can't write it",
            severity=CRITICAL,
            ok=writable,
            explanation="Markers and chapters are written into Plex's database.",
            reason=None if writable else report.message,
            fix_where=FIX_ON_NODE,
        )
    )
    if not writable:
        return rows
    held = details.get("lock_holder") is True
    rows.append(
        _row(
            "plex_db_held",
            "Plex has it open" if held else "Plex doesn't have it open",
            severity=CRITICAL,
            ok=held,
            explanation="The node writes only while Plex is running from the same folder.",
            reason=None if held else report.message,
            fix_where=FIX_ON_NODE,
        )
    )
    return rows


def node_readiness(
    details: NodeDetails,
    *,
    key: str,
    worker_groups: object,
    app_version: str,
    app_build: str = "",
    node_uid: str = "",
    file_checks: Callable[[str, str, str, str], Any] | None = _default_file_checks,
) -> dict[str, Any]:
    """The Setup Health envelope for one node.

    Args:
        details: The node's registry details.
        key: The node's stored key (for the Plex database file checks).
        worker_groups: The stored ``worker_groups`` list.
        app_version: This app's version.
        app_build: This app's commit ("" when unknown); shown when only the node's commit differs.
        node_uid: The node's saved id ("" before first contact), named on the Plex database file checks.
        file_checks: ``(url, key, node_uid, name) -> CapabilityReport`` for the Plex database role (tests pass their
            own);
            an answer of None (not checked yet) adds no rows. None leaves the database checks out. The node list's
            count passes :func:`cached_file_checks`: asking the node is too slow for it.

    Returns:
        ``{"overall_ok", "sections"}``.
    """
    view = details.view
    sections = [_section("node_connection", "Connection", _connection(details, app_version, app_build, node_uid))]
    usable = view.state not in (STATE_DISABLED, STATE_OFFLINE, STATE_CONNECTING, STATE_KEY_REFUSED, STATE_DUPLICATE)
    worker = protocol.ROLE_WORKER in view.roles or not view.roles
    if usable and worker and not view.legacy:
        sections.append(_section("node_media_access", "Media access", _media_access(details)))
        sections.append(_section("node_workers", "Workers", _workers(details, worker_groups)))
    if usable and protocol.ROLE_PLEX_DB in view.roles:
        rows = _plex_db_confirmed(details)
        if file_checks is not None:
            rows += _plex_db(view, key, node_uid, file_checks)
        sections.append(_section("node_plex_db", "Plex database", rows))
    kept = [s for s in sections if s is not None]
    overall_ok = not any(c["severity"] == CRITICAL and c["ok"] is False for s in kept for c in s["checks"])
    return {"overall_ok": overall_ok, "sections": kept}


def to_fix(envelope: dict[str, Any]) -> dict[str, int]:
    """How many rows of a Setup Health envelope are in its Must fix and Recommended buckets.

    Args:
        envelope: What :func:`node_readiness` returns.

    Returns:
        ``{"must_fix": n, "recommended": n}``, counted as the Nodes page buckets them.
    """
    must_fix = recommended = 0
    for section in envelope.get("sections") or []:
        for check in section.get("checks") or []:
            if check.get("ok") is not False:
                continue
            if check.get("severity") == CRITICAL:
                must_fix += 1
            elif check.get("severity") == RECOMMENDED and check.get("dismissed") is not True:
                recommended += 1
    return {"must_fix": must_fix, "recommended": recommended}
