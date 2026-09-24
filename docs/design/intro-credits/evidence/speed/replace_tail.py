"""Replace everything from an anchor line to the end of a file with a block (one exact anchor)."""

import sys
from pathlib import Path

target, block_file, anchor = sys.argv[1], sys.argv[2], sys.argv[3]
text = Path(target).read_text()
assert text.count(anchor) == 1, f"anchor found {text.count(anchor)} times"
Path(target).write_text(text[: text.index(anchor)] + Path(block_file).read_text())
print("replaced from", anchor)
