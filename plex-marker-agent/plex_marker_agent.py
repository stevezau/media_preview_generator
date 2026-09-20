"""The Plex marker agent: the app's database half, running on the machine Plex runs on.

Plex has no API for intro/credits markers, so the app writes them into Plex's own database — and SQLite's WAL locks
only work between processes on the same machine. An app that runs elsewhere therefore can't write markers at all.
This service closes that gap: run it beside Plex with Plex's config folder mounted, point the app at it, and the app
asks it to run the very same code it would have run itself
(``media_preview_generator.markers.publishers.plex_db.LocalPlexDb``).

What it is not: it is not a remote-control API for Plex. Its endpoints are all about one item's markers, and:

* the database path comes from this container's own ``PLEX_CONFIG_DIR`` — never from the caller;
* it runs no SQL the caller supplies, and answers nothing else about the library;
* it refuses exactly what the in-process writer refuses: a database that isn't on a local disk *here*, a schema it
  doesn't know, a missing or duplicated Plex marker tag row (it never creates one);
* every request needs the shared key, and a caller speaking another protocol version is refused by name.

Run it with one worker (``gunicorn --workers 1``): the app's "one writer at a time" guarantee is a lock inside one
process, and the database's own lock proof assumes this process is the only one of ours holding it.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from loguru import logger

from media_preview_generator.markers.publishers import plex_remote
from media_preview_generator.markers.publishers.base import PublishError
from media_preview_generator.markers.publishers.plex_db import LocalPlexDb, plex_db_path

# This build's version. The app refuses an agent older than its ``plex_remote.MIN_AGENT_VERSION`` and says both
# numbers, so an image left behind on the Plex host is a message, never a wrong write.
AGENT_VERSION = "1.0.0"
# The wire contracts this build implements. Add to it (never replace) while older apps are still in the wild.
PROTOCOLS = [plex_remote.AGENT_PROTOCOL]
# A request body is a marker set and one item's parts: kilobytes. Anything larger is refused before it is parsed.
MAX_BODY_BYTES = 2 * 1024 * 1024
# The longest any one request may make this process wait for the database locks, whatever the caller asks for.
MAX_DEADLINE_S = 120.0
# The longest one read-back request keeps reading before answering with what it has.
MAX_BUDGET_S = 120.0
READ_BACK_MAX_ITEMS = 200
DEFAULT_PORT = 9494


# Plex writes its own server id into Preferences.xml beside the database. The app compares it with the
# machineIdentifier its Plex reports, so an agent set up beside the wrong Plex is refused instead of writing another
# server's database. Read per request: it changes when a server is claimed.
_MACHINE_ID_RE = re.compile(r'ProcessedMachineIdentifier="([^"]+)"')


def machine_identifier(config_dir: str) -> str:
    """Plex's own server id from ``Preferences.xml``, or "" when it can't be read (no database is opened).

    Args:
        config_dir: Plex's config folder as this container sees it.

    Returns:
        The identifier, or "" — which the app treats as "unknown", never as a mismatch.
    """
    try:
        text = Path(config_dir, "Preferences.xml").read_text(errors="replace")
    except OSError:
        return ""
    found = _MACHINE_ID_RE.search(text)
    return found.group(1) if found else ""


def _agent_block() -> dict:
    return {"version": AGENT_VERSION, "protocols": list(PROTOCOLS)}


def _ok(result: dict) -> Any:
    return jsonify({"ok": True, "agent": _agent_block(), "result": result})


def _refused(exc: PublishError) -> Any:
    # 409: the agent worked, the database said no. The app rebuilds this exact exception (plex_remote).
    return jsonify({"ok": False, "agent": _agent_block(), "error": plex_remote.error_to_json(exc)}), 409


def _bad_request(message: str) -> Any:
    return jsonify({"ok": False, "agent": _agent_block(), "error": {"kind": "request", "message": message}}), 400


def _deadline(body: dict, key: str = "deadline_s", limit: float = MAX_DEADLINE_S) -> float:
    try:
        asked = float(body.get(key) or 0.0)
    except (TypeError, ValueError):
        asked = 0.0
    return time.monotonic() + max(0.0, min(asked, limit))


def create_app(
    *, config_dir: str | None = None, token: str | None = None, mountinfo_path: str = "/proc/self/mountinfo"
) -> Flask:
    """Build the agent.

    Args:
        config_dir: Plex's config folder as this container sees it (default: ``PLEX_CONFIG_DIR``). The database path
            is derived from it and never taken from a request.
        token: The key both sides share (default: ``AGENT_TOKEN``). The agent refuses to start without one.
        mountinfo_path: For tests.

    Returns:
        The Flask app.

    Raises:
        SystemExit: No shared key was set.
    """
    folder = (config_dir if config_dir is not None else os.environ.get("PLEX_CONFIG_DIR", "")).strip()
    shared_key = (token if token is not None else os.environ.get("AGENT_TOKEN", "")).strip()
    if not shared_key:
        raise SystemExit(
            "Set AGENT_TOKEN to the same shared key you paste into the app; the agent won't start without one."
        )
    # The app refuses to store a key that isn't printable ASCII (``markers/settings.py``), so a key with anything
    # else in it could never match one the app sends: every request would be refused with a 401 that looks like a
    # typo on the app's side. Said here once, at start, instead.
    if not shared_key.isascii():
        raise SystemExit("AGENT_TOKEN must be plain ASCII: the app can't send a key with any other character in it.")
    if not folder:
        raise SystemExit("Set PLEX_CONFIG_DIR to Plex's config folder as this container sees it.")

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    database = LocalPlexDb(lambda: plex_db_path(folder), label="agent", mountinfo_path=mountinfo_path)
    shared_key_bytes = shared_key.encode("utf-8", "surrogateescape")

    def authorised() -> bool:
        header = request.headers.get("Authorization", "")
        offered = header[7:].strip() if header.startswith("Bearer ") else ""
        # compare_digest: the same constant-time check the app's own API does for its token (web/auth.py). The
        # BYTES, not the str: werkzeug decodes headers as latin-1, and compare_digest raises TypeError on a str
        # holding anything above U+007F -- a key with one non-ASCII byte would 500 on the auth path, not 401.
        return bool(offered) and secrets.compare_digest(offered.encode("utf-8", "surrogateescape"), shared_key_bytes)

    def speaks_our_protocol() -> bool:
        asked = request.headers.get(plex_remote.PROTOCOL_HEADER, "").strip()
        return asked.isdigit() and int(asked) in PROTOCOLS

    @app.before_request
    def _guard() -> Any:
        if request.path == "/v1/health":
            return None
        if not authorised():
            # No detail: an unauthenticated caller learns nothing about this Plex.
            return jsonify({"ok": False, "agent": _agent_block(), "error": {"kind": "auth"}}), 401
        # /v1/ping is exempt from the protocol check on purpose: when the two sides disagree about the protocol,
        # "which version are you?" is the question the operator needs answered.
        if request.path != "/v1/ping" and not speaks_our_protocol():
            return jsonify(
                {
                    "ok": False,
                    "agent": _agent_block(),
                    "error": {
                        "kind": "protocol",
                        "message": (
                            f"This Plex marker agent is version {AGENT_VERSION} and speaks protocol "
                            f"{', '.join(str(p) for p in PROTOCOLS)}; the app asked for "
                            f"{request.headers.get(plex_remote.PROTOCOL_HEADER, 'none')}."
                        ),
                    },
                }
            ), 409
        if request.method == "POST" and not isinstance(request.get_json(silent=True), dict):
            return _bad_request("The request body must be a JSON object")
        return None

    @app.get("/v1/health")
    def health() -> Any:
        """Liveness for the container's HEALTHCHECK: no key, no database, nothing about the library."""
        return jsonify({"ok": True, "agent": _agent_block()})

    @app.get("/v1/ping")
    def ping() -> Any:
        """A shell check (`curl -H "Authorization: Bearer $AGENT_TOKEN" .../v1/ping`): the version, which Plex this
        agent serves, and whether the database file is even there. It answers whatever protocol the caller speaks,
        so it still works when an app and an agent disagree. The app doesn't call it — its own "Check again" runs
        the two checks below.
        """
        return _ok(
            {"db_present": os.path.isfile(plex_db_path(folder)), "machine_identifier": machine_identifier(folder)}
        )

    @app.post("/v1/checks/file")
    def checks_file() -> Any:
        """The database file's own checks, run here: found, local disk, writable, held open by Plex."""
        body = request.get_json(silent=True) or {}
        report = database.file_checks(deadline=_deadline(body))
        # The identity travels with the file checks so the app can refuse an agent next to a different Plex before
        # it writes anything (``PlexMarkerPublisher._another_plexs_agent``).
        return _ok({"report": plex_remote.report_to_json(report), "machine_identifier": machine_identifier(folder)})

    @app.post("/v1/checks/db")
    def checks_db() -> Any:
        """The schema, the marker data and Plex's single marker tag row."""
        body = request.get_json(silent=True) or {}
        report = database.db_checks(deadline=_deadline(body))
        return _ok({"report": plex_remote.report_to_json(report)})

    @app.post("/v1/item/read")
    def item_read() -> Any:
        """One item's live parts."""
        body = request.get_json(silent=True) or {}
        rating_key = body.get("rating_key")
        if not isinstance(rating_key, int) or isinstance(rating_key, bool) or rating_key < 0:
            return _bad_request("rating_key must be a whole number")
        try:
            item = database.read_item(rating_key, deadline=_deadline(body))
        except PublishError as exc:
            return _refused(exc)
        return _ok({"item": plex_remote.item_read_to_json(item)})

    @app.post("/v1/item/write")
    def item_write() -> Any:
        """Write one item's markers, in one transaction here."""
        body = request.get_json(silent=True) or {}
        try:
            write = plex_remote.write_request_from_json(body.get("write"))
        except ValueError as exc:
            return _bad_request(str(exc))
        try:
            result = database.write_item(write, deadline=_deadline(body))
        except PublishError as exc:
            return _refused(exc)
        logger.info(
            "item {}: {} ({} marker(s) ours)", write.rating_key, "changed" if result.changed else "no change",
            len(result.ours),
        )  # fmt: skip
        return _ok({"write": plex_remote.write_result_to_json(result)})

    @app.post("/v1/items/shown")
    def items_shown() -> Any:
        """Read items back: what each shows of what the app left on it."""
        body = request.get_json(silent=True) or {}
        raw_items = body.get("items")
        if not isinstance(raw_items, list) or len(raw_items) > READ_BACK_MAX_ITEMS:
            return _bad_request(f"items must be a list of at most {READ_BACK_MAX_ITEMS} entries")
        try:
            asks = [plex_remote.shown_ask_from_json(item) for item in raw_items]
        except ValueError as exc:
            return _bad_request(str(exc))
        try:
            timeout_s = max(0.0, min(float(body.get("timeout_s") or 0.0), MAX_DEADLINE_S))
        except (TypeError, ValueError):
            return _bad_request("timeout_s must be a number")
        budget = _deadline(body, "budget_s", MAX_BUDGET_S)
        batch = database.shown_many(asks, timeout_s=timeout_s, cancel_check=lambda: time.monotonic() >= budget)
        return _ok({"batch": plex_remote.shown_batch_to_json(batch)})

    @app.post("/v1/item/exists")
    def item_exists() -> Any:
        """Whether this Plex still has an item with that rating key."""
        body = request.get_json(silent=True) or {}
        rating_key = body.get("rating_key")
        if not isinstance(rating_key, int) or isinstance(rating_key, bool) or rating_key < 0:
            return _bad_request("rating_key must be a whole number")
        try:
            exists = database.item_exists(rating_key, deadline=_deadline(body))
        except PublishError as exc:
            return _refused(exc)
        return _ok({"exists": exists})

    logger.info("Plex marker agent {} ready; Plex database: {}", AGENT_VERSION, plex_db_path(folder))
    return app


__all__ = ["AGENT_VERSION", "PROTOCOLS", "create_app"]
