"""The job summary's "Decided by" counts: which source group a decided marker counts under, and which files count."""

from __future__ import annotations

import os
import threading

import pytest

from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.publishers.base import PublishError
from media_preview_generator.markers.source_counts import (
    DecidedByTally,
    decided_groups,
    source_group,
    stored_groups,
)
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import ready_publisher
from tests.markers.test_pipeline import (
    CHAPTERS_BOTH,
    INTRO_ONLY,
    TIDB_INTRO,
    _clients,
    _ctx,
    _probe,
    _registry,
    _run,
)

T = MarkerType


def _marker(*sources: str, mtype: MarkerType = T.CREDITS, locked: bool = False) -> Marker:
    return Marker(mtype, 1_000_000, 1_300_000, tuple(sources), locked)


class TestSourceGroup:
    @pytest.mark.parametrize(
        ("decided_by", "group"),
        [
            # One source alone counts under its own name.
            (("chapters",), "chapters"),
            (("credits_text",), "credits_text"),
            (("skipdb",), "skipdb"),
            # Two independent sources agreeing: both named, in the user's source order (decided_by's order).
            (("theintrodb", "skipdb"), "theintrodb+skipdb"),
            (("skipdb", "theintrodb"), "skipdb+theintrodb"),
            (("introdb", "season_audio"), "introdb+season_audio"),
            # A chapter decides alone; sources that only confirmed or trimmed it aren't a separate group.
            (("chapters", "theintrodb", "introdb"), "chapters"),
            (("theintrodb", "chapters"), "chapters"),
            # A server's markers named only as the one other opinion a single source needed, either kind.
            (("theintrodb", "server_markers"), "theintrodb+server_markers"),
            (("credits_text", "server_markers_imported"), "credits_text+server_markers"),
            (("server_markers", "skipdb"), "skipdb+server_markers"),
            # ...and left out when two other sources agreed anyway.
            (("theintrodb", "skipdb", "server_markers"), "theintrodb+skipdb"),
            (("theintrodb", "skipdb", "season_audio", "server_markers"), "theintrodb+skipdb+season_audio"),
            # A repeated source is one source.
            (("skipdb", "skipdb"), "skipdb"),
        ],
    )
    def test_group_of_a_decided_marker(self, decided_by, group):
        assert source_group(_marker(*decided_by)) == group

    @pytest.mark.parametrize("decided_by", [("chapters",), ("theintrodb", "skipdb"), ("user",)])
    def test_the_users_own_marker_is_its_own_group_whatever_it_was_built_from(self, decided_by):
        assert source_group(_marker(*decided_by, locked=True)) == Source.USER.value


class TestDecidedGroups:
    def test_only_decided_types_count(self):
        decisions = {
            T.INTRO: TypeDecision(T.INTRO, DecisionStatus.DECIDED, _marker("skipdb", mtype=T.INTRO), None, ""),
            T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.NEEDS_REVIEW, None, _marker("theintrodb"), "one"),
            T.RECAP: TypeDecision(T.RECAP, DecisionStatus.NO_EVIDENCE, None, None, ""),
            T.PREVIEW: TypeDecision(T.PREVIEW, DecisionStatus.DISABLED, None, None, ""),
        }

        assert decided_groups(decisions) == {T.INTRO: "skipdb"}

    def test_a_locked_marker_of_a_type_turned_off_still_counts(self):
        lock = _marker("chapters", mtype=T.RECAP, locked=True)
        decisions = {T.RECAP: TypeDecision(T.RECAP, DecisionStatus.DECIDED, lock, None, "locked by user")}

        assert decided_groups(decisions) == {T.RECAP: "user"}


class TestTally:
    def test_counts_files_per_type_and_group_in_marker_type_order(self):
        tally = DecidedByTally()
        tally.add({T.CREDITS: "chapters", T.INTRO: "chapters"})
        tally.add({T.CREDITS: "credits_text"})
        tally.add({T.CREDITS: "chapters"})
        tally.add({})

        snapshot = tally.snapshot()

        assert snapshot == {"intro": {"chapters": 1}, "credits": {"chapters": 2, "credits_text": 1}}
        assert list(snapshot) == ["intro", "credits"]

    def test_empty_tally_has_no_types(self):
        assert DecidedByTally().snapshot() == {}

    def test_snapshot_is_a_copy(self):
        tally = DecidedByTally()
        tally.add({T.INTRO: "skipdb"})
        snapshot = tally.snapshot()
        tally.add({T.INTRO: "skipdb"})

        assert snapshot == {"intro": {"skipdb": 1}}

    def test_adds_from_many_threads_are_all_counted(self):
        tally = DecidedByTally()

        def add_many():
            for _ in range(500):
                tally.add({T.INTRO: "chapters", T.CREDITS: "credits_text"})

        threads = [threading.Thread(target=add_many) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert tally.snapshot() == {"intro": {"chapters": 4000}, "credits": {"credits_text": 4000}}


class TestStoredGroups:
    @pytest.fixture
    def store(self, tmp_path):
        s = MarkerStore(str(tmp_path / "markers.db"))
        yield s
        s.close()

    def test_groups_of_what_the_store_decided(self, store):
        rec = store.upsert_file(FileIdentity("/m/a.mkv", 10, 1), duration_ms=1_320_000, season_key=None, is_movie=True)
        store.save_decisions(
            rec.id,
            {
                T.INTRO: TypeDecision(T.INTRO, DecisionStatus.DECIDED, _marker("chapters", mtype=T.INTRO), None, ""),
                T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.NEEDS_REVIEW, None, None, "one source"),
            },
            settings_fingerprint="fp",
        )

        assert stored_groups(store, "/m/a.mkv") == {T.INTRO: "chapters"}

    def test_a_file_the_store_doesnt_know_counts_nothing(self, store):
        assert stored_groups(store, "/m/unknown.mkv") == {}


class TestWhichFilesCount:
    """A file counts once its run finishes with any outcome but failed; only its decided types count."""

    def test_chapters_deciding_both_count_one_file_per_type(self, store, media):
        ctx = _ctx(store, _registry(media, ServerType.PLEX))

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))

        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert ctx.decided_by.snapshot() == {"intro": {"chapters": 1}, "credits": {"chapters": 1}}

    def test_two_agreeing_sources_count_under_both(self, store, media):
        skipdb_intro = Candidate(T.INTRO, 128_000, 157_000, Source.SKIPDB)
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)), skipdb=LookupResult("ok", (skipdb_intro,)))
        ctx = _ctx(store, _registry(media, ServerType.PLEX), clients=clients, settings_raw=INTRO_ONLY)

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()})

        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert ctx.decided_by.snapshot() == {"intro": {"theintrodb+skipdb": 1}}

    def test_a_type_needing_review_isnt_counted_but_the_decided_one_is(self, store, media):
        # TheIntroDB alone can't publish an intro; the credits chapter decides credits.
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        ctx = _ctx(store, _registry(media, ServerType.PLEX), clients=clients)

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe((CHAPTERS_BOTH[0], CHAPTERS_BOTH[3])))

        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        assert ctx.decided_by.snapshot() == {"credits": {"chapters": 1}}

    def test_a_file_needing_review_for_every_type_counts_nothing(self, store, media):
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        ctx = _ctx(store, _registry(media, ServerType.PLEX), clients=clients, settings_raw=INTRO_ONLY)

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()})

        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        assert ctx.decided_by.snapshot() == {}

    def test_a_failed_file_counts_nothing_though_its_markers_were_decided(self, store, media):
        plex = ready_publisher()
        plex.write.side_effect = PublishError("boom")
        ctx = _ctx(store, _registry(media, ServerType.PLEX))

        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))

        assert out.outcome_key == FileOutcome.FAILED.value
        assert ctx.decided_by.snapshot() == {}

    def test_a_file_every_server_skipped_still_counts_what_its_sources_decided(self, store, media):
        # No publisher for Emby here: the server is skipped, but the detection it would have sent was made.
        ctx = _ctx(store, _registry(media, ServerType.EMBY))

        out, _ = _run(ctx, media, {}, probe=_probe(CHAPTERS_BOTH))

        assert out.outcome_key == FileOutcome.SKIPPED.value
        assert ctx.decided_by.snapshot() == {"intro": {"chapters": 1}, "credits": {"chapters": 1}}

    def test_an_extra_skipped_before_detection_counts_nothing(self, store, tmp_path):
        path = tmp_path / "media" / "movies" / "Film (2020)" / "Trailers" / "Film.mkv"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * 100)
        ctx = _ctx(store, _registry(str(path), ServerType.PLEX))

        out, _ = _run(ctx, str(path), {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))

        assert out.outcome_key == FileOutcome.SKIPPED.value
        assert ctx.decided_by.snapshot() == {}

    def test_a_file_not_on_disk_counts_nothing(self, store, media):
        ctx = _ctx(store, _registry(media, ServerType.PLEX))
        os.remove(media)

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(CHAPTERS_BOTH))

        assert out.outcome_key == FileOutcome.FILE_NOT_FOUND.value
        assert ctx.decided_by.snapshot() == {}

    def test_an_unchanged_file_run_again_counts_again_in_its_new_job(self, store, media):
        # Up to date is still a decision this job made (the second run's context is a new job's).
        registry = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        _run(_ctx(store, registry), media, {"plex-1": plex}, probe=_probe(CHAPTERS_BOTH))
        ctx = _ctx(store, registry)

        out, _ = _run(ctx, media, {"plex-1": plex})

        assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        assert ctx.decided_by.snapshot() == {"intro": {"chapters": 1}, "credits": {"chapters": 1}}


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


@pytest.fixture
def media(tmp_path):
    folder = tmp_path / "media" / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
    folder.mkdir(parents=True)
    f = folder / "Rick and Morty (2013) - S01E01 - Pilot.mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


class TestJobPayload:
    def test_get_job_serves_the_counts_under_progress(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="Intro & Credits: 3 files", config={"kind": "intro_credits"})
        jm.set_marker_sources(job.id, {"credits": {"chapters": 2, "credits_text": 1}})

        resp = client.get(f"/api/jobs/{job.id}")

        assert resp.status_code == 200
        assert resp.get_json()["progress"]["marker_sources"] == {"credits": {"chapters": 2, "credits_text": 1}}

    def test_other_jobs_serve_none(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        job = get_job_manager().create_job(library_name="Movies")

        assert client.get(f"/api/jobs/{job.id}").get_json()["progress"]["marker_sources"] is None
