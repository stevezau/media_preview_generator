"""Answers from an older detector version, read again once (spec §6.2 step 3, "A detector's new version").

Every detector and reader stores its version with each answer (``evidence_versions``), and a file's run asks again an
answer from another version (``pipeline._detector_pending``, ``_stale_evidence``, ``_server_markers_due``). A file no job
runs keeps its older answer, though: after an update, credit text version 4 had reached most files with an answer, and
the files resting on version 3 kept its early answers. On every start the app compares the stored
versions with today's and queues the files whose decisions could move with them into LOW-priority Intro & Credits
jobs, at most ``BATCH_FILES`` a job with ``BATCH_GAP`` between jobs (``triggers.submit_version_reruns``).

A file is listed for a detector when an unlocked decided type rests on its older answer, or a type it answers is still
undecided (Needs review, no evidence) beside one; a type decided by other sources is left to the file's own next run,
as before. The decision rules have a version too (``decide.DECIDE_RULES_VERSION``): a file not recorded as decided
under today's (every run that decides a file records it) is listed when an unlocked type has a stored answer those
rules could decide differently. Its run is an ordinary one: it decides from what is stored and asks only what is due or
from an older version (a credits chapter rule 3 holds for credit text has it read). After the rules' first version
that lists most decided files once, 100 a batch.

A job holds its batch in its config, so a run revived after a restart runs the same files, and each file is recorded
with the versions it was read for as it finishes (``version_reruns``), whatever its outcome: it is never read again for
one version, and a file a batch never reached (a cancel, a restart the job isn't revived after) is taken by a later
batch. A later version takes it again.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from .audio.season import SEASON_AUDIO_ANSWER_VERSION
from .credits.detector import _WINDOW_VERSION_STEP, CREDITS_TEXT_VERSION
from .decide import DECIDE_RULES, DECIDE_RULES_VERSION
from .missing import files_on_disk, mark_missing_files
from .models import SERVER_SOURCES, MarkerType, Source
from .pipeline import PARSER_VERSIONS
from .sources.chapters import CHAPTER_RULES_VERSION
from .sources.server_markers import READER_VERSION

if TYPE_CHECKING:
    from ..servers.base import ServerConfig
    from .settings import GlobalMarkersSettings
    from .store import MarkerStore

# At most this many files a job, and this long between one job's end and the next one's start, so a large backlog
# after an update runs beside everything else instead of filling the workers for days.
BATCH_FILES = 100
BATCH_GAP = timedelta(minutes=30)
# Not a detector: a one-version Plex item left showing times within 2 s of a moved decision, before the publisher
# stopped keeping them (``MarkerStore.files_on_one_version_items_showing_other_times``). The file's next run sends the
# decided times; its version names that rule.
PUBLISHED_TIMES = "published_times"
PUBLISHED_TIMES_VERSION = 1

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
    """

    key: str
    sources: frozenset[Source]
    types: frozenset[MarkerType]
    version: int
    version_step: int = 0
    asked: Callable[[GlobalMarkersSettings], bool] | None = None


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
        ),
        AnswerVersion(
            "season_audio",
            frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}),
            frozenset({MarkerType.INTRO}),
            SEASON_AUDIO_ANSWER_VERSION,
        ),
        AnswerVersion("server_markers", SERVER_SOURCES, _EVERY_TYPE, READER_VERSION),
        AnswerVersion("chapters", frozenset({Source.CHAPTERS}), _EVERY_TYPE, CHAPTER_RULES_VERSION),
        *(
            AnswerVersion(source.value, frozenset({source}), _EVERY_TYPE, version)
            for source, version in PARSER_VERSIONS.items()
        ),
    )


def _asked(answer: AnswerVersion, settings: GlobalMarkersSettings) -> bool:
    if answer.asked is not None:
        return answer.asked(settings)
    return any(settings.source_enabled(source.value) for source in answer.sources)


def files_to_read_again(store: MarkerStore, settings: GlobalMarkersSettings) -> dict[str, dict[str, int]]:
    """The files to read again and, for each, the versions it is read again for.

    Args:
        store: The markers store.
        settings: The detection settings: a detector no run would ask lists nothing.

    Returns:
        ``{path: {detector: version}}``, files marked missing and files already taken for those versions left out.
    """
    due: dict[str, dict[str, int]] = {}
    for answer in answer_versions():
        if not _asked(answer, settings):
            continue
        for path in store.files_with_older_answers(
            answer.key,
            sources=answer.sources,
            types=answer.types,
            version=answer.version,
            version_step=answer.version_step,
        ):
            due.setdefault(path, {})[answer.key] = answer.version
    for path in store.files_on_one_version_items_showing_other_times(PUBLISHED_TIMES, PUBLISHED_TIMES_VERSION):
        due.setdefault(path, {})[PUBLISHED_TIMES] = PUBLISHED_TIMES_VERSION
    for path in store.files_decided_under_older_rules(DECIDE_RULES, DECIDE_RULES_VERSION):
        due.setdefault(path, {})[DECIDE_RULES] = DECIDE_RULES_VERSION
    return due


def next_batch(
    store: MarkerStore,
    settings: GlobalMarkersSettings,
    configs: Sequence[ServerConfig],
    *,
    limit: int = BATCH_FILES,
) -> dict[str, dict[str, int]]:
    """The next files to read again: the first ``limit`` still on disk, by season folder then path (a season's episodes
    run together). Nothing is recorded: the job records each file as it finishes (``record_taken``).

    A file not on disk now is left out and stays listed (its disk may only be unmounted); one whose disk says it is
    gone is marked missing (``mark_missing_files``), which leaves it out of every listing until it is back.

    Args:
        store: The markers store.
        settings: The detection settings.
        configs: The servers' configs (for their disk roots).
        limit: The most files to take.

    Returns:
        ``{path: {detector: version}}`` in run order.
    """
    due = files_to_read_again(store, settings)
    ordered = sorted(due, key=lambda path: (os.path.dirname(path), path))
    present, absent = files_on_disk(ordered, limit=limit)
    if absent:
        mark_missing_files(store, absent, configs)
    return {path: due[path] for path in present[:limit]}


def record_taken(store: MarkerStore, batch: Mapping[str, Mapping[str, int]]) -> None:
    """Record that files were read again for these versions, so no later listing takes them again for those."""
    store.record_version_reruns(
        (path, detector, version) for path, taken in batch.items() for detector, version in taken.items()
    )
