"""Verification only: the worktree's own credits frame extraction on one file through one vendor.

Usage: verify.py VENDOR TAG PATH OUTDIR [--episode]
VENDOR: vaapi (Intel UHD 770, /dev/dri/renderD128), cuda (TITAN RTX, cuda:0), cpu.

Three phases, all through the new code's entry points (frames.keyframe_thinning, frames.container_start_s,
frames.decode_rows, detector.find_credits):
  tail1  the tail's keyframes at 320x180 (scale=1); every Y plane written to OUTDIR/TAG_VENDOR_tail1.u8
  tail2  the same at 640x360 (scale=2); OUTDIR/TAG_VENDOR_tail2.u8
  find   detector.find_credits as the app runs it (the 640x360 re-read only when 320x180 finds nothing)
Text detection is the same in-process CPU ONNX session for every vendor, so boxes differ only if pixels differ.
Memory: a sampler polls this process and its ffmpeg children (VmRSS, VmHWM, i915/xe DRM fdinfo) every 50 ms.
"""

import argparse
import hashlib
import json
import os
import resource
import threading
import time

from media_preview_generator.markers.credits import detector, frames, textdet
from media_preview_generator.markers.decide import credits_limits_ms, earliest_credits_start_ms
from media_preview_generator.markers.probe import probe_media

FFMPEG = "/usr/local/bin/ffmpeg"
FFPROBE = "/usr/local/bin/ffprobe"
MODEL = "/app/models/ch_PP-OCRv4_det_infer.onnx"
VENDORS = {"vaapi": ("INTEL", "/dev/dri/renderD128"), "cuda": ("NVIDIA", "cuda:0"), "cpu": (None, None)}


def _kib(line: str) -> int:
    parts = line.split()
    value = int(parts[1])
    unit = parts[2] if len(parts) > 2 else "B"
    return {"B": value // 1024, "KiB": value, "kB": value, "MiB": value * 1024, "GiB": value * 1024 * 1024}[unit]


class Sampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.pid = os.getpid()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.phase = "setup"
        self.peaks: dict[str, dict[str, int]] = {}

    def set_phase(self, phase: str) -> None:
        with self.lock:
            self.phase = phase

    def _children(self) -> list[int]:
        out = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as fh:
                    stat = fh.read()
            except OSError:
                continue
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            if ppid == self.pid:
                out.append(int(entry))
        return out

    @staticmethod
    def _status(pid: int) -> dict[str, int]:
        found = {}
        try:
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith(("VmRSS:", "VmHWM:")):
                        found[line.split(":")[0]] = _kib(line)
        except OSError:
            pass
        return found

    @staticmethod
    def _drm(pid: int) -> dict[str, int]:
        totals: dict[str, int] = {}
        seen_clients = set()
        try:
            fds = os.listdir(f"/proc/{pid}/fdinfo")
        except OSError:
            return totals
        for fd in fds:
            try:
                with open(f"/proc/{pid}/fdinfo/{fd}") as fh:
                    text = fh.read()
            except OSError:
                continue
            if "drm-driver:" not in text:
                continue
            lines = text.splitlines()
            client = next((ln.split()[1] for ln in lines if ln.startswith("drm-client-id:")), fd)
            driver = next((ln.split()[1] for ln in lines if ln.startswith("drm-driver:")), "?")
            if (driver, client) in seen_clients:
                continue
            seen_clients.add((driver, client))
            for ln in lines:
                if ln.startswith(("drm-total-", "drm-resident-")):
                    key = f"{driver}:{ln.split(':')[0]}"
                    totals[key] = totals.get(key, 0) + _kib(ln)
        return totals

    def run(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                phase = self.phase
            sample: dict[str, int] = {}
            me = self._status(self.pid)
            sample["py_rss_kib"] = me.get("VmRSS", 0)
            ff_rss = ff_hwm = 0
            for child in self._children():
                st = self._status(child)
                ff_rss += st.get("VmRSS", 0)
                ff_hwm = max(ff_hwm, st.get("VmHWM", 0))
                for key, value in self._drm(child).items():
                    sample[f"ffmpeg_{key}"] = sample.get(f"ffmpeg_{key}", 0) + value
            sample["ffmpeg_rss_kib"] = ff_rss
            sample["ffmpeg_hwm_kib"] = ff_hwm
            peaks = self.peaks.setdefault(phase, {})
            for key, value in sample.items():
                peaks[key] = max(peaks.get(key, 0), value)
            time.sleep(0.05)


class Capture:
    """detect_boxes for frames.run_decode: the CPU ONNX detector, timing it, hashing every plane and optionally
    appending every plane to a file (never held in memory, so the sampler's RSS isn't the harness's)."""

    def __init__(self, det: textdet.TextDetector) -> None:
        self.det = det
        self.sink = None
        self.detect_s = 0.0
        self.frames_by_shape: dict[str, int] = {}
        self.hashes: list[str] = []

    def __call__(self, planes):
        start = time.monotonic()
        found = self.det.detect(planes)
        self.detect_s += time.monotonic() - start
        shape = f"{planes.shape[2]}x{planes.shape[1]}"
        self.frames_by_shape[shape] = self.frames_by_shape.get(shape, 0) + len(planes)
        self.hashes.extend(hashlib.sha1(plane.tobytes()).hexdigest() for plane in planes)
        if self.sink is not None:
            self.sink.write(planes.tobytes())
        return found


def rows_json(rows):
    return [[r[0], r[1], r[2], [list(b) for b in r[3]]] for r in rows]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("vendor", choices=sorted(VENDORS))
    p.add_argument("tag")
    p.add_argument("path")
    p.add_argument("out")
    p.add_argument("--episode", action="store_true")
    a = p.parse_args()
    gpu, dev = VENDORS[a.vendor]
    prefix = os.path.join(a.out, f"{a.tag}_{a.vendor}")

    sampler = Sampler()
    sampler.start()
    det = textdet.TextDetector(textdet.cpu_session(MODEL, 2), backend="cpu")

    commands: list[dict] = []
    orig_command = frames.decode_command
    orig_run = frames.run_decode
    phase_ref = {"name": "setup"}

    def logged_command(*args, **kwargs):
        argv, active = orig_command(*args, **kwargs)
        commands.append({"phase": phase_ref["name"], "argv": argv, "hw_active": active})
        return argv, active

    decode_times: list[dict] = []

    def timed_run(command, **kwargs):
        start = time.monotonic()
        try:
            rows = orig_run(command, **kwargs)
        except Exception as exc:
            decode_times.append(
                {
                    "phase": phase_ref["name"],
                    "secs": round(time.monotonic() - start, 2),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        decode_times.append(
            {
                "phase": phase_ref["name"],
                "secs": round(time.monotonic() - start, 2),
                "rows": len(rows),
                "scale": kwargs.get("scale", 1),
            }
        )
        return rows

    frames.decode_command = logged_command
    frames.run_decode = timed_run

    probe = probe_media(a.path, ffprobe=FFPROBE, timeout_s=60)
    dur_ms = probe.duration_ms
    window_ms, cap_ms = credits_limits_ms(is_episode=a.episode, tv_window_s=None, movie_window_s=None)
    earliest = (
        earliest_credits_start_ms(
            dur_ms, is_movie=not a.episode, credits_window_ms=window_ms, movie_credits_max_from_end_ms=cap_ms
        )
        / 1000.0
    )
    start_time_s = frames.container_start_s(a.path, FFMPEG)
    thinning = frames.keyframe_thinning(a.path, FFMPEG)
    tail_start = frames.tail_start_s(dur_ms, tail_s=frames.tail_length_s(is_episode=a.episode))
    result = {
        "vendor": a.vendor,
        "tag": a.tag,
        "path": a.path,
        "episode": a.episode,
        "dur_ms": dur_ms,
        "earliest_s": earliest,
        "start_time_s": start_time_s,
        "tail_start_s": tail_start,
        "thinning": thinning._asdict(),
        "phases": {},
    }

    for scale in (1, 2):
        phase = f"tail{scale}"
        phase_ref["name"] = phase
        sampler.set_phase(phase)
        cap = Capture(det)
        with open(f"{prefix}_{phase}.u8", "wb") as sink:
            cap.sink = sink
            start = time.monotonic()
            error = None
            try:
                rows = frames.decode_rows(
                    a.path,
                    ffmpeg=FFMPEG,
                    start_s=tail_start,
                    length_s=None,
                    keyframes_only=True,
                    fps=None,
                    gpu=gpu,
                    gpu_device_path=dev,
                    detect_boxes=cap,
                    start_time_s=start_time_s,
                    keep_every=thinning.keep_every,
                    drop_non_key=thinning.drop_non_key,
                    scale=scale,
                    download_format=thinning.download_format,
                )
            except Exception as exc:  # recorded, not hidden
                rows, error = [], f"{type(exc).__name__}: {exc}"
            secs = time.monotonic() - start
        result["phases"][phase] = {
            "secs": round(secs, 2),
            "detect_s": round(cap.detect_s, 2),
            "error": error,
            "frames": cap.frames_by_shape,
            "rows": rows_json(rows),
        }

    phase_ref["name"] = "find"
    sampler.set_phase("find")
    cap = Capture(det)
    start = time.monotonic()
    try:
        r = detector.find_credits(
            a.path,
            duration_ms=dur_ms,
            is_episode=a.episode,
            ffmpeg=FFMPEG,
            detect_boxes=cap,
            gpu=gpu,
            gpu_device_path=dev,
            earliest_start_s=earliest,
        )
        found = {
            "start_s": r.start_s,
            "end_s": r.end_s,
            "scale": r.scale,
            "key_rows": rows_json(r.key_rows),
            "fine_rows": rows_json(r.fine_rows),
            "end_rows": rows_json(r.end_rows),
            "run_rows": rows_json(r.run_rows),
            "overlays": [list(b) for b in r.overlays],
            "error": None,
        }
    except Exception as exc:
        found = {"start_s": None, "end_s": None, "scale": None, "error": f"{type(exc).__name__}: {exc}"}
    found.update(
        {
            "secs": round(time.monotonic() - start, 2),
            "detect_s": round(cap.detect_s, 2),
            "frames": cap.frames_by_shape,
            "hashes": cap.hashes,
        }
    )
    result["phases"]["find"] = found

    sampler.stop.set()
    sampler.join()
    result["commands"] = commands
    result["decodes"] = decode_times
    result["mem_peaks_kib"] = sampler.peaks
    result["ru_maxrss_self_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["ru_maxrss_children_kib"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    with open(f"{prefix}.json", "w") as fh:
        json.dump(result, fh)
    t1, t2, f = result["phases"]["tail1"], result["phases"]["tail2"], result["phases"]["find"]
    print(
        f"{a.tag} {a.vendor}: tail1 {len(t1['rows'])} rows {t1['secs']}s err={t1['error']} | "
        f"tail2 {len(t2['rows'])} rows {t2['secs']}s err={t2['error']} | find start={f['start_s']} end={f['end_s']} "
        f"scale={f['scale']} {f['secs']}s err={f['error']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
