"""Stand-in for the text detection helper process: the same protocol, boxes = bright pixels // 100 (at most 9).

    python fake_textdet_helper.py --backend cpu|webgpu --mode MODE [--idle-exit-s S] [--no-selftest]

Modes: ok, selftest-cpu, crash-on-request, crash-after-reply, idle-exit-on-request, idle-exit-slow-shutdown
(closes its pipes on a request, then takes 0.5 s to exit 75, so the answer stream ends before poll() has a code),
hang-on-request, hang-start, bad-ready, hang-on-exit, error-reply, hold-stdin (never reads; a process in its own
session keeps stdin open after a kill, its pid written to $FAKE_HOLDER_PID_FILE), hang-with-child (hangs on a request
after starting a child in its own process group, as a driver's worker process would be; the child's pid written to
$FAKE_CHILD_PID_FILE).
"""

import argparse
import json
import os
import select
import subprocess
import sys
import time

import numpy as np


def send(out, message):
    out.write((json.dumps(message) + "\n").encode())
    out.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="cpu")
    parser.add_argument("--mode", default="ok")
    parser.add_argument("--idle-exit-s", type=float, default=600.0)
    parser.add_argument("--no-selftest", action="store_true")
    args, _ = parser.parse_known_args()
    out = sys.stdout.buffer
    if args.mode == "hang-start":
        time.sleep(3600)
    if args.mode == "bad-ready":
        out.write(b"hello\n")
        out.flush()
        time.sleep(3600)
    webgpu = args.backend == "webgpu"
    backend = "cpu" if args.mode == "selftest-cpu" else args.backend
    selftest = (
        None
        if (not webgpu or args.no_selftest)
        else {
            "gpu_ms": 20.0 if backend == "cpu" else 9.0,
            "cpu_ms": 18.0,
            "ratio": 1.1111 if backend == "cpu" else 0.5,
            "same_boxes": True,
        }
    )
    send(
        out,
        {
            "ready": True,
            "backend": backend,
            "selftest": selftest,
            "reason": "the GPU wasn't at least 10% faster than the CPU "
            "(median 20.0 vs 18.0 ms per frame; GPU/CPU 1.1111 per round)"
            if backend != args.backend
            else "",
        },
    )
    stdin = sys.stdin.buffer
    if args.mode == "hold-stdin":
        holder = subprocess.Popen(["sleep", "60"], start_new_session=True)
        with open(os.environ["FAKE_HOLDER_PID_FILE"], "w") as fh:
            fh.write(str(holder.pid))
        time.sleep(3600)
    if args.mode == "hang-with-child":
        child = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        with open(os.environ["FAKE_CHILD_PID_FILE"], "w") as fh:
            fh.write(str(child.pid))
    while True:
        readable, _, _ = select.select([stdin], [], [], args.idle_exit_s)
        if not readable:
            return 75
        header = stdin.readline()
        if header and args.mode == "idle-exit-on-request":
            os._exit(75)  # its idle timer fired just as the request arrived
        if not header:
            if args.mode == "hang-on-exit":
                time.sleep(3600)
            return 0
        request = json.loads(header)
        size = request["frames"] * request["height"] * request["width"]
        planes = np.frombuffer(stdin.read(size), np.uint8).reshape(
            request["frames"], request["height"], request["width"]
        )
        if args.mode == "crash-on-request":
            os._exit(9)
        if args.mode == "idle-exit-slow-shutdown":
            # Its idle timer fired and it is on its way out: its pipes are gone (the parent sees the answer stream end
            # at once) while the process itself takes a while to leave, as a real one does with a session to tear down.
            os.close(1)
            os.close(0)
            time.sleep(0.5)
            os._exit(75)
        if args.mode in ("hang-on-request", "hang-with-child"):
            time.sleep(3600)
        if args.mode == "error-reply":
            send(out, {"id": request["id"], "error": "boom"})
            continue
        send(out, {"id": request["id"], "boxes": [min(9, int((p > 200).sum()) // 100) for p in planes]})
        if args.mode == "crash-after-reply":
            os._exit(9)


if __name__ == "__main__":
    sys.exit(main())
