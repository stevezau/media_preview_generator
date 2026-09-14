"""Scoring rules for the accuracy harness (spec §5.3: "useful" = end within 5 s and start within 15 s)."""

from __future__ import annotations

from dataclasses import dataclass

USEFUL_END_S = 5.0
USEFUL_START_S = 15.0


def judge_intro(segment: tuple[float, float] | None, truth: tuple[float, float]) -> str:
    """``useful``, ``wrong`` or ``missed`` for one detected intro against its truth (seconds)."""
    if segment is None:
        return "missed"
    useful = abs(segment[1] - truth[1]) <= USEFUL_END_S and abs(segment[0] - truth[0]) <= USEFUL_START_S
    return "useful" if useful else "wrong"


@dataclass
class Tally:
    """Useful / wrong / missed counts."""

    useful: int = 0
    wrong: int = 0
    missed: int = 0

    def add(self, verdict: str) -> None:
        """Count one verdict."""
        setattr(self, verdict, getattr(self, verdict) + 1)

    def as_dict(self) -> dict[str, int]:
        """The counts by name."""
        return {"useful": self.useful, "wrong": self.wrong, "missed": self.missed}

    def at_least(self, useful: int, wrong: int) -> bool:
        """Whether this is as good as a reference: as many useful or more, as many wrong or fewer."""
        return self.useful >= useful and self.wrong <= wrong
