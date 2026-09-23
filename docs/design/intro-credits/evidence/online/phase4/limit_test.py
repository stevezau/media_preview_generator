"""The one deliberate rate-limit boundary test: cross AniSkip's documented 120 GET/min, once.

Every other script here stays under the limit. This one crosses it to record the 429's shape, stops
at the first 429, and is hard-capped at 140 requests and one 60-second window either way.
"""

from __future__ import annotations

import time

import requests

URL = "https://api.aniskip.com/v2/skip-times/21/1"
PARAMS = [("types[]", "op"), ("episodeLength", 1440)]
USER_AGENT = "MediaPreviewGenerator-research/0.1 (intro-credits source evaluation; rate-limit check)"
MAX_REQUESTS = 140  # the documented limit is 120 per minute
WINDOW_S = 58


def main() -> None:
    """Send up to 140 requests in under a minute and report the first 429, or that none came."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    started = time.monotonic()
    for i in range(1, MAX_REQUESTS + 1):
        resp = session.get(URL, params=PARAMS, timeout=15)
        elapsed = time.monotonic() - started
        if resp.status_code == 429:
            limit_headers = {
                k: v for k, v in resp.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))
            }
            print(f"429 after {i} requests in {elapsed:.1f} s\nheaders: {limit_headers}\nbody: {resp.text[:300]}")
            return
        if i % 20 == 0:
            print(
                f"  {i} requests, {elapsed:.1f} s, status {resp.status_code}, "
                f"remaining {resp.headers.get('x-ratelimit-remaining')}, "
                f"reset {resp.headers.get('x-ratelimit-reset')}"
            )
        if elapsed > WINDOW_S:
            print(f"stopped at {i} requests: the 60 s window is nearly over and no 429 came")
            return
    print(f"{MAX_REQUESTS} requests in {time.monotonic() - started:.1f} s with no 429")


if __name__ == "__main__":
    main()
