"""Completed marker bookkeeping retained when unfinished work leaves memory.

Call only after the dispatcher has fenced and drained every callback. Never
serialize live clients, capability caches, file locks or detector objects.
"""

from __future__ import annotations

from .models import Source


def snapshot_context(ctx) -> dict:
    """Capture settled counts and obligations; unfinished files are freshly checked."""
    return {
        "decided_by": ctx.decided_by.snapshot(),
        "budget_exhausted": {key.value: value for key, value in ctx._budget_exhausted.items()},
        "key_refused": {key.value: value for key, value in ctx._key_refused.items()},
        "sent": dict(ctx._sent),
        "missing": ctx._missing,
        "busy_promised": sorted(ctx._busy_promised),
        "replaced_at_start": ctx._replaced_at_start,
    }


def restore_context(ctx, saved: dict) -> None:
    """Seed a fresh context before submission, preserving promised retries."""
    ctx.decided_by.restore(saved.get("decided_by", {}))
    ctx._busy_promised = set(saved.get("busy_promised", []))
    ctx._budget_exhausted = {Source(key): value for key, value in saved.get("budget_exhausted", {}).items()}
    ctx._key_refused = {Source(key): tuple(value) for key, value in saved.get("key_refused", {}).items()}
    ctx._sent = dict(saved.get("sent", {}))
    ctx._missing = int(saved.get("missing", 0))
    ctx._replaced_at_start = dict(saved.get("replaced_at_start", {}))
