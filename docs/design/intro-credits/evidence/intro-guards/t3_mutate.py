"""Apply one mutation (from a JSON spec: path, old, new, tests) to a source file, run the tests, and restore the file
whatever happens."""

import json
import subprocess
import sys

spec = json.load(open(sys.argv[1]))
path, old, new, tests = spec["path"], spec["old"], spec["new"], spec["tests"]
original = open(path).read()
assert original.count(old) == 1, "mutation site not found once"
try:
    open(path, "w").write(original.replace(old, new))
    result = subprocess.run(
        ["/home/data/.venv/bin/python", "-m", "pytest", "--no-cov", "-n", "0", "-q", *tests],
        capture_output=True,
        text=True,
    )
    lines = [
        ln
        for ln in result.stdout.splitlines()
        if ln.startswith(("FAILED", "ERROR")) or " passed" in ln or " failed" in ln
    ]
    print("\n".join(lines[-15:]))
finally:
    open(path, "w").write(original)
