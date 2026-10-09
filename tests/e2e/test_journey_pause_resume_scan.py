"""Backend-real E2E: global pause / resume of processing.

Audit gap #10: pause/resume not exercised at all in e2e. Bug class:
pause toggle desync between UI and worker pool, resume not picking up
where left off, ghost "paused" indicator after resume.

We exercise the real `/api/processing/pause` and `/api/processing/resume`
endpoints + the real JobManager's `processing_paused` flag, then assert
the dashboard's UI state reflects the change without a page reload (the
dashboard's `processingPaused` JS var is updated by the request handler).
"""

from __future__ import annotations

import time

import pytest
import requests

_AUTH_HEADERS = {"X-Auth-Token": "e2e-test-token"}
_AUTH_JSON_HEADERS = {"X-Auth-Token": "e2e-test-token", "Content-Type": "application/json"}
_API_TIMEOUT = 60


@pytest.mark.e2e
class TestPauseResumeScan:
    def test_pause_endpoint_flips_state_endpoint_returns_paused_true(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """POST /api/processing/pause -> GET /api/processing/state shows paused.

        The most basic correctness test: pause → state read confirms the
        flag flipped on the server side. Without this the UI button can
        flip its label while the worker pool keeps draining.
        """
        app_url, _ = backend_real_app

        # Initial state: not paused.
        state_resp = requests.get(
            f"{app_url}/api/processing/state",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert state_resp.ok, f"GET /api/processing/state: {state_resp.status_code} {state_resp.text}"
        assert state_resp.json().get("paused") is False, f"Fresh app booted with paused=true: {state_resp.json()}"

        # Pause via the real endpoint.
        pause_resp = requests.post(
            f"{app_url}/api/processing/pause",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert pause_resp.ok, f"pause failed: {pause_resp.status_code} {pause_resp.text}"
        body = pause_resp.json()
        assert body.get("paused") is True, f"pause endpoint returned {body}"

        # State must now reflect paused.
        state_resp = requests.get(
            f"{app_url}/api/processing/state",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert state_resp.ok
        assert state_resp.json().get("paused") is True, (
            f"State endpoint still reports paused=False after POST /api/processing/pause: "
            f"{state_resp.json()} — the pause flag was set in one place but read from another."
        )

    def test_resume_endpoint_clears_paused_state(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Pause then Resume: state must end at paused=False."""
        app_url, _ = backend_real_app

        requests.post(
            f"{app_url}/api/processing/pause",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        resume_resp = requests.post(
            f"{app_url}/api/processing/resume",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert resume_resp.ok, f"resume failed: {resume_resp.status_code}"
        assert resume_resp.json().get("paused") is False

        # Confirm via the state endpoint.
        state_resp = requests.get(
            f"{app_url}/api/processing/state",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert state_resp.json().get("paused") is False, (
            "Resume returned paused=False but a follow-up GET still shows paused=True. "
            "The resume only updated one of the two flag locations."
        )

    def test_paused_state_blocks_new_jobs_from_starting(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """When paused, a newly-POSTed job should NOT immediately go to RUNNING.

        This catches the bug where pause toggles the UI label but the
        worker pool keeps picking up new items. We POST a job while paused and
        watch it for a few seconds: it must stay "pending" throughout.
        """
        app_url, _ = backend_real_app

        # Pause first.
        requests.post(
            f"{app_url}/api/processing/pause",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        try:
            # POST a real job.
            post_resp = requests.post(
                f"{app_url}/api/jobs/manual",
                headers=_AUTH_JSON_HEADERS,
                data='{"file_paths": ["/tmp/paused_job_target.mkv"]}',
                timeout=_API_TIMEOUT,
            )
            assert post_resp.ok
            job_id = post_resp.json()["id"]

            # Watch the job for a window: while paused it must never start running. An ignored
            # pause shows up as "running" (or a terminal status reached without being resumed).
            statuses: set[str | None] = set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                r = requests.get(
                    f"{app_url}/api/jobs/{job_id}",
                    headers=_AUTH_HEADERS,
                    timeout=_API_TIMEOUT,
                )
                if r.ok:
                    statuses.add(r.json().get("status"))
                time.sleep(0.2)
        finally:
            requests.post(
                f"{app_url}/api/processing/resume",
                headers=_AUTH_HEADERS,
                timeout=_API_TIMEOUT,
            )

        assert statuses == {"pending"}, (
            f"Job {job_id} POSTed while paused should stay 'pending' the whole time; saw {sorted(map(str, statuses))}. "
            "This is the bug class where the pause toggle is purely cosmetic."
        )

    def test_pause_resume_emits_socketio_state_change(
        self,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Pause/resume must emit `processing_paused_changed` over SocketIO.

        That event drives the UI button swap in renderGlobalPauseResume().
        If the emit drops, the button label desyncs from the actual state.

        Uses a python-socketio client (not Playwright-driven browser
        observation) so the SocketIO subscription doesn't go through
        the Playwright Python↔Node IPC pipe — the pipe stalls under
        ``-n auto`` parallelism and crashes the xdist worker. The
        contract we're verifying (server emits event on pause/resume)
        is identical regardless of which client receives it.
        """
        import socketio

        from .conftest import _capture_session_cookie

        app_url, _ = backend_real_app

        # The /jobs namespace requires Flask-session auth on connect —
        # capture a session cookie via a real /login POST.
        cookie = _capture_session_cookie(app_url)
        cookie_header = f"{cookie['name']}={cookie['value']}"

        captured: list[dict] = []
        client = socketio.Client(reconnection=False)

        @client.on("processing_paused_changed", namespace="/jobs")
        def _on_paused_changed(data):
            captured.append(data)

        client.connect(
            app_url,
            namespaces=["/jobs"],
            transports=["polling"],
            wait_timeout=10,
            headers={"Cookie": cookie_header},
        )
        try:
            requests.post(
                f"{app_url}/api/processing/pause",
                headers=_AUTH_HEADERS,
                timeout=_API_TIMEOUT,
            )

            deadline = time.monotonic() + 5
            observed_pause = False
            while time.monotonic() < deadline:
                if any(d.get("paused") is True for d in captured):
                    observed_pause = True
                    break
                time.sleep(0.1)

            assert observed_pause, (
                "Pause did not emit processing_paused_changed(paused=True) over SocketIO. "
                "The pause endpoint flipped state but the UI never gets notified, so the "
                "button label desyncs from the actual server state."
            )

            captured.clear()
            requests.post(
                f"{app_url}/api/processing/resume",
                headers=_AUTH_HEADERS,
                timeout=_API_TIMEOUT,
            )
            deadline = time.monotonic() + 5
            observed_resume = False
            while time.monotonic() < deadline:
                if any(d.get("paused") is False for d in captured):
                    observed_resume = True
                    break
                time.sleep(0.1)

            assert observed_resume, "Resume did not emit processing_paused_changed(paused=False)."
        finally:
            client.disconnect()
