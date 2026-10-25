"""Test access: library sampling, the access-check request, per-library results and mapping suggestions."""

from __future__ import annotations

import os
import unicodedata

import pytest

from media_preview_generator.nodes import mappings as nm
from media_preview_generator.nodes import protocol
from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.protocol import PathMapping, map_app_path
from media_preview_generator.nodes.server import app as node_app
from media_preview_generator.nodes.server import info
from media_preview_generator.nodes.settings import MAX_MAPPINGS

NODE_KEY = "test-access-key"


def _video(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _server(tmp_path, libraries, *, enabled=True, server_id="p1"):
    return {
        "id": server_id,
        "type": "plex",
        "name": "Home",
        "enabled": enabled,
        "url": "http://p:32400",
        "libraries": libraries,
        "path_mappings": [{"remote_prefix": "/plexmedia", "local_prefix": str(tmp_path / "media")}],
    }


class FakeClient:
    """Finds a file when ``visible(path)`` says so; records every request."""

    def __init__(self, visible=lambda path: True, nearby=None):
        self.visible = visible
        self.nearby = nearby or {}
        self.requests: list[tuple[list[PathMapping], list[dict]]] = []

    def access_check(self, mappings, files):
        self.requests.append((mappings, files))
        results = []
        for f in files:
            ok = self.visible(f["path"])
            results.append(
                {
                    "path": f["path"],
                    "ok": ok,
                    "node_path": map_app_path(f["path"], mappings) or "/node" + f["path"],
                    "kind": "" if ok else "missing",
                    "message": "" if ok else "Not found on the node",
                }
            )
        return {"ok": True, "results": results, "nearby": self.nearby}


class RealNode:
    """``NodeClient.access_check`` answered by the real node app, so listings are only what a node can send."""

    def __init__(self, app):
        self._http = app.test_client()
        self.requests = 0

    def access_check(self, mappings, files):
        self.requests += 1
        headers = {"Authorization": f"Bearer {NODE_KEY}", protocol.PROTOCOL_HEADER: "1"}
        body = {"mapping": [m.to_json() for m in mappings], "files": files}
        response = self._http.post(protocol.ROUTE_ACCESS_CHECK, headers=headers, json=body)
        assert response.status_code == 200, response.get_json()
        return response.get_json()


@pytest.fixture
def real_node(tmp_path, monkeypatch):
    for name in ("NODE_KEY", "AGENT_TOKEN", "NODE_NAME", "NODE_ROLES", "PLEX_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "node-tmp"))
    monkeypatch.setattr(info, "_ffmpeg", lambda: {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 7.1"})
    app = node_app.create_node_app(config_dir=str(tmp_path / "node-config"), roles=["worker"], key=NODE_KEY, ops={})
    yield RealNode(app)
    app.extensions[node_app.EXTENSION].close()


class TestLibraryRoots:
    def test_enabled_libraries_resolve_through_the_servers_mappings(self, tmp_path):
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies", "/plexmedia/Movies"]},
                    {"id": "2", "name": "Off", "remote_paths": ["/plexmedia/Off"], "enabled": False},
                ],
            ),
            _server(tmp_path, [{"id": "9", "name": "X", "remote_paths": ["/plexmedia/X"]}], enabled=False),
            {"id": "bad", "type": "nonsense"},
        ]
        roots = nm.library_roots(servers)
        assert roots == [
            nm.LibraryRoot("Home", "p1", "Movies", "1", str(tmp_path / "media" / "Movies"), "/plexmedia/Movies")
        ]


class TestSampleFiles:
    def test_it_picks_videos_breadth_first_with_size_and_mtime(self, tmp_path):
        root = tmp_path / "Movies"
        _video(root / "b.mkv", b"12345")
        _video(root / "notes.txt")
        _video(root / "A Film" / "a.MP4")
        _video(root / ".hidden" / "h.mkv")
        found = nm.sample_files(str(root))
        assert [os.path.basename(f["path"]) for f in found] == ["b.mkv", "a.MP4"]
        assert found[0]["size"] == 5
        assert found[0]["mtime_ns"] == (root / "b.mkv").stat().st_mtime_ns

    def test_it_skips_nas_recycle_and_metadata_folders(self, tmp_path):
        root = tmp_path / "Movies"
        _video(root / "#recycle" / "deleted.mkv")
        _video(root / "@eaDir" / "x.mkv")
        _video(root / ".Trash-1000" / "t.mkv")
        _video(root / "Film" / "real.mkv")
        assert [os.path.basename(f["path"]) for f in nm.sample_files(str(root))] == ["real.mkv"]

    def test_it_skips_filesystem_housekeeping_folders(self, tmp_path):
        root = tmp_path / "Movies"
        _video(root / "lost+found" / "#1234.mkv")
        _video(root / "System Volume Information" / "x.mkv")
        _video(root / "Film" / "real.mkv")
        assert [os.path.basename(f["path"]) for f in nm.sample_files(str(root))] == ["real.mkv"]

    def test_film_folders_named_with_hash_or_at_are_sampled(self, tmp_path):
        root = tmp_path / "Movies"
        _video(root / "#Alive (2020)" / "a.mkv")
        _video(root / "@midnight (2013)" / "m.mkv")
        _video(root / "#Recycle" / "deleted.mkv")
        assert sorted(os.path.basename(f["path"]) for f in nm.sample_files(str(root))) == ["a.mkv", "m.mkv"]

    def test_it_stops_at_the_limit_and_the_walk_bound(self, tmp_path):
        root = tmp_path / "Movies"
        for i in range(6):
            _video(root / f"{i}.mkv")
        assert len(nm.sample_files(str(root))) == 3
        assert len(nm.sample_files(str(root), walk_limit=2)) == 2

    def test_a_library_of_thousands_of_folders_still_gives_videos(self, tmp_path):
        root = tmp_path / "Movies"
        for i in range(nm.WALK_LIMIT + 100):
            _video(root / f"Film {i:05d}" / f"Film {i:05d}.mkv")
        found = nm.sample_files(str(root))
        assert len(found) == 3
        assert len({os.path.dirname(f["path"]) for f in found}) == 3

    def test_a_missing_folder_gives_nothing(self, tmp_path):
        assert nm.sample_files(str(tmp_path / "nope")) == []

    def test_symlinked_show_folders_are_walked(self, tmp_path):
        _video(tmp_path / "disk3" / "tv" / "Show" / "s01e01.mkv")
        library = tmp_path / "media" / "tv"
        library.mkdir(parents=True)
        os.symlink(tmp_path / "disk3" / "tv" / "Show", library / "Show")

        found = nm.sample_files(str(library))

        assert [f["path"] for f in found] == [str(library / "Show" / "s01e01.mkv")]

    def test_a_symlink_loop_is_walked_once(self, tmp_path):
        library = tmp_path / "media" / "tv"
        _video(library / "Show" / "s01e01.mkv")
        os.symlink(library, library / "Show" / "again")
        os.symlink(library / "Show", library / "Show" / "self")

        found = nm.sample_files(str(library), limit=10)

        assert [f["path"] for f in found] == [
            str(library / "Show" / "s01e01.mkv"),
            str(library / "Show" / "self" / "s01e01.mkv"),
        ]


class TestRunTestAccess:
    def test_every_library_reports_its_files(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "a.mkv")
        _video(tmp_path / "media" / "TV" / "Show" / "s01e01.mkv")
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                    {"id": "2", "name": "TV", "remote_paths": ["/plexmedia/TV"]},
                ],
            )
        ]
        rows = [PathMapping(str(tmp_path / "media"), "/mnt/media")]
        client = FakeClient(visible=lambda path: "Movies" in path)

        result = nm.run_test_access(client, rows, servers)

        assert len(client.requests) == 1 and client.requests[0][0] == rows
        assert [f["path"] for f in client.requests[0][1]] == [
            str(tmp_path / "media" / "Movies" / "a.mkv"),
            str(tmp_path / "media" / "TV" / "Show" / "s01e01.mkv"),
        ]
        movies, tv = result["libraries"]
        assert {k: movies[k] for k in ("server", "library", "app_root", "checked", "ok", "suggestion", "error")} == {
            "server": "Home",
            "library": "Movies",
            "app_root": str(tmp_path / "media" / "Movies"),
            "checked": 1,
            "ok": True,
            "suggestion": None,
            "error": None,
        }
        assert movies["files"] == [
            {
                "path": str(tmp_path / "media" / "Movies" / "a.mkv"),
                "ok": True,
                "kind": "",
                "message": "",
                "node_path": "/mnt/media/Movies/a.mkv",
            }
        ]
        assert (tv["ok"], tv["files"][0]["kind"], tv["files"][0]["message"]) == (
            False,
            "missing",
            "Not found on the node",
        )

    def test_a_missing_library_says_the_app_cant_open_it(self, tmp_path):
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        client = FakeClient()
        result = nm.run_test_access(client, [PathMapping(str(tmp_path), "/node")], servers)
        lib = result["libraries"][0]
        assert (lib["checked"], lib["ok"]) == (0, False)
        assert "can't open" in lib["error"]
        assert lib["empty"] is False
        assert client.requests == []

    def test_an_empty_library_says_no_videos_were_found(self, tmp_path):
        (tmp_path / "media" / "Movies").mkdir(parents=True)
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        lib = nm.run_test_access(FakeClient(), [PathMapping(str(tmp_path), "/node")], servers)["libraries"][0]
        assert "No video files" in lib["error"]
        assert lib["empty"] is True

    @staticmethod
    def _multi_disk(tmp_path, disks):
        server = _server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/data/Movies"]}])
        server["path_mappings"] = [{"remote_prefix": "/data", "local_prefix": str(tmp_path / d)} for d in disks]
        return [server]

    def test_a_disk_without_the_library_folder_is_left_out_while_another_disk_holds_it(self, tmp_path, real_node):
        _video(tmp_path / "disk1" / "Movies" / "a.mkv", b"x" * 10)
        (tmp_path / "disk2").mkdir()
        _video(tmp_path / "nas" / "disk1" / "Movies" / "a.mkv", b"x" * 10)
        rows = [PathMapping(str(tmp_path / d), str(tmp_path / "nas" / d)) for d in ("disk1", "disk2")]

        libraries = nm.run_test_access(real_node, rows, self._multi_disk(tmp_path, ["disk1", "disk2"]))["libraries"]

        assert [(lib["app_root"], lib["ok"], lib["error"]) for lib in libraries] == [
            (str(tmp_path / "disk1" / "Movies"), True, None)
        ]

    def test_a_library_missing_from_every_disk_says_the_app_cant_open_it(self, tmp_path):
        for disk in ("disk1", "disk2"):
            (tmp_path / disk).mkdir()

        libraries = nm.run_test_access(
            FakeClient(), [PathMapping(str(tmp_path), "/node")], self._multi_disk(tmp_path, ["disk1", "disk2"])
        )["libraries"]

        assert [lib["error"] for lib in libraries] == [
            f"This app can't open {tmp_path / 'disk1' / 'Movies'}",
            f"This app can't open {tmp_path / 'disk2' / 'Movies'}",
        ]

    def test_a_misnamed_node_folder_gets_a_suggestion_from_a_real_node(self, tmp_path, real_node):
        _video(tmp_path / "media" / "Movies" / "a.mkv", b"x" * 10)
        nas = tmp_path / "nas"
        _video(nas / "MOVIES" / "a.mkv", b"x" * 10)
        (nas / "Music").mkdir()
        (nas / "notes.txt").write_text("")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [PathMapping(str(tmp_path / "media" / "Movies"), str(nas / "films"))]

        lib = nm.run_test_access(real_node, rows, servers)["libraries"][0]

        assert (lib["ok"], lib["files"][0]["kind"]) == (False, protocol.NOT_VISIBLE_MISSING)
        assert lib["nearby"] == {str(nas): ["MOVIES", "Music"]}
        assert lib["suggestion"] == {"from": str(nas / "films"), "to": str(nas / "MOVIES")}

    def test_a_node_folder_named_in_another_unicode_form_gets_a_suggestion(self, tmp_path, real_node):
        name = "Films Étrangers"
        _video(tmp_path / "media" / unicodedata.normalize("NFC", name) / "a.mkv", b"x" * 10)
        nas = tmp_path / "nas"
        seen_name = unicodedata.normalize("NFD", name.upper())  # as a Mac or an SMB share may store it
        _video(nas / seen_name / "a.mkv", b"x" * 10)
        servers = [_server(tmp_path, [{"id": "1", "name": "World", "remote_paths": [f"/plexmedia/{name}"]}])]
        rows = [PathMapping(str(tmp_path / "media" / name), str(nas / "films"))]

        lib = nm.run_test_access(real_node, rows, servers)["libraries"][0]

        assert lib["suggestion"] == {"from": str(nas / "films"), "to": str(nas / seen_name)}

    def test_a_root_below_the_mapped_folder_moves_the_rows_prefix_up_to_match(self, tmp_path, real_node):
        _video(tmp_path / "media" / "Movies" / "a.mkv", b"x" * 10)
        share = tmp_path / "volume1"
        _video(share / "Movies" / "a.mkv", b"x" * 10)
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [PathMapping(str(tmp_path / "media"), str(share / "old"))]

        lib = nm.run_test_access(real_node, rows, servers)["libraries"][0]

        assert lib["suggestion"] == {"from": str(share / "old"), "to": str(share)}, lib["nearby"]
        fixed = [PathMapping(str(tmp_path / "media"), lib["suggestion"]["to"])]
        assert nm.run_test_access(real_node, fixed, servers)["libraries"][0]["ok"] is True

    def test_a_root_below_the_mapped_folder_is_not_matched_to_a_folder_named_in_another_case(self, tmp_path, real_node):
        # Moving the row up would map the library to volume1/Movies, which a Linux node doesn't have.
        _video(tmp_path / "media" / "Movies" / "a.mkv", b"x" * 10)
        share = tmp_path / "volume1"
        _video(share / "movies" / "a.mkv", b"x" * 10)
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [PathMapping(str(tmp_path / "media"), str(share / "old"))]

        lib = nm.run_test_access(real_node, rows, servers)["libraries"][0]

        assert (lib["ok"], lib["suggestion"]) == (False, None), lib["nearby"]

    def test_a_library_the_node_doesnt_mount_leaves_the_row_the_others_pass_through_alone(self, tmp_path, real_node):
        # Seen live: the missing Open Films library's last folder "Movies" matched the movies library's own folder, and
        # the suggestion moved the shared row to "/", breaking both libraries that passed.
        app, node = tmp_path / "media", tmp_path / "node"
        for folder in (app, node):
            _video(folder / "movies" / "a.mkv", b"x" * 10)
            _video(folder / "tv" / "s01e01.mkv", b"x" * 10)
        _video(app / "openfilms" / "Movies" / "b.mkv", b"x" * 10)
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/movies"]},
                    {"id": "2", "name": "TV", "remote_paths": ["/plexmedia/tv"]},
                    {"id": "3", "name": "Open Films", "remote_paths": ["/plexmedia/openfilms/Movies"]},
                ],
            )
        ]
        rows = [PathMapping(str(app), str(node))]

        movies, tv, films = nm.run_test_access(real_node, rows, servers, node_name="lab-cpu")["libraries"]

        assert (movies["ok"], tv["ok"], films["ok"]) == (True, True, False)
        assert films["suggestion"] is None
        assert films["error"] == (
            "lab-cpu doesn't have this library's folder. Mount it on the node, or give each library its own row and leave "
            "this one out, so other workers take its files."
        )

    def test_a_library_no_row_covers_is_listed_as_not_mapped_and_never_sent_to_the_node(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "a.mkv")
        _video(tmp_path / "media" / "Anime" / "b.mkv")
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                    {"id": "2", "name": "Anime", "remote_paths": ["/plexmedia/Anime"]},
                ],
            )
        ]
        rows = [PathMapping(str(tmp_path / "media" / "Movies"), "/mnt/Movies")]
        client = FakeClient()

        movies, anime = nm.run_test_access(client, rows, servers, node_name="nuc")["libraries"]

        assert [os.path.basename(f["path"]) for f in client.requests[0][1]] == ["a.mkv"]
        assert (movies["ok"], movies["mapped"]) == (True, True)
        assert (anime["mapped"], anime["checked"], anime["ok"]) == (False, 0, False)
        assert anime["error"] == "Not mapped on nuc. Other workers take its files."
        assert reg.access_passed({"libraries": [movies, anime]}) is True

    def test_a_library_only_some_of_whose_folders_are_mapped_tests_the_files_under_those_rows(self, tmp_path):
        # The node has disk A of the library: it runs A's files, so A's files are the ones to test.
        _video(tmp_path / "media" / "Movies" / "0-first.mkv")
        _video(tmp_path / "media" / "Movies" / "A" / "a.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [PathMapping(str(tmp_path / "media" / "Movies" / "A"), "/mnt/A")]
        client = FakeClient()

        (movies,) = nm.run_test_access(client, rows, servers, node_name="nuc")["libraries"]

        assert [os.path.basename(f["path"]) for f in client.requests[0][1]] == ["a.mkv"]
        assert (movies["mapped"], movies["checked"], movies["ok"]) == (True, 1, True)

    def test_a_partly_mapped_library_the_node_cant_see_fails_without_a_suggestion(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "A" / "a.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [PathMapping(str(tmp_path / "media" / "Movies" / "A"), "/mnt/A")]

        (movies,) = nm.run_test_access(FakeClient(visible=lambda p: False), rows, servers)["libraries"]

        assert (movies["mapped"], movies["ok"], movies["suggestion"]) == (True, False, None)

    def test_every_row_inside_a_library_is_tested(self, tmp_path):
        for disk in ("A", "B"):
            for n in range(3):
                _video(tmp_path / "media" / "Movies" / disk / f"{disk}{n}.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [
            PathMapping(str(tmp_path / "media" / "Movies" / "A"), "/mnt/A"),
            PathMapping(str(tmp_path / "media" / "Movies" / "B"), "/mnt/B"),
        ]

        (movies,) = nm.run_test_access(FakeClient(visible=lambda p: "/B/" not in p), rows, servers)["libraries"]

        assert movies["ok"] is False
        assert {os.path.basename(f["path"])[0] for f in movies["files"]} == {"A", "B"}

    def test_an_inner_row_this_app_cant_open_fails_the_library(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "A" / "a.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        missing = tmp_path / "media" / "Movies" / "B"
        rows = [
            PathMapping(str(tmp_path / "media" / "Movies" / "A"), "/mnt/A"),
            PathMapping(str(missing), "/mnt/B"),
        ]

        (movies,) = nm.run_test_access(FakeClient(), rows, servers)["libraries"]

        assert (movies["checked"], movies["ok"]) == (1, False)
        assert movies["error"] == f"This app can't open {missing}"
        assert reg.access_passed({"libraries": [movies]}) is False

    def test_an_unreadable_inner_row_is_named_though_another_inner_row_is_empty(self, tmp_path):
        (tmp_path / "media" / "Movies" / "A").mkdir(parents=True)
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        missing = tmp_path / "media" / "Movies" / "B"
        rows = [
            PathMapping(str(tmp_path / "media" / "Movies" / "A"), "/mnt/A"),
            PathMapping(str(missing), "/mnt/B"),
        ]

        (movies,) = nm.run_test_access(FakeClient(), rows, servers)["libraries"]

        assert (movies["ok"], movies["error"]) == (False, f"This app can't open {missing}")

    def test_overlapping_rows_inside_a_library_test_each_file_once(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "A" / "sub" / "s.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [
            PathMapping(str(tmp_path / "media" / "Movies" / "A"), "/mnt/A"),
            PathMapping(str(tmp_path / "media" / "Movies" / "A" / "sub"), "/mnt/sub"),
        ]

        (movies,) = nm.run_test_access(FakeClient(), rows, servers)["libraries"]

        assert movies["checked"] == 1

    def test_a_covered_library_the_node_lacks_still_fails(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "a.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [PathMapping(str(tmp_path / "media"), "/mnt")]

        (movies,) = nm.run_test_access(FakeClient(visible=lambda p: False), rows, servers)["libraries"]

        assert (movies["mapped"], movies["ok"]) == (True, False)
        assert reg.access_passed({"libraries": [movies]}) is False

    def test_a_folder_that_is_another_librarys_is_not_suggested(self, tmp_path, real_node):
        _video(tmp_path / "media" / "Films" / "a.mkv", b"x" * 10)
        _video(tmp_path / "media" / "Movies" / "b.mkv", b"x" * 10)
        node = tmp_path / "node"
        _video(node / "movies" / "a.mkv", b"x" * 10)
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Films", "remote_paths": ["/plexmedia/Films"]},
                    {"id": "2", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                ],
            )
        ]
        rows = [
            PathMapping(str(tmp_path / "media" / "Films"), str(node / "movies")),
            PathMapping(str(tmp_path / "media" / "Movies"), str(node / "gone")),
        ]

        films, movies = nm.run_test_access(real_node, rows, servers)["libraries"]

        # The node's movies folder is where the Films row points, so it isn't where the node keeps Movies.
        assert (films["ok"], movies["ok"], movies["suggestion"]) == (True, False, None), movies["nearby"]

    def test_nothing_is_listed_above_the_node_folders_parent(self, tmp_path, real_node):
        _video(tmp_path / "media" / "Movies" / "a.mkv", b"x" * 10)
        _video(tmp_path / "nas" / "Movies" / "a.mkv", b"x" * 10)
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [PathMapping(str(tmp_path / "media" / "Movies"), str(tmp_path / "nas" / "gone" / "films"))]

        lib = nm.run_test_access(real_node, rows, servers)["libraries"][0]

        assert (lib["ok"], lib["nearby"], lib["suggestion"]) == (False, {}, None)

    def test_each_library_gets_only_the_listings_above_its_own_missing_files(self, tmp_path, real_node):
        _video(tmp_path / "media" / "Movies" / "a.mkv", b"x" * 10)
        _video(tmp_path / "media" / "TV" / "s01e01.mkv", b"x" * 10)
        nas = tmp_path / "nas"
        _video(nas / "Movies" / "a.mkv", b"x" * 10)
        _video(tmp_path / "srv" / "tv" / "s01e01.mkv", b"x" * 10)
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                    {"id": "2", "name": "TV", "remote_paths": ["/plexmedia/TV"]},
                ],
            )
        ]
        rows = [
            PathMapping(str(tmp_path / "media" / "Movies"), str(nas)),
            PathMapping(str(tmp_path / "media" / "TV"), str(tmp_path / "srv" / "tv")),
        ]

        movies, tv = nm.run_test_access(real_node, rows, servers)["libraries"]

        assert movies["nearby"] == {str(nas): ["Movies"]}
        assert movies["suggestion"] == {"from": str(nas), "to": str(nas / "Movies")}
        assert (tv["ok"], tv["nearby"], tv["suggestion"]) == (True, {}, None)

    def test_requests_are_split_into_batches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nm, "CHECK_BATCH", 2)
        for i in range(3):
            _video(tmp_path / "media" / "Movies" / f"{i}.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        client = FakeClient()
        assert (
            nm.run_test_access(client, [PathMapping(str(tmp_path), "/node")], servers)["libraries"][0]["checked"] == 3
        )
        assert [len(files) for _, files in client.requests] == [2, 1]


class TestSuggestRow:
    """Windows node paths, which the real node can't produce on this machine; the listing is what it would send."""

    def test_a_windows_node(self):
        rows = [PathMapping("/data/movies", "E:\\Films")]
        suggestion = nm.suggest_row("/data/movies", rows, {"E:\\": ["MOVIES", "TV"]})
        assert suggestion == {"from": "E:\\Films", "to": "E:\\MOVIES"}

    def test_a_windows_node_with_the_root_below_the_mapped_folder(self):
        rows = [PathMapping("/data", "E:\\Media\\Old")]
        suggestion = nm.suggest_row("/data/movies", rows, {"E:\\Media": ["Movies", "TV"]})
        assert suggestion == {"from": "E:\\Media\\Old", "to": "E:\\Media"}

    def test_an_exact_name_wins_over_one_in_another_case(self):
        rows = [PathMapping("/data/Movies", "/mnt/x")]
        assert nm.suggest_row("/data/Movies", rows, {"/mnt": ["MOVIES", "Movies"]})["to"] == "/mnt/Movies"

    def test_a_folder_named_like_the_root_two_levels_down_is_not_the_root(self):
        rows = [PathMapping("/media", "/media")]
        assert nm.suggest_row("/media/openfilms/Movies", rows, {"/media": ["movies", "scratch", "tv"]}) is None

    def test_another_librarys_folder_is_never_suggested(self):
        rows = [PathMapping("/data/Movies", "/mnt/x")]
        assert nm.suggest_row("/data/Movies", rows, {"/mnt": ["Movies"]}, taken={"/mnt/Movies"}) is None

    @pytest.mark.parametrize(
        ("rows", "nearby"),
        [
            ([], {"/mnt": ["Movies"]}),
            ([PathMapping("/data/Movies", "/mnt/Movies")], {"/mnt": ["Movies"]}),
            ([PathMapping("/data/Movies", "/mnt/x")], {"/mnt": ["Music"]}),
            ([PathMapping("/data/Movies", "/mnt/x")], "nonsense"),
        ],
        ids=["unmapped", "already-there", "no-match", "bad-listing"],
    )
    def test_no_suggestion(self, rows, nearby):
        assert nm.suggest_row("/data/Movies", rows, nearby) is None


class TestQuickAccessCheck:
    """Check now's quick access check: one file per library, the libraries Test access samples."""

    LIBRARIES = [
        {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
        {"id": "2", "name": "Open Films", "remote_paths": ["/plexmedia/Open Films"]},
    ]
    ROWS = [PathMapping("/", "/m")]  # what the node is told; the app's folders come from the servers

    def _media(self, tmp_path):
        for name in ("a.mkv", "b.mkv"):
            _video(tmp_path / "media" / "Movies" / name)
        _video(tmp_path / "media" / "Open Films" / "c.mkv")
        return [_server(tmp_path, self.LIBRARIES)]

    def test_one_file_per_library(self, tmp_path):
        servers = self._media(tmp_path)
        client = FakeClient()
        assert nm.quick_access_check(client, self.ROWS, servers) is True
        assert [os.path.basename(f["path"]) for f in client.requests[0][1]] == ["a.mkv", "c.mkv"]

    def test_a_library_the_node_cant_see_fails_it_though_another_under_the_same_row_passes(self, tmp_path):
        # One row (/media) covers both libraries: one file under the row passed while Open Films was still out of
        # the node's sight, so Check now un-paused a node Test access still failed.
        servers = self._media(tmp_path)
        rows = [PathMapping(str(tmp_path / "media"), "/media")]
        client = FakeClient(visible=lambda p: "Open Films" not in p)
        assert nm.quick_access_check(client, rows, servers) is False

    def test_a_library_no_row_covers_is_not_asked_about(self, tmp_path):
        servers = self._media(tmp_path)
        client = FakeClient()
        rows = [PathMapping(str(tmp_path / "media" / "Movies"), "/m")]
        assert nm.quick_access_check(client, rows, servers) is True
        assert [os.path.basename(f["path"]) for f in client.requests[0][1]] == ["a.mkv"]

    def test_a_row_inside_a_library_is_checked_through_its_own_files(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "0-first.mkv")
        _video(tmp_path / "media" / "Movies" / "A" / "a.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        client = FakeClient()
        rows = [PathMapping(str(tmp_path / "media" / "Movies" / "A"), "/mnt/A")]
        assert nm.quick_access_check(client, rows, servers) is True
        assert [os.path.basename(f["path"]) for f in client.requests[0][1]] == ["a.mkv"]

    def test_an_inner_row_this_app_cant_open_fails_it_as_test_access_does(self, tmp_path):
        _video(tmp_path / "media" / "Movies" / "A" / "a.mkv")
        servers = [_server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]}])]
        rows = [
            PathMapping(str(tmp_path / "media" / "Movies" / "A"), "/mnt/A"),
            PathMapping(str(tmp_path / "media" / "Movies" / "B"), "/mnt/B"),
        ]
        assert nm.quick_access_check(FakeClient(), rows, servers) is False

    def test_a_file_the_node_cant_see_fails_it(self, tmp_path):
        servers = self._media(tmp_path)
        assert nm.quick_access_check(FakeClient(visible=lambda p: False), self.ROWS, servers) is False

    def test_a_folder_that_hangs_fails_it_within_the_budget(self, tmp_path, monkeypatch):
        import threading
        import time

        servers = self._media(tmp_path)
        stalled = tmp_path / "media" / "Open Films"
        release = threading.Event()
        real_scandir = os.scandir

        def scandir(path):
            if os.fspath(path) == str(stalled):
                release.wait(30)  # stands in for a read stuck on a dead NFS server
            return real_scandir(path)

        monkeypatch.setattr(nm.os, "scandir", scandir)
        monkeypatch.setattr(nm, "SAMPLE_BUDGET_S", 0.5)
        client = FakeClient()
        started = time.monotonic()
        try:
            passed = nm.quick_access_check(client, self.ROWS, servers)
        finally:
            release.set()
        assert time.monotonic() - started < 5
        assert passed is False
        assert client.requests == []

    def test_nothing_to_check_does_not_pass(self, tmp_path):
        servers = [_server(tmp_path, self.LIBRARIES)]
        assert nm.quick_access_check(FakeClient(), self.ROWS, servers) is False


def test_default_mappings_are_one_row_per_top_library_folder(tmp_path):
    servers = [
        _server(
            tmp_path,
            [
                {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                {"id": "2", "name": "4K", "remote_paths": ["/plexmedia/Movies/4K"]},
                {"id": "3", "name": "TV", "remote_paths": ["/plexmedia/TV"]},
            ],
        )
    ]
    movies, tv = str(tmp_path / "media" / "Movies"), str(tmp_path / "media" / "TV")
    for folder in (movies, tv, f"{movies}/4K"):
        os.makedirs(folder)
    assert nm.default_mappings_for(servers) == [
        {"app_prefix": movies, "node_prefix": movies},
        {"app_prefix": tv, "node_prefix": tv},
    ]


def test_default_mappings_never_exceed_what_a_save_accepts(tmp_path):
    names = [f"L{n:03}" for n in range(MAX_MAPPINGS + 5)]
    servers = [
        _server(tmp_path, [{"id": str(i), "name": n, "remote_paths": [f"/plexmedia/{n}"]} for i, n in enumerate(names)])
    ]
    for name in names:
        os.makedirs(tmp_path / "media" / name)
    assert len(nm.default_mappings_for(servers)) == MAX_MAPPINGS


def test_default_mappings_leave_out_folders_this_app_cant_open(tmp_path):
    """The lab's Synth libraries weren't mounted in the app: Add node suggested a row for each, and each then failed
    Test access on this app's side."""
    servers = [
        _server(
            tmp_path,
            [
                {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                {"id": "2", "name": "Synth", "remote_paths": ["/plexmedia/Synth"]},
            ],
        )
    ]
    movies = str(tmp_path / "media" / "Movies")
    os.makedirs(movies)
    assert nm.default_mappings_for(servers) == [{"app_prefix": movies, "node_prefix": movies}]


def test_default_mappings_keep_a_folder_whose_read_hangs(tmp_path, monkeypatch):
    import threading

    servers = [_server(tmp_path, [{"id": "1", "name": "TV", "remote_paths": ["/plexmedia/TV"]}])]
    release = threading.Event()
    monkeypatch.setattr(nm.os, "scandir", lambda path: release.wait(5))
    try:
        tv = str(tmp_path / "media" / "TV")
        assert nm.default_mappings_for(servers, budget_s=0.2) == [{"app_prefix": tv, "node_prefix": tv}]
    finally:
        release.set()


class TestAStalledMount:
    """A directory read on a stalled network mount blocks in the kernel; Test access answers anyway."""

    def test_a_library_that_hangs_times_out_and_the_others_are_still_checked(self, tmp_path, monkeypatch):
        import threading
        import time

        _video(tmp_path / "media" / "Movies" / "a.mkv")
        stalled = tmp_path / "media" / "TV"
        stalled.mkdir(parents=True)
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                    {"id": "2", "name": "TV", "remote_paths": ["/plexmedia/TV"]},
                ],
            )
        ]
        release = threading.Event()
        real_scandir = os.scandir

        def scandir(path):
            if os.fspath(path) == str(stalled):
                release.wait(30)  # stands in for a read stuck on a dead NFS server
            return real_scandir(path)

        monkeypatch.setattr(nm.os, "scandir", scandir)
        monkeypatch.setattr(nm, "SAMPLE_BUDGET_S", 0.5)
        client = FakeClient()
        started = time.monotonic()
        try:
            result = nm.run_test_access(client, [PathMapping(str(tmp_path / "media"), "/mnt/media")], servers)
        finally:
            release.set()
        assert time.monotonic() - started < 5
        movies, tv = result["libraries"]
        assert (movies["checked"], movies["ok"], movies["error"]) == (1, True, None)
        assert (tv["checked"], tv["ok"]) == (0, False)
        assert tv["error"] == f"Timed out reading {stalled} (no answer within 0.5 s)"
        assert [f["path"] for f in client.requests[0][1]] == [str(tmp_path / "media" / "Movies" / "a.mkv")]

    def test_a_walk_still_going_at_the_deadline_stops_between_reads(self, tmp_path):
        import time

        _video(tmp_path / "lib" / "a" / "x.mkv")
        sampling = nm._Sampling()
        nm._sample_into(sampling, str(tmp_path / "lib"), limit=3, walk_limit=500, deadline=time.monotonic() - 1)
        assert sampling.timed_out is True and sampling.found == []


class TestInnerRowsOneRule:
    """Test access and the quick access check fail a library tested through rows inside it on the same terms."""

    @staticmethod
    def _setup(tmp_path):
        """Movies has no row of its own, only rows A and B inside it; TV, which a row covers, always passes."""
        _video(tmp_path / "media" / "TV" / "t.mkv")
        servers = [
            _server(
                tmp_path,
                [
                    {"id": "1", "name": "Movies", "remote_paths": ["/plexmedia/Movies"]},
                    {"id": "2", "name": "TV", "remote_paths": ["/plexmedia/TV"]},
                ],
            )
        ]
        movies = tmp_path / "media" / "Movies"
        rows = [
            PathMapping(str(movies / "A"), "/mnt/A"),
            PathMapping(str(movies / "B"), "/mnt/B"),
            PathMapping(str(tmp_path / "media" / "TV"), "/mnt/TV"),
        ]
        return servers, rows, movies

    @pytest.mark.parametrize(
        ("case", "passes", "error"),
        [
            ("both_fine", True, None),
            ("all_unreadable", False, "This app can't open {A}"),
            ("one_of_two_unreadable", False, "This app can't open {B}"),
            ("one_timed_out", False, "Timed out reading {B} (no answer within 0.5 s)"),
            ("one_the_node_cant_see", False, None),
        ],
    )
    def test_both_checks_agree(self, tmp_path, monkeypatch, case, passes, error):
        import threading

        servers, rows, movies = self._setup(tmp_path)
        if case != "all_unreadable":
            _video(movies / "A" / "a.mkv")
        if case in ("both_fine", "one_the_node_cant_see", "one_timed_out"):
            _video(movies / "B" / "b.mkv")
        visible = (lambda p: "/B/" not in p) if case == "one_the_node_cant_see" else (lambda p: True)
        release = threading.Event()
        if case == "one_timed_out":
            real_scandir = os.scandir

            def scandir(path):
                if os.fspath(path) == str(movies / "B"):
                    release.wait(30)  # stands in for a read stuck on a dead NFS server
                return real_scandir(path)

            monkeypatch.setattr(nm.os, "scandir", scandir)
            monkeypatch.setattr(nm, "SAMPLE_BUDGET_S", 0.5)
        try:
            result = nm.run_test_access(FakeClient(visible=visible), rows, servers)
            quick = nm.quick_access_check(FakeClient(visible=visible), rows, servers)
        finally:
            release.set()

        library = next(lib for lib in result["libraries"] if lib["library"] == "Movies")
        assert (reg.access_passed(result), quick) == (passes, passes)
        assert library["ok"] is passes
        expected = error.format(A=movies / "A", B=movies / "B") if error else None
        assert library["error"] == expected
        assert library["unreadable"] is (error is not None)

    def test_a_disk_whose_inner_rows_cant_be_opened_fails_though_another_disk_holds_the_folder(self, tmp_path):
        _video(tmp_path / "disk2" / "Movies" / "a.mkv")
        server = _server(tmp_path, [{"id": "1", "name": "Movies", "remote_paths": ["/data/Movies"]}])
        server["path_mappings"] = [
            {"remote_prefix": "/data", "local_prefix": str(tmp_path / d)} for d in ("disk1", "disk2")
        ]
        missing = tmp_path / "disk1" / "Movies" / "A"
        rows = [PathMapping(str(missing), "/mnt/A"), PathMapping(str(tmp_path / "disk2"), "/mnt/disk2")]

        result = nm.run_test_access(FakeClient(), rows, [server])

        assert [(lib["app_root"], lib["ok"], lib["error"]) for lib in result["libraries"]] == [
            (str(tmp_path / "disk1" / "Movies"), False, f"This app can't open {missing}"),
            (str(tmp_path / "disk2" / "Movies"), True, None),
        ]
        assert reg.access_passed(result) is False
        assert nm.quick_access_check(FakeClient(), rows, [server]) is False

    def test_the_library_folder_itself_is_not_read(self, tmp_path, monkeypatch):
        import threading
        import time

        servers, rows, movies = self._setup(tmp_path)
        for disk in ("A", "B"):
            _video(movies / disk / f"{disk}.mkv")
        release = threading.Event()
        real_scandir = os.scandir
        read: list[str] = []

        def scandir(path):
            read.append(os.fspath(path))
            if os.fspath(path) == str(movies):
                release.wait(30)  # a stalled library root would hold Test access for the whole budget
            return real_scandir(path)

        monkeypatch.setattr(nm.os, "scandir", scandir)
        monkeypatch.setattr(nm, "SAMPLE_BUDGET_S", 3.0)
        started = time.monotonic()
        try:
            result = nm.run_test_access(FakeClient(), rows, servers)
        finally:
            release.set()

        assert time.monotonic() - started < 2
        assert str(movies) not in read
        assert reg.access_passed(result) is True


class TestFilesVisible:
    ROWS = [PathMapping("/", "/m")]

    def test_it_sends_each_file_with_the_size_and_time_this_app_sees(self, tmp_path):
        path = tmp_path / "a.mkv"
        path.write_bytes(b"x" * 10)
        client = FakeClient()

        assert nm.files_visible(client, self.ROWS, [str(path)]) is True

        st = os.stat(path)
        assert client.requests == [(self.ROWS, [{"path": str(path), "size": 10, "mtime_ns": st.st_mtime_ns}])]

    def test_one_the_node_still_cant_see_fails_it(self, tmp_path):
        seen, unseen = tmp_path / "a.mkv", tmp_path / "b.mkv"
        seen.write_bytes(b"x")
        unseen.write_bytes(b"x")
        client = FakeClient(visible=lambda p: p == str(seen))

        assert nm.files_visible(client, self.ROWS, [str(seen), str(unseen)]) is False

    def test_files_gone_from_this_app_too_are_skipped(self, tmp_path):
        client = FakeClient(visible=lambda p: False)

        assert nm.files_visible(client, self.ROWS, [str(tmp_path / "replaced-old-name.mkv")]) is True
        assert client.requests == []

    def test_a_node_that_doesnt_answer_fails_it(self, tmp_path):
        path = tmp_path / "a.mkv"
        path.write_bytes(b"x")

        class Gone:
            def access_check(self, mappings, files):
                raise reg.NodeUnreachableError("gone")

        assert nm.files_visible(Gone(), self.ROWS, [str(path)]) is False
