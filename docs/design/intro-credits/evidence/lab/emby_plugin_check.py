#!/usr/bin/env python3
"""Lab proof for the Media Preview Bridge for Emby plugin (plan-phase2 Task 4), on each lab Emby.

    ./emby_plugin_check.py mlab-emby mlab-emby49     run the whole check table on each container, in order
    ./emby_plugin_check.py mlab-emby --checks 9,10,16,11
                                                     run only these checks (9 before 10, 16 and 11), merged into
                                                     that container's results file

Install the plugin first (emby-plugin/README.md). Checks 1-8, 12-15, 20 and 21 use Synth Chapters S01E01; 17 and 19
use S01E02; 9-11, 16 and 18 use a disposable copy of S01E01 inside the container's own config volume (library "Plugin
Check" at /config/plugcheck), so the synth folder the other lab servers mount never changes. Check 17 stops the
container once to add marker rows the plugin didn't write straight into Emby's library.db (Emby's own intro detection
needs Emby Premiere, which the lab doesn't have). Prints one line per check and writes
results/emby-plugin-<container>.json. Credentials come from ./env (EMBY_TOKEN/EMBY_UID, EMBY49_TOKEN/EMBY49_UID) and
are scrubbed from everything printed or written. The script ends by removing the markers, the copy, its library and
the sessions it created.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import phase1_matrix as pm

SERVERS = {
    "mlab-emby": {"url": "http://127.0.0.1:18096", "token": "EMBY_TOKEN", "uid": "EMBY_UID", "web_client": True},
    "mlab-emby49": {"url": "http://127.0.0.1:18099", "token": "EMBY49_TOKEN", "uid": "EMBY49_UID", "web_client": False},
}
ADMIN = object()
SEC = 10_000_000
INTRO = {"IntroStartTicks": 10 * SEC, "IntroEndTicks": 40 * SEC, "CreditsStartTicks": 100 * SEC}
INTRO_2 = {"IntroStartTicks": 12 * SEC, "IntroEndTicks": 42 * SEC}
E02_MARKERS = {"IntroStartTicks": 17 * SEC, "IntroEndTicks": 47 * SEC, "CreditsStartTicks": 100 * SEC}
ROWS_4 = [("IntroStart", 10 * SEC), ("IntroEnd", 40 * SEC), ("CreditsStart", 100 * SEC)]
ROWS_5 = [("IntroStart", 12 * SEC), ("IntroEnd", 42 * SEC)]
# Rows another writer left on S01E02 (Emby's MarkerType values: IntroStart 1, IntroEnd 2).
FOREIGN_INTRO = [(1, 20 * SEC, "Intro"), (2, 50 * SEC, "Intro End")]
FOREIGN_ROWS = [("IntroStart", 20 * SEC), ("IntroEnd", 50 * SEC)]
S01E01_HOST = pm.SYNTH_HOST_SEASON / "Synth Chapters (2021) - S01E01.webm"
S01E02_HOST = pm.SYNTH_HOST_SEASON / "Synth Chapters (2021) - S01E02.webm"
SYNTH_LIBRARY = "Synth Chapters"
COPY_LIBRARY = "Plugin Check"
COPY_ROOT = "/config/plugcheck"
COPY_SHOW = f"{COPY_ROOT}/Plugin Check (2021)"
COPY_PATH = f"{COPY_SHOW}/Season 01/Plugin Check (2021) - S01E01.webm"
VIEWER_DEVICE = "emby-plugin-check-viewer"
CLIENT = Path(__file__).resolve().parent / "emby_client.py"
CORRUPT_FORMS = {
    "garbage": b"corrupt-probe {{ not json",
    "NUL bytes": b"\0\0\0\0\0\0",
    "null": b"null",
    "[]": b"[]",
    "truncated number": b'{"IntroStartTicks":100000000,"IntroEndTicks":4',
    "truncated name": b'{"IntroSta',
    "empty": b"",
    # Ending in "}" gets past the plugin's completeness test to Emby's JSON reader.
    "garbage ending in }": b"corrupt-probe {{ not json }",
    "text where a number goes": b'{"IntroStartTicks":"corrupt-probe","IntroEndTicks":400000000}',
    "valid JSON without markers": b'{"FileSize":6421799}',
}
FOREIGN_WRITER = """
import json, sqlite3, sys
db = sqlite3.connect("/config/data/library.db")
item, rows = int(sys.argv[1]), json.loads(sys.argv[2])
index = db.execute("select coalesce(max(ChapterIndex), -1) from Chapters3 where ItemId = ?", (item,)).fetchone()[0]
for marker_type, ticks, name in rows:
    index += 1
    db.execute(
        "insert into Chapters3 (ItemId, ChapterIndex, StartPositionTicks, Name, MarkerType) values (?, ?, ?, ?, ?)",
        (item, index, ticks, name, marker_type),
    )
db.commit()
db.close()
"""


class EmbyCheck:
    def __init__(self, container: str) -> None:
        cfg = SERVERS[container]
        self.container = container
        self.url = cfg["url"]
        self.token = pm.ENV[cfg["token"]]
        self.uid = pm.ENV[cfg["uid"]]
        self.web_client = cfg["web_client"]
        self.size = S01E01_HOST.stat().st_size
        self.e02_size = S01E02_HOST.stat().st_size
        self.results: list[dict] = []
        self.session_tokens: list[str] = []
        self.item = self.other = self.copy = None
        self.copy_size = self.size

    # ---------------------------------------------------------------------------------------------------- helpers

    def scrub(self, value: Any) -> Any:
        value = pm.scrub(value)
        text = json.dumps(value, default=str)
        for secret in self.session_tokens:
            text = text.replace(secret, "****")
        return json.loads(text)

    def call(self, method: str, path: str, body: Any = None, token: Any = ADMIN, device: str = "") -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if token is ADMIN:
            headers["X-Emby-Token"] = self.token
        elif token:
            headers["X-Emby-Token"] = token
        if device:
            headers["X-Emby-Authorization"] = (
                f'Emby Client="emby_plugin_check", Device="{device}", DeviceId="{device}", Version="1.0"'
            )
        return pm.http(method, f"{self.url}/emby{path}", headers=headers, body=body)

    def answers(self) -> bool:
        try:
            return self.call("GET", "/System/Info")[0] == 200
        except OSError:  # refused or reset while Emby restarts
            return False

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        status, data = self.call(method, path, body)
        if status >= 300:
            raise RuntimeError(f"{method} {path} -> {status}: {self.scrub(data)}")
        return data

    def bridge(self, method: str, item_id: str, body: Any = None, token: Any = ADMIN) -> tuple[int, Any]:
        return self.call(method, f"/MediaPreviewBridge/Markers/{item_id}", body, token)

    def chapters(self, item_id: str) -> list[dict]:
        item = self.ok("GET", f"/Users/{self.uid}/Items/{item_id}?Fields=Chapters")
        return [
            {"Name": c.get("Name"), "MarkerType": c.get("MarkerType"), "Start": c.get("StartPositionTicks")}
            for c in item.get("Chapters") or []
        ]

    @staticmethod
    def plain(chapters: list[dict]) -> list[tuple]:
        return [(c["Name"], c["Start"]) for c in chapters if c["MarkerType"] == "Chapter"]

    @staticmethod
    def marker_rows(chapters: list[dict]) -> list[tuple]:
        return sorted(
            ((c["MarkerType"], c["Start"]) for c in chapters if c["MarkerType"] != "Chapter"), key=lambda r: r[1]
        )

    def docker(self, *args: str, check: bool = True) -> str:
        return pm.sh("docker", "exec", self.container, *args, check=check)

    def log_count(self, text: str) -> int:
        out = self.docker("grep", "-c", "-F", text, "/config/logs/embyserver.txt", check=False).strip()
        return int(out or 0)

    def log_lines(self, text: str) -> list[str]:
        return self.docker("grep", "-F", text, "/config/logs/embyserver.txt", check=False).splitlines()

    def store_dir(self) -> str:
        # Rotated logs too: a container up for more than a day has started a new embyserver.txt since.
        line = self.docker(
            "sh", "-c", "grep -h 'Media Preview Bridge: marker store' /config/logs/embyserver*.txt | tail -1"
        )
        return line.rsplit("marker store ", 1)[1].strip()

    def store_files(self) -> list[str]:
        return sorted(self.docker("sh", "-c", f"ls -1 '{self.store_dir()}' 2>/dev/null || true").split())

    def write_store_file(self, name: str, content: bytes) -> None:
        subprocess.run(
            ["docker", "exec", "-i", "-u", "1000:1000", self.container, "sh", "-c", 'cat > "$1"', "sh"]
            + [f"{self.store_dir()}/{name}"],
            input=content,
            check=True,
            capture_output=True,
            timeout=60,
        )

    def read_store_file(self, item_id: str) -> Any:
        return json.loads(self.docker("cat", f"{self.store_dir()}/{item_id}.json"))

    def real_store_file(self) -> str:
        """The store file POST writes for S01E01 (markers removed again afterwards)."""
        self.bridge("POST", self.item, {**INTRO, "FileSize": self.size})
        raw = self.docker("cat", f"{self.store_dir()}/{self.item}.json")
        self.bridge("DELETE", self.item)
        return raw

    def cut_inside_path(self) -> bytes:
        """A real store file for S01E01 cut in the middle of its Path string."""
        raw = self.real_store_file()
        start = raw.index('"Path":"') + len('"Path":"')
        return raw[: (start + raw.index('"', start)) // 2].encode()

    def wait_log(self, text: str, before: int, timeout: float = 60) -> bool:
        try:
            pm.wait_until(text, lambda: self.log_count(text) > before, timeout=timeout)
            return True
        except TimeoutError:
            return False

    def wait_ready(self) -> None:
        pm.wait_until("Emby back", self.answers, timeout=300, every=3)
        pm.wait_until("plugin entry point", lambda: self.log_count("Media Preview Bridge: marker store") > 0, 120)
        time.sleep(15)

    def restart(self) -> None:
        pm.sh("docker", "restart", self.container, timeout=300)
        self.wait_ready()

    def refresh(self, item_id: str, mode: str, replace_all: bool) -> bool:
        """Refresh one item and wait for Emby's "RefreshItem Complete" log line (False: not seen within 60 s)."""
        done = f"RefreshItem Complete: {item_id} "
        before = self.log_count(done)
        query = urllib.parse.urlencode(
            {
                "Recursive": "false",
                "MetadataRefreshMode": mode,
                "ImageRefreshMode": "Default",
                "ReplaceAllMetadata": str(replace_all).lower(),
                "ReplaceAllImages": "false",
            }
        )
        self.ok("POST", f"/Items/{item_id}/Refresh?{query}")
        return self.wait_log(done, before)

    def wait_task(self, key: str, started: str, timeout: float = 600) -> dict:
        def finished() -> dict | None:
            task = next(t for t in self.ok("GET", "/ScheduledTasks") if t.get("Key") == key)
            end = (task.get("LastExecutionResult") or {}).get("EndTimeUtc", "")
            return task if task["State"] == "Idle" and end[:19] >= started else None

        return pm.wait_until(key, finished, timeout=timeout, every=3)

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def scan_library(self) -> None:
        started = self.utc_now()
        self.ok("POST", "/Library/Refresh")
        self.wait_task("RefreshLibrary", started)

    def run_task(self, key: str) -> dict:
        task = next(t for t in self.ok("GET", "/ScheduledTasks") if t.get("Key") == key)
        started = self.utc_now()
        self.ok("POST", f"/ScheduledTasks/Running/{task['Id']}")
        return self.wait_task(key, started)

    def devices(self) -> dict[str, str]:
        return {d["Id"]: d.get("ReportedDeviceId", "") for d in self.ok("GET", "/Devices")["Items"]}

    def delete_devices(self, ids: list[str]) -> None:
        for device_id in ids:
            self.ok("DELETE", f"/Devices?Id={urllib.parse.quote(device_id)}")

    def find_episode(self, path: str, parent_id: str | None = None) -> str | None:
        query = {"Recursive": "true", "IncludeItemTypes": "Episode", "Fields": "Path"}
        if parent_id:
            query["ParentId"] = parent_id
        else:
            query["SearchTerm"] = "Synth Chapters"
        items = self.ok("GET", f"/Items?{urllib.parse.urlencode(query)}")["Items"]
        return next((i["Id"] for i in items if i.get("Path") == path), None)

    def library(self, name: str) -> dict | None:
        return next((lib for lib in self.ok("GET", "/Library/VirtualFolders") if lib["Name"] == name), None)

    def set_marker_detection(self, library_name: str, enabled: bool) -> bool:
        """Turn Emby's own intro detection on or off for a library; returns the previous setting."""
        library = self.library(library_name)
        options = library["LibraryOptions"]
        previous = bool(options.get("EnableMarkerDetection"))
        options["EnableMarkerDetection"] = enabled
        self.ok("POST", "/Library/VirtualFolders/LibraryOptions", {"Id": library["ItemId"], "LibraryOptions": options})
        return previous

    def plant_foreign_rows(self, item_id: str, rows: list[tuple]) -> None:
        """Add marker rows to Emby's library.db while the container is stopped, as another writer would leave them."""
        volume = pm.sh(
            "docker",
            "inspect",
            self.container,
            "--format",
            '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Name}}{{end}}{{end}}',
        ).strip()
        python_image = pm.sh("docker", "inspect", "mlab-app", "--format", "{{.Config.Image}}").strip()
        pm.sh("docker", "stop", self.container, timeout=120)
        try:
            pm.sh(
                "docker",
                "run",
                "--rm",
                "-u",
                "1000:1000",
                "--entrypoint",
                "python3",
                "-v",
                f"{volume}:/config",
                python_image,
                "-c",
                FOREIGN_WRITER,
                item_id,
                json.dumps(rows),
            )
        finally:
            pm.sh("docker", "start", self.container)
            self.wait_ready()

    def web_skip(self, label: str) -> dict:
        """Play the item in Emby web (Playwright, emby_client.py) to 0:22 and report the Skip Intro button."""
        shot = pm.SHOTS / f"emby-plugin-{self.container}-{label}.png"
        pm.SHOTS.mkdir(parents=True, exist_ok=True)
        before = set(self.devices())
        try:
            out = subprocess.run(
                ["nice", "-n", "19", sys.executable, str(CLIENT), self.item, str(shot)],
                capture_output=True,
                text=True,
                timeout=300,
            )
        finally:
            new_devices = sorted(set(self.devices()) - before)
            self.delete_devices(new_devices)
        line = next((ln for ln in out.stdout.splitlines() if ln.startswith("skip found:")), out.stderr[-500:])
        position = re.search(r"\| t: (-?[\d.]+)", line)
        return {
            "found": line.startswith("skip found: True"),
            "position_s": float(position.group(1)) if position else None,
            "client": line,
            "screenshot": str(shot),
            "web_devices_removed": new_devices,
        }

    def ensure_copy(self) -> str:
        """The disposable copy of S01E01 as an item in the "Plugin Check" library; its item id."""
        self.docker("mkdir", "-p", COPY_PATH.rsplit("/", 1)[0])
        self.docker("cp", pm.synth_path(1), COPY_PATH)
        self.docker("chown", "-R", "1000:1000", COPY_ROOT)
        self.copy_size = self.size
        library = self.library(COPY_LIBRARY)
        if library is None:
            options = {
                "EnableRealtimeMonitor": False,
                "EnableMarkerDetection": False,
                "EnableMarkerDetectionDuringLibraryScan": False,
                "EnableChapterImageExtraction": False,
                "PathInfos": [{"Path": COPY_ROOT}],
            }
            query = urllib.parse.urlencode(
                {"name": COPY_LIBRARY, "collectionType": "tvshows", "refreshLibrary": "true"}
            )
            self.ok("POST", f"/Library/VirtualFolders?{query}", {"LibraryOptions": options})
            library = self.library(COPY_LIBRARY)
        else:
            self.scan_library()
        return pm.wait_until("copy item", lambda: self.find_episode(COPY_PATH, library["ItemId"]), timeout=300, every=3)

    def record(self, number: int, title: str, passed: bool, evidence: dict) -> None:
        result = "PASS" if passed else "FAIL"
        self.results.append({"check": number, "title": title, "result": result, **self.scrub(evidence)})
        pm.say(f"{self.container} check {number}: {result} — {title}")

    def run_check(self, number: int, title: str, fn) -> None:
        try:
            passed, evidence = fn()
        except Exception as exc:  # a broken check is a failed check; the table keeps going
            passed, evidence = False, {"exception": f"{type(exc).__name__}: {exc}"}
        self.record(number, title, passed, evidence)

    # ------------------------------------------------------------------------------------------ S01E01: API basics

    def check_1_ping(self) -> tuple[bool, dict]:
        status, data = self.call("GET", "/MediaPreviewBridge/Ping", token=None)
        ok = status == 200 and isinstance(data, dict) and data.get("Ok") is True and data.get("Features") == ["markers"]
        return ok, {"status": status, "body": data}

    def check_2_not_found(self) -> tuple[bool, dict]:
        status, data = self.bridge("GET", "999999999")
        ok = status == 200 and data.get("Found") is False and data.get("Error") == "item not found"
        return ok, {"status": status, "body": data}

    def viewer_token(self) -> str:
        users = self.ok("GET", "/Users")
        if not any(u["Name"] == "viewer" for u in users):
            self.ok("POST", "/Users/New", {"Name": "viewer"})
        status, auth = self.call(
            "POST", "/Users/AuthenticateByName", {"Username": "viewer", "Pw": ""}, None, VIEWER_DEVICE
        )
        if status != 200:
            raise RuntimeError(f"viewer login -> {status}")
        self.session_tokens.append(auth["AccessToken"])
        if auth["User"]["Policy"].get("IsAdministrator"):
            raise RuntimeError("user viewer is an administrator")
        return auth["AccessToken"]

    def check_3_auth(self) -> tuple[bool, dict]:
        viewer = self.viewer_token()
        statuses = {}
        try:
            for who, token in (("no token", None), ("viewer", viewer)):
                for method, body in (("GET", None), ("POST", INTRO), ("DELETE", None)):
                    statuses[f"{who} {method}"] = self.bridge(method, self.item, body, token)[0]
        finally:
            self.call("POST", "/Sessions/Logout", token=viewer)
            self.delete_devices([i for i, reported in self.devices().items() if reported == VIEWER_DEVICE])
        ok = all(s == 401 for k, s in statuses.items() if k.startswith("no token")) and all(
            s in (401, 403) for k, s in statuses.items() if k.startswith("viewer")
        )
        return ok, {"statuses": statuses, "viewer_sessions_removed": VIEWER_DEVICE not in self.devices().values()}

    def check_4_post(self) -> tuple[bool, dict]:
        status, data = self.bridge("POST", self.item, {**INTRO, "FileSize": self.size})
        chapters = self.chapters(self.item)
        ok = (
            status == 200
            and data.get("Stored") == 3
            and not data.get("Error")
            and data.get("Stale") is False
            and self.marker_rows(chapters) == ROWS_4
            and self.plain(chapters) == self.originals
        )
        return ok, {"status": status, "body": data, "file_size": self.size, "chapters": chapters}

    def check_5_replace(self) -> tuple[bool, dict]:
        status, data = self.bridge("POST", self.item, {**INTRO_2, "FileSize": self.size})
        chapters = self.chapters(self.item)
        ok = (
            status == 200
            and data.get("Stored") == 2
            and self.marker_rows(chapters) == ROWS_5
            and self.plain(chapters) == self.originals
        )
        return ok, {"status": status, "body": data, "chapters": chapters}

    def check_6_invalid(self) -> tuple[bool, dict]:
        before = self.chapters(self.item)
        bodies = {
            "intro start without end": {"IntroStartTicks": 10 * SEC, "FileSize": self.size},
            "end <= start": {"IntroStartTicks": 40 * SEC, "IntroEndTicks": 40 * SEC},
            "negative credits": {"CreditsStartTicks": -1},
            "FileSize 0": {**INTRO_2, "FileSize": 0},
            "all null": {"IntroStartTicks": None, "IntroEndTicks": None, "CreditsStartTicks": None, "FileSize": None},
        }
        answers, ok = {}, True
        for name, body in bodies.items():
            status, data = self.bridge("POST", self.item, body)
            unchanged = self.chapters(self.item) == before
            answers[name] = {
                "status": status,
                "error": data.get("Error"),
                "found": data.get("Found"),
                "unchanged": unchanged,
            }
            ok = ok and status == 200 and data.get("Found") is True and bool(data.get("Error")) and unchanged
        _, stored = self.bridge("GET", self.item)
        ok = (
            ok
            and stored.get("IntroStartTicks") == INTRO_2["IntroStartTicks"]
            and stored.get("CreditsStartTicks") is None
        )
        return ok, {"answers": answers, "stored_after": stored}

    # ----------------------------------------------------------------------------- S01E01: refreshes and restarts

    def check_7_full_refresh(self) -> tuple[bool, dict]:
        heal = f"markers written back for item {self.item} ("
        modes, ok = {}, True
        for name, replace_all in (("Replace all metadata", True), ("Search for missing metadata", False)):
            before = self.log_count(heal)
            completed = self.refresh(self.item, "FullRefresh", replace_all=replace_all)
            healed = self.wait_log(heal, before)
            back = pm.wait_until("marker rows", lambda: self.marker_rows(self.chapters(self.item)) == ROWS_5, 60)
            chapters = self.chapters(self.item)
            modes[name] = {
                "refresh_complete_logged": completed,
                "wiped_and_written_back": healed,
                "marker_rows": self.marker_rows(chapters),
            }
            ok = ok and healed and bool(back) and self.plain(chapters) == self.originals
        return ok, {"modes": modes}

    def check_8_survives(self) -> tuple[bool, dict]:
        heal = f"markers written back for item {self.item} ("
        steps, ok = {}, True
        for step in ("Default refresh", "ValidationOnly refresh", "library scan", "docker restart"):
            before = self.log_count(heal)
            extra = {}
            if step == "Default refresh":
                done = self.refresh(self.item, "Default", replace_all=False)
            elif step == "ValidationOnly refresh":
                done = self.refresh(self.item, "ValidationOnly", replace_all=False)
            elif step == "library scan":
                self.scan_library()
                done = True
            else:
                # A save interrupted by a crash leaves <id>.json.tmp; start-up removes it.
                self.write_store_file("999999999.json.tmp", b'{"IntroStartTicks":1')
                self.restart()
                before = 0  # the restart starts a new embyserver.txt
                done = True
                extra = {
                    "tmp_removed": "999999999.json.tmp" not in self.store_files(),
                    "tmp_log": self.log_count("removed 1 unfinished marker file(s)") > 0,
                }
                ok = ok and extra["tmp_removed"]
            chapters = self.chapters(self.item)
            kept = self.marker_rows(chapters) == ROWS_5 and self.plain(chapters) == self.originals
            steps[step] = {
                "completed": done,
                "markers_kept": kept,
                "written_back_by_plugin": self.log_count(heal) > before,
                "marker_rows": self.marker_rows(chapters),
                **extra,
            }
            ok = ok and kept
        return ok, {"steps": steps}

    def check_12_web_skip(self) -> tuple[bool, dict]:
        seen = self.web_skip("skip")
        return seen["found"], seen

    def check_13_delete_no_skip(self) -> tuple[bool, dict]:
        status, data = self.bridge("DELETE", self.item)
        chapters = self.chapters(self.item)
        seen = self.web_skip("no-skip")
        ok = (
            status == 200
            and data.get("Stored") == 0
            and self.marker_rows(chapters) == []
            and self.plain(chapters) == self.originals
            and not seen["found"]
            and (seen["position_s"] or 0) >= 22  # the player really was inside the old intro
        )
        return ok, {"status": status, "body": data, "chapters": chapters, "web": seen}

    # ------------------------------------------------------------------------------ S01E01: what the store holds

    def check_14_corrupt_store(self) -> tuple[bool, dict]:
        self.bridge("DELETE", self.item)
        heal = f"markers written back for item {self.item} ("
        warning = f"ignoring marker file for item {self.item}:"
        forms, ok = {}, True
        cut = self.cut_inside_path()
        for name, content in {
            **CORRUPT_FORMS,
            "truncated inside Path": cut,
            "cut inside Path, then }": cut + b"}",
        }.items():
            warnings_before, heals_before = self.log_count(warning), self.log_count(heal)
            self.write_store_file(f"{self.item}.json", content)
            status, data = self.bridge("GET", self.item)
            completed = self.refresh(self.item, "FullRefresh", replace_all=True)
            self.bridge("GET", self.item)
            rows = self.marker_rows(self.chapters(self.item))
            new_warnings = self.log_lines(warning)[warnings_before:]
            forms[name] = {
                "status": status,
                "body": data,
                "refresh_complete_logged": completed,
                "marker_rows": rows,
                "written_back": self.log_count(heal) > heals_before,
                "warnings": [line.split("Media Preview Bridge: ", 1)[-1] for line in new_warnings],
            }
            ok = (
                ok
                and status == 200
                and data.get("Found") is True
                and not data.get("Error")
                and data.get("IntroStartTicks") is None
                and data.get("CreditsStartTicks") is None
                and completed
                and rows == []
                and not forms[name]["written_back"]
                and len(new_warnings) == 1
                and not any("corrupt-probe" in line for line in new_warnings)
            )
        status, data = self.bridge("POST", self.item, {**INTRO, "FileSize": self.size})
        replaced = status == 200 and data.get("Stored") == 3
        self.bridge("DELETE", self.item)
        return ok and replaced, {"forms": forms, "post_over_corrupt_file": {"status": status, "body": data}}

    def check_15_path_mismatch(self) -> tuple[bool, dict]:
        heal = f"markers written back for item {self.item} ("
        self.bridge("POST", self.item, INTRO)
        stored = self.read_store_file(self.item)
        path_saved = stored.get("Path") == pm.synth_path(1)
        self.bridge("DELETE", self.item)
        planted = {**INTRO, "FileSize": self.size, "Path": pm.synth_path(1).replace("S01E01", "S01E09")}
        self.write_store_file(f"{self.item}.json", json.dumps(planted).encode())
        _, other_path = self.bridge("GET", self.item)
        before = self.log_count(heal)
        self.refresh(self.item, "FullRefresh", replace_all=True)
        rows_other_path = self.marker_rows(self.chapters(self.item))
        not_written = self.log_count(heal) == before
        # Control: the same file with the item's real path is written back by the same refresh.
        self.write_store_file(f"{self.item}.json", json.dumps({**planted, "Path": pm.synth_path(1)}).encode())
        self.refresh(self.item, "FullRefresh", replace_all=True)
        rows_same_path = self.marker_rows(self.chapters(self.item))
        self.bridge("DELETE", self.item)
        ok = (
            path_saved
            and other_path.get("Stale") is True
            and rows_other_path == []
            and not_written
            and rows_same_path == ROWS_4
        )
        return ok, {
            "store_file_after_post": stored,
            "get_with_other_path": other_path,
            "rows_after_refresh_other_path": rows_other_path,
            "rows_after_refresh_same_path": rows_same_path,
        }

    def check_20_cut_path_api(self) -> tuple[bool, dict]:
        name = f"{self.item}.json"
        # Every prefix of a real store file must answer 200 JSON with no markers. Emby's reader alone would accept an
        # object cut after a complete token (a cut inside a number read as credits at 1 tick); the plugin refuses those.
        raw = self.real_store_file()
        not_json, read_as_markers = [], []
        for length in range(len(raw)):
            self.write_store_file(name, raw[:length].encode())
            status, body = self.bridge("GET", self.item)
            if status != 200 or not isinstance(body, dict) or body.get("Found") is not True:
                not_json.append({"length": length, "status": status})
            elif body.get("IntroStartTicks") is not None or body.get("CreditsStartTicks") is not None:
                read_as_markers.append(length)
        cut = self.cut_inside_path()
        self.write_store_file(name, cut)
        get_status, got = self.bridge("GET", self.item)
        delete_status, deleted = self.bridge("DELETE", self.item)
        file_after_delete = name in self.store_files()
        self.write_store_file(name, cut)
        post_status, posted = self.bridge("POST", self.item, {**INTRO, "FileSize": self.size})
        repaired = self.read_store_file(self.item)
        _, got_after = self.bridge("GET", self.item)
        rows = self.marker_rows(self.chapters(self.item))
        self.bridge("DELETE", self.item)
        answers = {"GET": (get_status, got), "DELETE": (delete_status, deleted), "POST": (post_status, posted)}
        ok = (
            not not_json
            and not read_as_markers
            and all(
                status == 200 and isinstance(body, dict) and body.get("Found") is True
                for status, body in answers.values()
            )
            and got.get("IntroStartTicks") is None
            and not file_after_delete
            and posted.get("Stored") == 3
            and repaired.get("Path") == pm.synth_path(1)
            and got_after.get("IntroStartTicks") == INTRO["IntroStartTicks"]
            and rows == ROWS_4
        )
        return ok, {
            "prefixes_tried": len(raw),
            "prefixes_not_json": not_json,
            "prefix_lengths_read_as_markers": read_as_markers,
            "cut_file_tail": cut.decode()[-24:],
            "answers": {k: {"status": v[0], "body": v[1]} for k, v in answers.items()},
            "store_file_after_delete": file_after_delete,
            "store_file_after_post": repaired,
            "get_after_post": got_after,
            "rows_after_post": rows,
        }

    # ---------------------------------------------------------------------------------------- disposable copy

    def check_9_stale(self) -> tuple[bool, dict]:
        self.copy = self.ensure_copy()
        self.copy_originals = originals = self.plain(self.chapters(self.copy))
        status, data = self.bridge("POST", self.copy, {**INTRO, "FileSize": self.size + 1})
        after_post = self.chapters(self.copy)
        heal = f"markers written back for item {self.copy} ("
        completed = self.refresh(self.copy, "FullRefresh", replace_all=True)
        time.sleep(5)
        after_refresh = self.chapters(self.copy)
        ok = (
            status == 200
            and data.get("Stale") is True
            and data.get("Stored") == 0
            and self.marker_rows(after_post) == []
            and self.plain(after_post) == originals
            and completed
            and self.marker_rows(after_refresh) == []
            and self.log_count(heal) == 0
        )
        return ok, {
            "copy_item": self.copy,
            "status": status,
            "body": data,
            "chapters_after_post": after_post,
            "refresh_complete_logged": completed,
            "chapters_after_full_refresh": after_refresh,
        }

    def check_10_delete(self) -> tuple[bool, dict]:
        _, stored = self.bridge("POST", self.copy, {**INTRO, "FileSize": self.size})
        before = {
            "stored": stored.get("Stored"),
            "rows": self.marker_rows(self.chapters(self.copy)),
            "files": self.store_files(),
        }
        status, data = self.bridge("DELETE", self.copy)
        chapters = self.chapters(self.copy)
        files = self.store_files()
        ok = (
            before["rows"] == ROWS_4
            and f"{self.copy}.json" in before["files"]
            and status == 200
            and data.get("Stored") == 0
            and self.marker_rows(chapters) == []
            and self.plain(chapters) == self.copy_originals
            and f"{self.copy}.json" not in files
        )
        return ok, {
            "before": before,
            "status": status,
            "body": data,
            "chapters": chapters,
            "store_dir": self.store_dir(),
            "store_files": files,
        }

    def check_16_replaced_file(self) -> tuple[bool, dict]:
        removed = f"removed markers of a replaced file for item {self.copy}"
        heal = f"markers written back for item {self.copy} ("
        # A: the file is replaced by a different-size file and the library is scanned.
        _, stored = self.bridge("POST", self.copy, {**INTRO, "FileSize": self.size})
        rows_before = self.marker_rows(self.chapters(self.copy))
        removed_before = self.log_count(removed)
        self.docker("cp", pm.synth_path(2), COPY_PATH)
        self.docker("chown", "1000:1000", COPY_PATH)
        self.copy_size = self.e02_size
        self.scan_library()
        same_item = self.find_episode(COPY_PATH, self.library(COPY_LIBRARY)["ItemId"]) == self.copy
        try:
            pm.wait_until("marker rows gone", lambda: self.marker_rows(self.chapters(self.copy)) == [], 120, every=3)
        except TimeoutError:
            pass
        chapters = self.chapters(self.copy)
        _, lookup = self.bridge("GET", self.copy)
        replaced = {
            "rows_before": rows_before,
            "same_item_id": same_item,
            "chapters_after_scan": chapters,
            "get_after": lookup,
            "removed_by_plugin": self.log_count(removed) > removed_before,
            "written_back": self.log_count(heal) > 0,
        }
        ok_replaced = (
            stored.get("Stored") == 3
            and rows_before == ROWS_4
            and same_item
            and self.marker_rows(chapters) == []
            and lookup.get("Stale") is True
            and not replaced["written_back"]
        )
        # B: the size changes but Emby keeps the rows (mtime kept, so it doesn't re-read the file); the next item update
        # (a metadata edit here; Default refresh and scans of an unchanged item raise none) must remove our rows.
        self.bridge("POST", self.copy, {**INTRO, "FileSize": self.copy_size})
        rows_kept_before = self.marker_rows(self.chapters(self.copy))
        self.docker(
            "sh",
            "-c",
            'touch -r "$1" /tmp/mtime && printf "\\0" >> "$1" && touch -r /tmp/mtime "$1" && rm /tmp/mtime',
            "sh",
            COPY_PATH,
        )
        self.copy_size += 1
        removed_before = self.log_count(removed)
        self.refresh(self.copy, "Default", replace_all=False)
        rows_after_default = self.marker_rows(self.chapters(self.copy))
        dto = self.ok("GET", f"/Users/{self.uid}/Items/{self.copy}")
        edit_status = self.call("POST", f"/Items/{self.copy}", dto)[0]
        try:
            pm.wait_until("marker rows removed", lambda: self.marker_rows(self.chapters(self.copy)) == [], 60, every=2)
        except TimeoutError:
            pass
        size_changed = {
            "rows_before": rows_kept_before,
            "rows_after_default_refresh": rows_after_default,
            "metadata_edit_status": edit_status,
            "rows_after_metadata_edit": self.marker_rows(self.chapters(self.copy)),
            "removed_by_plugin": self.log_count(removed) > removed_before,
        }
        ok_size_changed = (
            rows_kept_before == ROWS_4
            and size_changed["rows_after_metadata_edit"] == []
            and size_changed["removed_by_plugin"]
        )
        return ok_replaced and ok_size_changed, {
            "file_replaced_and_scanned": replaced,
            "size_changed_rows_kept": size_changed,
        }

    def check_11_removed(self) -> tuple[bool, dict]:
        _, stored = self.bridge("POST", self.copy, {**INTRO, "FileSize": self.copy_size})
        name = f"{self.copy}.json"
        present = name in self.store_files()
        forgot = f"forgot markers of removed item {self.copy}"
        self.docker("rm", "-f", COPY_PATH)
        self.scan_library()
        gone = pm.wait_until("store file removed", lambda: name not in self.store_files(), timeout=120, every=3)
        _, lookup = self.bridge("GET", self.copy)
        ok = stored.get("Stored") == 3 and present and bool(gone) and self.log_count(forgot) > 0
        return ok, {
            "store_file_before": present,
            "store_files_after": self.store_files(),
            "log_line": self.log_count(forgot) > 0,
            "lookup_after": lookup,
        }

    def check_18_sweep(self) -> tuple[bool, dict]:
        orphan = self.ensure_copy()
        self.bridge("POST", orphan, {**INTRO, "FileSize": self.size})
        self.bridge("POST", self.other, {**E02_MARKERS, "FileSize": self.e02_size})
        self.docker("rm", "-rf", COPY_SHOW)
        self.scan_library()
        pm.wait_until("orphan item gone", lambda: self.bridge("GET", orphan)[1].get("Found") is False, 120, every=3)
        before = self.store_files()
        summary = "marker cleanup checked"
        summaries_before = self.log_count(summary)
        task = self.run_task("MediaPreviewBridgeMarkerStoreSweep")
        after = self.store_files()
        lines = self.log_lines(summary)[summaries_before:]
        self.bridge("DELETE", self.other)
        ok = (
            f"{orphan}.json" in before
            and f"{orphan}.json" not in after
            and f"{self.other}.json" in after
            and any("removed 1" in line for line in lines)
        )
        return ok, {
            "orphan_item": orphan,
            "store_files_before_sweep": before,
            "store_files_after_sweep": after,
            "task": {k: task.get(k) for k in ("Name", "State", "LastExecutionResult")},
            "log": [line.split("Media Preview Bridge: ", 1)[-1] for line in lines],
        }

    # ------------------------------------------------------------------------------ S01E02: other writers, failures

    def check_17_other_writers(self) -> tuple[bool, dict]:
        _, registration = self.call("GET", "/Registrations/intro-detection")
        previous = self.set_marker_detection(SYNTH_LIBRARY, True)
        try:
            self.plant_foreign_rows(self.other, FOREIGN_INTRO)
            steps = {"planted": self.marker_rows(self.chapters(self.other))}
            status, data = self.bridge("DELETE", self.other)
            steps["delete with nothing stored"] = {
                "status": status,
                "stored": data.get("Stored"),
                "rows": self.marker_rows(self.chapters(self.other)),
            }
            status, data = self.bridge("POST", self.other, {**E02_MARKERS, "FileSize": self.e02_size})
            steps["post, replaceOwn unset"] = {
                "status": status,
                "stored": data.get("Stored"),
                "rows": self.marker_rows(self.chapters(self.other)),
            }
            status, data = self.bridge("DELETE", self.other)
            steps["delete"] = {
                "status": status,
                "stored": data.get("Stored"),
                "rows": self.marker_rows(self.chapters(self.other)),
                "store_file_left": f"{self.other}.json" in self.store_files(),
            }
            body = {**E02_MARKERS, "FileSize": self.e02_size, "ReplaceOwn": True}
            status, data = self.bridge("POST", self.other, body)
            steps["post, ReplaceOwn true"] = {
                "status": status,
                "stored": data.get("Stored"),
                "rows": self.marker_rows(self.chapters(self.other)),
            }
            status, data = self.bridge("DELETE", self.other)
            chapters = self.chapters(self.other)
            steps["delete after replace"] = {
                "status": status,
                "stored": data.get("Stored"),
                "rows": self.marker_rows(chapters),
            }
        finally:
            self.set_marker_detection(SYNTH_LIBRARY, previous)
        ours_credits = [("CreditsStart", 100 * SEC)]
        ours_all = [("IntroStart", 17 * SEC), ("IntroEnd", 47 * SEC), ("CreditsStart", 100 * SEC)]
        ok = (
            steps["planted"] == FOREIGN_ROWS
            and steps["delete with nothing stored"]["rows"] == FOREIGN_ROWS
            and steps["post, replaceOwn unset"]["stored"] == 1
            and steps["post, replaceOwn unset"]["rows"] == sorted(FOREIGN_ROWS + ours_credits, key=lambda r: r[1])
            and steps["delete"]["rows"] == FOREIGN_ROWS
            and not steps["delete"]["store_file_left"]
            and steps["post, ReplaceOwn true"]["stored"] == 3
            and steps["post, ReplaceOwn true"]["rows"] == ours_all
            and steps["delete after replace"]["rows"] == []
            and len(self.plain(chapters)) == 4
        )
        return ok, {
            "emby_intro_detection_registration": registration,
            "marker_detection_was": previous,
            "steps": steps,
        }

    def check_19_write_failure(self) -> tuple[bool, dict]:
        first = {**E02_MARKERS, "FileSize": self.e02_size}
        self.bridge("POST", self.other, first)
        rows_before = self.marker_rows(self.chapters(self.other))
        directory = self.store_dir()
        self.docker("chmod", "555", directory)
        try:
            post_status, post = self.bridge("POST", self.other, {**INTRO_2, "FileSize": self.e02_size})
            rows_after_post = self.marker_rows(self.chapters(self.other))
            _, stored_after_post = self.bridge("GET", self.other)
            delete_status, deleted = self.bridge("DELETE", self.other)
            rows_after_delete = self.marker_rows(self.chapters(self.other))
            file_after_delete = f"{self.other}.json" in self.store_files()
        finally:
            self.docker("chmod", "755", directory)
        final_status, _ = self.bridge("DELETE", self.other)
        ok = (
            post_status == 500
            and post.get("Found") is True
            and bool(post.get("Error"))
            and rows_after_post == rows_before
            and stored_after_post.get("IntroStartTicks") == first["IntroStartTicks"]
            and delete_status == 500
            and bool(deleted.get("Error"))
            and rows_after_delete == rows_before
            and file_after_delete
            and final_status == 200
            and self.marker_rows(self.chapters(self.other)) == []
        )
        return ok, {
            "rows_before": rows_before,
            "post": {"status": post_status, "body": post, "rows_after": rows_after_post},
            "stored_after_failed_post": stored_after_post,
            "delete": {"status": delete_status, "body": deleted, "rows_after": rows_after_delete},
            "store_file_kept": file_after_delete,
            "delete_after_restoring_permissions": final_status,
        }

    # ------------------------------------------------------------------------------------ S01E01: a stopped write

    def replacing_store(self, markers: dict | None, replacing: dict) -> bytes:
        """The store file a POST (``markers``) or DELETE (None) leaves when Emby stops before it writes the rows."""
        body = {**markers, "FileSize": self.size, "Path": pm.synth_path(1)} if markers else {}
        return json.dumps({**body, "Replacing": replacing}, separators=(",", ":")).encode()

    def item_update(self, item_id: str) -> int:
        """A metadata edit: Emby raises ItemUpdated without touching the item's marker rows (check 16B)."""
        dto = self.ok("GET", f"/Users/{self.uid}/Items/{item_id}")
        return self.call("POST", f"/Items/{item_id}", dto)[0]

    def wait_rows(self, item_id: str, rows: list[tuple]) -> list[tuple]:
        try:
            pm.wait_until("marker rows", lambda: self.marker_rows(self.chapters(item_id)) == rows, 60, every=2)
        except TimeoutError:
            pass
        return self.marker_rows(self.chapters(item_id))

    def check_21_stopped_write(self) -> tuple[bool, dict]:
        # The store is saved (with the rows it replaces) before the chapter rows. Each step plants the store file such a
        # save leaves while the rows are still the earlier set, as if Emby stopped in between (publishers audit LOW-3).
        name = f"{self.item}.json"
        old = {**INTRO, "FileSize": self.size}
        steps: dict[str, dict] = {}

        self.bridge("POST", self.item, old)
        self.write_store_file(name, self.replacing_store(INTRO_2, INTRO))
        _, got = self.bridge("GET", self.item)
        status, data = self.bridge("POST", self.item, {**INTRO_2, "FileSize": self.size})
        stored = self.read_store_file(self.item)
        steps["POST after a stopped POST"] = {
            "get_before": got,
            "status": status,
            "stored": data.get("Stored"),
            "rows": self.marker_rows(self.chapters(self.item)),
            "store_file": stored,
        }

        self.bridge("POST", self.item, old)
        self.write_store_file(name, self.replacing_store(INTRO_2, INTRO))
        status, data = self.bridge("DELETE", self.item)
        steps["DELETE after a stopped POST"] = {
            "status": status,
            "rows": self.marker_rows(self.chapters(self.item)),
            "store_file_left": name in self.store_files(),
        }

        finished = f"finished an interrupted marker write for item {self.item}"
        before = self.log_count(finished)
        self.bridge("POST", self.item, old)
        self.write_store_file(name, self.replacing_store(INTRO_2, INTRO))
        edit = self.item_update(self.item)
        rows = self.wait_rows(self.item, ROWS_5)
        steps["item update after a stopped POST"] = {
            "metadata_edit_status": edit,
            "rows": rows,
            "store_file": self.read_store_file(self.item) if name in self.store_files() else None,
            "finished_logged": self.wait_log(finished, before, timeout=30),
        }

        before = self.log_count(finished)
        self.write_store_file(name, self.replacing_store(None, INTRO_2))
        _, got = self.bridge("GET", self.item)
        edit = self.item_update(self.item)
        rows = self.wait_rows(self.item, [])
        steps["item update after a stopped DELETE"] = {
            "get_before": got,
            "metadata_edit_status": edit,
            "rows": rows,
            "store_file_left": name in self.store_files(),
            "finished_logged": self.wait_log(finished, before, timeout=30),
        }
        chapters = self.chapters(self.item)
        self.bridge("DELETE", self.item)

        post, delete = steps["POST after a stopped POST"], steps["DELETE after a stopped POST"]
        healed, deleted = steps["item update after a stopped POST"], steps["item update after a stopped DELETE"]
        ok = (
            post["get_before"].get("IntroStartTicks") == INTRO_2["IntroStartTicks"]
            and post["get_before"].get("CreditsStartTicks") is None
            and post["status"] == 200
            and post["stored"] == 2
            and post["rows"] == ROWS_5
            and "Replacing" not in post["store_file"]
            and delete["status"] == 200
            and delete["rows"] == []
            and not delete["store_file_left"]
            and healed["metadata_edit_status"] < 300
            and healed["rows"] == ROWS_5
            and healed["store_file"] is not None
            and "Replacing" not in healed["store_file"]
            and healed["store_file"].get("IntroStartTicks") == INTRO_2["IntroStartTicks"]
            and healed["finished_logged"]
            and deleted["get_before"].get("IntroStartTicks") is None
            and deleted["rows"] == []
            and not deleted["store_file_left"]
            and deleted["finished_logged"]
            and self.plain(chapters) == self.originals
        )
        return ok, {"steps": steps}

    # ------------------------------------------------------------------------------------------------------- run

    def cleanup(self) -> dict:
        """Leave the server as it was: no markers, no copy library, store folder writable."""
        done = {}
        directory = self.store_dir()
        self.docker("chmod", "755", directory, check=False)
        for item_id in (self.item, self.other):
            if item_id:
                done[f"delete_markers_{item_id}"] = self.bridge("DELETE", item_id)[0]
        library = self.library(COPY_LIBRARY)
        if library:
            # Emby 4.9 removes a library by Id only (by name alone it answers 500).
            query = urllib.parse.urlencode({"Id": library["ItemId"], "name": COPY_LIBRARY, "refreshLibrary": "false"})
            done["remove_copy_library"] = self.call("DELETE", f"/Library/VirtualFolders?{query}")[0]
        self.docker("rm", "-rf", COPY_ROOT)
        if self.store_files():
            # Removing a library raises no ItemRemoved for its episodes; the sweep deletes what they left.
            self.run_task("MediaPreviewBridgeMarkerStoreSweep")
        done["store_files_left"] = self.store_files()
        return done

    def run(self, only: set[int] | None = None) -> None:
        pm.say(f"== {self.container} ({self.url})")
        self.item = self.find_episode(pm.synth_path(1))
        self.other = self.find_episode(pm.synth_path(2))
        if not self.item or not self.other:
            raise RuntimeError("Synth Chapters S01E01/S01E02 not found")
        self.originals = self.plain(self.chapters(self.item))
        if not self.originals:
            raise RuntimeError("S01E01 has no plain chapters to protect")
        checks = [
            (1, "Ping without a token", self.check_1_ping),
            (2, "Unknown id with the admin key", self.check_2_not_found),
            (3, "No token and non-admin token are refused", self.check_3_auth),
            (4, "POST intro 10-40 s, credits 100 s", self.check_4_post),
        ]
        if self.web_client:
            checks.append((12, "Emby web shows Skip Intro at 0:22", self.check_12_web_skip))
        checks += [
            (5, "POST intro 12-42 s, no credits", self.check_5_replace),
            (6, "Invalid bodies store nothing", self.check_6_invalid),
            (7, "Replace all / Search for missing metadata: markers written back", self.check_7_full_refresh),
            (8, "Default, ValidationOnly, scan, restart keep markers; start-up clears .tmp", self.check_8_survives),
        ]
        if self.web_client:
            checks.append((13, "DELETE: Emby web shows no Skip Intro at 0:22", self.check_13_delete_no_skip))
        checks += [
            (14, "Corrupt store file reads as empty, logged once, never written back", self.check_14_corrupt_store),
            (15, "Stored path differs from the item's: stale, not written back", self.check_15_path_mismatch),
            (
                20,
                "Every cut of a store file: GET empty JSON; cut inside Path: DELETE/POST JSON, POST repairs",
                self.check_20_cut_path_api,
            ),
            (9, "Stale FileSize: stored, not shown, not written back", self.check_9_stale),
            (10, "DELETE clears rows and the store file", self.check_10_delete),
            (
                16,
                "File replaced at the same path: no rows of ours shown, none written back",
                self.check_16_replaced_file,
            ),
            (11, "Removed item: store file deleted", self.check_11_removed),
            (
                17,
                "Rows the plugin didn't write: kept, replaced only with ReplaceOwn, DELETE leaves them",
                self.check_17_other_writers,
            ),
            (18, "Sweep task deletes the store file of a removed show's episode", self.check_18_sweep),
            (19, "Store write failure: 500 JSON, rows and store unchanged", self.check_19_write_failure),
            (
                21,
                "Stopped write (store saved with the rows it replaces, rows not written): POST, DELETE and an item "
                "update take both sets for ours",
                self.check_21_stopped_write,
            ),
        ]
        for number, title, fn in checks:
            if only is not None and number not in only:
                continue
            if number in (10, 16, 11) and not self.copy:
                continue
            self.run_check(number, title, fn)
        cleanup = self.cleanup()
        body = {
            "container": self.container,
            "at": pm.now_iso(),
            "server": self.ok("GET", "/System/Info/Public"),
            "items": {"S01E01": self.item, "S01E02": self.other},
            "sizes": {"S01E01": self.size, "S01E02": self.e02_size},
            "original_chapters": self.originals,
            "checks": sorted(self.results, key=lambda r: r["check"]),
            "cleanup": cleanup,
        }
        pm.RESULTS.mkdir(exist_ok=True)
        path = pm.RESULTS / f"emby-plugin-{self.container}.json"
        if only is not None and path.exists():
            # Merged: the checks run now replace their earlier results.
            earlier = [r for r in json.loads(path.read_text())["checks"] if r["check"] not in only]
            body["checks"] = sorted([*earlier, *body["checks"]], key=lambda r: r["check"])
        path.write_text(json.dumps(self.scrub(body), indent=2, default=str) + "\n")
        failed = [r["check"] for r in self.results if r["result"] != "PASS"]
        summary = f"{len(self.results) - len(failed)}/{len(self.results)} passed" + (
            f", failed {failed}" if failed else ""
        )
        pm.say(f"{self.container}: {summary} -> {path}")


def main(argv: list[str]) -> int:
    only = None
    if "--checks" in argv:
        at = argv.index("--checks")
        only = {int(n) for n in argv[at + 1].split(",")}
        argv = argv[:at] + argv[at + 2 :]
    containers = argv or list(SERVERS)
    unknown = [c for c in containers if c not in SERVERS]
    if unknown:
        print(f"unknown container(s): {unknown}; choose from {list(SERVERS)}", file=sys.stderr)
        return 2
    for container in containers:
        EmbyCheck(container).run(only)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
