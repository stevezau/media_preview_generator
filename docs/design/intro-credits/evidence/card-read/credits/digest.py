"""Scratch: a tree's decode digest (the harness's decode cache key part). Usage: digest.py <tree>"""

import os
import sys

tree = sys.argv[1]
sys.path.insert(0, tree)
os.chdir(tree)
from tools.markers_eval.credits_text import decode_digest  # noqa: E402

print(decode_digest())
