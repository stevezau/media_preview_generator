"""Regression test: webhook job overrides MUST carry the trigger source.

Live bug 2026-05-10 caught during double-check verification. The retry-
chain row for "World's Most Secret Hotels S02E04" displayed:

  * the RAW filename (``World's Most Secret Hotels (2023) - S02E04 -
    Episode 4 [WEBDL-1080p][AAC 2.0][h264]-RAWR.mkv``) instead of the
    cleaned dispatcher title (``World's Most Secret Hotels S02E04``)
  * NO source pill (no Sonarr / Plex chip)

despite the parent dispatch job carrying both correctly. Root cause:
``create_vendor_webhook_job`` (Plex/Emby/Jellyfin direct webhooks) and
``_schedule_webhook_job`` (Sonarr/Radarr/Sportarr/custom debounced
webhooks) both stored ``source`` in ``job.config`` (so the parent row
rendered the pill) but did NOT add ``source`` to the ``overrides``
dict handed to ``_start_job_async``. The job runner's
``_apply_overrides`` pass only iterates ``overrides``, so my new
``"source"`` handler — which copies the value onto
``Config.webhook_source`` — never fired. Worker handed None →
chain row fell back to raw filename + no pill.

These tests pin BOTH webhook-creation paths so the contract is
enforced at the unit level — no need to spin up a real container to
catch the regression next time.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.webhooks import (
    create_vendor_webhook_job,
)


@pytest.fixture
def captured_overrides(monkeypatch):
    """Patch ``_start_job_async`` and capture every (job_id, overrides) call."""
    captured: list[dict] = []

    def fake_start(job_id, overrides):
        captured.append({"job_id": job_id, "overrides": dict(overrides or {})})

    # _start_job_async is imported lazily inside the webhook helpers via
    # `from .routes import _start_job_async`, so monkeypatch the source
    # module attribute (which the helpers will re-import on each call).
    monkeypatch.setattr(
        "media_preview_generator.web.routes._start_job_async",
        fake_start,
    )
    return captured


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """Each test gets its own settings/config dir so they don't bleed."""
    import media_preview_generator.web.settings_manager as sm_mod

    sm_mod._settings_manager = None
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    yield
    sm_mod._settings_manager = None


@pytest.fixture(autouse=True)
def _isolate_jobs(tmp_path):
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


class TestVendorWebhookJobCarriesSourceInOverrides:
    """``create_vendor_webhook_job`` is the immediate-dispatch path
    (Plex / Emby / Jellyfin direct webhook receivers). It must put
    ``source`` in BOTH ``job.config`` (UI badge) AND the overrides
    dict (Config plumbing for retry-chain rows).
    """

    @pytest.mark.parametrize("source", ["plex", "emby", "jellyfin"])
    def test_source_appears_in_overrides_for_vendor_dispatch(self, source, captured_overrides):
        # NOTE: ``create_vendor_webhook_job`` returns the new job's ID
        # (a string), not the Job object — see webhooks.py:457
        # ``return job.id``. Don't try to access .id on the return value.
        with patch("media_preview_generator.web.webhooks._check_and_record_dedup", return_value=None):
            job_id = create_vendor_webhook_job(
                source=source,
                title=f"Test {source.title()} Item",
                canonical_path="/data/Movies/Foo (2024)/Foo (2024).mkv",
                server_id=None,
            )
        assert job_id, "Webhook job creation should not be deduped under a fresh state"
        assert captured_overrides, "create_vendor_webhook_job MUST hand the job to _start_job_async"
        last = captured_overrides[-1]
        assert last["job_id"] == job_id
        assert last["overrides"].get("source") == source, (
            f"overrides.source MUST be {source!r} so the job runner's _apply_overrides pass "
            f"copies it onto Config.webhook_source. Pre-fix this was missing → worker handed "
            f"None to process_canonical_path → spawned retry-chain rows fell back to the raw "
            f"filename and rendered no source pill (live regression 2026-05-10 against "
            f"World's Most Secret Hotels S02E04). Got overrides={last['overrides']!r}"
        )

    def test_source_also_persisted_on_job_config(self, captured_overrides):
        """The Job dataclass needs source for the queue-table badge.
        Both surfaces (config + overrides) must carry it."""
        from media_preview_generator.web.jobs import get_job_manager

        with patch("media_preview_generator.web.webhooks._check_and_record_dedup", return_value=None):
            job_id = create_vendor_webhook_job(
                source="plex",
                title="Test",
                canonical_path="/data/Movies/Bar (2024)/Bar (2024).mkv",
                server_id=None,
            )
        job = get_job_manager().get_job(job_id)
        assert job is not None
        assert (job.config or {}).get("source") == "plex"


class TestSchedulesWebhookJobCarriesSourceInOverrides:
    """``_schedule_webhook_job`` is the debounced-batch path
    (Sonarr / Radarr / Sportarr / Tdarr / custom webhooks). Same
    contract — source MUST appear in overrides.
    """

    @pytest.mark.parametrize("source", ["sonarr", "radarr", "sportarr", "custom"])
    def test_source_appears_in_overrides_for_debounced_dispatch(self, source, captured_overrides):
        # _schedule_webhook_job uses a debounce timer — for the test we
        # synchronously fire the batch by patching the timer.
        from media_preview_generator.web import webhooks as wh

        # Monkey-patch the debounce delay to 0 so the batch fires immediately.
        # _check_and_record_dedup returns None for "fresh" / int age for "duplicate".
        # Force fresh.
        with (
            patch.object(wh, "_check_and_record_dedup", return_value=None),
            patch("media_preview_generator.web.webhooks.threading.Timer") as TimerMock,
        ):
            # threading.Timer(delay, fn, args=[...]).start() — capture
            # the wrapped invocation so we can fire it synchronously
            # instead of waiting for the real thread.
            captured_fns: list = []

            def fake_timer(_delay, fn, *positional, args=None, kwargs=None):
                t = MagicMock()
                # threading.Timer's run() invokes fn(*args, **kwargs).
                t.start = lambda: captured_fns.append((fn, list(positional) + list(args or []), kwargs or {}))
                return t

            TimerMock.side_effect = fake_timer

            queued = wh._schedule_webhook_job(
                source,
                "World's Most Secret Hotels S02E04",
                "/data/TV Shows/World's Most Secret Hotels (2023)/Season 02/Episode 4.mkv",
            )
            assert queued is True, "Webhook should be queued under a fresh dedup state"

            # Fire the captured timer callback synchronously.
            assert captured_fns, "Timer callback should have been scheduled"
            fn, fn_args, fn_kwargs = captured_fns[-1]
            fn(*fn_args, **fn_kwargs)

        assert captured_overrides, "_schedule_webhook_job MUST hand the job to _start_job_async"
        last = captured_overrides[-1]
        assert last["overrides"].get("source") == source, (
            f"overrides.source MUST be {source!r}. Pre-fix this was missing on the "
            f"_schedule_webhook_job path AND the create_vendor_webhook_job path; both have "
            f"now been fixed to forward source through to Config.webhook_source. "
            f"Got overrides={last['overrides']!r}"
        )


class TestWebhookJobsUseIncomingPriority:
    """Issue #285 — both webhook-creation paths must stamp the configured
    "Incoming job priority" onto the Job they create.

    Without it the Job lands at Normal, ties with the running full scan on
    ``(priority, submission_order)``, and loses the tiebreak to the scan's
    earlier submission — the reporter's exact complaint. Both paths are
    covered because they build their ``create_job`` call independently;
    fixing one and forgetting the other leaves Sonarr/Radarr users (or
    Plex/Emby/Jellyfin users) still stuck behind the scan.
    """

    @staticmethod
    def _fire_debounced(source: str, path: str, before_fire=None) -> None:
        """Run ``_schedule_webhook_job`` and its debounce callback inline.

        ``before_fire`` runs between the two, i.e. during the window where
        the batch is open but the timer has not yet fired.
        """
        from media_preview_generator.web import webhooks as wh

        with (
            patch.object(wh, "_check_and_record_dedup", return_value=None),
            patch("media_preview_generator.web.webhooks.threading.Timer") as TimerMock,
        ):
            captured_fns: list = []

            def fake_timer(_delay, fn, *positional, args=None, kwargs=None):
                t = MagicMock()
                t.start = lambda: captured_fns.append((fn, list(positional) + list(args or []), kwargs or {}))
                return t

            TimerMock.side_effect = fake_timer
            assert wh._schedule_webhook_job(source, "Some Show S01E01", path) is True
            if before_fire is not None:
                before_fire()
            assert captured_fns, "Timer callback should have been scheduled"
            fn, fn_args, fn_kwargs = captured_fns[-1]
            fn(*fn_args, **fn_kwargs)

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (None, 1),  # unset → High, the shipped default
            (1, 1),
            (2, 2),
            (3, 3),
            ("low", 3),  # label form, as a hand-edited settings.json might hold
        ],
    )
    def test_vendor_webhook_job_gets_configured_priority(self, configured, expected, captured_overrides):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        if configured is not None:
            get_settings_manager().set("incoming_job_priority", configured)

        with patch("media_preview_generator.web.webhooks._check_and_record_dedup", return_value=None):
            job_id = create_vendor_webhook_job(
                source="plex",
                title="Test",
                canonical_path="/data/Movies/Baz (2024)/Baz (2024).mkv",
                server_id=None,
            )

        job = get_job_manager().get_job(job_id)
        assert job is not None
        assert job.priority == expected, (
            f"Vendor webhook job must be created at the configured incoming priority "
            f"({configured!r} → {expected}); got {job.priority}"
        )

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (None, 1),
            (2, 2),
            (3, 3),
        ],
    )
    def test_debounced_webhook_job_gets_configured_priority(self, configured, expected, captured_overrides):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        if configured is not None:
            get_settings_manager().set("incoming_job_priority", configured)

        self._fire_debounced("sonarr", "/data/TV Shows/Some Show/Season 01/S01E01.mkv")

        assert captured_overrides, "_schedule_webhook_job MUST hand the job to _start_job_async"
        job = get_job_manager().get_job(captured_overrides[-1]["job_id"])
        assert job is not None
        assert job.priority == expected, (
            f"Debounced webhook job must be created at the configured incoming priority "
            f"({configured!r} → {expected}); got {job.priority}"
        )

    @pytest.mark.parametrize(("configured", "expected"), [(None, 1), (3, 3)])
    def test_recreated_batch_job_gets_configured_priority(self, configured, expected, captured_overrides):
        """The third ``create_job`` site: the batch's Job was deleted from
        the UI before the debounce timer fired, so ``_execute_webhook_job``
        rebuilds it.

        It is a separate ``create_job`` call from the batch-open one, and
        the rarely-exercised branch is exactly where a forgotten kwarg
        survives review. A user who tidies the queue mid-debounce should
        not silently get a Normal-priority job back.
        """
        from media_preview_generator.web import webhooks as wh
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        if configured is not None:
            get_settings_manager().set("incoming_job_priority", configured)

        def delete_the_batch_job():
            key = wh._debounce_key("sonarr")
            batch_job_id = wh._pending_batches[key]["job_id"]
            assert get_job_manager().delete_job(batch_job_id) is True

        self._fire_debounced(
            "sonarr",
            "/data/TV Shows/Some Show/Season 01/S01E02.mkv",
            before_fire=delete_the_batch_job,
        )

        assert captured_overrides, "the recreated job must still be handed to _start_job_async"
        job = get_job_manager().get_job(captured_overrides[-1]["job_id"])
        assert job is not None, "the recreate branch must produce a live Job"
        assert job.priority == expected, (
            f"recreated batch job must carry incoming_job_priority={configured!r} as {expected}; got {job.priority}"
        )
