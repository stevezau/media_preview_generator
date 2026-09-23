"""Plain-words job log lines for Intro & Credits: what each source answered, what was decided and what was sent where.

One file's run logs one entry of two lines (``file_lines``): what was sent where and decided, then every source's
answer, e.g. (each line wrapped here)::

    Rick and Morty (2013) S01E01: nothing sent to Plex · intro 2:07–2:36 found by IntroDB → needs review (only IntroDB
        has the intro; an online answer needs a check against the file) · credits not found
      sources: chapters none · TheIntroDB skipped (daily limit reached, resets 00:00 UTC) · IntroDB intro 2:07–2:36 ·
        Plex's own none

A Season job logs one line per season instead of one per unchanged episode (``season_line``), a job deciding files
again after the update one line instead of one per unchanged file (``decide_again_line``), the weekly online re-check
one line instead of one per file nothing new was found for (``online_recheck_line``), and every job ends with a totals
line (``totals_line``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from .decide import DecisionStatus, TypeDecision, shortened_by
from .external_ids import ids_from_path, is_season_folder
from .models import SERVER_SOURCES, MarkerType, Source
from .outcomes import NOT_IN_LIBRARY, PLEX_PASS_UNKNOWN, FileOutcome, ServerStatus, is_kept_own
from .sources.online import is_budget_exhausted
from .sources.ratelimit import RESET_TIME_LABEL
from .store import EvidenceRow

# The names the job summary's "Decided by" counts use (web/static/js/app.js MARKER_SOURCE_NAMES).
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
# An answer this file's run used without asking the source (stored by an earlier run, or by a sibling's season step).
SAVED = "(saved earlier)"
SOURCES_PREFIX = "  sources: "
_SEP = " · "
# Release tags and ids in a folder or file name ("{tvdb-275274}", "[imdbid-tt0944947]", "[1080p]").
_TAG_RE = re.compile(r"\s*[\{\[][^\}\]]*[\}\]]")
# How a job that logs one line per season names itself there: a Season job, or the job that checks files TheIntroDB's
# used-up daily budget refused again after the reset.
SEASON_RECHECK_LABEL = "Season re-check"
BUDGET_RECHECK_LABEL = "TheIntroDB recheck"


@dataclass
class RunNotes:
    """What one file's run did with each source, kept across its stages (the checking thread, then a worker).

    Attributes:
        asked: ``(source, origin)`` read or asked during this job (origin: a server's id for its markers, else "").
        unanswered: Sources asked this job whose answer couldn't be stored, with what happened ("unavailable (HTTP
            503)", "no answer this time (…)").
        not_asked: Why a source wasn't asked ("not needed", "not read: every server keeps its own credits").
    """

    asked: set[tuple[Source, str]] = field(default_factory=set)
    unanswered: dict[Source, str] = field(default_factory=dict)
    not_asked: dict[Source, str] = field(default_factory=dict)

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
    """One server's row for one file, with what of ours it has, which types it keeps as its own, and which of those
    this file decided (so ours of that type wasn't written there)."""

    row: Mapping
    ours: frozenset[MarkerType] = frozenset()
    kept: frozenset[MarkerType] = frozenset()
    withheld: frozenset[MarkerType] = frozenset()


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


def _span(start_ms: int, end_ms: int | None) -> str:
    return f"{clock(start_ms)}–{clock(end_ms)}" if end_ms is not None else f"from {clock(start_ms)}"


def _clean(name: str) -> str:
    return " ".join(_TAG_RE.sub("", name).split()).strip(" -._")


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
    """How the job log names a file: ``Show SxxEyy`` for an episode, the file's name for anything else.

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
    return " and ".join(t.value for t in MarkerType if t in chosen)


def _labels(sources: Iterable[str]) -> list[str]:
    names = []
    for source in sources:
        try:
            names.append(SOURCE_LABELS[Source(source)])
        except ValueError:
            names.append(source)
    return list(dict.fromkeys(names))


def type_phrase(decision: TypeDecision) -> str:
    """One marker type's result in plain words.

    Args:
        decision: The type's decision.

    Returns:
        E.g. ``credits 58:23–59:04 (TheIntroDB + credit text agree)``, ``intro not found`` or ``credits 47:36–48:38
        found by credit text → needs review (…)``.
    """
    mtype = decision.type.value
    if decision.status is DecisionStatus.DECIDED and decision.marker is not None:
        marker = decision.marker
        names = _labels(marker.decided_by)
        if marker.locked:
            why = "locked by you"
        elif len(names) > 1:
            why = f"{' + '.join(names)} agree"
        else:
            why = f"from {names[0]}" if names else decision.reason
        if not marker.locked and shortened_by(decision.reason) is not None:
            why += ", start moved to the server's own marker"
        return f"{mtype} {_span(marker.start_ms, marker.end_ms)} ({why})"
    if decision.status is DecisionStatus.NEEDS_REVIEW:
        reason = decision.reason
        if decision.proposed is None:
            return f"{mtype} → needs review ({reason})"
        found = " + ".join(_labels(decision.proposed.decided_by))
        span = _span(decision.proposed.start_ms, decision.proposed.end_ms)
        return f"{mtype} {span} found by {found} → needs review ({reason})"
    if decision.status is DecisionStatus.NO_EVIDENCE:
        return f"{mtype} not found" if decision.reason == "no evidence" else f"{mtype} not found ({decision.reason})"
    return f"{mtype}: {decision.reason}"


def review_note(decisions: Mapping[MarkerType, TypeDecision], types: Collection[MarkerType]) -> str:
    """What a file's types in review were found by, for a Season job's summary.

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
        names = _labels(decision.proposed.decided_by) if decision.proposed else []
        if len(names) == 1:
            notes.append(f"{mtype.value} from {names[0]} only")
        elif names:
            notes.append(f"{mtype.value} from {' + '.join(names)}")
        else:
            notes.append(mtype.value)
    return ", ".join(notes)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def server_phrase(result: ServerResult) -> str:
    """What happened on one server for one file, in plain words.

    Args:
        result: The server's row, what of ours it has now and the types it keeps as its own.

    Returns:
        E.g. ``sent intro and credits to Plex``, ``nothing sent to Plex``, ``waiting for Plex to add the file to its
        library``, each followed by the types it keeps as its own.
    """
    row = result.row
    name = str(row.get("server_name") or row.get("server_id") or "a server")
    status = row.get("status")
    message = str(row.get("message") or "")
    ours = _types(result.ours)
    if status == ServerStatus.WRITTEN.value:
        text = f"sent {ours} to {name}" if ours else f"cleared our markers from {name}"
    elif status == ServerStatus.UP_TO_DATE.value:
        text = f"{name} already has our {ours}" if ours else f"nothing sent to {name}"
    elif status in (ServerStatus.NEEDS_REVIEW.value, ServerStatus.NONE.value):
        text = f"nothing sent to {name}"
    elif status == ServerStatus.WAITING.value:
        code = row.get("reason_code")
        if code == NOT_IN_LIBRARY:
            text = f"waiting for {name} to add the file to its library"
        elif code == PLEX_PASS_UNKNOWN:
            text = f"waiting for {name} (couldn't confirm Plex Pass)"
        else:
            text = f"sent {ours} to {name}, {_lower_first(message)}" if ours else f"{name}: {_lower_first(message)}"
    elif status == ServerStatus.SKIPPED.value:
        text = f"{name} skipped ({message})"
    else:
        text = f"{name} failed ({message})"
    if result.kept:
        vendor = str(row.get("server_type") or "").capitalize() or name
        text += f'; {name} keeps its own {_types(result.kept)} ("Keep {vendor}\'s")'
        if result.withheld:
            text += f", our {_types(result.withheld)} not written"
    return text


def _skip_reason(label: str, detail: str) -> str:
    if is_budget_exhausted(detail):
        return f"daily limit reached, resets {RESET_TIME_LABEL}"
    return detail.removeprefix(f"{label} ")


def _answer(rows: list[EvidenceRow], empty: str) -> str:
    typed = sorted(
        {(r.type, r.start_ms, r.end_ms) for r in rows if r.type is not None and r.start_ms is not None},
        key=lambda t: (list(MarkerType).index(t[0]), t[1], t[2] if t[2] is not None else -1),
    )
    return ", ".join(f"{mtype.value} {_span(start, end)}" for mtype, start, end in typed) or empty


def source_answers(
    order: Iterable[str],
    rows: list[EvidenceRow],
    servers: Iterable[tuple[str, str]],
    notes: RunNotes,
    skipped: Mapping[Source, str],
    server_details: Mapping[str, str],
) -> list[str]:
    """Every enabled source's answer for one file, in the user's order.

    Args:
        order: The enabled sources' ids in the user's order (``GlobalMarkersSettings.ordered_enabled_sources``).
        rows: The file's stored evidence (``MarkerStore.evidence_rows``).
        servers: ``(id, name)`` of every server that has the file (their markers are read whatever their Intro &
            Credits switch says).
        notes: What this job did with each source for the file.
        skipped: Sources the file was checked without for the whole job's reason, with the answer that stopped them.
        server_details: Plain words for a server's stored detail that says its markers weren't usable.

    Returns:
        E.g. ``["chapters none (saved earlier)", "TheIntroDB skipped (daily limit reached, resets 00:00 UTC)",
        "credit text credits from 47:36", "Plex's own none"]``.
    """
    parts = []
    for source_id in order:
        try:
            source = Source(source_id)
        except ValueError:
            continue
        if source is Source.SERVER_MARKERS:
            parts.extend(_server_answers(rows, servers, notes, server_details))
            continue
        parts.append(_source_answer(source, rows, notes, skipped))
        if source is Source.SEASON_AUDIO and any(r.source is Source.SEASON_AUDIO_PREVIOUS for r in rows):
            parts.append(_source_answer(Source.SEASON_AUDIO_PREVIOUS, rows, notes, skipped))
    return parts


def _source_answer(source: Source, rows: list[EvidenceRow], notes: RunNotes, skipped: Mapping[Source, str]) -> str:
    label = SOURCE_LABELS[source]
    if source in skipped:
        return f"{label} skipped ({_skip_reason(label, skipped[source])})"
    if source in notes.unanswered:
        return f"{label} {notes.unanswered[source]}"
    own = [r for r in rows if r.source is source and r.origin == ""]
    if own:
        answer = _answer(own, _EMPTY_ANSWER.get(source, "none"))
        return f"{label} {answer}" if (source, "") in notes.asked else f"{label} {answer} {SAVED}"
    return f"{label} {notes.not_asked.get(source, 'not asked')}"


def _server_answers(
    rows: list[EvidenceRow], servers: Iterable[tuple[str, str]], notes: RunNotes, server_details: Mapping[str, str]
) -> list[str]:
    parts = []
    for server_id, name in servers:
        own = [r for r in rows if r.source in SERVER_SOURCES and r.origin == server_id]
        if not own:
            parts.append(f"{name}'s own not read")
            continue
        imported = any(r.source is Source.SERVER_MARKERS_IMPORTED for r in own)
        label = f"{name}'s imported markers" if imported else f"{name}'s own"
        unusable = next((server_details[r.detail] for r in own if r.detail in server_details), None)
        answer = unusable or _answer(own, "none")
        earlier = "" if (Source.SERVER_MARKERS, server_id) in notes.asked else f" {SAVED}"
        parts.append(f"{label} {answer}{earlier}")
    return parts


def file_lines(
    canonical_path: str,
    *,
    decisions: Mapping[MarkerType, TypeDecision],
    types: Collection[MarkerType],
    servers: Iterable[ServerResult],
    sources: Iterable[str],
) -> str:
    """The job log's two lines for one file: what was sent where and decided, then every source's answer.

    Args:
        canonical_path: The file's local path.
        decisions: The file's decisions.
        types: The types detected for the file (a locked type is shown whatever this says).
        servers: Each server's result for the file, in registry order.
        sources: Every source's answer (``source_answers``).

    Returns:
        Both lines, joined by a newline.
    """
    shown = [
        decisions[mtype]
        for mtype in MarkerType
        if mtype in decisions
        and (mtype in types or (decisions[mtype].marker is not None and decisions[mtype].marker.locked))
    ]
    results = [server_phrase(server) for server in servers]
    decided = [type_phrase(decision) for decision in shown] or ["nothing to detect for this file"]
    first = f"{display_name(canonical_path)}: {_SEP.join([*results, *decided])}"
    return f"{first}\n{SOURCES_PREFIX}{_SEP.join(sources) or 'none enabled'}"


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
