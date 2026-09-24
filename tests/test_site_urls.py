"""Every public URL points at mediapreviewgenerator.dev, apart from the few that must name the old host.

The github.io project URLs 301 to the custom domain, so a stale one still works, but through a
redirect that costs a round trip, splits search signals, and would break outright if the custom
domain were ever removed. Each allowed exception says why it needs the old host.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from media_preview_generator.servers.jellyfin import JellyfinServer

REPO_ROOT = Path(__file__).resolve().parent.parent
OLD_HOST = "stevezau.github.io/media_preview_generator"
NEW_MANIFEST = "https://mediapreviewgenerator.dev/jellyfin-plugin/manifest.json"
ALLOWED = {
    "media_preview_generator/servers/jellyfin.py": "LEGACY_PLUGIN_REPO_URLS recognises installs made before the move",
    "tests/test_servers_jellyfin.py": "tests that recognition",
    "tests/test_site_urls.py": "this file",
}


def test_no_old_host_outside_the_allowlist() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\n")
    offenders = []
    for relative in filter(None, tracked):
        if relative.startswith("docs/design/") or relative in ALLOWED:
            continue  # design docs are dated records and keep the URLs they were written with
        try:
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        if OLD_HOST in text:
            offenders.append(relative)
    assert offenders == []


def test_site_and_manifest_urls_use_the_custom_domain() -> None:
    config = yaml.safe_load((REPO_ROOT / "docs" / "_config.yml").read_text(encoding="utf-8"))
    assert config["url"] == "https://mediapreviewgenerator.dev"
    docs_yml = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8"))
    assert docs_yml["env"]["LIVE_MANIFEST_URL"] == NEW_MANIFEST
    release = (REPO_ROOT / ".github" / "workflows" / "jellyfin-plugin.yml").read_text(encoding="utf-8")
    assert f'PREV_URL="{NEW_MANIFEST}"' in release
    # The release notes' links too, and the URL the app registers on real Jellyfin servers.
    manifests = re.findall(r"https://[^\s\"'`)]+/jellyfin-plugin/manifest\.json", release)
    assert len(manifests) >= 3 and set(manifests) == {NEW_MANIFEST}
    assert JellyfinServer.PLUGIN_REPO_URL == NEW_MANIFEST
