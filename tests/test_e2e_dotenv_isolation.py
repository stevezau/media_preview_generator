"""The e2e app subprocess must not read a developer's repo-root ``.env``.

Three e2e journey tests failed locally while passing in CI for months.
Cause: ``tests/conftest.py::_isolate_dotenv_from_tests`` neuters
``load_dotenv`` inside the pytest process, but the e2e app runs in a
*subprocess* and escaped it — so a real ``PLEX_URL``/``PLEX_TOKEN`` reached
``load_config()``, config validation started passing instead of failing,
and jobs the tests expect to fail fast became 60s retry chains.

The symptom was pure timing, never an error, which is why it read as
flakiness. This test pins the mechanism instead of the symptom.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.e2e.conftest import app_boot_payload

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_boot_payload_disables_dotenv() -> None:
    """The payload must make ``dotenv.load_dotenv`` a no-op before app import."""
    payload = app_boot_payload(1234)
    assert "dotenv.load_dotenv = lambda" in payload
    # The patch must precede the app import, or config binds the real one.
    assert payload.index("dotenv.load_dotenv") < payload.index("media_preview_generator")


def test_dotenv_is_inert_in_a_subprocess_run_from_the_repo_root(tmp_path) -> None:
    """End-to-end proof: a ``.env`` in the cwd must not reach ``os.environ``.

    Runs the payload's prefix for real, from a directory containing a
    ``.env``, and asserts the variable never lands. Without the patch
    python-dotenv's ``find_dotenv()`` falls back to ``os.getcwd()`` (a
    ``python -c`` ``__main__`` has no ``__file__``) and this would be set.
    """
    (tmp_path / ".env").write_text("MPG_DOTENV_CANARY=leaked\n")

    prefix = app_boot_payload(1234).split("from media_preview_generator")[0]
    probe = prefix + "import os, dotenv; dotenv.load_dotenv(); print(os.environ.get('MPG_DOTENV_CANARY'))"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == "None", (
        f"a .env in the cwd reached the subprocess despite the patch: {result.stdout!r}"
    )

    # Control: without the patch the same probe DOES pick the .env up —
    # proving this test would catch a regression rather than passing for
    # an unrelated reason (e.g. python-dotenv not installed).
    unpatched = subprocess.run(
        [sys.executable, "-c", "import os, dotenv; dotenv.load_dotenv(); print(os.environ.get('MPG_DOTENV_CANARY'))"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert unpatched.stdout.strip() == "leaked", (
        "control probe did not pick up the .env — this test cannot prove the patch works"
    )


def test_every_e2e_app_launcher_uses_the_shared_payload() -> None:
    """No launcher may hand-roll the boot string and skip the dotenv patch.

    ``_start_app`` was fixed first while three other launchers still built
    their own ``python -c`` string; each one silently reopened the leak.
    """
    offenders = []
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "web.app import run_server" in line and "dotenv" not in text.split(line)[0][-400:]:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()[:80]}")
    assert not offenders, "app launchers that bypass the dotenv patch:\n" + "\n".join(offenders)
