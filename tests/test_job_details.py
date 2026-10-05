"""What the Jobs page shows beside a job's own fields: the libraries it covers (``library_names`` on ``GET /api/jobs``)
and the files its expanded row lists (``GET /api/jobs/<id>/file-list``)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers.titles import TITLE_CACHE
from media_preview_generator.web.job_details import (
    job_file_list,
    job_library_names,
    job_library_scope,
    saved_server_configs,
)
from media_preview_generator.web.jobs import Job, JobProgress


def _server(sid: str, stype: str, libraries: list[dict], enabled: bool = True) -> dict:
    return {
        "id": sid,
        "type": stype,
        "name": sid.upper(),
        "enabled": enabled,
        "libraries": libraries,
        "path_mappings": [],
        "exclude_paths": [],
    }


# Plex and Jellyfin both number a library "1": an id alone doesn't say which library it is.
SERVERS = [
    _server(
        "plex-1",
        "plex",
        [
            {"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True},
            {"id": "2", "name": "TV Shows", "remote_paths": ["/data/tv"], "enabled": True},
        ],
    ),
    _server("jf-1", "jellyfin", [{"id": "1", "name": "Anime", "remote_paths": ["/data/anime"], "enabled": True}]),
    _server("emby-1", "emby", [{"id": "9", "name": "Shows", "remote_paths": ["/data/tv"], "enabled": True}]),
]


@pytest.fixture
def configs():
    return saved_server_configs(SERVERS)


def _job(config: dict | None = None, **fields) -> Job:
    return Job(id="j1", config=config or {}, **fields)


class TestJobLibraryNames:
    def test_an_intro_and_credits_job_names_the_libraries_it_was_started_on(self, configs):
        job = _job(
            {
                "kind": JOB_KIND_INTRO_CREDITS,
                "libraries": [{"server_id": "plex-1", "library_id": "2"}, {"server_id": "jf-1", "library_id": "1"}],
            },
            kind=JOB_KIND_INTRO_CREDITS,
        )
        assert job_library_names(job, configs) == ["TV Shows", "Anime"]

    def test_a_preview_scan_names_its_library_ids_on_its_server(self, configs):
        job = _job({"selected_library_ids": ["1", "2"], "server_id": "plex-1"}, server_id="plex-1")
        assert job_library_names(job, configs) == ["Movies", "TV Shows"]

    def test_a_scheduled_scan_names_its_one_library_on_its_server(self, configs):
        job = _job({}, library_id="1", server_id="jf-1")
        assert job_library_names(job, configs) == ["Anime"]

    @pytest.mark.parametrize(
        ("library_id", "expected"),
        [("1", []), ("9", ["Shows"])],
        ids=["id-on-two-servers-names-none", "id-on-one-server"],
    )
    def test_an_id_with_no_server_names_a_library_only_when_one_server_has_it(self, configs, library_id, expected):
        job = _job({"selected_library_ids": [library_id]})
        assert job_library_names(job, configs) == expected

    def test_a_webhook_batch_names_the_libraries_holding_its_files_on_every_server(self, configs):
        job = _job({"file_paths": ["/data/tv/Show/Season 01/e01.mkv", "/data/tv/Show/Season 01/e02.mkv"]})
        assert job_library_names(job, configs) == ["TV Shows", "Shows"]

    def test_a_job_pinned_to_a_server_names_only_that_servers_library(self, configs):
        job = _job({"file_paths": ["/data/tv/Show/Season 01/e01.mkv"], "server_id": "emby-1"})
        assert job_library_names(job, configs) == ["Shows"]

    def test_a_preview_webhook_names_the_library_of_its_paths(self, configs):
        job = _job({"webhook_paths": ["/data/movies/Heat (1995)/Heat (1995).mkv"]})
        assert job_library_names(job, configs) == ["Movies"]

    def test_a_senders_path_is_read_through_the_webhook_prefixes(self):
        # Sonarr sends its own view of the file; the follow-up keeps that path.
        mapping = {"remote_prefix": "/data/tv", "local_prefix": "/data/tv", "webhook_prefixes": ["/sonarr/tv"]}
        configs = saved_server_configs([{**SERVERS[0], "path_mappings": [mapping]}])
        job = _job({"file_paths": ["/sonarr/tv/Show/Season 01/e01.mkv"]})
        assert job_library_names(job, configs) == ["TV Shows"]

    def test_a_disabled_server_names_nothing(self):
        configs = saved_server_configs([{**SERVERS[0], "enabled": False}])
        job = _job({"webhook_paths": ["/data/movies/Heat (1995)/Heat (1995).mkv"]})
        assert job_library_names(job, configs) == []

    @pytest.mark.parametrize(
        "config",
        [{}, {"file_paths": ["/elsewhere/x.mkv"]}, {"libraries": [{"server_id": "gone", "library_id": "1"}]}],
        ids=["names-nothing", "file-in-no-library", "library-no-longer-saved"],
    )
    def test_nothing_known_names_nothing(self, configs, config):
        assert job_library_names(_job(config), configs) == []

    def test_an_unreadable_server_type_is_skipped(self):
        configs = saved_server_configs([{**SERVERS[0], "type": "kodi"}, SERVERS[1], "not-a-server"])
        assert [c.id for c in configs] == ["jf-1"]


class TestJobLibraryScope:
    def test_origin_is_not_a_target_pin(self, configs):
        job = _job({"webhook_paths": ["/data/tv/Show/a.mkv"]}, server_id="plex-1")
        assert [(row["server_id"], row["library_id"]) for row in job_library_scope(job, configs)] == [
            ("plex-1", "2"),
            ("emby-1", "9"),
        ]

    def test_explicit_pin_limits_path_scope(self, configs):
        job = _job({"file_paths": ["/data/tv/a.mkv"], "server_id": "emby-1"}, server_id="plex-1")
        assert [(row["server_id"], row["library_id"]) for row in job_library_scope(job, configs)] == [("emby-1", "9")]

    def test_same_ids_are_distinct_explicit_library_pairs(self, configs):
        job = _job(
            {"libraries": [{"server_id": "plex-1", "library_id": "1"}, {"server_id": "jf-1", "library_id": "1"}]}
        )
        scope = job_library_scope(job, configs)
        assert [(row["server_id"], row["library_id"], row["library_name"]) for row in scope] == [
            ("plex-1", "1", "Movies"),
            ("jf-1", "1", "Anime"),
        ]

    def test_ambiguous_legacy_library_id_stays_unknown(self, configs):
        assert job_library_scope(_job({"selected_library_ids": ["1"]}), configs) == []

    def test_publisher_outcomes_do_not_invent_library_associations(self, configs):
        job = _job(publishers=[{"server_id": "plex-1", "status": "published"}])
        assert job_library_scope(job, configs) == []

    def test_all_requested_paths_count_with_one_lookup_per_directory(self, configs):
        from media_preview_generator.web import job_details

        paths = [f"/data/movies/Archive/movie-{i}.mkv" for i in range(10000)] + ["/data/anime/Show/episode.mkv"]
        with patch.object(job_details, "_library_matches", wraps=job_details._library_matches) as lookup:
            scope = job_library_scope(_job({"file_paths": paths}), configs)
        assert [(row["server_id"], row["library_id"]) for row in scope] == [("plex-1", "1"), ("jf-1", "1")]
        assert lookup.call_count == 2
        assert [call.args[0] for call in lookup.call_args_list] == [paths[0], paths[-1]]

    def test_exact_folder_mapping_boundaries_do_not_share_directory_cache(self):
        server = _server(
            "plex",
            "plex",
            [
                {"id": "1", "name": "Movies", "remote_paths": ["/media"], "enabled": True},
                {"id": "2", "name": "Shows", "remote_paths": ["/archive"], "enabled": True},
            ],
        )
        server["path_mappings"] = [
            {
                "remote_prefix": "/incoming/Movies",
                "local_prefix": "/media/movies",
                "webhook_prefixes": ["/sender/Movies"],
            },
            {
                "remote_prefix": "/incoming/Shows",
                "local_prefix": "/archive/shows",
                "webhook_prefixes": ["/sender/Shows"],
            },
        ]
        configs = saved_server_configs([server])
        cache = {}
        for prefix in ("/incoming", "/sender"):
            for folder, expected in (("Movies", "1"), ("Shows", "2")):
                scope = job_library_scope(_job({"file_paths": [f"{prefix}/{folder}"]}), configs, cache)
                assert [row["library_id"] for row in scope] == [expected]

    def test_unknown_paths_and_unknown_legacy_names_stay_unknown(self, configs):
        assert job_library_scope(_job({"file_paths": ["/unknown/file.mkv"]}), configs) == []
        assert job_library_scope(_job({"selected_libraries": ["Not saved"]}), configs) == []

    def test_explicit_removed_library_identity_is_preserved_without_guessing_name(self, configs):
        scope = job_library_scope(_job({"libraries": [{"server_id": "removed", "library_id": "old-id"}]}), configs)
        assert scope == [
            {
                "server_id": "removed",
                "server_name": "removed",
                "server_type": "",
                "library_id": "old-id",
                "library_name": "old-id",
            }
        ]


class TestJobFileList:
    EPISODE = "/data/tv/Rick and Morty (2013)/Season 01/Rick.and.Morty.S01E02.1080p.mkv"
    FILM = "/data/movies/Heat (1995)/Heat.1995.2160p.mkv"

    @pytest.fixture(autouse=True)
    def _titles(self):
        TITLE_CACHE.clear()
        yield
        TITLE_CACHE.clear()

    def test_each_file_has_the_title_its_job_log_uses_and_its_name(self):
        TITLE_CACHE.put(self.FILM, ("Heat", 1995))
        job = _job({"file_paths": [self.EPISODE, self.FILM]})

        files, total = job_file_list(job, [], limit=10)

        assert files == [
            {"title": "Rick and Morty (2013) S01E02", "name": "Rick.and.Morty.S01E02.1080p.mkv", "path": self.EPISODE},
            {"title": "Heat (1995)", "name": "Heat.1995.2160p.mkv", "path": self.FILM},
        ]
        assert total == 2

    def test_files_given_come_first_then_files_run_it_wasnt_given(self):
        job = _job({"file_paths": ["/data/tv/S/Season 01/b.mkv"]})
        results = [{"file": "/data/tv/S/Season 01/a.mkv"}, {"file": "/data/tv/S/Season 01/b.mkv"}, {"file": ""}]

        files, total = job_file_list(job, results, limit=10)

        assert [f["name"] for f in files] == ["b.mkv", "a.mkv"]
        assert total == 2

    def test_a_re_check_lists_the_batch_it_holds(self):
        job = _job({"version_rerun": True, "version_rerun_files": {self.EPISODE: {"credits_text": 4}}})
        files, _ = job_file_list(job, [], limit=10)
        assert [f["path"] for f in files] == [self.EPISODE]

    def test_a_large_job_lists_the_first_files_and_counts_the_rest(self):
        results = [{"file": f"/data/tv/S/Season 01/e{i:02d}.mkv"} for i in range(30)]
        job = _job({}, progress=JobProgress(total_items=1568))

        files, total = job_file_list(job, results, limit=10)

        assert [f["name"] for f in files] == [f"e{i:02d}.mkv" for i in range(10)]
        assert total == 1568


@pytest.fixture()
def app(tmp_path):
    from media_preview_generator.web.app import create_app

    config_dir = str(tmp_path / "cfg")
    os.makedirs(config_dir, exist_ok=True)
    env = {"CONFIG_DIR": config_dir, "WEB_AUTH_TOKEN": "test-token-12345678", "WEB_PORT": "8099"}
    with patch.dict(os.environ, env):
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app


_HEADERS = {"Authorization": "Bearer test-token-12345678"}


class TestJobsApi:
    def test_all_filters_intersect_before_pagination_and_keep_chain_heads(self, app):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("media_servers", SERVERS)
        jm = get_job_manager()
        for index in range(12):
            jm.create_job(library_name=f"Other {index}", config={"server_id": "plex-1", "selected_library_ids": ["1"]})
        matched = []
        for index in range(3):
            matched.append(
                jm.create_job(
                    library_name=f"Needle {index}",
                    kind="intro_credits",
                    server_id="plex-1",
                    config={"file_paths": ["/data/tv/Show/a.mkv"], "source": "sonarr", "is_retry_chain": True},
                )
            )
        jm.create_job(
            library_name="Needle retry child",
            kind="intro_credits",
            config={"file_paths": ["/data/tv/Show/a.mkv"], "is_retry": True, "parent_job_id": matched[0].id},
        )
        params = {
            "q": "needle",
            "status": "pending",
            "kind": "intro_credits",
            "server_id": "emby-1",
            "library_server_id": "emby-1",
            "library_id": "9",
            "page": 2,
            "per_page": 2,
        }
        response = app.test_client().get("/api/jobs", query_string=params, headers=_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 3
        assert body["pages"] == 2
        assert [row["id"] for row in body["jobs"]] == [matched[2].id]
        assert {(pair["server_id"], pair["library_id"]) for pair in body["jobs"][0]["library_scope"]} == {
            ("plex-1", "2"),
            ("emby-1", "9"),
        }
        params["include_retry_attempts"] = "1"
        assert app.test_client().get("/api/jobs", query_string=params, headers=_HEADERS).get_json()["total"] == 4

    def test_same_named_libraries_use_server_and_library_ids(self, app):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        servers = [
            SERVERS[0],
            {**SERVERS[1], "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/jellyfin/movies"]}]},
        ]
        get_settings_manager().set("media_servers", servers)
        jm = get_job_manager()
        plex = jm.create_job(config={"server_id": "plex-1", "selected_library_ids": ["1"]})
        jellyfin = jm.create_job(config={"server_id": "jf-1", "selected_library_ids": ["1"]})
        for server_id, expected in (("plex-1", plex), ("jf-1", jellyfin)):
            body = (
                app.test_client()
                .get("/api/jobs", query_string={"library_server_id": server_id, "library_id": "1"}, headers=_HEADERS)
                .get_json()
            )
            assert [row["id"] for row in body["jobs"]] == [expected.id]
            assert body["total"] == 1
            assert len([lib for lib in body["filter_options"]["libraries"] if lib["name"] == "Movies"]) == 2

    @pytest.mark.parametrize("params", [{"kind": "chapters"}, {"library_id": "1"}, {"library_server_id": "plex-1"}])
    def test_invalid_filter_contract_returns_a_clear_error(self, app, params):
        response = app.test_client().get("/api/jobs", query_string=params, headers=_HEADERS)
        assert response.status_code == 400
        assert response.get_json()["error"]

    def test_server_filter_accepts_origin_even_when_another_server_is_pinned(self, app):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("media_servers", SERVERS)
        job = get_job_manager().create_job(
            server_id="plex-1", config={"server_id": "emby-1", "file_paths": ["/data/tv/a.mkv"]}
        )
        for server_id in ("plex-1", "emby-1"):
            body = (
                app.test_client().get("/api/jobs", query_string={"server_id": server_id}, headers=_HEADERS).get_json()
            )
            assert [row["id"] for row in body["jobs"]] == [job.id]
        body = (
            app.test_client()
            .get(
                "/api/jobs",
                query_string={"server_id": "plex-1", "library_server_id": "emby-1", "library_id": "9"},
                headers=_HEADERS,
            )
            .get_json()
        )
        assert [row["id"] for row in body["jobs"]] == [job.id]

    def test_search_finds_visible_associated_owner_without_publisher_results(self, app):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("media_servers", SERVERS)
        job = get_job_manager().create_job(server_id="plex-1", config={"file_paths": ["/data/tv/a.mkv"]})
        body = app.test_client().get("/api/jobs", query_string={"q": "EMBY-1"}, headers=_HEADERS).get_json()
        assert [row["id"] for row in body["jobs"]] == [job.id]

    def test_the_jobs_list_carries_each_jobs_library_names(self, app):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("media_servers", SERVERS)
        jm = get_job_manager()
        follow_up = jm.create_job(
            library_name="Intro & Credits · Show",
            config={"kind": JOB_KIND_INTRO_CREDITS, "file_paths": ["/data/anime/Show/Season 01/e01.mkv"]},
            kind=JOB_KIND_INTRO_CREDITS,
        )
        whole_server = jm.create_job(library_name="All Libraries", config={})

        rows = {r["id"]: r for r in app.test_client().get("/api/jobs", headers=_HEADERS).get_json()["jobs"]}

        assert rows[follow_up.id]["library_names"] == ["Anime"]
        assert rows[whole_server.id]["library_names"] == []

    def test_an_unreadable_server_config_leaves_the_tags_empty_not_the_queue(self, app):
        from media_preview_generator.web.jobs import get_job_manager

        job = get_job_manager().create_job(library_name="Show", config={"webhook_paths": ["/data/tv/x.mkv"]})
        with patch("media_preview_generator.web.job_details.find_library_matches", side_effect=RuntimeError("bad")):
            body = app.test_client().get("/api/jobs", headers=_HEADERS).get_json()

        assert [(r["id"], r["library_names"]) for r in body["jobs"]] == [(job.id, [])]

    def test_the_file_list_gives_the_first_files_and_the_total(self, app):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        paths = [f"/data/tv/Show (2020)/Season 01/Show.S01E{i:02d}.mkv" for i in range(1, 13)]
        job = jm.create_job(
            library_name="Intro & Credits · 12 files",
            config={"kind": JOB_KIND_INTRO_CREDITS, "file_paths": paths},
            kind=JOB_KIND_INTRO_CREDITS,
        )

        body = app.test_client().get(f"/api/jobs/{job.id}/file-list?limit=2", headers=_HEADERS).get_json()

        assert body == {
            "files": [
                {"title": "Show (2020) S01E01", "name": "Show.S01E01.mkv", "path": paths[0]},
                {"title": "Show (2020) S01E02", "name": "Show.S01E02.mkv", "path": paths[1]},
            ],
            "total": 12,
        }

    def test_the_file_list_of_a_missing_job_is_not_found(self, app):
        response = app.test_client().get("/api/jobs/nope/file-list", headers=_HEADERS)
        assert response.status_code == 404

    def test_the_file_list_needs_the_token(self, app):
        from media_preview_generator.web.jobs import get_job_manager

        job = get_job_manager().create_job(library_name="x", config={})
        response = app.test_client().get(f"/api/jobs/{job.id}/file-list")
        assert response.status_code == 401
