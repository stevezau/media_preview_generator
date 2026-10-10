"""Data shapes for the library health check."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CheckCancelled(Exception):
    """Raised inside a running check when the user cancels it."""


class Feature(str, Enum):
    """The things a library can be checked for."""

    PREVIEWS = "previews"
    LOUDNESS = "loudness"
    INTRO = "intro"
    CREDITS = "credits"


class CellState(str, Enum):
    """What a library/feature cell shows."""

    COUNTED = "counted"
    OFF = "off"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass
class Cell:
    """One library/feature result: counts when counted, otherwise a state with a reason."""

    state: CellState
    total: int = 0
    done: int = 0
    nothing_found: int = 0
    not_showing: int = 0  # Plex previews only
    reason: str = ""  # shown for OFF / NOT_APPLICABLE / UNAVAILABLE / ERROR

    @property
    def todo(self) -> int:
        """Files still to do: not done and not already searched with nothing found."""
        return max(self.total - self.done - self.nothing_found, 0)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, including the computed ``todo``."""
        return {
            "state": self.state.value,
            "total": self.total,
            "done": self.done,
            "nothing_found": self.nothing_found,
            "not_showing": self.not_showing,
            "reason": self.reason,
            "todo": self.todo,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Cell:
        """Rebuild from ``to_dict`` output (``todo`` is recomputed)."""
        return cls(
            state=CellState(data["state"]),
            total=data.get("total", 0),
            done=data.get("done", 0),
            nothing_found=data.get("nothing_found", 0),
            not_showing=data.get("not_showing", 0),
            reason=data.get("reason", ""),
        )


@dataclass(frozen=True)
class TodoFile:
    """A file that still needs a feature (or, for Plex previews, a re-read)."""

    feature: Feature
    path: str  # canonical local path
    title: str
    item_id: str = ""  # server item id (Plex metadata item id for re-read)
    not_showing: bool = False  # Plex previews: BIF exists but Plex flag missing
    library_id: str = ""  # which library the file is in; the store keys its rows by it


@dataclass
class LibraryResult:
    """Counts for one library."""

    library_id: str
    name: str
    kind: str | None
    total: int
    cells: dict[Feature, Cell]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form; cell keys are feature value strings."""
        return {
            "library_id": self.library_id,
            "name": self.name,
            "kind": self.kind,
            "total": self.total,
            "cells": {feature.value: cell.to_dict() for feature, cell in self.cells.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LibraryResult:
        """Rebuild from ``to_dict`` output."""
        return cls(
            library_id=data["library_id"],
            name=data["name"],
            kind=data.get("kind"),
            total=data.get("total", 0),
            cells={Feature(key): Cell.from_dict(value) for key, value in data.get("cells", {}).items()},
        )


@dataclass
class ServerResult:
    """Everything one check found for one server."""

    server_id: str
    name: str
    type: str  # "plex" | "emby" | "jellyfin"
    libraries: list[LibraryResult]
    todo: list[TodoFile]  # todo AND not_showing entries; not serialised by to_dict
    access: str = ""  # e.g. "Reads Plex's database directly" / "No access to Plex's database"
    error: str = ""  # whole-server failure message
    checked_at: float = 0.0
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form without the ``todo`` list (the store keeps that in its own table)."""
        return {
            "server_id": self.server_id,
            "name": self.name,
            "type": self.type,
            "libraries": [lib.to_dict() for lib in self.libraries],
            "access": self.access,
            "error": self.error,
            "checked_at": self.checked_at,
            "duration_s": self.duration_s,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerResult:
        """Rebuild from ``to_dict`` output; ``todo`` comes back empty."""
        return cls(
            server_id=data["server_id"],
            name=data["name"],
            type=data["type"],
            libraries=[LibraryResult.from_dict(lib) for lib in data.get("libraries", [])],
            todo=[],
            access=data.get("access", ""),
            error=data.get("error", ""),
            checked_at=data.get("checked_at", 0.0),
            duration_s=data.get("duration_s", 0.0),
        )


@dataclass
class CheckProgress:
    """Live state of a running check or re-read."""

    kind: str  # "check" | "reread"
    started_at: float
    reason: str  # "manual" | "nightly" | "after_reread"
    server_name: str = ""
    step: str = ""  # plain words, e.g. "Checking markers"
    done: int = 0
    total: int = 0
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "kind": self.kind,
            "started_at": self.started_at,
            "reason": self.reason,
            "server_name": self.server_name,
            "step": self.step,
            "done": self.done,
            "total": self.total,
            "cancelled": self.cancelled,
        }
