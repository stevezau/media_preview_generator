"""Season audio answers for Spring of the Blade S01 (spring_st.json) as a replay injection, and sheets of the three
intro chapters the new rule moves (their ends, old vs new)."""

import json
import subprocess
import sys

from common import HERE

d = json.load(open(HERE / "spring_st.json"))["details"]
out = {p: (v["answer"][:2] if v["answer"] else None) for p, v in d.items()}
json.dump(out, open(HERE / "audio_inject.json", "w"), indent=0)
chapters = {"S01E02": 111.0, "S01E03": 141.0, "S01E14": 134.0}
for path, answer in out.items():
    for code, chapter_end in chapters.items():
        if f" - {code} - " in path and answer:
            subprocess.run(
                [sys.executable, str(HERE / "sheets.py"), str(HERE / "sheets_intro"), path, "intro",
                 str(chapter_end), str(answer[1]), f"spring_{code}_end"],
                check=True,
            )  # fmt: skip
            subprocess.run(
                [sys.executable, str(HERE / "sheets.py"), str(HERE / "sheets_intro"), path, "intro", "0",
                 "12", f"spring_{code}_start"],
                check=True,
            )  # fmt: skip
print(len(out))
