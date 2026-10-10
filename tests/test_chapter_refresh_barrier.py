"""Keep chapter registration after Plex's destructive Analyze request completes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.output.journal import mark_plex_refresh_pending, write_meta
from media_preview_generator.processing import chapters, multi_server
from media_preview_generator.processing.generator import CancellationError
from media_preview_generator.processing.multi_server import (
    MultiServerResult,
    MultiServerStatus,
    PublisherResult,
    PublisherStatus,
)
from media_preview_generator.processing.plex_refresh import PlexRefreshQueue
from tests.test_processing_chapters import _registry
from tests.test_processing_chapters import config as _config_fixture
from tests.test_processing_chapters import extraction as _extraction_fixture
from tests.test_processing_chapters import plan as _plan_fixture

config = _config_fixture
extraction = _extraction_fixture
plan = _plan_fixture


@pytest.mark.parametrize("result", [True, False, "offline"])
def test_barrier_requires_successful_http_completion(result):
    def refresh(path, item_id):
        assert (path, item_id) == ("movie", "42")
        if result == "offline":
            raise ConnectionError("offline")
        return result

    queue = PlexRefreshQueue(idle_seconds=0.01)
    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    try:
        assert queue.enqueue(server, "movie", "42")
        assert queue.wait("plex", "movie", required=True, timeout=2) is (result is True)
        assert queue.wait("plex", "untouched") is True
        assert queue.wait("plex", "untouched", required=True) is False
    finally:
        queue.close()


def test_barrier_includes_a_newer_pending_generation_and_has_bounded_wait():
    first, release_first, second, release_second = Event(), Event(), Event(), Event()
    calls = []

    def refresh(path, item_id):
        calls.append((path, item_id))
        if len(calls) == 1:
            first.set()
            assert release_first.wait(2)
        else:
            second.set()
            assert release_second.wait(2)
        return True

    queue = PlexRefreshQueue(idle_seconds=0.01)
    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    try:
        assert queue.enqueue(server, "movie", "1")
        assert first.wait(2)
        assert queue.enqueue(server, "movie", "2")
        release_first.set()
        assert second.wait(2)
        assert not queue.wait("plex", "movie", required=True, timeout=0.01)
        with pytest.raises(CancellationError, match="waiting for Plex Analyze"):
            queue.wait("plex", "movie", cancel_check=lambda: True)
        release_second.set()
        assert queue.wait("plex", "movie", required=True, timeout=2)
        assert calls == [("movie", "1"), ("movie", "2")]
    finally:
        release_first.set()
        release_second.set()
        queue.close()


def test_queue_worker_cannot_wait_for_itself():
    queue = PlexRefreshQueue(idle_seconds=0.01)
    observations = []

    def refresh(path, item_id):
        observations.append(queue.wait("plex", path, required=True, timeout=2))
        return True

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    try:
        queue.enqueue(server, "movie", "1")
        assert queue.wait("plex", "movie", required=True, timeout=2)
        assert observations == [False]
    finally:
        queue.close()


def _pipeline(plan, monkeypatch, *, required=True, outputs=()):
    registry = _registry(plan)
    plan.server = registry.get("plex")
    publisher = PublisherResult(
        "plex",
        "Plex",
        "plex_bundle",
        PublisherStatus.PUBLISHED,
        output_paths=list(outputs),
        plex_refresh_requested=required,
    )
    previews = MagicMock(return_value=MultiServerResult(plan.canonical_path, MultiServerStatus.PUBLISHED, [publisher]))
    monkeypatch.setattr(multi_server, "_process_canonical_path_previews", previews)
    monkeypatch.setattr(multi_server, "_resolve_publishers", lambda *_a, **_kw: [(plan.server, None, "1")])
    return registry, previews, publisher


def test_delayed_analyze_then_fresh_registration_reuses_all_jpegs(plan, config, extraction, monkeypatch):
    extract, register = extraction
    assert chapters.publish_chapters(plan, config).status == "ready"
    extract.reset_mock()
    register.reset_mock()
    registry, previews, publisher = _pipeline(plan, monkeypatch)
    initial = plan.target
    current = [initial]
    prepare = MagicMock(side_effect=lambda *_a, **_kw: replace(plan, target=current[0]))
    monkeypatch.setattr(chapters, "prepare_chapters", prepare)
    started, release, waiting = Event(), Event(), Event()
    queue = PlexRefreshQueue(idle_seconds=0.01)

    def analyze(path, item_id):
        assert (path, item_id) == (plan.canonical_path, "1")
        started.set()
        assert release.wait(2)
        current[0] = replace(initial, source_updated_at=101)
        return True

    plan.server.refresh_preview_metadata = analyze

    def preview(*_args, **kwargs):
        assert kwargs["cancel_check"] is cancel_check
        queue.enqueue(plan.server, plan.canonical_path, "1")
        return MultiServerResult(plan.canonical_path, MultiServerStatus.PUBLISHED, [publisher])

    previews.side_effect = preview

    def wait(server_id, path, **kwargs):
        assert (server_id, path) == ("plex", plan.canonical_path)
        assert kwargs == {"required": True, "cancel_check": cancel_check}
        waiting.set()
        return queue.wait(server_id, path, timeout=2, **kwargs)

    monkeypatch.setattr(multi_server, "wait_for_plex_refresh", wait)

    def cancel_check():
        return False

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                multi_server.process_canonical_path, plan.canonical_path, registry, config, cancel_check=cancel_check
            )
            assert started.wait(2) and waiting.wait(2)
            register.assert_not_called()
            release.set()
            result = future.result(timeout=2)
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"
        assert prepare.call_count == 2
        assert prepare.call_args.kwargs == {
            "item_id_hint": 1,
            "cancel_check": cancel_check,
            "trust_server_hash": True,
            "regenerate": False,
        }
        extract.assert_not_called()
        assert register.call_args.args[:2] == (plan.server, current[0])
        assert set(register.call_args.args[2]) == {1, 2}
        register.call_args.kwargs["verify_source"]()
    finally:
        release.set()
        queue.close()


@pytest.mark.parametrize("condition", ["failed_request", "pending_journal", "source_changed"])
def test_unconfirmed_refresh_never_publishes_chapters(plan, config, extraction, monkeypatch, condition):
    bif = plan.folder.parent / "index-sd.bif"
    bif.write_bytes(b"bif")
    registry, _previews, publisher = _pipeline(plan, monkeypatch, outputs=[bif])
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)

    def wait(*_args, **_kwargs):
        if condition == "source_changed":
            from pathlib import Path

            Path(plan.canonical_path).write_bytes(b"replacement")
        return condition != "failed_request"

    monkeypatch.setattr(multi_server, "wait_for_plex_refresh", wait)
    if condition == "pending_journal":
        write_meta([bif], plan.canonical_path, publisher="plex_bundle", source_fingerprint=plan.source_fingerprint)
        assert mark_plex_refresh_pending([bif], plan.canonical_path, "plex", source_fingerprint=plan.source_fingerprint)
    result = multi_server.process_canonical_path(plan.canonical_path, registry, config)
    assert result.publishers[0] is publisher
    outcome = publisher.artifacts["chapters"]
    assert outcome["status"] == ("failed" if condition == "source_changed" else "waiting")
    assert outcome["retryable"]
    assert bif.read_bytes() == b"bif"
    extraction[0].assert_not_called()
    extraction[1].assert_not_called()


@pytest.mark.parametrize("requested", [False, True])
def test_check_phase_cannot_accept_ready_images_with_analyze_pending(plan, config, extraction, monkeypatch, requested):
    chapters.publish_chapters(plan, config)
    images = chapters._fresh_images(plan)
    plan.target = replace(
        plan.target,
        chapters=tuple(
            replace(
                c, thumb_url=f"/library/media/2/chapterImages/{c.index}?mpgChapter={images[str(c.index)]['sha256']}"
            )
            for c in plan.target.chapters
        ),
    )
    assert not chapters.chapter_work_needed(plan)
    registry, _preview, _publisher = _pipeline(plan, monkeypatch, required=requested)
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    wait = MagicMock(return_value=False)
    monkeypatch.setattr(multi_server, "wait_for_plex_refresh", wait)
    result = multi_server.process_canonical_path(plan.canonical_path, registry, config, check_only=True)
    assert result.status is MultiServerStatus.NEEDS_GENERATION
    if requested:
        wait.assert_not_called()
    else:
        wait.assert_called_once_with("plex", plan.canonical_path, timeout=0)
