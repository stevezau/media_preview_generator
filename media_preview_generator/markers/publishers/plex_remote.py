"""The Plex marker agent's wire protocol, and the transport that speaks it (spec §3.1, §6.3; plan phase 4 Task 10).

Plex's database can only be written from the machine Plex runs on (SQLite's WAL locks are not shared over a network
filesystem), so an app on another machine has no way in. The **Plex marker agent** is a small container the user runs
beside Plex with Plex's config folder mounted: it runs ``plex_db.LocalPlexDb`` there, and this module is how the app
asks it to. Both sides use the codec below, so a remote write is the same code as a local one — the agent is a
transport for arguments, never a second implementation of the rules.

What the agent will not do: it takes no database path from the app (its own ``PLEX_CONFIG_DIR`` decides), it exposes
no SQL, and it refuses everything ``LocalPlexDb`` refuses (a database that isn't on a local disk **on its side**, a
schema it doesn't know, a missing or duplicated marker tag row).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests
from loguru import logger

from ..models import Marker, MarkerType
from .base import Capability, CapabilityReport, ItemNotFoundError, PublishError, Shown
from .plex_db import (
    ItemRead,
    LocalPlexDb,
    PlexDatabase,
    ShownAnswer,
    ShownAsk,
    ShownBatch,
    WriteRequest,
    WriteResult,
    _Part,
)

# The wire contract. Bumped only when a request or answer changes shape; an agent says which protocols it implements
# and refuses the others, so a skew in either direction is a refusal with both versions named, never a wrong write.
AGENT_PROTOCOL = 1
PROTOCOL_HEADER = "X-Marker-Agent-Protocol"
# The oldest agent build this app works with. The agent's version is its own (its image tag), not the app's.
MIN_AGENT_VERSION = "1.0.0"
# Seconds allowed for the TCP connect, and for the agent to answer after the deadline it was given has passed.
CONNECT_TIMEOUT_S = 4.0
ANSWER_GRACE_S = 3.0
# One read-back request: at most this many items, and the agent stops reading after this long and answers with what
# it has (the items left are simply unread, exactly as a cancelled local read-back leaves them).
READ_BACK_CHUNK = 50
READ_BACK_BUDGET_S = 60.0
# Agent states the Edit tab badges (``capability().details["agent"]["state"]``).
AGENT_CONNECTED = "connected"
AGENT_UNREACHABLE = "unreachable"
AGENT_REJECTED = "rejected"
AGENT_INCOMPATIBLE = "incompatible"


class AgentError(PublishError):
    """The agent couldn't be used (unreachable, wrong key, wrong version) — not a failure of the database itself."""


def _version_tuple(version: str) -> tuple[int, ...] | None:
    parts = str(version or "").strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def agent_too_old(version: str) -> bool:
    """Whether an agent's reported version is older than ``MIN_AGENT_VERSION``.

    Args:
        version: The version the agent reports.

    Returns:
        True only when both versions parse as dotted numbers and the agent's is lower. A version this app can't read
        (including the ``""`` of an answer with no ``agent`` block at all) is not "too old": neither this check nor
        the protocol one -- which passes an empty ``protocols`` list (``_read``) -- refuses such an answer on its own.
        Something that isn't our agent is turned away by the ``result`` it doesn't send, as "answered N instead of a
        result".
    """
    theirs, mine = _version_tuple(version), _version_tuple(MIN_AGENT_VERSION)
    return bool(theirs and mine and theirs < mine)


# --------------------------------------------------------------------------- codec
# One place both sides encode and decode, so the app and the agent can never disagree about a field.


def marker_to_json(marker: Marker) -> dict:
    """Serialise one decided marker."""
    return {
        "type": marker.type.value,
        "start_ms": marker.start_ms,
        "end_ms": marker.end_ms,
        "decided_by": list(marker.decided_by),
        "locked": marker.locked,
    }


def marker_from_json(raw: Any) -> Marker:
    """Read one decided marker.

    Raises:
        ValueError: The value isn't a marker.
    """
    if not isinstance(raw, dict):
        raise ValueError("marker must be an object")
    try:
        mtype = MarkerType(raw["type"])
        start, end = _as_int(raw["start_ms"]), _as_int(raw["end_ms"])
        decided_by = tuple(str(x) for x in raw.get("decided_by") or ())
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a marker: {exc}") from exc
    return Marker(mtype, start, end, decided_by, bool(raw.get("locked")))


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{value!r} is not a whole number")
    return value


def _as_opt_int(value: Any) -> int | None:
    return None if value is None else _as_int(value)


def _as_opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not text")
    return value


def part_to_json(part: _Part) -> dict:
    """Serialise one ``media_parts`` row."""
    return {
        "id": part.id,
        "media_item_id": part.media_item_id,
        "file": part.file,
        "extra_data": part.extra_data,
        "proxy_type": part.proxy_type,
    }


def part_from_json(raw: Any) -> _Part:
    """Read one ``media_parts`` row.

    Raises:
        ValueError: The value isn't a part.
    """
    if not isinstance(raw, dict):
        raise ValueError("part must be an object")
    try:
        return _Part(
            _as_int(raw["id"]),
            _as_int(raw["media_item_id"]),
            str(raw["file"]),
            _as_opt_str(raw.get("extra_data")),
            _as_opt_int(raw.get("proxy_type")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a media part: {exc}") from exc


def _types_to_json(types: frozenset[MarkerType]) -> list[str]:
    return sorted(t.value for t in types)


def _types_from_json(raw: Any) -> frozenset[MarkerType]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise ValueError("marker types must be a list")
    return frozenset(MarkerType(str(t)) for t in raw)


def _markers_from_json(raw: Any) -> list[Marker]:
    if not isinstance(raw, list):
        raise ValueError("markers must be a list")
    return [marker_from_json(m) for m in raw]


def write_request_to_json(request: WriteRequest) -> dict:
    """Serialise one item's write."""
    return {
        "rating_key": request.rating_key,
        "parts": [part_to_json(p) for p in request.parts],
        "wanted": [marker_to_json(m) for m in request.wanted],
        "prior": [marker_to_json(m) for m in request.prior],
        "duration_ms": request.duration_ms,
        "own_prior": [marker_to_json(m) for m in request.own_prior],
        "calling_part_ids": list(request.calling_part_ids),
        "kept_types": _types_to_json(request.kept_types),
        "keep_plex": request.keep_plex,
    }


def write_request_from_json(raw: Any) -> WriteRequest:
    """Read one item's write.

    Raises:
        ValueError: The body isn't a write request.
    """
    if not isinstance(raw, dict):
        raise ValueError("write must be an object")
    try:
        return WriteRequest(
            rating_key=_as_int(raw["rating_key"]),
            parts=[part_from_json(p) for p in raw["parts"]],
            wanted=_markers_from_json(raw["wanted"]),
            prior=_markers_from_json(raw["prior"]),
            duration_ms=_as_opt_int(raw.get("duration_ms")),
            own_prior=_markers_from_json(raw.get("own_prior") or []),
            calling_part_ids=tuple(_as_int(i) for i in raw.get("calling_part_ids") or ()),
            kept_types=_types_from_json(raw.get("kept_types")),
            keep_plex=bool(raw.get("keep_plex")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a write request: {exc}") from exc


def write_result_to_json(result: WriteResult) -> dict:
    """Serialise what one write did."""
    return {
        "changed": result.changed,
        "ours": [marker_to_json(m) for m in result.ours],
        "kept_types": _types_to_json(result.kept_types),
        "replaced_own": _types_to_json(result.replaced_own),
    }


def write_result_from_json(raw: Any) -> WriteResult:
    """Read what one write did.

    Raises:
        ValueError: The answer isn't a write result.
    """
    if not isinstance(raw, dict):
        raise ValueError("write result must be an object")
    try:
        return WriteResult(
            bool(raw["changed"]),
            _markers_from_json(raw["ours"]),
            _types_from_json(raw.get("kept_types")),
            _types_from_json(raw.get("replaced_own")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a write result: {exc}") from exc


def item_read_to_json(item: ItemRead) -> dict:
    """Serialise one item's parts."""
    return {"exists": item.exists, "parts": [part_to_json(p) for p in item.parts]}


def item_read_from_json(raw: Any) -> ItemRead:
    """Read one item's parts.

    Raises:
        ValueError: The answer isn't an item.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("parts"), list):
        raise ValueError("not an item")
    return ItemRead(bool(raw.get("exists")), [part_from_json(p) for p in raw["parts"]])


def shown_ask_to_json(ask: ShownAsk) -> dict:
    """Serialise one item of a read-back."""
    return {
        "rating_key": ask.rating_key,
        "ours": [marker_to_json(m) for m in ask.ours],
        "kept_types": _types_to_json(ask.kept_types),
        "item_files": None if ask.item_files is None else list(ask.item_files),
    }


def shown_ask_from_json(raw: Any) -> ShownAsk:
    """Read one item of a read-back.

    Raises:
        ValueError: The value isn't a read-back item.
    """
    if not isinstance(raw, dict):
        raise ValueError("read-back item must be an object")
    try:
        files = raw.get("item_files")
        return ShownAsk(
            _as_int(raw["rating_key"]),
            _markers_from_json(raw["ours"]),
            _types_from_json(raw.get("kept_types")),
            None if files is None else tuple(str(f) for f in files),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a read-back item: {exc}") from exc


def shown_batch_to_json(batch: ShownBatch) -> dict:
    """Serialise a read-back's answers."""
    return {
        "answers": [
            {"shown": None if a.shown is None else a.shown.value, "version_files": list(a.version_files)}
            for a in batch.answers
        ],
        "unreadable": batch.unreadable,
    }


def shown_batch_from_json(raw: Any) -> ShownBatch:
    """Read a read-back's answers.

    Raises:
        ValueError: The answer isn't a read-back batch.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), list):
        raise ValueError("not a read-back batch")
    answers = []
    for item in raw["answers"]:
        if not isinstance(item, dict):
            raise ValueError("read-back answer must be an object")
        shown = item.get("shown")
        answers.append(
            ShownAnswer(
                None if shown is None else Shown(str(shown)),
                tuple(str(f) for f in item.get("version_files") or ()),
            )
        )
    return ShownBatch(answers, bool(raw.get("unreadable")))


def report_to_json(report: CapabilityReport) -> dict:
    """Serialise a capability report."""
    return {"state": report.state.value, "message": report.message, "details": report.details}


def report_from_json(raw: Any) -> CapabilityReport:
    """Read a capability report.

    Raises:
        ValueError: The answer isn't a capability report.
    """
    if not isinstance(raw, dict):
        raise ValueError("not a capability report")
    try:
        details = raw.get("details")
        return CapabilityReport(
            Capability(str(raw["state"])), str(raw.get("message") or ""), details if isinstance(details, dict) else {}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a capability report: {exc}") from exc


def error_to_json(exc: PublishError) -> dict:
    """Serialise a refusal so the app can raise the very same exception."""
    return {
        "kind": "item_not_found" if isinstance(exc, ItemNotFoundError) else "publish",
        "message": str(exc),
        "state": exc.state.value if exc.state else None,
    }


def error_from_json(raw: Any) -> PublishError:
    """Rebuild the refusal the agent raised, so a remote failure reads exactly like a local one.

    Args:
        raw: The ``error`` object of a refused answer.

    Returns:
        The same exception type, message and capability state the agent's ``LocalPlexDb`` raised.
    """
    if not isinstance(raw, dict):
        return PublishError("The Plex marker agent refused this write without saying why")
    message = str(raw.get("message") or "The Plex marker agent refused this write")
    state = None
    with_state = raw.get("state")
    if with_state:
        try:
            state = Capability(str(with_state))
        except ValueError:
            state = None
    kind = ItemNotFoundError if raw.get("kind") == "item_not_found" else PublishError
    return kind(message, state=state)


# --------------------------------------------------------------------------- transport


class AgentClient:
    """One agent's HTTP endpoint: the key, the protocol check and the error mapping, nothing about markers."""

    def __init__(self, url: str, token: str, *, session: Any = None) -> None:
        """Create the client.

        Args:
            url: The agent's base address, e.g. ``http://plex-host.lan:9494``.
            token: The key both sides share. Never logged, never in an error message.
            session: A ``requests.Session`` to use (tests pass their own).
        """
        self.url = str(url or "").strip().rstrip("/")
        self._token = str(token or "")
        self._session = session or requests.Session()
        self.version = ""
        self.state = AGENT_UNREACHABLE
        # Plex's own machine identifier, as the agent reads it beside the database it would write.
        self.machine_identifier = ""

    def describe(self) -> str:
        """The address, for messages the user reads."""
        return self.url or "(no address)"

    def post(self, path: str, body: dict, *, timeout: float) -> dict:
        """Send one request and return its ``result``.

        Args:
            path: The endpoint under the agent's base address, e.g. ``/v1/item/read``.
            body: The request body.
            timeout: Seconds allowed for the answer, on top of the connect timeout.

        Returns:
            The answer's ``result`` object.

        Raises:
            AgentError: The agent couldn't be reached, refused the key, or speaks another protocol.
            PublishError: The agent ran the request and it was refused (the original exception, rebuilt).
        """
        if not self.url:
            raise AgentError("No address is set for the Plex marker agent.", state=Capability.AGENT_UNAVAILABLE)
        try:
            response = self._session.post(
                f"{self.url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {self._token}", PROTOCOL_HEADER: str(AGENT_PROTOCOL)},
                timeout=(CONNECT_TIMEOUT_S, max(1.0, timeout)),
            )
        except requests.RequestException as exc:
            self.state = AGENT_UNREACHABLE
            # The exception text can hold the URL but never the key (it is a header).
            raise AgentError(
                f"Can't reach the Plex marker agent at {self.describe()} ({type(exc).__name__}). "
                "Markers wait here until it answers again; nothing is lost.",
                state=Capability.AGENT_UNAVAILABLE,
            ) from exc
        return self._read(response)

    def _read(self, response: Any) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
        self.version = str(agent.get("version") or "")
        protocols = agent.get("protocols") if isinstance(agent.get("protocols"), list) else []
        if response.status_code == 401:
            self.state = AGENT_REJECTED
            raise AgentError(
                f"The Plex marker agent at {self.describe()} refused this app's key. Set the same shared key on "
                "both sides.",
                state=Capability.AGENT_UNAVAILABLE,
            )
        speaks_ours = AGENT_PROTOCOL in protocols if protocols else True
        if not speaks_ours or (self.version and agent_too_old(self.version)):
            self.state = AGENT_INCOMPATIBLE
            named = self.version or "unknown"
            # Which side has to move. An agent this app knows to be too old is updated; anything else that doesn't
            # speak our protocol has moved past us, and telling the user to update it again would send them in a
            # circle — including an agent that reports no version at all.
            if agent_too_old(self.version):
                advice = f"this app needs {MIN_AGENT_VERSION} or newer. Update the agent container."
            else:
                spoken = ", ".join(str(p) for p in protocols) or "another protocol"
                advice = (
                    f"it speaks protocol {spoken} and this app speaks {AGENT_PROTOCOL}. Update this app to match "
                    "the agent."
                )
            raise AgentError(
                f"The Plex marker agent at {self.describe()} is version {named}; {advice}",
                state=Capability.AGENT_UNAVAILABLE,
            )
        if response.status_code == 409 and isinstance(payload.get("error"), dict):
            self.state = AGENT_CONNECTED
            raise error_from_json(payload["error"])
        if response.status_code != 200 or not isinstance(payload.get("result"), dict):
            self.state = AGENT_UNREACHABLE
            raise AgentError(
                f"The Plex marker agent at {self.describe()} answered {response.status_code} instead of a result.",
                state=Capability.AGENT_UNAVAILABLE,
            )
        self.state = AGENT_CONNECTED
        return payload["result"]

    def agent_details(self) -> dict:
        """What the Edit tab shows about the agent: address, version, connection state and which Plex it serves."""
        return {
            "url": self.url,
            "version": self.version,
            "state": self.state,
            "machine_identifier": self.machine_identifier,
        }


class RemotePlexDb(PlexDatabase):
    """Plex's database as the Plex marker agent sees it, on Plex's own machine.

    Every method is the one ``LocalPlexDb`` runs there; this class only carries the arguments and the answer, and
    turns a transport failure into the ``AGENT_UNAVAILABLE`` capability the Edit tab explains.
    """

    # A write can fail with its answer lost after the agent's transaction committed (a blip on the way back), so
    # what is ours after a failure is unknown — unlike the in-process writer, where one transaction settles it.
    atomic_writes = False

    def __init__(self, url: str, token: str, *, session: Any = None) -> None:
        """Create the transport.

        Args:
            url: The agent's base address.
            token: The key both sides share.
            session: A ``requests.Session`` (tests pass their own).
        """
        self._client = AgentClient(url, token, session=session)

    def agent_details(self) -> dict:
        """The agent block for ``capability().details``."""
        return self._client.agent_details()

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _with_agent(self, report: CapabilityReport) -> CapabilityReport:
        return CapabilityReport(report.state, report.message, {**report.details, "agent": self.agent_details()})

    def file_checks(self, *, deadline: float) -> CapabilityReport:
        """Ask the agent to check the database file on its own machine.

        Args:
            deadline: When to stop waiting.

        Returns:
            The agent's own report (its path, its filesystem, its lock proof), with the agent's state in
            ``details["agent"]``; AGENT_UNAVAILABLE when the agent itself couldn't be used.
        """
        remaining = self._remaining(deadline)
        try:
            result = self._client.post("/v1/checks/file", {"deadline_s": remaining}, timeout=remaining + ANSWER_GRACE_S)
            self._client.machine_identifier = str(result.get("machine_identifier") or "")
            return self._with_agent(report_from_json(result.get("report")))
        except PublishError as exc:
            return self._with_agent(CapabilityReport(exc.state or Capability.AGENT_UNAVAILABLE, str(exc)))
        except ValueError as exc:
            logger.debug("Plex marker agent {}: unreadable answer: {}", self._client.describe(), exc)
            return self._with_agent(
                CapabilityReport(
                    Capability.AGENT_UNAVAILABLE,
                    f"The Plex marker agent at {self._client.describe()} sent an answer this app can't read.",
                )
            )

    def db_checks(self, *, deadline: float) -> CapabilityReport:
        """Ask the agent to check the schema, the marker data and Plex's marker tag row.

        Args:
            deadline: When to stop waiting.

        Returns:
            The agent's report, or AGENT_UNAVAILABLE when the agent itself couldn't be used.
        """
        remaining = self._remaining(deadline)
        try:
            result = self._client.post("/v1/checks/db", {"deadline_s": remaining}, timeout=remaining + ANSWER_GRACE_S)
            return self._with_agent(report_from_json(result.get("report")))
        except PublishError as exc:
            return self._with_agent(CapabilityReport(exc.state or Capability.AGENT_UNAVAILABLE, str(exc)))
        except ValueError as exc:
            logger.debug("Plex marker agent {}: unreadable answer: {}", self._client.describe(), exc)
            return self._with_agent(
                CapabilityReport(
                    Capability.AGENT_UNAVAILABLE,
                    f"The Plex marker agent at {self._client.describe()} sent an answer this app can't read.",
                )
            )

    def read_item(self, rating_key: int, *, deadline: float) -> ItemRead:
        """Read one item's live parts through the agent.

        Args:
            rating_key: Plex's metadata item id.
            deadline: When to stop waiting.

        Returns:
            Whether the database knows the item, and its live parts.

        Raises:
            PublishError: The agent refused, or couldn't be used.
        """
        remaining = self._remaining(deadline)
        result = self._client.post(
            "/v1/item/read", {"rating_key": rating_key, "deadline_s": remaining}, timeout=remaining + ANSWER_GRACE_S
        )
        try:
            return item_read_from_json(result.get("item"))
        except ValueError as exc:
            raise self._unreadable(exc) from exc

    def write_item(self, request: WriteRequest, *, deadline: float) -> WriteResult:
        """Run one item's write on the agent, in one transaction there.

        Args:
            request: The item, the parts its markers were decided for, and those markers.
            deadline: When to stop waiting.

        Returns:
            Whether the database changed, and what the item shows as ours.

        Raises:
            PublishError: Nothing was written (the agent's own refusal, or the agent couldn't be used).
        """
        remaining = self._remaining(deadline)
        body = {"deadline_s": remaining, "write": write_request_to_json(request)}
        result = self._client.post("/v1/item/write", body, timeout=remaining + ANSWER_GRACE_S)
        try:
            return write_result_from_json(result.get("write"))
        except ValueError as exc:
            raise self._unreadable(exc) from exc

    def shown_many(
        self, items: list[ShownAsk], *, timeout_s: float, cancel_check: Callable[[], bool] | None = None
    ) -> ShownBatch:
        """Read items back through the agent, in chunks so a cancel is still answered quickly.

        Args:
            items: What to ask about each item.
            timeout_s: The longest one item's read waits for the database locks, on the agent.
            cancel_check: True once the job is cancelled; checked between chunks.

        Returns:
            One answer per item read, and whether the database stopped being readable at all. A chunk the agent
            couldn't be asked for at all ends the call with ``unreadable``.
        """
        answers: list[ShownAnswer] = []
        for start in range(0, len(items), READ_BACK_CHUNK):
            if cancel_check and cancel_check():
                return ShownBatch(answers, False)
            chunk = items[start : start + READ_BACK_CHUNK]
            body = {
                "timeout_s": timeout_s,
                "budget_s": READ_BACK_BUDGET_S,
                "items": [shown_ask_to_json(a) for a in chunk],
            }
            try:
                result = self._client.post("/v1/items/shown", body, timeout=READ_BACK_BUDGET_S + ANSWER_GRACE_S)
                batch = shown_batch_from_json(result.get("batch"))
            except (PublishError, ValueError) as exc:
                logger.debug("Plex marker agent {}: read-back stopped: {}", self._client.describe(), exc)
                return ShownBatch(answers, True)
            answers.extend(batch.answers)
            # The agent stopped early: either the database went unreadable (its own flag) or its budget ran out,
            # which leaves the items unread exactly as a cancel does.
            if batch.unreadable:
                return ShownBatch(answers, True)
            if len(batch.answers) < len(chunk):
                return ShownBatch(answers, False)
        return ShownBatch(answers, False)

    def item_exists(self, rating_key: int, *, deadline: float) -> bool:
        """Whether the agent's database has this rating key.

        Args:
            rating_key: Plex's metadata item id.
            deadline: When to stop waiting.

        Returns:
            True when the item is there.

        Raises:
            PublishError: The agent refused, or couldn't be used.
        """
        remaining = self._remaining(deadline)
        result = self._client.post(
            "/v1/item/exists", {"rating_key": rating_key, "deadline_s": remaining}, timeout=remaining + ANSWER_GRACE_S
        )
        if not isinstance(result.get("exists"), bool):
            raise self._unreadable(ValueError("no exists flag"))
        return result["exists"]

    def _unreadable(self, exc: Exception) -> AgentError:
        logger.debug("Plex marker agent {}: unreadable answer: {}", self._client.describe(), exc)
        return AgentError(
            f"The Plex marker agent at {self._client.describe()} sent an answer this app can't read.",
            state=Capability.AGENT_UNAVAILABLE,
        )


def plex_database(
    settings: Any,
    *,
    path_provider: Callable[[], str | None],
    label: str,
    mountinfo_path: str = "/proc/self/mountinfo",
    session: Any = None,
) -> PlexDatabase:
    """The database half for one Plex server: the agent when one is set up, the local file otherwise.

    Args:
        settings: That server's ``ServerMarkersSettings``.
        path_provider: Returns the database path this app can see (used only without an agent).
        label: The server's name, for log lines.
        mountinfo_path: For tests.
        session: A ``requests.Session`` for the agent (tests pass their own).

    Returns:
        A ``RemotePlexDb`` when the server has a Plex marker agent switched on with an address, else a
        ``LocalPlexDb``.
    """
    if getattr(settings, "agent_enabled", False) and getattr(settings, "agent_url", ""):
        return RemotePlexDb(settings.agent_url, getattr(settings, "agent_token", ""), session=session)
    return LocalPlexDb(path_provider, label=label, mountinfo_path=mountinfo_path)
