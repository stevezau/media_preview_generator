"""Real full-scan routing, processor filtering, and adapter metadata extraction."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.jobs import orchestrator
from media_preview_generator.processing import get_processor_for
from media_preview_generator.servers import EmbyServer, JellyfinServer, PlexServer
from media_preview_generator.servers.base import Library, ServerConfig, ServerType


@pytest.mark.parametrize(
    ("vendor", "route"),
    [("plex", "single"), ("plex", "multi"), ("emby", "multi"), ("jellyfin", "multi")],
)
@pytest.mark.parametrize("no_matches", [False, True])
def test_filters_reach_real_adapters_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, vendor: str, route: str, no_matches: bool
) -> None:
    server_type = ServerType(vendor)
    server_cfg = ServerConfig(
        id="server",
        type=server_type,
        name="Test",
        enabled=True,
        url="http://test",
        auth={},
        path_mappings=[{"remote_prefix": "/remote", "local_prefix": "/local"}],
    )
    client = {"plex": PlexServer, "emby": EmbyServer, "jellyfin": JellyfinServer}[vendor](server_cfg)
    libraries = [
        Library(id=key, name=key, remote_paths=("/remote",), kind=kind)
        for key, kind in (("movies", "movie"), ("shows", "show"), ("shows2", "show"))
    ]
    now = datetime.now(UTC)
    rows = {
        "movies": [
            {"id": "film", "year": 2024, "paths": ["/remote/film.mkv", "/remote/film-4k.mkv"]},
            {"id": "old-film", "year": 2010},
        ],
        "shows": [
            {"id": "a1", "series": "a", "season": 1},
            {"id": "a3", "series": "a", "season": 3, "old": True},
            {"id": "b2", "series": "b", "season": 2},
            {"id": "b-special", "series": "b", "season": 0},
        ],
        # Stable show IDs still must not merge season sets across libraries.
        "shows2": [{"id": "a2-other-library", "series": "a", "season": 2}],
    }
    for lib_id, records in rows.items():
        for row in records:
            row["kind"] = "movie" if lib_id == "movies" else "episode"
            row["added"] = now - timedelta(days=30 if row.get("old") else 1)
            row.setdefault("paths", [f"/remote/{row['id']}.mkv"])
    monkeypatch.setattr(client, "list_libraries", lambda: libraries)
    monkeypatch.setattr(get_processor_for(server_type), "_make_client", lambda cfg: client)
    if vendor == "plex":
        sections = []
        for library in libraries:
            kind = "movie" if library.id == "movies" else "episode"
            section = MagicMock(key=library.id, title=library.name, METADATA_TYPE=kind)
            section.search.return_value = [
                SimpleNamespace(
                    ratingKey=row["id"],
                    title=row["id"],
                    addedAt=row["added"],
                    locations=row["paths"],
                    year=row.get("year"),
                    grandparentRatingKey=row.get("series"),
                    parentIndex=row.get("season"),
                    index=1,
                    grandparentTitle="Same title for every show",
                )
                for row in rows[library.id]
            ]
            sections.append(section)
        connection = MagicMock()
        connection.library.sections.return_value = sections
        monkeypatch.setattr(client, "_connect", lambda: connection)
    else:
        monkeypatch.setattr("media_preview_generator.servers._embyish._LIST_ITEMS_PAGE_SIZE", 2)

        def request(method: str, path: str, *, params: dict, **kwargs: object) -> MagicMock:
            response = MagicMock()
            if "Ids" in params:
                assert params["Ids"] == "film"
                response.json.return_value = {
                    "Items": [
                        {
                            "MediaSources": [
                                {"Id": "film", "Path": "/remote/film.mkv"},
                                {"Id": "film-4k", "Path": "/remote/film-4k.mkv"},
                            ]
                        }
                    ]
                }
            else:
                expected_fields = "Path,MediaSourceCount,DateCreated"
                if vendor == "emby":
                    expected_fields += ",ProductionYear"
                assert params["Fields"] == expected_fields
                all_rows = rows[params["ParentId"]]
                start = params["StartIndex"]
                response.json.return_value = {
                    "TotalRecordCount": len(all_rows),
                    "Items": [
                        {
                            "Id": row["id"],
                            "Name": row["id"],
                            "Path": row["paths"][0],
                            "MediaSourceCount": len(row["paths"]),
                            "Type": row["kind"].title(),
                            "SeriesId": row.get("series"),
                            "ParentIndexNumber": row.get("season"),
                            "ProductionYear": row.get("year"),
                            "DateCreated": row["added"].isoformat(),
                        }
                        for row in all_rows[start : start + 2]
                    ],
                }
            return response

        monkeypatch.setattr(client, "_request", request)

    config = SimpleNamespace(
        sort_by="newest", added_filter="last_days", added_last_days=7, latest_seasons=1, movie_year_from=2020
    )
    if no_matches:
        config.added_filter = "date_range"
        config.added_from = config.added_to = "9999-12-31"
    registry = MagicMock()
    registry.configs.return_value = [server_cfg]
    log = MagicMock()
    monkeypatch.setattr("media_preview_generator.processing._shared.logger.info", log)
    dispatch = MagicMock(return_value={"completed": 4, "failed": 0, "cancelled": False})
    if route == "single":
        totals = {"processed": 0, "successful": 0, "failed": 0, "cancelled": False}
        assert orchestrator._run_plex_full_scan_phase(
            config,
            registry,
            dispatch_items=dispatch,
            progress_callback=None,
            cancel_check=None,
            totals=totals,
            aggregate_outcome={},
        )
        if no_matches:
            assert totals["processed"] == totals["failed"] == 0
        items = dispatch.call_args.args[0] if dispatch.called else []
    else:
        monkeypatch.setattr(orchestrator, "_build_multi_server_registry", lambda cfg: registry)
        monkeypatch.setattr(orchestrator, "_dispatch_processable_items", dispatch)
        warnings = []
        counts = orchestrator._run_full_scan_multi_server(config, selected_gpus=[], warnings_out=warnings)
        assert warnings == []
        if no_matches:
            assert all(value == 0 for value in counts.values())
        items = [item for cfg, item in dispatch.call_args.args[0]] if dispatch.called else []
    expected = (
        [] if no_matches else ["/local/film.mkv", "/local/film-4k.mkv", "/local/b2.mkv", "/local/a2-other-library.mkv"]
    )
    assert [item.canonical_path for item in items] == expected
    if no_matches:
        dispatch.assert_not_called()
    summaries = [call.args for call in log.call_args_list if call.args and str(call.args[0]).startswith("Filters kept")]
    assert len(summaries) == 3
    assert sum(args[1] for args in summaries) == len(expected)
    assert sum(args[2] for args in summaries) == 8 - len(expected)


def test_relative_cutoff_is_shared_across_servers_and_fresh_for_each_run(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = [
        ServerConfig(id=name, type=ServerType.PLEX, name=name, enabled=True, url="http://test", auth={})
        for name in ("first", "second")
    ]
    registry = MagicMock()
    registry.configs.return_value = servers
    monkeypatch.setattr(orchestrator, "_build_multi_server_registry", lambda cfg: registry)
    snapshots = []
    processor = MagicMock()

    def enumerate_items(cfg: ServerConfig, **kwargs: object):
        snapshots.append(kwargs["filters"])
        return iter(())

    processor.list_canonical_paths.side_effect = enumerate_items
    monkeypatch.setattr("media_preview_generator.processing.get_processor_for", lambda vendor: processor)
    config = SimpleNamespace(added_filter="last_days", added_last_days=30)
    orchestrator._run_full_scan_multi_server(config, selected_gpus=[])
    orchestrator._run_full_scan_multi_server(config, selected_gpus=[])
    assert snapshots[0] is snapshots[1]
    assert snapshots[2] is snapshots[3]
    assert snapshots[0] is not snapshots[2]
    assert snapshots[0].now < snapshots[2].now
    assert all(snapshot.added_last_days == 30 for snapshot in snapshots)


def test_recently_added_automation_does_not_apply_full_scan_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    from media_preview_generator.processing.types import ProcessableItem

    server = ServerConfig(id="server", type=ServerType.JELLYFIN, name="Test", enabled=True, url="http://test", auth={})
    registry = MagicMock()
    registry.configs.return_value = [server]
    monkeypatch.setattr(orchestrator, "_build_multi_server_registry", lambda cfg: registry)
    processor = MagicMock()
    media = ProcessableItem(canonical_path="/media/old-season.mkv", server_id="server")
    processor.scan_recently_added.return_value = iter([media])
    monkeypatch.setattr("media_preview_generator.processing.get_processor_for", lambda vendor: processor)
    dispatch = MagicMock(return_value={})
    monkeypatch.setattr(orchestrator, "_dispatch_processable_items", dispatch)
    orchestrator._run_recently_added_multi_server(
        SimpleNamespace(added_filter="last_days", added_last_days=1, latest_seasons=1, movie_year_from=9999),
        selected_gpus=[],
        lookback_hours=24,
    )
    processor.scan_recently_added.assert_called_once_with(server, lookback_hours=24, library_ids=None)
    processor.list_canonical_paths.assert_not_called()
    assert dispatch.call_args.args[0] == [(server, media)]
