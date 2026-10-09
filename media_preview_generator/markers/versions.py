"""The versions of the detectors and readers whose stored answers carry one, for deciding whether an answer is older.

Every detector and reader stores its version with each answer (``evidence_versions``), and a file's run asks again an
answer from another version (``pipeline._detector_pending``, ``_stale_evidence``, ``_server_markers_due``). Files are
refreshed by their next manual or scheduled run.

A detector that reads the file itself and checks what other sources decided (credit text moves a credits chapter's
start and wins an online start, season audio checks an intro chapter and a lone online answer) marks its version
``checks_others``: its older answer matters for an unlocked decided type whatever decided it. The decision rules have a
version too (``decide.DECIDE_RULES_VERSION``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .audio.season import SEASON_AUDIO_ANSWER_VERSION
from .credits.detector import _WINDOW_VERSION_STEP, CREDITS_TEXT_VERSION
from .models import SERVER_SOURCES, MarkerType, Source
from .pipeline import PARSER_VERSIONS
from .sources.chapters import CHAPTER_RULES_VERSION
from .sources.server_markers import READER_VERSION

if TYPE_CHECKING:
    from .settings import GlobalMarkersSettings

_EVERY_TYPE = frozenset(MarkerType)


@dataclass(frozen=True)
class AnswerVersion:
    """A detector or reader that stores its version with every answer.

    Attributes:
        key: Its name in ``version_reruns``.
        sources: The sources its answers are stored under, which a decided marker names when it rests on one.
        types: The marker types it answers.
        version: Its version now.
        version_step: A stored version is compared modulo this; 0 compares it whole.
        asked: Whether a run asks it with these settings.
        checks_others: Its answer checks decisions other sources made, so its older answer lists a file whatever
            decided the type (credit text and season audio).
    """

    key: str
    sources: frozenset[Source]
    types: frozenset[MarkerType]
    version: int
    version_step: int = 0
    asked: Callable[[GlobalMarkersSettings], bool] | None = None
    checks_others: bool = False


def answer_versions() -> tuple[AnswerVersion, ...]:
    """Every detector and reader whose answers carry a version, at its version now.

    Season audio's includes its end-picture check's (``SEASON_AUDIO_ANSWER_VERSION``), and credit text's stored version
    carries the user's window above ``_WINDOW_VERSION_STEP`` (``credits.detector.credits_answer_version``).

    Returns:
        The detectors and readers.
    """
    return (
        AnswerVersion(
            "credits_text",
            frozenset({Source.CREDITS_TEXT}),
            frozenset({MarkerType.CREDITS}),
            CREDITS_TEXT_VERSION,
            _WINDOW_VERSION_STEP,
            asked=lambda settings: settings.detect_credits and settings.source_enabled(Source.CREDITS_TEXT.value),
            checks_others=True,
        ),
        AnswerVersion(
            "season_audio",
            frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}),
            frozenset({MarkerType.INTRO}),
            SEASON_AUDIO_ANSWER_VERSION,
            checks_others=True,
        ),
        AnswerVersion("server_markers", SERVER_SOURCES, _EVERY_TYPE, READER_VERSION),
        AnswerVersion("chapters", frozenset({Source.CHAPTERS}), _EVERY_TYPE, CHAPTER_RULES_VERSION),
        *(
            AnswerVersion(source.value, frozenset({source}), _EVERY_TYPE, version)
            for source, version in PARSER_VERSIONS.items()
        ),
    )
