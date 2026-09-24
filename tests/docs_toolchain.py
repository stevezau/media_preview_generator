"""Run the docs site's Ruby toolchain (Jekyll through Bundler) from tests.

Uses a host `bundle` when there is one (CI installs it with ruby/setup-ruby), otherwise the pinned
`ruby` Docker image, so a machine without Ruby can still build the site. CI sets
MPG_REQUIRE_DOCS_BUILD=1 so a missing toolchain fails the run instead of silently skipping every
docs test. MPG_DOCS_TOOLCHAIN=docker forces the Docker path even when `bundle` exists.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
RUBY_IMAGE = "ruby:3.4-bookworm"
# Gems installed by the Docker path, kept between runs (outside the repo, owned by the caller).
BUNDLE_CACHE = Path.home() / ".cache" / "mpg-docs-bundle"


def _docker_works() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


def toolchain() -> str:
    """Return "bundle" or "docker"; skip the test (or fail it under MPG_REQUIRE_DOCS_BUILD=1)."""
    if shutil.which("bundle") and os.environ.get("MPG_DOCS_TOOLCHAIN") != "docker":
        return "bundle"
    if _docker_works():
        return "docker"
    message = "no Ruby toolchain: install Ruby and Bundler, or Docker, to build the docs site"
    if os.environ.get("MPG_REQUIRE_DOCS_BUILD") == "1":
        pytest.fail(message)
    pytest.skip(message)


def run_bundle(args: list[str], *, out_dir: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    """Run `bundle exec <args>` in docs/.

    Args:
        args: The command after `bundle exec`, e.g. ["jekyll", "build", ...].
        out_dir: A folder the command writes to. The Docker path mounts it read-write at the same
            absolute path; the repo itself is mounted read-only.
        timeout: Seconds before the subprocess is killed.

    Returns:
        The finished process, stdout and stderr captured as text.
    """
    env = {**os.environ, "JEKYLL_ENV": "production", "BUNDLE_FROZEN": "true"}
    if toolchain() == "bundle":
        return subprocess.run(
            ["bundle", "exec", *args], cwd=DOCS_DIR, env=env, capture_output=True, text=True, timeout=timeout
        )
    BUNDLE_CACHE.mkdir(parents=True, exist_ok=True)
    mounts = ["-v", f"{REPO_ROOT}:{REPO_ROOT}:ro", "-v", f"{BUNDLE_CACHE}:/bundle"]
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)  # else Docker creates it root-owned
        mounts += ["-v", f"{out_dir}:{out_dir}"]
    script = "bundle install --quiet && bundle exec " + " ".join(shlex.quote(arg) for arg in args)
    command = [
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp", "-e", "BUNDLE_PATH=/bundle", "-e", "BUNDLE_FROZEN=true", "-e", "JEKYLL_ENV=production",
        *mounts, "-w", str(DOCS_DIR), RUBY_IMAGE, "sh", "-c", script,
    ]  # fmt: skip
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def build_site(dest: Path) -> subprocess.CompletedProcess[str]:
    """Build the site into `dest` exactly as docs.yml does (no disk cache: the source is read-only)."""
    return run_bundle(["jekyll", "build", "--disable-disk-cache", "--destination", str(dest)], out_dir=dest)
