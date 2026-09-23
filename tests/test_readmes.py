"""README.md, DOCKERHUB_README.md and docs/llms.txt: the shape spec §6-§7 asks for.

Each fails on a regression someone would actually ship: a relative link on Docker Hub (which has no
repo to resolve it against), a README section that drifted back, a badge in the wrong colour, or an
llms.txt that stopped listing a docs page.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
DOCKERHUB = (REPO_ROOT / "DOCKERHUB_README.md").read_text(encoding="utf-8")
LLMS = (REPO_ROOT / "docs" / "llms.txt").read_text(encoding="utf-8")
CONFIG = yaml.safe_load((REPO_ROOT / "docs" / "_config.yml").read_text(encoding="utf-8"))
ROOT = f"{CONFIG['url']}{CONFIG.get('baseurl') or ''}/"
LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)|(?:src|href)=\"([^\"]+)\"")
README_SECTIONS = ["What it does", "What it looks like", "Features", "Where it fits", "Quick start",
                   "Documentation", "Support the project", "Get help", "License"]  # fmt: skip
# A status badge's colour is the status (a red build must look red), so only its label is amber.
STATUS_BADGES = {"build", "codecov", "issues"}
BRAND_COLOURED_BADGES = {"ai", "kofi"}


def _targets(text: str) -> list[str]:
    return [a or b for a, b in LINK_TARGET.findall(text)]


def _shields() -> dict[str, str]:
    return dict(re.findall(r"^\[([\w-]+)-shield\]: (\S+)$", README, re.MULTILINE))


def _shared_section(text: str, heading: str) -> str:
    """One section, minus what differs by design: link targets, <sub> versus _italic_ captions, line
    wrapping, and the README's badge definitions after its last section."""
    section = text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0].split("\n<!--", 1)[0]
    section = re.sub(r"\]\([^)\s]+\)", "]()", section)
    section = re.sub(r"</?sub>|^_|_$", "", section, flags=re.MULTILINE)
    return " ".join(section.split())


class TestReadme:
    def test_sections_in_order(self) -> None:
        headings = re.findall(r"^## (.+)$", README, re.MULTILINE)
        assert headings == README_SECTIONS

    def test_badges_are_amber_and_trimmed(self) -> None:
        shields = _shields()
        assert 8 <= len(shields) <= 10
        for name, url in shields.items():
            if name in BRAND_COLOURED_BADGES:
                continue
            query = parse_qs(urlsplit(url).query)
            amber, other = ("labelColor", "color") if name in STATUS_BADGES else ("color", "labelColor")
            assert query.get(amber) == ["a06a00"] and other not in query, (name, url)
        assert not re.search(r"contributors-shield|forks-shield|sponsor-shield", README)
        assert "Built With" not in README

    def test_build_badge_follows_the_branch_ci_runs_on(self) -> None:
        # ci.yml runs on pushes to dev, tags and PRs; a branch=main badge would show one old run forever.
        query = parse_qs(urlsplit(_shields()["build"]).query)
        assert (query.get("branch"), query.get("event")) == (["dev"], ["push"])
        assert "actions/workflows/ci.yml?query=branch%3Adev+event%3Apush" in README

    def test_hero_is_the_players_image_with_a_disclosing_caption(self) -> None:
        assert "docs/images/players-3up.webp" in README
        assert re.search(r"<sub>[^<]*test servers[^<]*</sub>", README)

    def test_no_horizontal_rules_between_sections(self) -> None:
        assert "\n---\n" not in README

    def test_links_to_the_docs_site(self) -> None:
        assert f'href="{ROOT}"' in README


class TestDockerHubReadme:
    def test_every_link_and_image_is_absolute(self) -> None:
        relative = [t for t in _targets(DOCKERHUB) if not t.startswith(("http://", "https://", "#", "mailto:"))]
        assert relative == []

    def test_fits_docker_hubs_limit_and_skips_github_only_syntax(self) -> None:
        assert len(DOCKERHUB) <= 25_000
        assert not re.search(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", DOCKERHUB)

    def test_carries_the_readme_story(self) -> None:
        headings = re.findall(r"^## (.+)$", DOCKERHUB, re.MULTILINE)
        assert [h for h in headings if h in README_SECTIONS] == README_SECTIONS

    @pytest.mark.parametrize("heading", README_SECTIONS)
    def test_section_matches_the_readme(self, heading: str) -> None:
        # Both files carry the same copy by hand; an edit that reached only one of them fails here.
        assert _shared_section(DOCKERHUB, heading) == _shared_section(README, heading)


class TestLlmsTxt:
    def test_opens_with_the_question_it_answers(self) -> None:
        body = LLMS.split("---\n", 2)[2]
        assert body.startswith("# Media Preview Generator\n\n> ")
        assert "?" in body.split("\n\n", 2)[1]

    def test_full_file_is_the_first_link(self) -> None:
        body = LLMS.split("---\n", 2)[2]
        first = re.search(r"https?://\S+", body).group(0)
        assert first.rstrip(".,)") == f"{ROOT}llms-full.txt"

    def test_every_docs_page_is_linked_with_a_description(self) -> None:
        docs = LLMS.split("\n## Docs", 1)[1]
        urls = [ROOT] + [ROOT + url.strip("/") + "/" for url in _nav_urls()]
        for url in urls:
            assert re.search(rf"^- \[[^\]]+\]\({re.escape(url)}\): \S", docs, re.MULTILINE), url

    def test_dev_image_note_and_llms_clause_are_removed_together(self) -> None:
        # Until Intro & Credits reaches a release, the pages say it's in the dev image and llms.txt says
        # so too. At release all three go; this fails if only some of them do.
        llms_says_dev = "(dev image until the next release)" in LLMS
        note = "Intro & Credits is in the `dev` image"
        pages = [REPO_ROOT / "docs" / "skip-intro-credits.md", REPO_ROOT / "docs" / "faq.md"]
        assert [p.name for p in pages if (note in p.read_text(encoding="utf-8")) != llms_says_dev] == []

    def test_has_a_status_line_and_a_bold_distinction(self) -> None:
        assert re.search(r"^Status: ", LLMS, re.MULTILINE)
        how = LLMS.split("## How it differs", 1)[1].split("\n## ", 1)[0]
        # The distinction is the paragraph that opens in bold; the bullets above it start with "- ".
        assert re.search(r"^\*\*[^*]+\*\*", how, re.MULTILINE), "no bold distinction paragraph"


def _nav_urls() -> list[str]:
    urls = []
    for entry in CONFIG["nav"]:
        urls.append(entry["url"])
        urls.extend(child["url"] for child in entry.get("children", []))
    return urls
