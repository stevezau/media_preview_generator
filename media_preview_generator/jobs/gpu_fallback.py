"""Per-GPU count of files in a row that needed the CPU fallback, kept for the life of the process.

A GPU worker records every file it finishes (``Worker._record_gpu_file``): "fell back" when the file needed the CPU
(a whole-file rerun or a step that fell back inside a kind's own run), otherwise "ok". ``GPU_FALLBACK_STREAK`` fallen-back
files in a row flag the GPU; the bell shows one card per flagged GPU (``web.notifications``), and the next ok file on
that GPU clears the flag and the count. Cancelled files and CPU workers never count.

Workers are threads and several can share one GPU, so every record takes the lock. The key is the GPU's ``device``
from ``gpu_config`` (the worker's ``gpu_device``), the same identity Settings → GPU uses.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from ..utils import redact_secrets

GPU_FALLBACK_STREAK = 5

_NOTIFICATION_ID_PREFIX = "gpu_keeps_failing_"


def notification_id(key: str) -> str:
    """The bell card's stable id for one GPU; a device path is made URL-segment safe (``/dev/dri/renderD128``)."""
    return _NOTIFICATION_ID_PREFIX + re.sub(r"[^A-Za-z0-9_.-]+", "_", key)


def is_gpu_notification_id(notification_id_value: str) -> bool:
    """Whether a notification id names one of these per-GPU cards."""
    return notification_id_value.startswith(_NOTIFICATION_ID_PREFIX)


@dataclass
class GpuFallbackState:
    """One GPU's current streak."""

    key: str
    name: str
    streak: int = 0
    last_reason: str | None = None

    @property
    def flagged(self) -> bool:
        return self.streak >= GPU_FALLBACK_STREAK


class GpuFallbackTracker:
    """Thread-safe per-GPU streak counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, GpuFallbackState] = {}

    def record(self, key: str, name: str, *, fell_back: bool, reason: str | None) -> None:
        """Count one finished file for a GPU.

        Args:
            key: The GPU's identity (its ``gpu_config`` device).
            name: The GPU's name as the worker cards show it.
            fell_back: True when the file needed the CPU fallback.
            reason: Why it fell back; secrets are masked before it is kept.
        """
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = self._states[key] = GpuFallbackState(key=key, name=name)
            state.name = name or state.name
            was_flagged = state.flagged
            if fell_back:
                state.streak += 1
                state.last_reason = redact_secrets(reason or "") or "GPU processing failed"
            else:
                state.streak = 0
                state.last_reason = None
        if was_flagged and not fell_back:
            # The card was hidden for this streak; the next one must show again.
            from ..web.notifications import undismiss_session

            undismiss_session(notification_id(key))

    def states(self) -> list[GpuFallbackState]:
        """Every GPU seen so far, copied, in key order."""
        with self._lock:
            return [GpuFallbackState(s.key, s.name, s.streak, s.last_reason) for _, s in sorted(self._states.items())]

    def flagged(self) -> list[GpuFallbackState]:
        """The GPUs currently at or past the streak, copied, in key order."""
        return [s for s in self.states() if s.flagged]

    def reset(self) -> None:
        """Forget every GPU (tests)."""
        with self._lock:
            self._states.clear()


_tracker = GpuFallbackTracker()


def get_gpu_fallback_tracker() -> GpuFallbackTracker:
    """The process-wide tracker."""
    return _tracker
