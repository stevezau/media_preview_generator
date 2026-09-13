import sqlite3

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_PREVIEWS, parse_job_kind
from media_preview_generator.web.jobs import JobManager, JobStorage


def test_parse_job_kind_defaults_to_previews():
    assert parse_job_kind("intro_credits") == JOB_KIND_INTRO_CREDITS
    assert parse_job_kind(None) == JOB_KIND_PREVIEWS
    assert parse_job_kind("bogus") == JOB_KIND_PREVIEWS


def test_kind_round_trips_through_storage(tmp_path):
    jm = JobManager(config_dir=str(tmp_path))
    job = jm.create_job(library_name="R&M S01", kind=JOB_KIND_INTRO_CREDITS)
    assert job.to_dict()["kind"] == JOB_KIND_INTRO_CREDITS
    reloaded = JobStorage(str(tmp_path / "jobs.db")).all_jobs()
    assert [j.kind for j in reloaded if j.id == job.id] == [JOB_KIND_INTRO_CREDITS]


def test_legacy_row_without_kind_column_loads_as_previews(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, "
        "started_at TEXT, completed_at TEXT, library_id TEXT, library_name TEXT, server_id TEXT, "
        "server_name TEXT, server_type TEXT, priority INTEGER NOT NULL DEFAULT 2, paused INTEGER NOT NULL "
        "DEFAULT 0, error TEXT, progress_json TEXT NOT NULL DEFAULT '{}', config_json TEXT NOT NULL DEFAULT "
        "'{}', publishers_json TEXT NOT NULL DEFAULT '[]')"
    )
    conn.execute("INSERT INTO jobs (id, status, created_at) VALUES ('old', 'completed', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    jobs = JobStorage(str(db)).all_jobs()
    assert jobs[0].kind == JOB_KIND_PREVIEWS
