"""Version helpers for the update badge: current version, SemVer parsing, and GitHub release / branch-head lookups."""

import re

import requests
from loguru import logger


def get_current_version() -> str:
    """Get the current version from package metadata.

    Priority order:
    1. Local _version.py (when running from source)
    2. Installed package metadata (when installed via pip)
    3. Fallback to "0.0.0"

    Returns:
        str: Current version string (e.g., "2.1.2")

    """
    try:
        from . import __version__

        return __version__
    except (ImportError, AttributeError):
        pass

    try:
        import importlib.metadata

        return importlib.metadata.version("media-preview-generator")
    except Exception:
        logger.debug("importlib.metadata version lookup failed", exc_info=True)

    logger.debug("Could not determine current version, using fallback")
    return "0.0.0"


def parse_version(version_str: str) -> tuple[int, int, int]:
    """Parse a semantic version string into comparable tuple.

    Args:
        version_str: Version string like "2.0.0", "1.5.3", "2.1.1.post14"

    Returns:
        Tuple of (major, minor, patch) integers

    Raises:
        ValueError: If version string format is invalid

    """
    # Remove any 'v' prefix and extract version parts
    clean_version = version_str.lstrip("v")

    # Match semantic version pattern (major.minor.patch) with optional suffixes
    # Supports: 2.0.0, v2.1.2, 2.1.1.post14, 2.3.1.dev5, 0.0.0+unknown, 2.3.1.dev5+g1234abc
    match = re.match(
        r"^(\d+)\.(\d+)\.(\d+)(?:\.(?:post|dev)\d+)?(?:-[a-zA-Z0-9.-]+)?(?:\+[a-zA-Z0-9.-]+)?$",
        clean_version,
    )

    if not match:
        raise ValueError(f"Invalid version format: {version_str}")

    major, minor, patch = match.groups()[:3]
    return (int(major), int(minor), int(patch))


def get_latest_github_release() -> str | None:
    """Query GitHub API for the latest release version.

    Returns:
        str: Latest release version string, or None if failed

    """
    try:
        # GitHub API endpoint for latest release
        url = "https://api.github.com/repos/stevezau/media_preview_generator/releases/latest"

        # Set timeout and user agent
        headers = {"User-Agent": "media-preview-generator-version-check"}

        response = requests.get(url, headers=headers, timeout=3)
        response.raise_for_status()

        data = response.json()
        latest_version = data.get("tag_name", "")

        if not latest_version:
            logger.debug("GitHub API returned empty tag_name")
            return None

        return latest_version

    except requests.exceptions.Timeout:
        logger.debug("Version check timed out - no internet connection or slow response")
        return None
    except requests.exceptions.ConnectionError:
        logger.debug("Version check failed - no internet connection")
        return None
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.debug("Repository or releases not found on GitHub")
        elif e.response.status_code == 429:
            logger.debug("GitHub API rate limit exceeded")
        else:
            logger.debug("GitHub API error: {}", e.response.status_code)
        return None
    except requests.exceptions.RequestException as e:
        logger.debug("Version check request failed: {}", e)
        return None
    except (KeyError, ValueError) as e:
        logger.debug("Invalid response from GitHub API: {}", e)
        return None
    except Exception:
        logger.warning(
            "Update-check against GitHub failed unexpectedly. "
            "The dashboard's 'New release available' badge will be silent until the next check succeeds.",
            exc_info=True,
        )
        return None


def get_branch_head_sha(branch: str) -> str | None:
    """Query GitHub API for the latest commit SHA on a branch.

    Args:
        branch: Branch name (e.g., "dev")

    Returns:
        str: Full 40-char SHA of branch head, or None if failed

    """
    try:
        url = f"https://api.github.com/repos/stevezau/media_preview_generator/branches/{branch}"
        headers = {"User-Agent": "media-preview-generator-version-check"}
        response = requests.get(url, headers=headers, timeout=3)
        response.raise_for_status()
        data = response.json()
        commit = data.get("commit", {})
        sha = commit.get("sha", "")
        if not sha:
            logger.debug("GitHub API returned empty branch sha")
            return None
        return sha
    except requests.exceptions.Timeout:
        logger.debug("Branch head check timed out - no internet connection or slow response")
        return None
    except requests.exceptions.ConnectionError:
        logger.debug("Branch head check failed - no internet connection")
        return None
    except requests.exceptions.HTTPError as e:
        logger.debug("GitHub branch API error: {}", getattr(e.response, "status_code", "unknown"))
        return None
    except requests.exceptions.RequestException as e:
        logger.debug("Branch head request failed: {}", e)
        return None
    except Exception as e:
        logger.debug("Unexpected error during branch head check: {}", e)
        return None
