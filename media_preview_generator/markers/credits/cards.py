"""Prose cards at the credits start (spec §5.4, "Prose cards"): an epilogue's sentences on black glued onto the roll.

Rule J reads boxes, not words, so an epilogue card on black ("Robert Hanssen is now serving a life sentence...") that
touches the roll is its first card, and the start lands on it (A Beautiful Imperfection, Accused, Breach: 11-71 s
early). The detector reads the card the start lands on: 1 fps frames after the start split into cards
(:func:`cards`), each card read once at full size (``textrec``), and while a card reads as prose (:func:`is_prose`) the
start moves on to the next card (:func:`past_prose`). Pure: the detector decodes and reads.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import rule_j
from .rule_j import Row, _inside, boxes_of

# The card the start lands on is found in this much of the file from the start (1 fps): the longest card measured stays
# 20 s (A Beautiful Imperfection's epilogue, its sentences appearing one by one).
FIRST_CARD_WINDOW_S = 30.0
# How far after the start the cards are read: twice the longest epilogue measured (Gandhari's cards run 46 s from the
# start to the first credit card). A start whose cards are still prose there stays where it was.
CARD_WINDOW_S = 90.0
# A card is read at this many times 320x180 (1280x720): through the app's own decode it calls every one of M2's 84
# cards as 1920x1080 does (10 of 11 epilogue cards prose, 1 of 41 first credit cards), at under half the CPU time.
READ_SCALE = 4
# Prose (M2): a sentence (ending in a full stop at the end of a line), or a line of this many words or more, most of
# them starting lower-case.
PROSE_WORDS = 7
# ... where a sentence runs from the last line-end full stop and holds at least this many words, and its full stop
# isn't an initialism's: a producer's "p.g.a." (Jay Kelly's first card), "U.S." or a lone name's "Jr." end credits too.
SENTENCE_WORDS = 4
# ... and fills its lines, this many words to a line on average: wrapped prose does, while a list of names and roles
# whose line ends in a full stop doesn't ("CHRIS EUBANK JR." after 12 names, a crawl's dot leaders "Travis....").
SENTENCE_WORDS_PER_LINE = 3
_WORD = re.compile(r"[^\W\d_]+")
# Letters and full stops in turn, at least twice: "p.g.a.", "U.S.", "D.C." (an ellipsis, "KIDNAPPED...DAILY.", isn't).
_INITIALISM = re.compile(r"^(?:[^\W\d_]{1,2}\.){2,}$")
_CLOSERS = "\"'”’»)]"


@dataclass(frozen=True)
class Card:
    """Text that stays on screen: the 1 fps seconds from its first to its last.

    Attributes:
        first_s: Its first second.
        last_s: Its last second.
        read_s: The second it is read at: the middle one of those holding the most boxes, when the whole card is on
            screen (its lines can appear one after another, and fade).
        dark: Its read second is dark (rule J's dark frame): a card on black.
    """

    first_s: float
    last_s: float
    read_s: float
    dark: bool


def _within(boxes: Sequence, holders: Sequence) -> bool:
    return all(any(_inside(box, holder) >= rule_j.OVERLAY_CONTAINMENT for holder in holders) for box in boxes)


def _continues(before: Sequence, after: Sequence) -> bool:
    """Whether one second's text is the same card as the second before: one second's boxes all lie mostly inside the
    other's. Lines appear one after another (A Beautiful Imperfection's epilogue) or fade out on the same card; text
    that moved, or other text elsewhere on screen, starts another card (a crawl moves every second)."""
    return _within(before, after) or _within(after, before)


def cards(rows: Sequence[Row], start_s: float) -> list[Card]:
    """The cards from ``start_s``: consecutive 1 fps seconds with text, split where a second's text is gone or moved.

    Args:
        rows: 1 fps rows (overlays left out), any order.
        start_s: The credits start.

    Returns:
        The cards, in time order.
    """
    found: list[Card] = []
    current: list[Row] = []

    def close() -> None:
        if current:
            most = max(len(boxes_of(row)) for row in current)
            fullest = [row for row in current if len(boxes_of(row)) == most]
            read = fullest[len(fullest) // 2]
            found.append(Card(current[0][0], current[-1][0], read[0], read[2] < rule_j.RULE_J.dark))
            current.clear()

    for row in sorted((row for row in rows if row[0] >= start_s), key=lambda row: row[0]):
        if not boxes_of(row):
            close()
            continue
        if current and not _continues(boxes_of(current[-1]), boxes_of(row)):
            close()
        current.append(row)
    close()
    return found


def _ends_a_sentence(token: str) -> bool:
    """A word ending in a full stop (a closing quote or bracket after it aside) that isn't an initialism."""
    token = token.rstrip(_CLOSERS)
    return token.endswith(".") and not _INITIALISM.match(token)


def is_prose(lines: Sequence[str]) -> bool:
    """Whether a card's text reads as prose (M2: 10 of 11 epilogue cards, 1 of 41 first credit cards, itself an
    epilogue's sentence): a sentence of at least ``SENTENCE_WORDS`` words and ``SENTENCE_WORDS_PER_LINE`` to a line, or
    a line of at least ``PROSE_WORDS`` words, more than half of them starting lower-case.

    Args:
        lines: The card's lines as read, top to bottom.

    Returns:
        True for prose.
    """
    sentence = sentence_lines = 0
    for line in lines:
        tokens = line.split()
        sentence += len(tokens)
        sentence_lines += 1
        if tokens and _ends_a_sentence(tokens[-1]):
            if sentence >= SENTENCE_WORDS and sentence >= SENTENCE_WORDS_PER_LINE * sentence_lines:
                return True
            sentence = sentence_lines = 0
        words = _WORD.findall(line)
        if len(words) >= PROSE_WORDS and 2 * sum(word[0].islower() for word in words) > len(words):
            return True
    return False


def past_prose(
    first: Card | None, rest: Callable[[], Sequence[Card]], read: Callable[[Card], Sequence[str]]
) -> float | None:
    """Where the credits start once the prose cards at the start are passed over.

    Only a start on a card on black is read: epilogue text sits on black. When that card reads as prose, the cards
    after it are read in turn while they do, and the start moves to the first card that doesn't. A start whose card
    isn't prose stays, and so does one whose cards are all prose up to the end of the window: the roll isn't in sight.
    After the first card, a card on screen for a single second is text on the move, a crawl: the roll itself, never
    read, so the start never moves past the point where the roll clearly continues (Paris, Texas: the cast crawl after
    the dedication). The first card is read however long it stays: the second the start lands on can be its fade-in
    alone (#SKYKING's disclaimer fades in over one second, then stays for 17).

    Args:
        first: The card at the start, or None when there is none.
        rest: The cards after it, asked for only when it reads as prose (the caller decodes them then).
        read: A card's lines, read at full size.

    Returns:
        The first second of the first card after the prose ones, or None to keep the start.
    """
    if first is None or not first.dark or not is_prose(read(first)):
        return None
    for card in rest():
        if _moving(card) or not is_prose(read(card)):
            return card.first_s
    return None


def _moving(card: Card) -> bool:
    return card.last_s == card.first_s
