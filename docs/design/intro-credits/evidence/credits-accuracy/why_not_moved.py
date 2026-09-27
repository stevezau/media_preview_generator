"""Why a credits chapter didn't move: the chapter, the work tree's credit text answer and chapter_moves_to's inputs.

Usage: why_not_moved.py <replay_work.json> <id> [<id> ...]
"""

import json
import sys

sys.path.insert(0, __import__("os").environ.get("CREDFIX_WORK", str(__import__("pathlib").Path(__file__).resolve().parents[5])))
from media_preview_generator.markers.credits import rule_j  # noqa: E402
from tools.markers_eval.decode_cache import rows_from_json  # noqa: E402

from common import HERE  # noqa: E402

work = json.load(open(sys.argv[1]))
items = json.load(open(HERE / "items.json"))
texts = json.load(open(HERE / "vtext_work.json"))
by_id = {it["id"]: it for it in items["verdict"] + items["plex"]}
for ident in sys.argv[2:]:
    it = by_id[ident]
    row = work[f"{it['fid']}:credits"]
    r = texts.get(it["path"])
    print(ident, it["path"].split("/")[-1][:60], "truth", it["truth"]["start"], "decided", row["marker"], row["reason"])
    print("   stored", row["stored_marker"], row["stored_reason"])
    if not r:
        print("   no credit text answer")
        continue
    start = r["start_s"]
    rows = rule_j.without_overlays(rows_from_json(r["key"]), [tuple(b) for b in r["overlays"]])
    chapter = row["stored_marker"][0] / 1000 if row["stored_marker"] else None
    print("   text start", start, "end", r["end_s"], "chapter", chapter,
          "moves", None if chapter is None or start is None else rule_j.chapter_moves_to(rows, start, chapter))  # fmt: skip
    if chapter is not None and start is not None:
        lo, hi = min(start, chapter) - 3, max(start, chapter) + 3
        for x in sorted(rows, key=lambda x: x[0]):
            if lo <= x[0] <= hi:
                print(f"      {x[0]:9.2f} n={x[1]:2d} luma={x[2]:5.1f}")
