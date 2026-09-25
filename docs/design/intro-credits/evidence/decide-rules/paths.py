"""Where the decide-rules scripts find things.

The local-only inputs and outputs (they name real library files) live in the main checkout's evidence folder on
storage, as every other topic's do (``../README.md``); ``REPO`` is the tree measured as "work", ``LOCAL/base`` the one
measured as "base" (``dev`` at 40311c3).
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
EVIDENCE_DIR = Path("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence")
LOCAL = EVIDENCE_DIR / "decide-rules" / "local"
