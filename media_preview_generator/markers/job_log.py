"""Plain-words job log lines for Intro & Credits: what each source answered, what was decided and what was sent where.

Every line is its own log record, with its own time and level. A file's run ends in a block: a header naming the file
and what it's checked for, one line per source, what was decided, one line per server and how long it took, then a
done line — written together so another file's block or a worker's pickup line can't land inside it
(``write_lines``)::

    32 Frames: A 9/11 Mystery (2026): checking credits (films get credits only)
      Chapters: "Credits" chapter at 2:00:11–2:03:39
      SkipDB: no entry
      Credit text: credits start at 2:00:14 (keeps the "Credits" chapter at 2:00:11; saved earlier)
      Plex's own markers: none
      Decided: credits 2:00:11–2:03:39, from the "Credits" chapter
      Sent to Plex: credits 2:00:11–2:03:39
    32 Frames: A 9/11 Mystery (2026): done in 0.5 s, no worker needed

A file a GPU or CPU worker runs is announced first by the worker's own line (``pickup_line``), logged when the worker
starts it: "GPU Worker 2 (Intel UHD 770) picked up Accused S04E05". Its block still opens with its own header once the
worker finishes it, so another file's pickup line or block landing between the two can never split one file's lines. A
file whose answer didn't change and whose servers are up to date logs one line (``compact_line``). A Season job logs
one line per season instead of one per unchanged episode (``season_line``), a job deciding files again after the update
one line instead of one per unchanged file (``decide_again_line``), the weekly online re-check one line instead of one
per file nothing new was found for (``online_recheck_line``); every job starts with ``start_line`` and ends with a
totals line (``totals_line``).
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from loguru import logger

from .carry_over import CARRIED_OVER
from .decide import (
    AUDIO_OVER_CHAPTER_REASON,
    KEPT_BEFORE_RULE_CHANGE,
    TEXT_MOVES_CHAPTER_REASON,
    TEXT_OVER_CHAPTER_REASON,
    TEXT_START_NOTE,
    DecisionStatus,
    TypeDecision,
    kept_before_rule_change,
    read_chapter_hint,
    shortened_by,
    took_start_from_text,
)
from .external_ids import ids_from_path, is_season_folder
from .models import SERVER_SOURCES, STALE_SERVER_MARKERS_DETAIL, Marker, MarkerType, Source
from .outcomes import NOT_IN_LIBRARY, PLEX_PASS_UNKNOWN, FileOutcome, ServerStatus, is_kept_own
from .sources.online import is_budget_exhausted
from .sources.ratelimit import RESET_TIME_LABEL
from .store import EvidenceRow

# The names the job summary's "Decided by" counts use (web/static/js/app.js MARKER_SOURCE_NAMES), and how a decision
# names its sources.
SOURCE_LABELS: dict[Source, str] = {
    Source.CHAPTERS: "chapters",
    Source.THEINTRODB: "TheIntroDB",
    Source.INTRODB: "IntroDB",
    Source.SKIPDB: "SkipDB",
    Source.SEASON_AUDIO: "season audio",
    Source.SEASON_AUDIO_PREVIOUS: "last season's audio",
    Source.CREDITS_TEXT: "credit text",
    Source.SERVER_MARKERS: "server markers",
    Source.SERVER_MARKERS_IMPORTED: "server markers",
    Source.USER: "your edits",
}
# How a source's own line starts.
_LINE_LABELS: dict[Source, str] = {
    Source.CHAPTERS: "Chapters",
    Source.THEINTRODB: "TheIntroDB",
    Source.INTRODB: "IntroDB",
    Source.SKIPDB: "SkipDB",
    Source.SEASON_AUDIO: "Season audio",
    Source.SEASON_AUDIO_PREVIOUS: "Last season's audio",
    Source.CREDITS_TEXT: "Credit text",
}
# The order the source lines are in, whatever the user's order (the servers' own markers stand for one line per
# server). "Last season's audio" follows season audio when the file has that answer.
SOURCE_LINE_ORDER: tuple[Source, ...] = (
    Source.CHAPTERS,
    Source.THEINTRODB,
    Source.INTRODB,
    Source.SKIPDB,
    Source.SEASON_AUDIO,
    Source.CREDITS_TEXT,
    Source.SERVER_MARKERS,
)
# Sources that describe TV episodes only: a film's block leaves them out (its first line says films get credits only).
EPISODE_ONLY_SOURCES = frozenset({Source.INTRODB, Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS})
# Sources that read the file itself ("not read") rather than being asked ("not asked"), and the types each can answer.
_READS_FILE: dict[Source, frozenset[MarkerType]] = {
    Source.SEASON_AUDIO: frozenset({MarkerType.INTRO}),
    Source.SEASON_AUDIO_PREVIOUS: frozenset({MarkerType.INTRO}),
    Source.CREDITS_TEXT: frozenset({MarkerType.CREDITS}),
}
# Sources whose line says where and how long this job's read took (``RunNotes.reads``).
_READ_SHOWN = frozenset({Source.CREDITS_TEXT})
# What a source that looked and found nothing answered.
_EMPTY_ANSWER = {
    Source.CHAPTERS: "none",
    Source.THEINTRODB: "no entry",
    Source.INTRODB: "no entry",
    Source.SKIPDB: "no entry",
    Source.SEASON_AUDIO: "no match",
    Source.SEASON_AUDIO_PREVIOUS: "no match",
    Source.CREDITS_TEXT: "none found",
}
# The pipeline's note for a source skipped because the types it answers were already decided; its line says how.
ALREADY_DECIDED = "not needed (already decided)"
# An answer this file's run used without asking the source (stored by an earlier run, or by a sibling's season step).
SAVED = "saved earlier"
INDENT = "  "
_SEP = " · "
# The decision reason of a type a chapter decides alone (``decide._chapter_decision``).
_CHAPTER_RULE = "chapters"
# Release tags and ids in a folder or file name ("{tvdb-275274}", "[imdbid-tt0944947]", "[1080p]").
_TAG_RE = re.compile(r"[\{\[][^\}\]]*[\}\]]")
# A release group after the tags ("…[h264]-cinepth"): dropped with them.
_GROUP_RE = re.compile(r"(?<=[\]\}])-[^\s\[\]\{\}()]+$")
# Separators left behind once the tags are gone ("(2026) - -", "--").
_SEPARATOR_RUN_RE = re.compile(r"\s*-(?:\s*-)+\s*")
# A release year in a file or folder name ("Heat (1995)").
_YEAR_RE = re.compile(r"\(((?:19|20)\d\d)\)")
# A worker's display name without its device ("GPU Worker 2 (Intel UHD 770)" → "GPU Worker 2").
_DEVICE_RE = re.compile(r"\s*\([^()]*\)$")
# How a job that logs one line per season names itself there: a Season job, or the job that checks files TheIntroDB's
# used-up daily budget refused again after the reset.
SEASON_RECHECK_LABEL = "Season re-check"
BUDGET_RECHECK_LABEL = "TheIntroDB recheck"
# A marker carried over from a replaced file names no source (``carry_over.CARRIED_OVER``; web/static/js/app.js too).
CARRIED_OVER_LABEL = "the file it replaced"

# Held while a block of lines is logged, so a block's records are consecutive (another file's block or a worker's
# pickup line waits for it).
_WRITE_LOCK = threading.Lock()


@dataclass
class RunNotes:
    """What one file's run did with each source, kept across its stages (the checking thread, then a worker).

    Attributes:
        asked: ``(source, origin)`` read or asked during this job (origin: a server's id for its markers, else "").
        unanswered: Sources asked this job whose answer couldn't be stored, with what happened ("unavailable (HTTP
            503)", "no answer this time (…)").
        not_asked: Why a source wasn't asked (``ALREADY_DECIDED``, "not read (every server keeps its own credits)").
        title: How the log names the file (``file_title``); "" until a stage needs it.
        types: The types the file is checked for, once a stage knew them (the worker's pickup line names them).
        is_episode: Whether the file was checked as a TV episode, once a stage knew it.
        started: When the stage that finishes the file started (``PipelineContext.monotonic``).
        worker: The display name of the worker running that stage; "" on a checking thread.
        cpu_rerun: Whether that stage is a GPU worker's rerun of the file on the CPU.
        reads: Per local detector this job ran, where and how long it read the file ("read on the GPU in 13 s").
        logged: Whether the file's lines were written.
    """

    asked: set[tuple[Source, str]] = field(default_factory=set)
    unanswered: dict[Source, str] = field(default_factory=dict)
    not_asked: dict[Source, str] = field(default_factory=dict)
    title: str = ""
    types: frozenset[MarkerType] | None = None
    is_episode: bool | None = None
    started: float | None = None
    worker: str = ""
    cpu_rerun: bool = False
    reads: dict[Source, str] = field(default_factory=dict)
    logged: bool = False

    def answered(self, source: Source, origin: str = "") -> None:
        """Record that ``source`` answered (and the answer was stored) during this job.

        Args:
            source: The source.
            origin: A server's id for its markers, else "".
        """
        self.asked.add((source, origin))
        self.unanswered.pop(source, None)
        self.not_asked.pop(source, None)


@dataclass(frozen=True)
class ServerResult:
    """One server's row for one file, with our markers it shows now, which types it keeps as its own, and which of those
    this file decided (so ours of that type wasn't written there)."""

    row: Mapping
    ours: tuple[Marker, ...] = ()
    kept: frozenset[MarkerType] = frozenset()
    withheld: frozenset[MarkerType] = frozenset()


def write_lines(lines: Iterable[str], level: str = "INFO") -> None:
    """Log each line as its own record, all of them consecutively: another thread's block waits until they're written.

    Args:
        lines: The lines, in order (a detail line starts with ``INDENT``).
        level: Their level.
    """
    with _WRITE_LOCK:
        for line in lines:
            logger.log(level, "{}", line)


def clock(ms: int) -> str:
    """A time in the file as ``m:ss``, or ``h:mm:ss`` from an hour on.

    Args:
        ms: Milliseconds from the start.

    Returns:
        E.g. ``47:36`` or ``1:02:05``.
    """
    seconds = max(0, int(ms)) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def duration(seconds: float) -> str:
    """How long a step took, in plain words.

    Args:
        seconds: The time taken.

    Returns:
        E.g. ``0.5 s``, ``13 s``, ``2 min 5 s`` or ``1 h 4 min``.
    """
    seconds = max(0.0, float(seconds))
    if seconds < 9.95:
        return f"{f'{seconds:.1f}'.removesuffix('.0')} s"
    total = int(round(seconds))
    if total < 60:
        return f"{total} s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def read_phrase(on_gpu: bool, seconds: float, fallback: str = "", *, gpu_read_nothing: bool = False) -> str:
    """Where and how long a local detector read the file.

    Args:
        on_gpu: Whether it read on the worker's GPU.
        seconds: How long it took.
        fallback: What a step of it fell back to the CPU for, as the detector reported it; "" when none did.
        gpu_read_nothing: Whether the GPU read no frames and the detector read the file again on the CPU.

    Returns:
        E.g. ``read on the GPU in 13 s``, ``read on the GPU in 20 s; credit text detection on the CPU: …`` or ``read on
        the CPU after the GPU read nothing (40 s in all)``.
    """
    if gpu_read_nothing:
        # Whatever the CPU found, this says it: a GPU fallback's own reason would only repeat it.
        return f"read on the CPU after the GPU read nothing ({duration(seconds)} in all)"
    text = f"read on the {'GPU' if on_gpu else 'CPU'} in {duration(seconds)}"
    return f"{text}; {_lower_first(fallback)}" if fallback else text


def _span(start_ms: int, end_ms: int | None) -> str:
    return f"{clock(start_ms)}–{clock(end_ms)}" if end_ms is not None else f"from {clock(start_ms)}"


def _typed_span(mtype: MarkerType, start_ms: int, end_ms: int | None) -> str:
    if end_ms is not None:
        return f"{mtype.value} {clock(start_ms)}–{clock(end_ms)}"
    return f"{mtype.value} {'start' if mtype is MarkerType.CREDITS else 'starts'} at {clock(start_ms)}"


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _clean(name: str) -> str:
    stripped = _TAG_RE.sub(" ", _GROUP_RE.sub("", name.strip()))
    stripped = _SEPARATOR_RUN_RE.sub(" - ", stripped)
    return " ".join(stripped.split()).strip(" -._")


def show_name(canonical_path: str) -> str:
    """The show an episode belongs to: its season folder's parent, or its own folder when episodes sit straight in the
    show folder, without the ids and tags in the folder name.

    Args:
        canonical_path: The episode's local path.

    Returns:
        E.g. ``Rick and Morty (2013)``.
    """
    folder = os.path.dirname(canonical_path)
    show_folder = os.path.dirname(folder) if is_season_folder(os.path.basename(folder)) else folder
    return _clean(os.path.basename(show_folder)) or os.path.basename(show_folder)


def display_name(canonical_path: str) -> str:
    """How the job log names a file without its server's title: ``Show SxxEyy`` for an episode, the file's name without
    tags and release group for anything else.

    Args:
        canonical_path: The file's local path.

    Returns:
        E.g. ``Rick and Morty (2013) S01E01`` or ``Heat (1995)``.
    """
    ids = ids_from_path(canonical_path)
    if ids.is_episode and ids.season is not None and ids.episode is not None:
        return f"{show_name(canonical_path)} S{ids.season:02d}E{ids.episode:02d}"
    stem = os.path.splitext(os.path.basename(canonical_path))[0]
    return _clean(stem) or os.path.basename(canonical_path)


def file_title(canonical_path: str, server_title: str | None = None, year: object = None) -> str:
    """How the job log names a file: an episode as ``Show SxxEyy``, anything else by its server's title and year when
    a server gave them, else by its file name.

    Args:
        canonical_path: The file's local path.
        server_title: The title a media server gives the file's item, when known.
        year: That item's year, when known.

    Returns:
        E.g. ``32 Frames: A 9/11 Mystery (2026)``, ``Accused S04E05`` or ``Heat (1995)``.
    """
    if ids_from_path(canonical_path).is_episode:
        return display_name(canonical_path)
    title = " ".join(str(server_title or "").split())
    if not title:
        return display_name(canonical_path)
    year_text = str(year or "").strip()
    return f"{title} ({year_text})" if year_text.isdigit() and f"({year_text})" not in title else title


def path_year(canonical_path: str) -> str | None:
    """The year a file's name, or else its folder's, gives in parentheses ("Heat (1995).mkv").

    Args:
        canonical_path: The file's local path.

    Returns:
        E.g. ``1995``; None when neither gives one.
    """
    for name in (os.path.basename(canonical_path), os.path.basename(os.path.dirname(canonical_path))):
        found = _YEAR_RE.search(name)
        if found:
            return found.group(1)
    return None


def season_of(canonical_path: str) -> tuple[str, str]:
    """The season a Season job's summary line groups an episode under, and the episode's short name.

    Args:
        canonical_path: The episode's local path.

    Returns:
        ``("Rick and Morty (2013) S01", "E01")``; the folder and the file's name when the path has no SxxEyy.
    """
    ids = ids_from_path(canonical_path)
    if ids.is_episode and ids.season is not None and ids.episode is not None:
        return f"{show_name(canonical_path)} S{ids.season:02d}", f"E{ids.episode:02d}"
    return _clean(os.path.basename(os.path.dirname(canonical_path))), display_name(canonical_path)


def _types(types: Iterable[MarkerType]) -> str:
    chosen = set(types)
    return _and([t.value for t in MarkerType if t in chosen]) if chosen else ""


def checking_phrase(types: Collection[MarkerType], *, is_episode: bool) -> str:
    """What a file is checked for.

    Args:
        types: The types detected for the file.
        is_episode: Whether it is checked as a TV episode.

    Returns:
        E.g. ``checking intro and credits`` or ``checking credits (films get credits only)``.
    """
    if not types:
        return "nothing to check (detection of every type it could have is off)"
    return f"checking {_types(types)}" + ("" if is_episode else " (films get credits only)")


def pickup_line(worker: str, title: str) -> str:
    """The line a worker logs when it starts a file, before its block: what it's checked for is named on the block's
    own header once the worker finishes it, so this line doesn't repeat it.

    Args:
        worker: The worker's display name.
        title: The file's title (``file_title``).

    Returns:
        E.g. ``GPU Worker 2 (Intel UHD 770) picked up Accused S04E05``.
    """
    return f"{worker} picked up {title}"


def head_line(title: str, types: Collection[MarkerType], *, is_episode: bool) -> str:
    """The first line of a file's block, naming it and what it's checked for.

    Args:
        title: The file's title (``file_title``).
        types: The types the file is checked for.
        is_episode: Whether it is checked as a TV episode.

    Returns:
        E.g. ``32 Frames: A 9/11 Mystery (2026): checking credits (films get credits only)``.
    """
    return f"{title}: {checking_phrase(types, is_episode=is_episode)}"


def done_line(
    title: str, seconds: float | None, *, worker: str = "", cpu_rerun: bool = False, failed: bool = False
) -> str:
    """The last line of a file's block.

    Args:
        title: The file's title.
        seconds: How long the stage that finished it took; None when unknown.
        worker: The display name of the worker that ran it; "" for a checking thread.
        cpu_rerun: Whether it was a GPU worker's rerun on the CPU.
        failed: Whether the file failed.

    Returns:
        E.g. ``Accused S04E05: done in 25 s on GPU Worker 2`` or ``Heat (1995): done in 0.5 s, no worker needed``.
    """
    took = "" if seconds is None else f" {'after' if failed else 'in'} {duration(seconds)}"
    text = f"{title}: {'failed' if failed else 'done'}{took}"
    if worker:
        text += f" on {_DEVICE_RE.sub('', worker)}" + (", rerun on the CPU" if cpu_rerun else "")
    elif not failed:
        text += ", no worker needed"
    return text


def _names(sources: Iterable[str], chapter: str = "") -> list[str]:
    names = []
    for source in sources:
        if source == CARRIED_OVER:
            names.append(CARRIED_OVER_LABEL)
        elif source == Source.CHAPTERS.value and chapter:
            names.append(f'the "{chapter}" chapter')
        else:
            try:
                names.append(SOURCE_LABELS[Source(source)])
            except ValueError:
                names.append(source)
    return list(dict.fromkeys(names))


def chapter_names(rows: Iterable[EvidenceRow]) -> dict[MarkerType, dict[int, str]]:
    """The chapter names of a file's chapter answers, per type and start.

    Args:
        rows: The file's stored evidence.

    Returns:
        ``{type: {start_ms: name}}``.
    """
    names: dict[MarkerType, dict[int, str]] = {}
    for row in rows:
        if row.source is Source.CHAPTERS and row.type is not None and row.start_ms is not None and row.label:
            names.setdefault(row.type, {})[row.start_ms] = row.label
    return names


def _chapter_of(names: Mapping[MarkerType, Mapping[int, str]], mtype: MarkerType, start_ms: int | None) -> str:
    of_type = names.get(mtype) or {}
    if start_ms in of_type:
        return of_type[start_ms]
    return next(iter(of_type.values()), "") if len(of_type) == 1 else ""


def _with_notes(core: str, notes: list[str]) -> str:
    """``core`` with extra notes: inside its parenthesis when it ends with one, else after a comma."""
    if not notes:
        return core
    if core.endswith(")"):
        return f"{core[:-1]}; {'; '.join(notes)})"
    return f"{core}, {', '.join(notes)}"


def type_phrase(decision: TypeDecision, chapters: Mapping[MarkerType, Mapping[int, str]] | None = None) -> str:
    """One marker type's result in plain words, with why.

    Args:
        decision: The type's decision.
        chapters: The file's chapter names (``chapter_names``), so a chapter that decided is named.

    Returns:
        E.g. ``credits 58:23–59:04 (TheIntroDB and credit text agree)``, ``credits 2:00:11–2:03:39, from the "Credits"
        chapter``, ``intro nothing found`` or ``credits 47:36–48:38 from credit text needs review (…)``.
    """
    mtype = decision.type.value
    names_by_type = chapters or {}
    if decision.status is DecisionStatus.DECIDED and decision.marker is not None:
        marker = decision.marker
        chapter = _chapter_of(names_by_type, decision.type, marker.start_ms)
        names = _names(marker.decided_by, chapter)
        span = f"{mtype} {_span(marker.start_ms, marker.end_ms)}"
        # The chapter a rule moved or overruled no longer starts where the marker does: named when it is the only one.
        overruled = _chapter_of(names_by_type, decision.type, None)
        overruled = f'the "{overruled}" chapter' if overruled else "the chapters"
        if marker.locked:
            core = f"{span} (locked by you)"
        elif decision.reason.startswith(TEXT_MOVES_CHAPTER_REASON):
            core = f"{span} ({overruled}, moved to the first credit card by credit text)"
        elif len(names) > 1:
            core = f"{span} ({_and(names)} agree)"
        elif names == [CARRIED_OVER_LABEL]:
            core = f"{span} (from {CARRIED_OVER_LABEL})"
        elif tuple(marker.decided_by) == (Source.CHAPTERS.value,):
            core = f"{span}, from {names[0] if chapter else 'the chapters'}"
        else:
            core = f"{span} ({names[0] if names else decision.reason})"
        notes = []
        if not marker.locked and took_start_from_text(decision.reason):
            notes.append(TEXT_START_NOTE)
        if not marker.locked and decision.reason.startswith(AUDIO_OVER_CHAPTER_REASON):
            notes.append(f"{overruled} runs on into the episode")
        if not marker.locked and decision.reason.startswith(TEXT_OVER_CHAPTER_REASON):
            notes.append(f"{overruled} is off the credit roll")
        if not marker.locked and shortened_by(decision.reason) is not None:
            notes.append("start moved to the server's own marker")
        if kept_before_rule_change(decision.reason):
            notes.append(KEPT_BEFORE_RULE_CHANGE)
        return _with_notes(core, notes)
    if decision.status is DecisionStatus.NEEDS_REVIEW:
        if decision.proposed is None:
            return f"{mtype} needs review ({decision.reason})"
        proposed = decision.proposed
        found = _and(_names(proposed.decided_by, _chapter_of(names_by_type, decision.type, proposed.start_ms)))
        return f"{mtype} {_span(proposed.start_ms, proposed.end_ms)} from {found} needs review ({decision.reason})"
    if decision.status is DecisionStatus.NO_EVIDENCE:
        return f"{mtype} nothing found" + ("" if decision.reason == "no evidence" else f" ({decision.reason})")
    if is_kept_own(decision.status, decision.reason):
        return f"{mtype} {decision.reason.removesuffix(' markers').removesuffix(' marker')}"
    return f"{mtype}: {decision.reason}"


def _shown(decisions: Mapping[MarkerType, TypeDecision], types: Collection[MarkerType]) -> list[TypeDecision]:
    """The decisions a file's lines name: its detected types, and a type the user locked whatever the settings say."""
    return [
        decisions[mtype]
        for mtype in MarkerType
        if mtype in decisions
        and (mtype in types or (decisions[mtype].marker is not None and decisions[mtype].marker.locked))
    ]


def decided_line(
    decisions: Mapping[MarkerType, TypeDecision], types: Collection[MarkerType], rows: Iterable[EvidenceRow] = ()
) -> str:
    """The block's line of what was decided per type, and why.

    Args:
        decisions: The file's decisions.
        types: The types detected for the file (a locked type is shown whatever this says).
        rows: The file's stored evidence, for the chapter names.

    Returns:
        E.g. ``  Decided: intro 0:41–1:12 (IntroDB and season audio agree) · credits 41:48–43:10 (credit text)``.
    """
    names = chapter_names(rows)
    parts = [type_phrase(decision, names) for decision in _shown(decisions, types)]
    return f"{INDENT}Decided: {_SEP.join(parts) or 'nothing to detect for this file'}"


def review_note(decisions: Mapping[MarkerType, TypeDecision], types: Collection[MarkerType]) -> str:
    """What a file's types in review were found by, for a Season job's summary and a file's one-line result.

    Args:
        decisions: The file's decisions.
        types: The types detected for the file.

    Returns:
        E.g. ``credits from credit text only``; "" when no type is in review.
    """
    notes = []
    for mtype in MarkerType:
        decision = decisions.get(mtype)
        if mtype not in types or decision is None or decision.status is not DecisionStatus.NEEDS_REVIEW:
            continue
        names = _names(decision.proposed.decided_by) if decision.proposed else []
        if len(names) == 1:
            notes.append(f"{mtype.value} from {names[0]} only")
        elif names:
            notes.append(f"{mtype.value} from {' + '.join(names)}")
        else:
            notes.append(mtype.value)
    return ", ".join(notes)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _server_name(row: Mapping) -> str:
    return str(row.get("server_name") or row.get("server_id") or "a server")


def _markers(markers: Iterable[Marker]) -> str:
    order = list(MarkerType)
    return _SEP.join(
        f"{m.type.value} {_span(m.start_ms, m.end_ms)}" for m in sorted(markers, key=lambda m: order.index(m.type))
    )


def _kept_phrase(result: ServerResult, name: str) -> str:
    text = f"kept {name}'s own {_types(result.kept)}"
    if result.withheld:
        vendor = str(result.row.get("server_type") or "").capitalize() or name
        text += f' instead of ours ("Keep {vendor}\'s")'
    return text


def sent_line(result: ServerResult) -> str:
    """What happened on one server for one file.

    Args:
        result: The server's row, our markers it shows now and the types it keeps as its own.

    Returns:
        E.g. ``  Sent to Plex: intro 0:41–1:12 · credits 41:48–43:10``, ``  Sent to Plex: already up to date (…)``,
        ``  Sent to Plex: kept Plex's own credits``, ``  Sent to Plex: failed (…)`` or ``  Sent to Plex: not in Plex's
        library yet (…)``.
    """
    row = result.row
    name = _server_name(row)
    status = row.get("status")
    message = str(row.get("message") or "")
    ours = _markers(result.ours)
    kept = _kept_phrase(result, name) if result.kept else ""
    if status == ServerStatus.WRITTEN.value:
        text = ours or "cleared our markers"
    elif status in (ServerStatus.UP_TO_DATE.value, ServerStatus.NEEDS_REVIEW.value, ServerStatus.NONE.value):
        text = f"already up to date ({ours})" if ours else ("" if kept else "nothing to send")
    elif status == ServerStatus.WAITING.value:
        code = row.get("reason_code")
        if code == NOT_IN_LIBRARY:
            text = f"not in {name}'s library yet (waiting for it to add the file)"
        elif code == PLEX_PASS_UNKNOWN:
            text = "waiting (couldn't confirm Plex Pass)"
        else:
            text = f"{ours}; {_lower_first(message)}" if ours else f"waiting ({_lower_first(message)})"
    elif status == ServerStatus.SKIPPED.value:
        text = f"skipped ({message})"
    else:
        text = f"failed ({message})"
    return f"{INDENT}Sent to {name}: {'; '.join(part for part in (text, kept) if part)}"


def _skip_reason(label: str, detail: str) -> str:
    if is_budget_exhausted(detail):
        return f"daily limit reached, resets {RESET_TIME_LABEL}"
    return detail.removeprefix(f"{label} ")


def _season_detail(source: Source, label: str) -> str:
    """Season audio's "support/others" label in words: the episodes that share the theme, of those compared."""
    support, _, others = label.partition("/")
    if not (support.isdigit() and others.isdigit()):
        return ""
    if source is Source.SEASON_AUDIO_PREVIOUS:
        return f"same theme found in {support} of last season's {others} episodes"
    # The label counts the episode's partners; the episode itself has the theme too.
    return f"same theme found in {int(support) + 1} of {int(others) + 1} episodes"


def _chapter_check(label: str, chapters: Mapping[MarkerType, Mapping[int, str]]) -> str:
    """What a credit text answer read against the file's credits chapter found (``decide.chapter_hint``): the chapter
    off the roll, so the answer moves its start, or the chapter kept; "" for an answer read without one."""
    hint = read_chapter_hint(label)
    if hint is None:
        return ""
    chapter_ms, moves, to_ms = hint
    name = (chapters.get(MarkerType.CREDITS) or {}).get(chapter_ms) or _chapter_of(chapters, MarkerType.CREDITS, None)
    chapter = f'the "{name}" chapter' if name else "the credits chapter"
    if moves and to_ms is not None:
        return f"moves {chapter} at {clock(chapter_ms)} to the first text after it, {clock(to_ms)}"
    if moves:
        return f"moves {chapter} at {clock(chapter_ms)} to the first credit card"
    return f"keeps {chapter} at {clock(chapter_ms)}"


def _row_phrase(row: EvidenceRow, chapters: Mapping[MarkerType, Mapping[int, str]]) -> tuple[str, str]:
    """A typed evidence row's answer, and a note about it ("" when none)."""
    if row.source is Source.CHAPTERS:
        where = _span(row.start_ms, row.end_ms)
        where = where if row.end_ms is None else f"at {where}"
        return (f'"{row.label}" chapter {where}' if row.label else f"{row.type.value} chapter {where}"), ""
    phrase = _typed_span(row.type, row.start_ms, row.end_ms)
    if row.source in (Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS):
        return phrase, _season_detail(row.source, row.label)
    if row.source is Source.CREDITS_TEXT:
        return phrase, _chapter_check(row.label, chapters)
    # A server's marker made for an earlier file is shown, but counted for nothing (``Candidate.stale``).
    return phrase, "made for an earlier file" if row.detail == STALE_SERVER_MARKERS_DETAIL else ""


def _answer(
    rows: list[EvidenceRow],
    empty: str,
    extras: list[str],
    chapters: Mapping[MarkerType, Mapping[int, str]] | None = None,
) -> str:
    order = list(MarkerType)
    # Rows that give the same answer are named once.
    unique: dict[tuple, EvidenceRow] = {}
    for r in rows:
        if r.type is not None and r.start_ms is not None:
            unique.setdefault((r.type, r.start_ms, r.end_ms, r.label, r.detail == STALE_SERVER_MARKERS_DETAIL), r)
    typed = sorted(
        unique.values(),
        key=lambda r: (order.index(r.type), r.start_ms, r.end_ms if r.end_ms is not None else -1, r.label),
    )
    phrases = [_row_phrase(r, chapters or {}) for r in typed]
    if len(phrases) == 1 and phrases[0][1]:
        text, extras = phrases[0][0], [phrases[0][1], *extras]
    else:
        text = ", ".join(f"{phrase} ({note})" if note else phrase for phrase, note in phrases) or empty
    return f"{text} ({'; '.join(extras)})" if extras else text


def _already_decided(
    source: Source,
    decisions: Mapping[MarkerType, TypeDecision],
    types: Collection[MarkerType],
    chapters: Mapping[MarkerType, Mapping[int, str]],
) -> str:
    """Why a source wasn't needed: the types it answers were decided, a chapter named for its type used as it is."""
    verb = "not read" if source in _READS_FILE else "not asked"
    answers = _READS_FILE.get(source)
    decided = [
        decisions[t]
        for t in MarkerType
        if t in types
        and t in decisions
        and (answers is None or t in answers)
        and decisions[t].status is DecisionStatus.DECIDED
        and decisions[t].marker is not None
    ]
    names = [_chapter_of(chapters, d.type, d.marker.start_ms) for d in decided if d.reason == _CHAPTER_RULE]
    if decided and len(names) == len(decided) and all(names):
        if len(names) == 1:
            return f"{verb} (a chapter named {names[0]} is used as-is)"
        return f"{verb} (chapters named {_and(names)} are used as-is)"
    return f"{verb} (already decided)"


@dataclass(frozen=True)
class _SourceView:
    """What a file's source lines are built from."""

    rows: list[EvidenceRow]
    notes: RunNotes
    skipped: Mapping[Source, str]
    decisions: Mapping[MarkerType, TypeDecision]
    types: Collection[MarkerType]
    chapters: Mapping[MarkerType, Mapping[int, str]]


def _source_line(source: Source, view: _SourceView) -> tuple[str, bool]:
    """A source's line and whether it answered (this job, or saved earlier); False when it wasn't asked."""
    label = _LINE_LABELS[source]
    notes = view.notes
    if source in view.skipped:
        return f"{INDENT}{label}: skipped ({_skip_reason(label, view.skipped[source])})", True
    if source in notes.unanswered:
        return f"{INDENT}{label}: {notes.unanswered[source]}", True
    own = [r for r in view.rows if r.source is source and r.origin == ""]
    if own:
        if (source, "") not in notes.asked:
            extras = [SAVED]
        else:
            extras = [notes.reads[source]] if source in _READ_SHOWN and source in notes.reads else []
        return f"{INDENT}{label}: {_answer(own, _EMPTY_ANSWER.get(source, 'none'), extras, view.chapters)}", True
    note = notes.not_asked.get(source)
    if note == ALREADY_DECIDED:
        note = _already_decided(source, view.decisions, view.types, view.chapters)
    return f"{INDENT}{label}: {note or ('not read' if source in _READS_FILE else 'not asked')}", False


def _server_line(server_id: str, name: str, view: _SourceView, server_details: Mapping[str, str]) -> tuple[str, bool]:
    own = [r for r in view.rows if r.source in SERVER_SOURCES and r.origin == server_id]
    if not own:
        return f"{INDENT}{name}'s own markers: not read", False
    imported = any(r.source is Source.SERVER_MARKERS_IMPORTED for r in own)
    label = f"{name}'s imported markers" if imported else f"{name}'s own markers"
    extras = [] if (Source.SERVER_MARKERS, server_id) in view.notes.asked else [SAVED]
    unusable = next((server_details[r.detail] for r in own if r.detail in server_details), None)
    if unusable:
        return f"{INDENT}{label}: {_with_notes(unusable, extras)}", True
    return f"{INDENT}{label}: {_answer(own, 'none', extras)}", True


def source_lines(
    enabled: Iterable[str],
    rows: list[EvidenceRow],
    servers: Iterable[tuple[str, str]],
    notes: RunNotes,
    skipped: Mapping[Source, str],
    server_details: Mapping[str, str],
    *,
    decisions: Mapping[MarkerType, TypeDecision],
    types: Collection[MarkerType],
    is_episode: bool,
) -> list[str]:
    """One line per enabled source for one file: first the sources that answered (this job or saved earlier), then the
    ones that weren't asked and why, each group in ``SOURCE_LINE_ORDER``. A film leaves out the sources that only
    describe TV episodes.

    Args:
        enabled: The enabled sources' ids (``GlobalMarkersSettings.ordered_enabled_sources``).
        rows: The file's stored evidence (``MarkerStore.evidence_rows``).
        servers: ``(id, name)`` of every server that has the file (their markers are read whatever their Intro &
            Credits switch says).
        notes: What this job did with each source for the file.
        skipped: Sources the file was checked without for the whole job's reason, with the answer that stopped them.
        server_details: Plain words for a server's stored detail that says its markers weren't usable.
        decisions: The file's decisions, for why a source wasn't needed.
        types: The types detected for the file.
        is_episode: Whether the file was checked as a TV episode.

    Returns:
        E.g. ``['  Chapters: "Credits" chapter at 2:00:11–2:03:39', "  SkipDB: no entry", "  Plex's own markers: none",
        "  Credit text: not read (a chapter named Credits is used as-is)"]``.
    """
    on: set[Source] = set()
    for source_id in enabled:
        try:
            on.add(Source(source_id))
        except ValueError:
            continue
    view = _SourceView(rows, notes, skipped, decisions, types, chapter_names(rows))
    answered: list[str] = []
    not_asked: list[str] = []
    for source in SOURCE_LINE_ORDER:
        if source not in on or (not is_episode and source in EPISODE_ONLY_SOURCES):
            continue
        if source is Source.SERVER_MARKERS:
            lines = [_server_line(sid, name, view, server_details) for sid, name in servers]
        else:
            lines = [_source_line(source, view)]
            if source is Source.SEASON_AUDIO and any(r.source is Source.SEASON_AUDIO_PREVIOUS for r in rows):
                lines.append(_source_line(Source.SEASON_AUDIO_PREVIOUS, view))
        for line, has_answer in lines:
            (answered if has_answer else not_asked).append(line)
    return [*answered, *not_asked]


def _compact_server(result: ServerResult) -> str:
    name = _server_name(result.row)
    ours = _types(m.type for m in result.ours)
    text = f"{name} already has our {ours}" if ours else f"nothing sent to {name}"
    if result.kept:
        text += f"; {name} keeps its own {_types(result.kept)}"
    return text


def compact_line(
    title: str,
    servers: Iterable[ServerResult],
    decisions: Mapping[MarkerType, TypeDecision],
    types: Collection[MarkerType],
) -> str:
    """The one line of a file whose answer didn't change and whose servers are up to date.

    Args:
        title: The file's title.
        servers: Each server's result for the file.
        decisions: The file's decisions.
        types: The types detected for the file.

    Returns:
        E.g. ``Accused S04E05: unchanged, Plex already has our intro and credits``.
    """
    parts = [_compact_server(result) for result in servers]
    note = review_note(decisions, types)
    shown = _shown(decisions, types)
    if note:
        parts.append(f"still needs review ({note})")
    elif shown and all(d.status is DecisionStatus.NO_EVIDENCE for d in shown):
        parts.append("nothing found")
    return f"{title}: unchanged, {'; '.join(parts) or 'nothing to send'}"


def is_unchanged_result(rows: Iterable[Mapping]) -> bool:
    """Whether a file's server rows say nothing was written and nothing is pending (a file whose answer also didn't
    change gets ``compact_line``).

    Args:
        rows: The file's server rows.

    Returns:
        True when every row is up to date, in review or had nothing to send.
    """
    quiet = (ServerStatus.UP_TO_DATE.value, ServerStatus.NEEDS_REVIEW.value, ServerStatus.NONE.value)
    return all(row.get("status") in quiet for row in rows)


def kept_types(decisions: Mapping[MarkerType, TypeDecision], item_kept: Iterable[MarkerType]) -> frozenset[MarkerType]:
    """The types one server keeps as its own for a file: decided types the server kept its own markers of, and types
    left undecided because every server keeps its own.

    Args:
        decisions: The file's decisions.
        item_kept: The types the server's item keeps as its own (``ItemPublishStateRow.kept_types``).

    Returns:
        Those types.
    """
    kept = set(item_kept)
    return frozenset(
        mtype
        for mtype, decision in decisions.items()
        if (mtype in kept and decision.status is DecisionStatus.DECIDED)
        or is_kept_own(decision.status, decision.reason)
    )


def start_line(job_id: str, files: int, trigger: str) -> str:
    """A job's first line.

    Args:
        job_id: The job's id.
        files: How many files it runs.
        trigger: What started it, in plain words (``job_runner.trigger_words``); "" when that isn't known.

    Returns:
        E.g. ``Intro & Credits job 6742472e started: 1 file, follow-up to preview job c7ca6327 (Radarr import)``.
    """
    return f"Intro & Credits job {job_id[:8]} started: {_files(files)}" + (f", {trigger}" if trigger else "")


@dataclass(frozen=True)
class SeasonEpisode:
    """One episode a Season job checked: its short name, whether its decisions changed, and its review note."""

    episode: str
    changed: bool
    review: str


def _joined(names: list[str]) -> str:
    return "/".join(names)


def season_line(season: str, episodes: list[SeasonEpisode], label: str = SEASON_RECHECK_LABEL) -> str:
    """A Season job's one line for one season.

    Args:
        season: The season's name (``season_of``).
        episodes: The episodes of that season the job checked.
        label: What the job is (``SEASON_RECHECK_LABEL``, or ``BUDGET_RECHECK_LABEL`` for a TheIntroDB recheck).

    Returns:
        E.g. ``Season re-check, Rick and Morty (2013) S01 (3 episodes): no change, E01/E03/E04 still need review
        (credits from credit text only)``.
    """
    ordered = sorted(episodes, key=lambda e: e.episode)
    changed = [e.episode for e in ordered if e.changed]
    same = [e for e in ordered if not e.changed]
    parts = []
    if changed:
        parts.append(f"{_joined(changed)} changed (logged above)")
    if same:
        text = "no change" if not changed else f"no change for {_joined([e.episode for e in same])}"
        review = [e for e in same if e.review]
        if review:
            verb = "needs" if len(review) == 1 else "need"
            # The unchanged episodes are already named when some changed: they aren't named twice.
            who = "" if changed and len(review) == len(same) else f" {_joined([e.episode for e in review])}"
            text += f",{who} still {verb} review"
            notes = {e.review for e in review}
            if len(notes) == 1:
                text += f" ({notes.pop()})"
        parts.append(text)
    count = len(ordered)
    return f"{label}, {season} ({count} episode{'' if count == 1 else 's'}): {'; '.join(parts)}"


def _files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def decide_again_line(files: Iterable[tuple[bool, bool]]) -> str:
    """The one line of the job deciding files again after the upgrade that removed "Publish when",
    for the files whose decisions didn't change; the ones that did were logged file by file.

    Args:
        files: Per file it ran: whether its decisions changed, and whether a type is still in review.

    Returns:
        E.g. ``Decided again after the update (3 files): 2 changed (logged above); 1 unchanged, still needs review``.
    """
    results = list(files)
    changed = sum(1 for was_changed, _ in results if was_changed)
    same = len(results) - changed
    review = sum(1 for was_changed, in_review in results if not was_changed and in_review)
    parts = [f"{changed} changed (logged above)"] if changed else []
    if same:
        text = f"{same} unchanged"
        if review:
            verb = "needs" if review == 1 else "need"
            text += f", still {verb} review" if review == same else f", {review} still {verb} review"
        parts.append(text)
    return f"Decided again after the update ({_files(len(results))}): {'; '.join(parts) or 'nothing to decide'}"


def online_recheck_line(files: Iterable[tuple[bool, bool]]) -> str:
    """The one line of the weekly job asking the online databases again about files they had no entry for; the files
    newly found or whose decisions changed were logged file by file.

    Args:
        files: Per file it ran: whether an online database it asked now has an entry for it, and whether its decisions
            changed.

    Returns:
        E.g. ``Weekly online re-check (5 files): 2 newly found online, 3 unchanged``.
    """
    results = list(files)
    found = sum(1 for newly_found, _ in results if newly_found)
    changed = sum(1 for newly_found, was_changed in results if was_changed and not newly_found)
    parts = [f"{found} newly found online"]
    if changed:
        parts.append(f"{changed} changed otherwise")
    parts.append(f"{len(results) - found - changed} unchanged")
    return f"Weekly online re-check ({_files(len(results))}): {', '.join(parts)}"


# File outcomes the totals line names only when some file had them, in this order.
_OTHER_OUTCOMES = (
    (FileOutcome.UP_TO_DATE, "already up to date"),
    (FileOutcome.WAITING, "waiting for a server"),
    (FileOutcome.SKIPPED, "skipped"),
    (FileOutcome.NO_OWNERS, "on no server with Intro & Credits on"),
    (FileOutcome.FILE_NOT_FOUND, "not on disk"),
    (FileOutcome.SOURCE_GONE, "gone from disk"),
    (FileOutcome.FAILED, "failed"),
)


def totals_line(outcome: Mapping[str, int], sent: Mapping[str, int], skipped_sources: Mapping[str, int]) -> str:
    """The line a job ends its log with.

    Args:
        outcome: Files per ``FileOutcome`` value (the job's counts, files finished before a restart included).
        sent: Per server name, the files this job wrote markers to it for (0 for a server it wrote nothing to).
        skipped_sources: Per online source label, the files checked without it (daily limit or refused key).

    Returns:
        E.g. ``Done: 1 file · 0 sent to Plex · 1 needs review · 0 nothing found · TheIntroDB skipped for 1 file``.
    """
    total = sum(count for count in outcome.values() if isinstance(count, int))
    review = outcome.get(FileOutcome.NEEDS_REVIEW.value, 0)
    parts = [f"Done: {_files(total)}"]
    parts += [f"{count} sent to {name}" for name, count in sorted(sent.items())]
    parts.append(f"{review} {'needs' if review == 1 else 'need'} review")
    parts.append(f"{outcome.get(FileOutcome.NO_MARKERS.value, 0)} nothing found")
    parts += [f"{outcome[key.value]} {words}" for key, words in _OTHER_OUTCOMES if outcome.get(key.value)]
    parts += [f"{label} skipped for {_files(count)}" for label, count in sorted(skipped_sources.items()) if count]
    return _SEP.join(parts)
