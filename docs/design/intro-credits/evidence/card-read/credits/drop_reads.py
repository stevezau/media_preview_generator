"""Scratch: drop the decode cache's stored card reads (entries holding only "text"), so a reader change reads again.

Only ~/.cache/markers_eval/credits_decodes entries are touched; decoded rows and probes stay.
"""

import json
from pathlib import Path

root = Path.home() / ".cache/markers_eval/credits_decodes"
assert str(root).startswith("/home/data/.cache/"), root
dropped = kept = 0
for entry in root.glob("*.json"):
    try:
        data = json.loads(entry.read_text())
    except (OSError, ValueError):
        continue
    if isinstance(data, dict) and set(data) == {"text"}:
        entry.unlink()
        dropped += 1
    else:
        kept += 1
print("dropped", dropped, "kept", kept)
