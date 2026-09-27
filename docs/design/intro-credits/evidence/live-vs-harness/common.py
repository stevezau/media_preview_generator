"""Shared paths for the 2026-09-28 live-vs-harness lane (evidence, not shipped).

Local-only data (gitignored, it names library files) sits in ``local/``: ``after/`` the 2026-09-28 audit of sflix
(``work/`` verdicts and samples, ``frames/`` its contact sheets, ``db/`` its markers.db copy with ``pre-320/`` and
``pre-323/`` snapshots), ``baseline/work`` the 2026-09-27 audit's verdicts, ``replay/`` replay outputs,
``trees/82dc2bc`` an export of the shipped tree. The tree under test is ``$CODE`` (default this repo).
"""

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL = HERE / "local"
AFTER = LOCAL / "after"
DB_NOW = AFTER / "db" / "markers.db"
DB_PRE323 = AFTER / "db" / "pre-323" / "markers.db"
DB_PRE320 = AFTER / "db" / "pre-320" / "markers.db"
CODE = os.environ.get("CODE", str(HERE.parents[4]))
