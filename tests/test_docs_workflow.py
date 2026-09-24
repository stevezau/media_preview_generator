"""The Pages workflows keep the Jellyfin plugin manifest safe (docs/design/discoverability.md).

A Pages deploy replaces the whole site, and every Jellyfin install of the plugin polls
/jellyfin-plugin/manifest.json on it. These pin the order that makes a deploy safe: build the
site, THEN place and validate the live manifest (failing the job if it can't), THEN upload and
deploy; and a plugin release still deploys through this same workflow.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTIONLINT_IMAGE = "rhysd/actionlint:1.7.7"
# The docs tests docs-check.yml exists to run.
DOCS_TEST_FILES = [
    "tests/test_benchmark_summary.py",
    "tests/test_brand_assets.py",
    "tests/test_docs_images.py",
    "tests/test_docs_links.py",
    "tests/test_docs_site.py",
    "tests/test_docs_workflow.py",
    "tests/test_llms_full.py",
    "tests/test_readmes.py",
    "tests/test_site_claims.py",
    "tests/test_site_urls.py",
    "tests/test_thumbnail_quality_copy.py",
]
COLLECT_ONLY = [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-cov", "-n", "0", "-p", "no:cacheprovider"]
PAGES_WORKFLOWS = [
    ".github/workflows/docs.yml",
    ".github/workflows/jellyfin-plugin.yml",
    ".github/workflows/docs-check.yml",
]


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    return workflow.get("on") or workflow[True]


@pytest.fixture(scope="module")
def steps() -> list[dict]:
    return _load("docs.yml")["jobs"]["deploy"]["steps"]


def _step(steps: list[dict], name: str) -> tuple[int, dict]:
    names = [step.get("name") for step in steps]
    assert name in names, f"docs.yml has no step named {name!r}: {names}"
    index = names.index(name)
    return index, steps[index]


class TestDeployOrder:
    def test_manifest_is_placed_after_the_build_and_before_upload(self, steps: list[dict]) -> None:
        build, _ = _step(steps, "Build site")
        manifest, _ = _step(steps, "Place and validate Jellyfin plugin manifest")
        upload, _ = _step(steps, "Upload Pages artifact")
        deploy, _ = _step(steps, "Deploy to GitHub Pages")
        assert build < manifest < upload < deploy

    def test_manifest_step_always_runs_and_fails_closed(self, steps: list[dict]) -> None:
        _, step = _step(steps, "Place and validate Jellyfin plugin manifest")
        assert "if" not in step
        assert "set -euo pipefail" in step["run"]
        assert 'curl -fsSL --retry 3 "$LIVE_MANIFEST_URL"' in step["run"]
        assert "must only come from the plugin release" in step["run"]
        # The JSON check: a 200 carrying an HTML error page or "[]" must not be deployed either.
        assert "if not isinstance(data, list) or not data:" in step["run"]
        assert "refusing to deploy" in step["run"]

    def test_no_deploy_step_is_skippable_or_allowed_to_fail(self, steps: list[dict]) -> None:
        # continue-on-error would let a deploy go out without a valid manifest; an `if:` on the build,
        # upload or deploy could ship the manifest without the site, or nothing at all.
        assert "continue-on-error" not in _load("docs.yml")["jobs"]["deploy"]
        assert [step.get("name") for step in steps if "continue-on-error" in step] == []
        for name in ("Build site", "Upload Pages artifact", "Deploy to GitHub Pages"):
            _, step = _step(steps, name)
            assert "if" not in step, name

    def test_build_writes_the_folder_that_gets_uploaded(self, steps: list[dict]) -> None:
        _, build = _step(steps, "Build site")
        _, upload = _step(steps, "Upload Pages artifact")
        assert build["working-directory"] == "docs"
        assert "bundle exec jekyll build" in build["run"]
        assert "--destination ../site" in build["run"]
        assert upload["with"]["path"] == "site"

    def test_ruby_and_gems_come_from_the_committed_lockfile(self, steps: list[dict]) -> None:
        _, ruby = _step(steps, "Setup Ruby")
        assert ruby["uses"].startswith("ruby/setup-ruby@")
        assert ruby["with"] == {"working-directory": "docs", "bundler-cache": True}
        assert (REPO_ROOT / "docs" / "Gemfile.lock").is_file()
        assert (REPO_ROOT / "docs" / ".ruby-version").is_file()

    def test_live_manifest_url_ends_at_the_manifest_path(self) -> None:
        assert _load("docs.yml")["env"]["LIVE_MANIFEST_URL"].endswith("/jellyfin-plugin/manifest.json")

    def test_deploys_are_serialised_in_the_pages_group(self) -> None:
        job = _load("docs.yml")["jobs"]["deploy"]
        assert job["concurrency"] == {"group": "pages", "cancel-in-progress": False}

    def test_push_trigger_watches_the_jekyll_sources_only(self) -> None:
        paths = _triggers(_load("docs.yml"))["push"]["paths"]
        assert "docs/**" in paths
        assert [p for p in paths if "mkdocs" in p or "docs_theme" in p or p.startswith("llms")] == []


class TestPluginReleaseShipsTheSite:
    def test_plugin_release_deploys_through_docs_yml_with_its_manifest(self) -> None:
        jobs = _load("jellyfin-plugin.yml")["jobs"]
        callers = [job for job in jobs.values() if job.get("uses") == "./.github/workflows/docs.yml"]
        assert len(callers) == 1
        assert callers[0]["with"]["manifest_artifact"] == "jellyfin-plugin-manifest"

    def test_docs_yml_accepts_what_the_plugin_release_passes(self) -> None:
        inputs = _triggers(_load("docs.yml"))["workflow_call"]["inputs"]
        assert {"manifest_artifact", "docs_ref"} <= set(inputs)


class TestDocsOnlyChangesAreTested:
    def test_docs_check_runs_the_docs_tests_on_docs_only_changes(self) -> None:
        # ci.yml's paths-ignore skips docs/** and *.md: exactly the changes that break these tests.
        workflow = _load("docs-check.yml")
        for event in ("pull_request", "push"):
            assert {"docs/**", "*.md"} <= set(_triggers(workflow)[event]["paths"]), event
        step = workflow["jobs"]["docs-tests"]["steps"][-1]
        assert step["env"]["MPG_REQUIRE_DOCS_BUILD"] == "1"
        assert "docs or llms" in step["run"]

    @pytest.mark.timeout(180)
    def test_docs_check_selects_every_docs_test(self) -> None:
        # The step picks tests with -k; a name the expression misses is silently never run on a
        # docs-only change, because ci.yml skips those.
        run = _load("docs-check.yml")["jobs"]["docs-tests"]["steps"][-1]["run"]
        expression = re.search(r'-k "([^"]+)"', run).group(1)

        def collected(*extra: str) -> set[str]:
            result = subprocess.run(
                [*COLLECT_ONLY, *extra, *DOCS_TEST_FILES],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=170,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            return {line for line in result.stdout.splitlines() if "::" in line}

        everything = collected()
        assert len(everything) > 100
        assert sorted(everything - collected("-k", expression)) == []


@pytest.mark.timeout(300)
def test_pages_workflows_pass_actionlint() -> None:
    if shutil.which("actionlint"):
        command = ["actionlint", *PAGES_WORKFLOWS]
    elif shutil.which("docker"):
        command = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{REPO_ROOT}:/repo",
            "-w",
            "/repo",
            ACTIONLINT_IMAGE,
            *PAGES_WORKFLOWS,
        ]
    else:
        pytest.skip("neither actionlint nor docker is available")
    result = subprocess.run(command, capture_output=True, text=True, timeout=280)
    assert result.returncode == 0, result.stdout + result.stderr
