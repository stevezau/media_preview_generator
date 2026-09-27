"""The files a tree's version re-run lists on a markers.db copy (usage: CODE=tree due_diff.py <copy>)."""

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODE  # noqa: E402

sys.path.insert(0, CODE)
from media_preview_generator.markers import versions
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.store import MarkerStore

store = MarkerStore(sys.argv[1])  # a copy of a markers.db
srcs = [
    {"id": s, "enabled": True}
    for s in ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers")
]
settings = load_global(
    validate_global({"detect": {"intro": True, "credits": True, "recap": False}, "sources": srcs}, None)[0]
)
due = versions.files_to_read_again(store, settings)
c = collections.Counter(k for v in due.values() for k in v)
print(len(due), dict(c))
for p in due:
    if "10 Things I Hate" in p or "Beautiful Imperfection" in p:
        print("  ", p[-80:], due[p])
