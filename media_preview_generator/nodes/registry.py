"""The app's view of its nodes: who they are, their live state, and how much work each can take (spec §5, §10.2).

The configured nodes come from settings.json (``nodes``, re-read whenever ``nodes_revision`` moves). Each one gets a
live record the health loop keeps current: every 30 s a check asks the node for ``/v1/info`` (three attempts 4 s
apart before it counts as offline, the same ~12 s the remote executor allows a task poll), backing off to 5 minutes
while it stays offline. Job code reads :meth:`NodeRegistry.capacity` and reports what it saw (a node that stopped
answering, a file it couldn't see, a GPU that failed); every state change bumps :attr:`NodeRegistry.revision`.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from loguru import logger

from . import protocol
from .access_results import AccessResultStore
from .client import (
    NodeAddress,
    NodeAuthError,
    NodeClient,
    NodeError,
    NodeProtocolError,
    NodeRateLimitedError,
    NodeTimeoutError,
    unreachable_advice,
)
from .protocol import PathMapping
from .settings import find_node, node_mappings, server_agent

STATE_CONNECTING = "connecting"
STATE_READY = "ready"
STATE_OFFLINE = "offline"
STATE_UPDATE_NEEDED = "update_needed"
STATE_KEY_REFUSED = "key_refused"
STATE_ACCESS_PROBLEM = "access_problem"
STATE_GPU_LOST = "gpu_lost"
STATE_NO_FFMPEG = "no_ffmpeg"
STATE_TEMP_PROBLEM = "temp_problem"
STATE_DISABLED = "disabled"
# The machine is already added as another node (the same node id answers at both addresses).
STATE_DUPLICATE = "duplicate"

# Why an offline node gets no work (``NodeView.cause``): only "didn't answer" and "not a node" are about its address.
OFFLINE_UNREACHABLE = "unreachable"
OFFLINE_NOT_A_NODE = "not_a_node"
OFFLINE_RESTARTING = "restarting"
OFFLINE_STUCK = "stuck"
OFFLINE_RATE_LIMITED = "rate_limited"
# Why a worker node needs an update (``NodeView.cause``) when this app is the older side: this app is updated.
UPDATE_APP_BEHIND = "app_behind"

# States that raise a bell notification (``web.notifications``).
ALERT_STATES = (
    STATE_OFFLINE,
    STATE_UPDATE_NEEDED,
    STATE_KEY_REFUSED,
    STATE_ACCESS_PROBLEM,
    STATE_GPU_LOST,
    STATE_NO_FFMPEG,
    STATE_DUPLICATE,
    STATE_TEMP_PROBLEM,
)

HEALTH_INTERVAL_S = 30.0
# Delay before the next check after the 1st, 2nd, 3rd and later offline checks in a row.
OFFLINE_BACKOFF_S = (30.0, 60.0, 120.0, 300.0)
# How soon an offline node is checked again while a job waits for it alone (``work_waiting``) instead of its back-off.
WAITING_RECHECK_S = 15.0
# A node that said it is stopping (a restart, an image update) is back in seconds: it is checked this often, with no
# offline back-off, until it answers or for at most RESTART_WINDOW_S after it said so (one stopped for good).
RESTART_RECHECK_S = 3.0
RESTART_WINDOW_S = 120.0
# A node a check found refusing the key isn't checked again for job code's reports this soon after: each running
# task's next poll is refused and reported, and a check per report spent the node's wrong-key allowance (20 at once,
# then one every 3 s) on this app's address, so the node held back the right key pasted next too.
KEY_REFUSED_RECHECK_S = 10.0
CHECK_ATTEMPTS = 3
ATTEMPT_GAP_S = 4.0
NOT_VISIBLE_WINDOW_S = 600.0
NOT_VISIBLE_LIMIT = 3
# A node paused for files it couldn't see runs the quick access check this often on its own: files that changed under
# it (a share still listing a renamed episode's old name) need nothing fixed, and nobody may be there to press Test
# access.
ACCESS_PROBLEM_RECHECK_S = 300.0
# A Test access of rows not saved yet is adopted only if those rows are saved within this long.
PENDING_ACCESS_TTL_S = 600.0
GPU_LOST_RECHECK_S = 300.0
MAX_PARALLEL_CHECKS = 8
# Bounds on what a node's /v1/info and GPU rescan answers may hold once kept here (every reader copies them): far
# above what a node sends (a few GPUs, short facts), low enough that a broken or hostile answer can't break readers.
INFO_TEXT_MAX = 4096
INFO_LIST_MAX = 256
INFO_DEPTH_MAX = 8
LEGACY_PING_TIMEOUT_S = 10.0
LOOP_TICK_S = 1.0
# How long a Plex database request waits for a node's first check (just started, or its address changed) before it
# is told to wait for it instead: a node that answers takes well under a second.
FIRST_CHECK_WAIT_S = 5.0
# The node's own words for its temp folder's problem go into the reason; a node can't make it any longer than this.
TEMP_PROBLEM_MAX = 300

# The short reason a worker member shows when its node gives it no workers.
_CAPACITY_REASONS = {
    STATE_CONNECTING: "connecting to node",
    STATE_OFFLINE: "node offline",
    STATE_UPDATE_NEEDED: "node needs an update",
    STATE_KEY_REFUSED: "key refused",
    STATE_ACCESS_PROBLEM: "fix media access first",
    STATE_NO_FFMPEG: "FFmpeg missing on node",
    STATE_TEMP_PROBLEM: "node's temp folder can't be used",
    STATE_DISABLED: "node turned off",
    STATE_DUPLICATE: "node added twice",
}
# An offline node that answered it is restarting, or asked this app to wait, has nothing to fix on it: "node offline"
# beside it contradicted its Setup Health and bell, which say so. A worker node ahead of this app has nothing to fix on
# it either: this app is the one to update.
_CAUSE_CAPACITY_REASONS = {
    OFFLINE_RESTARTING: "node restarting",
    OFFLINE_RATE_LIMITED: "node asks to wait",
    UPDATE_APP_BEHIND: "app needs an update",
}
# The states whose ``NodeView.cause`` is kept (a turned-off node's last check doesn't say why it gets nothing).
_CAUSE_STATES = (STATE_OFFLINE, STATE_UPDATE_NEEDED)


@dataclass(frozen=True)
class NodeView:
    """A snapshot of one configured node."""

    id: str
    name: str
    url: str
    enabled: bool
    state: str
    reason: str
    version: str
    roles: tuple[str, ...]
    gpus: tuple[dict, ...]
    cpu_cores: int
    legacy: bool
    last_seen: float | None
    # A health check is running or queued for it.
    checking: bool = False
    # Why an offline node is offline (``OFFLINE_*``), or ``UPDATE_APP_BEHIND`` for a node that needs an update because
    # this app is older; "" otherwise.
    cause: str = ""
    # A Plex database node reported a new id this app couldn't tie to its Plex: its key must be typed again.
    plex_db_unconfirmed: bool = False


@dataclass(frozen=True)
class NodeCapacity:
    """Whether a node's CPU / GPU members may run work now, and the reason shown when they may not."""

    cpu: bool
    gpu: bool
    reason: str


@dataclass(frozen=True)
class NodeDetails:
    """What the Nodes page shows beyond :class:`NodeView`: the last ``/v1/info`` facts and the last Test access.

    ``gpus`` keeps each GPU's ``error`` (the view drops it); ``busy`` is how many tasks the node was running.
    """

    view: NodeView
    platform: dict
    cpu: dict
    busy: int
    info: dict | None
    access_result: dict | None
    access_checked_at: float | None
    gpus: tuple[dict, ...] = ()


@dataclass(frozen=True)
class _Outcome:
    """What one health check found."""

    state: str
    reason: str = ""
    info: dict | None = None
    legacy: bool = False
    version: str = ""
    roles: tuple[str, ...] = ()
    uid: str = ""
    cause: str = ""
    # The node said it is stopping (SIGTERM).
    restarting: bool = False


class PlexDbHold(NamedTuple):
    """Why this app sends no Plex database work to a node now."""

    message: str
    # The node's id is still to be checked: the work waits for that check (as for a node that doesn't answer)
    # instead of failing.
    waits: bool = False


class _PendingAccess(NamedTuple):
    """A Test access of rows not saved yet, kept until exactly those rows are saved at the same address."""

    rows: list
    result: dict
    checked_at: float
    url: str
    held_at: float


@dataclass
class _Live:
    record: dict
    client: NodeClient
    probe_state: str = STATE_CONNECTING
    probe_reason: str = ""
    probe_cause: str = ""
    info: dict | None = None
    legacy: bool = False
    version: str = ""
    roles: tuple[str, ...] = ()
    gpus: tuple[dict, ...] = ()
    last_seen: float | None = None
    # The address it answered at: a name that stops resolving there may be a stopped node, a new address a typo.
    seen_url: str = ""
    offline_streak: int = 0
    next_check: float = 0.0
    # When (``clock``) the node last said it is stopping, until it answers again; None meanwhile.
    restarting_since: float | None = None
    # When (``clock``) a check last found the node refusing the key.
    key_refused_at: float | None = None
    checking: bool = False
    # Check now asked for the quick access check too: the next check to finish runs it.
    access_wanted: bool = False
    not_visible: deque = field(default_factory=lambda: deque(maxlen=64))
    access_problem: str = ""
    next_access_check: float = 0.0
    gpu_lost: str = ""
    lost_device: str = ""
    next_gpu_check: float = 0.0
    access_result: dict | None = None
    access_checked_at: float | None = None
    # A Test access of rows not saved yet (the Add node wizard and the Media tab test before they save).
    pending_access: _PendingAccess | None = None
    # A new id a Plex database node reported that this app couldn't tie to the Plex it serves; its Plex database
    # writes are refused until the user types the node's key again (which clears the saved id).
    unconfirmed_uid: str = ""
    # A new id ``_new_uid`` took in place of a saved one, until the node's first Plex database request compares it
    # with the Plex of the servers using the node (``_tie_new_uid``): with none switched on when it was taken,
    # nothing did, and nothing would once a server is switched back on.
    untied_uid: str = ""
    # The id the last check that reached the node heard, when it is the saved id or a new one ``_new_uid`` accepted
    # ("" when it gives none, an old Plex helper); None for any other id (one held back, a twin's), until a check
    # since this app started reaches it, and again once a check doesn't, it is reported unavailable, it gets another
    # address or its saved id changes. Until then another machine could be answering there.
    heard_uid: str | None = None
    check_lock: threading.Lock = field(default_factory=threading.Lock)
    # Health checks begun so far, the number of the last one that came back ready, and how many had begun when job
    # code last reported the node unavailable: only a check begun after that report vouches for the node again.
    checks_begun: int = 0
    ready_check: int = 0
    reported_at_check: int = 0


def _app_version() -> str:
    from .. import __version__

    return str(__version__)


def _default_revision() -> object:
    from ..web.settings_manager import get_settings_manager

    sm = get_settings_manager()
    # The manager's identity is part of the key: a new manager (another config dir) can start at the same revision.
    return (id(sm), sm.nodes_revision)


def _default_nodes() -> list[dict]:
    from ..web.settings_manager import get_settings_manager

    return get_settings_manager().nodes


def _default_client(record: dict) -> NodeClient:
    return NodeClient(NodeAddress(str(record.get("url") or ""), str(record.get("key") or "")))


def _default_legacy_ping(url: str, key: str) -> tuple[str, str]:
    """Ask an address the Plex helper's own ``/v1/ping``; answers ``(agent state, agent version)``."""
    from ..markers.publishers.base import PublishError
    from ..markers.publishers.plex_remote import AgentClient

    agent = AgentClient(url, key)
    try:
        agent.ping(timeout=LEGACY_PING_TIMEOUT_S)
    except PublishError:
        pass  # AgentClient records what went wrong in ``state``.
    return agent.state, agent.version


def _default_served_plex_ids(node_id: str) -> list[str]:
    """The machine identifier of every Plex server that writes through ``node_id`` ("" where unknown).

    A server switched off, or with its Plex database node switched off, writes nothing through the node though it
    remembers it, so it doesn't hold back the node's new id.
    """
    from ..web.settings_manager import get_settings_manager

    out = []
    for entry in get_settings_manager().get("media_servers") or []:
        agent = server_agent(entry)
        if agent is None or not agent.get("enabled") or agent.get("node_id") != node_id:
            continue
        if entry.get("enabled", True):
            out.append(str(entry.get("server_identity") or ""))
    return out


def _default_record_uid(node_id: str, uid: str, url: str) -> None:
    """Save the id a node reported, when the record still has the address it was learnt at.

    The saved key at the saved address is what makes a worker node this node: one that answers with a new id
    (recreated without its ``/config``, reinstalled) is the same node, so the new id replaces the old one. A Plex
    database node's new id only gets here once it is tied to its Plex (``NodeRegistry._new_uid_confirmed``).
    """
    from ..web.settings_manager import get_settings_manager

    sm = get_settings_manager()
    try:
        with sm.locked():
            nodes = sm.nodes
            node = find_node(nodes, node_id)
            if node is None or node.get("node_uid") == uid or node.get("url") != url:
                return
            if any(other.get("node_uid") == uid and other.get("id") != node_id for other in nodes):
                return  # Another node is this machine already (two checks can race): this one reads as added twice.
            if node.get("node_uid"):
                logger.info("{} reports a new id; it was probably reinstalled", node.get("name") or node_id)
            node["node_uid"] = uid
            sm.update_nodes(nodes)
    except (OSError, ValueError) as exc:
        logger.warning("Couldn't record which node answers at a saved address: {}", exc)


def _default_library_names(app_paths: list[str]) -> list[str]:
    """The names of the libraries holding ``app_paths`` (by the deepest library folder above each), in path order."""
    from ..web.settings_manager import get_settings_manager
    from .mappings import library_roots

    roots = library_roots(get_settings_manager().get("media_servers") or [])
    names: list[str] = []
    for path in app_paths:
        covering = [r for r in roots if path == r.app_root or path.startswith(r.app_root.rstrip("/") + "/")]
        if covering:
            name = max(covering, key=lambda r: len(r.app_root)).library
            if name not in names:
                names.append(name)
    return names


def _temp_folder_advice(problem: str) -> str:
    """What to do about a node's temp folder, by the cause in the node's words (its own, or the OS's error text).

    Args:
        problem: The node's sentence about its temp folder.

    Returns:
        One sentence of advice.
    """
    said = problem.lower()
    if any(cause in said for cause in (" is full", "filled up", "no space left", "quota")):
        return "Free up space there, or set NODE_TEMP_DIR on the node to a folder with more room."
    if "read-only" in said:
        return "Mount it writable (not read-only), or set NODE_TEMP_DIR on the node to a folder it can write to."
    if any(cause in said for cause in ("permission denied", "access is denied", "not permitted")):
        return (
            "Let the node's user write to it (in Docker, check PUID and PGID), or set NODE_TEMP_DIR on the node to a "
            "folder it can write to."
        )
    if any(cause in said for cause in ("no such file", "cannot find")):
        return "Create or mount that folder, or set NODE_TEMP_DIR on the node to a folder it can write to."
    return "Set NODE_TEMP_DIR on the node to a folder it can write to."


def _restarting_reason(name: object) -> str:
    """A restarting node's reason (``OFFLINE_RESTARTING``)."""
    return f"{name or 'The node'} is restarting; it gets no files until it is back."


def no_id_reason(name: object) -> str:
    """Why a node with a saved id that answers as an older release, which gives none, gets no Plex database work."""
    return (
        f"{name or 'The node'} answers as an older release that gives no id, so this app can't confirm it is the same "
        "Plex. Update the node, or type its key again on its Settings tab if it is the same Plex."
    )


def _default_quick_access(client: NodeClient, mappings: list[PathMapping], unseen: list[str]) -> bool:
    from ..web.settings_manager import get_settings_manager
    from .mappings import files_visible, quick_access_check

    servers = get_settings_manager().get("media_servers") or []
    return quick_access_check(client, mappings, servers) and files_visible(client, mappings, unseen)


def _renamed(text: str, old: str, new: str) -> str:
    """A reason that starts with the node's old name, starting with its new one instead."""
    return new + text[len(old) :] if text.startswith(old) else text


VULKAN_TEXT_MAX = 600


def _clean_vulkan(raw: object) -> dict | None:
    """A node's Vulkan check for one GPU, keeping only the known keys (text cut to one short line)."""
    if not isinstance(raw, dict):
        return None
    cleaned: dict[str, Any] = {"ok": raw.get("ok") is True}
    for key in ("cause", "title", "why", "fix"):
        cleaned[key] = protocol.relayed_text(raw.get(key), VULKAN_TEXT_MAX)
    return cleaned


def _clean_gpus(raw: object) -> tuple[dict, ...]:
    if not isinstance(raw, list):
        return ()
    gpus = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        gpu: dict[str, Any] = {
            "type": str(g.get("type") or ""),
            "device": str(g.get("device") or ""),
            "name": str(g.get("name") or ""),
            "status": str(g.get("status") or ""),
            "error": str(g.get("error") or ""),
        }
        vulkan = _clean_vulkan(g.get("vulkan"))
        if vulkan is not None:
            gpu["vulkan"] = vulkan
        gpus.append(gpu)
    return tuple(gpus)


def _count(raw: Any) -> int:
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded(value: object, depth: int = 0) -> Any:
    """A JSON value a node sent, cut to the ``INFO_*`` bounds, each text one line; a number that isn't finite becomes
    null.

    JSON's ``1e400`` parses to infinity, which ``int()`` refuses, a 400-digit whole number is too big for a float, and a
    deeply nested answer breaks ``deepcopy``.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return protocol.relayed_text(value, INFO_TEXT_MAX)
    if isinstance(value, int | float):
        try:
            return value if math.isfinite(value) else None
        except OverflowError:  # isfinite turns a whole number into a float first
            return None
    if depth >= INFO_DEPTH_MAX:
        return None
    if isinstance(value, dict):
        return {str(k)[:INFO_TEXT_MAX]: _bounded(v, depth + 1) for k, v in list(value.items())[:INFO_LIST_MAX]}
    if isinstance(value, list | tuple):
        return [_bounded(v, depth + 1) for v in value[:INFO_LIST_MAX]]
    return None


def _gpus_in(answer: object) -> tuple[dict, ...]:
    """The GPU list in an ``/v1/info`` or ``/v1/gpus/rescan`` answer (``worker.gpus``, or a bare ``gpus``)."""
    if not isinstance(answer, dict):
        return ()
    if isinstance(answer.get("gpus"), list):
        return _clean_gpus(answer["gpus"])
    worker = answer.get("worker")
    return _clean_gpus(worker.get("gpus")) if isinstance(worker, dict) else ()


def _gpu_detection_pending(answer: object) -> str:
    """Why a node couldn't detect its GPUs yet: its ``gpu_error`` while it has no GPU list at all, else "".

    A node keeps its last list when a detection fails, so an error beside an empty list means no detection has
    worked since it started; its GPUs may still be there.
    """
    if not isinstance(answer, dict) or _gpus_in(answer):
        return ""
    worker = answer.get("worker")
    error = worker.get("gpu_error") if isinstance(worker, dict) else None
    return str(error) if error else ""


def _gpu_ok(gpus: tuple[dict, ...], device: str) -> tuple[bool, str]:
    match = next((g for g in gpus if g["device"] == device), None)
    if match is None:
        return False, "the node no longer finds it"
    if match["status"] != "ok":
        return False, match["error"] or f"its status is {match['status'] or 'unknown'}"
    return True, ""


def _beside_their_plex(served: list[str], info: dict | None) -> bool:
    """Whether a node's ``/v1/info`` names the Plex machine identifier of every server in ``served``; an empty one on
    either side (a Windows node with a custom Plex folder) proves nothing."""
    plex_db = (info or {}).get("plex_db")
    theirs = str(plex_db.get("machine_identifier") or "") if isinstance(plex_db, dict) else ""
    return bool(theirs) and all(ours == theirs for ours in served)


class NodeRegistry:
    """Configured nodes and their live state.

    Args:
        revision_provider: Returns a value that changes whenever the stored node list does.
        nodes_provider: Returns the stored node records.
        client_factory: Builds the client for one record.
        legacy_ping: Asks an address the old Plex helper's ping; answers ``(agent state, version)``.
        record_uid: Persists the ``node_uid`` a node reported, given the address it answered at.
        quick_access: Checks one file per library on a node, and the files it couldn't see; True when every one is
            visible.
        served_plex_ids: The machine identifiers of the Plex servers that use a node as their Plex database node.
        app_version: This app's version (a worker node must match it exactly).
        app_build: This app's git commit, "" when unknown (a worker node reporting another one needs an update).
        attempt_gap_s: Seconds between the attempts of one check.
        clock: Monotonic seconds.
        wall_clock: Epoch seconds (for ``last_seen``).
    """

    def __init__(
        self,
        *,
        revision_provider: Callable[[], object] = _default_revision,
        nodes_provider: Callable[[], list[dict]] = _default_nodes,
        client_factory: Callable[[dict], NodeClient] = _default_client,
        legacy_ping: Callable[[str, str], tuple[str, str]] = _default_legacy_ping,
        record_uid: Callable[[str, str, str], None] = _default_record_uid,
        quick_access: Callable[[NodeClient, list[PathMapping], list[str]], bool] = _default_quick_access,
        library_names: Callable[[list[str]], list[str]] = _default_library_names,
        served_plex_ids: Callable[[str], list[str]] = _default_served_plex_ids,
        app_version: Callable[[], str] = _app_version,
        app_build: Callable[[], str] = protocol.build_sha,
        attempt_gap_s: float = ATTEMPT_GAP_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        access_store: AccessResultStore | None = None,
    ) -> None:
        self._revision_provider = revision_provider
        # Keeps each node's last Test access across restarts; None keeps it in memory only.
        self._access_store = access_store
        self._nodes_provider = nodes_provider
        self._client_factory = client_factory
        self._legacy_ping = legacy_ping
        self._record_uid = record_uid
        self._quick_access = quick_access
        self._library_names = library_names
        self._served_plex_ids = served_plex_ids
        self._app_version = app_version
        self._app_build = app_build
        self._attempt_gap_s = attempt_gap_s
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        # Notified whenever a check finishes (with ``_lock`` held), for :meth:`plex_db_hold`'s wait.
        self._check_done = threading.Condition(self._lock)
        self._nodes: dict[str, _Live] = {}
        self._settings_key: object = object()
        self._revision = 0
        self._signature: tuple = ()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------ reading

    def settings_replaced(self) -> None:
        """Re-read the nodes on the next read, whatever ``nodes_revision`` says.

        A restored settings.json can hold other nodes at the same revision as the file it replaced.
        """
        with self._lock:
            self._settings_key = object()

    @property
    def revision(self) -> int:
        """Bumps on any state, capacity or settings change."""
        self._sync()
        return self._revision

    def views(self) -> list[NodeView]:
        """Every configured node, in settings order."""
        self._sync()
        with self._lock:
            return [self._view(live) for live in self._nodes.values()]

    def view(self, node_id: str) -> NodeView | None:
        """One node, or None when it isn't configured."""
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            return self._view(live) if live else None

    def details(self, node_id: str) -> NodeDetails | None:
        """A node's view plus the last facts it reported and its last Test access, or None."""
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return None
            info = copy.deepcopy(live.info)
            worker = (info or {}).get("worker") or {}
            return NodeDetails(
                view=self._view(live),
                platform=dict((info or {}).get("platform") or {}),
                cpu=dict(worker.get("cpu") or {}),
                busy=_count(worker.get("busy")),
                info=info,
                access_result=copy.deepcopy(live.access_result),
                access_checked_at=live.access_checked_at,
                gpus=copy.deepcopy(live.gpus),
            )

    def client(self, node_id: str) -> NodeClient:
        """The node's client. Raises KeyError for an unknown node."""
        self._sync()
        with self._lock:
            return self._nodes[node_id].client

    def mappings(self, node_id: str) -> list[PathMapping]:
        """The node's mapping rows ([] for an unknown node)."""
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            return node_mappings(live.record) if live else []

    def capacity(self, node_id: str) -> NodeCapacity:
        """Whether the node's CPU / GPU members may take work now."""
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return NodeCapacity(False, False, "node removed")
            state, reason = self._state(live)
            if state == STATE_READY:
                if protocol.ROLE_WORKER not in live.roles:
                    return NodeCapacity(False, False, "node doesn't run workers")
                return NodeCapacity(True, True, "")
            if state == STATE_GPU_LOST:
                return NodeCapacity(True, False, reason)
            if state in _CAUSE_STATES and live.probe_cause in _CAUSE_CAPACITY_REASONS:
                return NodeCapacity(False, False, _CAUSE_CAPACITY_REASONS[live.probe_cause])
            return NodeCapacity(False, False, _CAPACITY_REASONS.get(state, reason))

    def plex_db_refusal(self, node_id: str) -> str:
        """Why this app sends no Plex database work to the node now ("" when it may); see :meth:`plex_db_hold`."""
        hold = self.plex_db_hold(node_id)
        return hold.message if hold is not None else ""

    def plex_db_hold(self, node_id: str) -> PlexDbHold | None:
        """Why this app sends no Plex database work to the node now, or None when it may.

        A Plex database node that reports a new id could be a different machine that was given the same key and
        address; unless its Plex is the one the server uses, the user confirms it by typing its key again. A node
        that answers as another saved node is refused too: its answer is that other machine's, not this one's. So is
        one with a saved id that answers with none (an older release): nothing says which Plex it is beside.

        Either is known only from a check that reached the node, so until one has since this app started (or since
        the node last didn't answer, or got another address) a node with a saved id gets nothing either: its work
        waits, and a check starts now (within :data:`WAITING_RECHECK_S` of one that found it down); a held new id or
        answer with none brings its next check forward the same way, so the hold lifts soon after the saved machine
        answers there again. The node's first
        check is waited for, up to :data:`FIRST_CHECK_WAIT_S`, so work asked for just after a restart isn't held back
        by a check that is about to answer. A new id taken in place of a saved one is compared with the Plex of the
        servers using the node here first (``_tie_new_uid``): none may have been switched on when it was taken.
        """
        self._sync()
        tie_decided = self._tie_new_uid(node_id)
        deadline = time.monotonic() + FIRST_CHECK_WAIT_S
        waited = False
        started = False
        with self._check_done:
            while True:
                live = self._nodes.get(node_id)
                if live is None:
                    return None
                if live.probe_state == STATE_DUPLICATE:
                    return PlexDbHold(f"{live.probe_reason} Until then this app doesn't use it for Plex's database.")
                name = str(live.record.get("name") or "The Plex database node")
                checking = PlexDbHold(
                    f"Checking {name} before sending it Plex database work. That work waits until the node answers; "
                    "nothing is lost.",
                    waits=True,
                )
                hold = None
                if live.unconfirmed_uid:
                    hold = PlexDbHold(
                        f"{name} reports a new id and this app can't confirm it is the same Plex. Type the node's key "
                        "again on its Settings tab to confirm."
                    )
                elif not tie_decided:
                    return checking
                # ``heard_uid`` holds only the saved id or one ``_new_uid`` accepted; an answer with no id ("") says
                # nothing about which Plex is there.
                elif not live.record.get("node_uid") or live.heard_uid:
                    return None
                elif live.heard_uid == "" and live.probe_state == STATE_READY:
                    hold = PlexDbHold(no_id_reason(name))
                elif live.heard_uid == "":
                    hold = PlexDbHold(f"{live.probe_reason} Until then this app doesn't use it for Plex's database.")
                # A new id or none is lifted only by a check that hears the saved machine again (another machine gone
                # from the address, the node updated), so that check is brought forward too.
                if not live.checking and not waited and live.record.get("enabled", True):
                    now = self._clock()
                    if live.next_check <= now:
                        live.checking = True
                        started = True
                        self._submit(self._run_check, node_id, False, True)
                    else:
                        # One that didn't answer keeps its back-off (a check per request ran them back to back),
                        # brought forward as for a job waiting on it.
                        live.next_check = min(live.next_check, now + WAITING_RECHECK_S)
                left = deadline - time.monotonic()
                if hold is not None:
                    # The check just started is the one that lifts it (the saved machine answering again): wait for it
                    # as a first check does, or one file is skipped after the impostor has already left.
                    if not (started and live.checking and left > 0):
                        return hold
                elif not live.checking or live.probe_state != STATE_CONNECTING or left <= 0:
                    return checking
                waited = True
                self._check_done.wait(left)

    # ------------------------------------------------------------------ reports from job code

    def report_unavailable(self, node_id: str) -> None:
        """A node stopped answering mid-task, or another machine refused its Plex database work: check it now rather
        than at its next turn (its Plex database work waits for that check).

        A check already running began before the report, so another one follows it straight away, unless it finds the
        node refusing the key. No check runs for a node a check found refusing the key within
        ``KEY_REFUSED_RECHECK_S``: the report is that refusal again.
        """
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return
            live.reported_at_check = live.checks_begun
            live.heard_uid = None
            if (
                live.probe_state == STATE_KEY_REFUSED
                and live.key_refused_at is not None
                and self._clock() - live.key_refused_at < KEY_REFUSED_RECHECK_S
            ):
                return
            live.next_check = 0.0
        if self._running():
            self._wake.set()
        else:
            self._submit(self._run_check, node_id)

    def work_waiting(self, node_ids: Iterable[str]) -> None:
        """A job waits for these nodes alone: an offline one is checked within ``WAITING_RECHECK_S``.

        Its back-off reaches five minutes, so the job went on "waiting for a node" for minutes after the node was
        back. Cheap enough to call on every look at the job's capacity: nothing but the next check's time moves.
        """
        soon = self._clock() + WAITING_RECHECK_S
        with self._lock:
            for node_id in node_ids:
                live = self._nodes.get(str(node_id))
                if live is not None and live.probe_state == STATE_OFFLINE and live.next_check > soon:
                    live.next_check = soon

    def checked_ready_since_unavailable(self, node_id: str) -> bool:
        """Whether the node has capacity and a health check begun after it was last reported unavailable found it so.

        A node that lost its GPU counts: its CPU members keep their capacity, and its GPU members get none anyway.
        """
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None or self._state(live)[0] not in (STATE_READY, STATE_GPU_LOST):
                return False
            return live.ready_check > live.reported_at_check

    def report_not_visible(self, node_id: str, app_path: str, reason: str) -> None:
        """A node couldn't see a file; three different files within 10 minutes is an access problem.

        The problem pauses all of the node's workers, not only for the libraries it missed: one wrong mapping row
        makes the others suspect too, and the files it can't see run elsewhere meanwhile. Its reason names the
        libraries those files are in.
        """
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return
            now = self._clock()
            while live.not_visible and now - live.not_visible[0][0] > NOT_VISIBLE_WINDOW_S:
                live.not_visible.popleft()
            live.not_visible.append((now, app_path))
            distinct = sorted({path for _, path in live.not_visible})
            if len(distinct) < NOT_VISIBLE_LIMIT or live.access_problem:
                self._touch()
                return
        # Reads the settings, so outside the registry's lock.
        try:
            libraries = self._library_names(distinct)
        except Exception as exc:
            logger.debug("Couldn't name the libraries of the files node {} couldn't see: {}", node_id, exc)
            libraries = []
        where = f" in {', '.join(libraries)}" if libraries else ""
        with self._lock:
            # A passing Test access meanwhile cleared the files it couldn't see.
            if self._nodes.get(node_id) is not live or live.access_problem or (now, app_path) not in live.not_visible:
                return
            name = live.record.get("name") or "The node"
            live.access_problem = (
                f"{name} couldn't see {len(distinct)} different files{where} in 10 minutes (the last one: "
                f"{protocol.not_visible_text(reason)}). Its workers are paused until it can see your media again "
                "(checked every 5 minutes): run Test access and fix the folder mappings."
            )
            live.next_access_check = now + ACCESS_PROBLEM_RECHECK_S
            logger.warning(
                "Node {} couldn't see {} different files{} in 10 minutes; pausing its workers until media access "
                "is fixed",
                name,
                len(distinct),
                where,
            )
            self._touch()

    def report_gpu_error(self, node_id: str, device: str) -> None:
        """A GPU failed on a node: rescan its GPUs, and mark it lost when that GPU is missing or not OK."""
        self._sync()
        with self._lock:
            if node_id not in self._nodes:
                return
        self._submit(self._gpu_check, node_id, device)

    def record_access_result(
        self, node_id: str, result: dict, *, tested_mappings: list[dict], tested_url: str | None = None
    ) -> None:
        """Remember a Test access result; a passing one clears an access problem.

        A result for rows other than the saved ones (rows still being edited, or mappings that changed during the
        test) says nothing about how the node runs work now. It is held aside and adopted only if exactly those
        rows are saved next, at the same address, within :data:`PENDING_ACCESS_TTL_S`. A result from an address the
        node no longer has is dropped: it was another machine's answer.

        Args:
            node_id: The node.
            result: ``{"libraries": [...]}`` as ``mappings.run_test_access`` returns it.
            tested_mappings: The ``path_mappings`` rows the test ran with.
            tested_url: The address the test asked; None for the node's saved one.
        """
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return
            url = str(live.record.get("url") or "")
            if tested_url is not None and tested_url != url:
                return
            checked_at = self._wall_clock()
            if tested_mappings != (live.record.get("path_mappings") or []):
                live.pending_access = _PendingAccess(
                    copy.deepcopy(tested_mappings), copy.deepcopy(result), checked_at, url, self._clock()
                )
                return
            live.pending_access = None
            mappings, keep = self._adopt_access(live, result, checked_at), set(self._nodes)
            self._touch()
        if self._access_store is not None:
            self._access_store.save(node_id, url, mappings, result, checked_at, keep=keep)

    def _adopt_access(self, live: _Live, result: dict, checked_at: float) -> list:
        """Make ``result`` the node's Test access result (the caller holds the lock); answers the saved rows."""
        live.access_result = copy.deepcopy(result)
        live.access_checked_at = checked_at
        if access_passed(result):
            live.access_problem = ""
            live.not_visible.clear()
        return copy.deepcopy(live.record.get("path_mappings") or [])

    # ------------------------------------------------------------------ checks on demand

    def check_now(self, node_id: str) -> NodeView | None:
        """Run a health check now (blocking). When the node has an access problem, a passing quick access check
        (one file per library) clears it.

        Returns:
            The view after the check, or None for an unknown node.
        """
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return None
            if not live.record.get("enabled", True):
                return self._view(live)
        self._run_check(node_id, access=True)
        return self.view(node_id)

    def request_check(self, node_id: str) -> NodeView | None:
        """Start the check :meth:`check_now` runs on the health checks' pool, and return at once.

        A node that doesn't answer takes up to ~40 s to check (three attempts 4 s apart), far too long to hold a
        web server thread for. A check already running or queued for the node answers this request too, so asking
        again queues nothing more.

        Returns:
            The view now (``checking`` until the check is done), or None for an unknown node.
        """
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return None
            if not live.record.get("enabled", True):
                return self._view(live)
            live.access_wanted = True
            if live.checking:
                return self._view(live)
            live.checking = True
            view = self._view(live)
        self._submit(self._run_check, node_id, False, True)
        return view

    def rescan_gpus(self, node_id: str) -> NodeView | None:
        """Ask a node to detect its GPUs again (blocking); clears ``gpu_lost`` when the lost GPU is back.

        Raises:
            NodeError: The node couldn't be asked, so the GPUs it reported before are all there is.
        """
        self._sync()
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return None
            device = live.lost_device
        self._gpu_check(node_id, device or None, raise_unreachable=True)
        return self.view(node_id)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the health loop (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_CHECKS, thread_name_prefix="node-check")
            self._thread = threading.Thread(target=self._loop, name="node-health", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the health loop and abandon checks still waiting to run."""
        with self._lock:
            thread, executor = self._thread, self._executor
            self._thread = self._executor = None
        self._stop.set()
        self._wake.set()
        if thread is not None:
            thread.join(timeout=5.0)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _submit(self, fn: Callable[..., Any], *args: Any) -> None:
        executor = self._executor
        if executor is not None:
            try:
                executor.submit(fn, *args)
                return
            except RuntimeError:
                pass  # Shut down meanwhile.
        threading.Thread(target=fn, args=args, name="node-check", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("Node health loop tick failed; trying again in a second")
            self._wake.wait(LOOP_TICK_S)
            self._wake.clear()

    def _tick(self) -> None:
        self._sync()
        now = self._clock()
        due_checks, due_gpus = [], []
        with self._lock:
            for node_id, live in self._nodes.items():
                if not live.record.get("enabled", True) or live.checking:
                    continue
                if live.next_check <= now:
                    live.checking = True
                    due_checks.append(node_id)
                elif live.gpu_lost and live.next_gpu_check <= now:
                    live.next_gpu_check = now + GPU_LOST_RECHECK_S
                    due_gpus.append((node_id, live.lost_device))
        for node_id in due_checks:
            self._submit(self._run_check, node_id, False, True)
        for node_id, device in due_gpus:
            self._submit(self._gpu_check, node_id, device)

    # ------------------------------------------------------------------ settings

    def _sync(self) -> None:
        try:
            key = self._revision_provider()
        except Exception as exc:
            logger.debug("Node settings unreadable: {}", exc)
            return
        if key == self._settings_key:
            return
        try:
            records = self._nodes_provider()
        except Exception as exc:
            logger.debug("Node settings unreadable: {}", exc)
            return
        store_work: list[tuple[str, str, Any]] = []
        with self._lock:
            if key == self._settings_key:
                return
            fresh: dict[str, _Live] = {}
            for record in records:
                node_id = str(record.get("id") or "")
                if not node_id or node_id in fresh:
                    continue
                fresh[node_id] = self._merge(self._nodes.get(node_id), record, store_work)
            self._nodes = fresh
            self._settings_key = key
            self._touch()
        self._wake.set()
        self._run_store_work(store_work)

    def _run_store_work(self, work: list[tuple[str, str, Any]]) -> None:
        """The access store's file reads and writes ``_merge`` asked for, done after the registry's lock is released
        (registry before store, never the other way round)."""
        store = self._access_store
        if store is None:
            return
        for action, node_id, payload in work:
            if action == "forget":
                keep_url, keep_mappings = payload if payload is not None else (None, None)
                store.forget(node_id, keep_url=keep_url, keep_mappings=keep_mappings)
                continue
            if action == "save":
                url, rows, result, checked_at = payload
                with self._lock:
                    keep = set(self._nodes)
                store.save(node_id, url, rows, result, checked_at, keep=keep)
                continue
            url, mappings = payload
            stored = store.load(node_id, url, mappings)
            if stored is None:
                continue
            with self._lock:
                live = self._nodes.get(node_id)
                # A test recorded meanwhile, or the address or mappings changed again, wins over what was on disk.
                if (
                    live is not None
                    and live.access_result is None
                    and str(live.record.get("url") or "") == url
                    and live.record.get("path_mappings") == mappings
                ):
                    live.access_result, live.access_checked_at = stored

    def _merge(self, live: _Live | None, record: dict, store_work: list[tuple[str, str, Any]]) -> _Live:
        """A node's live record after its stored record changed (or a new one for a new node).

        Args:
            live: The node's live record, or None for a new node.
            record: Its stored record.
            store_work: Collects ``("load", node id, (url, mappings))``, ``("forget", node id, (url, mappings) to
                keep, or None)`` and ``("save", node id, (url, mappings, result, checked_at))`` for
                :meth:`_run_store_work`.
        """
        url, mappings = str(record.get("url") or ""), copy.deepcopy(record.get("path_mappings"))
        if live is None:
            live = _Live(record=copy.deepcopy(record), client=self._client_factory(record))
            if self._access_store is not None:
                store_work.append(("load", str(record.get("id")), (url, mappings)))
            return live
        old = live.record
        address_changed = (old.get("url"), old.get("key")) != (record.get("url"), record.get("key"))
        reconnect = (
            address_changed
            or old.get("path_mappings") != record.get("path_mappings")
            or (record.get("enabled", True) and not old.get("enabled", True))
        )
        live.record = copy.deepcopy(record)
        old_name, new_name = str(old.get("name") or ""), str(record.get("name") or "")
        if old_name and new_name and old_name != new_name:
            # Reasons are written with the name the node had when it was checked: a rename shows now, not after the
            # next check (up to minutes for a node that is offline).
            live.probe_reason, live.access_problem, live.gpu_lost = (
                _renamed(text, old_name, new_name) for text in (live.probe_reason, live.access_problem, live.gpu_lost)
            )
            for other in self._nodes.values():
                if other is not live and other.probe_state == STATE_DUPLICATE:
                    other.probe_reason = other.probe_reason.replace(f" is {old_name} again,", f" is {new_name} again,")
        if address_changed or old.get("node_uid") != record.get("node_uid"):
            # Another address or key, or the key typed again (which clears the saved id): nothing to confirm now.
            live.unconfirmed_uid = ""
        restored_uid = old.get("node_uid") != record.get("node_uid") and record.get("node_uid") not in (
            None,
            "",
            live.heard_uid,
        )
        if address_changed or restored_uid or (record.get("enabled", True) and not old.get("enabled", True)):
            # Whoever answers there now (or the id a restored settings file names) is still to be checked.
            live.heard_uid = None
            live.next_check = 0.0
        if address_changed:
            live.client = self._client_factory(record)
        if reconnect:
            # A new address, key or mapping set is a different node as far as anything learnt before goes.
            live.probe_state, live.probe_reason, live.probe_cause = STATE_CONNECTING, "", ""
            live.offline_streak = 0
            live.next_check = 0.0
            live.access_problem = ""
            live.not_visible.clear()
            if address_changed:
                live.gpu_lost = live.lost_device = ""
                # A test held aside was another machine's answer, or one asked with the old key.
                live.pending_access = None
        if address_changed or old.get("path_mappings") != record.get("path_mappings"):
            # A test of another address or key (another machine, as far as anyone knows) or of other mappings says
            # nothing about these, unless it tested exactly the rows just saved, at this address.
            pending, live.pending_access = live.pending_access, None
            if (
                pending is not None
                and pending.rows == (record.get("path_mappings") or [])
                and pending.url == url
                and self._clock() - pending.held_at <= PENDING_ACCESS_TTL_S
            ):
                rows = self._adopt_access(live, pending.result, pending.checked_at)
                if self._access_store is not None:
                    store_work.append(("save", str(record.get("id")), (url, rows, pending.result, pending.checked_at)))
            else:
                live.access_result = live.access_checked_at = None
                if self._access_store is not None:
                    # Only a mappings change keeps a result a test just saved for the new rows: the file doesn't
                    # know the key, so a result saved at this address may be the old key's machine.
                    keep = None if address_changed else (url, mappings)
                    store_work.append(("forget", str(record.get("id")), keep))
        return live

    # ------------------------------------------------------------------ state

    def _state(self, live: _Live) -> tuple[str, str]:
        if not live.record.get("enabled", True):
            return STATE_DISABLED, "Turned off on the Nodes page"
        if live.probe_state == STATE_CONNECTING:
            return STATE_CONNECTING, "Checking the node"
        if live.probe_state != STATE_READY:
            return live.probe_state, live.probe_reason
        if live.access_problem:
            return STATE_ACCESS_PROBLEM, live.access_problem
        if live.gpu_lost:
            return STATE_GPU_LOST, live.gpu_lost
        return STATE_READY, ""

    def _view(self, live: _Live) -> NodeView:
        state, reason = self._state(live)
        worker = (live.info or {}).get("worker") or {}
        cpu = worker.get("cpu") or {}
        cores = _count(cpu.get("cores"))
        return NodeView(
            id=str(live.record.get("id") or ""),
            name=str(live.record.get("name") or ""),
            url=str(live.record.get("url") or ""),
            enabled=bool(live.record.get("enabled", True)),
            state=state,
            reason=reason,
            version=live.version,
            roles=live.roles,
            gpus=tuple({k: g[k] for k in ("type", "device", "name", "status")} for g in live.gpus),
            cpu_cores=cores,
            legacy=live.legacy,
            last_seen=live.last_seen,
            checking=live.checking,
            cause=live.probe_cause if state in _CAUSE_STATES else "",
            plex_db_unconfirmed=bool(live.unconfirmed_uid),
        )

    def _touch(self) -> None:
        """Bump the revision when anything a reader acts on changed (not on every ``last_seen``)."""
        with self._lock:
            signature = tuple(
                (
                    v.id,
                    v.name,
                    v.url,
                    v.enabled,
                    v.state,
                    v.reason,
                    v.version,
                    v.roles,
                    v.gpus,
                    v.legacy,
                    str(self._nodes[v.id].record.get("path_mappings")),
                )
                for v in (self._view(live) for live in self._nodes.values())
            )
            if signature != self._signature:
                self._signature = signature
                self._revision += 1

    # ------------------------------------------------------------------ the checks

    def _run_check(self, node_id: str, access: bool = False, claimed: bool = False) -> None:
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return
            if not claimed:
                live.checking = True
            record, client = copy.deepcopy(live.record), live.client
        try:
            with live.check_lock:
                with self._lock:
                    live.checks_begun += 1
                    number = live.checks_begun
                    seen_before = live.last_seen is not None and live.seen_url == str(record.get("url") or "")
                try:
                    outcome = self._probe(record, client, seen_before=seen_before)
                except Exception:
                    # Still a finished check: one that ended here would leave the node as it was (a ready node
                    # keeps getting work) whatever it answers now.
                    name = record.get("name") or "The node"
                    logger.opt(exception=True).warning("Health check of node {} couldn't read its answer", name)
                    outcome = _Outcome(
                        STATE_OFFLINE,
                        f"{name} answered in a way this app couldn't read. Check that the address is the node's and "
                        "that the node runs this app's version.",
                    )
                with self._lock:
                    if self._nodes.get(node_id) is not live or live.client is not client:
                        return  # Settings changed while it ran: this answer is about an old address.
                    if live.record.get("node_uid") != record.get("node_uid"):
                        # The saved id changed while it ran (a restore, the key typed again): the id it heard was
                        # weighed against the old one. The next check, now, weighs it against this one.
                        live.next_check = 0.0
                        return
                    twin = self._twin_of(node_id, outcome.uid)
                    if twin:
                        # Sent work as two nodes, one machine would get twice its workers' files.
                        outcome = replace(
                            outcome,
                            state=STATE_DUPLICATE,
                            reason=f"{record.get('name') or 'This node'} is {twin} again, added at another address. "
                            "Remove one of them.",
                        )
                    self._apply(live, outcome, number)
                    new_uid = bool(outcome.uid and not twin and outcome.uid != record.get("node_uid"))
                    if new_uid or twin:
                        # A new id is settled by ``_new_uid`` first: until then nothing may reach the node (the saved
                        # id heard before is another machine's now). A twin's is the other node's machine, whatever
                        # this record says.
                        live.heard_uid = None
                    elif outcome.uid or outcome.state != STATE_OFFLINE:
                        live.heard_uid = outcome.uid
                    if not new_uid and outcome.uid and record.get("node_uid") and outcome.uid != live.untied_uid:
                        # A twin's id is never recorded, so another machine stays unconfirmed: once the other node is
                        # removed, the next check decides whether this one is the same Plex. An id still to be tied
                        # keeps what ``_tie_new_uid`` decided: hearing it again says nothing about its Plex.
                        live.unconfirmed_uid = "" if outcome.uid == record.get("node_uid") else outcome.uid
                    has_access_problem = bool(live.access_problem)
                    access = access or live.access_wanted or live.next_access_check <= self._clock()
                    live.access_wanted = False
                if new_uid:
                    self._new_uid(node_id, live, record, outcome)
                if access and has_access_problem and outcome.state == STATE_READY:
                    self._quick_access_clear(node_id, live, client)
                if outcome.info is not None:
                    # It answered (ready or not: a full temp folder needs them most): the DELETEs lost while it was
                    # away reach it now, rather than its finished tasks keeping their temp dirs for 10 minutes.
                    from .executor import send_pending_deletes

                    send_pending_deletes(node_id, client)
        except Exception:
            logger.exception("Health check of node {} failed unexpectedly", record.get("name") or node_id)
        finally:
            with self._lock:
                live.checking = False
                self._check_done.notify_all()

    def _twin_of(self, node_id: str, uid: str) -> str:
        """The name of another node already saved with id ``uid`` ("" when none; the caller holds the lock)."""
        for other_id, other in self._nodes.items():
            if uid and other_id != node_id and other.record.get("node_uid") == uid:
                return str(other.record.get("name") or other_id)
        return ""

    def _new_uid(self, node_id: str, live: _Live, record: dict, outcome: _Outcome) -> None:
        """Record a node's new id, or hold back a Plex database node's until it is tied to its Plex."""
        confirmed = self._new_uid_confirmed(node_id, record, outcome)
        with self._lock:
            if self._nodes.get(node_id) is not live or live.record.get("node_uid") != record.get("node_uid"):
                live.next_check = 0.0
                return  # The saved id changed meanwhile (a restore, the key typed again): the next check settles it.
            if confirmed is None:
                # ``heard_uid`` stays None, so the node's Plex database work waits for that check.
                live.next_check = min(live.next_check, self._clock() + WAITING_RECHECK_S)
                return
            if confirmed:
                live.unconfirmed_uid = ""
                live.heard_uid = outcome.uid
                live.untied_uid = outcome.uid if record.get("node_uid") else ""
            else:
                # ``heard_uid`` stays None: it holds only the saved id or one accepted here.
                if live.unconfirmed_uid != outcome.uid:
                    logger.warning(
                        "{} reports a new id and isn't next to a Plex this app can confirm; its Plex database work "
                        "waits until its key is typed again",
                        record.get("name") or node_id,
                    )
                live.unconfirmed_uid = outcome.uid
        if confirmed:
            self._record_uid(node_id, outcome.uid, str(record.get("url") or ""))

    def _new_uid_confirmed(self, node_id: str, record: dict, outcome: _Outcome) -> bool | None:
        """Whether a node's new id may replace the saved one without asking, or None when that can't be told yet.

        A worker node (or one with no saved id yet) is this node by its saved key and address. A node that a Plex
        server uses as its Plex database node writes into that Plex's database, so its new id is taken only when it
        names the Plex machine identifier of every server that uses it; an empty identifier on either side (a Windows
        node with a custom Plex folder) proves nothing. That holds whether or not this answer names the role: an id
        taken without it would already match once the node names it.

        Which servers use the node comes from the settings; while they can't be read the id is neither taken nor held
        for the user to confirm (None), and the next check, soon, decides.
        """
        if not record.get("node_uid"):
            return True
        try:
            served = self._served_plex_ids(node_id)
        except Exception as exc:  # noqa: BLE001 - unreadable settings can't confirm anything
            logger.debug("Couldn't read which Plex servers use node {}: {}", node_id, exc)
            return None
        if not served:
            return True  # No server writes through it now; one switched on later ties it first (``_tie_new_uid``).
        return _beside_their_plex(served, outcome.info)

    def _tie_new_uid(self, node_id: str) -> bool:
        """Compare an id ``_new_uid`` took in place of a saved one (``_Live.untied_uid``) with the Plex of the
        switched-on servers using the node now; one that isn't theirs is held as ``_new_uid`` holds it.

        Returns:
            False while the servers using the node can't be read (nothing is decided), else True.
        """
        with self._lock:
            live = self._nodes.get(node_id)
            uid = live.untied_uid if live is not None else ""
            if live is None or not uid or live.record.get("node_uid") != uid:
                return True
            info = copy.deepcopy(live.info)
        try:
            served = self._served_plex_ids(node_id)
        except Exception as exc:  # noqa: BLE001 - unreadable settings can't tie anything
            logger.debug("Couldn't read which Plex servers use node {}: {}", node_id, exc)
            return False
        if not served:
            return True  # Still nothing to protect: no switched-on server writes through it.
        tied = _beside_their_plex(served, info)
        with self._lock:
            if self._nodes.get(node_id) is not live or live.untied_uid != uid or live.record.get("node_uid") != uid:
                return True  # Settled meanwhile (the key typed again, another id).
            if tied:
                live.untied_uid = ""
                if live.unconfirmed_uid == uid:
                    live.unconfirmed_uid = ""
                return True
            if live.unconfirmed_uid != uid:
                logger.warning(
                    "{} reports a new id and isn't next to a Plex this app can confirm; its Plex database work "
                    "waits until its key is typed again",
                    live.record.get("name") or node_id,
                )
            live.unconfirmed_uid = uid
        return True

    def _apply(self, live: _Live, outcome: _Outcome, number: int) -> None:
        now = self._clock()
        if outcome.restarting and live.restarting_since is None:
            live.restarting_since = now
        restarting = live.restarting_since is not None and now - live.restarting_since < RESTART_WINDOW_S
        if restarting and outcome.cause in (OFFLINE_UNREACHABLE, OFFLINE_STUCK):
            # Refused, or dropped before an answer, while it starts again: the restart it announced, so its card keeps
            # saying so rather than sending someone to check its address or restart it.
            outcome = replace(outcome, reason=_restarting_reason(live.record.get("name")), cause=OFFLINE_RESTARTING)
        live.probe_state, live.probe_reason, live.probe_cause = outcome.state, outcome.reason, outcome.cause
        if outcome.state == STATE_OFFLINE and not outcome.uid:
            # It didn't reach the node: whoever answers there next gives its id again first.
            live.heard_uid = None
        if outcome.state == STATE_OFFLINE and restarting:
            # Saying it stops, then not answering while it starts again: neither is a node that went away.
            live.offline_streak = 0
            delay = RESTART_RECHECK_S
        elif outcome.state == STATE_OFFLINE:
            live.restarting_since = None
            live.offline_streak += 1
            delay = OFFLINE_BACKOFF_S[min(live.offline_streak, len(OFFLINE_BACKOFF_S)) - 1]
        else:
            live.restarting_since = None
            live.offline_streak = 0
            delay = HEALTH_INTERVAL_S
            live.last_seen = self._wall_clock()
            live.seen_url = str(live.record.get("url") or "")
        if outcome.state == STATE_READY:
            live.ready_check = number
        if outcome.state == STATE_KEY_REFUSED:
            live.key_refused_at = now
        # Tasks report the refused key as it reaches each of their polls; checking again would only be refused again.
        recheck = number <= live.reported_at_check and outcome.state != STATE_KEY_REFUSED
        live.next_check = 0.0 if recheck else now + delay
        if outcome.info is not None or outcome.legacy:
            live.info = outcome.info
            live.legacy = outcome.legacy
            live.version = outcome.version
            live.roles = outcome.roles
            live.gpus = _gpus_in(outcome.info)
            if live.gpu_lost and live.lost_device and _gpu_ok(live.gpus, live.lost_device)[0]:
                live.gpu_lost = live.lost_device = ""
            self._note_gpu_detection(live, outcome.info)
        self._touch()

    def _note_gpu_detection(self, live: _Live, answer: object) -> None:
        """Pause a worker node's GPU members while it couldn't detect its GPUs yet (the caller holds the lock).

        Read as a GPU check still to come (``gpu_lost`` with no ``lost_device``), so its members come back once a
        detection works rather than waiting on someone.
        """
        error = _gpu_detection_pending(answer) if protocol.ROLE_WORKER in live.roles else ""
        if error and not live.gpu_lost:
            name = str(live.record.get("name") or "The node")
            logger.warning(
                "Node {} couldn't detect its GPUs ({}); its GPU workers wait for its next check", name, error
            )
            live.gpu_lost = (
                f"{name} couldn't check its GPUs yet ({error}). It tries again in a few minutes; its CPU workers "
                "keep running."
            )
            live.next_gpu_check = self._clock() + GPU_LOST_RECHECK_S
        elif not error and live.gpu_lost and not live.lost_device:
            live.gpu_lost = ""

    def _probe(self, record: dict, client: NodeClient, *, seen_before: bool = False) -> _Outcome:
        name = str(record.get("name") or "The node")
        url = str(record.get("url") or "")
        info: dict | None = None
        timeouts = 0
        rate_limited = False
        why = ""
        for attempt in range(CHECK_ATTEMPTS):
            try:
                info = _bounded(client.info())
                break
            except NodeAuthError:
                return _Outcome(
                    STATE_KEY_REFUSED,
                    f"{name} refused the key. Copy the key from the node's log (Docker) or node.json (Windows) into this node's key field.",
                )
            except NodeProtocolError as exc:
                if exc.node is not None:
                    return self._other_protocol(name, _bounded(exc.node))
                return self._probe_legacy(record)
            except NodeError as exc:
                # A wait (429) is tried again too: the node lets one more key through every few seconds, so a check
                # held back once by something else sharing this app's address mustn't read the node as offline.
                logger.debug("Node {} check attempt {} failed: {}", name, attempt + 1, exc)
                timeouts += isinstance(exc, NodeTimeoutError)
                # Only the last attempt's: a wait that passed, then refused connections, is a node that's down.
                rate_limited = isinstance(exc, NodeRateLimitedError)
                why = str(getattr(exc, "reason", "") or "")
                if attempt + 1 < CHECK_ATTEMPTS and self._stop.wait(self._attempt_gap_s):
                    break
        if info is None and rate_limited:
            return _Outcome(
                STATE_OFFLINE,
                f"{name} asks this app to wait: too many wrong keys came from this app's address. Something else "
                "there (another app, an old copy) has an old key: give it the right one, or stop it.",
                cause=OFFLINE_RATE_LIMITED,
            )
        # Over https, timeouts are what a node without an https proxy in front looks like: the address is wrong.
        if info is None and timeouts == CHECK_ATTEMPTS and not url.lower().startswith("https:"):
            return _Outcome(STATE_OFFLINE, unreachable_advice(why, url, name, connected=True), cause=OFFLINE_STUCK)
        if info is None:
            return _Outcome(
                STATE_OFFLINE, unreachable_advice(why, url, name, was_seen=seen_before), cause=OFFLINE_UNREACHABLE
            )
        return self._read_info(record, info)

    def _other_protocol(self, name: str, block: dict) -> _Outcome:
        """A node that refused this app's node protocol: one of the two needs an update (it is a node: no ping)."""
        version = str(block.get("version") or "")
        roles = tuple(r for r in (block.get("roles") or []) if r in protocol.ROLES)
        return _Outcome(
            STATE_UPDATE_NEEDED,
            f"{name} runs version {version or 'unknown'} and this app runs {self._app_version()}, and they don't "
            "speak the same node protocol. Update the older one so both run the same version.",
            info={"node": dict(block)},
            version=version,
            roles=roles,
        )

    def _read_info(self, record: dict, info: dict) -> _Outcome:
        from ..markers.publishers.plex_remote import (
            AGENT_PROTOCOL_NODE_UID,
            MIN_AGENT_VERSION,
            agent_older_than,
            agent_too_old,
        )

        name = str(record.get("name") or "The node")
        node_block = info.get("node")
        block: dict = node_block if isinstance(node_block, dict) else {}
        uid = str(block.get("id") or "")
        roles = tuple(r for r in (block.get("roles") or []) if r in protocol.ROLES)
        version = protocol.node_version(info)
        outcome: dict[str, Any] = {"info": info, "version": version, "roles": roles, "uid": uid}
        if info.get("stopping") is True:
            # It refuses every start until it is back (SIGTERM: a restart or an image update).
            return _Outcome(
                STATE_OFFLINE,
                _restarting_reason(name),
                cause=OFFLINE_RESTARTING,
                restarting=True,
                **outcome,
            )
        app_version = self._app_version()
        if protocol.ROLE_WORKER in roles and version != app_version:
            # A node that is ahead is matched by updating this app; one whose version can't be read is updated.
            app_behind = agent_older_than(app_version, version)
            update = f"Update this app to {version}." if app_behind else f"Update the node to {app_version}."
            return _Outcome(
                STATE_UPDATE_NEEDED,
                f"{name} runs version {version or 'unknown'} and this app runs {app_version}. {update}",
                cause=UPDATE_APP_BEHIND if app_behind else "",
                **outcome,
            )
        build, app_build = str(block.get("build") or ""), self._app_build()
        if protocol.ROLE_WORKER in roles and not protocol.same_build(build, app_build):
            return _Outcome(
                STATE_UPDATE_NEEDED,
                f"{name} runs another build of version {version} ({build[:7]}) than this app ({app_build[:7]}). "
                "Update the node to the app's image.",
                **outcome,
            )
        worker = info.get("worker")
        ffmpeg = worker.get("ffmpeg") if isinstance(worker, dict) else None
        if protocol.ROLE_WORKER in roles and isinstance(ffmpeg, dict) and not ffmpeg.get("path"):
            return _Outcome(
                STATE_NO_FFMPEG,
                f"{name} can't find FFmpeg, so every file sent to it would fail. Install FFmpeg on that machine "
                "(or put it on its PATH), then restart the node.",
                **outcome,
            )
        # The node answers a path with no version when the FFmpeg it found won't run (wrong architecture, a missing
        # library, no permission): every file would fail just the same.
        if protocol.ROLE_WORKER in roles and isinstance(ffmpeg, dict) and not ffmpeg.get("version"):
            return _Outcome(
                STATE_NO_FFMPEG,
                f"{name} found FFmpeg at {ffmpeg.get('path')}, but it doesn't run, so every file sent to it would "
                "fail. Reinstall FFmpeg on that machine, then restart the node.",
                **outcome,
            )
        temp = worker.get("temp") if isinstance(worker, dict) else None
        temp_problem = temp.get("problem") if isinstance(temp, dict) else None
        if protocol.ROLE_WORKER in roles and isinstance(temp_problem, str) and temp_problem.strip():
            # The node refuses every start while it can't make a task folder there.
            said = temp_problem.strip()[:TEMP_PROBLEM_MAX]
            return _Outcome(
                STATE_TEMP_PROBLEM,
                f"{name}'s temp folder can't be used, so it gets no files: {said[:1].lower()}{said[1:]}. "
                f"{_temp_folder_advice(said)}",
                **outcome,
            )
        agent = info.get("agent")
        agent_version = str(agent.get("version") or "") if isinstance(agent, dict) else ""
        if protocol.ROLE_PLEX_DB in roles and agent_version and agent_too_old(agent_version):
            return _Outcome(
                STATE_UPDATE_NEEDED,
                f"{name}'s Plex database role is version {agent_version}; this app needs {MIN_AGENT_VERSION} or "
                "newer. Update the node.",
                **outcome,
            )
        agent_protocols = agent.get("protocols") if isinstance(agent, dict) else None
        if (
            protocol.ROLE_PLEX_DB in roles
            and uid
            and isinstance(agent_protocols, list)
            and AGENT_PROTOCOL_NODE_UID not in agent_protocols
        ):
            # Its role ignores the node id this app names, so this app sends it the protocol that carries the id,
            # which it refuses: every Plex database request would fail while the node read as ready.
            return _Outcome(
                STATE_UPDATE_NEEDED,
                f"{name}'s Plex database role is version {agent_version or 'unknown'}, too old for this app. Update "
                "the node.",
                **outcome,
            )
        return _Outcome(STATE_READY, **outcome)

    def _probe_legacy(self, record: dict) -> _Outcome:
        """A node that doesn't know ``/v1/info``: an old Plex helper keeps working as a Plex database node, unless an id
        was saved for the node (``no_id_reason``)."""
        from ..markers.publishers.plex_remote import (
            AGENT_CONNECTED,
            AGENT_INCOMPATIBLE,
            AGENT_REJECTED,
            MIN_AGENT_VERSION,
            agent_too_old,
        )

        name = str(record.get("name") or "The node")
        state, version = self._legacy_ping(str(record.get("url") or ""), str(record.get("key") or ""))
        legacy: dict[str, Any] = {"legacy": True, "version": version, "roles": (protocol.ROLE_PLEX_DB,), "info": None}
        if state == AGENT_CONNECTED:
            if record.get("node_uid"):
                # A node that gave an id answered here before: every Plex database request names it, which this
                # release refuses, so it reads as needing an update rather than ready.
                return _Outcome(STATE_UPDATE_NEEDED, no_id_reason(record.get("name")), **legacy)
            return _Outcome(STATE_READY, **legacy)
        if state == AGENT_REJECTED:
            return _Outcome(STATE_KEY_REFUSED, f"{name} refused the key. Set the same key on both sides.", **legacy)
        if state == AGENT_INCOMPATIBLE:
            if agent_too_old(version):
                reason = (
                    f"{name} is a Plex database node, version {version}; this app needs {MIN_AGENT_VERSION} or "
                    "newer. Update it."
                )
            else:
                reason = f"{name} speaks a newer protocol than this app. Update this app to match it."
            return _Outcome(STATE_UPDATE_NEEDED, reason, **legacy)
        return _Outcome(
            STATE_OFFLINE,
            "Something answered at this address, but not as a node this app can use. Check the address.",
            cause=OFFLINE_NOT_A_NODE,
        )

    def _quick_access_clear(self, node_id: str, live: _Live, client: NodeClient) -> None:
        with self._lock:
            live.next_access_check = self._clock() + ACCESS_PROBLEM_RECHECK_S
            problem = live.access_problem
            mappings = node_mappings(live.record)
            unseen = sorted({path for _, path in live.not_visible})
        try:
            passed = self._quick_access(client, mappings, unseen)
        except Exception as exc:
            logger.debug("Quick access check on node {} failed: {}", node_id, exc)
            return
        if not passed:
            return
        with self._lock:
            # A Test access or new mappings meanwhile settled this pause, and files missed since may have raised
            # another: this check passed on the old one.
            if self._nodes.get(node_id) is not live or live.access_problem != problem:
                return
            live.access_problem = ""
            live.not_visible.clear()
            self._touch()
        logger.info("Node {} can see your media again; its workers resume", live.record.get("name") or node_id)

    def _gpu_check(self, node_id: str, device: str | None, raise_unreachable: bool = False) -> None:
        with self._lock:
            live = self._nodes.get(node_id)
            if live is None:
                return
            client, name = live.client, str(live.record.get("name") or "The node")
        try:
            answer = _bounded(client.rescan_gpus())
            gpus = _gpus_in(answer)
        except NodeError as exc:
            logger.debug("GPU rescan on node {} failed: {}", name, exc)
            self.report_unavailable(node_id)
            if raise_unreachable:
                raise
            return
        with self._lock:
            if self._nodes.get(node_id) is not live or live.client is not client:
                return
            live.gpus = gpus
            if device:
                ok, why = _gpu_ok(gpus, device)
                if ok:
                    if live.lost_device == device:
                        live.gpu_lost = live.lost_device = ""
                else:
                    if not live.gpu_lost:
                        logger.warning("Node {} lost its GPU {} ({}); its GPU workers are paused", name, device, why)
                    live.gpu_lost = (
                        f"{name} lost its GPU {device} ({why}). Its CPU workers keep running. Check the GPU driver on "
                        "the node."
                    )
                    live.lost_device = device
                    live.next_gpu_check = self._clock() + GPU_LOST_RECHECK_S
            self._note_gpu_detection(live, answer)
            self._touch()


def access_passed(result: object) -> bool:
    """Whether a Test access result saw every sampled file (and sampled at least one).

    A library failed for a row inside it that this app couldn't read counts even with no file checked.
    """
    if not isinstance(result, dict):
        return False
    libraries = [lib for lib in result.get("libraries") or [] if isinstance(lib, dict)]
    if any(lib.get("unreadable") for lib in libraries):
        return False
    checked = [lib for lib in libraries if lib.get("checked")]
    return bool(checked) and all(lib.get("ok") for lib in checked)


_registry: NodeRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> NodeRegistry:
    """The process-wide registry."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = NodeRegistry(access_store=AccessResultStore())
        return _registry


def note_work_waiting(node_ids: Iterable[str]) -> None:
    """:meth:`NodeRegistry.work_waiting` on the process-wide registry; nothing before one exists (no node checked)."""
    with _registry_lock:
        registry = _registry
    if registry is not None:
        registry.work_waiting(node_ids)


def set_registry(registry: NodeRegistry | None) -> None:
    """Replace the process-wide registry (tests, and the app factory)."""
    global _registry
    with _registry_lock:
        _registry = registry
