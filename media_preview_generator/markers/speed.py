"""Playback speed families: film-rate and 25 fps releases of the same show (a PAL speed-up).

A 25 fps release of a 23.976 fps show plays every frame and every sound 25 / 23.976 (4.3 %) faster, pitch raised with
it: Bones seasons 5-8 on the owner's library mix 25 fps WEB releases with 23.976 Blu-rays, and IntroDB's times for
them come from a 23.976 release. Two speeds are handled, named by their nominal frame rate: film (23.976 and 24 fps)
and PAL (25 fps). Any other rate (29.97, 30, 50, ...) is left alone: nothing measured says how its audio relates to
the others'.
"""

from __future__ import annotations

from collections.abc import Iterable

FILM_FPS = 24000 / 1001
PAL_FPS = 25.0
# How far a probed rate may be from a nominal one: containers report 23.976 as 24000/1001, 23.98 or 2997/125.
_RATE_TOLERANCE = 0.05
_FAMILIES = ((FILM_FPS, FILM_FPS), (24.0, FILM_FPS), (PAL_FPS, PAL_FPS))


def playback_speed(frame_rate: float | None) -> float | None:
    """The speed a file plays at, named by its family's nominal frame rate.

    Args:
        frame_rate: The file's video frame rate (None when unknown).

    Returns:
        ``FILM_FPS`` for 23.976 and 24 fps, ``PAL_FPS`` for 25 fps, None for any other or unknown rate.
    """
    if not frame_rate:
        return None
    for rate, family in _FAMILIES:
        if abs(frame_rate - rate) <= _RATE_TOLERANCE:
            return family
    return None


def match_speed(speeds: Iterable[float | None]) -> float | None:
    """The speed a season group is matched at.

    Args:
        speeds: Each file's :func:`playback_speed`.

    Returns:
        None when the files of known speed all play at one speed (nothing is retimed), else the speed most of them
        play at, film on a tie.
    """
    film = pal = 0
    for value in speeds:
        film += value == FILM_FPS
        pal += value == PAL_FPS
    if not (film and pal):
        return None
    return PAL_FPS if pal > film else FILM_FPS


def retime_factor(speed: float | None, clock: float | None) -> float | None:
    """How many of a file's own seconds pass per second of its group's clock.

    A file's fingerprint is made with its audio retimed by this factor, so it plays at the group's speed; a time
    matched on that fingerprint times the factor is the file's own time.

    Args:
        speed: The file's :func:`playback_speed`.
        clock: The group's :func:`match_speed`.

    Returns:
        ``clock / speed``, or None when the file plays at the clock's speed or either is unknown.
    """
    if speed is None or clock is None or speed == clock:
        return None
    return clock / speed


def online_time_scale(frame_rate: float | None) -> float | None:
    """What an online database's times are multiplied by to read them on a file's own clock, when they were taken
    from a release at the other speed.

    Args:
        frame_rate: The file's video frame rate.

    Returns:
        23.976 / 25 on a 25 fps file (the database's release was film-rate), 25 / 23.976 on a film-rate file (it was a
        25 fps one); None for any other or unknown rate.
    """
    own = playback_speed(frame_rate)
    if own is None:
        return None
    other = FILM_FPS if own == PAL_FPS else PAL_FPS
    return other / own
