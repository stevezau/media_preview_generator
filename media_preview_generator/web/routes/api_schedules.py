"""Schedule management API routes."""

from flask import current_app, jsonify, request
from loguru import logger

from ...library_health.runner import CATCH_UP_MAX_FILES, get_runner
from ...scan_filters import normalize_scan_filter_config
from ..auth import api_token_required
from ..builtin_jobs import (
    CHECK_SERVERS_ID,
    LIBRARY_HEALTH_ID,
    load_builtin_jobs,
    save_builtin_job,
    validate_builtin_update,
)
from ..scheduler import (
    _QUIET_HOURS_DAYS,
    _parse_hhmm,
    _resume_orphaned_check_servers_jobs,
    get_schedule_manager,
    is_now_in_any_quiet_window,
    normalise_quiet_hours,
    normalize_schedule_config,
)
from ..settings_manager import get_settings_manager
from . import api


def _normalised_config(raw_config):
    """A request's schedule config in the current shape, with scan filters kept only for whole-library schedules.

    Raises:
        ValueError: The config isn't an object, or its job kinds or filters are invalid.
    """
    if not isinstance(raw_config, dict):
        raise ValueError("config must be an object")
    config = normalize_schedule_config(raw_config)
    return normalize_scan_filter_config(config, full_scan=config["job_type"] == "full_library")


@api.route("/schedules")
@api_token_required
def get_schedules():
    """Get all schedules.

    Includes a ``load_status`` block so the UI can surface a recovery
    banner when ``schedules.json`` failed to load (the typical cause
    is wrong file ownership in the user's container — pre-fix the
    user just saw an empty list with no hint that their schedules
    existed but were unreadable).
    """
    schedule_manager = get_schedule_manager()
    return jsonify(
        {
            "schedules": schedule_manager.get_all_schedules(),
            "load_status": schedule_manager.load_status,
        }
    )


@api.route("/schedules/recover_from_backup", methods=["POST"])
@api_token_required
def recover_schedules_from_backup():
    """Restore ``schedules.json`` from the newest backup file.

    Used by the Schedules page's recovery banner when the original
    file failed to load (PermissionError / corrupt JSON). Returns the
    outcome ({status: "ok"|"no_backup"|"backup_unreadable"|"backup_invalid"|"write_failed"})
    plus details so the UI can show a clear next-step message.
    """
    schedule_manager = get_schedule_manager()
    result = schedule_manager.recover_schedules_from_backup()
    http_status = 200 if result.get("status") == "ok" else 409
    return jsonify(result), http_status


@api.route("/schedules/<schedule_id>")
@api_token_required
def get_schedule(schedule_id):
    """Get a specific schedule."""
    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.get_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules", methods=["POST"])
@api_token_required
def create_schedule():
    """Create a new schedule."""
    data = request.get_json() or {}
    if "config" in data and data["config"] is not None:
        try:
            data["config"] = _normalised_config(data["config"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    if not data.get("name"):
        return jsonify({"error": "Name is required"}), 400

    if not data.get("cron_expression") and not data.get("interval_minutes"):
        return jsonify({"error": "Either cron_expression or interval_minutes is required"}), 400

    # Both schedule types work for every vendor: full-library scans go
    # through _run_full_scan_multi_server and recently-added through
    # _run_recently_added_multi_server, both of which dispatch via the
    # per-vendor VendorProcessor.
    try:
        schedule_manager = get_schedule_manager()
        schedule = schedule_manager.create_schedule(
            name=data["name"],
            cron_expression=data.get("cron_expression"),
            interval_minutes=data.get("interval_minutes"),
            library_id=data.get("library_id"),
            library_ids=data.get("library_ids"),
            library_name=data.get("library_name", ""),
            config=data.get("config", {}),
            enabled=data.get("enabled", True),
            priority=data.get("priority"),
            server_id=data.get("server_id") or None,
            stop_time=str(data.get("stop_time") or "").strip(),
        )
        return jsonify(schedule), 201
    except ValueError as e:
        logger.warning(
            "New schedule rejected — invalid parameters ({}: {}). "
            "Common causes are an empty name, missing trigger (need either a cron expression "
            "or an interval in minutes), or a malformed cron syntax.",
            type(e).__name__,
            e,
        )
        return jsonify({"error": f"Invalid schedule parameters: {e}"}), 400
    except Exception as e:
        # A read-only /config makes APScheduler's INSERT into scheduler.db
        # fail with "attempt to write a readonly database" — nothing to do
        # with cron syntax. Detect it and give the actionable permissions fix
        # instead of the misleading duplicate/cron hint (issue #278).
        if "readonly database" in str(e).lower() or "unable to open database" in str(e).lower():
            from ..config_health import probe_config_health

            health = probe_config_health(current_app.config.get("CONFIG_DIR", "/config"))
            # The re-probe may transiently disagree with the DB write that just
            # failed (race, or config made writable in the interim) — never emit
            # a blank 503; fall back to the DB error's own message.
            detail = health["detail"] or "The config folder isn't writable, so the schedule can't be saved."
            hint = health["hint"] or (
                "Make the config folder writable by the container (check PUID/PGID and that the "
                "mount isn't read-only), then try again."
            )
            logger.error(
                "Could not save the new schedule {!r} — the config folder isn't writable. {} {}",
                data.get("name", "<unnamed>"),
                detail,
                hint,
            )
            return jsonify({"error": detail, "hint": hint, "config_health": health}), 503
        logger.exception(
            "Could not save the new schedule {!r} ({}: {}). "
            "Most often this is a malformed cron expression or a clash with an existing schedule — "
            "check the cron syntax (e.g. '0 3 * * *' for 3am daily) and the schedules list for duplicates.",
            data.get("name", "<unnamed>"),
            type(e).__name__,
            e,
        )
        return jsonify({"error": "Failed to create schedule"}), 500


@api.route("/schedules/<schedule_id>", methods=["PUT"])
@api_token_required
def update_schedule(schedule_id):
    """Update a schedule."""
    data = request.get_json() or {}
    if "config" in data and data["config"] is not None:
        try:
            data["config"] = _normalised_config(data["config"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    schedule_manager = get_schedule_manager()
    try:
        # stop_time: pass through only when the client included the
        # field (None = leave alone; "" = clear; "HH:MM" = set).
        update_kwargs = dict(
            schedule_id=schedule_id,
            name=data.get("name"),
            cron_expression=data.get("cron_expression"),
            interval_minutes=data.get("interval_minutes"),
            library_id=data.get("library_id"),
            library_ids=data.get("library_ids"),
            library_name=data.get("library_name"),
            config=data.get("config"),
            enabled=data.get("enabled"),
            server_id=data.get("server_id"),
        )
        # priority: absent = leave alone; null = clear the pin (fall back to
        # the global default); 1|2|3 = pin. Forwarding ``data.get("priority")``
        # unconditionally would make "absent" and "null" indistinguishable,
        # so an unpinned schedule could never be saved.
        if "priority" in data:
            update_kwargs["priority"] = data.get("priority")
        if "stop_time" in data:
            update_kwargs["stop_time"] = str(data.get("stop_time") or "").strip()
        schedule = schedule_manager.update_schedule(**update_kwargs)
    except ValueError as e:
        # APScheduler's CronTrigger.from_crontab raises ValueError on malformed
        # input. Surface a friendly 400 instead of the generic 500 the
        # framework would otherwise emit, mirroring create_schedule's contract.
        logger.warning(
            "Schedule {} update rejected — invalid parameters ({}: {}). "
            "Common causes: malformed cron syntax, or both cron_expression and "
            "interval_minutes set to mutually-incompatible values.",
            schedule_id,
            type(e).__name__,
            e,
        )
        return jsonify({"error": f"Invalid schedule parameters: {e}"}), 400

    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>", methods=["DELETE"])
@api_token_required
def delete_schedule(schedule_id):
    """Delete a schedule."""
    schedule_manager = get_schedule_manager()
    if schedule_manager.delete_schedule(schedule_id):
        return jsonify({"success": True})
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>/enable", methods=["POST"])
@api_token_required
def enable_schedule(schedule_id):
    """Enable a schedule."""
    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.enable_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>/disable", methods=["POST"])
@api_token_required
def disable_schedule(schedule_id):
    """Disable a schedule."""
    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.disable_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>/run", methods=["POST"])
@api_token_required
def run_schedule_now(schedule_id):
    """Run a schedule immediately.

    Like Start job, the job is created even while processing is paused; the pause holds it until processing
    resumes, so the response says ``paused: true`` in that case.
    """
    schedule_manager = get_schedule_manager()
    if schedule_manager.run_now(schedule_id):
        return jsonify({"success": True, "paused": bool(get_settings_manager().processing_paused)})
    return jsonify({"error": "Schedule not found"}), 404


def _builtin_job_dict(job_id: str) -> dict:
    """One built-in job as the Schedules page shows it, with its next run from the live scheduler."""
    from ...markers.triggers import markers_enabled_anywhere
    from ..scheduler import _LIBRARY_HEALTH_NIGHTLY, _MARKERS_CHECK_SERVERS_DAILY, job_next_run

    settings = load_builtin_jobs()[job_id]
    if job_id == LIBRARY_HEALTH_ID:
        next_run = job_next_run(_LIBRARY_HEALTH_NIGHTLY)
        return {
            "id": job_id,
            "name": "Library Health check",
            **settings,
            "catch_up_max_files": CATCH_UP_MAX_FILES,
            "next_run": next_run.isoformat() if next_run else None,
        }
    next_run = job_next_run(_MARKERS_CHECK_SERVERS_DAILY)
    return {
        "id": job_id,
        "name": "Check servers",
        **settings,
        "intro_credits_on": markers_enabled_anywhere(),
        "next_run": next_run.isoformat() if next_run else None,
    }


@api.route("/schedules/builtin")
@api_token_required
def get_builtin_jobs():
    """The app's built-in daily jobs (Library Health check, Check servers) with their saved settings."""
    return jsonify({"jobs": [_builtin_job_dict(LIBRARY_HEALTH_ID), _builtin_job_dict(CHECK_SERVERS_ID)]})


@api.route("/schedules/builtin/<job_id>", methods=["PUT"])
@api_token_required
def update_builtin_job(job_id):
    """Change a built-in job's time or switches; the body holds only the fields to change."""
    try:
        changes = validate_builtin_update(job_id, request.get_json(silent=True))
    except KeyError:
        return jsonify({"error": "Built-in job not found"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # One lock across save and apply, so two quick saves can't register the older settings last.
    with get_settings_manager().locked():
        save_builtin_job(job_id, changes)
        get_schedule_manager().apply_builtin_jobs()
    return jsonify(_builtin_job_dict(job_id))


@api.route("/schedules/builtin/<job_id>/run", methods=["POST"])
@api_token_required
def run_builtin_job_now(job_id):
    """Run a built-in job now, the same way its daily run does."""
    if job_id == LIBRARY_HEALTH_ID:
        started, _progress = get_runner().start_check("nightly")
        if not started:
            return jsonify({"error": "A Library Health check is already running"}), 409
    elif job_id == CHECK_SERVERS_ID:
        from ...markers.reconcile import run_markers_reconcile

        _resume_orphaned_check_servers_jobs()
        queued = run_markers_reconcile()
        if queued.job_id is None:
            return jsonify({"error": "Intro & Credits is off on every server, so there is nothing to check"}), 409
        return jsonify({"ok": True, "job_id": queued.job_id, "reused": not queued.created})
    else:
        return jsonify({"error": "Built-in job not found"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Quiet Hours (D21 + D26) — global queue pause/resume schedule
#
# D26 expanded the single-window D21 model to support multiple windows
# each with a per-day-of-week filter. Persisted shape:
#   {"enabled": bool,
#    "windows": [{"start": "HH:MM", "end": "HH:MM",
#                 "days": ["mon","tue",...]}]}
# Legacy single-window payloads {enabled, start, end} are normalised on
# read in the scheduler module so a settings.json from D21 keeps working.
# ---------------------------------------------------------------------------


def _quiet_hours_payload(qh: dict | None) -> dict:
    """Return the normalised multi-window shape + the currently-active flag."""
    normalised = normalise_quiet_hours(qh)
    return {
        "enabled": normalised["enabled"],
        "windows": normalised["windows"],
        "day_basis": "start",
        "currently_in_quiet_window": is_now_in_any_quiet_window(normalised),
    }


def _validate_window(raw: object, idx: int) -> dict:
    """Validate a single window dict, raising ValueError with a 1-indexed label."""
    if not isinstance(raw, dict):
        raise ValueError(f"Window #{idx + 1}: must be an object")
    start = str(raw.get("start") or "").strip()
    end = str(raw.get("end") or "").strip()
    if not start or not end:
        raise ValueError(f"Window #{idx + 1}: start and end are required")
    try:
        _parse_hhmm(start)
        _parse_hhmm(end)
    except ValueError as exc:
        raise ValueError(f"Window #{idx + 1}: {exc}") from exc
    raw_days = raw.get("days")
    if raw_days is None:
        days = list(_QUIET_HOURS_DAYS)
    else:
        if not isinstance(raw_days, list):
            raise ValueError(f"Window #{idx + 1}: days must be a list")
        days = [str(d).strip().lower() for d in raw_days]
        bad = [d for d in days if d not in _QUIET_HOURS_DAYS]
        if bad:
            raise ValueError(f"Window #{idx + 1}: invalid day(s) {bad}")
        if not days:
            days = list(_QUIET_HOURS_DAYS)
    return {"start": start, "end": end, "days": days}


@api.route("/quiet-hours")
@api_token_required
def get_quiet_hours():
    """Return the current quiet-hours config (D21 + D26)."""
    sm = get_settings_manager()
    return jsonify(_quiet_hours_payload(sm.get("quiet_hours")))


@api.route("/quiet-hours", methods=["POST"])
@api_token_required
def update_quiet_hours():
    """Update the quiet-hours config and re-register the boundary crons.

    Body (D26 multi-window form):
        ``{"enabled": bool,
           "windows": [{"start": "HH:MM", "end": "HH:MM",
                        "days": ["mon","tue",...]}]}``

    Backwards-compat: a body with top-level ``start``/``end`` is accepted
    and treated as a single all-week window (so old front-ends keep
    working through a deploy gap).
    """
    data = request.get_json() or {}
    enabled = bool(data.get("enabled"))

    raw_windows = data.get("windows")
    if raw_windows is None:
        # Legacy single-window body — accept and migrate.
        start = str(data.get("start") or "").strip()
        end = str(data.get("end") or "").strip()
        if start and end:
            raw_windows = [{"start": start, "end": end, "days": list(_QUIET_HOURS_DAYS)}]
        else:
            raw_windows = []

    if not isinstance(raw_windows, list):
        return jsonify({"error": "windows must be a list"}), 400

    try:
        windows = [_validate_window(w, i) for i, w in enumerate(raw_windows)]
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    qh = {"enabled": enabled, "windows": windows, "day_basis": "start"}
    sm = get_settings_manager()
    sm.set("quiet_hours", qh)

    schedule_manager = get_schedule_manager()
    try:
        schedule_manager.apply_quiet_hours(qh)
    except Exception:
        logger.exception("Could not apply quiet-hours cron registration")
        return jsonify({"error": "Could not apply quiet-hours schedule"}), 500

    return jsonify(_quiet_hours_payload(qh))
