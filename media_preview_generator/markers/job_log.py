"""Plain-words job log lines for Intro & Credits: what each source answered, what was decided and what was sent where.

Every line is its own log record, logged the moment that step happens -- not batched until the file finishes. A
worker's pickup line names the file and what it's checked for in one line; every line after that for the same file
starts with its short title and " · ", so lines from several workers (or the checking thread and a worker) can
interleave in the log and still be read one file at a time (``titled``)::

    GPU Worker 2 (Intel UHD 770) picked up: Accused S04E05, checking intro and credits
    Accused S04E05 · Checking chapters… none
    Accused S04E05 · Checking IntroDB… intro 0:41–1:12 (asked now)
    Accused S04E05 · Checking season audio… intro 0:41–1:12 (same theme found in 9 of 10 episodes)
    Accused S04E05 · Reading credit text on the GPU (Intel UHD 770)…
    Accused S04E05 · Credit text: credits start at 41:48 (13 s)
    Accused S04E05 · Checking Plex's own markers… none (asked now)
    Accused S04E05 · Decided: intro 0:41–1:12 (IntroDB and season audio agree) · credits 41:48–43:10 (credit text)
    Accused S04E05 · [Plex] Added intro 0:41–1:12 and credits 41:48–43:10
    GPU Worker 2 (Intel UHD 770) completed: Accused S04E05 (success, 26 s)

A file with no worker (everything decided on the checking thread) opens with its own line instead of a pickup line
("Accused S04E05: checking intro and credits") and ends with "Accused S04E05 · done in 0.5 s" rather than a worker's
"completed" line. A step that reads the file itself (a credit-text read, a season-audio fingerprint) logs a
"Reading … on the GPU/CPU…" line when it starts and its result as its own line when it finishes; a fallback, a retry
or an error is logged live too, with the reason.

Every file gets the same lines, whatever it did: an unchanged file, a Season job's unchanged episode, a decide-again
job's unchanged file and a weekly online re-check's file with nothing new all log every source's check too -- a
source's own answer this job didn't ask for again shows the stored answer with "saved <date>" it was last stored
(``_saved_note``), and a source not read or not asked always says why. A Season job (and a TheIntroDB recheck)
additionally counts its episodes into one line per season after every episode's lines (``season_line``), a job
deciding files again after the update into one line (``decide_again_line``), the weekly online re-check into one
line (``online_recheck_line``); every job starts with ``start_line`` and ends with a totals line (``totals_line``),
after every file's lines and summary line.
"""

from __future__ import annotations

import os
import re
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
# How a source's own line names it, mid-sentence after "Checking " (a proper noun keeps its own capitals).
_CHECKING_LABEL: dict[Source, str] = {
    Source.CHAPTERS: "chapters",
    Source.THEINTRODB: "TheIntroDB",
    Source.INTRODB: "IntroDB",
    Source.SKIPDB: "SkipDB",
    Source.SEASON_AUDIO: "season audio",
    Source.SEASON_AUDIO_PREVIOUS: "last season's audio",
    Source.CREDITS_TEXT: "credit text",
}
# The two local detectors that read the file itself for long enough to get their own "Reading … on the GPU/CPU…"
# start line and a separate result line, rather than one "Checking …" line.
_READING_LABEL: dict[Source, str] = {
    Source.CREDITS_TEXT: "credit text",
    Source.SEASON_AUDIO: "season audio",
}
# Sources that describe TV episodes only: a film's block leaves them out (its first line says films get credits only).
EPISODE_ONLY_SOURCES = frozenset({Source.INTRODB, Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS})
# Sources that read the file itself ("not read") rather than being asked ("not asked"), and the types each can answer.
_READS_FILE: dict[Source, frozenset[MarkerType]] = {
    Source.SEASON_AUDIO: frozenset({MarkerType.INTRO}),
    Source.SEASON_AUDIO_PREVIOUS: frozenset({MarkerType.INTRO}),
    Source.CREDITS_TEXT: frozenset({MarkerType.CREDITS}),
}
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
# An answer this file's run used without asking the source (stored by an earlier run, or by a sibling's season step),
# when the source's rows don't say when (``_saved_note`` prefers the stored date).
SAVED = "saved earlier"
# A source's own answer this job asked for and got, when nothing more specific (a read's time and device) says so.
ASKED_NOW = "asked now"
# A server's stored answer this job cleared without asking the server (an older reader's answer, dropped once the
# server shows our markers: ``_drop_older_reader_answer``). The server was never contacted, so this never says
# "asked now".
DROPPED_NOW = "dropped now"
_SEP = " · "
_DOT = " · "
# The decision reason of a type a chapter decides alone (``decide._chapter_decision``).
_CHAPTER_RULE = "chapters"
# Release tags and ids in a folder or file name ("{tvdb-275274}", "[imdbid-tt0944947]", "[1080p]").
_TAG_RE = re.compile(r"[\{\[][^\}\]]*[\}\]]")
# A resolution or cut a file's own name gives ("Heat (1995) - [Bluray-1080p]…", "…2160p…", "…Extended…"): the only
# tell-apart a job log line can afford for two copies of the same film sharing a title -- data already in the path,
# never a lookup.
_VERSION_TAG_RE = re.compile(
    r"\b(2160p|1080p|720p|480p|4K|UHD|Extended|Director'?s Cut|Unrated|Remastered)\b", re.IGNORECASE
)
# A release group after the tags ("…[h264]-cinepth"): dropped with them.
_GROUP_RE = re.compile(r"(?<=[\]\}])-[^\s\[\]\{\}()]+$")
# Separators left behind once the tags are gone ("(2026) - -", "--").
_SEPARATOR_RUN_RE = re.compile(r"\s*-(?:\s*-)+\s*")
# A release year in a file or folder name ("Heat (1995)").
_YEAR_RE = re.compile(r"\(((?:19|20)\d\d)\)")
# A worker's display name without its device ("GPU Worker 2 (Intel UHD 770)" → "GPU Worker 2").
_DEVICE_RE = re.compile(r"\s*\(([^()]*)\)$")
# How a job that logs one line per season names itself there: a Season job, or the job that checks files TheIntroDB's
# used-up daily budget refused again after the reset.
SEASON_RECHECK_LABEL = "Season re-check"
BUDGET_RECHECK_LABEL = "TheIntroDB recheck"
# A marker carried over from a replaced file names no source (``carry_over.CARRIED_OVER``; web/static/js/app.js too).
CARRIED_OVER_LABEL = "the file it replaced"


@dataclass
class RunNotes:
    """What one file's run did with each source, kept across its stages (the checking thread, then a worker).

    Attributes:
        asked: ``(source, origin)`` read or asked during this job (origin: a server's id for its markers, else "").
        unanswered: Sources asked this job whose answer couldn't be stored, with what happened ("unavailable (HTTP
            503)", "no answer this time (…)").
        not_asked: Why a source wasn't asked (``ALREADY_DECIDED``, "not read (every server keeps its own credits)").
        server_not_read: Why a server's own markers were never read for this file at all ("this server shows our
            markers"), keyed by server id; consulted only when the file has no evidence row for that server.
        title: How the log names the file (``file_title``); "" until a stage needs it.
        types: The types the file is checked for, once a stage knew them (the pickup line names them).
        is_episode: Whether the file was checked as a TV episode, once a stage knew it.
        started: When the stage that finishes the file started (``PipelineContext.monotonic``).
        worker: The display name of the worker running that stage; "" on a checking thread.
        cpu_rerun: Whether that stage is a GPU worker's rerun of the file on the CPU.
        sent_before: Per server id, what we last published there before this run's write (``None`` when we never
            published there at all, so a written status falls back to what the server's own evidence says it had).
        start_logged: Whether the file's own start line (no worker) was already written this attempt, so a retry on
            the same checking thread doesn't announce it twice.
        logged_sources: Each ``(source, origin)`` (origin: a server's id for its own markers, else "") a line was
            already logged for this run, whether its own line or its read (``reading_line``/``read_result_line``): a
            worker picking up where the checking thread left off, with nothing new to say about a source, doesn't
            repeat its line (a source actually re-read this stage marks the key itself, live, so a genuine change is
            never held back by it -- it's simply never checked against this set in the first place).
        dropped: Each ``(Source.SERVER_MARKERS, server_id)`` whose stored answer this run cleared without asking the
            server (``_drop_older_reader_answer``): its line says "dropped now", never "asked now" -- the server was
            never contacted.
        logged: Whether the file's lines were written.
    """

    asked: set[tuple[Source, str]] = field(default_factory=set)
    unanswered: dict[Source, str] = field(default_factory=dict)
    not_asked: dict[Source, str] = field(default_factory=dict)
    server_not_read: dict[str, str] = field(default_factory=dict)
    title: str = ""
    types: frozenset[MarkerType] | None = None
    is_episode: bool | None = None
    started: float | None = None
    worker: str = ""
    cpu_rerun: bool = False
    sent_before: dict[str, tuple[Marker, ...] | None] = field(default_factory=dict)
    start_logged: bool = False
    logged_sources: set[tuple[Source, str]] = field(default_factory=set)
    dropped: set[tuple[Source, str]] = field(default_factory=set)
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
        # A fresh answer, even to a key an earlier stage of this run already logged a line for (``logged_sources``):
        # the line that answer earns isn't held back as a repeat of one that no longer describes it.
        self.logged_sources.discard((source, origin))

    def dropped_evidence(self, source: Source, origin: str = "") -> None:
        """Record that ``source``'s stored evidence was cleared this job without asking it (``origin``'s server was
        never contacted): the opposite of :meth:`answered`, so its line says "dropped now", not "asked now".

        Args:
            source: The source.
            origin: A server's id for its markers, else "".
        """
        self.dropped.add((source, origin))
        self.not_asked.pop(source, None)
        self.logged_sources.discard((source, origin))


@dataclass(frozen=True)
class ServerResult:
    """One server's row for one file, with our markers it shows now, which types it keeps as its own, which of those
    this file decided (so ours of that type wasn't written there), and what it held before this run's write.

    Attributes:
        had_is_ours: Whether ``had`` is our own last-published record (``notes.sent_before``), so a type missing
            from it now really was removed; False when it's a fallback (the server's own evidence, read this run or
            saved), which was never ours to call removed in the first place -- a type only there is simply left out.
    """

    row: Mapping
    ours: tuple[Marker, ...] = ()
    kept: frozenset[MarkerType] = frozenset()
    withheld: frozenset[MarkerType] = frozenset()
    had: tuple[Marker, ...] | None = None
    had_is_ours: bool = True


def write_line(text: str, level: str = "INFO") -> None:
    """Log one line as its own record.

    Args:
        text: The line.
        level: Its level.
    """
    logger.log(level, "{}", text)


def write_lines(lines: Iterable[str], level: str = "INFO") -> None:
    """Log each of several lines as its own record, in order.

    Args:
        lines: The lines, in order.
        level: Their level.
    """
    for line in lines:
        write_line(line, level)


def titled(title: str, text: str) -> str:
    """A line after a file's first, named so it can be told apart from another file's or worker's line interleaved
    with it in the log.

    Args:
        title: The file's title (``file_title``).
        text: The line's own content.

    Returns:
        E.g. ``Accused S04E05 · Checking chapters… none``.
    """
    return f"{title}{_DOT}{text}"


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


def version_tag(canonical_path: str) -> str | None:
    """A resolution or cut a file's own name gives, when its job log title needs a short tell-apart from another
    file in the same job that resolved to the same title (two copies of one film, say).

    Args:
        canonical_path: The file's local path.

    Returns:
        E.g. ``"1080p"``; None when the name gives none.
    """
    found = _VERSION_TAG_RE.search(os.path.basename(canonical_path))
    return found.group(1) if found else None


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


def worker_device(worker: str) -> str:
    """The device a worker's display name carries in parentheses ("GPU Worker 2 (Intel UHD 770)" → "Intel UHD 770");
    "" for a worker name with none (a CPU worker, or a checking thread's "" name)."""
    found = _DEVICE_RE.search(worker)
    return found.group(1) if found else ""


def file_start_line(title: str, types: Collection[MarkerType], *, is_episode: bool, worker: str = "") -> str:
    """The first line naming a file: a worker's pickup line naming what it's checked for too, or (with no worker) the
    file's own line, logged live as the checking begins rather than once the file finishes.

    Args:
        title: The file's title (``file_title``).
        types: The types the file is checked for.
        is_episode: Whether it is checked as a TV episode.
        worker: The display name of the worker running it; "" for a checking thread.

    Returns:
        E.g. ``GPU Worker 2 (Intel UHD 770) picked up: Accused S04E05, checking intro and credits`` or ``32 Frames: A
        9/11 Mystery (2026): checking credits (films get credits only)``.
    """
    phrase = checking_phrase(types, is_episode=is_episode)
    if worker:
        return f"{worker} picked up: {title}, {phrase}"
    return f"{title}: {phrase}"


def worker_completed_line(
    worker: str, title: str, seconds: float | None, *, status: str, reason: str = "", cpu_rerun: bool = False
) -> str:
    """The line a worker logs when it finishes a file, matching the preview log's own "completed" line.

    Args:
        worker: The worker's display name.
        title: The file's title.
        seconds: How long the worker's stage took; None when unknown.
        status: ``success``, ``failed`` or ``skipped``.
        reason: Why it failed or was skipped; "" for success.
        cpu_rerun: Whether it was a GPU worker's rerun on the CPU.

    Returns:
        E.g. ``GPU Worker 2 (Intel UHD 770) completed: Accused S04E05 (success, 26 s)`` or ``GPU Worker 1 completed:
        Heat (1995) (failed: Plex refused the write, 3 s)``.
    """
    detail = f": {reason}" if reason else ""
    took = f", {duration(seconds)}" if seconds is not None else ""
    rerun = ", rerun on the CPU" if cpu_rerun else ""
    return f"{worker} completed: {title} ({status}{detail}{rerun}{took})"


def done_line(seconds: float | None, *, failed: bool = False, nothing_sent: bool = False) -> str:
    """The last line of a file run on no worker (a checking thread alone decided everything).

    Args:
        seconds: How long it took; None when unknown.
        failed: Whether the file failed.
        nothing_sent: Whether nothing was written to any server this run (``nothing_was_sent``).

    Returns:
        E.g. ``done in 0.5 s``, ``failed after 3 s`` or ``done in 0.5 s (nothing new to send)``.
    """
    took = "" if seconds is None else f" {'after' if failed else 'in'} {duration(seconds)}"
    text = f"{'failed' if failed else 'done'}{took}"
    if nothing_sent and not failed:
        text += " (nothing new to send)"
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
    """The line of what was decided per type, and why, logged once every source has had its turn.

    Args:
        decisions: The file's decisions.
        types: The types detected for the file (a locked type is shown whatever this says).
        rows: The file's stored evidence, for the chapter names.

    Returns:
        E.g. ``Decided: intro 0:41–1:12 (IntroDB and season audio agree) · credits 41:48–43:10 (credit text)``.
    """
    names = chapter_names(rows)
    parts = [type_phrase(decision, names) for decision in _shown(decisions, types)]
    return f"Decided: {_SEP.join(parts) or 'nothing to detect for this file'}"


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


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


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


def _by_type(markers: tuple[Marker, ...] | None) -> dict[MarkerType, Marker] | None:
    return None if markers is None else {m.type: m for m in markers}


def _written_phrase(result: ServerResult, name: str) -> str:
    """What a written status says: added, replaced (old → new), unchanged or removed, per type, grouped by which it
    was.

    ``result.had`` is the server's prior value: our own last-published record (``RunNotes.sent_before``) when we've
    published there before -- ``result.had_is_ours`` True, so a type it's missing now really was removed, and a span
    that matches it is really unchanged -- else the server's own markers as its evidence read this run or saved says
    (a type it shows that we never sent isn't ours to call removed, so it's simply left out; a span that happens to
    match it is this type's first send, "added … (same as …'s own)", not "unchanged"), else None when neither is
    known.
    """
    new_by_type = {m.type: m for m in result.ours}
    if not new_by_type and not result.kept:
        return "Cleared our markers"
    had_by_type = _by_type(result.had)
    if had_by_type is not None:
        # A type the server keeps its own of was never ours to write: its prior value isn't a removal, it's
        # ``_kept_phrase``'s to describe.
        had_by_type = {t: m for t, m in had_by_type.items() if t not in result.kept}
    order = list(MarkerType)
    all_types = sorted({*new_by_type, *(had_by_type or {})}, key=order.index)
    added, added_same, replaced, unchanged, removed, unread = [], [], [], [], [], []
    for mtype in all_types:
        new = new_by_type.get(mtype)
        had = (had_by_type or {}).get(mtype)
        if new is not None and had is not None:
            if (had.start_ms, had.end_ms) != (new.start_ms, new.end_ms):
                replaced.append(f"{mtype.value} {_span(had.start_ms, had.end_ms)} → {_span(new.start_ms, new.end_ms)}")
            elif result.had_is_ours:
                unchanged.append(f"{mtype.value} {_span(new.start_ms, new.end_ms)}")
            else:
                # The span matches only because it's the server's own markers, never ours before now (decided from
                # them, say): this is the type's first send, not a write that changed nothing.
                added_same.append(f"{mtype.value} {_span(new.start_ms, new.end_ms)}")
        elif new is not None:
            entry = f"{mtype.value} {_span(new.start_ms, new.end_ms)}"
            (unread if had_by_type is None else added).append(entry)
        elif result.had_is_ours:
            removed.append(mtype.value)
        # else: only the server's own evidence shows this type -- never ours to send, so never ours to call removed
    if unchanged and not (added or added_same or replaced or removed or unread):
        # A WRITTEN row only happens when the write really changed the server (a basis mismatch): every type
        # matching what we last sent means it must have drifted since (a Plex rescan dropping markers, say), and
        # this write restored it -- neither "added" (nothing's new) nor merely "unchanged" (bytes were sent).
        return _upper_first(f"restored {_and(unchanged)}")
    parts = []
    if added:
        parts.append(f"added {_and(added)}")
    if added_same:
        parts.append(f"added {_and(added_same)} (same as {name}'s own)")
    if replaced:
        parts.append(f"replaced {_and(replaced)}")
    if unchanged:
        parts.append(f"unchanged {_and(unchanged)}")
    if removed:
        holds = "it no longer holds" if len(removed) == 1 else "they no longer hold"
        parts.append(f"removed {_and(removed)} ({holds})")
    if unread:
        parts.append(f"sent {_and(unread)} (what {name} had before wasn't read)")
    return _upper_first("; ".join(parts))


def server_result_line(result: ServerResult) -> str:
    """What happened on one server for one file, in the preview log's ``[<server>] …`` style.

    Args:
        result: The server's row, our markers it shows now, the types it keeps as its own and what it held before.

    Returns:
        E.g. ``[Plex] Added intro 0:41–1:12 and credits 41:48–43:10``, ``[Plex] Replaced credits 24:30 → 24:59``,
        ``[Plex] Removed intro (it no longer holds)``, ``[Plex] Already up to date (…)``, ``[Plex] Kept Plex's own
        credits``, ``[Plex] Failed (…)`` or ``[Plex] Not in Plex's library yet (…)``.
    """
    row = result.row
    name = _server_name(row)
    status = row.get("status")
    message = str(row.get("message") or "")
    ours = _markers(result.ours)
    kept = _kept_phrase(result, name) if result.kept else ""
    if status == ServerStatus.WRITTEN.value:
        text = _written_phrase(result, name)
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
    parts = [part for part in (text, kept) if part]
    body = "; ".join(parts)
    return f"[{name}] {_upper_first(body)}"


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


def _saved_note(rows: Iterable[EvidenceRow]) -> str:
    """When a reused answer was last stored, from its rows' own ``fetched_at`` (their date; ``SAVED`` when none say)."""
    dates = [r.fetched_at[:10] for r in rows if r.fetched_at]
    return f"saved {max(dates)}" if dates else SAVED


def make_source_view(
    rows: list[EvidenceRow],
    notes: RunNotes,
    skipped: Mapping[Source, str],
    decisions: Mapping[MarkerType, TypeDecision],
    types: Collection[MarkerType],
) -> _SourceView:
    """The state one source's (or one server's) line is built from, fetched fresh so it sees this run's own writes.

    Args:
        rows: The file's stored evidence (``MarkerStore.evidence_rows``), read again after each source answers.
        notes: What this job has done with each source for the file so far.
        skipped: Sources the file is checked without for the whole job's reason, with the answer that stopped them.
        decisions: The file's decisions so far.
        types: The types detected for the file.

    Returns:
        The view.
    """
    return _SourceView(rows, notes, skipped, decisions, types, chapter_names(rows))


def source_line(source: Source, view: _SourceView) -> str:
    """One enabled source's line, right when it has its turn: what it answered (this job or reused), or why it wasn't.

    Args:
        source: The source (not ``Source.SERVER_MARKERS``: see ``server_source_line``).
        view: The file's current state (``make_source_view``).

    Returns:
        E.g. ``Checking chapters… "Credits" chapter at 2:00:11–2:03:39``, ``Checking SkipDB… no entry (asked now)`` or
        ``Checking credit text… not read (a chapter named Credits is used as-is)``.
    """
    label = _CHECKING_LABEL[source]
    notes = view.notes
    if source in view.skipped:
        return f"Checking {label}… skipped ({_skip_reason(label, view.skipped[source])})"
    if source in notes.unanswered:
        return f"Checking {label}… {notes.unanswered[source]}"
    own = [r for r in view.rows if r.source is source and r.origin == ""]
    if own:
        extras = [ASKED_NOW] if (source, "") in notes.asked else [_saved_note(own)]
        return f"Checking {label}… {_answer(own, _EMPTY_ANSWER.get(source, 'none'), extras, view.chapters)}"
    note = notes.not_asked.get(source)
    if note == ALREADY_DECIDED:
        note = _already_decided(source, view.decisions, view.types, view.chapters)
    reason = note or ("not read" if source in _READS_FILE else "not asked")
    return f"Checking {label}… {reason}"


def reading_line(source: Source, *, on_gpu: bool, device: str = "") -> str:
    """The line logged right before a long local read starts (a credit-text read, a season-audio fingerprint).

    Args:
        source: ``Source.CREDITS_TEXT`` or ``Source.SEASON_AUDIO``.
        on_gpu: Whether it reads on the worker's GPU.
        device: The GPU's name, when known; "" for the CPU or when it isn't known.

    Returns:
        E.g. ``Reading credit text on the GPU (Intel UHD 770)…`` or ``Reading season audio on the CPU…``.
    """
    where = f"GPU ({device})" if on_gpu and device else ("GPU" if on_gpu else "CPU")
    return f"Reading {_READING_LABEL[source]} on the {where}…"


def gpu_failure_line(source: Source, reason: str, ffmpeg_lines: Iterable[str]) -> str:
    """Why a read on the worker's GPU failed, with what ffmpeg itself said, logged before the worker reads the file
    again on the CPU.

    Args:
        source: ``Source.CREDITS_TEXT`` or ``Source.SEASON_AUDIO``.
        reason: The failure, as the worker's CPU rerun names it.
        ffmpeg_lines: ffmpeg's last lines that say why (``frames.GpuDecodeError.stderr_tail``).

    Returns:
        E.g. ``Credit text: couldn't be read on the GPU (the GPU's decoder hit a hardware or driver error (ffmpeg
        exited 251)). FFmpeg's last lines: [hevc @ 0x1] Failed to sync surface 0x5 | [vist#0:0/hevc @ 0x2] Decoding
        error: Input/output error``.
    """
    label = "Credit text" if source is Source.CREDITS_TEXT else "Season audio"
    return f"{label}: couldn't be read on the GPU ({reason}). FFmpeg's last lines: {' | '.join(ffmpeg_lines)}"


def _read_result_note(seconds: float, fallback: str = "", *, gpu_read_nothing: bool = False) -> str:
    """The parenthesised note a read's result line ends with: just the time it took (the start line already said
    where), unless a step fell back to the CPU or the GPU read nothing and it was read again there."""
    if gpu_read_nothing:
        return f"read on the CPU after the GPU read nothing ({duration(seconds)} in all)"
    return f"{duration(seconds)}; {_lower_first(fallback)}" if fallback else duration(seconds)


def read_result_line(
    source: Source,
    unanswered: str | None,
    rows: list[EvidenceRow],
    chapters: Mapping[MarkerType, Mapping[int, str]],
    *,
    seconds: float,
    fallback: str = "",
    gpu_read_nothing: bool = False,
) -> str:
    """A long local read's result, logged as its own line once it finishes.

    Args:
        source: ``Source.CREDITS_TEXT`` or ``Source.SEASON_AUDIO``.
        unanswered: Why it had no answer to store this time; None when it did.
        rows: This source's own evidence rows (``origin == ""``), for the answer's own words.
        chapters: The file's chapter names, for credit text's own chapter check.
        seconds: How long the read took.
        fallback: What a step of it fell back to the CPU for; "" when none did.
        gpu_read_nothing: Whether the GPU read no frames and it was read again on the CPU.

    Returns:
        E.g. ``Credit text: credits start at 41:48 (13 s)`` or ``Credit text: the file ends before its stated length
        (10:00 of 22:01 readable)``.
    """
    label = "Credit text" if source is Source.CREDITS_TEXT else "Season audio"
    if unanswered is not None:
        return f"{label}: {unanswered}"
    note = _read_result_note(seconds, fallback, gpu_read_nothing=gpu_read_nothing)
    return f"{label}: {_answer(rows, _EMPTY_ANSWER[source], [note], chapters)}"


def last_seasons_audio_line(view: _SourceView) -> str | None:
    """ "Last season's audio" follows season audio's own line when the file has that answer; None when it doesn't.

    Args:
        view: The file's current state.

    Returns:
        The line, or None.
    """
    if not any(r.source is Source.SEASON_AUDIO_PREVIOUS for r in view.rows):
        return None
    return source_line(Source.SEASON_AUDIO_PREVIOUS, view)


def server_source_line(server_id: str, name: str, view: _SourceView, server_details: Mapping[str, str]) -> str:
    """One server's own-markers line, right when it has its turn.

    Args:
        server_id: The server's id.
        name: The server's display name.
        view: The file's current state.
        server_details: Plain words for a server's stored detail that says its markers weren't usable.

    Returns:
        E.g. ``Checking Plex's own markers… none (asked now)``, ``Checking Plex's own markers… not read (this server
        shows our markers)`` or ``Checking Plex's imported markers… intro 0:41–1:12 (saved 2026-09-25)``.
    """
    own = [r for r in view.rows if r.source in SERVER_SOURCES and r.origin == server_id]
    if not own:
        reason = view.notes.server_not_read.get(server_id, "not due this run")
        return f"Checking {name}'s own markers… not read ({reason})"
    imported = any(r.source is Source.SERVER_MARKERS_IMPORTED for r in own)
    label = f"{name}'s imported markers" if imported else f"{name}'s own markers"
    key = (Source.SERVER_MARKERS, server_id)
    if key in view.notes.asked:
        extras = [ASKED_NOW]
    elif key in view.notes.dropped:
        extras = [DROPPED_NOW]
    else:
        extras = [_saved_note(own)]
    unusable = next((server_details[r.detail] for r in own if r.detail in server_details), None)
    if unusable:
        return f"Checking {label}… {_with_notes(unusable, extras)}"
    return f"Checking {label}… {_answer(own, 'none', extras)}"


def nothing_was_sent(rows: Iterable[Mapping]) -> bool:
    """Whether every one of a file's server rows already had nothing new to send (the done line then says so).

    A waiting, failed or skipped row is something unsettled, not simply "nothing new": this is only true when
    every row is ``ServerStatus.UP_TO_DATE`` or ``ServerStatus.NONE`` (including no rows at all).

    Args:
        rows: The file's server rows.

    Returns:
        True when every row is ``ServerStatus.UP_TO_DATE`` or ``ServerStatus.NONE``.
    """
    settled = (ServerStatus.UP_TO_DATE.value, ServerStatus.NONE.value)
    return all(row.get("status") in settled for row in rows)


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
