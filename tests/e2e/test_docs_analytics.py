"""Only the public docs' approved paths/events may reach the optional counter."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e
SCRIPT = (Path(__file__).parents[2] / "docs/assets/js/analytics.js").read_text()
ORIGIN = "https://mediapreviewgenerator.dev"
ENDPOINT = "https://mpg-test.goatcounter.com/count"


def load_docs(page: Page, *, origin: str = ORIGIN, init: str = "", endpoint: str = ENDPOINT) -> list[dict]:
    """Serve a tiny docs DOM and capture every request; never contact GoatCounter."""
    sent = []
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => false});" + init)
    markup = f'''<button id="docs-analytics-toggle">Exclude my visits</button>
        <span id="docs-analytics-status"></span>
        <a id="start" href="/getting-started/?token=private#secret">Start</a>
        <a id="install" href="https://hub.docker.com/r/stevezzau/media_preview_generator?token=private">Install</a>
        <a id="release" href="https://github.com/stevezau/media_preview_generator/releases/latest">Release</a>
        <a id="other" href="https://example.org/private?q=secret">Other</a>
        <script>document.addEventListener('click', e => e.preventDefault());</script>
        <script src="/analytics.js" data-endpoint="{endpoint}" data-page="/guides/"></script>'''

    def route(request):
        url = request.request.url
        if url.startswith(ENDPOINT):
            sent.append(
                {"query": parse_qs(urlsplit(url).query, keep_blank_values=True), "headers": request.request.headers}
            )
            request.fulfill(status=200, body="")
        elif url.endswith("/analytics.js"):
            request.fulfill(content_type="text/javascript", body=SCRIPT)
        else:
            request.fulfill(content_type="text/html", body=markup)

    page.route("**/*", route)
    page.goto(origin + "/guides/?token=private#secret", referer="https://search.example.com/private?q=secret")
    page.wait_for_load_state("networkidle")
    return sent


def test_only_canonical_page_referrer_origin_and_fixed_events_are_sent(page: Page) -> None:
    sent = load_docs(page)
    for link in ["start", "install", "release", "other"]:
        page.locator("#" + link).click()
    page.wait_for_load_state("networkidle")
    assert [x["query"]["p"] for x in sent] == [
        ["/guides/"],
        ["get-started"],
        ["install-docker-hub"],
        ["install-github-releases"],
    ]
    for request in sent:
        query = request["query"]
        assert query["r"] == ["https://search.example.com"]
        assert set(query) <= {"p", "r", "t", "rnd", "e"}
        assert "private" not in str(query) and "secret" not in str(query)
        assert "referer" not in request["headers"] and "cookie" not in request["headers"]
    assert "e" not in sent[0]["query"]
    assert all(x["query"]["e"] == ["true"] for x in sent[1:])


@pytest.mark.parametrize(
    "init",
    [
        "Object.defineProperty(navigator, 'doNotTrack', {get: () => '1'});",
        "Object.defineProperty(navigator, 'globalPrivacyControl', {get: () => true});",
        "localStorage.setItem('skipgc', 't');",
        "Object.defineProperty(window, 'localStorage', {get: () => {throw new Error('unavailable')}});",
    ],
)
def test_privacy_preferences_prevent_every_request(page: Page, init: str) -> None:
    sent = load_docs(page, init=init)
    page.locator("#install").click()
    page.wait_for_load_state("networkidle")
    assert sent == []


@pytest.mark.parametrize(
    "origin", ["http://localhost:4000", "https://preview.example.com", "https://mediapreviewgenerator.dev:8443"]
)
def test_other_origins_never_count_even_with_production_config(page: Page, origin: str) -> None:
    sent = load_docs(page, origin=origin)
    page.locator("#install").click()
    assert sent == []


def test_opt_out_survives_reload_and_blocks_events(page: Page) -> None:
    sent = load_docs(page)
    assert len(sent) == 1
    page.get_by_role("button", name="Exclude my visits").click()
    expect(page.locator("#docs-analytics-status")).to_contain_text("excluded")
    page.locator("#start").click()
    page.reload()
    page.wait_for_load_state("networkidle")
    assert len(sent) == 1
    expect(page.get_by_role("button", name="Allow my visits to be counted")).to_be_visible()


def test_empty_or_untrusted_endpoint_never_counts(page: Page) -> None:
    sent = load_docs(page, endpoint="https://other.example/count")
    page.locator("#start").click()
    assert sent == []
