"""scripts/build_jellyfin_manifest.py: the Jellyfin plugin manifest, rebuilt from plugin-v* releases.

Every Pages deploy runs the builder, so whichever deploy wins the `pages` queue ships the same file.
Real Jellyfin servers poll it, so the builder must fail closed: a download error, a digest mismatch, an
unreadable template or an empty version list stops the deploy instead of publishing a broken or empty
manifest. Everything in it, the plugin's description included, comes from the releases: text edited on
`dev` reaches users only with the next plugin release.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import urllib.error
from pathlib import Path

import pytest
import yaml

from media_preview_generator.servers.jellyfin import JellyfinServer
from scripts import build_jellyfin_manifest as builder
from scripts.build_jellyfin_manifest import ManifestError, build_manifest, list_releases, main, render

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_REPO_PATH = "jellyfin-plugin/manifest.template.json"
WORKING_TREE_TEMPLATE = json.loads((REPO_ROOT / TEMPLATE_REPO_PATH).read_text(encoding="utf-8"))
DOWNLOAD = "https://github.com/stevezau/media_preview_generator/releases/download"
REPO_API = "https://api.github.com/repos/stevezau/media_preview_generator"
API = f"{REPO_API}/releases"
CONTENTS = f"{REPO_API}/contents/{TEMPLATE_REPO_PATH}"
PUBLISHED = "2026-06-29T04:38:25Z"
PLUGIN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "jellyfin-plugin.yml"


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


def _template(description: str) -> list[dict]:
    template = copy.deepcopy(WORKING_TREE_TEMPLATE)
    template[0]["description"] = description
    return template


class FakeDownloads:
    """Serves each release zip's fake bytes and records which URLs were fetched."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.fetched.append(url)
        version = url.rsplit("/", 1)[1].removeprefix("media-preview-bridge_").removesuffix(".zip")
        return _zip_bytes(version)


class FakeTemplates:
    """Serves manifest.template.json as it was at a tag, and records which tags were asked for."""

    def __init__(self, by_tag: dict[str, list[dict]] | None = None) -> None:
        self.by_tag = by_tag or {}
        self.default = copy.deepcopy(WORKING_TREE_TEMPLATE)
        self.requested: list[str] = []

    def __call__(self, tag: str) -> object:
        self.requested.append(tag)
        return self.by_tag.get(tag, self.default)


@pytest.fixture
def downloads() -> FakeDownloads:
    return FakeDownloads()


@pytest.fixture
def templates() -> FakeTemplates:
    return FakeTemplates()


def _versions(manifest: list[dict]) -> list[str]:
    return [entry["version"] for entry in manifest[0]["versions"]]


class TestEntries:
    def test_dual_abi_release_gives_one_entry_per_abi_when_both_zips_are_uploaded(self, downloads, templates) -> None:
        manifest = build_manifest([_release("10.11.0.4")], downloads, templates)

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

    def test_legacy_release_gives_one_entry_when_only_the_10_11_zip_exists(self, downloads, templates) -> None:
        release = _release("10.11.0.3", abis=("10.11",), published_at="2026-05-08T21:19:30Z")

        manifest = build_manifest([release], downloads, templates)

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


class TestMetadataFromTheNewestReleaseTag:
    def test_metadata_is_the_template_at_the_newest_listed_release_tag(self, downloads) -> None:
        older, newer = _template("text released with 0.3"), _template("text released with 0.4")
        templates = FakeTemplates({"plugin-v10.11.0.3": older, "plugin-v10.11.0.4": newer})

        manifest = build_manifest(
            [_release("10.11.0.3"), _release("10.11.0.4"), _release("10.11.0.2")], downloads, templates
        )

        assert templates.requested == ["plugin-v10.11.0.4"]
        assert len(manifest) == 1
        assert {k: v for k, v in manifest[0].items() if k != "versions"} == {
            k: v for k, v in newer[0].items() if k != "versions"
        }
        assert manifest[0]["description"] == "text released with 0.4"
        assert _versions(manifest)[0] == "12.0.0.4"

    def test_a_release_still_uploading_does_not_supply_the_metadata(self, downloads) -> None:
        # Its text would describe a build users can't install yet.
        uploading = _release("10.11.0.5")
        for asset in uploading["assets"]:
            asset["state"] = "starter"
        templates = FakeTemplates({"plugin-v10.11.0.5": _template("unreleased text")})

        manifest = build_manifest([uploading, _release("10.11.0.4")], downloads, templates)

        assert templates.requested == ["plugin-v10.11.0.4"]
        assert manifest[0]["description"] == WORKING_TREE_TEMPLATE[0]["description"]

    def test_the_template_is_not_asked_for_when_there_is_nothing_to_list(self, downloads, templates) -> None:
        with pytest.raises(ManifestError, match="no versions"):
            build_manifest([_release("10.11.0.4", abis=())], downloads, templates)

        assert templates.requested == []


class TestIgnoredReleases:
    def test_app_emby_and_draft_releases_are_ignored(self, downloads, templates) -> None:
        app = {"tag_name": "4.4.2", "draft": False, "published_at": PUBLISHED, "assets": []}
        app_v = {"tag_name": "v4.4.1", "draft": False, "published_at": PUBLISHED, "assets": []}
        emby = {
            "tag_name": "emby-plugin-v1.0.0.0",
            "draft": False,
            "published_at": PUBLISHED,
            "assets": [_asset("emby-plugin-v1.0.0.0", "10.11.0.0")],
        }
        draft = _release("10.11.0.9", draft=True, published_at=None)

        manifest = build_manifest([app, app_v, emby, draft, _release("10.11.0.4")], downloads, templates)

        assert _versions(manifest) == ["12.0.0.4", "10.11.0.4"]
        assert all("/plugin-v10.11.0.4/" in url for url in downloads.fetched)
        assert templates.requested == ["plugin-v10.11.0.4"]

    def test_assets_not_named_for_the_release_are_ignored(self, downloads, templates) -> None:
        release = _release("10.11.0.4")
        release["assets"] += [
            _asset("plugin-v10.11.0.4", "10.11.0.3"),
            {"name": "notes.txt", "state": "uploaded", "browser_download_url": f"{DOWNLOAD}/x/notes.txt"},
        ]

        manifest = build_manifest([release], downloads, templates)

        assert _versions(manifest) == ["12.0.0.4", "10.11.0.4"]
        assert len(downloads.fetched) == 2


class TestMissingZips:
    def test_release_without_its_zip_is_skipped_with_a_warning(self, downloads, templates, capsys) -> None:
        manifest = build_manifest([_release("10.11.0.4", abis=()), _release("10.11.0.3")], downloads, templates)

        assert _versions(manifest) == ["12.0.0.3", "10.11.0.3"]
        err = capsys.readouterr().err
        assert "::warning::" in err
        assert "plugin-v10.11.0.4" in err

    def test_zip_still_uploading_is_skipped_with_a_warning(self, downloads, templates, capsys) -> None:
        release = _release("10.11.0.4")
        release["assets"][1]["state"] = "starter"  # the 12.0 zip

        manifest = build_manifest([release], downloads, templates)

        assert _versions(manifest) == ["10.11.0.4"]
        assert downloads.fetched == [f"{DOWNLOAD}/plugin-v10.11.0.4/media-preview-bridge_10.11.0.4.zip"]
        err = capsys.readouterr().err
        assert "::warning::" in err
        assert "media-preview-bridge_12.0.0.4.zip" in err


class TestFailsClosed:
    def test_download_error_propagates(self, templates) -> None:
        def broken(url: str) -> bytes:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        with pytest.raises(urllib.error.HTTPError):
            build_manifest([_release("10.11.0.4")], broken, templates)

    def test_digest_mismatch_raises(self, downloads, templates) -> None:
        release = _release("10.11.0.4")
        release["assets"][0]["digest"] = "sha256:" + hashlib.sha256(b"something else").hexdigest()

        with pytest.raises(ManifestError, match="media-preview-bridge_10.11.0.4.zip"):
            build_manifest([release], downloads, templates)

    def test_asset_without_a_digest_raises(self, downloads, templates) -> None:
        release = _release("10.11.0.4")
        release["assets"][0]["digest"] = None

        with pytest.raises(ManifestError, match="digest"):
            build_manifest([release], downloads, templates)

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
    def test_zero_versions_raises_so_the_bare_template_is_never_published(self, downloads, templates, releases) -> None:
        with pytest.raises(ManifestError, match="no versions"):
            build_manifest(releases, downloads, templates)

    def test_template_unreadable_at_the_tag_propagates(self, downloads) -> None:
        def missing(tag: str) -> object:
            raise urllib.error.HTTPError(f"{CONTENTS}?ref={tag}", 404, "Not Found", None, None)

        with pytest.raises(urllib.error.HTTPError):
            build_manifest([_release("10.11.0.4")], downloads, missing)

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda t: t[0]["versions"].append({"version": "10.11.0.0"}), id="versions not empty"),
            pytest.param(lambda t: t.append(copy.deepcopy(t[0])), id="two plugins"),
            pytest.param(lambda t: t.clear(), id="no plugin"),
        ],
    )
    def test_malformed_template_at_the_tag_raises(self, downloads, templates, mutate) -> None:
        mutate(templates.default)

        with pytest.raises(ManifestError, match="template at plugin-v10.11.0.4"):
            build_manifest([_release("10.11.0.4")], downloads, templates)


class TestOrdering:
    def test_versions_are_newest_first_with_12_0_before_10_11(self, downloads, templates) -> None:
        releases = [
            _release("10.11.0.2", abis=("10.11",)),
            _release("10.11.0.10"),
            _release("10.11.0.9"),
        ]

        manifest = build_manifest(releases, downloads, templates)

        assert _versions(manifest) == ["12.0.0.10", "12.0.0.9", "10.11.0.10", "10.11.0.9", "10.11.0.2"]
        assert templates.requested == ["plugin-v10.11.0.10"]

    def test_shuffled_input_gives_byte_identical_output(self, downloads, templates) -> None:
        releases = [_release("10.11.0.2", abis=("10.11",)), _release("10.11.0.3"), _release("10.11.0.4")]
        shuffled = copy.deepcopy(releases[::-1])
        for release in shuffled:
            random.Random(7).shuffle(release["assets"])

        rendered = render(build_manifest(releases, downloads, templates))

        assert render(build_manifest(shuffled, downloads, templates)) == rendered
        assert _versions(json.loads(rendered)) == ["12.0.0.4", "12.0.0.3", "10.11.0.4", "10.11.0.3", "10.11.0.2"]


class TestRaceRegression:
    def test_a_later_deploy_lists_a_release_whose_own_deploy_was_cancelled(self, downloads, templates) -> None:
        # The docs deploy that replaced a cancelled plugin deploy only has the release list to go on.
        pristine = copy.deepcopy(templates.default)
        before = build_manifest([_release("10.11.0.2"), _release("10.11.0.3")], downloads, templates)

        after = build_manifest(
            [_release("10.11.0.2"), _release("10.11.0.3"), _release("10.11.0.4")], downloads, templates
        )

        assert "10.11.0.4" not in _versions(before)
        assert _versions(after)[:1] == ["12.0.0.4"]
        assert {"12.0.0.4", "10.11.0.4"} <= set(_versions(after))
        assert templates.default == pristine  # nothing from an earlier build leaks into a later one


class TestRequiredTag:
    """The plugin release's own deploy passes its tag: that release must be listed with every ABI's zip."""

    def test_a_fully_listed_required_release_changes_nothing_in_the_output(self, downloads, templates) -> None:
        releases = [_release("10.11.0.3"), _release("10.11.0.4")]

        required = build_manifest(releases, downloads, templates, require_tag="plugin-v10.11.0.4")

        assert render(required) == render(build_manifest(releases, downloads, templates))
        assert {"12.0.0.4", "10.11.0.4"} <= set(_versions(required))

    def test_an_empty_required_tag_requires_nothing(self, downloads, templates) -> None:
        manifest = build_manifest([_release("10.11.0.3")], downloads, templates, require_tag="")

        assert _versions(manifest) == ["12.0.0.3", "10.11.0.3"]

    @pytest.mark.parametrize(
        "releases, message",
        [
            pytest.param([_release("10.11.0.3")], "plugin-v10.11.0.4 is not among the releases", id="release missing"),
            pytest.param(
                [_release("10.11.0.3"), _release("10.11.0.4", draft=True)], "plugin-v10.11.0.4 is a draft", id="draft"
            ),
            pytest.param(
                [_release("10.11.0.3"), _release("10.11.0.4", abis=("10.11",))],
                "no uploaded media-preview-bridge_12.0.0.4.zip",
                id="12.0 zip missing",
            ),
            pytest.param(
                [_release("10.11.0.4", abis=())],
                "no uploaded media-preview-bridge_10.11.0.4.zip",
                id="no zips at all",
            ),
        ],
    )
    def test_the_required_release_not_fully_listed_raises(self, downloads, templates, releases, message) -> None:
        with pytest.raises(ManifestError, match=re.escape(message)):
            build_manifest(releases, downloads, templates, require_tag="plugin-v10.11.0.4")

    def test_a_required_zip_still_uploading_raises(self, downloads, templates) -> None:
        release = _release("10.11.0.4")
        release["assets"][1]["state"] = "starter"  # the 12.0 zip

        with pytest.raises(ManifestError, match=re.escape("no uploaded media-preview-bridge_12.0.0.4.zip")):
            build_manifest([release], downloads, templates, require_tag="plugin-v10.11.0.4")

    def test_a_misnamed_required_zip_raises_and_names_what_the_release_has(self, downloads, templates) -> None:
        release = _release("10.11.0.4", abis=("10.11",))
        release["assets"].append(_asset("plugin-v10.11.0.4", "12.0.0.4", name="media-preview-bridge-12.0.0.4.zip"))

        with pytest.raises(ManifestError, match=re.escape("media-preview-bridge-12.0.0.4.zip")):
            build_manifest([release], downloads, templates, require_tag="plugin-v10.11.0.4")

    @pytest.mark.parametrize("tag", ["plugin-v10.12.0.0", "plugin-v12.0.0.4", "emby-plugin-v1.0.0.0", "10.11.0.4"])
    def test_a_required_tag_the_builder_would_skip_raises(self, downloads, templates, tag) -> None:
        # Tags that don't look like plugin-v10.11.X.Y are skipped silently, which is only safe when nobody needs them.
        release = _release("10.11.0.4", tag_name=tag)

        with pytest.raises(ManifestError, match=re.escape(tag)):
            build_manifest([release, _release("10.11.0.3")], downloads, templates, require_tag=tag)
        assert downloads.fetched == []


class TestPluginReleaseContract:
    def test_the_builder_lists_what_jellyfin_plugin_yml_releases(self, downloads, templates) -> None:
        # The tag, zip names and ABIs are a contract between the release workflow and the builder. Each pinned line
        # below is one side of it: change it and this names the builder rule that must follow.
        job = yaml.safe_load(PLUGIN_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["build-release"]
        steps = {step["name"]: step for step in job["steps"]}
        workflow_abis = {row["abi"] for row in job["strategy"]["matrix"]["include"]}
        resolve = steps["Resolve version"]["run"]
        release_step = steps["Create / update GitHub release"]["with"]

        assert workflow_abis == set(builder.ABIS)
        assert r'"$VERSION" =~ ^10\.11\.[0-9]+\.[0-9]+$' in resolve
        assert 'if [[ "$ABI" == "10.11" ]]; then' in resolve
        assert 'ROW_VERSION="$VERSION"' in resolve
        assert 'ROW_VERSION="12.0.$(echo "$VERSION" | cut -d. -f3-4)"' in resolve
        assert 'ZIP="media-preview-bridge_${ROW_VERSION}.zip"' in steps["Pack zip"]["run"]
        assert release_step["tag_name"] == "plugin-v${{ steps.ver.outputs.release_version }}"
        assert release_step["files"] == "jellyfin-plugin/${{ steps.pack.outputs.zip }}"

        version = "10.11.7.3"
        tag = f"plugin-v{version}"
        row_versions = {abi: version if abi == "10.11" else f"12.0.{version.split('.', 2)[2]}" for abi in workflow_abis}
        release = {
            "tag_name": tag,
            "draft": False,
            "published_at": PUBLISHED,
            "assets": [_asset(tag, row_versions[abi]) for abi in sorted(workflow_abis)],
        }

        manifest = build_manifest([release], downloads, templates, require_tag=tag)

        assert {(entry["version"], entry["targetAbi"]) for entry in manifest[0]["versions"]} == {
            (row_versions[abi], builder.ABIS[abi]) for abi in workflow_abis
        }


GOOD_ENTRY = {
    "version": "10.11.0.4",
    "changelog": "Automated release for plugin-v10.11.0.4.",
    "targetAbi": "10.11.0.0",
    "sourceUrl": f"{DOWNLOAD}/plugin-v10.11.0.4/media-preview-bridge_10.11.0.4.zip",
    "checksum": "9888E4AB6DEB86D431115F4CD5FC4F68",
    "timestamp": PUBLISHED,
}


class TestVersionGuards:
    def test_a_good_entry_passes(self) -> None:
        builder._check_versions([dict(GOOD_ENTRY)])

    @pytest.mark.parametrize(
        "versions, message",
        [
            pytest.param([GOOD_ENTRY, dict(GOOD_ENTRY)], "listed twice", id="duplicate version"),
            pytest.param([{**GOOD_ENTRY, "checksum": "1FFF" * 16}], "not an MD5", id="SHA-256, which Jellyfin rejects"),
            pytest.param(
                [{**GOOD_ENTRY, "checksum": "9888e4ab6deb86d431115f4cd5fc4f68"}], "not an MD5", id="lowercase"
            ),
            pytest.param([{**GOOD_ENTRY, "timestamp": None}], "missing ['timestamp']", id="no timestamp"),
            pytest.param([{**GOOD_ENTRY, "sourceUrl": ""}], "missing ['sourceUrl']", id="empty sourceUrl"),
            pytest.param(
                [{k: v for k, v in GOOD_ENTRY.items() if k != "targetAbi"}], "missing ['targetAbi']", id="no targetAbi"
            ),
        ],
    )
    def test_a_bad_version_list_raises(self, versions, message) -> None:
        with pytest.raises(ManifestError, match=re.escape(message)):
            builder._check_versions(versions)

    def test_the_same_release_listed_twice_raises(self, downloads, templates) -> None:
        # A release published between two page fetches shifts the listing, so one can land on both pages.
        with pytest.raises(ManifestError, match="listed twice"):
            build_manifest([_release("10.11.0.4"), _release("10.11.0.4")], downloads, templates)

    def test_a_release_without_published_at_raises(self, downloads, templates) -> None:
        with pytest.raises(ManifestError, match=re.escape("missing ['timestamp']")):
            build_manifest([_release("10.11.0.4", published_at=None)], downloads, templates)


class TestTemplateIdentity:
    @pytest.mark.parametrize(
        "mutate, field",
        [
            pytest.param(lambda plugin: plugin.pop("guid"), "guid", id="no guid"),
            pytest.param(lambda plugin: plugin.update(guid=""), "guid", id="empty guid"),
            pytest.param(lambda plugin: plugin.pop("name"), "name", id="no name"),
            pytest.param(lambda plugin: plugin.update(name=None), "name", id="null name"),
        ],
    )
    def test_a_template_without_guid_or_name_raises(self, downloads, templates, mutate, field) -> None:
        mutate(templates.default[0])

        with pytest.raises(ManifestError, match=f"template at plugin-v10.11.0.4.*{field}"):
            build_manifest([_release("10.11.0.4")], downloads, templates)

    def test_template_guid_matches_the_plugin_and_the_app(self) -> None:
        # Jellyfin matches catalog entries to installed plugins by this GUID, and the app finds and removes the
        # plugin by it: a drift in any one of the three orphans installs.
        plugin_cs = (REPO_ROOT / "jellyfin-plugin" / "Plugin.cs").read_text(encoding="utf-8")
        plugin_guid = re.search(r'Guid Id => Guid\.Parse\("([0-9a-fA-F-]{36})"\)', plugin_cs).group(1)

        assert WORKING_TREE_TEMPLATE[0]["guid"] == plugin_guid == JellyfinServer.PLUGIN_GUID


class FakeResponse:
    def __init__(self, body: bytes = b"[]", link: str | None = None) -> None:
        self.body = body
        self.headers = {"Link": link} if link else {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


class TestHttpGet:
    URL = f"{API}?per_page=100"

    @pytest.fixture
    def network(self, monkeypatch):
        """Stands in for urllib.request.urlopen: plays back one scripted outcome per call, recording the requests."""

        class Network:
            def __init__(self) -> None:
                self.outcomes: list[object] = []
                self.requests: list[tuple[object, float]] = []
                self.sleeps: list[float] = []

            def urlopen(self, request, timeout):
                self.requests.append((request, timeout))
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        network = Network()
        monkeypatch.setattr(builder.urllib.request, "urlopen", network.urlopen)
        monkeypatch.setattr(builder.time, "sleep", network.sleeps.append)
        return network

    def _http_error(self, code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(self.URL, code, "error", {}, None)

    def test_returns_the_body_and_link_header_and_sends_the_headers(self, network) -> None:
        network.outcomes = [FakeResponse(b'[{"tag_name": "x"}]', '<next>; rel="next"')]

        body, link = builder.http_get(self.URL, {"Authorization": "Bearer t"})

        assert (body, link) == (b'[{"tag_name": "x"}]', '<next>; rel="next"')
        request, timeout = network.requests[0]
        assert request.full_url == self.URL
        assert request.get_header("Authorization") == "Bearer t"
        assert request.get_header("User-agent") == builder.USER_AGENT
        assert timeout == 60

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(500, id="500"),
            pytest.param(502, id="502"),
            pytest.param(503, id="503"),
            pytest.param(429, id="429 rate limited"),
            pytest.param("network", id="network error"),
        ],
    )
    def test_server_errors_rate_limits_and_network_errors_are_retried(self, network, failure) -> None:
        error = urllib.error.URLError("connection reset") if failure == "network" else self._http_error(failure)
        network.outcomes = [error, FakeResponse(b"ok")]

        assert builder.http_get(self.URL, {}) == (b"ok", None)
        assert len(network.requests) == 2
        assert network.sleeps == [2]

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_other_client_errors_raise_at_once(self, network, code) -> None:
        network.outcomes = [self._http_error(code), FakeResponse(b"never reached")]

        with pytest.raises(urllib.error.HTTPError) as raised:
            builder.http_get(self.URL, {})

        assert raised.value.code == code
        assert len(network.requests) == 1
        assert network.sleeps == []

    @pytest.mark.parametrize("failure", [502, "network"])
    def test_the_last_attempt_reraises(self, network, failure) -> None:
        def error() -> urllib.error.URLError:
            return urllib.error.URLError("timed out") if failure == "network" else self._http_error(failure)

        network.outcomes = [error() for _ in range(builder.HTTP_ATTEMPTS)] + [FakeResponse(b"never reached")]

        with pytest.raises(urllib.error.URLError):
            builder.http_get(self.URL, {})

        assert len(network.requests) == builder.HTTP_ATTEMPTS == 3
        assert network.sleeps == [2, 4]

    @pytest.mark.parametrize("url", ["http://github.com/x.zip", "file:///etc/passwd", "ftp://example.com/x"])
    def test_refuses_anything_but_https(self, network, url) -> None:
        with pytest.raises(ManifestError, match="non-HTTPS"):
            builder.http_get(url, {})

        assert network.requests == []


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
    TAG_TEMPLATE_URL = f"{CONTENTS}?ref=plugin-v10.11.0.4"

    @pytest.fixture
    def tag_template(self) -> list[dict]:
        return _template("text as released with plugin-v10.11.0.4")

    @pytest.fixture
    def fake_http(self, monkeypatch, tag_template) -> list[tuple[str, dict[str, str]]]:
        """Serve one dual-ABI release and its tag's template through the builder's HTTP layer, recording headers."""
        calls: list[tuple[str, dict[str, str]]] = []
        release = _release("10.11.0.4")

        def http_get(url: str, headers: dict[str, str]) -> tuple[bytes, str | None]:
            calls.append((url, headers))
            if url.startswith(API):
                return json.dumps([release]).encode(), None
            if url == self.TAG_TEMPLATE_URL:
                return json.dumps(tag_template).encode(), None
            if url.startswith(DOWNLOAD):
                return FakeDownloads()(url), None
            raise AssertionError(f"unexpected request {url}")

        monkeypatch.setattr(builder, "http_get", http_get)
        return calls

    def _args(self, out: str) -> list[str]:
        return ["--repo", "stevezau/media_preview_generator", "--template", TEMPLATE_REPO_PATH, "--out", out]

    def test_writes_the_manifest_when_releases_build(self, fake_http, tmp_path, tag_template) -> None:
        out = tmp_path / "manifest.json"

        assert main(self._args(str(out))) == 0

        written = out.read_text(encoding="utf-8")
        expected = build_manifest(
            [_release("10.11.0.4")], FakeDownloads(), FakeTemplates({"plugin-v10.11.0.4": tag_template})
        )
        assert written == render(expected)
        assert written.endswith("}\n]\n")

    def test_metadata_comes_from_the_newest_tag_not_the_working_tree(self, fake_http, tmp_path, monkeypatch) -> None:
        # Run from somewhere with no template on disk: the file must come from the tag, never the checkout.
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "manifest.json"

        assert main(self._args(str(out))) == 0

        description = json.loads(out.read_text(encoding="utf-8"))[0]["description"]
        assert description == "text as released with plugin-v10.11.0.4"
        assert description != WORKING_TREE_TEMPLATE[0]["description"]
        assert [url for url, _ in fake_http if url.startswith(CONTENTS)] == [self.TAG_TEMPLATE_URL]

    def test_api_requests_carry_the_token_and_downloads_do_not(self, fake_http, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "test-token")

        assert main(self._args(str(tmp_path / "manifest.json"))) == 0

        api = {url: headers for url, headers in fake_http if url.startswith(REPO_API)}
        zips = [headers for url, headers in fake_http if url.startswith(DOWNLOAD)]
        assert len(api) == 2 and len(zips) == 2
        assert all(headers["Authorization"] == "Bearer test-token" for headers in api.values())
        assert api[self.TAG_TEMPLATE_URL]["Accept"] == "application/vnd.github.raw+json"
        assert all("Authorization" not in headers for headers in zips)

    def test_a_required_tag_that_is_not_listed_fails_without_writing(self, fake_http, tmp_path, capsys) -> None:
        out = tmp_path / "manifest.json"

        assert main([*self._args(str(out)), "--require-tag", "plugin-v10.11.0.5"]) == 1

        assert not out.exists()
        assert "plugin-v10.11.0.5 is not among the releases" in capsys.readouterr().err

    def test_a_listed_required_tag_writes_the_same_manifest(self, fake_http, tmp_path) -> None:
        plain, required = tmp_path / "plain.json", tmp_path / "required.json"

        assert main(self._args(str(plain))) == 0
        assert main([*self._args(str(required)), "--require-tag", "plugin-v10.11.0.4"]) == 0

        assert required.read_bytes() == plain.read_bytes()

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

    def test_writes_nothing_and_fails_when_the_template_is_missing_at_the_tag(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        def http_get(url: str, headers: dict[str, str]) -> tuple[bytes, str | None]:
            if url.startswith(API):
                return json.dumps([_release("10.11.0.4")]).encode(), None
            if url.startswith(CONTENTS):
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            return FakeDownloads()(url), None

        monkeypatch.setattr(builder, "http_get", http_get)
        out = tmp_path / "manifest.json"

        assert main(self._args(str(out))) == 1

        assert not out.exists()
        assert "::error::" in capsys.readouterr().err
