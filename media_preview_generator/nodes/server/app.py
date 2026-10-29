"""The node's Flask app: auth, the node routes of protocol 1 (spec §3), and the roles it was started with.

Import discipline (spec §10.1): this module loads only Flask, loguru, ``nodes.protocol`` and the small server
modules at import. GPU detection and the ops (FFmpeg, numpy, ONNX) are imported only when the worker role is on, and
the Plex database role only when that role is on, so the slim Plex database builds can run ``create_node_app``.
"""

from __future__ import annotations

import os
import platform
import secrets
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from flask import Blueprint, Flask, jsonify, request, send_file
from loguru import logger
from werkzeug.exceptions import HTTPException

from ... import __version__
from .. import protocol
from ..protocol import OpFn
from . import access, identity, nodelog, tasks

# Task specs and access checks are a path, a few mapping rows and small params: kilobytes. Anything larger is
# refused before it is parsed.
MAX_BODY_BYTES = 2 * 1024 * 1024
ACCESS_CHECK_MAX_FILES = 200
LOG_DEFAULT_LINES = 200
# How much of a protocol header that isn't this node's the refusal repeats back.
PROTOCOL_ECHO_MAX = 32
EXTENSION = "mpg_node"
# ``app.config`` key: how many requests the server in front of the app serves at once (``wsgi.create_node_server``).
SERVER_THREADS = "MPG_NODE_SERVER_THREADS"
# ``FILE__NODE_KEY=/run/secrets/node_key`` sets NODE_KEY to that file's contents (linuxserver's way to pass secrets).
FILE_ENV_PREFIX = "FILE__"
# Read before the node starts (the image's init and wrapper.sh; Python itself reads TZ), so a FILE__ file can't set
# them: they are left alone, and the node's server says so.
CONTAINER_SETTINGS = ("MODE", "PUID", "PGID", "TZ")


@dataclass
class NodeServerState:
    """What one node app holds (``app.extensions["mpg_node"]``)."""

    identity: identity.NodeIdentity
    roles: list[str]
    node_block: dict[str, Any]
    log: nodelog.NodeLogBuffer
    tasks: tasks.TaskTable
    worker_info: Any = None
    plex_db_module: Any = None

    def close(self, wait_s: float = 0.0) -> None:
        """Stop the background threads and cancel every task.

        Args:
            wait_s: How long to wait for the cancelled tasks' ops (and their FFmpeg) to stop; a node shutting down
                waits, tests don't.
        """
        self.tasks.close(wait_s)
        if self.worker_info is not None:
            self.worker_info.stop()


def _env_from_files(environ: MutableMapping[str, str] | None = None) -> None:
    """Set each ``FILE__<NAME>`` variable's ``<NAME>`` to the contents of the file it names (a Docker secret).

    The main image's s6 init would do this, but the image keeps the container's own environment (``S6_KEEP_ENV``), so
    its copy never reaches the node; the Plex database image has no such init. Whitespace around the value (the
    newline ``echo key > file`` leaves) and a byte-order mark (Notepad's) are dropped. The node's server reads them
    before its port and host (``wsgi``), and :func:`create_node_app` again for the other entry points: a value already
    set from its file is left as it is.

    Args:
        environ: For tests; default ``os.environ``.

    Raises:
        SystemExit: A named file can't be read or holds nothing: the node would otherwise start with a key of its
            own, and the app's every request would be refused.
    """
    environ = os.environ if environ is None else environ
    for var, path in sorted(environ.items()):
        name = var[len(FILE_ENV_PREFIX) :]
        if not var.startswith(FILE_ENV_PREFIX) or not name or name in CONTAINER_SETTINGS:
            continue
        try:
            with open(path, encoding="utf-8-sig") as handle:
                value = handle.read().strip()
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "strerror", None) or type(exc).__name__
            # Never the path: the obvious slip is the value itself (the key) in FILE__NODE_KEY, and this is logged.
            # ``from None`` too: the OSError's own text names the path.
            raise SystemExit(
                f"{var} names a file the node can't read ({reason}). Set it to the path of a file holding {name} "
                f"(or set {name} to the value itself), and let {identity.node_user()} read that file."
            ) from None
        if not value:
            # A secret never filled in (``touch``, ``echo $UNSET > file``): NODE_KEY would be blank, which the node
            # skips for node.json's key without a word.
            raise SystemExit(f"{var} names {path}, which is empty. Put {name} in that file, or set {name} instead.")
        if environ.get(name) == value:
            continue
        environ[name] = value
        logger.info("{} set from {}", name, var)


def _resolve_roles(roles: list[str] | None) -> list[str]:
    raw = roles if roles is not None else (os.environ.get("NODE_ROLES", "").strip() or protocol.ROLE_WORKER).split(",")
    chosen: list[str] = []
    for role in (str(r).strip() for r in raw):
        if not role:
            continue
        if role not in protocol.ROLES:
            raise SystemExit(f"NODE_ROLES: unknown role {role!r}; use {' and/or '.join(protocol.ROLES)}.")
        if role not in chosen:
            chosen.append(role)
    if not chosen:
        raise SystemExit(f"NODE_ROLES must name at least one role: {', '.join(protocol.ROLES)}.")
    # Set by the slim Plex database image, which carries no FFmpeg or GPU code: a worker role there would look healthy
    # and fail every file sent to it.
    image_roles = [r.strip() for r in os.environ.get("NODE_IMAGE_ROLES", "").split(",") if r.strip()]
    if image_roles and protocol.ROLE_WORKER in chosen and protocol.ROLE_WORKER not in image_roles:
        raise SystemExit(
            "This image only has the Plex database role; use the main image (with MODE=node) for a worker node."
        )
    return chosen


def _worker_temp_root() -> str:
    """The worker role's temp folder (``NODE_TEMP_DIR``, else the system temp folder's ``mpg-node``), made now.

    Raises:
        SystemExit: It can't be made: a path under a file, a parent the node can't write, or no writable system temp
            folder (a container started with ``--read-only`` and no tmpfs).
    """
    chosen = os.environ.get("NODE_TEMP_DIR", "").strip()
    try:
        root = tasks.default_temp_root()
    except OSError as exc:
        raise SystemExit(
            f"The node found no temp folder it can write to ({exc.strerror or exc}). Set NODE_TEMP_DIR to one "
            "(a container started read-only needs a tmpfs or a volume for it)."
        ) from exc
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        setting = f"NODE_TEMP_DIR ({root})" if chosen else f"The node's temp folder {root}"
        raise SystemExit(
            f"{setting} can't be made ({exc.strerror or exc}). Set NODE_TEMP_DIR to a folder the node can write to."
        ) from exc
    return root


def create_node_app(
    *,
    config_dir: str | None = None,
    roles: list[str] | None = None,
    ops: Mapping[str, OpFn] | None = None,
    key: str | None = None,
    unpolled_expiry_s: float = protocol.TASK_UNPOLLED_EXPIRY_S,
    finished_keep_s: float = protocol.TASK_FINISHED_KEEP_S,
    reap_interval_s: float = tasks.REAP_INTERVAL_S,
) -> Flask:
    """Build the node.

    Args:
        config_dir: Where ``node.json`` lives (default ``CONFIG_DIR``, else ``/config``).
        roles: The roles to serve (default ``NODE_ROLES``, else ``worker``).
        ops: Op name -> function (default ``nodes.server.ops.OPS`` when the worker role is on).
        key: The key the app must send (default ``NODE_KEY``, ``AGENT_TOKEN``, else node.json's).
        unpolled_expiry_s: For tests: drop a running task nobody polled for this long.
        finished_keep_s: For tests: drop a finished task this long after its last poll.
        reap_interval_s: For tests: how often expiry is checked.

    Returns:
        The Flask app.

    Raises:
        SystemExit: A ``FILE__`` variable naming a file it can't read, an unknown role, a bad key, an unreadable
            node.json, the worker role without a temp folder it can make, or the Plex database role without
            ``PLEX_CONFIG_DIR``.
    """
    _env_from_files()
    chosen_roles = _resolve_roles(roles)
    worker = protocol.ROLE_WORKER in chosen_roles
    # Only the worker role runs tasks: a Plex database node needs no temp folder (it may run with --read-only; the
    # server keeps answers waiting for the app in memory, wsgi.OUTBUF_OVERFLOW_BYTES). Made before the identity, so a
    # node that can't start hasn't spent the one-time notice of its new key.
    temp_root = _worker_temp_root() if worker else None
    me = identity.load_identity(config_dir, key=key)
    node_block = {
        "id": me.id,
        "name": me.name,
        "version": __version__,
        "build": protocol.build_sha(),
        "protocols": [protocol.NODE_PROTOCOL],
        "roles": list(chosen_roles),
    }

    # static_folder=None: a node serves its API only; Flask's default would add /static/<path:filename>.
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    app.json.sort_keys = False  # type: ignore[attr-defined]  # DefaultJSONProvider has it; the base class does not

    log_buffer = nodelog.install()
    if worker and ops is None:
        from .ops import OPS, load_op_code

        load_op_code()
        ops = OPS
    worker_info = None
    if temp_root is not None:
        from .info import WorkerInfo

        worker_info = WorkerInfo(temp_root=temp_root)
    table = tasks.TaskTable(
        ops if worker and ops is not None else {},
        temp_root=temp_root,
        unpolled_expiry_s=unpolled_expiry_s,
        finished_keep_s=finished_keep_s,
        reap_interval_s=reap_interval_s,
        on_gpu_failed=worker_info.note_gpu_error if worker_info is not None else None,
    )
    if worker_info is not None:
        worker_info.start()
    state = NodeServerState(
        identity=me, roles=chosen_roles, node_block=node_block, log=log_buffer, tasks=table, worker_info=worker_info
    )
    app.extensions[EXTENSION] = state

    app.register_blueprint(_node_blueprint(state, identity.wrong_key_limit(app)))

    if protocol.ROLE_PLEX_DB in chosen_roles:
        plex_config_dir = os.environ.get("PLEX_CONFIG_DIR", "").strip()
        if not plex_config_dir:
            raise SystemExit("The Plex database role needs PLEX_CONFIG_DIR: Plex's config folder as this node sees it.")
        from . import plex_db

        plex_db.register_plex_db(app, plex_config_dir=plex_config_dir, key=me.key, node_uid=me.id)
        state.plex_db_module = plex_db

    logger.info(
        "Node {} ({}) ready: version {}, roles {}, key from {}",
        me.name,
        me.id[:8],
        __version__,
        ", ".join(chosen_roles),
        me.key_source,
    )
    return app


def _node_blueprint(state: NodeServerState, wrong_keys: identity.WrongKeyLimit) -> Blueprint:
    """The node routes. Their auth and protocol checks live on the blueprint, not the app, so the Plex database
    role's routes keep the agent's own header check."""
    bp = Blueprint("node", __name__)
    key_bytes = state.identity.key.encode("utf-8", "surrogateescape")
    node_block = state.node_block
    table = state.tasks

    def answer(body: dict[str, Any] | None = None, status: int = 200, *, ok: bool = True) -> Any:
        return jsonify({"ok": ok, "node": node_block, **(body or {})}), status

    def refuse(kind: str, status: int, message: str = "") -> Any:
        error: dict[str, Any] = {"kind": kind}
        if message:
            error["message"] = message
        return answer({"error": error}, status, ok=False)

    def authorised(caller: str) -> bool:
        header = request.headers.get("Authorization", "")
        offered = header[7:].strip() if header.startswith("Bearer ") else ""
        # Compared as BYTES: werkzeug decodes headers as latin-1, and compare_digest raises TypeError on a str
        # holding anything above U+007F, so a stray non-ASCII byte would 500 on the auth path instead of 401.
        ok = bool(offered) and secrets.compare_digest(offered.encode("utf-8", "surrogateescape"), key_bytes)
        if not ok:
            wrong_keys.note_wrong(caller)
        return ok

    @bp.before_request
    def _guard() -> Any:
        if request.path == protocol.ROUTE_HEALTH:
            return None
        caller = request.remote_addr or ""
        wait_s = wrong_keys.blocked_for(caller)
        if wait_s:
            body, status = refuse(protocol.KIND_RATE_LIMITED, 429)
            return body, status, {"Retry-After": str(wait_s)}
        if not authorised(caller):
            # No detail: an unauthenticated caller learns nothing about this node.
            return refuse(protocol.KIND_AUTH, 401)
        asked = request.headers.get(protocol.PROTOCOL_HEADER, "").strip()
        if protocol.whole_number(asked) != protocol.NODE_PROTOCOL:
            return refuse(
                protocol.KIND_PROTOCOL,
                409,
                f"This node is version {__version__} and speaks node protocol {protocol.NODE_PROTOCOL}; the app "
                f"asked for {asked[:PROTOCOL_ECHO_MAX] or 'none'}.",
            )
        return None

    @bp.errorhandler(HTTPException)
    def _http_error(exc: HTTPException) -> Any:
        return refuse(protocol.KIND_REQUEST, exc.code or 400, exc.description or exc.name)

    def json_body() -> dict | None:
        try:
            body = request.get_json(silent=True)
        except RecursionError:  # silent=True only covers ValueError; a deeply nested body raises this instead
            return None
        return body if isinstance(body, dict) else None

    def unknown_task() -> Any:
        return refuse(protocol.KIND_UNKNOWN_TASK, 404, "This node doesn't know that task")

    @bp.get(protocol.ROUTE_HEALTH)
    def health() -> Any:
        """Liveness for the container's HEALTHCHECK: no key needed, nothing about the media."""
        body: dict[str, Any] = {"stopping": table.stopping}
        if state.plex_db_module is not None:
            body["agent"] = state.plex_db_module.agent_block()
        return answer(body)

    @bp.get(protocol.ROUTE_INFO)
    def info() -> Any:
        body: dict[str, Any] = {
            "platform": {"os": platform.system(), "arch": platform.machine(), "python": platform.python_version()},
            # Stopping (SIGTERM): it takes no new task, so the app's check reads it as not ready.
            "stopping": table.stopping,
        }
        if state.worker_info is not None:
            body["worker"] = state.worker_info.block(busy=table.busy(), filled=table.filled_problem())
        info_block = getattr(state.plex_db_module, "info_block", None)
        if callable(info_block):
            body["plex_db"] = info_block()
        if state.plex_db_module is not None:
            # The app's health check reads the role's version here (its MIN_AGENT_VERSION rule).
            body["agent"] = state.plex_db_module.agent_block()
        return answer(body)

    @bp.post(protocol.ROUTE_GPUS_RESCAN)
    def gpus_rescan() -> Any:
        if state.worker_info is None:
            return refuse(protocol.KIND_REQUEST, 400, "This node has no worker role")
        state.worker_info.rescan()
        return answer(
            {
                "worker": {
                    "gpus": state.worker_info.gpus(),
                    "gpu_checked_at": state.worker_info.gpu_checked_at,
                    "gpu_error": state.worker_info.gpu_error,
                }
            }
        )

    @bp.post(protocol.ROUTE_ACCESS_CHECK)
    def access_check() -> Any:
        # The roots come from the request, so on a node without the worker role (one beside Plex, maybe Windows with
        # its user's own rights) the route would only list folders for whoever holds the Plex database key.
        if state.worker_info is None:
            return refuse(protocol.KIND_REQUEST, 400, "This node has no worker role")
        body = json_body()
        if body is None:
            return refuse(protocol.KIND_REQUEST, 400, "The request body must be a JSON object")
        try:
            mappings = tasks.parse_mappings(body.get("mapping"))
        except tasks.TaskSpecError as exc:
            return refuse(protocol.KIND_REQUEST, 400, str(exc))
        files = body.get("files")
        if not isinstance(files, list) or len(files) > ACCESS_CHECK_MAX_FILES:
            return refuse(protocol.KIND_REQUEST, 400, f"files must be a list of at most {ACCESS_CHECK_MAX_FILES}")
        results: list[dict[str, Any]] = []
        nearby: dict[str, list[str]] = {}
        for entry in files:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
                return refuse(protocol.KIND_REQUEST, 400, "each file needs a path")
            try:
                size = tasks.optional_int(entry, "size")
                mtime_ns = tasks.optional_int(entry, "mtime_ns")
            except tasks.TaskSpecError as exc:
                return refuse(protocol.KIND_REQUEST, 400, str(exc))
            try:
                node_path = access.resolve(entry["path"], size, mtime_ns, mappings)
            except access.NotVisible as exc:
                results.append(
                    {
                        "path": entry["path"],
                        "ok": False,
                        "node_path": exc.node_path,
                        "kind": exc.reason,
                        "message": exc.message,
                    }  # fmt: skip
                )
                if exc.reason == protocol.NOT_VISIBLE_MISSING and exc.node_path:
                    found = access.nearby(exc.node_path, mappings)
                    if found is not None:
                        nearby.setdefault(found[0], found[1])
                continue
            results.append({"path": entry["path"], "ok": True, "node_path": node_path, "kind": None, "message": ""})
        return answer({"results": results, "nearby": nearby})

    @bp.post(protocol.ROUTE_TASKS)
    def start_task() -> Any:
        body = json_body()
        if body is None:
            return refuse(protocol.KIND_REQUEST, 400, "The request body must be a JSON object")
        try:
            task_id = table.start(body)
        except tasks.TaskSpecError as exc:
            return refuse(protocol.KIND_REQUEST, 400, str(exc))
        except tasks.TaskDeletedError as exc:
            return refuse(protocol.KIND_TASK_DELETED, 409, str(exc))
        except tasks.TaskStartError as exc:
            return refuse(protocol.KIND_FAILED, 400, str(exc))
        except tasks.NodeStoppingError as exc:
            return refuse(protocol.KIND_NODE_STOPPING, 503, str(exc))
        return answer({"id": task_id}, 202)

    @bp.get(f"{protocol.ROUTE_TASKS}/<task_id>")
    def poll_task(task_id: str) -> Any:
        raw = request.args.get("log_from", "0").strip()
        log_from = protocol.whole_number(raw.lstrip("-"))
        if log_from is None:
            return refuse(protocol.KIND_REQUEST, 400, "log_from must be a whole number")
        try:
            polled = table.poll(task_id, 0 if raw.startswith("-") else log_from)
        except tasks.NodeStoppingError as exc:
            return refuse(protocol.KIND_NODE_STOPPING, 503, str(exc))
        if polled is None:
            return unknown_task()
        return answer(polled)

    @bp.post(f"{protocol.ROUTE_TASKS}/<task_id>/cancel")
    def cancel_task(task_id: str) -> Any:
        return answer() if table.cancel(task_id) else unknown_task()

    @bp.post(f"{protocol.ROUTE_TASKS}/<task_id>/pause")
    def pause_task(task_id: str) -> Any:
        return answer() if table.set_paused(task_id, True) else unknown_task()

    @bp.post(f"{protocol.ROUTE_TASKS}/<task_id>/resume")
    def resume_task(task_id: str) -> Any:
        return answer() if table.set_paused(task_id, False) else unknown_task()

    @bp.get(f"{protocol.ROUTE_TASKS}/<task_id>/artifact")
    def task_artifact(task_id: str) -> Any:
        try:
            task = table.get(task_id)
        except tasks.NodeStoppingError as exc:
            return refuse(protocol.KIND_NODE_STOPPING, 503, str(exc))
        if task is None:
            return unknown_task()
        path = task.artifact
        if path is None or not os.path.isfile(path):
            return refuse(protocol.KIND_REQUEST, 404, "This task has no artifact")
        return send_file(path, mimetype="application/octet-stream", as_attachment=True,
                         download_name=os.path.basename(path), conditional=False)  # fmt: skip

    @bp.delete(f"{protocol.ROUTE_TASKS}/<task_id>")
    def delete_task(task_id: str) -> Any:
        return answer() if table.delete(task_id) else unknown_task()

    @bp.get(protocol.ROUTE_LOG)
    def node_log() -> Any:
        lines = protocol.whole_number(request.args.get("lines", str(LOG_DEFAULT_LINES)))
        if lines is None:
            return refuse(protocol.KIND_REQUEST, 400, "lines must be a whole number")
        count = max(1, min(lines, nodelog.NODE_LOG_LINES))
        return answer({"lines": state.log.lines(count)})

    return bp
