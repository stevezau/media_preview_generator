"""Processing order reaches the vendor query through both full-scan routes."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.jobs import orchestrator
from media_preview_generator.processing import get_processor_for
from media_preview_generator.servers import EmbyServer, JellyfinServer, PlexServer
from media_preview_generator.servers.base import Library, ServerConfig, ServerType


@pytest.mark.parametrize("sort_by", ["newest", "oldest", "default", None, "random"])
@pytest.mark.parametrize("route", ["single_plex", "multi_server"])
@pytest.mark.parametrize("vendor", ["plex_movie", "plex_episode", "emby", "jellyfin"])
def test_full_scan_submits_requested_order(
    monkeypatch: pytest.MonkeyPatch, sort_by: str | None, route: str, vendor: str
) -> None:
    """Mock only server I/O and dispatch, keeping processors and adapters real.

    Server order deliberately differs from added-date order. Emby/Jellyfin
    span pages, so forgetting the sort on a later page also fails this test.
    """
    if route == "single_plex" and not vendor.startswith("plex"):
        pytest.skip("Non-Plex scans always use the multi-server route")
    server_type = ServerType.PLEX if vendor.startswith("plex") else ServerType(vendor)
    server_cfg = ServerConfig(id="server", type=server_type, name="Test", enabled=True, url="http://test", auth={})
    classes = {ServerType.PLEX: PlexServer, ServerType.EMBY: EmbyServer, ServerType.JELLYFIN: JellyfinServer}
    client = classes[server_type](server_cfg)
    libraries = [Library(id=key, name=key, enabled=True, remote_paths=("/media",)) for key in ("1", "2")]
    monkeypatch.setattr(client, "list_libraries", lambda: libraries)
    monkeypatch.setattr(get_processor_for(server_type), "_make_client", lambda cfg: client)
    query_calls = []
    # Alphabetical/server order is middle, old, new, not added-date order.
    original_ids = ["middle", "old", "new"]

    if server_type is ServerType.PLEX:
        sections = []
        for library in libraries:
            section = MagicMock(key=library.id, title=library.name, METADATA_TYPE=vendor.split("_")[1])

            def search(*, _library_id: str = library.id, **kwargs: str) -> list[SimpleNamespace]:
                query_calls.append(kwargs)
                ids = {"addedAt:desc": ["new", "middle", "old"], "addedAt:asc": ["old", "middle", "new"]}.get(
                    kwargs.get("sort"), original_ids
                )
                return [
                    SimpleNamespace(
                        ratingKey=f"{_library_id}-{item_id}",
                        title=item_id,
                        locations=[f"/media/{_library_id}-{item_id}.mkv"],
                        grandparentTitle="Series",
                        parentIndex=1,
                        index=1,
                    )
                    for item_id in ids
                ]

            section.search.side_effect = search
            sections.append(section)
        connection = MagicMock()
        connection.library.sections.return_value = sections
        monkeypatch.setattr(client, "_connect", lambda: connection)
    else:
        monkeypatch.setattr("media_preview_generator.servers._embyish._LIST_ITEMS_PAGE_SIZE", 2)

        def request(method: str, path: str, *, params: dict, **kwargs: object) -> MagicMock:
            assert method == "GET" and path == "/Items"
            query_calls.append(params)
            ids = original_ids
            if params.get("SortBy") == "DateCreated":
                ids = ["new", "middle", "old"] if params["SortOrder"] == "Descending" else ["old", "middle", "new"]
            start = params["StartIndex"]
            response = MagicMock()
            response.json.return_value = {
                "Items": [
                    {"Id": item_id, "Name": item_id, "Path": f"/media/{params['ParentId']}-{item_id}.mkv"}
                    for item_id in ids[start : start + 2]
                ],
                "TotalRecordCount": len(ids),
            }
            return response

        monkeypatch.setattr(client, "_request", request)

    registry = MagicMock()
    registry.configs.return_value = [server_cfg]
    config = SimpleNamespace(sort_by=sort_by, plex_library_ids=None)
    shuffle = MagicMock(side_effect=lambda items: items.reverse())
    monkeypatch.setattr(orchestrator.random, "Random", lambda: SimpleNamespace(shuffle=shuffle))

    if route == "single_plex":
        dispatch = MagicMock(return_value={"completed": 6, "failed": 0, "cancelled": False})
        assert orchestrator._run_plex_full_scan_phase(
            config,
            registry,
            dispatch_items=dispatch,
            progress_callback=None,
            cancel_check=None,
            totals={"processed": 0, "successful": 0, "failed": 0, "cancelled": False},
            aggregate_outcome={},
        )
        items = dispatch.call_args.args[0]
    else:
        monkeypatch.setattr(orchestrator, "_build_multi_server_registry", lambda cfg: registry)
        dispatch = MagicMock(return_value={})
        monkeypatch.setattr(orchestrator, "_dispatch_processable_items", dispatch)
        orchestrator._run_full_scan_multi_server(config, selected_gpus=[])
        items = [item for cfg, item in dispatch.call_args.args[0]]

    expected_ids = {"newest": ["new", "middle", "old"], "oldest": ["old", "middle", "new"]}.get(sort_by, original_ids)
    expected_paths = [f"/media/{library.id}-{item_id}.mkv" for library in libraries for item_id in expected_ids]
    if sort_by == "random":
        expected_paths.reverse()
        shuffle.assert_called_once()
    else:
        shuffle.assert_not_called()
    assert [item.canonical_path for item in items] == expected_paths
    assert len(query_calls) == (2 if server_type is ServerType.PLEX else 4)
    for query in query_calls:
        if server_type is ServerType.PLEX:
            expected_query = {"libtype": "episode"} if vendor == "plex_episode" else {}
            if sort_by in ("newest", "oldest"):
                expected_query["sort"] = "addedAt:desc" if sort_by == "newest" else "addedAt:asc"
            assert query == expected_query
        elif sort_by in ("newest", "oldest"):
            assert query["SortBy"] == "DateCreated"
            assert query["SortOrder"] == ("Descending" if sort_by == "newest" else "Ascending")
        else:
            assert "SortBy" not in query and "SortOrder" not in query
