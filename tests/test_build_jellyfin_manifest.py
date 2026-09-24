"""scripts/build_jellyfin_manifest.py: the Jellyfin plugin manifest, rebuilt from plugin-v* releases.

Every Pages deploy runs the builder, so whichever deploy wins the `pages` queue ships the same file.
Real Jellyfin servers poll it, so the builder must fail closed: a download error, a digest mismatch or
an empty version list stops the deploy instead of publishing a broken or empty manifest.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import urllib.error
from pathlib import Path

import pytest

from scripts import build_jellyfin_manifest as builder
from scripts.build_jellyfin_manifest import ManifestError, build_manifest, list_releases, main, render

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "jellyfin-plugin" / "manifest.template.json"
DOWNLOAD = "https://github.com/stevezau/media_preview_generator/releases/download"
API = "https://api.github.com/repos/stevezau/media_preview_generator/releases"
PUBLISHED = "2026-06-29T04:38:25Z"


def _zip_bytes(version: str) -> bytes:
    return f"zip for {version}".encode()


def _asset(tag: str, version: str, **overrides: object) -> dict:
    name = f"media-preview-bridge_{version}.zip"
    asset = {
        "name": name,
        "state": "uploaded",
        "digest": "sha256:" + hashlib.sha256(_zip_bytes(version)).hexdigest(),
        "browser_download_url": f"{DOWNLOAD}/{tag}/{name}",
    }
    asset.update(overrides)
    return asset


def _release(tag_version: str, abis: tuple[str, ...] = ("10.11", "12.0"), **overrides: object) -> dict:
    tag = f"plugin-v{tag_version}"
    build = tag_version.split(".", 2)[2]
    release = {
        "tag_name": tag,
        "draft": False,
        "published_at": PUBLISHED,
        "assets": [_asset(tag, f"{abi}.{build}") for abi in abis],
    }
    release.update(overrides)
    return release


class FakeDownloads:
    """Serves each release zip's fake bytes and records which URLs were fetched."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.fetched.append(url)
        version = url.rsplit("/", 1)[1].removeprefix("media-preview-bridge_").removesuffix(".zip")
        return _zip_bytes(version)


@pytest.fixture
def template() -> list[dict]:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def downloads() -> FakeDownloads:
    return FakeDownloads()


def _versions(manifest: list[dict]) -> list[str]:
    return [entry["version"] for entry in manifest[0]["versions"]]


class TestEntries:
    def test_dual_abi_release_gives_one_entry_per_abi_when_both_zips_are_uploaded(self, template, downloads) -> None:
        manifest = build_manifest(template, [_release("10.11.0.4")], downloads)

        tag_url = f"{DOWNLOAD}/plugin-v10.11.0.4"
        assert manifest[0]["versions"] == [
            {
                "version": "12.0.0.4",
                "changelog": "Automated release for plugin-v10.11.0.4.",
                "targetAbi": "12.0.0.0",
                "sourceUrl": f"{tag_url}/media-preview-bridge_12.0.0.4.zip",
                "checksum": hashlib.md5(_zip_bytes("12.0.0.4")).hexdigest().upper(),
                "timestamp": PUBLISHED,
            },
            {
                "version": "10.11.0.4",
                "changelog": "Automated release for plugin-v10.11.0.4.",
                "targetAbi": "10.11.0.0",
                "sourceUrl": f"{tag_url}/media-preview-bridge_10.11.0.4.zip",
                "checksum": hashlib.md5(_zip_bytes("10.11.0.4")).hexdigest().upper(),
                "timestamp": PUBLISHED,
            },
        ]
        assert sorted(downloads.fetched) == sorted(entry["sourceUrl"] for entry in manifest[0]["versions"])

    def test_legacy_release_gives_one_entry_when_only_the_10_11_zip_exists(self, template, downloads) -> None:
        release = _release("10.11.0.3", abis=("10.11",), published_at="2026-05-08T21:19:30Z")

        manifest = build_manifest(template, [release], downloads)

        assert manifest[0]["versions"] == [
            {
                "version": "10.11.0.3",
                "changelog": "Automated release for plugin-v10.11.0.3.",
                "targetAbi": "10.11.0.0",
                "sourceUrl": f"{DOWNLOAD}/plugin-v10.11.0.3/media-preview-bridge_10.11.0.3.zip",
                "checksum": hashlib.md5(_zip_bytes("10.11.0.3")).hexdigest().upper(),
                "timestamp": "2026-05-08T21:19:30Z",
            }
        ]

    def test_plugin_metadata_comes_from_the_template(self, template, downloads) -> None:
        manifest = build_manifest(template, [_release("10.11.0.4")], downloads)

        assert len(manifest) == 1
        assert {k: v for k, v in manifest[0].items() if k != "versions"} == {
            k: v for k, v in template[0].items() if k != "versions"
        }
        assert _versions(manifest) == ["12.0.0.4", "10.11.0.4"]


class TestIgnoredReleases:
    def test_app_emby_and_draft_releases_are_ignored(self, template, downloads) -> None:
        app = {"tag_name": "4.4.2", "draft": False, "published_at": PUBLISHED, "assets": []}
        app_v = {"tag_name": "v4.4.1", "draft": False, "published_at": PUBLISHED, "assets": []}
        emby = {
            "tag_name": "emby-plugin-v1.0.0.0",
            "draft": False,
            "published_at": PUBLISHED,
            "assets": [_asset("emby-plugin-v1.0.0.0", "10.11.0.0")],
        }
        draft = _release("10.11.0.9", draft=True, published_at=None)

        manifest = build_manifest(template, [app, app_v, emby, draft, _release("10.11.0.4")], downloads)

        assert _versions(manifest) == ["12.0.0.4", "10.11.0.4"]
        assert all("/plugin-v10.11.0.4/" in url for url in downloads.fetched)

    def test_assets_not_named_for_the_release_are_ignored(self, template, downloads) -> None:
        release = _release("10.11.0.4")
        release["assets"] += [
            _asset("plugin-v10.11.0.4", "10.11.0.3"),
            {"name": "notes.txt", "state": "uploaded", "browser_download_url": f"{DOWNLOAD}/x/notes.txt"},
        ]

        manifest = build_manifest(template, [release], downloads)

        assert _versions(manifest) == ["12.0.0.4", "10.11.0.4"]
        assert len(downloads.fetched) == 2


class TestMissingZips:
    def test_release_without_its_zip_is_skipped_with_a_warning(self, template, downloads, capsys) -> None:
        manifest = build_manifest(template, [_release("10.11.0.4", abis=()), _release("10.11.0.3")], downloads)

        assert _versions(manifest) == ["12.0.0.3", "10.11.0.3"]
        err = capsys.readouterr().err
        assert "::warning::" in err
        assert "plugin-v10.11.0.4" in err

    def test_zip_still_uploading_is_skipped_with_a_warning(self, template, downloads, capsys) -> None:
        release = _release("10.11.0.4")
        release["assets"][1]["state"] = "starter"  # the 12.0 zip

        manifest = build_manifest(template, [release], downloads)

        assert _versions(manifest) == ["10.11.0.4"]
        assert downloads.fetched == [f"{DOWNLOAD}/plugin-v10.11.0.4/media-preview-bridge_10.11.0.4.zip"]
        err = capsys.readouterr().err
        assert "::warning::" in err
        assert "media-preview-bridge_12.0.0.4.zip" in err


class TestFailsClosed:
    def test_download_error_propagates(self, template) -> None:
        def broken(url: str) -> bytes:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        with pytest.raises(urllib.error.HTTPError):
            build_manifest(template, [_release("10.11.0.4")], broken)

    def test_digest_mismatch_raises(self, template, downloads) -> None:
        release = _release("10.11.0.4")
        release["assets"][0]["digest"] = "sha256:" + hashlib.sha256(b"something else").hexdigest()

        with pytest.raises(ManifestError, match="media-preview-bridge_10.11.0.4.zip"):
            build_manifest(template, [release], downloads)

    def test_asset_without_a_digest_raises(self, template, downloads) -> None:
        release = _release("10.11.0.4")
        release["assets"][0]["digest"] = None

        with pytest.raises(ManifestError, match="digest"):
            build_manifest(template, [release], downloads)

    @pytest.mark.parametrize(
        "releases",
        [
            pytest.param([], id="no releases at all"),
            pytest.param(
                [{"tag_name": "4.4.2", "draft": False, "published_at": PUBLISHED, "assets": []}],
                id="only app releases",
            ),
            pytest.param([_release("10.11.0.4", abis=())], id="plugin release with no zip"),
        ],
    )
    def test_zero_versions_raises_so_the_bare_template_is_never_published(self, template, downloads, releases) -> None:
        with pytest.raises(ManifestError, match="no versions"):
            build_manifest(template, releases, downloads)

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda t: t[0]["versions"].append({"version": "10.11.0.0"}), id="versions not empty"),
            pytest.param(lambda t: t.append(copy.deepcopy(t[0])), id="two plugins"),
            pytest.param(lambda t: t.clear(), id="no plugin"),
        ],
    )
    def test_malformed_template_raises(self, template, downloads, mutate) -> None:
        mutate(template)

        with pytest.raises(ManifestError, match="template"):
            build_manifest(template, [_release("10.11.0.4")], downloads)


class TestOrdering:
    def test_versions_are_newest_first_with_12_0_before_10_11(self, template, downloads) -> None:
        releases = [
            _release("10.11.0.2", abis=("10.11",)),
            _release("10.11.0.10"),
            _release("10.11.0.9"),
        ]

        manifest = build_manifest(template, releases, downloads)

        assert _versions(manifest) == ["12.0.0.10", "12.0.0.9", "10.11.0.10", "10.11.0.9", "10.11.0.2"]

    def test_shuffled_input_gives_byte_identical_output(self, template, downloads) -> None:
        releases = [_release("10.11.0.2", abis=("10.11",)), _release("10.11.0.3"), _release("10.11.0.4")]
        shuffled = copy.deepcopy(releases[::-1])
        for release in shuffled:
            random.Random(7).shuffle(release["assets"])

        rendered = render(build_manifest(template, releases, downloads))

        assert render(build_manifest(template, shuffled, downloads)) == rendered
        assert _versions(json.loads(rendered)) == ["12.0.0.4", "12.0.0.3", "10.11.0.4", "10.11.0.3", "10.11.0.2"]


class TestRaceRegression:
    def test_a_later_deploy_lists_a_release_whose_own_deploy_was_cancelled(self, template, downloads) -> None:
        # The docs deploy that replaced a cancelled plugin deploy only has the release list to go on.
        pristine = copy.deepcopy(template)
        before = build_manifest(template, [_release("10.11.0.2"), _release("10.11.0.3")], downloads)

        after = build_manifest(
            template, [_release("10.11.0.2"), _release("10.11.0.3"), _release("10.11.0.4")], downloads
        )

        assert "10.11.0.4" not in _versions(before)
        assert _versions(after)[:1] == ["12.0.0.4"]
        assert {"12.0.0.4", "10.11.0.4"} <= set(_versions(after))
        assert template == pristine  # nothing from an earlier build leaks into a later one


class TestListReleases:
    def test_follows_next_links_so_a_plugin_release_on_page_two_is_listed(self) -> None:
        page_two = f"{API}?per_page=100&page=2"
        pages = {
            f"{API}?per_page=100": (
                json.dumps([{"tag_name": "4.4.2"}]).encode(),
                f'<{page_two}>; rel="next", <{page_two}>; rel="last"',
            ),
            page_two: (json.dumps([{"tag_name": "plugin-v10.11.0.3"}]).encode(), f'<{API}?page=1>; rel="first"'),
        }
        requested: list[str] = []

        def get(url: str) -> tuple[bytes, str | None]:
            requested.append(url)
            return pages[url]

        releases = list_releases("stevezau/media_preview_generator", get)

        assert [r["tag_name"] for r in releases] == ["4.4.2", "plugin-v10.11.0.3"]
        assert requested == [f"{API}?per_page=100", page_two]

    def test_an_api_error_body_raises(self) -> None:
        def get(url: str) -> tuple[bytes, str | None]:
            return json.dumps({"message": "Bad credentials"}).encode(), None

        with pytest.raises(ManifestError, match="Bad credentials"):
            list_releases("stevezau/media_preview_generator", get)

    def test_a_malformed_repo_name_raises(self) -> None:
        with pytest.raises(ManifestError, match="repo"):
            list_releases("stevezau/../../evil", lambda url: (b"[]", None))


class TestMain:
    @pytest.fixture
    def fake_http(self, monkeypatch) -> list[tuple[str, dict[str, str]]]:
        """Serve one dual-ABI release through the builder's HTTP layer, recording each request's headers."""
        calls: list[tuple[str, dict[str, str]]] = []
        release = _release("10.11.0.4")

        def http_get(url: str, headers: dict[str, str]) -> tuple[bytes, str | None]:
            calls.append((url, headers))
            if url.startswith(API):
                return json.dumps([release]).encode(), None
            return FakeDownloads()(url), None

        monkeypatch.setattr(builder, "http_get", http_get)
        return calls

    def _args(self, out: str) -> list[str]:
        return ["--repo", "stevezau/media_preview_generator", "--template", str(TEMPLATE_PATH), "--out", out]

    def test_writes_the_manifest_when_releases_build(self, fake_http, tmp_path, template) -> None:
        out = tmp_path / "manifest.json"

        assert main(self._args(str(out))) == 0

        written = out.read_text(encoding="utf-8")
        assert written == render(build_manifest(template, [_release("10.11.0.4")], FakeDownloads()))
        assert written.endswith("}\n]\n")

    def test_api_requests_carry_the_token_and_downloads_do_not(self, fake_http, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "test-token")

        assert main(self._args(str(tmp_path / "manifest.json"))) == 0

        api = [headers for url, headers in fake_http if url.startswith(API)]
        zips = [headers for url, headers in fake_http if url.startswith(DOWNLOAD)]
        assert len(api) == 1 and len(zips) == 2
        assert api[0]["Authorization"] == "Bearer test-token"
        assert all("Authorization" not in headers for headers in zips)

    def test_writes_nothing_and_fails_when_a_download_fails(self, monkeypatch, tmp_path, capsys) -> None:
        def http_get(url: str, headers: dict[str, str]) -> tuple[bytes, str | None]:
            if url.startswith(API):
                return json.dumps([_release("10.11.0.4")]).encode(), None
            raise urllib.error.HTTPError(url, 502, "Bad Gateway", None, None)

        monkeypatch.setattr(builder, "http_get", http_get)
        out = tmp_path / "manifest.json"

        assert main(self._args(str(out))) == 1

        assert not out.exists()
        assert "::error::" in capsys.readouterr().err
