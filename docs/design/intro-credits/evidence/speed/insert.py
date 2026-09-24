"""Insert a text block before an anchor line in a file (one exact anchor)."""

import sys
from pathlib import Path

target, block_file, anchor = sys.argv[1], sys.argv[2], sys.argv[3]
text = Path(target).read_text()
block = Path(block_file).read_text()
anchor = anchor.encode().decode("unicode_escape")
assert text.count(anchor) == 1, f"anchor found {text.count(anchor)} times"
Path(target).write_text(text.replace(anchor, block + anchor, 1))
print("inserted", len(block.splitlines()), "lines")
