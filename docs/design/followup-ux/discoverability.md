# Discoverability delivery checklist

Source work for the owner-approved follow-up, 2026-10-05. This record distinguishes source changes from live account setup. It is excluded from the public Jekyll site.

## Provided in this branch

- [x] Preserve the existing canonical URLs, sitemap, crawler access, structured data, section search and deep links.
- [x] Recheck the existing live site: all 18 sitemap pages return HTTP 200, have one H1, use self-referencing canonical URLs and do not declare noindex. HTTP, the old github.io deep URL and the slashless install URL each redirect permanently to the HTTPS canonical. A missing page returns 404. Evidence: `public-site-audit-2026-10-05.json`. These are existing healthy behaviors, not new implementation claims.
- [x] Improve existing Plex GPU, Jellyfin trickplay, Intro & Credits and multi-server guides with requirements, a first-file check and an explicit remote-processing path. Retain current URLs and API details. Correct the outdated Plex scan prerequisite and llms.txt's obsolete claim that chapter images are unsupported.
- [x] Update README and Docker Hub full description together for cross-server reuse, remote processing, chapter thumbnails and the local-CPU loudness exception. Docker Hub short description is sourced from `pyproject.toml`, as the existing release workflow expects.
- [x] Retain the existing 1280×640 baseline JPEG social card and add explicit width, height and meaningful alternative text to social metadata. The card depicts existing approved open-film player captures; it is not a new synthetic product screenshot.
- [x] Add optional public-docs counting, with no Flask integration or application telemetry. Production builds and the exact HTTPS docs origin are both required. Blank endpoint disables the include, request and preference control.
- [x] Limit collection to a build-known page path and referring public-site origin; clicks produce only `get-started`, `install-docker-hub` or `install-github-releases`. Queries, hashes, titles, search terms, media paths, arbitrary outbound URLs and screen measurements are omitted. Requests omit credentials and HTTP referrers.
- [x] Respect Do Not Track, Global Privacy Control, GoatCounter's existing `skipgc=t` preference, unavailable browser storage, framed/local/preview contexts, WebDriver and prerendering. The footer offers a persistent browser opt-out. Counting never blocks link navigation.
- [x] Prepare release and community demo drafts in `discovery-copy.md`; no community messages have been published from this task.
- [x] Final docs/browser validation: 167 docs, image, metadata, README and generated-document checks passed, including the real Jekyll analytics build matrix; all 10 intercepted browser privacy/event tests passed in the complete browser run. See `verification.md` for whole-branch results and the corrected setup-label regression. Syntax/lint/diff checks passed. Final desktop dark/mobile light guide captures are `docs-offloading-1440.png` and `docs-offloading-390.png`. `llms-full.txt` was regenerated and its freshness check passed.

## Account-dependent work

- [x] Root created the separate Media Preview Generator site and supplied its verified endpoint: `https://mediapreviewgenerator.goatcounter.com/count` (site 111927). The source now uses that endpoint. No traffic is sent to the separate Shortlist site.
- [x] Root independently reloaded account settings: private dashboard, public-docs link domain `https://mediapreviewgenerator.dev`, Sessions and Referrer enabled, all other collection dimensions/individual-hit export disabled.
- [x] A browser on the public docs origin sent one explicitly labelled `implementation-verification` event to the MPG endpoint; it returned HTTP 200 with `image/gif`. This proves endpoint acceptance, not production integration or dashboard aggregation.
- [ ] Inspect normal page/event arrivals after deployment. The current public deployment does not include this branch yet.
- [ ] Search Console account verification is blocked: Google rejected the automated browser with “This browser or app may not be secure.” Asked the owner to check the property and submitted sitemap in their Mac browser. Source and HTTP checks cannot establish Google indexing or rankings; no verification token or indexing claim was invented.
- [x] Independently fetched GitHub's custom repository Open Graph image: it is byte-identical to `docs/images/social-preview.jpg` (95,785 bytes, SHA256 `9e162a2939f89c3d3d99ce68d7193882fa81c14e44de423e9cb26859c274a331`). Existing upload is correct; no replacement needed.
- [x] Published and read back the revised Docker Hub short and full description through the owner account (HTTP 200, exact match). The live version links to the existing multi-server guide root until the new section anchor is deployed. Existing CI refreshes both descriptions only on release tags, not dev merges.
- [ ] Deploy the final branch through the separately authorized workflow, then verify published metadata, guidance and counting. No visit/ranking growth is claimed.

Root task owns external account actions and their final status. The existing GitHub homepage/topics were already correct; root reported the About description was updated separately.

## Analytics implementation choice

The current official `count.js` includes `location.search` as a separate `q` field even when the page path is overridden, and includes screen width. The integration instead uses GoatCounter's documented stable browser `/count` GET interface. Its supported `p`, `r`, `t`, `e` and cache-busting `rnd` parameters are sufficient. No private API key is embedded and no third-party JavaScript is loaded. GoatCounter still receives normal connection information (IP address and browser headers), disclosed beside the opt-out; this is not described as collecting no personal data.

Official references checked 2026-10-05:

- [GoatCounter stable browser count endpoint and parameters](https://www.goatcounter.com/help/pixel)
- [GoatCounter events](https://www.goatcounter.com/help/events)
- [GoatCounter development/own-visit exclusion](https://www.goatcounter.com/help/skip-dev)
- [Google canonical URL guidance](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls)
- [Google sitemap guidance](https://developers.google.com/search/docs/crawling-indexing/sitemaps/overview)
- [Google title links](https://developers.google.com/search/docs/appearance/title-link)
- [Google snippets](https://developers.google.com/search/docs/appearance/snippet)

The existing search-oriented pages are improved in place, with accurate task-specific titles and summaries. No duplicate keyword pages, unsupported speed claims, artificial ratings or promises of indexing were added.
