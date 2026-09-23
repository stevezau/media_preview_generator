"""Ask AniSkip once per resolved episode, paced below the documented limit, caching every answer.

One GET per episode for all five segment types. Stops on the first 429 and reports it. Re-running
skips the episodes already in ``aniskip_sweep.jsonl``, so an interrupted run resumes.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE / "aniskip_sweep.jsonl"
BASE_URL = "https://api.aniskip.com/v2/skip-times"
USER_AGENT = "MediaPreviewGenerator-research/0.1 (intro-credits source evaluation)"
TYPES = ("op", "ed", "mixed-op", "mixed-ed", "recap")
# The service's own code allows 120 GET/min per IP; its rate-limit headers never decrement, so pace from this.
PACE_S = 0.70


def already_asked() -> set[str]:
    """Files already in the output, so a re-run resumes instead of re-asking."""
    if not OUT.exists():
        return set()
    done = set()
    for line in OUT.open():
        try:
            done.add(json.loads(line)["file"])
        except (ValueError, KeyError):
            continue
    return done


def main() -> None:
    """Sweep every resolved episode."""
    rows = json.loads((HERE / "resolved.json").read_text())
    done = already_asked()
    todo = [r for r in rows if r["file"] not in done]
    print(f"{len(rows)} resolved episodes, {len(done)} already asked, {len(todo)} to go", flush=True)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    type_params = [("types[]", t) for t in TYPES]

    with OUT.open("a") as fh:
        for i, row in enumerate(todo, 1):
            duration_ms = row.get("duration")
            # episodeLength=0 turns the cut check off entirely, so a file with no duration is never asked.
            if not duration_ms:
                continue
            params = [*type_params, ("episodeLength", round(duration_ms / 1000, 3))]
            try:
                resp = session.get(f"{BASE_URL}/{row['mal']}/{row['mal_ep']}", params=params, timeout=20)
            except requests.RequestException as exc:
                fh.write(json.dumps({"file": row["file"], "error": str(exc)[:120]}) + "\n")
                time.sleep(2)
                continue
            if resp.status_code == 429:
                print(f"429 after {i} requests — stopping. headers={dict(resp.headers)} body={resp.text[:300]}")
                sys.exit(2)
            body = resp.json() if "json" in resp.headers.get("content-type", "") else None
            fh.write(
                json.dumps(
                    {
                        "file": row["file"],
                        "show": row["show"],
                        "season": row["season"],
                        "episode": row["episode"],
                        "mal": row["mal"],
                        "mal_ep": row["mal_ep"],
                        "how": row["how"],
                        "duration_ms": duration_ms,
                        "status": resp.status_code,
                        "body": body,
                        "ratelimit": {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")},
                    }
                )
                + "\n"
            )
            if i % 100 == 0:
                fh.flush()
                print(f"  {i}/{len(todo)}", flush=True)
            time.sleep(PACE_S)
    print("sweep done", flush=True)


if __name__ == "__main__":
    main()
