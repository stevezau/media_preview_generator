"""Routing rules: send preview files that are much faster on a GPU to a GPU worker.

A rule checks a file's MediaInfo details before the file is queued. When it matches, the file gets a label: a
capability its worker must offer, and a short reason. GPU workers offer "gpu" (``Worker.capabilities``), the only
capability so far.

``JobDispatcher._claim_worker`` gives a labelled file to a worker that offers its capability. While such a worker runs
previews, the file waits for it, even a busy one. When none does, any worker takes the file, so nothing gets stuck.
The reason shows on the job card, on the worker's card and in the job log.

To add a rule, write a function that takes the file's path and its MediaInfo details (None if MediaInfo couldn't open
the file) and returns a short reason, such as "AV1", or None. Then add a :class:`Rule` for it to ``RULES``. The
Settings page lists it with its own switch, ``/api/settings`` saves it under ``routing_rules``, and it starts off.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..processing.generator import read_media_info
from ..processing.hdr_detection import hdr_reason

GPU = "gpu"


@dataclass(frozen=True)
class Requirement:
    """What a file needs from its worker, and why."""

    capabilities: frozenset[str]
    reason: str


@dataclass(frozen=True)
class Rule:
    """A routing rule. The Settings page lists each one with its own switch.

    Attributes:
        id: Its key in the ``routing_rules`` setting.
        title: Its name on the Settings page.
        description: One line there: which files it matches, and where it sends them.
        capability: What a matched file needs from its worker.
        reason: ``(path, media_info) -> reason or None``; ``media_info`` is None when MediaInfo couldn't open the file.
    """

    id: str
    title: str
    description: str
    capability: str
    reason: Callable[[str, Any], str | None]


RULES: tuple[Rule, ...] = (
    Rule(
        id="hdr",
        title="HDR and Dolby Vision on a GPU",
        description="Sends HDR and Dolby Vision files to a GPU worker. A CPU is slow at these.",
        capability=GPU,
        reason=hdr_reason,
    ),
)


def rule_states(saved: object) -> dict[str, bool]:
    """Each rule's switch from the saved ``routing_rules`` setting. A rule is on only when saved as ``True``.

    A rule with nothing saved, on a fresh install or added since, is off. A saved id no rule has is left out.

    Args:
        saved: The setting's value: rule id -> on.

    Returns:
        Rule id -> on, for every rule in ``RULES``.
    """
    states = saved if isinstance(saved, dict) else {}
    return {rule.id: states.get(rule.id) is True for rule in RULES}


def file_requirement(path: str, config: Any) -> Requirement | None:
    """What ``path`` needs from its worker, by the rules ``config.routing_rules`` switches on, or None.

    With a rule on, MediaInfo reads the file once, and only its headers (``parse_speed=0``). The fields rules read are
    there, so a 90 MB file costs 0.2 MB rather than the 15 MB of a default parse. Never raises: a rule that fails is
    logged and skipped, because the checking stage must queue every file it checked.

    Args:
        path: The file.
        config: The job's ``Config``.

    Returns:
        The capabilities and reasons of every rule that matched, or None when none did.
    """
    states = rule_states(getattr(config, "routing_rules", None))
    enabled = [rule for rule in RULES if states[rule.id]]
    if not enabled:
        return None
    try:
        media_info = read_media_info(path, parse_speed=0)
    except Exception as exc:
        logger.debug("MediaInfo couldn't open {} for the routing rules ({}: {})", path, type(exc).__name__, exc)
        media_info = None
    capabilities: set[str] = set()
    reasons: list[str] = []
    for rule in enabled:
        try:
            reason = rule.reason(path, media_info)
        except Exception as exc:
            logger.warning(
                "Routing rule {} failed on {}, so it was skipped: {}: {}", rule.id, path, type(exc).__name__, exc
            )
            continue
        if reason:
            capabilities.add(rule.capability)
            reasons.append(reason)
    return Requirement(frozenset(capabilities), ", ".join(reasons)) if capabilities else None
