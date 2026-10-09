"""Scheduling system for the web interface.

Uses APScheduler with SQLite storage for persistent scheduled jobs.
"""

import copy
import json
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
)
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    """Parse an "HH:MM" string into ``(hour, minute)``.

    Returns ``None`` when ``value`` is empty / None (caller treats this
    as "no stop time configured"). Raises ``ValueError`` on a non-empty
    but malformed input so the API layer can surface a 400 rather than
    silently dropping a misconfiguration.
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"stop_time must be HH:MM, got {value!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"stop_time must be HH:MM, got {value!r}") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"stop_time out of range, got {value!r}")
    return hour, minute


# Quiet-hours job ids. The per-minute recheck is the only live job; the other
# ids belong to earlier schemes and are removed on every apply.
_QUIET_HOURS_PAUSE_JOB_ID = "__quiet_hours_pause"
_QUIET_HOURS_RESUME_JOB_ID = "__quiet_hours_resume"
_QUIET_HOURS_PAUSE_PREFIX = "__qh_pause_"
_QUIET_HOURS_RESUME_PREFIX = "__qh_resume_"
_QUIET_HOURS_RECHECK = "__qh_recheck"

# APScheduler day_of_week names (Mon-first). Order matters for cron
# strings — keep these literal so a typo in the JS payload can't slip
# through to a silent no-op.
_QUIET_HOURS_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def normalise_quiet_hours(raw: dict | None) -> dict:
    """Read or migrate quiet hours to explicit start-day intervals."""
    from ..quiet_hours import migrate_quiet_hours

    return migrate_quiet_hours(raw)


def is_now_in_any_quiet_window(quiet_hours: dict | None, now: datetime | None = None) -> bool:
    """Evaluate current local wall time, including exact legacy weekday migration."""
    from ..quiet_hours import quiet_hours_weekly_mask
    from ..worker_groups import local_now

    moment = local_now(now)
    minute = moment.weekday() * 1440 + moment.hour * 60 + moment.minute
    return bool(quiet_hours_weekly_mask(quiet_hours) & (1 << minute))


def _quiet_hours_recompute_and_apply(*, drain: bool = True) -> None:
    """Idempotent state flip — set processing_paused to whether ANY window is active.

    Called by the per-minute recheck job and from the boot-time gate.
    """
    try:
        from .jobs import get_job_manager
        from .settings_manager import get_settings_manager

        sm = get_settings_manager()
        target = is_now_in_any_quiet_window(sm.get("quiet_hours"))
        if not sm.set_processing_pause_reason("quiet_hours", target):
            return
        effective = sm.processing_paused
        try:
            get_job_manager().emit_processing_paused_changed(effective)
        except Exception:
            logger.debug(
                "Could not emit processing_paused_changed on quiet-hours flip",
                exc_info=True,
            )
        if effective:
            logger.info("Quiet hours: processing paused (queue will fill until resume time)")
        elif drain:
            # Start the PENDING backlog the same way the manual/auto resume
            # paths do — otherwise jobs revived PENDING-while-paused at boot
            # (pause now survives restarts) sit forever when a quiet window
            # ends, since flipping the flag alone dispatches nothing.
            from .routes.job_runner import resume_running_and_drain_pending

            resume_running_and_drain_pending()
            logger.info("Quiet hours: processing resumed (queue draining)")
    except Exception:
        logger.exception("Quiet-hours recompute hit an unexpected error")


# Jobs stored by older releases (``__qh_pause_N`` / ``__qh_resume_N``) still reference these names.
def _quiet_hours_pause() -> None:
    _quiet_hours_recompute_and_apply()


def _quiet_hours_resume() -> None:
    _quiet_hours_recompute_and_apply()


def execute_schedule_stop(schedule_id: str) -> None:
    """Add this schedule's hold to its pending and running jobs.

    Pickled by APScheduler's SQLAlchemy jobstore alongside the start
    cron, so it must live at module scope. Looks up the JobManager
    singleton and calls ``request_pause`` for matching ``parent_schedule_id``
    values. The normal job pause mechanism freezes active processing;
    unlike worker-group reductions, this is not a drain operation.
    """
    from .jobs import JobStatus, get_job_manager

    manager = get_schedule_manager()
    schedule = manager.get_schedule(schedule_id) if manager else None
    name = (schedule or {}).get("name", schedule_id)
    stop_time = (schedule or {}).get("stop_time", "")

    job_manager = get_job_manager()
    paused_count = 0
    for job in job_manager.get_all_jobs():
        if job.parent_schedule_id != schedule_id:
            continue
        if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
            continue
        if job_manager.request_pause(job.id, by_schedule=True):
            job_manager.add_log(
                job.id,
                f"INFO - Paused by schedule {name!r} stop time ({stop_time})",
            )
            paused_count += 1

    if paused_count:
        logger.info(
            "Schedule {!r} ({}): stop-time fired, paused {} pending or running job(s)",
            name,
            schedule_id,
            paused_count,
        )
    else:
        logger.info(
            "Schedule {!r} ({}): stop-time fired, no unheld pending or running jobs from this schedule",
            name,
            schedule_id,
        )


# Serialises a Find markers tick's "unfinished job?" check with its job's creation (Run now beside a start tick).
_FIND_MARKERS_TICK_LOCK = threading.Lock()
_FULL_LIBRARY_TICK_LOCK = threading.Lock()


def _resumed_kind(config: dict | None) -> str | None:
    """The job kind a schedule's start tick resumes after its stop time paused it (``execute_scheduled_job``).

    Returns:
        ``intro_credits`` or ``previews`` (including Recently Added).
    """
    from ..job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_PREVIEWS

    job_type = str((config or {}).get("job_type", "full_library"))
    return JOB_KIND_INTRO_CREDITS if job_type == "intro_credits" else JOB_KIND_PREVIEWS


def _warn_stop_paused_jobs_left_behind(schedule_id: str, name: str, config: dict | None) -> None:
    """Log a WARNING for each job this schedule's stop time paused that its start ticks no longer resume.

    A schedule switched between previews, Intro & Credits and Recently Added leaves the other kind's paused job paused,
    like a deleted schedule's; this names it instead of leaving it silently.

    Args:
        schedule_id: The schedule's id.
        name: Its name.
        config: Its config after the change.
    """
    from .jobs import PAUSED_BY_SCHEDULE, JobStatus, get_job_manager
    from .routes.job_runner import RECENTLY_ADDED_JOB_SOURCE

    kind = _resumed_kind(config)
    try:
        jobs = get_job_manager().get_all_jobs()
    except Exception:
        logger.exception("Schedule {} changed, but its paused jobs couldn't be listed", schedule_id)
        return
    for job in jobs:
        same_mode = ((job.config or {}).get("source") == RECENTLY_ADDED_JOB_SOURCE) == (
            (config or {}).get("job_type") == "recently_added"
        )
        if job.parent_schedule_id != schedule_id or (job.kind == kind and same_mode):
            continue
        if not (job.paused and job.status is JobStatus.RUNNING and (job.config or {}).get(PAUSED_BY_SCHEDULE)):
            continue
        logger.warning(
            "Schedule {!r} ({}) no longer runs the kind of job its stop time paused: job {} ({}) stays paused, and no "
            "later start tick of this schedule will resume it. Resume or cancel it on the dashboard.",
            name,
            schedule_id,
            job.id[:8],
            job.library_name,
        )


def _warn_paused_jobs_of_deleted_schedule(schedule_id: str, name: str) -> None:
    """Log a WARNING for each paused job of a deleted schedule: no start tick of it will resume the job any more.

    The jobs are left paused; a paused Check servers job keeps every other Check servers run from queueing.

    Args:
        schedule_id: The deleted schedule's id.
        name: Its name.
    """
    from .jobs import JobStatus, get_job_manager

    try:
        jobs = get_job_manager().get_all_jobs()
    except Exception:
        logger.exception("Schedule {} was deleted, but its paused jobs couldn't be listed", schedule_id)
        return
    for job in jobs:
        if job.parent_schedule_id == schedule_id and job.paused and job.status is JobStatus.RUNNING:
            logger.warning(
                "Schedule {!r} ({}) was deleted while its job {} ({}) is paused; no later start tick of this schedule "
                "will resume it. Resume or cancel it on the dashboard.",
                name,
                schedule_id,
                job.id[:8],
                job.library_name,
            )


def _start_scheduled_intro_credits_job(
    manager: "ScheduleManager",
    schedule_id: str,
    library_ids: list[str],
    library_name: str,
    priority: int | str | None,
    server_id: str | None,
) -> None:
    """Create the Intro & Credits job for a schedule tick (LOW unless the schedule sets a priority).

    Nothing is created while a Find markers job of this schedule is still pending or running (a Check servers job it
    queued before a switch doesn't count); that check and the creation are one step, so Run now beside a start tick
    queues one job. Library ids are only meaningful with their server, so ids without one are
    pinned to the server that owns them and a tick that can't be pinned starts nothing rather than checking every
    library. Chosen libraries outside the server's Intro & Credits selection are left out; a schedule with only a
    server takes that server's selection.
    """
    with _FIND_MARKERS_TICK_LOCK:
        try:
            from ..job_kinds import JOB_KIND_INTRO_CREDITS
            from ..markers.ownership import marker_libraries
            from ..markers.triggers import create_intro_credits_job
            from ..servers.registry import UnsupportedServerTypeError, server_config_from_dict
            from .jobs import PRIORITY_LOW, JobStatus, get_job_manager, is_live_retry_chain, parse_priority
            from .settings_manager import get_settings_manager

            # A job whose own run is done and only its retry chain is left doesn't hold the schedule back.
            unfinished = next(
                (
                    job
                    for job in get_job_manager().get_all_jobs()
                    if job.parent_schedule_id == schedule_id
                    and job.kind == JOB_KIND_INTRO_CREDITS
                    and not (job.config or {}).get("reconcile")
                    and not is_live_retry_chain(job.config)
                    and job.status in (JobStatus.PENDING, JobStatus.RUNNING)
                ),
                None,
            )
            if unfinished is not None:
                logger.info(
                    "Schedule {}: Intro & Credits job {} from an earlier run hasn't finished; not starting another",
                    schedule_id,
                    unfinished.id[:8],
                )
                return
            if library_ids and not server_id:
                from .routes.api_jobs import _infer_server_from_library_ids

                server_id = _infer_server_from_library_ids(library_ids)[0]
                if not server_id:
                    logger.warning(
                        "Schedule {}: libraries {} don't belong to exactly one enabled media server, so its Intro & "
                        "Credits job wasn't started. Edit the schedule and pick the server.",
                        schedule_id,
                        library_ids,
                    )
                    return
            if server_id:
                entry = next(
                    (
                        e
                        for e in get_settings_manager().get("media_servers") or []
                        if isinstance(e, dict) and e.get("id") == server_id
                    ),
                    None,
                )
                try:
                    cfg = server_config_from_dict(entry) if entry is not None else None
                except UnsupportedServerTypeError:
                    cfg = None
                allowed = [lib.id for lib in marker_libraries(cfg)] if cfg is not None and cfg.enabled else []
                if library_ids:
                    names = {lib.id: lib.name for lib in cfg.libraries} if cfg is not None else {}
                    outside = [names.get(lid) or lid for lid in library_ids if lid not in allowed]
                    if outside:
                        logger.warning(
                            "Schedule {}: Intro & Credits isn't on for {} on server {}; those libraries are left out",
                            schedule_id,
                            outside,
                            server_id,
                        )
                    library_ids = [lid for lid in library_ids if lid in allowed]
                else:
                    library_ids = allowed
                if not library_ids:
                    logger.warning(
                        "Schedule {}: none of its libraries on server {} has Intro & Credits turned on; nothing to check",
                        schedule_id,
                        server_id,
                    )
                    return
            libraries = [{"server_id": server_id, "library_id": str(lid)} for lid in library_ids] if server_id else []
            # A schedule for one server's libraries publishes to that server only, as its scheduled preview job does
            # (``config["server_id"]`` in execute_scheduled_job): another server holding the same files isn't touched.
            pin = {"server_id": server_id} if server_id else {}
            create_intro_credits_job(
                library_name=f"Intro & Credits: {library_name or 'all libraries'}",
                priority=parse_priority(priority) if priority is not None else PRIORITY_LOW,
                source="schedule",
                libraries=libraries,
                parent_schedule_id=schedule_id,
                **pin,
            )
            manager._update_last_run(schedule_id)
        except Exception:
            logger.exception(
                "Scheduled Intro & Credits job {} could not start. It will try again on its next scheduled tick.",
                schedule_id,
            )


def _start_scheduled_check_servers(manager: "ScheduleManager", schedule_id: str, priority: int | str | None) -> None:
    """Queue Intro & Credits · Check servers for a schedule tick (LOW unless the schedule sets a priority).

    Nothing is queued while a Check servers job from anywhere is still queued or running; the schedule's last run is
    stamped only when this tick queued the job.
    """
    try:
        from ..markers.reconcile import run_markers_reconcile
        from .jobs import PRIORITY_LOW, parse_priority

        queued = run_markers_reconcile(
            priority=parse_priority(priority) if priority is not None else PRIORITY_LOW,
            parent_schedule_id=schedule_id,
        )
        if queued.created:
            manager._update_last_run(schedule_id)
        elif queued.job_id is not None:
            logger.info(
                "Schedule {}: Check servers job {} hasn't finished; not queueing another",
                schedule_id,
                queued.job_id[:8],
            )
    except Exception:
        logger.exception(
            "Scheduled Check servers {} could not start. It will try again on its next scheduled tick.", schedule_id
        )


# Module-level function for APScheduler to call
# Must be at module level to be picklable
def execute_scheduled_job(
    schedule_id: str,
    library_ids_or_id=None,
    library_name: str = "",
    config: dict | None = None,
    priority: int | None = None,
    server_id: str | None = None,
    *,
    library_id: str | None = None,
    ignore_pause: bool = False,
) -> None:
    """Execute a scheduled job — module-level function for APScheduler pickling.

    This function must be at module level (not a class method) because
    APScheduler's SQLAlchemy jobstore needs to pickle it.

    Dispatches on ``config["job_type"]``:

    * ``"recently_added"`` — runs the Recently Added scanner against the
      schedule's libraries (or all libraries when none specified).
      Uses ``config["lookback_hours"]`` (default 1). Plex-only.
    * ``"intro_credits"`` — creates an Intro & Credits job for the schedule's
      libraries (every library Intro & Credits goes to when none are chosen),
      LOW priority unless the schedule sets one. With ``config["reconcile"]``
      it queues Check servers instead (every server; libraries don't apply).
    * anything else (including missing) — legacy **full library** scan via
      ``manager.run_job_callback``, which creates a job processing every
      item in the targeted libraries.

    Args:
        schedule_id: The ID of the schedule triggering this job
        library_ids_or_id: Library section IDs to process. Accepts either a
            list of strings (Phase H7 canonical shape) or a single string
            (back-compat with persisted schedules from earlier versions).
            Pass ``None`` or ``[]`` to process all libraries.
        library_name: Human-readable library name(s) for display
        config: Job configuration dict — may include ``job_type`` and
            ``lookback_hours``
        priority: Dispatch priority (1=high, 2=normal, 3=low)
        server_id: Configured-server id this schedule targets (optional).
            Pinned through to the created job so per-server attribution
            works in the Jobs UI and the dispatcher routes only to that
            server.

    """
    # Normalise to list[str]; tolerate legacy callers that still pass
    # ``library_id=`` (single string) instead of the new positional list.
    if library_ids_or_id is None and library_id is not None:
        library_ids_or_id = library_id
    if isinstance(library_ids_or_id, str):
        library_ids = [library_ids_or_id] if library_ids_or_id else []
    elif isinstance(library_ids_or_id, list):
        library_ids = [str(x) for x in library_ids_or_id if str(x).strip()]
    else:
        library_ids = []
    primary_library_id = library_ids[0] if len(library_ids) == 1 else None

    cfg = dict(config or {})
    if server_id and "server_id" not in cfg:
        cfg["server_id"] = server_id
    job_type = str(cfg.get("job_type", "full_library"))
    manager = get_schedule_manager()

    # D21 — global processing-paused gate. When the queue is paused
    # (manual Pause All button OR quiet hours window), we DO NOT spawn
    # a new Job from this scheduled tick — that would pile up redundant
    # work (e.g. a "every 15 min recently_added" schedule firing all
    # day during quiet hours would balloon the queue). The schedule
    # re-fires on its next normal tick; the first one to land outside
    # the paused window will spawn the Job. Manual jobs and webhook
    # triggers still queue up — those go through different code paths
    # and are what the user explicitly wanted "to pile up".
    try:
        from .settings_manager import get_settings_manager

        if not ignore_pause and get_settings_manager().processing_paused:
            logger.info(
                "Schedule {} skipped — processing is currently paused (quiet hours / manual pause). "
                "It will fire again on its next normal tick.",
                schedule_id,
            )
            return
    except Exception:
        logger.debug(
            "Could not check processing_paused gate for schedule {}; allowing dispatch",
            schedule_id,
            exc_info=True,
        )

    # D20 — auto-resume the jobs this schedule's stop time paused
    # instead of spawning a fresh one. Lets a multi-night library scan
    # span across stop_time pauses with the same Job ID and progress.
    # Match kind and scan mode; clear only this schedule's hold so manual
    # pauses remain independent.
    schedule_kind = _resumed_kind(cfg)
    if schedule_kind is not None:
        try:
            from .jobs import PAUSED_BY_SCHEDULE, JobStatus, get_job_manager
            from .routes.job_runner import RECENTLY_ADDED_JOB_SOURCE

            # A schedule switched between previews and Intro & Credits must not resume the other kind's job (the
            # switch logs a WARNING naming it). Between finding markers and checking servers it does: nothing else
            # would resume the other mode's job.
            job_manager = get_job_manager()
            resumed = []
            for job in job_manager.get_all_jobs():
                if job.parent_schedule_id != schedule_id or job.kind != schedule_kind:
                    continue
                if ((job.config or {}).get("source") == RECENTLY_ADDED_JOB_SOURCE) != (job_type == "recently_added"):
                    continue
                if not job.paused or job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
                    continue
                if not (job.config or {}).get(PAUSED_BY_SCHEDULE):
                    continue
                # Checked again under the job manager's lock: a pause by hand since this listing stays paused.
                if job_manager.request_resume(job.id, only_paused_by_schedule=True):
                    job_manager.add_log(
                        job.id,
                        (
                            f"INFO - Schedule {schedule_id!r} pause cleared; manual pause remains"
                            if job.paused
                            else f"INFO - Resumed by schedule {schedule_id!r} start tick"
                        ),
                    )
                    resumed.append(job.id[:8])
                    if job.status is JobStatus.PENDING and not job.paused:
                        from .routes.job_runner import _start_job_async

                        _start_job_async(job.id, job.config or {})
            if resumed:
                # Nothing else is queued on this tick, whatever the schedule's mode now: a new job would compete with
                # the resumed ones for a slot and could run past the stop time. It queues on a later tick.
                logger.info(
                    "Schedule {}: resumed paused job(s) {} instead of spawning a new one",
                    schedule_id,
                    ", ".join(resumed),
                )
                manager._update_last_run(schedule_id)
                return
        except Exception:
            logger.exception(
                "Could not check for resumable paused jobs for schedule {} — falling back "
                "to spawning a new job (the previous paused one will need a manual resume).",
                schedule_id,
            )

    if job_type == "intro_credits" and cfg.get("reconcile"):
        _start_scheduled_check_servers(manager, schedule_id, priority)
        return
    if job_type == "intro_credits":
        _start_scheduled_intro_credits_job(manager, schedule_id, library_ids, library_name, priority, server_id)
        return

    if job_type == "recently_added":
        try:
            lookback = float(cfg.get("lookback_hours", 1) or 1)
        except (TypeError, ValueError):
            lookback = 1.0
        lookback = max(0.25, min(720.0, lookback))
        logger.info(
            "Executing scheduled recently-added scan: {} (library={}, lookback={:.2g}h, server={})",
            schedule_id,
            library_name or "all libraries",
            lookback,
            server_id or "(all)",
        )
        try:
            # Per-vendor processor path (Phase E): works for Plex, Emby,
            # AND Jellyfin — every vendor's processor implements
            # scan_recently_added against its native API. No fall-back to
            # the old Plex-only scanner.
            #
            # Spawn a gated Job rather than running inline on the
            # APScheduler worker thread. Pre-fix, this code called
            # _run_recently_added_multi_server directly here — that did
            # real publish work without acquiring the JobGate, so it
            # could start up alongside any number of other enumerations,
            # and it never appeared as a Job in the UI. The new helper
            # creates a Job row, waits for a start-up slot, then runs the
            # same scan.
            from .routes.job_runner import _start_recently_added_job_async

            display_label = library_name or "all libraries"
            _start_recently_added_job_async(
                schedule_id=schedule_id,
                server_id=server_id,
                library_ids=library_ids or None,
                lookback_hours=lookback,
                library_name=f"Recently added: {display_label}",
                priority=priority,
            )
            manager._update_last_run(schedule_id)
        except Exception:
            logger.exception(
                "Scheduled 'recently added' scan {} could not run. "
                "It will retry on its next scheduled tick — verify the target server is reachable and credentials are valid.",
                schedule_id,
            )
        return

    logger.info("Executing scheduled job: {} for library: {}", schedule_id, library_name)

    if manager.run_job_callback:
        try:
            # For multi-library schedules, hand off the full list via
            # config.selected_library_ids so the orchestrator processes each
            # library in one job. The legacy single-library shortcut goes via
            # library_id when there's exactly one (keeps existing behaviour).
            if len(library_ids) > 1:
                cfg = dict(cfg)
                cfg["selected_library_ids"] = sorted(set(library_ids))
            kwargs = {
                "library_id": primary_library_id,
                "library_name": library_name,
                "config": cfg,
                "parent_schedule_id": schedule_id,
            }
            if priority is not None:
                kwargs["priority"] = priority
            if server_id:
                kwargs["server_id"] = server_id
            # One unfinished scan owns this exact scheduled scope, including
            # its pauses, parked checkpoints and retries. A later tick must
            # not restart the same library beside it or clear a manual pause.
            signature = json.dumps(
                {"config": cfg, "libraries": sorted(set(library_ids)), "server_id": server_id, "priority": priority},
                sort_keys=True,
                separators=(",", ":"),
            )
            cfg["schedule_scope"] = signature
            from .jobs import JobStatus, get_job_manager

            with _FULL_LIBRARY_TICK_LOCK:
                existing = next(
                    (
                        job
                        for job in get_job_manager().get_all_jobs()
                        if job.parent_schedule_id == schedule_id
                        and job.kind == "previews"
                        and job.status in (JobStatus.PENDING, JobStatus.RUNNING)
                        and (job.config or {}).get("schedule_scope") == signature
                    ),
                    None,
                )
                if existing is None:
                    manager.run_job_callback(**kwargs)
                manager._update_last_run(schedule_id)
        except Exception:
            logger.exception(
                "Scheduled job {} for library {!r} could not start. "
                "It will retry on its next scheduled tick — check the Jobs page for any prior error details.",
                schedule_id,
                library_name or "all libraries",
            )
    else:
        logger.warning(
            "Scheduled job {} fired but no job runner is wired up — this is an internal startup issue. "
            "Restart the app; if it persists, please open an issue with the latest log lines.",
            schedule_id,
        )


class ScheduleManager:
    """Manages scheduled jobs using APScheduler.

    Provides CRUD operations for schedules with cron expression support
    and persistent storage via SQLite.
    """

    def __init__(self, config_dir: str = "/config", run_job_callback: Callable | None = None):
        """Initialize schedule manager with config directory and optional callback."""
        self.config_dir = config_dir
        self.db_path = os.path.join(config_dir, "scheduler.db")
        self.schedules_file = os.path.join(config_dir, "schedules.json")
        self.run_job_callback = run_job_callback
        self._schedules: dict[str, dict] = {}
        # Surface the most recent schedules.json load result to the API +
        # UI. Pre-fix the loader's PermissionError was logged but the
        # Schedules page rendered "no schedules" with no recovery hint
        # (live regression 2026-05-10: /config/schedules.json shipped as
        # root:root 0600 in the user's container; gunicorn ran as
        # abc:abc and silently couldn't load any schedules).
        self._load_status: dict[str, object] = {"status": "ok"}
        # Single RLock guarding ``_schedules`` mutations + reads. Without
        # this, APScheduler firing _update_last_run on multiple schedules
        # concurrently with a CRUD call (create/update/delete) could trip
        # "dictionary changed size during iteration" inside _save_schedules
        # which serialises the dict for atomic_json_save_with_backup.
        self._lock = threading.RLock()

        # Ensure config directory exists
        os.makedirs(config_dir, exist_ok=True)

        # Initialize scheduler with SQLite job store
        jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{self.db_path}")}

        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 3600,  # 1 hour grace period
            },
        )

        # Add event listeners
        self.scheduler.add_listener(self._on_job_executed, EVENT_JOB_EXECUTED)
        self.scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        self.scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)
        self.scheduler.add_listener(self._on_job_fired, EVENT_JOB_SUBMITTED | EVENT_JOB_MAX_INSTANCES)

        # Load schedule metadata
        self._load_schedules()

    def _load_schedules(self) -> None:
        """Load schedule metadata from persistent storage."""
        if os.path.exists(self.schedules_file):
            try:
                with open(self.schedules_file) as f:
                    data = json.load(f)
                raw_schedules = data.get("schedules", {}) if isinstance(data, dict) else {}

                # J4: filter per-record so one bad schedule doesn't wipe the rest.
                # Mirrors web/jobs.py:_load_jobs (Fix-5 Phase H pattern). A
                # corrupt entry is logged + skipped; valid entries still load.
                self._schedules = {}
                bad_count = 0
                for sched_id, sched in raw_schedules.items():
                    if not isinstance(sched, dict):
                        bad_count += 1
                        logger.warning(
                            "Skipping schedule {!r}: not an object on disk (got {}).",
                            sched_id,
                            type(sched).__name__,
                        )
                        continue
                    self._schedules[sched_id] = sched
                if bad_count:
                    logger.warning(
                        "Skipped {} malformed schedule record(s); valid ones loaded normally.",
                        bad_count,
                    )

                # Phase H7 migration: legacy schedules stored a single
                # ``library_id``. Promote to ``library_ids`` (list) so the
                # rest of the codebase can treat them uniformly. We do NOT
                # delete ``library_id`` — kept as a derived back-compat
                # field for one release in case any external script still
                # reads it.
                migrated = 0
                for sched in self._schedules.values():
                    if "library_ids" not in sched or not isinstance(sched.get("library_ids"), list):
                        legacy = sched.get("library_id")
                        sched["library_ids"] = [str(legacy)] if legacy else []
                        migrated += 1
                logger.info("Loaded {} schedule configurations", len(self._schedules))
                if migrated:
                    logger.info(
                        "Migrated {} legacy schedule(s) from single library_id to library_ids list",
                        migrated,
                    )
                    self._save_schedules()
                # D30 — re-register every enabled schedule with APScheduler
                # so a fresh / wiped scheduler.db doesn't leave the schedules
                # dormant. Treat schedules.json as the source of truth and
                # the SQLAlchemy jobstore as a derived/cache; rebuilding on
                # every load makes restarts robust to a scheduler.db that
                # was wiped, corrupted, or never persisted (we saw this on
                # the canary: 3 schedules in JSON, 0 jobs in apscheduler_jobs,
                # crons silently never fired). replace_existing=True is the
                # safe-merge with whatever the jobstore did persist.
                self._reregister_loaded_schedules()
            except (OSError, json.JSONDecodeError) as e:
                backup_path = self._latest_schedules_backup()
                bak_hint = (
                    f" A backup is at {backup_path} — `mv` it to {self.schedules_file} and restart to recover."
                    if backup_path
                    else ""
                )
                logger.warning(
                    "Could not read saved schedules from {} ({}: {}).{}"
                    " Starting with an empty schedule list — your existing schedules will reappear "
                    "if the file becomes readable; otherwise re-create them on the Schedules page.",
                    self.schedules_file,
                    type(e).__name__,
                    e,
                    bak_hint,
                )
                # Build a structured status block the API can hand to the UI
                # so the user gets a visible recovery hint instead of an
                # unexplained empty schedule list.
                status: dict[str, object] = {
                    "status": (
                        "permission_denied"
                        if isinstance(e, PermissionError)
                        else ("corrupt_json" if isinstance(e, json.JSONDecodeError) else "load_failed")
                    ),
                    "path": self.schedules_file,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "backup_path": backup_path,
                    "process_user": f"{os.getuid()}:{os.getgid()}",
                }
                try:
                    st = os.stat(self.schedules_file)
                    status["file_owner"] = f"{st.st_uid}:{st.st_gid}"
                    status["file_mode"] = oct(st.st_mode & 0o777)
                except OSError:
                    pass
                if isinstance(e, PermissionError):
                    suid = status.get("file_owner", "?")
                    puid = status["process_user"]
                    if backup_path:
                        status["recovery_hint"] = (
                            f"The schedules file is owned {suid} but this process runs as "
                            f"{puid}. Click 'Recover from backup' to restore from {backup_path}, "
                            f"or run `chown {puid} {self.schedules_file}` on the host."
                        )
                    else:
                        status["recovery_hint"] = (
                            f"The schedules file is owned {suid} but this process runs as "
                            f"{puid}. Run `chown {puid} {self.schedules_file}` on the host to "
                            f"restore access."
                        )
                elif isinstance(e, json.JSONDecodeError) and backup_path:
                    status["recovery_hint"] = (
                        f"The schedules file is corrupt. Click 'Recover from backup' to restore from {backup_path}."
                    )
                else:
                    status["recovery_hint"] = (
                        "Schedules failed to load. Re-create them on the Schedules page, or "
                        "restore from a backup if one is available in the config directory."
                    )
                self._load_status = status

    def _enumerate_schedules_backups(self) -> list[str]:
        """Return ``schedules.json[.<stamp>].bak`` paths newest-first.

        We rotate timestamped backups (``schedules.json.20260504-024519.bak``)
        on every save AND keep a plain ``schedules.json.bak``. Both are
        candidates for recovery; sort by mtime descending so callers get
        the closest-to-current state first.

        Live regression 2026-05-10: the user's NEWEST .bak was also
        owned root:root 0600 (same chown event that broke the live
        file). We MUST iterate, not pick a single newest, so callers
        can fall through to the next-readable backup.
        """
        try:
            cfg_dir = os.path.dirname(self.schedules_file) or "."
            stem = os.path.basename(self.schedules_file)  # "schedules.json"
        except OSError:
            return []
        candidates: list[tuple[float, str]] = []
        try:
            for name in os.listdir(cfg_dir):
                if not name.startswith(stem):
                    continue
                if not name.endswith(".bak"):
                    continue
                full = os.path.join(cfg_dir, name)
                try:
                    candidates.append((os.path.getmtime(full), full))
                except OSError:
                    continue
        except OSError:
            return []
        candidates.sort(reverse=True)
        return [path for _mtime, path in candidates]

    def _latest_schedules_backup(self) -> str | None:
        """Newest ``.bak`` file or None — used for the load-status hint.

        Doesn't filter by readability; the recovery flow does that
        per-candidate (see :meth:`recover_schedules_from_backup`).
        """
        backups = self._enumerate_schedules_backups()
        return backups[0] if backups else None

    @property
    def load_status(self) -> dict[str, object]:
        """Last result of :meth:`_load_schedules` for the API/UI to surface."""
        return dict(self._load_status)

    def recover_schedules_from_backup(self) -> dict[str, object]:
        """Atomic restore of ``schedules.json`` from the newest readable backup.

        Iterates backups newest-first. Skips any that are unreadable
        (PermissionError — typical when a host-side `chown root` event
        broke ownership on the live file AND the most-recent saves
        that inherited it) or malformed. Returns a dict the API can
        hand back to the UI describing the outcome (``ok``, count
        restored, list of attempted backups + per-attempt failure
        reason, or ``no_backup`` when nothing was usable).

        Best-effort ``os.chown`` to the runtime user — surfaces EPERM
        cleanly instead of failing the whole restore.
        """
        backups = self._enumerate_schedules_backups()
        if not backups:
            return {
                "status": "no_backup",
                "message": "No schedules.json backup file was found in the config directory.",
            }

        attempts: list[dict[str, str]] = []
        backup: str | None = None
        data: dict | None = None
        for candidate in backups:
            try:
                with open(candidate) as f:
                    parsed = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                attempts.append({"path": candidate, "error": f"{type(exc).__name__}: {exc}"})
                continue
            if not isinstance(parsed, dict) or "schedules" not in parsed:
                attempts.append(
                    {
                        "path": candidate,
                        "error": "Missing top-level 'schedules' key",
                    }
                )
                continue
            backup = candidate
            data = parsed
            break

        if backup is None or data is None:
            # All candidates failed. Pick the most-instructive failure
            # for the surface error: prefer a PermissionError (so the
            # UI hint can call out chown) over a malformed-shape one.
            primary = next(
                (a for a in attempts if "PermissionError" in a.get("error", "")),
                attempts[0] if attempts else None,
            )
            return {
                "status": "no_backup",
                "message": (
                    "Found backup file(s) but none were readable. "
                    "Most likely the same chown event that broke schedules.json also "
                    "affected the most recent backup(s) — see attempts for details."
                ),
                "primary_error": primary,
                "attempts": attempts,
                "recovery_hint": (
                    f"Run `chown {os.getuid()}:{os.getgid()} /config/schedules.json*` "
                    "on the host to restore ownership of every backup, then click "
                    "Recover from backup again."
                ),
            }

        # Atomic write: temp file in same dir, then os.replace. Avoids a
        # partially-written schedules.json if the disk fills mid-write.
        tmp = self.schedules_file + ".restore.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.schedules_file)
        except OSError as exc:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
            return {
                "status": "write_failed",
                "backup_path": backup,
                "error": f"{type(exc).__name__}: {exc}",
                "recovery_hint": (
                    "Could not write the restored file. The config directory may not be "
                    "writable by the running process — fix ownership on the host."
                ),
            }
        # Best-effort chown so a future load isn't blocked by the same issue
        # (the original failure was almost always 'wrong owner'). This is
        # expected to fail in unprivileged containers (EPERM); we surface
        # it but keep going since the restore itself succeeded.
        chown_result: dict[str, object] = {"attempted": True}
        try:
            os.chown(self.schedules_file, os.getuid(), os.getgid())
            chown_result["ok"] = True
        except (OSError, AttributeError) as exc:
            chown_result["ok"] = False
            chown_result["error"] = f"{type(exc).__name__}: {exc}"
            chown_result["hint"] = (
                f"Could not chown the restored file to {os.getuid()}:{os.getgid()} "
                "(typical in unprivileged containers). The restore still wrote successfully — "
                "if the next load fails, run `chown` on the host."
            )

        # Reload so APScheduler picks up the restored crons immediately.
        with self._lock:
            self._schedules = {}
            self._load_status = {"status": "ok"}
            self._load_schedules()

        return {
            "status": "ok",
            "backup_path": backup,
            "restored_count": len(self._schedules),
            "chown": chown_result,
        }

    def _reregister_loaded_schedules(self) -> None:
        """Re-register every enabled in-memory schedule with APScheduler (D30).

        Called from :meth:`_load_schedules` after the JSON metadata has been
        loaded. Builds a trigger from each schedule's persisted
        ``trigger_type``/``trigger_value`` and adds the job back into the
        APScheduler instance with ``replace_existing=True`` so a fresh /
        empty / corrupted ``scheduler.db`` (the SQLAlchemy jobstore) can't
        leave the schedules dormant. The schedules.json file is treated as
        the source of truth; the jobstore is just a derived cache.

        Disabled schedules and schedules with malformed trigger expressions
        are skipped with a warning — never raises so a single bad entry can
        never block boot.
        """
        if not self._schedules:
            return
        registered = 0
        skipped_bad = 0
        skipped_disabled = 0
        for sched_id, sched in self._schedules.items():
            if not sched.get("enabled"):
                skipped_disabled += 1
                continue
            try:
                trigger_type = sched.get("trigger_type")
                trigger_value = sched.get("trigger_value")
                if trigger_type == "cron":
                    trigger = CronTrigger.from_crontab(str(trigger_value or ""))
                elif trigger_type == "interval":
                    trigger = IntervalTrigger(minutes=int(trigger_value))
                else:
                    logger.warning(
                        "Schedule {!r} has unknown trigger_type {!r}; skipping re-registration.",
                        sched.get("name") or sched_id,
                        trigger_type,
                    )
                    skipped_bad += 1
                    continue

                ids_canonical = list(sched.get("library_ids") or [])
                ids_canonical = [str(x) for x in ids_canonical if str(x).strip()]
                self.scheduler.add_job(
                    execute_scheduled_job,
                    trigger=trigger,
                    id=sched_id,
                    args=[
                        sched_id,
                        ids_canonical,
                        sched.get("library_name", ""),
                        sched.get("config") or {},
                        sched.get("priority"),
                        sched.get("server_id"),
                    ],
                    replace_existing=True,
                )
                # Refresh the persisted next_run snapshot so the UI shows
                # the future fire time immediately (not the stale value
                # from the previous boot — which the canary observed as
                # "Next: 3 days ago" until the cron actually fired).
                # Compute via the trigger directly because add_job() called
                # BEFORE scheduler.start() returns a pending Job that has
                # no next_run_time attribute yet (APScheduler queues these
                # and assigns next_run_time at start time).
                try:
                    next_fire = trigger.get_next_fire_time(None, datetime.now(UTC))
                    if next_fire is not None:
                        sched["next_run"] = next_fire.isoformat()
                except Exception as exc:
                    logger.debug(
                        "next_run computation failed for schedule {!r}: {}",
                        sched.get("name") or sched_id,
                        exc,
                    )
                # Re-register the daily stop-cron (D20) if configured. _parse_hhmm
                # returns None for empty/blank, in which case we skip silently.
                stop_time = str(sched.get("stop_time") or "")
                try:
                    stop_hm = _parse_hhmm(stop_time)
                except ValueError:
                    stop_hm = None  # tolerate bad on-disk data
                if stop_hm is not None:
                    self._register_stop_job(sched_id, stop_hm)
                registered += 1
            except Exception as exc:
                logger.warning(
                    "Could not re-register schedule {!r} on startup ({}: {}); it will not fire "
                    "until edited via the UI. Other schedules are unaffected.",
                    sched.get("name") or sched_id,
                    type(exc).__name__,
                    exc,
                )
                skipped_bad += 1
        logger.info(
            "Re-registered {} schedule(s) with APScheduler ({} disabled, {} skipped due to errors)",
            registered,
            skipped_disabled,
            skipped_bad,
        )
        # Persist the refreshed next_run snapshots so the UI sees future
        # fire times immediately on next API call instead of the stale
        # value from the previous boot.
        if registered > 0:
            self._save_schedules()

    def _save_schedules(self) -> None:
        """Save schedule metadata to persistent storage.

        Caller is expected to hold ``self._lock`` (or this is the bootstrap
        load path before any concurrent firings can happen). Callers from
        the public CRUD methods all wrap save under the same lock.
        """
        try:
            from ..utils import atomic_json_save_with_backup

            # Snapshot under the lock so the dict can't mutate mid-serialisation.
            with self._lock:
                snapshot = copy.deepcopy(self._schedules)
            atomic_json_save_with_backup(self.schedules_file, {"schedules": snapshot})
        except OSError as e:
            logger.error(
                "Could not save schedules to {} ({}: {}). "
                "Your changes are still active in memory but won't survive a restart — "
                "check that the config directory is writable (Docker: confirm the volume mount permissions and PUID/PGID).",
                self.schedules_file,
                type(e).__name__,
                e,
            )

    def _on_job_fired(self, event) -> None:
        """Store a schedule's next run as soon as APScheduler moves on to it.

        APScheduler advances the job's ``next_run_time`` when it fires, but
        the ``next_run`` kept in ``schedules.json`` was refreshed only by page
        reads, so a save during the run (``_update_last_run``) stored the run
        that had just fired. APScheduler dispatches this event after it has
        stored the new ``next_run_time``.

        Saves only when the value changed: every save rotates a backup, and
        the restore list keeps only the last few.
        """
        with self._lock:
            schedule = self._schedules.get(event.job_id)
            if schedule is None:
                return
            try:
                job = self.scheduler.get_job(event.job_id)
            except Exception:
                logger.debug("Could not fetch next_run for schedule {}", event.job_id, exc_info=True)
                return
            next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
            if schedule.get("next_run") == next_run:
                return
            schedule["next_run"] = next_run
            self._save_schedules()

    def _on_job_executed(self, event) -> None:
        """Handle successful job execution."""
        logger.info("Scheduled job {} executed successfully", event.job_id)

    def _on_job_error(self, event) -> None:
        """Handle job execution error."""
        logger.error(
            "Scheduled job {} raised an error: {}. "
            "It will retry on its next scheduled tick — see earlier log lines for the underlying cause.",
            event.job_id,
            event.exception,
        )

    def _on_job_missed(self, event) -> None:
        """Handle missed job."""
        logger.warning(
            "Scheduled job {} did not run on time and was skipped. "
            "This usually means the app was offline when the schedule fired — "
            "it will run normally on the next scheduled tick.",
            event.job_id,
        )

    def start(self) -> None:
        """Start the scheduler."""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler started")

    def apply_quiet_hours(self, settings_dict: dict | None, *, drain: bool = True) -> None:
        """Re-evaluate quiet hours now and (re)register the per-minute recheck.

        ``settings_dict`` is the value of ``settings["quiet_hours"]``; legacy
        shapes are migrated by ``normalise_quiet_hours``. The recheck job
        recomputes "is any window active?" from the weekly mask every
        minute, which also recovers missed boundaries after a restart or DST
        change, so no per-window cron jobs are needed.
        """
        for job in self.scheduler.get_jobs():
            if job.id in (
                _QUIET_HOURS_RECHECK,
                _QUIET_HOURS_PAUSE_JOB_ID,
                _QUIET_HOURS_RESUME_JOB_ID,
            ) or job.id.startswith((_QUIET_HOURS_PAUSE_PREFIX, _QUIET_HOURS_RESUME_PREFIX)):
                try:
                    self.scheduler.remove_job(job.id)
                except Exception:
                    logger.debug("Could not remove quiet-hours job {}", job.id)

        qh = normalise_quiet_hours(settings_dict)
        _quiet_hours_recompute_and_apply(drain=drain)
        if not qh.get("enabled") or not qh.get("windows"):
            return

        if not self.scheduler.running:
            self.start()

        self.scheduler.add_job(
            _quiet_hours_recompute_and_apply,
            trigger=IntervalTrigger(minutes=1),
            id=_QUIET_HOURS_RECHECK,
            replace_existing=True,
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def set_run_job_callback(self, callback: Callable) -> None:
        """Set the callback function for running jobs."""
        self.run_job_callback = callback

    def _update_last_run(self, schedule_id: str) -> None:
        """Update the last run time for a schedule."""
        with self._lock:
            if schedule_id in self._schedules:
                self._schedules[schedule_id]["last_run"] = datetime.now(UTC).isoformat()
                self._save_schedules()

    def create_schedule(
        self,
        name: str,
        cron_expression: str | None = None,
        interval_minutes: int | None = None,
        library_id: str | None = None,
        library_name: str = "",
        config: dict | None = None,
        enabled: bool = True,
        priority: int | None = None,
        server_id: str | None = None,
        library_ids: list[str] | None = None,
        stop_time: str = "",
    ) -> dict:
        """Create a new schedule.

        Args:
            name: Human-readable name for the schedule
            cron_expression: Cron expression (e.g., "0 2 * * *" for 2 AM daily)
            interval_minutes: Interval in minutes (alternative to cron)
            library_id: Optional library ID to process
            library_name: Library name for display
            config: Optional configuration overrides
            enabled: Whether the schedule is enabled
            priority: Dispatch priority for jobs created by this schedule (1-3)
            server_id: Optional configured-server id this schedule targets.
                When set, jobs created by this schedule are pinned to that
                server only — important when multiple servers share a
                library name (e.g. both Plex and Emby have "Movies").
            stop_time: Optional "HH:MM" container-local time (D20). When
                set, registers a daily stop cron that pauses any RUNNING
                job spawned by this schedule. The next start tick
                resumes the paused job instead of spawning a new one,
                so a multi-night library scan can span pauses with the
                same Job ID. Empty / unset = no stop behaviour.

        Returns:
            Schedule metadata dict

        """
        schedule_id = str(uuid.uuid4())
        # Validate stop_time up front so callers see the ValueError
        # before any partial side-effects (jobstore add, json save).
        stop_hm = _parse_hhmm(stop_time)

        # Create trigger
        if cron_expression:
            trigger = self._build_trigger("cron", cron_expression)
            trigger_type = "cron"
            trigger_value = cron_expression
        elif interval_minutes:
            trigger = self._build_trigger("interval", str(interval_minutes))
            trigger_type = "interval"
            trigger_value = str(interval_minutes)
            # stop_time only makes sense for time-of-day triggers.
            stop_hm = None
            stop_time = ""
        else:
            raise ValueError("Either cron_expression or interval_minutes must be provided")

        # Multi-select libraries (Phase H7). Canonical store is library_ids
        # (a list); library_id is kept as a derived back-compat field for any
        # legacy reader that hasn't migrated yet.
        ids_canonical = list(library_ids) if library_ids else ([library_id] if library_id else [])
        ids_canonical = [str(x) for x in ids_canonical if str(x).strip()]
        single_id = ids_canonical[0] if len(ids_canonical) == 1 else None

        # Store metadata
        schedule_meta = {
            "id": schedule_id,
            "name": name,
            "trigger_type": trigger_type,
            "trigger_value": trigger_value,
            "library_id": single_id,
            "library_ids": ids_canonical,
            "library_name": library_name,
            "server_id": server_id,
            "config": config or {},
            "enabled": enabled,
            "created_at": datetime.now(UTC).isoformat(),
            "last_run": None,
            "next_run": None,
            "priority": priority,
            "stop_time": stop_time or "",
        }

        # Ensure scheduler is running
        if not self.scheduler.running:
            self.start()

        # Add job to scheduler if enabled
        if enabled:
            job = self.scheduler.add_job(
                execute_scheduled_job,
                trigger=trigger,
                id=schedule_id,
                args=[schedule_id, ids_canonical, library_name, config, priority, server_id],
                replace_existing=True,
            )
            schedule_meta["next_run"] = job.next_run_time.isoformat() if job.next_run_time else None
            if stop_hm is not None:
                self._register_stop_job(schedule_id, stop_hm)

        with self._lock:
            self._schedules[schedule_id] = schedule_meta
            self._save_schedules()

        logger.info("Created schedule '{}' (ID: {})", name, schedule_id)
        return schedule_meta

    def _stop_job_id(self, schedule_id: str) -> str:
        """APScheduler job id for the per-schedule stop-cron (D20)."""
        return f"{schedule_id}__stop"

    def _register_stop_job(self, schedule_id: str, stop_hm: tuple[int, int]) -> None:
        """Add the daily stop-cron for ``schedule_id`` (D20)."""
        hour, minute = stop_hm
        self.scheduler.add_job(
            execute_schedule_stop,
            trigger=CronTrigger(hour=hour, minute=minute),
            id=self._stop_job_id(schedule_id),
            args=[schedule_id],
            replace_existing=True,
        )

    def _remove_stop_job(self, schedule_id: str) -> None:
        """Best-effort removal of the per-schedule stop-cron (D20)."""
        try:
            self.scheduler.remove_job(self._stop_job_id(schedule_id))
        except Exception:
            logger.debug("No stop-cron to remove for schedule {}", schedule_id)

    #: Distinguishes "caller omitted priority" from "caller passed null to
    #: clear the pin". A plain ``None`` default cannot express both, and
    #: clearing is what lets a schedule fall back to the global default.
    _PRIORITY_UNSET = object()

    def update_schedule(
        self,
        schedule_id: str,
        name: str | None = None,
        cron_expression: str | None = None,
        interval_minutes: int | None = None,
        library_id: str | None = None,
        library_name: str | None = None,
        config: dict | None = None,
        enabled: bool | None = None,
        priority: int | None | object = _PRIORITY_UNSET,
        server_id: str | None = None,
        library_ids: list[str] | None = None,
        stop_time: str | None = None,
    ) -> dict | None:
        """Update an existing schedule.

        ``stop_time``: pass an "HH:MM" string to set, "" to clear, or
        ``None`` to leave unchanged. D20.

        ``priority``: pass 1/2/3 to pin, ``None`` to clear the pin (the
        job then takes the global default — for Recently Added sweeps
        that is the ``incoming_job_priority`` setting, issue #285), or
        omit the argument entirely to leave it unchanged.
        """
        with self._lock:
            if schedule_id not in self._schedules:
                return None

            stored = self._schedules[schedule_id]
            # Edit a copy so invalid input (bad cron, bad stop_time) leaves the
            # stored schedule and its APScheduler job untouched.
            schedule = copy.deepcopy(stored)

            if name is not None:
                schedule["name"] = name
            if library_ids is not None:
                # Canonical multi-select store. Also mirror to library_id for
                # any downstream that hasn't migrated.
                ids = [str(x) for x in (library_ids or []) if str(x).strip()]
                schedule["library_ids"] = ids
                schedule["library_id"] = ids[0] if len(ids) == 1 else None
            elif library_id is not None:
                # Single-library back-compat path.
                schedule["library_id"] = library_id
                schedule["library_ids"] = [str(library_id)] if library_id else []
            if library_name is not None:
                schedule["library_name"] = library_name
            resumes_other_jobs = config is not None and (
                _resumed_kind(config) != _resumed_kind(stored.get("config"))
                or (config.get("job_type") == "recently_added")
                != ((stored.get("config") or {}).get("job_type") == "recently_added")
            )
            if config is not None:
                schedule["config"] = config
            if enabled is not None:
                schedule["enabled"] = enabled
            if priority is not ScheduleManager._PRIORITY_UNSET:
                schedule["priority"] = priority
            if server_id is not None:
                # Empty string means "clear the pin", null means "leave alone".
                schedule["server_id"] = server_id or None
            if stop_time is not None:
                _parse_hhmm(stop_time)  # raises ValueError, surfaced as a 400
                schedule["stop_time"] = stop_time or ""

            if cron_expression is not None:
                schedule["trigger_type"] = "cron"
                schedule["trigger_value"] = cron_expression
            elif interval_minutes is not None:
                schedule["trigger_type"] = "interval"
                schedule["trigger_value"] = str(interval_minutes)
                # stop_time is meaningless for interval triggers; clear it so
                # changing trigger type doesn't leave an orphan stop cron.
                schedule["stop_time"] = ""

            trigger = None
            if schedule["enabled"]:
                trigger = self._build_trigger(schedule["trigger_type"], schedule["trigger_value"])
            stop_hm = _parse_hhmm(schedule.get("stop_time") or "")

            # Everything validated; now swap the jobs.
            try:
                self.scheduler.remove_job(schedule_id)
            except Exception:
                logger.debug("No existing scheduler job to remove for {}", schedule_id)
            self._remove_stop_job(schedule_id)

            if trigger is not None:
                job = self.scheduler.add_job(
                    execute_scheduled_job,
                    trigger=trigger,
                    id=schedule_id,
                    args=[
                        schedule_id,
                        schedule.get("library_ids", []),
                        schedule["library_name"],
                        schedule["config"],
                        schedule.get("priority"),
                        schedule.get("server_id"),
                    ],
                    replace_existing=True,
                )
                schedule["next_run"] = job.next_run_time.isoformat() if job.next_run_time else None
                if stop_hm is not None and schedule["trigger_type"] == "cron":
                    self._register_stop_job(schedule_id, stop_hm)
            else:
                schedule["next_run"] = None

            stored.clear()
            stored.update(schedule)
            self._save_schedules()

        logger.info("Updated schedule {}", schedule_id)
        if resumes_other_jobs:
            _warn_stop_paused_jobs_left_behind(schedule_id, schedule.get("name", schedule_id), schedule["config"])
        return stored

    @staticmethod
    def _build_trigger(trigger_type: str, trigger_value: str):
        """Build the APScheduler trigger for a schedule, raising ValueError if invalid."""
        if trigger_type == "cron":
            return CronTrigger.from_crontab(trigger_value)
        minutes = int(trigger_value)
        if minutes <= 0:
            raise ValueError("interval_minutes must be a positive integer")
        return IntervalTrigger(minutes=minutes)

    def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule."""
        with self._lock:
            if schedule_id not in self._schedules:
                return False

            # Remove from scheduler (may not exist if schedule was disabled)
            try:
                self.scheduler.remove_job(schedule_id)
            except Exception:
                logger.debug("No existing scheduler job to remove for {}", schedule_id)
            self._remove_stop_job(schedule_id)

            name = self._schedules[schedule_id].get("name", schedule_id)
            del self._schedules[schedule_id]
            self._save_schedules()

            logger.info("Deleted schedule {}", schedule_id)
        _warn_paused_jobs_of_deleted_schedule(schedule_id, name)
        return True

    def get_schedule(self, schedule_id: str) -> dict | None:
        """Get a schedule by ID."""
        schedule = self._schedules.get(schedule_id)
        if schedule:
            try:
                job = self.scheduler.get_job(schedule_id)
                if job and job.next_run_time:
                    schedule["next_run"] = job.next_run_time.isoformat()
            except Exception:
                logger.debug("Could not fetch next_run for schedule {}", schedule_id)
        return schedule

    def get_all_schedules(self) -> list[dict]:
        """Get all schedules."""
        with self._lock:
            entries = list(self._schedules.items())
        schedules = []
        for schedule_id, schedule in entries:
            try:
                job = self.scheduler.get_job(schedule_id)
                if job and job.next_run_time:
                    schedule["next_run"] = job.next_run_time.isoformat()
            except Exception:
                logger.debug("Could not fetch next_run for schedule {}", schedule_id)
            schedules.append(schedule)
        return schedules

    def enable_schedule(self, schedule_id: str) -> dict | None:
        """Enable a schedule."""
        return self.update_schedule(schedule_id, enabled=True)

    def disable_schedule(self, schedule_id: str) -> dict | None:
        """Disable a schedule."""
        return self.update_schedule(schedule_id, enabled=False)

    def run_now(self, schedule_id: str) -> bool:
        """Run a schedule immediately."""
        schedule = self._schedules.get(schedule_id)
        if not schedule:
            return False

        logger.info("Running schedule '{}' now", schedule["name"])
        ids = schedule.get("library_ids") or ([schedule["library_id"]] if schedule.get("library_id") else [])
        execute_scheduled_job(
            schedule_id,
            ids,
            schedule.get("library_name", ""),
            schedule.get("config"),
            schedule.get("priority"),
            schedule.get("server_id"),
            ignore_pause=True,  # explicit user action: "Run now" must not silently no-op while paused
        )
        with self._lock:
            self._save_schedules()
        return True


# Global scheduler instance
_schedule_manager: ScheduleManager | None = None
_schedule_lock = threading.Lock()


def get_schedule_manager(config_dir: str | None = None, run_job_callback: Callable | None = None) -> ScheduleManager:
    """Get or create the global ScheduleManager instance (thread-safe)."""
    global _schedule_manager
    with _schedule_lock:
        if _schedule_manager is None:
            _schedule_manager = ScheduleManager(
                config_dir=config_dir or os.environ.get("CONFIG_DIR", "/config"),
                run_job_callback=run_job_callback,
            )
        elif run_job_callback and _schedule_manager.run_job_callback is None:
            _schedule_manager.set_run_job_callback(run_job_callback)
        return _schedule_manager


def schedule_name(schedule_id: str) -> str | None:
    """A schedule's name from the running schedule manager, without creating one.

    Args:
        schedule_id: The schedule's id.

    Returns:
        Its name; None when no manager is running or it has no such schedule.
    """
    manager = _schedule_manager
    if manager is None or not schedule_id:
        return None
    with manager._lock:
        schedule = manager._schedules.get(schedule_id)
    return str(schedule.get("name") or "") or None if schedule else None
