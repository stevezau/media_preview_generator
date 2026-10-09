"""
Tests for version_check.py module.

Tests version parsing and GitHub API interaction.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from media_preview_generator.version_check import (
    get_current_version,
    get_latest_github_release,
    parse_version,
)


class TestGetCurrentVersion:
    """Test getting current version.

    The function tries 3 sources in order:
      1. ``from . import __version__`` (set by setuptools-scm at build time)
      2. ``importlib.metadata.version("media-preview-generator")``
      3. Fallback ``"0.0.0"``

    These tests force each branch and assert the return value, instead of
    just asserting a string came back (which would pass even on a complete
    breakage of all 3 sources).
    """

    def test_returns_package_version_when_dunder_version_present(self, monkeypatch):
        """Branch 1: __version__ attribute exists ⇒ returned verbatim."""
        import media_preview_generator as pkg

        monkeypatch.setattr(pkg, "__version__", "9.9.9-test", raising=False)
        assert get_current_version() == "9.9.9-test"

    def test_falls_back_to_importlib_metadata_when_dunder_missing(self, monkeypatch):
        """Branch 2: __version__ missing ⇒ importlib.metadata.version() used."""
        import importlib.metadata

        import media_preview_generator as pkg

        monkeypatch.delattr(pkg, "__version__", raising=False)
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "5.4.3")
        assert get_current_version() == "5.4.3"

    def test_falls_back_to_zero_zero_zero_when_all_sources_fail(self, monkeypatch):
        """Branch 3: both sources fail ⇒ "0.0.0" sentinel returned."""
        import importlib.metadata

        import media_preview_generator as pkg

        monkeypatch.delattr(pkg, "__version__", raising=False)

        def boom(_name):
            raise importlib.metadata.PackageNotFoundError("not installed")

        monkeypatch.setattr(importlib.metadata, "version", boom)
        assert get_current_version() == "0.0.0"


class TestParseVersion:
    """Test version string parsing."""

    def test_parse_version_valid(self):
        """Test parsing valid version string."""
        version = parse_version("2.0.0")
        assert version == (2, 0, 0)

    def test_parse_version_with_v_prefix(self):
        """Test parsing version with 'v' prefix."""
        version = parse_version("v2.0.0")
        assert version == (2, 0, 0)

    def test_parse_version_with_metadata(self):
        """Test parsing version with metadata."""
        version = parse_version("2.0.0-alpha+build123")
        assert version == (2, 0, 0)

    def test_parse_version_with_local_identifier(self):
        """Test parsing version with local identifier (PEP 440)."""
        version = parse_version("0.0.0+unknown")
        assert version == (0, 0, 0)

        version = parse_version("2.3.1.dev5+g1234abc")
        assert version == (2, 3, 1)

    def test_parse_version_invalid(self):
        """Test error on invalid version format."""
        with pytest.raises(ValueError):
            parse_version("invalid")

        with pytest.raises(ValueError):
            parse_version("2.0")

        with pytest.raises(ValueError):
            parse_version("2.0.0.1")


class TestGetLatestGitHubRelease:
    """Test GitHub API interaction."""

    @patch("requests.get")
    def test_get_latest_github_release(self, mock_get):
        """Test fetching latest release from GitHub."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": "v2.1.0"}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        version = get_latest_github_release()
        assert version == "v2.1.0"

    @patch("requests.get")
    def test_get_latest_github_release_timeout(self, mock_get):
        """Test timeout handling."""
        mock_get.side_effect = requests.exceptions.Timeout("Timeout")

        version = get_latest_github_release()
        assert version is None

    @patch("requests.get")
    def test_get_latest_github_release_connection_error(self, mock_get):
        """Test connection error handling."""
        mock_get.side_effect = requests.exceptions.ConnectionError("No connection")

        version = get_latest_github_release()
        assert version is None

    @patch("requests.get")
    def test_get_latest_github_release_rate_limit(self, mock_get):
        """Test rate limit handling."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
        mock_get.return_value = mock_response

        version = get_latest_github_release()
        assert version is None

    @patch("requests.get")
    def test_get_latest_github_release_404(self, mock_get):
        """Test 404 error handling."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
        mock_get.return_value = mock_response

        version = get_latest_github_release()
        assert version is None

    @patch("requests.get")
    def test_get_latest_github_release_empty_tag(self, mock_get):
        """Test handling of empty tag_name."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": ""}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        version = get_latest_github_release()
        assert version is None


class TestGetBranchHeadSha:
    """Test get_branch_head_sha function."""

    @patch("requests.get")
    def test_returns_sha_on_success(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_response = MagicMock()
        mock_response.json.return_value = {"commit": {"sha": "abc1234567890abcdef1234567890abcdef123456"}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        sha = get_branch_head_sha("dev")
        assert sha == "abc1234567890abcdef1234567890abcdef123456"

    @patch("requests.get")
    def test_returns_none_on_timeout(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_get.side_effect = requests.exceptions.Timeout("Timeout")
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_connection_error(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_get.side_effect = requests.exceptions.ConnectionError("fail")
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_http_error(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.side_effect = requests.exceptions.HTTPError(response=mock_response)
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_empty_sha(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_response = MagicMock()
        mock_response.json.return_value = {"commit": {"sha": ""}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_request_exception(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_get.side_effect = requests.exceptions.RequestException("fail")
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_unexpected_error(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_get.side_effect = RuntimeError("unexpected")
        assert get_branch_head_sha("dev") is None


class TestGetLatestGitHubReleaseExtra:
    """Additional edge cases for get_latest_github_release."""

    @patch("requests.get")
    def test_handles_generic_http_error(self, mock_get):
        """Test handling of generic HTTP error (not 404/429)."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
        mock_get.return_value = mock_response
        assert get_latest_github_release() is None

    @patch("requests.get")
    def test_handles_generic_request_exception(self, mock_get):
        mock_get.side_effect = requests.exceptions.RequestException("fail")
        assert get_latest_github_release() is None

    @patch("requests.get")
    def test_handles_key_error(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.side_effect = KeyError("missing")
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        assert get_latest_github_release() is None

    @patch("requests.get")
    def test_handles_unexpected_exception(self, mock_get):
        mock_get.side_effect = RuntimeError("unexpected")
        assert get_latest_github_release() is None
