"""Write fix3_season_across_disks.patch: the worktree now (speed work + fixes 1 and 2) -> the snapshot with fix 3."""

import difflib
import os

WT = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
HERE = os.path.dirname(os.path.abspath(__file__))
WITH = os.path.join(HERE, "fix3_with")
OUT = os.path.join(HERE, "fix3_season_across_disks.patch")

parts = []
for dirpath, _dirs, names in sorted(os.walk(WITH)):
    for name in sorted(names):
        after_path = os.path.join(dirpath, name)
        rel = os.path.relpath(after_path, WITH)
        before_path = os.path.join(WT, rel)
        after = open(after_path, encoding="utf-8").read().splitlines(keepends=True)
        exists = os.path.exists(before_path)
        before = open(before_path, encoding="utf-8").read().splitlines(keepends=True) if exists else []
        if before == after:
            continue
        head = f"diff --git a/{rel} b/{rel}\n"
        if not exists:
            head += "new file mode 100644\n"
        diff = difflib.unified_diff(before, after, "a/" + rel if exists else "/dev/null", "b/" + rel, n=3)
        parts.append(head + "".join(diff))
open(OUT, "w", encoding="utf-8").write("".join(parts))
print(OUT, len(parts), "files")
