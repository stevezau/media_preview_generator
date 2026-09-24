"""Copy the worktree (without fix 3) to scratchpad/fix3_check, apply fix3_season_across_disks.patch there, and run the
markers tests that fix 3 touches, to prove the patch is complete on its own."""

import os
import shutil
import subprocess

WT = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "fix3_check")
if os.path.exists(DEST):
    shutil.rmtree(DEST)
ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "_site", ".jekyll-cache", "node_modules")
os.makedirs(DEST)
for name in ("media_preview_generator", "tests", "tools", "docs", "scripts"):
    shutil.copytree(os.path.join(WT, name), os.path.join(DEST, name), ignore=ignore, symlinks=True)
for name in ("pyproject.toml", "conftest.py", "pytest.ini", "setup.cfg"):
    if os.path.exists(os.path.join(WT, name)):
        shutil.copy2(os.path.join(WT, name), DEST)
subprocess.run(["git", "apply", os.path.join(HERE, "fix3_season_across_disks.patch")], cwd=DEST, check=True)
tests = [
    "tests/markers/audio/test_season_folders.py",
    "tests/markers/audio/test_season.py",
    "tests/markers/audio/test_season_guards.py",
    "tests/markers/test_inspect.py",
    "tests/markers/test_triggers.py",
    "tests/markers/test_season_followups.py",
    "tests/markers/test_missing.py",
]
proc = subprocess.run(
    [
        "nice",
        "-n",
        "19",
        "/home/data/.venv/bin/python",
        "-m",
        "pytest",
        "--no-cov",
        "-n",
        "8",
        "-q",
        "-p",
        "no:cacheprovider",
        *tests,
    ],
    cwd=DEST,
    capture_output=True,
    text=True,
)
lines = [
    ln for ln in proc.stdout.splitlines() if ln.startswith(("FAILED", "ERROR")) or " passed" in ln or " failed" in ln
]
print("\n".join(lines[-12:]))
import_check = subprocess.run(
    [
        "/home/data/.venv/bin/python",
        "-c",
        "import media_preview_generator.markers.audio.season as s; print(s.__file__, hasattr(s, 'season_folders'))",
    ],
    cwd=DEST,
    capture_output=True,
    text=True,
)
print(import_check.stdout.strip(), import_check.stderr.strip()[-300:])
