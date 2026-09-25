"""The shipped package never points at a developer's private files (plans, notes, home directories).

Such a path means nothing on a user's install, and a reference to it reads as documentation the code doesn't have.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "media_preview_generator"
# A developer's own tree: Claude Code plans/notes, or a concrete home directory (``/home/<user>`` as a placeholder in a
# comment is fine).
PRIVATE = re.compile(r"\.claude/(plans|projects|worktrees)|/home/(?!<)[A-Za-z0-9_.-]+/")


def test_no_package_source_references_a_private_path():
    hits = [
        f"{path.relative_to(PACKAGE.parent)}:{number}: {line.strip()}"
        for path in sorted(PACKAGE.rglob("*"))
        if path.suffix in {".py", ".js", ".html", ".css", ".json"} and path.is_file()
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1)
        if PRIVATE.search(line)
    ]
    assert hits == []
