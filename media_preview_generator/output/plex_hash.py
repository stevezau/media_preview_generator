"""Derive Plex's media bundle hash from a stable local source file.

For files of at least 64 KiB, Plex hashes the decimal size followed by the
hexadecimal SHA-1 digests of the first and last 64 KiB. Smaller files use the
size followed by the file's SHA-1 digest directly, without the outer hash.
These boundary rules were verified against Plex 1.43.4.10903's database and
API; the commonly cited forum recipe omits both small-file exceptions.
"""

from __future__ import annotations

import hashlib
import os
import stat
from functools import lru_cache

SourceFingerprint = tuple[int, int, int, int, int]
_BLOCK_SIZE = 65_536


class SourceFileChangedError(OSError):
    """The source changed while its preview or bundle hash was being built."""


def _fingerprint(info: os.stat_result) -> SourceFingerprint:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def get_source_fingerprint(path: str | os.PathLike[str]) -> SourceFingerprint:
    """Return identity and change markers for a regular media file.

    Args:
        path: Local media filename.

    Returns:
        Device, inode, size, modification time and change time.

    Raises:
        OSError: The source cannot be inspected or is not a regular file.
    """
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Media source is not a regular file: {path}")
    return _fingerprint(info)


@lru_cache(maxsize=1024)
def _hash_stable_source(path: str, expected: SourceFingerprint) -> str:
    with open(path, "rb") as source:
        if _fingerprint(os.fstat(source.fileno())) != expected:
            raise SourceFileChangedError(f"Media source changed before hashing: {path}")
        size = expected[2]
        first = source.read(_BLOCK_SIZE)
        if len(first) != min(size, _BLOCK_SIZE):
            raise SourceFileChangedError(f"Media source changed while hashing: {path}")
        signature = str(size) + hashlib.sha1(first, usedforsecurity=False).hexdigest()
        if size >= _BLOCK_SIZE:
            source.seek(size - _BLOCK_SIZE)
            last = source.read(_BLOCK_SIZE)
            if len(last) != _BLOCK_SIZE:
                raise SourceFileChangedError(f"Media source changed while hashing: {path}")
            signature += hashlib.sha1(last, usedforsecurity=False).hexdigest()
        if _fingerprint(os.fstat(source.fileno())) != expected:
            raise SourceFileChangedError(f"Media source changed while hashing: {path}")
    if size < _BLOCK_SIZE:
        return signature
    return hashlib.sha1(signature.encode("ascii"), usedforsecurity=False).hexdigest()


def calculate_plex_hash(path: str | os.PathLike[str]) -> str:
    """Calculate the current file's Plex bundle hash without a server lookup.

    Results are cached by file identity and change markers, so checks and
    publishers for the same stable source share the bounded media reads.
    Plex metadata is deliberately not used: it can still describe an older
    file that an importer has replaced at the same path.

    Args:
        path: Local media filename.

    Returns:
        The Plex media bundle identifier. Small files include a decimal size
        prefix and therefore do not have a fixed 40-character length.

    Raises:
        OSError: The file is unavailable or is not a regular file.
        SourceFileChangedError: The source was replaced or modified during hashing.
    """
    filename = os.path.abspath(path)
    expected = get_source_fingerprint(filename)
    result = _hash_stable_source(filename, expected)
    if get_source_fingerprint(filename) != expected:
        raise SourceFileChangedError(f"Media source changed while hashing: {filename}")
    return result
