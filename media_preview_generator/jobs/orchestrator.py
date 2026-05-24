"""Core processing workflow for video preview generation.

Contains run_processing() which orchestrates Plex library scanning,
media item dispatch, and worker pool management.  Used exclusively
by the web layer (job_runner.py).
"""

import os
import random
import shutil

from loguru import logger

from ..processing.generator import ProcessingResult, clear_failures, log_failure_summary
from ..servers.ownership import apply_path_mappings, apply_webhook_prefixes, find_owning_servers
from .worker import WorkerPool


# Max cadence for worker-snapshot SocketIO emits during a multi-server
# dispatch. See the long comment in ``_dispatch_processable_items`` —
def _resolve_webhook_path_to_canonical(
    path: str, server_configs: list, *, log_resolution: bool = True
) -> tuple[str, list]:
    """Resolve a webhook-source path to a canonical server-view path + its owners.

    Sonarr/Radarr emit paths in their own view (e.g. ``/data/TV Shows/X.mkv``)
    which won't match a server's library ``remote_paths`` (e.g.
    ``/data_16tb/TV Shows``) until translated through the server's
    ``path_mappings`` ``webhook_prefixes`` list. Calling
    :func:`find_owning_servers` with the raw webhook path silently
    misses every install where the webhook source and the media-
    server use different mount roots — including the downstream
    ownership check inside ``process_canonical_path``, which uses the
    canonical path stored on the :class:`ProcessableItem` directly.

    Returns ``(canonical_path, matches)``:

    * ``canonical_path`` — the path form to store on the
      ``ProcessableItem``. Picked from the candidates by preferring,
      in order: (1) a candidate that exists on disk via
      :func:`os.path.exists` so frame extraction can read the source,
      (2) any candidate that owners agree on, (3) the raw input.
    * ``matches`` — the **aggregated** deduplicated list of
      :class:`~servers.ownership.OwnershipMatch` across EVERY
      candidate. This is the audit-P2 fix: the previous version
      returned at the first matching candidate, silently dropping
      owners whose libraries matched a different candidate. On a
      heterogeneous-mount install (Plex on ``/data_16tb``, Emby on
      ``/em-media``, both with ``webhook_prefixes=['/data']``), the
      first match would return Plex only and Emby would never publish.
      Now both servers' owners are returned.

    Tries the raw path first (the dominant case for installs without
    webhook_prefixes mappings), then every translated candidate.

    ``log_resolution`` controls whether the per-path mapping breadcrumb
    (the INFO "webhook X → resolved Y" line and the fallback WARNING) is
    emitted. Count-only callers (the owning-servers summary breadcrumb)
    pass ``False`` so the detail isn't logged twice per path per job —
    the real dispatch pass logs it. When run on a job thread these lines
    are captured into the per-job log, surfacing path mappings in the UI.
    """
    if not path or not server_configs:
        return path, []

    candidate_paths: list[str] = [path]
    seen_candidates: set[str] = {path}
    for cfg in server_configs:
        # Two namespaces can arrive here, and each needs its own translator:
        #   * Sonarr/Radarr/Tdarr send their own view (``/data/...``) →
        #     apply_webhook_prefixes maps the configured webhook_prefixes to
        #     local.
        #   * A Plex/Emby/Jellyfin ``library.new`` webhook resolves the file
        #     via the server's API, so the path arrives in the MEDIA-SERVER's
        #     own view (``/mnt/Media/...``) → apply_path_mappings does the
        #     same remote→local translation we already apply to a library's
        #     remote_paths. Without this, an install with a path mapping
        #     never matched its own server's webhooks (issue #254): the
        #     ownership check translated the library prefix to ``/media`` but
        #     compared it against the untranslated ``/mnt/Media`` path.
        for translator in (apply_webhook_prefixes, apply_path_mappings):
            for translated in translator(path, cfg.path_mappings or []):
                if translated not in seen_candidates:
                    seen_candidates.add(translated)
                    candidate_paths.append(translated)

    # Aggregate owners across ALL candidates that match. Track which
    # candidate each owner came from so we can pick a canonical path
    # form that at least one owner agrees on.
    seen_servers: set[str] = set()
    aggregated: list = []
    matching_candidates: list[str] = []
    for candidate in candidate_paths:
        owners = find_owning_servers(candidate, server_configs)
        if not owners:
            continue
        if candidate not in matching_candidates:
            matching_candidates.append(candidate)
        for match in owners:
            if match.server_id in seen_servers:
                continue
            seen_servers.add(match.server_id)
            aggregated.append(match)

    if not aggregated:
        return path, []

    # Canonical-path picker:
    #   1. If a matching candidate exists on disk, pick that. Frame
    #      extraction reads from this path, so on multi-disk installs
    #      (file lives on /data_16tb2 but /data_16tb is the first
    #      mapping) we MUST pick the disk that actually has the file
    #      or FFmpeg fails with "no such file or directory".
    #   2. Otherwise pick the first candidate any owner matched. Beats
    #      the raw input because the raw is usually the source-side
    #      view (Sonarr's /data/...) which the publishers' downstream
    #      ownership check (registry.find_owning_servers) doesn't
    #      translate.
    canonical: str | None = None
    for cand in matching_candidates:
        try:
            if os.path.exists(cand):
                canonical = cand
                break
        except OSError:
            # Defensive: a malformed path (super long, weird chars)
            # could raise on some filesystems. Skip it.
            continue
    if canonical is None:
        # Owners matched, so the mapping is correct — yet none of the mapped
        # disks hold the file. That's the stale-bind-mount / unmounted-volume
        # signature (or a still-copying file), NOT a mapping typo. Log the
        # full candidate set so the operator can see we probed every backing
        # disk before falling back; "missing on every mapped disk for a file
        # that's clearly on the host" points straight at a container mount
        # problem. See project_stale_bindmount_missing_on_disk.
        canonical = matching_candidates[0]
        if log_resolution:
            logger.warning(
                "Path mapping: webhook {!r} matched {}'s library but exists on NONE of the mapped "
                "disks — checked {}. Falling back to {!r}. If the file is clearly present on the host, "
                "the media volume may not be mounted inside this container "
                "(stale bind-mount / unmounted volume).",
                path,
                _owner_label(aggregated, server_configs),
                ", ".join(matching_candidates),
                canonical,
            )
    elif log_resolution and canonical != path:
        logger.info(
            "Path mapping: webhook {!r} → resolved {!r} (owner: {})",
            path,
            canonical,
            _owner_label(aggregated, server_configs),
        )
    else:
        logger.debug("Webhook path resolved: {!r} → {!r}", path, canonical)
    return canonical, aggregated


def _owner_label(matches: list, server_configs: list) -> str:
    """Human-readable owner names for a mapping breadcrumb.

    Prefers the configured server ``name`` over the opaque ``server_id``
    so the per-job log reads "Plex" instead of a 32-char hex id.
    """
    name_by_id = {cfg.id: (cfg.name or cfg.id) for cfg in server_configs}
    names = sorted({name_by_id.get(m.server_id, m.server_id) for m in matches})
    return ", ".join(names) if names else "no server"


def _outcome_for_multi_server_status(status) -> ProcessingResult:
    """Map a :class:`MultiServerStatus` to the legacy ProcessingResult.

    Mirrors ``Worker._record_outcome`` so the multi-server dispatch path
    (which bypasses :class:`Worker` and calls ``process_canonical_path``
    directly) can persist file-result rows with the same outcome strings
    the Files panel filters on. Without this the multi-server scan path
    skipped ``record_file_result`` entirely and the panel stayed empty
    for the duration of the run.
    """
    from ..processing.multi_server import MultiServerStatus

    if status is MultiServerStatus.PUBLISHED:
        return ProcessingResult.GENERATED
    if status is MultiServerStatus.SKIPPED:
        return ProcessingResult.SKIPPED_BIF_EXISTS
    if status is MultiServerStatus.SKIPPED_NOT_INDEXED:
        return ProcessingResult.SKIPPED_NOT_INDEXED
    if status is MultiServerStatus.SKIPPED_FILE_NOT_FOUND:
        return ProcessingResult.SKIPPED_FILE_NOT_FOUND
    if status is MultiServerStatus.NO_OWNERS:
        return ProcessingResult.NO_MEDIA_PARTS
    return ProcessingResult.FAILED


def _publisher_rows_from_result(result, canonical_path: str) -> list[dict]:
    """Flatten a MultiServerResult into wire-friendly publisher rows for Job UI.

    Persisted on Job.publishers so the dashboard can render
    "this file: Plex ✓, Emby ✗" without re-grepping the log stream.
    Also looks up the server type from media_servers so the badge
    palette matches.
    """
    rows = []
    type_by_id: dict[str, str] = {}
    try:
        from ..web.settings_manager import get_settings_manager

        for entry in get_settings_manager().get("media_servers") or []:
            if isinstance(entry, dict) and entry.get("id"):
                type_by_id[str(entry["id"])] = (entry.get("type") or "").lower()
    except Exception:
        pass
    for pub in (result.publishers or []) if result is not None else []:
        status = pub.status.value if hasattr(pub.status, "value") else str(pub.status)
        rows.append(
            {
                "server_id": pub.server_id,
                "server_name": pub.server_name,
                "server_type": type_by_id.get(str(pub.server_id), ""),
                "adapter_name": pub.adapter_name,
                "status": status,
                "message": pub.message or "",
                "canonical_path": canonical_path,
                # Frame provenance ("extracted" | "cache_hit" | "output_existed")
                # so the Job UI can render a distinct badge when frames were
                # reused across a sibling-server webhook.
                "frame_source": getattr(pub, "frame_source", "extracted"),
                # output_paths feeds the BIF-viewer deep-link in the Files
                # panel — see record_file_result + job_modal.js (D34).
                "output_paths": [str(op) for op in (getattr(pub, "output_paths", None) or [])],
            }
        )
    return rows


# Per-(server, path) precedence for picking the "most informative"
# publisher status across all attempts in a retry chain. Lower rank wins.
#
# Background — the chain head's publishers_json was originally written
# using the LATEST status per path on the assumption that a retry's
# outcome always supersedes the head's. That assumption is correct for
# Jellyfin's ``pending_registration → skipped_output_exists`` upgrade
# (bridge plugin registered the row — the file is now fully indexed,
# adapter gate at ``multi_server.py::_publish_all`` line ~1411
# distinguishes ``needs_registration``-still-true from done), but it
# silently overwrote Plex/Emby's ``published`` with the retry's no-op
# ``skipped_output_exists`` for every chain that hit a retry. The
# user-visible bug: jobs that freshly generated previews showed
# "already existed" everywhere in the modal's Servers strip.
#
# Cases preserved by the table:
#   * Plex/Emby ``published → skipped_output_exists`` keeps
#     ``published``. The retry's "BIF exists" observation is a no-op.
#   * Jellyfin ``published_pending_registration → skipped_output_exists``
#     keeps ``skipped_output_exists``. The adapter only emits
#     SKIPPED_OUTPUT_EXISTS for a JF retry once ``item_id`` resolves
#     (i.e., the row IS registered) — so the upgrade is structurally
#     guarded at the source, not just at the merge.
#   * ``failed → published`` (retry recovered) keeps ``published``.
#
# One case the table intentionally hides (with rationale):
#   * ``published → failed`` would keep ``published``, hiding a late
#     failure. This is structurally impossible in the current retry
#     pipeline: retries fire ONLY when at least one publisher returned
#     a PENDING status (see ``retry_queue.PENDING_PUBLISHER_STATUSES``).
#     A ``published`` server's row is never re-evaluated by the retry
#     in a way that could downgrade it to FAILED — at worst the retry
#     re-publishes (status=published again) or sees the BIF on disk
#     (status=skipped_output_exists). If a future refactor lets a retry
#     re-emit FAILED for an already-published server, demote ``published``
#     below ``failed`` in this table. The matching regression test is
#     ``test_published_then_failed_keeps_published_documents_assumption``.
_PUBLISHER_STATUS_PRECEDENCE: dict[str, int] = {
    "published": 0,
    "skipped_output_exists": 1,
    "published_pending_registration": 2,
    "skipped_not_indexed": 3,
    "skipped_not_in_library": 4,
    "failed": 5,
}

# Publisher statuses whose frame_source actually describes a frame outcome
# (Generated=extracted / Reused=cache_hit / Already-Existed=output_existed).
# Every PublisherResult carries a default ``frame_source="extracted"`` even for
# not-indexed / no-owners / no-frames skips, so the per-server frame_sources
# tally MUST gate on status — otherwise a not-indexed skip is mislabeled as
# "Generated". Both the live fold (fold_publisher_rows_into_aggregate) and the
# completed-chain merge gate on this set so the two views stay in lock-step.
_FRAME_PROVENANCE_STATUSES: frozenset[str] = frozenset(
    {"published", "published_pending_registration", "skipped_output_exists"}
)


def _best_publisher_status(statuses: list[str]) -> str:
    """Return the most informative status from a list of attempts.

    Unknown statuses sort to the end (rank 99) so they only win if no
    known status was observed. The list is expected to be non-empty;
    callers should skip empty lists before calling.
    """
    return min(statuses, key=lambda s: _PUBLISHER_STATUS_PRECEDENCE.get(s, 99))


def merge_chain_publishers_best_per_path(file_results: list[dict]) -> list[dict]:
    """Aggregate per-(server, path) publisher outcomes across a chain's attempts.

    ``file_results`` is the JSONL of dispatches recorded by ``_file_result_cb``
    for the chain's head and every retry child — typically obtained from
    ``JobManager.get_file_results(chain_head_id, dedup_by_path=False)``.

    Returns the publisher rows in the same shape as
    ``fold_publisher_rows_into_aggregate`` produces (id/name/type/counts plus
    a per-server ``frame_sources`` tally), but with one row per (server, path)
    folded using the ``_PUBLISHER_STATUS_PRECEDENCE`` "best wins" rule rather
    than the original "latest dedup wins". See the module-level constant
    docstring for the bug this fixes.
    """
    # (server_id, path) → list of (status, frame_source) in observation order
    per_server_path: dict[tuple[str, str], list[tuple[str, str]]] = {}
    # server_id → metadata for the result rows (name / type)
    server_meta: dict[str, dict] = {}

    for fr in file_results:
        if not isinstance(fr, dict):
            continue
        path = fr.get("file") or fr.get("canonical_path") or fr.get("path") or ""
        for s in fr.get("servers") or []:
            if not isinstance(s, dict):
                continue
            sid = s.get("id") or s.get("server_id") or ""
            if not sid:
                continue
            status = s.get("status") or ""
            if not status:
                continue
            per_server_path.setdefault((sid, path), []).append((status, s.get("frame_source") or ""))
            # Late-arriving name/type wins over an empty one (matches
            # fold_publisher_rows_into_aggregate's protection against
            # the first row carrying only an id).
            meta = server_meta.setdefault(sid, {"server_name": "", "server_type": ""})
            if not meta["server_name"]:
                meta["server_name"] = s.get("name") or s.get("server_name") or ""
            if not meta["server_type"]:
                meta["server_type"] = (s.get("type") or s.get("server_type") or "").lower()

    aggregate: dict[str, dict] = {}
    for (sid, _path), observations in per_server_path.items():
        statuses = [st for st, _ in observations]
        best = _best_publisher_status(statuses)
        entry = aggregate.setdefault(
            sid,
            {
                "server_id": sid,
                "server_name": server_meta.get(sid, {}).get("server_name", ""),
                "server_type": server_meta.get(sid, {}).get("server_type", ""),
                "counts": {},
            },
        )
        entry["counts"][best] = entry["counts"].get(best, 0) + 1
        # Frame provenance for the winning outcome, mirroring
        # fold_publisher_rows_into_aggregate (same _FRAME_PROVENANCE_STATUSES
        # gate) so the per-server Generated/Reused/Already-Existed breakdown
        # survives a *completed* retry chain identically to the live run. Slim
        # file_result rows omit frame_source when it's the "extracted" default
        # (jobs.py:2242), so a published row with no recorded source is treated
        # as extracted.
        if best in _FRAME_PROVENANCE_STATUSES:
            best_fs = next((fs for st, fs in observations if st == best and fs), "")
            if not best_fs and best in ("published", "published_pending_registration"):
                best_fs = "extracted"
            if best_fs:
                frame_sources = entry.setdefault("frame_sources", {})
                frame_sources[best_fs] = frame_sources.get(best_fs, 0) + 1

    return list(aggregate.values())


def fold_publisher_rows_into_aggregate(aggregate: dict[str, dict], rows: list[dict]) -> None:
    """Fold per-task publisher rows into a per-server count aggregate.

    ``aggregate`` is keyed by ``server_id`` and shaped::

        {server_id: {"server_id": ..., "server_name": ...,
                     "server_type": ..., "counts": {status: count}}}

    Mutates ``aggregate`` in place. Both job-dispatch paths (legacy
    WorkerPool dispatcher and the multi-server full-scan / webhook
    ThreadPoolExecutor) feed this so they cannot drift again — commit
    1ecf099 ("aggregate per-server, not per-file") patched only the
    dispatcher path. ``_dispatch_processable_items`` was missed and
    kept ``append_publishers``-ing one row per (file × server), which
    on a 117k-item full library scan turned into an O(N²) SQLite write
    storm (publishers_json grew to 11.8 MB and was re-encoded + UPSERTed
    after every item, dropping throughput from ~30 items/sec early on
    to <8 items/sec by minute 28).
    """
    for row in rows:
        server_id = row.get("server_id") or ""
        if not server_id:
            continue
        entry = aggregate.get(server_id)
        if entry is None:
            entry = {
                "server_id": server_id,
                "server_name": row.get("server_name") or "",
                "server_type": (row.get("server_type") or "").lower(),
                "counts": {},
            }
            aggregate[server_id] = entry
        else:
            # Late-arriving name/type wins over an empty one — protects
            # against the first row for a server having only the id
            # (e.g. before settings_manager has loaded the display name).
            if not entry.get("server_name") and row.get("server_name"):
                entry["server_name"] = row["server_name"]
            if not entry.get("server_type") and row.get("server_type"):
                entry["server_type"] = row["server_type"].lower()
        status = row.get("status") or "unknown"
        entry["counts"][status] = entry["counts"].get(status, 0) + 1
        # Per-server frame provenance, additive alongside the status counts so
        # existing consumers of ``counts`` are untouched. Lets the Job UI show
        # each server's "Generated (extracted) / Reused (cache_hit) / Already
        # existed (output_existed)" split. Gated on _FRAME_PROVENANCE_STATUSES:
        # a not-indexed / no-owners skip carries a default "extracted" stamp
        # that does NOT mean frames were produced, so it must not be tallied.
        fs = row.get("frame_source")
        if fs and status in _FRAME_PROVENANCE_STATUSES:
            frame_sources = entry.setdefault("frame_sources", {})
            frame_sources[fs] = frame_sources.get(fs, 0) + 1


def _log_webhook_owning_servers(config, paths: list[str]) -> None:
    """Log a one-line summary of which configured servers own the webhook paths.

    Best-effort: any failure resolving ownership is swallowed so a logging
    bug never blocks the actual dispatch. Used purely as a breadcrumb so
    the operator can read the log top-down and see, before any per-server
    work runs, *which* servers will be touched and how many paths each
    owns. Without this line the legacy single-Plex resolver path looks
    indistinguishable from the multi-server fan-out path.
    """
    try:
        from ..servers.registry import server_config_from_dict
        from ..web.settings_manager import get_settings_manager

        raw = get_settings_manager().get("media_servers") or []
        configs = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled") is False:
                continue
            try:
                configs.append(server_config_from_dict(entry))
            except Exception:
                continue

        if not configs:
            logger.info(
                "Resolving {} webhook path(s) — no media servers configured yet, skipping ownership lookup.",
                len(paths),
            )
            return

        name_by_id = {cfg.id: (cfg.name or cfg.id) for cfg in configs}
        owners_by_server: dict[str, int] = {}
        unowned = 0
        for path in paths:
            _canonical, uniq_matches = _resolve_webhook_path_to_canonical(path, configs, log_resolution=False)
            if not uniq_matches:
                unowned += 1
                continue
            for match in uniq_matches:
                key = name_by_id.get(match.server_id, match.server_id)
                owners_by_server[key] = owners_by_server.get(key, 0) + 1

        if not owners_by_server:
            logger.info(
                "Resolving {} webhook path(s) — none match any configured server's enabled libraries yet "
                "(retry queue will keep trying).",
                len(paths),
            )
            return

        ordered = ", ".join(f"{name} ({count} path(s))" for name, count in owners_by_server.items())
        pinned = getattr(config, "server_id_filter", None)
        scope_note = f" (pinned to server_id={pinned!r})" if pinned else ""
        suffix = f"; {unowned} path(s) unowned" if unowned else ""
        logger.info(
            "Resolving {} webhook path(s) across owning server(s): {}{}{}",
            len(paths),
            ordered,
            scope_note,
            suffix,
        )
    except Exception as exc:  # never block dispatch on a logging failure
        logger.debug("owning-servers breadcrumb skipped: {}", exc)


def _enumerate_plex_full_scan_items(
    config,
    registry,
    *,
    cancel_check=None,
    progress_callback=None,
):
    """Yield :class:`ProcessableItem` for the Plex full-library scan flow.

    Pulled out as a module-level function so tests can patch this single
    boundary instead of stubbing PlexProcessor + ServerRegistry +
    get_processor_for separately. Production code in ``run_processing``
    invokes this exactly once per Plex full-scan dispatch.
    """
    from ..processing import get_processor_for
    from ..servers.base import ServerType

    # Defence in depth for issue #244: the gate at
    # :func:`_should_use_multi_server_full_scan` routes 2+ enabled Plex
    # installs to the multi-server path, so reaching this site with more
    # than one enabled Plex would itself be the bug. Filter on ``enabled``
    # here too so a "[disabled Plex-A, enabled Plex-B]" layout doesn't
    # connect to the disabled one when ``media_servers[0]`` happens to be
    # disabled — the user's intent is the enabled server.
    plex_cfg = next((c for c in registry.configs() if c.type is ServerType.PLEX and c.enabled), None)
    if plex_cfg is None:
        return
    plex_processor = get_processor_for(ServerType.PLEX)
    library_ids = list(getattr(config, "plex_library_ids", None) or []) or None
    # Mirror the Emby/Jellyfin multi-server enumerator's "Querying…"
    # banner (see ``_enumerate_items_for_servers``). A full Plex library
    # enumeration on a large TV library can take a while before the
    # first item is yielded via ``list_canonical_paths``; without this
    # the progress bar sits at "0/0" with no message and the job looks
    # frozen — live user report on job 90301a18. Log at INFO so it
    # lands in the per-job log file too (the UI's Job Detail tab reads
    # from that file, not the progress_callback stream).
    _label = plex_cfg.name or plex_cfg.id or plex_cfg.type.value
    logger.info("Querying {} library… (can take a while for large libraries)", _label)
    if progress_callback is not None:
        try:
            progress_callback(0, 0, f"Querying {_label} library…")
        except Exception as exc:
            logger.debug("progress_callback raised during Plex enumeration banner: {}", exc)
    yield from plex_processor.list_canonical_paths(
        plex_cfg,
        library_ids=library_ids,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )


def _dispatch_processable_items(
    items,
    *,
    config,
    registry,
    selected_gpus,
    progress_callback=None,
    cancel_check=None,
    pause_check=None,
    job_id: str | None = None,
    label: str = "scan",
    server_id_filter: str | None = None,
    worker_callback=None,
    on_dispatch_start=None,
    worker_pool_callback=None,
) -> dict:
    """Submit ``(server_config, ProcessableItem)`` pairs to the shared dispatcher.

    This used to be a second, parallel execution engine (its own
    ThreadPoolExecutor + slot pool + hot-reload poller). It now feeds the one
    shared :class:`JobDispatcher` — the same engine webhooks and single-Plex
    scans use — so there is a SINGLE worker model with the checking/processing
    split for every trigger and every vendor, and a scan + an incoming webhook
    share one capped pool instead of oversubscribing two.

    ``items`` is ``[(server_config, ProcessableItem), ...]``; only the
    ProcessableItem is forwarded — the per-item publish pin is resolved from
    ``config.server_id_filter`` + ``item.server_id`` inside the worker /
    checking stage (:func:`jobs.worker.resolve_per_item_pin`), equivalent to
    the old per-tuple ``server_cfg.type`` logic since ``item.server_id ==
    server_cfg.id`` for enumerated items. ``server_id_filter`` is accepted for
    signature compatibility and is already carried on ``config``.

    Returns the tracker's per-file ProcessingResult outcome counts
    (``generated`` / ``skipped_bif_exists`` / …) — the same per-item scheme the
    webhook + single-Plex path already produces and every consumer already
    reads.
    """
    import uuid

    from ..web.jobs import PRIORITY_NORMAL
    from .dispatcher import get_dispatcher
    from .worker import WorkerPool

    empty = {r.value: 0 for r in ProcessingResult}
    plain_items = [item for (_server_cfg, item) in items]
    total = len(plain_items)
    if not plain_items:
        return empty

    # Reuse the shared dispatcher's pool when one already exists (e.g. a
    # concurrent webhook created it); otherwise build one sized from the
    # user's GPU/CPU worker config + selected GPUs (mirrors the legacy
    # ``_create_worker_pool`` in run_processing).
    existing = get_dispatcher()
    if existing is not None:
        worker_pool = existing.worker_pool
        logger.info(
            "Multi-server {}: reusing shared worker pool ({} worker(s))",
            label,
            len(worker_pool._snapshot_workers()),
        )
    else:
        sel = list(selected_gpus or [])
        # ``config.gpu_threads`` can be > 0 while no GPU is actually
        # selected/detected (a configured GPU that's currently absent), and
        # WorkerPool raises if asked for GPU workers with no devices. Clamp to
        # 0 → CPU-only, never crash the whole dispatch. With GPUs present this
        # is a no-op (in production gpu_threads and selected_gpus both derive
        # from gpu_config, so they agree).
        worker_pool = WorkerPool(
            gpu_workers=int(getattr(config, "gpu_threads", 0) or 0) if sel else 0,
            cpu_workers=int(getattr(config, "cpu_threads", 0) or 0),
            selected_gpus=sel,
        )
    dispatcher = get_dispatcher(worker_pool)

    # Reconcile the (possibly reused/stale) pool to the current GPU config —
    # the same hook the webhook/single-Plex path uses so worker counts track
    # settings. Replaces Engine B's bespoke 1.5s poller.
    if worker_pool_callback:
        try:
            worker_pool_callback(worker_pool)
        except Exception as exc:
            logger.debug("worker_pool_callback raised during {} dispatch: {}", label, exc)

    if on_dispatch_start:
        try:
            on_dispatch_start()
        except Exception as exc:
            logger.debug("on_dispatch_start raised: {}", exc)

    if progress_callback:
        try:
            progress_callback(0, total, f"Dispatching {total} item(s)…")
        except Exception as exc:
            logger.debug("progress_callback raised on dispatch banner: {}", exc)

    # A real job always carries a job_id; enumeration helpers occasionally
    # call with None (no Job row). Use a unique synthetic key so two such runs
    # can't collide on the singleton dispatcher's tracker map.
    effective_job_id = job_id or f"{label.replace(' ', '-')}-{uuid.uuid4().hex[:8]}"

    tracker = dispatcher.submit_items(
        job_id=effective_job_id,
        items=plain_items,
        config=config,
        registry=registry,
        title_max_width=200,
        library_name="",
        callbacks={
            "progress_callback": progress_callback,
            "worker_callback": worker_callback,
            "cancel_check": cancel_check,
            "pause_check": pause_check,
        },
        priority=PRIORITY_NORMAL,
    )
    tracker.wait()
    logger.info("Multi-server {} complete: {} item(s) processed.", label, tracker.completed)
    return tracker.get_result()["outcome"]


def _enumerate_items_for_servers(
    candidates,
    *,
    enumerate_one,
    cancel_check=None,
    label: str,
    progress_callback=None,
):
    """Walk every server in ``candidates`` and collect the items each yields.

    Shared by :func:`_run_full_scan_multi_server` and
    :func:`_run_recently_added_multi_server` — both walk the same list of
    candidate :class:`ServerConfig` objects, look up the right
    :class:`VendorProcessor`, and dispatch to *some* enumeration method
    on it. ``enumerate_one(processor, server_cfg) -> Iterator[ProcessableItem]``
    captures the only thing that actually differs between the two callers
    (``processor.list_canonical_paths`` vs ``processor.scan_recently_added``).

    Returns ``(all_items, enumeration_errors)``:
    * ``all_items`` — list of ``(server_config, ProcessableItem)`` ready
      to feed into :func:`_dispatch_processable_items`.
    * ``enumeration_errors`` — list of ``(server_label, "ExcName: msg")``
      tuples, one per server whose enumeration raised. Callers use
      this to distinguish "library was empty" (zero items, no errors —
      green badge fine) from "library couldn't be reached" (zero items
      AND error logged — should surface as a job-level warning so the
      user sees the amber badge instead of a misleading green check).
      Job b6deeac3 was the originating regression: Jellyfin's /Items
      timed out, the library was skipped, and the job reported
      "completed" with no indication anything went wrong.

    De-duping across servers (Phase P4) lives in this helper so it
    applies uniformly to full-scan AND recently-added flows.
    """
    from ..processing import get_processor_for

    all_items: list = []
    enumeration_errors: list[tuple[str, str]] = []
    by_canonical: dict[str, int] = {}  # canonical_path → index in all_items

    for server_cfg in candidates:
        try:
            processor = get_processor_for(server_cfg.type)
        except KeyError as exc:
            logger.warning(
                "No processor registered for {!r} ({}). Skipping this server.",
                server_cfg.type,
                exc,
            )
            enumeration_errors.append((server_cfg.name or server_cfg.id or server_cfg.type.value, f"KeyError: {exc}"))
            continue
        if cancel_check and cancel_check():
            logger.info("Cancellation requested while enumerating items — aborting {}.", label)
            return all_items, enumeration_errors
        # Surface a "Querying…" status BEFORE the per-server walk so the UI
        # progress bar shows the system is alive during the slow library
        # enumeration phase (Emby/Jellyfin TV libraries can take 10–60s
        # before the first item is yielded). Without this, the job sits at
        # "0/0" with no message and users assume it's stuck. Log at INFO
        # so it also lands in the per-job log file (the progress_callback
        # stream is in-memory only — the Job Detail log tab reads the
        # file that the loguru log_sink writes to).
        _server_label = server_cfg.name or server_cfg.id or server_cfg.type.value
        logger.info("Querying {} library… (can take 10–60s for large libraries)", _server_label)
        if progress_callback:
            try:
                progress_callback(0, 0, f"Querying {_server_label} library…")
            except Exception as exc:
                logger.debug("progress_callback raised during enumeration banner: {}", exc)
        try:
            for item in enumerate_one(processor, server_cfg):
                if cancel_check and cancel_check():
                    logger.info("Cancellation requested mid-enumeration — aborting {}.", label)
                    return all_items, enumeration_errors

                # Phase P4: when the same canonical_path appears on more
                # than one server (typical: Plex+Jellyfin sharing media,
                # or two Plex servers with shared storage), keep ONE
                # ProcessableItem and merge every server's vendor item-id
                # hint into it. The publish-side fan-out (_resolve_publishers)
                # already targets every owning server; deduping here just
                # avoids dispatching the same path twice.
                existing_index = by_canonical.get(item.canonical_path)
                if existing_index is None:
                    by_canonical[item.canonical_path] = len(all_items)
                    all_items.append((server_cfg, item))
                else:
                    existing_cfg, existing_item = all_items[existing_index]
                    merged_hints = dict(existing_item.item_id_by_server or {})
                    merged_hints.update(item.item_id_by_server or {})
                    if merged_hints != (existing_item.item_id_by_server or {}):
                        from ..processing.types import ProcessableItem

                        all_items[existing_index] = (
                            existing_cfg,
                            ProcessableItem(
                                canonical_path=existing_item.canonical_path,
                                server_id=existing_item.server_id,
                                item_id_by_server=merged_hints,
                                title=existing_item.title or item.title,
                                library_id=existing_item.library_id,
                            ),
                        )
        except Exception as exc:
            logger.warning(
                "Enumeration on {} server {!r} failed ({}: {}). Continuing with the next server in scope.",
                server_cfg.type.value,
                server_cfg.name or server_cfg.id,
                type(exc).__name__,
                exc,
            )
            enumeration_errors.append(
                (
                    server_cfg.name or server_cfg.id or server_cfg.type.value,
                    f"{type(exc).__name__}: {exc}",
                )
            )

    return all_items, enumeration_errors


def _build_multi_server_registry(config):
    """Load the live :class:`ServerRegistry` for a multi-server scan/dispatch.

    Wraps the pair of ``settings_manager.get + ServerRegistry.from_settings``
    calls every multi-server entry point repeats and surfaces any failure
    as a warning + ``None`` so callers can ``return zero counts`` early.
    """
    from ..servers import ServerRegistry
    from ..web.settings_manager import get_settings_manager

    try:
        raw_servers = list(get_settings_manager().get("media_servers") or [])
    except Exception as exc:
        logger.warning(
            "Could not read media_servers when running multi-server scan ({}: {}). "
            "Open the Servers page and verify at least one enabled server is configured.",
            type(exc).__name__,
            exc,
        )
        return None
    try:
        return ServerRegistry.from_settings(raw_servers, legacy_config=config)
    except Exception as exc:
        logger.warning(
            "Could not build the media-server registry for multi-server scan ({}: {}). "
            "Open the Servers page and verify each server has valid auth and a reachable URL.",
            type(exc).__name__,
            exc,
        )
        return None


def _run_full_scan_multi_server(
    config,
    *,
    selected_gpus,
    server_id_filter: str | None = None,
    library_ids: list[str] | None = None,
    progress_callback=None,
    cancel_check=None,
    pause_check=None,
    job_id: str | None = None,
    worker_callback=None,
    on_dispatch_start=None,
    worker_pool_callback=None,
    warnings_out: list[str] | None = None,
) -> dict:
    """Multi-server full-library scan via the per-vendor :class:`VendorProcessor`.

    Walks every enabled server (or just ``server_id_filter`` when set) using
    the right :class:`VendorProcessor` from the registry, then dispatches each
    enumerated :class:`ProcessableItem` through ``process_canonical_path`` in
    parallel via a :class:`ThreadPoolExecutor`. Workers are sized off the
    user's GPU/CPU configuration and items are distributed across GPUs
    round-robin so a single GPU isn't oversubscribed.

    All vendors (Plex, Emby, Jellyfin) flow through this same path now —
    no separate legacy worker pool. The unified :func:`process_canonical_path`
    handles publish-to-every-owner fan-out so a Plex+Jellyfin install
    publishes both bundles from a single FFmpeg pass.

    Returns the aggregated ProcessingResult counts keyed by enum value
    (same shape as :func:`_dispatch_webhook_paths_multi_server`).

    ``warnings_out``: optional list the caller passes in to collect
    user-visible warning strings. The function appends one string per
    server whose enumeration failed AND ended up contributing zero
    items to the scan — these surface in the job UI's amber-badge
    "completed with warning" state via ``complete_job(warning=...)``.
    Job b6deeac3 reproduced the silent-green-badge regression this
    out-parameter addresses: Jellyfin's /Items timed out, the library
    was skipped, and the job ended as "completed successfully" with
    zero items processed.
    """
    counts = {r.value: 0 for r in ProcessingResult}

    registry = _build_multi_server_registry(config)
    if registry is None:
        return counts

    candidates = [
        cfg for cfg in registry.configs() if cfg.enabled and (not server_id_filter or cfg.id == server_id_filter)
    ]
    if not candidates:
        logger.warning(
            "No enabled servers matched the multi-server scan request (server_id_filter={!r}). Nothing to process.",
            server_id_filter,
        )
        return counts

    all_items, enumeration_errors = _enumerate_items_for_servers(
        candidates,
        enumerate_one=lambda processor, server_cfg: processor.list_canonical_paths(
            server_cfg,
            library_ids=library_ids,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        ),
        cancel_check=cancel_check,
        label="full scan",
        progress_callback=progress_callback,
    )

    # Surface enumeration failures regardless of whether any items
    # came through from OTHER servers. The 2×2 matrix is
    # ``{items=0, items>0} × {errors=0, errors>0}``. The cell
    # ``items>0 AND errors>0`` (e.g. Plex enumerated fine, Jellyfin
    # timed out) is the multi-server analogue of job b6deeac3 — the
    # job processes some files but silently drops every Jellyfin
    # path. Without this hoist the badge stays green and the user
    # has no signal that Jellyfin's catalogue was missed. The
    # legitimate-empty-library case (no enumeration_errors at all)
    # still falls through to plain green.
    if warnings_out is not None and enumeration_errors:
        servers_with_errors = ", ".join(name for name, _err in enumeration_errors)
        error_detail = "; ".join(f"{name}: {msg}" for name, msg in enumeration_errors)
        if all_items:
            warnings_out.append(
                f"Enumeration failed for {len(enumeration_errors)} server(s) "
                f"({servers_with_errors}) — these libraries were skipped but other "
                f"servers contributed items. {error_detail}. Retry the scan once "
                f"the server is healthy to pick up the missed catalogue."
            )
        else:
            warnings_out.append(
                f"Enumeration failed for {len(enumeration_errors)} server(s) "
                f"({servers_with_errors}) — zero items processed. {error_detail}. "
                f"Retry the scan once the server is healthy."
            )

    if not all_items:
        # Was INFO. WARN it: a "successful" scan that processed nothing is
        # the worst-of-both — the job UI shows green, but the user wonders why
        # no previews appeared. Common real causes: a stale library_id (vendor
        # renamed/recreated the library), an auth token scoped away from the
        # library, vendor's background indexer still catching up, or a
        # library type that filters to no Movies/Episodes. Surface it loudly.
        logger.warning(
            "Multi-server scan walked {} server(s) for library_ids={!r} but found "
            "ZERO items to process. Common causes: (a) the library_ids you passed "
            "no longer match a library on the server (try Refresh libraries on the "
            "Servers page), (b) the auth token can't see this library, (c) the "
            "vendor hasn't finished its own library scan yet, or (d) the library "
            "contains no Movie/Episode items.",
            len(candidates),
            library_ids,
        )
        return counts

    return _dispatch_processable_items(
        all_items,
        config=config,
        registry=registry,
        selected_gpus=selected_gpus,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        pause_check=pause_check,
        job_id=job_id,
        label="full scan",
        server_id_filter=server_id_filter,
        worker_callback=worker_callback,
        on_dispatch_start=on_dispatch_start,
        worker_pool_callback=worker_pool_callback,
    )


def _run_recently_added_multi_server(
    config,
    *,
    selected_gpus,
    server_id_filter: str | None = None,
    library_ids: list[str] | None = None,
    lookback_hours: float = 1.0,
    progress_callback=None,
    cancel_check=None,
    pause_check=None,
    job_id: str | None = None,
    worker_callback=None,
    on_dispatch_start=None,
    worker_pool_callback=None,
    warnings_out: list[str] | None = None,
) -> dict:
    """Recently-added scan for any vendor via :class:`VendorProcessor`.

    Walks every enabled server (or just ``server_id_filter``) calling
    ``processor.scan_recently_added`` for each. Per-vendor processors
    handle the API differences (Plex's ``addedAt>>`` filter vs.
    Emby/Jellyfin's ``DateCreated`` sort) so the orchestrator stays
    vendor-agnostic.

    Returns the aggregated ProcessingResult counts. ``warnings_out``
    mirrors :func:`_run_full_scan_multi_server` — without this plumbing
    a Sonarr/Radarr-driven recently-added scan whose Jellyfin /Items
    times out would silently report "completed" with zero items, the
    same shape of bug as job b6deeac3.
    """
    counts = {r.value: 0 for r in ProcessingResult}

    registry = _build_multi_server_registry(config)
    if registry is None:
        return counts

    candidates = [
        cfg for cfg in registry.configs() if cfg.enabled and (not server_id_filter or cfg.id == server_id_filter)
    ]
    if not candidates:
        logger.warning(
            "No enabled servers matched the recently-added scan request (server_id_filter={!r}). Nothing to process.",
            server_id_filter,
        )
        return counts

    lookback_int = int(max(1, lookback_hours))
    all_items, enumeration_errors = _enumerate_items_for_servers(
        candidates,
        enumerate_one=lambda processor, server_cfg: processor.scan_recently_added(
            server_cfg,
            lookback_hours=lookback_int,
            library_ids=library_ids,
        ),
        cancel_check=cancel_check,
        label="recently-added scan",
        progress_callback=progress_callback,
    )

    # Same warning-on-enumeration-failure contract as the full-scan
    # path — partial-success and zero-success cells both surface a
    # warning when any server's enumeration raised.
    if warnings_out is not None and enumeration_errors:
        servers_with_errors = ", ".join(name for name, _err in enumeration_errors)
        error_detail = "; ".join(f"{name}: {msg}" for name, msg in enumeration_errors)
        if all_items:
            warnings_out.append(
                f"Recently-added enumeration failed for {len(enumeration_errors)} server(s) "
                f"({servers_with_errors}) — those libraries were skipped but other servers "
                f"contributed items. {error_detail}."
            )
        else:
            warnings_out.append(
                f"Recently-added enumeration failed for {len(enumeration_errors)} server(s) "
                f"({servers_with_errors}) — zero items processed. {error_detail}."
            )

    if not all_items:
        logger.info(
            "Recently-added scan walked {} server(s) but found no items in the lookback window ({}h).",
            len(candidates),
            lookback_hours,
        )
        return counts

    return _dispatch_processable_items(
        all_items,
        config=config,
        registry=registry,
        selected_gpus=selected_gpus,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        pause_check=pause_check,
        job_id=job_id,
        label="recently-added scan",
        server_id_filter=server_id_filter,
        worker_callback=worker_callback,
        on_dispatch_start=on_dispatch_start,
        worker_pool_callback=worker_pool_callback,
    )


def _resolve_pinned_server(sid_filter: str | None) -> tuple[dict | None, str]:
    """Look up the media_servers entry for ``sid_filter`` and return ``(entry, type)``.

    Returns ``(None, "")`` when ``sid_filter`` is unset, when settings can't
    be loaded, or when no entry matches. ``type`` is the lowercased server
    type string ("plex" / "emby" / "jellyfin" / ""). Used by the dispatch-
    mode selector to detect non-Plex pins.
    """
    if not (isinstance(sid_filter, str) and sid_filter):
        return None, ""
    try:
        from ..web.settings_manager import get_settings_manager

        raw = get_settings_manager().get("media_servers") or []
    except Exception:
        return None, ""
    pinned_entry = next((e for e in raw if isinstance(e, dict) and e.get("id") == sid_filter), None)
    pinned_type = ((pinned_entry or {}).get("type") or "").lower()
    return pinned_entry, pinned_type


def _should_use_multi_server_full_scan(config, pinned_type: str) -> bool:
    """Decide whether the full-library scan should go through the multi-server path.

    Use the multi-server scan when ANY of the following holds (and there are
    no webhook paths — the webhook flow has its own selector):

    * Pinned to a non-Plex server.
    * No Plex configured at all.
    * At least one non-Plex server (Emby / Jellyfin) is enabled.
    * Two or more enabled Plex servers are configured.

    The legacy Plex-only branch only fires for the pure single-Plex install.
    It enumerates exactly one Plex config (the first enabled match from
    ``registry.configs()``) and has no notion of ``server_id_filter`` —
    so on a 2-Plex install pinning to the second Plex would still scan
    the first, silently. Issue #244 (May 2026) was the live reproducer.
    """
    no_webhook_paths = not getattr(config, "webhook_paths", None)
    if not no_webhook_paths:
        return False
    non_plex_pin = bool(pinned_type) and pinned_type != "plex"
    no_plex_at_all = not (config.plex_url and config.plex_token)
    has_non_plex_server = False
    enabled_plex_count = 0
    try:
        from ..web.settings_manager import get_settings_manager

        raw = get_settings_manager().get("media_servers") or []
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("enabled", True):
                continue
            entry_type = (entry.get("type") or "").lower()
            if entry_type in ("emby", "jellyfin"):
                has_non_plex_server = True
            elif entry_type == "plex":
                enabled_plex_count += 1
    except Exception:
        has_non_plex_server = False
        enabled_plex_count = 0
    multi_plex = enabled_plex_count >= 2
    return non_plex_pin or no_plex_at_all or has_non_plex_server or multi_plex


def _maybe_log_path_mapping_misconfig(aggregate_outcome: dict, processed: int) -> bool:
    """Emit the path-mapping misconfiguration warning when the run looks broken.

    Returns ``True`` when the warning fired so callers and tests can assert on
    the exact predicate (every processed item finished as
    ``skipped_file_not_found`` and zero items were generated). Splitting this
    out lets the rule be unit-tested without exercising the entire
    ``run_processing`` pipeline; before the extraction the only test coverage
    re-implemented dictionary arithmetic in the test file and never ran the
    real predicate.
    """
    not_found = aggregate_outcome.get("skipped_file_not_found", 0)
    generated = aggregate_outcome.get("generated", 0)
    if processed > 0 and not_found > 0 and generated == 0:
        logger.warning(
            "All {} item(s) finished with the file not found locally — no previews were generated this run. "
            "This almost always means your path mappings are wrong: Plex reports the file at one path, but this "
            "app can't see it at that path. Open Settings → Path mappings and add a row that translates Plex's "
            "path to the local path this app sees. The Plex server itself is fine — only file access is broken.",
            not_found,
        )
        return True
    return False


def _format_outcome_summary(aggregate_outcome: dict) -> str:
    """Build the one-line "X generated, Y already existed, Z failed" string for the end-of-job log.

    Pure formatter — only counts that fired appear in the output, in a stable
    order. Returns the literal string ``"no items processed"`` when every
    counter is zero so the log line is never empty.
    """
    parts = []
    counters = (
        ("generated", "{n} generated"),
        ("skipped_bif_exists", "{n} already existed"),
        ("skipped_not_indexed", "{n} not indexed yet"),
        ("skipped_file_not_found", "{n} not found"),
        ("skipped_excluded", "{n} excluded"),
        ("skipped_invalid_hash", "{n} invalid hash"),
        ("failed", "{n} failed"),
        ("no_media_parts", "{n} no media parts"),
    )
    for key, template in counters:
        n = aggregate_outcome.get(key, 0)
        if n:
            parts.append(template.format(n=n))
    return ", ".join(parts) if parts else "no items processed"


def _build_path_mapping_mismatch_hints(unresolved_paths: list[str], server_configs: list) -> dict[str, str]:
    """Detect likely path-mapping mismatches and return per-path hints.

    Audit P4 fix — previously returned ``list[str]``; the consumer
    (``job_runner.py``) used ``hints[0]`` for every unresolved row, so
    a multi-path webhook with different mismatches showed the SAME
    hint on every row (often the wrong one). Returning a dict keyed
    by the originating path lets each row pick its own hint.

    For each unresolved webhook path, walks every configured server's
    library remote_paths and looks for a location that's a
    path-boundary substring of the webhook path — the fingerprint of
    "Sonarr/Radarr is reporting one prefix but the server stores
    another." When found, the dict entry contains a hint string the
    file_result row can show in place of the generic "Not found"
    message. When no library location is a substring of the path at all
    (the sender's mount root differs entirely — issue #254), the path
    still gets a generic fallback hint naming the received root and the
    "Path the webhook sends" field, so every unresolved path is keyed in
    the returned dict (none are left for the caller to handle generically).
    The only way a path is absent is if there are no configured library
    locations at all, in which case the function returns ``{}`` early.
    """
    if not unresolved_paths or not server_configs:
        return {}

    from ..plex_client import _mismatch_covered_by_mappings

    locations: list[str] = []
    location_owners: dict[str, list[tuple[str, list[dict]]]] = {}
    for cfg in server_configs:
        if not getattr(cfg, "enabled", True):
            continue
        for lib in cfg.libraries or []:
            if not getattr(lib, "enabled", True):
                continue
            for loc in lib.remote_paths or ():
                if not str(loc).strip():
                    continue
                norm_loc = str(loc).rstrip("/")
                locations.append(norm_loc)
                location_owners.setdefault(norm_loc, []).append((cfg.name or cfg.id, list(cfg.path_mappings or [])))

    if not locations:
        return {}

    # Longest-first so more-specific library locations match before
    # broader ones (e.g. /media/tv before /media so the hint suggests
    # the closer parent).
    norm_locations = sorted({loc for loc in locations}, key=len, reverse=True)
    all_mappings = [mappings for owners_list in location_owners.values() for _, mappings in owners_list]

    hints: dict[str, str] = {}
    for upath in unresolved_paths:
        upath_norm = upath.replace("\\", "/")
        upath_lower = upath_norm.lower()
        for server_loc in norm_locations:
            loc_lower = server_loc.lower()
            idx = upath_lower.find(loc_lower)
            if idx <= 0:
                continue
            # Path-boundary check — /media/tv must not match /media/tv2.
            end_idx = idx + len(loc_lower)
            if end_idx < len(upath_lower) and upath_lower[end_idx] != "/":
                continue

            extra = upath_norm[:idx]
            # Suggest the parent so the mapping covers sibling
            # libraries (e.g. /media covers both /media/tv and
            # /media/movies).
            server_parent = os.path.dirname(server_loc)
            if server_parent and server_parent != "/":
                server_pfx = server_parent
                webhook_pfx = extra.rstrip("/") + server_parent
            else:
                server_pfx = server_loc
                webhook_pfx = extra.rstrip("/") + server_loc

            # Coverage check: is there already a mapping that would
            # have bridged this prefix gap on the owning server?
            candidate_owners: list[tuple[str, list[dict]]] = []
            for loc, owners_list in location_owners.items():
                if loc == server_pfx or loc.startswith(server_pfx.rstrip("/") + "/"):
                    candidate_owners.extend(owners_list)
            owner_covers = any(
                _mismatch_covered_by_mappings(webhook_pfx, server_pfx, mappings) for _, mappings in candidate_owners
            )
            if owner_covers:
                hints[upath] = (
                    f"Path mapping '{webhook_pfx}' → '{server_pfx}' is configured but file not "
                    "found (may not be indexed yet)"
                )
                break

            other_covers = any(_mismatch_covered_by_mappings(webhook_pfx, server_pfx, m) for m in all_mappings)
            if other_covers:
                hints[upath] = (
                    f"Path mapping '{webhook_pfx}' → '{server_pfx}' is configured on a different "
                    "server but the owning server is missing it — add the mapping there too in "
                    "Settings → Path mappings"
                )
            else:
                hints[upath] = (
                    f"Possible prefix mismatch: webhook sends '{webhook_pfx}' but a configured "
                    f"library uses '{server_pfx}'. Add a path mapping in Settings: server path = "
                    f"{server_pfx}, webhook path = {webhook_pfx}"
                )
            break

        if upath not in hints:
            # No configured library location appears anywhere in this webhook
            # path, so the substring-pivot above found nothing — the sender's
            # mount root differs entirely from every server's view (the classic
            # Sonarr/Radarr-on-a-different-mount case, issue #254, where neither
            # the media-server's remote prefix nor this app's local prefix is a
            # prefix of the incoming path). We can't derive the exact split, but
            # we CAN name the root the webhook arrived with and point the
            # operator straight at the field that fixes it — far more useful
            # than a bare "Not found".
            upath_norm = upath.replace("\\", "/")
            segments = [s for s in upath_norm.split("/") if s]
            webhook_root = "/" + segments[0] if segments else upath_norm
            example_loc = norm_locations[-1]
            owner_name = location_owners.get(example_loc, [("your server", [])])[0][0]
            hints[upath] = (
                f"Webhook sent '{upath_norm}' but no configured library matches it. "
                f"If this came from Sonarr/Radarr/Tdarr, they see your media at a different path "
                f"than {owner_name} reports (e.g. '{example_loc}'). Add the webhook's path root "
                f"(e.g. '{webhook_root}') under Settings → {owner_name} → Path mappings → "
                "'Path the webhook sends'."
            )

    return hints


def _classify_processing_mode(config) -> str:
    """Decide which processing phase ``run_processing`` should execute.

    Returns one of:

    * ``"webhook_paths"`` — the job has a concrete path list; dispatch via
      :func:`_run_webhook_paths_phase`.
    * ``"refuse_malformed_webhook"`` — the job is webhook-origin
      (``webhook_source`` set) but ``webhook_paths`` is empty / None.
      The caller MUST NOT fall through to a full library scan: doing so
      attributes a 100k+ item scan to a Job that the UI presents as a
      single-file webhook entry. See Job e7968486 (May 2026): one Sonarr
      webhook for one TV episode triggered eight separate full-library
      scans across eleven container restarts because the original
      "Job-at-batch-open" refactor forgot to persist ``webhook_paths``
      in ``job.config``. The webhook-side fix closes the primary hole;
      this branch is defense in depth against any future code path that
      ships a webhook job without paths.
    * ``"full_scan"`` — no webhook markers at all; legitimate scheduled
      or manual full-library scan.

    Pure function; only reads attributes on ``config``. Tested in
    ``tests/test_orchestrator_webhook_fallthrough.py``.
    """
    if getattr(config, "webhook_paths", None):
        return "webhook_paths"
    if getattr(config, "webhook_source", None):
        return "refuse_malformed_webhook"
    return "full_scan"


def _run_webhook_paths_phase(
    config,
    registry,
    *,
    dispatch_items,
    progress_callback,
    cancel_check,
    pause_check=None,
    job_id: str | None,
    totals: dict,
    aggregate_outcome: dict,
) -> dict:
    """Dispatch every webhook path through the unified peer-equal fan-out.

    Mutates ``totals`` (keys: ``processed``, ``successful``, ``failed``,
    ``cancelled``) and ``aggregate_outcome`` in place so the caller can
    keep accumulating across phases. Returns the ``webhook_resolution``
    dict that becomes part of the job's return_data.

    Architecture: every webhook path is a :class:`ProcessableItem` and
    runs through ``dispatch_items`` → ``process_canonical_path``. That
    worker handles per-server ownership resolution + parallel fan-out
    so Plex, Emby, and Jellyfin all publish for any path they own.
    There is no Plex-first stage, no fallback, and no K4: every server
    is a peer. Paths owned by no enabled server fast-skip here so a
    worker thread never gets handed a path it can't process.

    Vendor-webhook hints (Plex/Emby/Jellyfin native plugins that
    already named the item id) flow through unchanged via
    ``ProcessableItem.item_id_by_server`` so the relevant adapter
    skips a slow reverse-lookup. The dispatcher's lazy
    ``_make_item_id_resolver`` handles the no-hint case per-server.
    """
    from ..processing.types import ProcessableItem as _PI

    paths = list(config.webhook_paths or [])
    total_paths = len(paths)
    if not paths:
        return {
            "unresolved_paths": [],
            "skipped_paths": [],
            "resolved_count": 0,
            "total_paths": 0,
            "path_hints": [],
        }

    if progress_callback:
        progress_callback(0, total_paths, f"Resolving {total_paths} webhook path(s) across configured servers…")
    _log_webhook_owning_servers(config, paths)

    hints = getattr(config, "webhook_item_id_hints", None) or {}
    server_configs = list(registry.configs())

    webhook_items: list[_PI] = []
    # Audit A3/A4 — keep a parallel canonical→raw-input map so the
    # ``unresolved_paths`` list (consumed by job_runner.py for
    # file_result rows + retry hint lookup keying) can stay in a
    # SINGLE namespace (the raw webhook input) regardless of whether
    # a path was a no_owner skip or a FAILED dispatch outcome.
    # Without this, no_owner paths landed in unresolved_paths as raw
    # webhook strings while FAILED paths landed as server-view
    # canonical strings → the retry job's
    # ``webhook_item_id_hints`` lookup (keyed by raw input) missed
    # FAILED items entirely → retries paid the slow Jellyfin Pass 2
    # cost instead of using the hint short-circuit.
    canonical_to_input: dict[str, str] = {}
    no_owners: list[str] = []
    for path in paths:
        # Resolve the webhook-source path to a server-view canonical
        # path AND its owners in one pass. Sonarr/Radarr emit paths in
        # their own namespace (``/data/TV Shows/...``) and the server
        # libraries report a different one (``/data_16tb/TV Shows``);
        # the helper translates via webhook_prefixes and returns the
        # canonical form that matches the library, so the downstream
        # ``process_canonical_path._resolve_owners`` lookup (which
        # doesn't translate) gets a path it can match. Without this
        # the worker picks up the path, the per-server check fails,
        # and the job lands NO_OWNERS milliseconds later despite
        # multiple servers actually owning the file.
        canonical_path, owners = _resolve_webhook_path_to_canonical(path, server_configs)
        per_path = hints.get(path) or {}
        if not owners:
            # Audit A2 — when no library covers the path BUT the
            # webhook payload supplied a vendor item-id hint (Plex
            # ``library.new``, Emby ``ItemAdded``, Jellyfin plugin
            # webhook all do), the dispatcher's ``_resolve_publishers``
            # would still honour the hint via the hinted server's
            # adapter. The orchestrator gate previously fast-skipped
            # this case → user adds a library, gets a webhook before
            # our cache refreshes, the very webhook that should
            # bootstrap the new library silently does nothing.
            #
            # Library-cache staleness during library-add → first
            # webhook silently dropped. Honour the hint: dispatch the
            # path with the hinted server pinned via item.server_id;
            # downstream resolver looks up the hinted server's id and
            # fans out only there.
            if not per_path:
                no_owners.append(path)
                continue
            logger.debug(
                "Webhook path {} has no library coverage but vendor hint(s) supplied "
                "{!r} — honouring hint and dispatching anyway (library cache may be "
                "stale post library-add).",
                path,
                list(per_path.keys()),
            )
        # Hint dicts always have one entry today (vendor webhooks carry
        # exactly one server hint); a future caller passing multiple
        # gets dict-insertion order with a debug line so it's traceable.
        if len(per_path) > 1:
            logger.debug(
                "ProcessableItem for {} has {} hint server(s); using first ({}). "
                "Other hints still flow into item_id_by_server.",
                path,
                len(per_path),
                next(iter(per_path)),
            )
        server_id = next(iter(per_path), "")
        webhook_items.append(
            _PI(
                canonical_path=canonical_path,
                server_id=server_id,
                item_id_by_server=dict(per_path),
                title=os.path.basename(canonical_path.rstrip("/")) or canonical_path,
                library_id=None,
            )
        )
        # Track canonical → raw input so a FAILED outcome can be
        # surfaced under the original webhook path (audit A3/A4).
        canonical_to_input[canonical_path] = path

    unresolved: list[str] = list(no_owners)
    # Path-keyed mismatch hints (audit P4). Built per-path so a
    # multi-path webhook with N different mismatches displays N
    # different hints — one per file_result row, not one borrowed
    # from slot 0.
    path_hint_map: dict[str, str] = {}

    if no_owners:
        logger.info(
            "Webhook arrived with {} path(s) that no enabled server claims — fast-skipping "
            "(no worker pickup, no retry). Verify path mappings under Settings line up with "
            "what each server reports for its libraries.",
            len(no_owners),
        )
        # When a path is unowned but a configured library's location is
        # a path-boundary substring of the webhook path, the user almost
        # certainly has a path-mapping mismatch (Sonarr/Radarr send
        # ``/data/Movies/X.mkv`` but Plex/Emby/Jellyfin reports
        # ``/media/Movies/X.mkv``, no mapping configured). Surfacing
        # this hint per-row keeps the UX the legacy Plex-first stage
        # gave users — without it, the file_result row just says "Not
        # found", which doesn't tell the user *why*.
        path_hint_map.update(_build_path_mapping_mismatch_hints(no_owners, server_configs))

    if webhook_items:
        result = dispatch_items(webhook_items, "Webhook Targets")
        totals["successful"] += result["completed"]
        totals["failed"] += result["failed"]
        totals["processed"] += result["completed"] + result["failed"]
        totals["cancelled"] = totals["cancelled"] or result["cancelled"]
        for k, v in (result.get("outcome") or {}).items():
            aggregate_outcome[k] = aggregate_outcome.get(k, 0) + v
        # ``dispatch_items`` doesn't tell us WHICH paths failed — only
        # the aggregate count. Single-path batch (the dominant vendor-
        # webhook case) is unambiguous; multi-path batches mark exactly
        # N (count is correct, identity is unknowable, retries on
        # already-succeeded paths short-circuit cheaply via .meta).
        # See audit H2.
        failed_count = result.get("failed", 0)
        if failed_count:
            # Audit A3/A4 — surface the RAW webhook-input path in the
            # unresolved list so the retry job's
            # ``webhook_item_id_hints`` lookup (keyed by raw input)
            # finds its hint. Pre-fix this stored the server-view
            # canonical_path; the retry job's webhook_paths matched,
            # but the hint dict (keyed by raw) didn't → retries paid
            # full reverse-lookup cost on every retry round.
            failed_inputs = [
                canonical_to_input.get(item.canonical_path, item.canonical_path)
                for item in webhook_items[:failed_count]
            ]
            unresolved.extend(failed_inputs)
            # Pass-1 audit #6: also build hints for FAILED items, not
            # only no_owners. A path that owners exist for but every
            # publisher failed (e.g. publisher 5xx, source missing
            # post-rebind) gets the same diagnostic UX as a no-owner
            # path. Hints are best-effort — if no mismatch is detected
            # the path simply doesn't appear in the dict and the
            # consumer falls back to a generic message.
            path_hint_map.update(_build_path_mapping_mismatch_hints(failed_inputs, server_configs))

    return {
        "unresolved_paths": unresolved,
        "skipped_paths": [],
        "resolved_count": total_paths - len(unresolved),
        "total_paths": total_paths,
        # Backwards-compatible: legacy callers that consumed
        # ``path_hints`` as a list still see a flat list of hint
        # strings (the same set, dedup-preserved). New callers read
        # ``path_hint_map`` for per-path correspondence.
        "path_hints": list(dict.fromkeys(path_hint_map.values())),
        "path_hint_map": dict(path_hint_map),
    }


def _run_plex_full_scan_phase(
    config,
    registry,
    *,
    dispatch_items,
    progress_callback,
    cancel_check,
    totals: dict,
    aggregate_outcome: dict,
) -> bool:
    """Enumerate the full Plex library and dispatch every item.

    Mutates ``totals`` (keys: ``processed``, ``successful``, ``failed``,
    ``cancelled``) and ``aggregate_outcome`` in place. Returns ``True`` if
    enumeration completed (even if no items were found); ``False`` if the
    enumeration itself raised — the caller should treat that as a fatal
    job error.

    The dispatch goes through the same unified per-vendor processor →
    ProcessableItem → process_canonical_path path that Emby and Jellyfin
    use. The legacy tuple-shape pump is gone — keep this in mind when
    reading per-item logs (they'll mention the per-vendor adapter).
    """
    all_media_items: list = []
    try:
        for item in _enumerate_plex_full_scan_items(
            config,
            registry,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        ):
            if cancel_check and cancel_check():
                totals["cancelled"] = True
                break
            all_media_items.append(item)
    except Exception:
        logger.exception(
            "Plex full-scan enumeration failed. Verify Plex is reachable and the access token in Settings is valid."
        )
        return False

    if cancel_check and cancel_check():
        logger.info("Cancellation requested before dispatch — skipping processing")
        totals["cancelled"] = True
        return True

    if not all_media_items:
        logger.info("No media items found across selected libraries")
        return True

    # When sort_by is "random", shuffle the combined cross-library list so
    # parallel workers statistically pull from multiple physical disks at
    # once (big win on unraid shfs / mergerfs / JBOD setups).
    if config.sort_by == "random":
        random.Random().shuffle(all_media_items)
        logger.info("Shuffled {} items for random processing order", len(all_media_items))

    total_items = len(all_media_items)
    logger.info("Processing {} items across selected Plex libraries", total_items)

    result = dispatch_items(all_media_items, "All Libraries")
    totals["successful"] += result["completed"]
    totals["failed"] += result["failed"]
    totals["processed"] += result["completed"] + result["failed"]
    totals["cancelled"] = totals["cancelled"] or result["cancelled"]
    outcome = result.get("outcome") or {}
    for k, v in outcome.items():
        aggregate_outcome[k] = aggregate_outcome.get(k, 0) + v
    return True


def run_processing(
    config,
    selected_gpus,
    progress_callback=None,
    worker_callback=None,
    item_complete_callback=None,
    cancel_check=None,
    pause_check=None,
    worker_pool_callback=None,
    job_id=None,
    on_dispatch_start=None,
    priority=None,
):
    """Run the main processing workflow.

    Args:
        config: Configuration object.
        selected_gpus: List of (gpu_type, gpu_device, gpu_info) tuples
            for enabled GPUs.
        progress_callback: Optional callback(current, total, message)
            for progress updates.
        worker_callback: Optional callback(workers_list) for worker
            status updates.
        item_complete_callback: Optional callback(display_name, title,
            success) when a worker finishes an item.
        cancel_check: Optional callable returning True when processing
            should stop.
        pause_check: Optional callable returning True when processing
            should pause dispatch.
        worker_pool_callback: Optional callable receiving WorkerPool on
            create/cleanup.
        job_id: Optional job identifier for multi-job dispatch.
        on_dispatch_start: Optional callable invoked once before the
            first batch of items is dispatched.
        priority: Optional dispatch priority (1=high, 2=normal, 3=low).

    Returns:
        Dict with outcome counts and optional webhook resolution info,
        or None on fatal error.

    """
    return_data = None
    worker_pool = None
    try:
        # Multi-server guard: when this job is pinned to a non-Plex server, or
        # when no Plex is configured at all, the legacy Plex orchestrator can't
        # do anything useful — full-library enumeration uses the Plex API.
        # Honest no-op: log clearly and return so the job ends cleanly instead
        # of crashing with a Plex connection error.
        sid_filter = getattr(config, "server_id_filter", None)
        sid_filter = sid_filter if isinstance(sid_filter, str) and sid_filter else None
        _pinned_entry, pinned_type = _resolve_pinned_server(sid_filter)

        # Defense in depth (BEFORE either full-scan branch): a job marked
        # as webhook-origin but missing webhook_paths is malformed —
        # likely an auto-requeue after restart where the path list got
        # lost. Refuse outright; never let a webhook job degrade into a
        # full-library scan. See Job e7968486 (May 2026) for the
        # regression this guards. Tested in
        # tests/test_orchestrator_webhook_fallthrough.py.
        if _classify_processing_mode(config) == "refuse_malformed_webhook":
            logger.error(
                "Refusing to run job as full library scan: it carries a webhook "
                "source ({!r}) but no webhook_paths. Most likely cause: the job "
                "was created by a webhook, persisted without webhook_paths in "
                "job.config, then revived after a container restart with the path "
                "list lost. Re-trigger the originating webhook to process the "
                "original file. See Job e7968486 (May 2026) for the regression "
                "this guards against.",
                getattr(config, "webhook_source", None),
            )
            empty_outcome = {r.value: 0 for r in ProcessingResult}
            return {"outcome": empty_outcome, "error": "webhook_paths_missing"}

        if _should_use_multi_server_full_scan(config, pinned_type):
            library_ids = list(getattr(config, "plex_library_ids", None) or [])
            # Operator breadcrumb so a multi-Plex install that just got
            # routed through the fan-out path (issue #244 fix) can be
            # spotted in logs without grepping the gate function. The
            # pre-fix log trail was identical for legacy and multi-server
            # paths, so a 2-Plex no-pin scan looked like it had always
            # walked both servers when in fact it only walked the first.
            logger.info(
                "Full-scan dispatch: multi-server path (pin={!r}). Walks every "
                "enabled server matching the pin; honours server_id_filter end-to-end.",
                sid_filter,
            )
            scan_warnings: list[str] = []
            outcome_counts = _run_full_scan_multi_server(
                config,
                selected_gpus=selected_gpus,
                server_id_filter=sid_filter,
                library_ids=library_ids or None,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                pause_check=pause_check,
                job_id=job_id,
                worker_callback=worker_callback,
                on_dispatch_start=on_dispatch_start,
                worker_pool_callback=worker_pool_callback,
                warnings_out=scan_warnings,
            )
            result: dict = {"outcome": outcome_counts}
            if scan_warnings:
                # Joined warning string the job_runner pipes into
                # ``complete_job(warning=...)`` — flips the dashboard
                # badge from green "completed" to amber "completed
                # with warning" so a silently-failed enumeration
                # doesn't masquerade as a successful run.
                result["warning"] = " | ".join(scan_warnings)
            return result

        # Per-server PlexServer instances are established lazily by
        # the dispatch path (`process_canonical_path` → adapter →
        # `_resolve_one_path`) when a path actually needs Plex
        # resolution. The orchestrator no longer pre-connects:
        # * The result was a dead parameter on
        #   ``_run_webhook_paths_phase`` after the K4 → peer-equal
        #   unification (commit 3edd185). The full-scan phase never
        #   took it.
        # * Eagerly opening a Plex session blocked job start by ~300ms
        #   even on jobs whose paths only Emby/Jellyfin own — and
        #   would abort the entire job (ConnectionError) on a Plex
        #   outage that shouldn't have touched non-Plex paths at all.
        # * The "[Plex] Connecting to Plex" log line landing before
        #   the unified-dispatch "Resolving N webhook path(s)…" read
        #   like Plex-first dispatch in the timeline (user-flagged on
        #   job 3b154264).
        clear_failures()

        # Build a registry covering EVERY configured media server so the
        # dispatch path can fan out to all owning publishers (Plex + Emby +
        # Jellyfin). Previously this used from_legacy_config which only
        # produced a single-Plex registry — webhook + scheduled jobs then
        # silently dropped fan-out, publishing only to Plex even when the
        # canonical path was also owned by Emby/Jellyfin libraries. Falls
        # back to the legacy single-Plex shim only when the persisted
        # media_servers list is empty (fresh install / pre-migration).
        from ..servers.registry import ServerRegistry as _ServerRegistry
        from ..web.settings_manager import get_settings_manager as _get_sm

        try:
            _media_servers_raw = _get_sm().get("media_servers") or []
        except Exception:
            _media_servers_raw = []
        if _media_servers_raw:
            registry = _ServerRegistry.from_settings(_media_servers_raw, legacy_config=config)
        else:
            registry = _ServerRegistry.from_legacy_config(config)

        title_max_width = 200

        def _create_worker_pool():
            pool = WorkerPool(
                gpu_workers=config.gpu_threads,
                cpu_workers=config.cpu_threads,
                selected_gpus=selected_gpus,
            )
            if worker_pool_callback:
                worker_pool_callback(pool)
            return pool

        # Mutable accumulators threaded through the phase helpers. A dict
        # rather than several `nonlocal` ints because the phase helpers
        # are module-level functions, not closures.
        totals = {"processed": 0, "successful": 0, "failed": 0, "cancelled": False}
        aggregate_outcome = {r.value: 0 for r in ProcessingResult}

        # (Headless is the only mode this app runs in — the legacy CLI
        # console-display path was removed when the web UI became the only
        # interface. The "headless mode" wording in worker.process_items_headless
        # remains as a load-bearing API name.)

        _dispatch_started = False

        def _dispatch_items(items, library_name):
            """Dispatch items via shared dispatcher or local pool."""
            nonlocal worker_pool, _dispatch_started
            if job_id:
                from .dispatcher import get_dispatcher

                existing = get_dispatcher()
                if existing is not None:
                    worker_pool = existing.worker_pool
                    # D33 — Surface the reuse so the per-job log doesn't
                    # silently start dispatching with no worker context.
                    # Without this, the absence of "Initialized N workers"
                    # on a reused pool looked like the job was running
                    # without any workers — confusing when comparing
                    # back-to-back job logs.
                    try:
                        worker_count = len(worker_pool._snapshot_workers())
                    except Exception:
                        worker_count = 0
                    logger.info(
                        "Reusing existing worker pool ({} worker(s)) — no fresh init needed",
                        worker_count,
                    )
                elif worker_pool is None:
                    worker_pool = _create_worker_pool()
                dispatcher = get_dispatcher(worker_pool)

                # Reconcile the pool with the latest settings.  The pool
                # may have been created minutes ago with stale config
                # (e.g. 0 workers because the user hadn't configured GPUs
                # yet at startup).  The callback re-reads current settings
                # and calls reconcile_gpu_workers so the pool matches.
                if worker_pool_callback:
                    worker_pool_callback(worker_pool)

                if not _dispatch_started and on_dispatch_start:
                    on_dispatch_start()
                    _dispatch_started = True
                    # Emit the initial 0% progress AFTER the job
                    # transitions to RUNNING so the frontend's
                    # active-job DOM elements exist before the
                    # job_progress SocketIO event arrives.
                    if progress_callback:
                        progress_callback(0, len(items), f"Starting {library_name}")

                callbacks = {
                    "progress_callback": progress_callback,
                    "worker_callback": worker_callback,
                    "on_item_complete": item_complete_callback,
                    "cancel_check": cancel_check,
                    "pause_check": pause_check,
                }
                from ..web.jobs import PRIORITY_NORMAL

                tracker = dispatcher.submit_items(
                    job_id=job_id,
                    items=items,
                    config=config,
                    registry=registry,
                    title_max_width=title_max_width,
                    library_name=library_name,
                    callbacks=callbacks,
                    priority=priority if priority is not None else PRIORITY_NORMAL,
                )
                tracker.wait()
                # D12 — Dispatcher._merge_worker_outcome maintains a
                # per-server publisher aggregate on the tracker and
                # mirrors it onto the Job (set_publishers) every task.
                # Per-file × per-server detail lives in the Files panel
                # JSONL via record_file_result; nothing to drain here.
                return tracker.get_result()
            else:
                # Local pool mode (no dispatcher) — emit initial progress
                # before starting the pool.
                if progress_callback:
                    progress_callback(0, len(items), f"Starting {library_name}")
                if worker_pool is None:
                    worker_pool = _create_worker_pool()
                return worker_pool.process_items_headless(
                    items,
                    config,
                    registry,
                    title_max_width,
                    library_name=library_name,
                    progress_callback=progress_callback,
                    worker_callback=worker_callback,
                    on_item_complete=item_complete_callback,
                    cancel_check=cancel_check,
                    pause_check=pause_check,
                )

        # ``_classify_processing_mode`` here picks between
        # "webhook_paths" and "full_scan" — the "refuse_malformed_webhook"
        # case was already short-circuited at the top of run_processing
        # (before the multi-server fast path), so the third branch below
        # is intentionally unreachable today. Keeping it as an explicit
        # AssertionError instead of an open ``else`` makes the invariant
        # load-bearing: if some future edit lifts the early refusal,
        # this site will fail loudly instead of silently degrading
        # malformed webhook jobs into a Plex full scan.
        mode = _classify_processing_mode(config)
        if mode == "webhook_paths":
            webhook_resolution_payload = _run_webhook_paths_phase(
                config,
                registry,
                dispatch_items=_dispatch_items,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                pause_check=pause_check,
                job_id=job_id,
                totals=totals,
                aggregate_outcome=aggregate_outcome,
            )
            return_data = {"webhook_resolution": webhook_resolution_payload}
        elif mode == "full_scan":
            ok = _run_plex_full_scan_phase(
                config,
                registry,
                dispatch_items=_dispatch_items,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                totals=totals,
                aggregate_outcome=aggregate_outcome,
            )
            if not ok:
                return {"outcome": aggregate_outcome}
        else:
            raise AssertionError(
                f"Unreachable: refuse_malformed_webhook should have been caught "
                f"by the early refusal at the top of run_processing — got mode={mode!r}"
            )

        summary = _format_outcome_summary(aggregate_outcome)
        if totals["cancelled"]:
            logger.info("Processing stopped by cancellation: {}", summary)
        else:
            logger.info("Processing complete: {}", summary)

        _maybe_log_path_mapping_misconfig(aggregate_outcome, totals["processed"])

        log_failure_summary()

        return_data = return_data or {}
        return_data["outcome"] = aggregate_outcome

        return return_data

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down gracefully...")
    except ConnectionError as e:
        logger.error(
            "Could not reach Plex while running this job ({}). "
            "Job aborted — verify the Plex URL and token in Settings, that the Plex server is running, "
            "and that there's no firewall between the two. Re-run the job once Plex is reachable.",
            e,
        )
        return None
    except Exception:
        logger.exception(
            "Unexpected error during the preview-generation job — aborting this job. "
            "This is likely a bug. The web UI and other jobs keep running. "
            "The full traceback is included above; please report it at "
            "https://github.com/stevezau/media_preview_generator/issues."
        )
        raise
    finally:
        try:
            if worker_pool is not None and not job_id:
                worker_pool.shutdown()
        except Exception as worker_error:
            logger.warning(
                "Worker pool didn't shut down cleanly: {}. "
                "Background threads may still be running — usually harmless, but if you see orphan FFmpeg "
                "processes after the job ends, restart the container.",
                worker_error,
            )
        finally:
            if not job_id and worker_pool_callback:
                worker_pool_callback(None)

        try:
            if os.path.isdir(config.working_tmp_folder):
                shutil.rmtree(config.working_tmp_folder)
                logger.debug("Cleaned up working temp folder: {}", config.working_tmp_folder)
        except Exception as cleanup_error:
            logger.warning(
                "Could not delete the working temp folder at {}: {}. "
                "This won't break future runs but the folder will accumulate data over time — "
                "watch your disk and manually clear it if it grows large.",
                config.working_tmp_folder,
                cleanup_error,
            )
