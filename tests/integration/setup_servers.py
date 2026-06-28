"""API-driven configuration of the integration test stack.

Runs after ``docker compose up -d`` to authenticate against each server,
configure a media library pointing at the synthetic test fixtures, and
write a ``servers.env`` file alongside this script with the resulting
credentials and identities.

Two server types are fully automated:

* **Emby** (``emby/embyserver:latest``) ships with an unconfigured default
  admin "MyEmbyUser" with no password. We authenticate as that user,
  capture the ``AccessToken`` and ``ServerId``, then create a movies
  library pointing at ``/em-media``.
* **Jellyfin** (``lscr.io/linuxserver/jellyfin:10.11``) — the first-run
  ``/Startup/User`` wizard is broken on a fresh install across 10.9-10.11
  (``Sequence contains no elements``), so we inject an admin API key straight
  into the ``ApiKeys`` table (verified to satisfy the plugin's
  ``RequiresElevation`` endpoints with no completed wizard), install the Media
  Preview Bridge plugin into the bind-mounted config, and create a Movies
  library at ``/jf-media``. ``--jellyfin-token``/``--jellyfin-user-id`` still
  accept a manually-captured token to skip the injection.

Plex needs a one-time ``PLEX_CLAIM`` token from <https://plex.tv/claim>;
its setup is not currently automated by this script.

The output ``servers.env`` is consumed by the integration tests (which
import it via :mod:`os.environ` to address the live containers).
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SERVERS_ENV = HERE / "servers.env"

EMBY_URL = "http://127.0.0.1:8096"
JELLYFIN_URL = "http://127.0.0.1:8097"
PLEX_URL = "http://127.0.0.1:32401"

_AUTH_HEADER = (
    'MediaBrowser Client="PlexGeneratePreviewsIntegration", '
    'Device="PlexGeneratePreviewsIntegration", '
    f'DeviceId="{uuid.uuid3(uuid.NAMESPACE_DNS, "PlexGeneratePreviewsIntegration").hex}", '
    'Version="1.0"'
)


@dataclass
class ServerCredentials:
    """Captured auth + identity for one configured test server."""

    name: str
    server_id: str
    access_token: str
    user_id: str
    base_url: str
    library_remote_path: str
    # Host path of the server's program-data root (where the app would mount
    # the config dir for off-media trickplay). Empty for vendors that don't
    # need it. For Jellyfin (linuxserver image) this is <bind>/data.
    config_dir: str = ""


def _wait_for_http(url: str, *, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=3)
            if 200 <= response.status_code < 300:
                return
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(2)
    raise TimeoutError(f"server at {url} not ready after {timeout}s; last error: {last_exc}")


def _authed_headers(token: str) -> dict:
    return {
        "Authorization": _AUTH_HEADER,
        "X-Emby-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def setup_emby(*, base_url: str = EMBY_URL) -> ServerCredentials:
    """Authenticate as the default Emby admin and configure a movies library.

    Emby's docker image auto-creates an admin "MyEmbyUser" with no
    password on first start. We authenticate via
    ``/Users/AuthenticateByName`` to capture the ``AccessToken`` and
    ``ServerId``, then ensure a "Movies" virtual folder exists pointing
    at ``/em-media``.
    """
    _wait_for_http(f"{base_url}/System/Info/Public")

    # 1. Authenticate as the seeded default user.
    auth_response = requests.post(
        f"{base_url}/Users/AuthenticateByName",
        json={"Username": "MyEmbyUser", "Pw": ""},
        headers={"Authorization": _AUTH_HEADER, "Content-Type": "application/json"},
        timeout=30,
    )
    auth_response.raise_for_status()
    auth_data = auth_response.json()

    access_token = str(auth_data.get("AccessToken") or "")
    user_id = str((auth_data.get("User") or {}).get("Id") or "")
    server_id = str(auth_data.get("ServerId") or "")
    if not access_token or not server_id:
        raise RuntimeError(f"Emby auth response missing AccessToken/ServerId: {auth_data}")

    # 2. Configure a Movies library pointing at /em-media if not already there.
    folders_response = requests.get(
        f"{base_url}/Library/VirtualFolders",
        headers=_authed_headers(access_token),
        timeout=30,
    )
    folders_response.raise_for_status()
    existing = folders_response.json()
    have_movies = any(isinstance(f, dict) and f.get("Name") == "Movies" for f in (existing or []))
    if not have_movies:
        # Emby's AddVirtualFolder takes query params, not a JSON body.
        add_response = requests.post(
            f"{base_url}/Library/VirtualFolders",
            params={
                "name": "Movies",
                "collectionType": "movies",
                "paths": "/em-media/Movies",
                "refreshLibrary": "true",
            },
            headers=_authed_headers(access_token),
            timeout=60,
        )
        if not add_response.ok:
            raise RuntimeError(
                f"Emby AddVirtualFolder failed: HTTP {add_response.status_code} {add_response.text[:300]}"
            )

    return ServerCredentials(
        name="emby",
        server_id=server_id,
        access_token=access_token,
        user_id=user_id,
        base_url=base_url,
        library_remote_path="/em-media/Movies",
    )


def setup_plex(*, base_url: str = PLEX_URL) -> ServerCredentials:
    """Capture Plex's admin token from the running container.

    Assumes the container was started with a valid PLEX_CLAIM env
    var (the claim token from https://plex.tv/claim, 4-min validity).
    On first start Plex consumes the claim and persists an admin
    token in Preferences.xml; we read it via ``docker exec``.
    """
    import re
    import subprocess

    _wait_for_http(f"{base_url}/identity")

    proc = subprocess.run(
        [
            "docker",
            "exec",
            "previews-test-plex",
            "grep",
            "-oP",
            r'PlexOnlineToken="\K[^"]+',
            "/config/Library/Application Support/Plex Media Server/Preferences.xml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"Could not extract Plex admin token: {proc.stderr.strip()}")
    token = proc.stdout.strip()

    resp = requests.get(f"{base_url}/identity", timeout=10)
    resp.raise_for_status()
    machine_id_match = re.search(r'machineIdentifier="([^"]+)"', resp.text)
    machine_id = machine_id_match.group(1) if machine_id_match else ""

    return ServerCredentials(
        name="plex",
        server_id=machine_id,
        access_token=token,
        user_id="",
        base_url=base_url,
        library_remote_path="/media/Movies",
    )


# Jellyfin (linuxserver image) bind-mount layout, all relative to HERE:
#   <bind>/                       = /config
#   <bind>/data/                  = ProgramDataPath (/config/data) → off-media jellyfin_config_folder
#   <bind>/data/data/jellyfin.db  = the SQLite DB (the ApiKeys table lives here)
#   <bind>/data/plugins/          = plugin install dir
_JF_BIND = HERE / "jellyfin-config"
_JF_PROGRAMDATA = _JF_BIND / "data"
_JF_DB = _JF_PROGRAMDATA / "data" / "jellyfin.db"
_JF_PLUGINS = _JF_PROGRAMDATA / "plugins"

_PLUGIN_DIR = HERE.parent.parent / "jellyfin-plugin"
_PLUGIN_DLL = _PLUGIN_DIR / "bin" / "Release" / "net9.0" / "Jellyfin.Plugin.MediaPreviewBridge.dll"
_PLUGIN_GUID = "c2cb9bf9-7c5d-4f1a-9a07-2d6f5e5b0001"

_COMPOSE = ["docker", "compose", "-f", str(HERE / "docker-compose.test.yml")]


def _plugin_version() -> str:
    import re

    text = (_PLUGIN_DIR / "Jellyfin.Plugin.MediaPreviewBridge.csproj").read_text(encoding="utf-8")
    m = re.search(r"<Version>([^<]+)</Version>", text)
    return m.group(1) if m else "10.11.0.0"


def _ensure_plugin_dll() -> Path:
    """Build the Media Preview Bridge plugin DLL via the dotnet SDK image if absent.

    A fresh CI checkout has no ``bin/`` so this builds from the committed plugin
    source (~20s). Locally it reuses an existing build for fast iteration.
    """
    import subprocess

    if _PLUGIN_DLL.exists():
        return _PLUGIN_DLL
    print("[setup] building Media Preview Bridge plugin (dotnet sdk:9.0)…", flush=True)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{_PLUGIN_DIR}:/src",
            "-w",
            "/src",
            "mcr.microsoft.com/dotnet/sdk:9.0",
            "dotnet",
            "build",
            "-c",
            "Release",
        ],
        check=True,
        capture_output=True,
    )
    if not _PLUGIN_DLL.exists():
        raise RuntimeError(f"plugin build did not produce {_PLUGIN_DLL}")
    return _PLUGIN_DLL


def _install_plugin() -> None:
    """Install the built DLL + meta.json into Jellyfin's plugins dir (idempotent)."""
    import json
    import shutil

    dll = _ensure_plugin_dll()
    ver = _plugin_version()
    # Remove any older install so exactly one version loads.
    if _JF_PLUGINS.exists():
        for old in _JF_PLUGINS.glob("Media Preview Bridge_*"):
            shutil.rmtree(old, ignore_errors=True)
    dest = _JF_PLUGINS / f"Media Preview Bridge_{ver}"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dll, dest / dll.name)
    (dest / "meta.json").write_text(
        json.dumps(
            {
                "category": "Media Tools",
                "guid": _PLUGIN_GUID,
                "name": "Media Preview Bridge",
                "owner": "stevezau",
                "targetAbi": "10.11.0.0",
                "version": ver,
                "status": "Active",
                "autoUpdate": False,
                "assemblies": [],
            }
        ),
        encoding="utf-8",
    )


def _jf_existing_token() -> str:
    """Read the integration-test API key from the bind-mounted DB (read-only)."""
    import sqlite3

    if not _JF_DB.exists():
        return ""
    try:
        # mode=ro (not immutable): Jellyfin may hold the DB open + WAL-writing;
        # immutable=1 would skip the -wal sidecar and risk a stale read.
        con = sqlite3.connect(f"file:{_JF_DB}?mode=ro", uri=True)
        row = con.execute(
            "SELECT AccessToken FROM ApiKeys WHERE Name='integration-test' ORDER BY DateLastActivity DESC LIMIT 1"
        ).fetchone()
        con.close()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


def _jf_plugin_loaded(base_url: str) -> bool:
    try:
        r = requests.get(f"{base_url}/MediaPreviewBridge/Ping", timeout=5)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


def setup_jellyfin_via_api_key_injection(*, base_url: str = JELLYFIN_URL) -> ServerCredentials:
    """Bootstrap a real Jellyfin 10.11 for the integration suite.

    Jellyfin's first-run wizard (``POST /Startup/User``) is broken across
    10.9-10.11 (Users.First() on an empty user table), so we inject an admin
    API key straight into the ``ApiKeys`` table — verified to satisfy the
    plugin's ``RequiresElevation`` endpoints without a completed wizard.

    The config dir is bind-mounted (``<here>/jellyfin-config``) so we can also
    drop the Media Preview Bridge plugin into ``<config>/data/plugins`` and so
    the off-media test can write trickplay into Jellyfin's data folder. On the
    linuxserver image ProgramDataPath is ``/config/data`` and the DB lives at
    ``<config>/data/data/jellyfin.db``.

    Idempotent: reuses an existing key, only stops/starts Jellyfin when it must
    inject a key or load a freshly-installed plugin.
    """
    import secrets
    import subprocess
    from datetime import datetime, timezone

    _install_plugin()  # drop the DLL in before deciding whether a restart is needed

    token = _jf_existing_token()
    need_restart = not _jf_plugin_loaded(base_url)  # plugin just installed / not yet loaded

    if not token or need_restart:
        subprocess.run([*_COMPOSE, "stop", "jellyfin"], check=True, capture_output=True)
        # try/finally so a sqlite failure (locked DB, schema drift) never leaves
        # Jellyfin stopped — the next up.sh would find a dead server.
        try:
            if not token:
                import sqlite3

                token = f"integration-{secrets.token_hex(16)}"
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
                con = sqlite3.connect(str(_JF_DB))
                con.execute(
                    "INSERT INTO ApiKeys (DateCreated, DateLastActivity, Name, AccessToken) VALUES (?,?,?,?)",
                    (now, now, "integration-test", token),
                )
                con.commit()
                con.close()
        finally:
            subprocess.run([*_COMPOSE, "start", "jellyfin"], check=True, capture_output=True)

    _wait_for_http(f"{base_url}/System/Info/Public")

    # Verify the token works + capture identity.
    info = requests.get(
        f"{base_url}/System/Info",
        headers={"X-Emby-Token": token, "Accept": "application/json"},
        timeout=30,
    )
    info.raise_for_status()
    server_id = str(info.json().get("Id") or "")
    if not server_id:
        raise RuntimeError(f"Jellyfin /System/Info returned no Id field with the injected token: {info.json()}")

    # Configure a Movies library if not already present (idempotent).
    folders_resp = requests.get(
        f"{base_url}/Library/VirtualFolders",
        headers={"X-Emby-Token": token, "Accept": "application/json"},
        timeout=30,
    )
    folders_resp.raise_for_status()
    have_movies = any(isinstance(f, dict) and f.get("Name") == "Movies" for f in (folders_resp.json() or []))
    if not have_movies:
        add_resp = requests.post(
            f"{base_url}/Library/VirtualFolders",
            params={
                "name": "Movies",
                "collectionType": "movies",
                "paths": "/jf-media/Movies",
                "refreshLibrary": "true",
            },
            headers={"X-Emby-Token": token, "Content-Type": "application/json"},
            json={
                "LibraryOptions": {
                    "EnabledMetadataReaders": ["Nfo"],
                    "DisabledMetadataReaders": [],
                    "EnabledMetadataFetcherOrder": [],
                    "DisabledImageFetchers": [],
                    "EnabledImageFetchers": [],
                    "DisabledImageFetcherOrder": [],
                }
            },
            timeout=60,
        )
        if not add_resp.ok:
            raise RuntimeError(f"Jellyfin AddVirtualFolder failed: HTTP {add_resp.status_code} {add_resp.text[:300]}")

    return ServerCredentials(
        name="jellyfin",
        server_id=server_id,
        access_token=token,
        user_id="",  # API-key auth doesn't have a user context
        base_url=base_url,
        library_remote_path="/jf-media/Movies",
        # ProgramDataPath host path = off-media jellyfin_config_folder.
        config_dir=str(_JF_PROGRAMDATA.resolve()),
    )


def setup_jellyfin_with_existing_token(
    *,
    base_url: str,
    access_token: str,
    user_id: str,
) -> ServerCredentials:
    """Capture identity for an already-set-up Jellyfin (manual wizard).

    Jellyfin 10.9-10.11 have a bug where ``POST /Startup/User`` throws on
    a fresh install (the controller calls ``Users.First()`` against an
    empty user table). Until that's fixed upstream we don't try to
    automate the wizard — instead the user runs it once via the web UI
    and passes the resulting access token here.
    """
    info = requests.get(
        f"{base_url}/System/Info",
        headers=_authed_headers(access_token),
        timeout=30,
    )
    info.raise_for_status()
    server_id = str(info.json().get("Id") or "")
    if not server_id:
        raise RuntimeError("Jellyfin /System/Info missing Id field")

    return ServerCredentials(
        name="jellyfin",
        server_id=server_id,
        access_token=access_token,
        user_id=user_id,
        base_url=base_url,
        library_remote_path="/jf-media/Movies",
    )


def _write_env_file(credentials: list[ServerCredentials]) -> None:
    """Persist credentials to ``servers.env``, merging with any existing entries.

    Calling this with --server emby then --server plex appends Plex's
    keys without clobbering Emby's. Re-running with the same vendor
    overwrites that vendor's keys only.
    """
    existing: dict[str, str] = {}
    if SERVERS_ENV.exists():
        for raw in SERVERS_ENV.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            existing[key.strip()] = value.strip()

    for c in credentials:
        prefix = c.name.upper()
        existing[f"{prefix}_URL"] = c.base_url
        existing[f"{prefix}_SERVER_ID"] = c.server_id
        existing[f"{prefix}_ACCESS_TOKEN"] = c.access_token
        existing[f"{prefix}_USER_ID"] = c.user_id
        existing[f"{prefix}_LIBRARY_REMOTE_PATH"] = c.library_remote_path
        if c.config_dir:
            existing[f"{prefix}_CONFIG_DIR"] = c.config_dir

    SERVERS_ENV.write_text(
        "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        choices=("all", "emby", "jellyfin", "plex"),
        default="all",
        help="Which server(s) to configure (default: all).",
    )
    parser.add_argument(
        "--jellyfin-token",
        default=None,
        help="Jellyfin AccessToken from a manually-completed wizard (Jellyfin's API "
        "first-run flow is broken; we don't automate it). Pair with --jellyfin-user-id.",
    )
    parser.add_argument(
        "--jellyfin-user-id",
        default=None,
        help="Jellyfin admin user id matching --jellyfin-token.",
    )
    args = parser.parse_args()

    captured: list[ServerCredentials] = []

    if args.server in ("all", "emby"):
        print("[setup] configuring emby ...", flush=True)
        try:
            captured.append(setup_emby())
        except Exception as exc:
            print(f"[setup] emby failed: {exc}", file=sys.stderr)
            if args.server == "emby":
                return 1

    if args.server in ("all", "plex"):
        print("[setup] configuring plex ...", flush=True)
        try:
            captured.append(setup_plex())
        except Exception as exc:
            print(f"[setup] plex failed: {exc}", file=sys.stderr)
            if args.server == "plex":
                return 1

    if args.server in ("all", "jellyfin"):
        if args.jellyfin_token and args.jellyfin_user_id:
            print("[setup] capturing jellyfin identity ...", flush=True)
            try:
                captured.append(
                    setup_jellyfin_with_existing_token(
                        base_url=JELLYFIN_URL,
                        access_token=args.jellyfin_token,
                        user_id=args.jellyfin_user_id,
                    )
                )
            except Exception as exc:
                print(f"[setup] jellyfin failed: {exc}", file=sys.stderr)
        else:
            print("[setup] configuring jellyfin via API-key injection ...", flush=True)
            try:
                captured.append(setup_jellyfin_via_api_key_injection())
            except Exception as exc:
                print(f"[setup] jellyfin failed: {exc}", file=sys.stderr)
                if args.server == "jellyfin":
                    return 1

    if not captured:
        print("[setup] no servers configured; nothing to write", file=sys.stderr)
        return 1

    _write_env_file(captured)
    print(f"[setup] credentials written to {SERVERS_ENV}", flush=True)
    for c in captured:
        print(f"  - {c.name}: id={c.server_id} url={c.base_url}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
