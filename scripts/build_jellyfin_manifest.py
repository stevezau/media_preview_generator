#!/usr/bin/env python3
"""Build the Jellyfin plugin repository manifest from the plugin-v* GitHub releases.

Every Jellyfin install of Media Preview Bridge polls https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json.
Every Pages deploy (docs.yml, whether a docs push or a plugin release calls it) runs this, so all deploys ship the
same manifest. A deploy GitHub cancels while it waits in the `pages` queue loses nothing: whichever deploy runs next
lists the releases itself. Nothing reads the live copy back.

Each non-draft plugin-v10.11.X.Y release gives one entry per uploaded zip: media-preview-bridge_10.11.X.Y.zip
(targetAbi 10.11.0.0) and, from the dual-ABI releases on, media-preview-bridge_12.0.X.Y.zip (targetAbi 12.0.0.0).
Plugin metadata (name, guid, description...) comes from jellyfin-plugin/manifest.template.json as it was at the
newest listed release's tag, read through the GitHub contents API (docs.yml's checkout is shallow and has no tags).
So the manifest derives entirely from releases: text edited on dev reaches users only with the next plugin release,
and never describes a feature no installable build has.

Fails closed, exiting non-zero without writing anything, on an API or download error, a zip whose bytes don't
match GitHub's sha256 digest (or that has none), a template unreadable or malformed at that tag, or zero
versions. A zip that isn't fully uploaded yet is left
out with a warning; the plugin release's own deploy runs after both uploads and lists it.

The plugin release's own deploy passes --require-tag with its tag. The build then also fails unless that release is
listed with one zip per ABI, so a renamed zip or tag can't let a release go green with its versions left out. The
manifest is the same with or without it.

Usage:
    GH_TOKEN=... python scripts/build_jellyfin_manifest.py --repo OWNER/NAME \\
        --template jellyfin-plugin/manifest.template.json --out site/jellyfin-plugin/manifest.json
    --template is the file's path in the repository; it is read at the release tag, never from disk.
    Use --out - to print the manifest instead (dry run).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

API_ROOT = "https://api.github.com"
PLUGIN_TAG = re.compile(r"plugin-v(10\.11\.(\d+\.\d+))")
REPO_NAME = re.compile(r"[\w.-]+/[\w.-]+")
REPO_PATH = re.compile(r"[\w.-]+(/[\w.-]+)*")
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')
MD5_HEX = re.compile(r"[0-9A-F]{32}")
# Asset-name ABI prefix -> the manifest's targetAbi. Jellyfin only offers a version whose targetAbi is <= the
# server's version, so 10.11 servers get the 10.11 build and 12.0 servers the 12.0 one.
ABIS = {"10.11": "10.11.0.0", "12.0": "12.0.0.0"}
ENTRY_FIELDS = ("version", "changelog", "targetAbi", "sourceUrl", "checksum", "timestamp")
HTTP_ATTEMPTS = 3
USER_AGENT = "media-preview-generator-jellyfin-manifest"


class ManifestError(Exception):
    """The manifest can't be built safely, so nothing must be deployed."""


def _warn(message: str) -> None:
    # stderr keeps `--out -` output clean; the Actions runner reads workflow commands from both streams.
    print(f"::warning::{message}", file=sys.stderr)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _check_template(template: object, tag: str) -> dict:
    if not (isinstance(template, list) and len(template) == 1 and isinstance(template[0], dict)):
        raise ManifestError(f"template at {tag} must be a JSON list holding exactly one plugin")
    plugin = template[0]
    if plugin.get("versions") != []:
        raise ManifestError(f'template at {tag}: the plugin must have "versions": [] (versions come from the releases)')
    missing = [field for field in ("guid", "name") if not isinstance(plugin.get(field), str) or not plugin[field]]
    if missing:
        raise ManifestError(f"template at {tag}: the plugin has no {' or '.join(missing)}")
    return plugin


def _verified_bytes(asset: dict, fetch_bytes: Callable[[str], bytes]) -> bytes:
    """Download an asset and check it against the sha256 digest GitHub recorded at upload.

    Args:
        asset: One entry of a release's ``assets`` list from the GitHub API.
        fetch_bytes: Downloads a URL; errors propagate.

    Returns:
        The asset's bytes.

    Raises:
        ManifestError: The asset has no sha256 digest, or the downloaded bytes don't match it.
    """
    name = asset["name"]
    digest = asset.get("digest") or ""
    if not digest.startswith("sha256:"):
        raise ManifestError(f"{name} has no sha256 digest to verify the download against (got {digest!r})")
    data = fetch_bytes(asset["browser_download_url"])
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest.removeprefix("sha256:").lower():
        raise ManifestError(f"{name} downloaded with sha256 {actual}, but GitHub recorded {digest}")
    return data


def _release_entries(release: dict, fetch_bytes: Callable[[str], bytes], required: bool) -> list[dict]:
    """Manifest entries for one plugin release: one per uploaded zip, skipping zips still uploading.

    Args:
        release: One plugin-v10.11.X.Y release from the GitHub API.
        fetch_bytes: Downloads a URL; errors propagate.
        required: The release is --require-tag's: every ABI's zip must be uploaded, or this raises.

    Returns:
        The release's manifest entries.

    Raises:
        ManifestError: ``required`` and an ABI's zip is missing, misnamed or still uploading; or a digest check fails.
    """
    tag = release["tag_name"]
    tag_version, build = PLUGIN_TAG.fullmatch(tag).groups()
    assets = {asset.get("name"): asset for asset in release.get("assets") or []}
    entries = []
    for abi, target_abi in ABIS.items():
        version = f"{abi}.{build}"
        name = f"media-preview-bridge_{version}.zip"
        asset = assets.get(name)
        if required and (asset is None or asset.get("state") != "uploaded"):
            present = sorted(f"{other} ({assets[other].get('state')})" for other in map(str, assets))
            raise ManifestError(f"required release {tag} has no uploaded {name}; its assets: {present or 'none'}")
        if asset is None:
            continue  # releases before 12.0 support carry only the 10.11 zip
        if asset.get("state") != "uploaded":
            _warn(f"{tag}: {asset['name']} is {asset.get('state')!r}, not uploaded yet; left out of this deploy.")
            continue
        data = _verified_bytes(asset, fetch_bytes)
        entries.append(
            {
                "version": version,
                "changelog": f"Automated release for plugin-v{tag_version}.",
                "targetAbi": target_abi,
                "sourceUrl": asset["browser_download_url"],
                # Jellyfin's InstallationManager verifies the download with MD5
                # (MediaBrowser.Controller/Plugins/InstallationManager.cs uses
                # MD5.HashData()) — publishing SHA-256 here fails every install
                # with "The checksum of the received data doesn't match".
                # The Jellyfin official plugin repo also uses MD5 (32 hex chars).
                "checksum": hashlib.md5(data, usedforsecurity=False).hexdigest().upper(),
                "timestamp": release.get("published_at"),
            }
        )
    if not entries:
        _warn(f"{tag} has no uploaded media-preview-bridge zip; left out of the manifest.")
    return entries


def _check_versions(versions: list[dict]) -> None:
    if not versions:
        raise ManifestError("no versions: no plugin-v* release has an uploaded zip; refusing to publish the template")
    seen: set[str] = set()
    for entry in versions:
        missing = [field for field in ENTRY_FIELDS if not isinstance(entry.get(field), str) or not entry[field]]
        if missing:
            raise ManifestError(f"version {entry.get('version')!r} is missing {missing}")
        if not MD5_HEX.fullmatch(entry["checksum"]):
            raise ManifestError(f"version {entry['version']} checksum {entry['checksum']!r} is not an MD5")
        if entry["version"] in seen:
            raise ManifestError(f"version {entry['version']} is listed twice")
        seen.add(entry["version"])


def _check_required_release(releases: list[dict], tag: str) -> None:
    """Fail before any download unless ``tag`` names a published release the builder would list."""
    if not PLUGIN_TAG.fullmatch(tag):
        raise ManifestError(f"required tag {tag!r} isn't plugin-v10.11.X.Y, so the builder would skip its release")
    matching = [release for release in releases if release.get("tag_name") == tag]
    if not matching:
        raise ManifestError(f"required release {tag} is not among the releases")
    if any(release.get("draft") for release in matching):
        raise ManifestError(f"required release {tag} is a draft")


def build_manifest(
    releases: list[dict],
    fetch_bytes: Callable[[str], bytes],
    template_at: Callable[[str], object],
    require_tag: str = "",
) -> list[dict]:
    """Build the manifest from the repository's releases.

    Args:
        releases: Every release of the repository, as the GitHub releases API lists them.
        fetch_bytes: Downloads a URL and returns its bytes; errors propagate.
        template_at: Returns the parsed manifest.template.json as it was at a tag; errors propagate.
        require_tag: A release that must be listed with every ABI's zip (the plugin release's own deploy).
            Empty requires nothing. It never changes the output, only whether the build succeeds.

    Returns:
        The manifest: the newest listed release's template plugin with ``versions`` filled in, newest first.

    Raises:
        ManifestError: A zip that fails its digest check, zero versions, a malformed template at the tag, or a
            required release that isn't fully listed.
    """
    if require_tag:
        _check_required_release(releases, require_tag)
    versions = []
    listed_tags = []
    for release in releases:
        if release.get("draft") or not PLUGIN_TAG.fullmatch(release.get("tag_name") or ""):
            continue
        entries = _release_entries(release, fetch_bytes, required=release["tag_name"] == require_tag)
        if entries:
            listed_tags.append(release["tag_name"])
        versions.extend(entries)
    versions.sort(key=lambda entry: _version_key(entry["version"]), reverse=True)
    _check_versions(versions)
    # The newest release users can install, not one still uploading: its text must match a build they can get.
    newest_tag = max(listed_tags, key=lambda tag: _version_key(PLUGIN_TAG.fullmatch(tag).group(1)))
    plugin = copy.deepcopy(_check_template(template_at(newest_tag), newest_tag))
    plugin["versions"] = versions
    return [plugin]


def list_releases(repo: str, get: Callable[[str], tuple[bytes, str | None]]) -> list[dict]:
    """List every release of ``repo``, following the API's pagination.

    Args:
        repo: ``owner/name``.
        get: Fetches a URL and returns ``(body, Link header)``; errors propagate.

    Returns:
        All releases, in the order the API returned them.

    Raises:
        ManifestError: A malformed repo name, or a page that isn't a JSON list.
    """
    if not REPO_NAME.fullmatch(repo) or ".." in repo:
        raise ManifestError(f"--repo must look like owner/name (got {repo!r})")
    releases: list[dict] = []
    url: str | None = f"{API_ROOT}/repos/{repo}/releases?per_page=100"
    while url:
        body, link = get(url)
        page = json.loads(body)
        if not isinstance(page, list):
            raise ManifestError(f"releases API returned {str(page)[:200]} instead of a list")
        releases.extend(page)
        match = NEXT_LINK.search(link or "")
        url = match.group(1) if match else None
    return releases


def template_url(repo: str, path: str, tag: str) -> str:
    """The contents-API URL of ``path`` in ``repo`` as it was at ``tag``."""
    if not REPO_PATH.fullmatch(path) or ".." in path:
        raise ManifestError(f"--template must be a path inside the repository (got {path!r})")
    return f"{API_ROOT}/repos/{repo}/contents/{path}?ref={urllib.parse.quote(tag, safe='')}"


def render(manifest: list[dict]) -> str:
    """Serialise the manifest the way it is published."""
    return json.dumps(manifest, indent=4) + "\n"


def http_get(url: str, headers: dict[str, str]) -> tuple[bytes, str | None]:
    """GET an HTTPS URL, retrying server errors and network failures.

    Args:
        url: The URL to fetch.
        headers: Extra request headers.

    Returns:
        The response body and its ``Link`` header, if any.

    Raises:
        ManifestError: The URL isn't HTTPS.
        urllib.error.URLError: The request still fails after the last attempt, or fails with a 4xx.
    """
    if not url.startswith("https://"):
        raise ManifestError(f"refusing to fetch a non-HTTPS URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            # HTTPS only, checked above.
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
                return response.read(), response.headers.get("Link")
        except urllib.error.HTTPError as exc:
            if attempt == HTTP_ATTEMPTS or (exc.code < 500 and exc.code != 429):
                raise
        except urllib.error.URLError:
            if attempt == HTTP_ATTEMPTS:
                raise
        time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: build the manifest and write it to --out, or fail without writing anything."""
    parser = argparse.ArgumentParser(description="Build the Jellyfin plugin manifest from plugin-v* releases.")
    parser.add_argument("--repo", required=True, help="owner/name of the GitHub repository")
    parser.add_argument(
        "--template", required=True, help="repository path of manifest.template.json, read at the newest release tag"
    )
    parser.add_argument("--out", required=True, help="where to write the manifest; - for stdout")
    parser.add_argument(
        "--require-tag",
        default="",
        help="fail unless this plugin-v* release is listed with every ABI's zip; empty requires nothing",
    )
    args = parser.parse_args(argv)

    api_headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        api_headers["Authorization"] = f"Bearer {token}"
    raw_headers = {**api_headers, "Accept": "application/vnd.github.raw+json"}
    try:
        releases = list_releases(args.repo, lambda url: http_get(url, api_headers))
        manifest = build_manifest(
            releases,
            # The zips are public. No token here: the download redirects to another host, and urllib would
            # forward the Authorization header there.
            lambda url: http_get(url, {})[0],
            lambda tag: json.loads(http_get(template_url(args.repo, args.template, tag), raw_headers)[0]),
            require_tag=args.require_tag,
        )
    except (ManifestError, OSError, ValueError, http.client.HTTPException) as exc:
        print(f"::error::Jellyfin manifest not built, refusing to deploy: {exc}", file=sys.stderr)
        return 1

    content = render(manifest)
    if args.out == "-":
        sys.stdout.write(content)
    else:
        Path(args.out).write_text(content, encoding="utf-8")
    versions = manifest[0]["versions"]
    print(f"Jellyfin manifest: {len(versions)} version(s), newest {versions[0]['version']}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
