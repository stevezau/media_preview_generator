# Discoverability outreach — research + drafts

Research only. Nothing in this file has been posted, submitted, or used to create an account anywhere. All drafts are starting points for the maintainer to personally review, edit into their own voice, and submit by hand — several channels (awesome-selfhosted-data, Reddit) explicitly require human-authored, human-submitted content and ban AI agents from doing the submission itself (see that section).

**Project facts** (from `gh api repos/stevezau/media_preview_generator` and `gh release list -R stevezau/media_preview_generator`, checked 2026-09-23):

- Repo created: **2022-04-24** (`created_at`)
- First tagged release: **v3.5.3, 2026-04-01T06:55:44Z**
- Latest release: **v4.4.2, 2026-09-04T13:51:48Z**
- License: MIT · ~267 GitHub stars · Docker-only deployment, web UI, no CLI

Today is 2026-09-23. Repo age (4+ years) clears every age gate found below by a wide margin; the only live gate is awesome-selfhosted-data's 4-months-since-first-release rule, computed per-channel below.

---

## Summary

| Channel | Status | Eligible |
|---|---|---|
| awesome-selfhosted-data | Not listed | Now (age gate cleared 2026-08-01) |
| awesome-jellyfin | Not listed | Now |
| awesome-plex | No maintained list exists | N/A — don't pursue |
| awesome-emby | No maintained list exists | N/A — don't pursue |
| selfh.st apps directory | Not listed | Now |
| AlternativeTo | Not listed (no good category fit — see below) | Now, but needs an account |
| Unraid Community Applications | Template files exist in-repo; **not in the live CA search feed** | Now — files already meet requirements, just needs the submit step |
| r/selfhosted | — | Now (project old enough to skip the mandatory megathread) |
| r/PleX | — | **Blocked** — sidebar Rule 6 bans self-promotion posts outright |
| r/jellyfin | — | Now, mod-discretion (no published self-promo rule) |
| r/emby | — | Unverified — rules page unreachable, check before posting |
| r/unRAID | — | Unverified — rules page unreachable, check before posting |

---

## 1. awesome-selfhosted (awesome-selfhosted-data repo)

**Status:** not listed. Confirmed via GitHub code search across `awesome-selfhosted/awesome-selfhosted-data` for `media_preview_generator` / `stevezau` — zero results, and no `software/*.yml` file under any plausible filename exists.

**Rules** (source: [CONTRIBUTING.md](https://github.com/awesome-selfhosted/awesome-selfhosted-data/blob/master/CONTRIBUTING.md), [.github/ISSUE_TEMPLATE/addition.md](https://github.com/awesome-selfhosted/awesome-selfhosted-data/blob/master/.github/ISSUE_TEMPLATE/addition.md)):

- Age gate, exact quote: "Any software project you are adding was first released more than 4 months ago." A maintainer canned reply clarifies this counts from the first tagged release, not repo creation. First release here was 2026-04-01, so the gate cleared **2026-08-01** — already eligible.
- "Any software project you are adding to the list is actively maintained" and "has working installation instructions" — both satisfied (latest release 2026-09-04; README has install steps).
- Licence field is an allow-list checked against `licenses.yml`, not free-form SPDX text — MIT is already in that list.
- No star-count or fork-exclusion rule found. No explicit self-submission ban — self-submission is the normal path.
- **Hard rule, must not be skipped:** CONTRIBUTING.md has an explicit block addressed to AI agents: *"Do not: Open a pull request... Write the text of an entry... that a person will then submit as their own... Check the 'The submission was done by a human, not a machine/LLM' box... That statement is made by a human to the maintainers. An agent cannot make it truthfully."* Both the issue and PR templates require checking that box, and separately state: *"Machine/LLM-generated contributions are not allowed and will result in a ban."* This draft is a starting point only — it must be rewritten in your own words and submitted by you, not pasted verbatim, and not submitted by an agent on your behalf.

**Submission process:** add `software/media-preview-generator.yml` via a PR (or an issue if you'd rather not do the PR yourself), kebab-case filename, one entry per PR. `make awesome_lint` checks the file against schema. Merges land roughly a week after approval.

**Draft YAML** (starting point — verify current `tags/` and `platforms/` taxonomy before finalizing; only `Docker` and `MIT` are confirmed against the current allow-lists):

```yaml
name: "Media Preview Generator"
website_url: "https://stevezau.github.io/media_preview_generator/"
source_code_url: "https://github.com/stevezau/media_preview_generator"
description: "Generates GPU-accelerated video preview thumbnails (BIF/trickplay) for Plex, Emby and Jellyfin, one FFmpeg decode per file."
licenses:
  - MIT
platforms:
  - Docker
tags:
  - Media  # verify against current tags/ directory before submitting
```

---

## 2. awesome-jellyfin

**Status:** not listed. Repo: [awesome-jellyfin/awesome-jellyfin](https://github.com/awesome-jellyfin/awesome-jellyfin) — actively maintained (9,370 stars, last push 2026-09-12).

**Rules** (source: [CONTRIBUTING.md](https://github.com/awesome-jellyfin/awesome-jellyfin/blob/main/CONTRIBUTING.md)):

- Conventional Commits format for the PR; entries sorted alphabetically by canonical text.
- Project must be ≥4 weeks old, show recent/ongoing activity and continued maintenance, and pass automated checks.
- Needs a clear README/documentation (already true — see `docs/` site).
- Must be "broadly useful beyond a single use case," not a one-off script — clears this easily.
- No malicious/deceptive/spam association. No explicit self-submission ban.
- Categories are open to proposal; existing ones include Playback, Integration & Sync, Library Management, Metadata Providers — none is an exact fit for "thumbnail generation," so the category choice needs a maintainer call (Playback or a new category) rather than a confident match here.

**Eligible now.**

**Draft markdown line** (standard awesome-list format — confirm the exact category heading in the live README before placing it):

```markdown
- [Media Preview Generator](https://github.com/stevezau/media_preview_generator) - GPU-accelerated video preview thumbnails for Plex, Emby and Jellyfin. Docker-only, MIT licensed, no CLI; needs write access to Jellyfin's media folder (or its config folder with the companion plugin).
```

---

## 3. awesome-plex / awesome-emby

**Status: don't pursue.** Neither has a maintained curated list to submit to.

- `awesome-plex`-named repos found on GitHub are either an unrelated 2017 storage-setup guide (2 stars, last push 2017) or a thin, contentless repo (1 star) — neither is a real curated tool list.
- `awesome-emby` doesn't exist as a repo; the closest match (`Kern-bit/Awesome-Emby-Clients`) is a narrow client list (3 stars), not a general tools list.

No draft — there's nothing viable to submit to.

---

## 4. selfh.st apps directory

**Status:** not listed. Checked the live dataset (`r2.selfh.st/directory/software.json`, 1,251 entries) for the project name, author, and related keywords (preview/thumbnail/bif) — zero matches.

**Rules** (source: [selfh.st/apps-about](https://selfh.st/apps-about/), [introducing-selfhst-apps](https://selfh.st/post/introducing-selfhst-apps/)):

- No submission form or GitHub PR flow despite the FAQ implying one (`github.com/selfhst/apps` 404s — that org only has `icons`/`cdn` repos). The actual process is: email **hello@selfh.st** with the project details; the maintainer (Ethan Sholly) adds it himself.
- No stated open-source requirement (directory covers both open- and closed-source), no Docker requirement, no minimum age/maturity bar, no fixed format for the pitch.
- Once added, the listing's metadata (description, license, tags, icon) is auto-pulled from the GitHub API, not something you format yourself.
- Ranking (not gating) considers repo age, latest commit date, stars, and search interest.

**Eligible now.**

**Draft email to hello@selfh.st:**

```
Subject: App submission: Media Preview Generator

Hi Ethan,

I maintain Media Preview Generator (https://github.com/stevezau/media_preview_generator),
a self-hosted Docker app that generates video preview thumbnails for Plex, Emby and
Jellyfin using FFmpeg with GPU decoding (NVIDIA, Intel, AMD on Linux). It runs as its own
container and triggers per-file from Sonarr/Radarr/media-server webhooks, a Recently Added
poll, or a schedule, rather than only as a periodic scan.

It's Docker-only (no CLI, no native installer) and needs write access to wherever each
server keeps its thumbnails — Plex's data folder, or the media folder for Emby and
Jellyfin's default layout. MIT licensed. It doesn't do intro/credit detection or chapter
thumbnails.

Docs: https://stevezau.github.io/media_preview_generator/
Repo: https://github.com/stevezau/media_preview_generator

Happy to answer anything you need for the listing.

Thanks,
Steve
```

---

## 5. AlternativeTo

**Status:** not listed — no page found for the project or its old repo name.

**Rules** (source: [alternativeto.net/faq](https://alternativeto.net/faq)):

- Submission is "Suggest new application" from the account menu; requires an account with email verification ("to discourage spammers and bots"). Fields: platforms, license, descriptions, tags, category, pricing model. Manual review before it goes live.
- No ban on listing your own software through the normal form. What's banned: using your *user profile* to advertise, and manipulating votes/reviews (fake accounts, incentivized upvotes).
- **Category fit is the real question here.** AlternativeTo's data model is binary "X is an alternative to Y" — there's no companion/plugin relation. This project doesn't replace Plex, Jellyfin, or Emby; it's a utility that writes files into them. Forcing it onto the Plex/Jellyfin/Emby "alternatives" pages would misrepresent it and risks reading as the spam pattern the site polices. **Best fit is a standalone software page**, tagged Docker / self-hosted / media-server-tools, with Plex/Jellyfin/Emby named in the description text rather than structured as "alternative to."

**Eligible now, but needs an account** — out of scope for this research-only pass; the maintainer would need to create the account and submit personally.

**Draft (standalone page fields):**

- **Name:** Media Preview Generator
- **Description:** "Self-hosted Docker app that generates video preview thumbnails for Plex, Emby and Jellyfin using FFmpeg with GPU decoding (NVIDIA, Intel, AMD on Linux). Runs as its own container, triggered per-file by Sonarr/Radarr/media-server webhooks, a library poll, or a schedule. Writes each server's native format — a Plex BIF bundle, an Emby sidecar BIF, Jellyfin trickplay tiles — from one decode when a file is shared across servers. Docker-only, no CLI. Needs write access to each server's thumbnail location (Plex's data folder, or the media folder for Emby/Jellyfin). Does not do intro/credit detection or chapter thumbnails."
- **License:** MIT / Open Source
- **Platforms:** Docker, Linux, self-hosted
- **Tags:** self-hosted, docker, plex, jellyfin, emby, media-server, ffmpeg, gpu
- **Pricing model:** Free

---

## 6. Unraid Community Applications

**Status:** the repo already ships a working Unraid template (`unraid-templates/media-preview-generator.xml`, `ca_profile.xml`) that installs today via **Docker → Add Container → Template Repositories → paste the GitHub URL** (this is the repo's own README "Option 2," which needs no approval from anyone). What's *not* done is submission to the **curated, in-app-searchable** CA feed — that's a separate, gated step, and no evidence of it having happened was found (checked `Squidly271/community.applications`, the CA moderators repo, Unraid forums, and r/unRAID for any mention — zero hits).

**Rules** (source: [docs.unraid.net/community-applications](https://docs.unraid.net/community-applications/), the CA submission site, and forum topic 38582):

- Curated feed submission happens at **ca.unraid.net/submit** (run by Squidly271): paste the template repo, it runs a duplicate check and live validation scan, then publishes into the feed.
- Requirements checked against the existing template — **all already satisfied**: `Container version="2"` ✓, `Registry` ✓, `Network` ✓, `Support` and `Project` links (either is required; this template has both) ✓, `Category` populated (`MediaApp:Video MediaServer:Video`) ✓, populated `ca_profile.xml` for maintainer/support metadata ✓, `Overview` present ✓.
- One gotcha found in the forum rules: ampersands in the XML must be `&amp;amp;`-encoded or the app gets blacklisted — worth a manual check of `media-preview-generator.xml` before submitting, since the file has multi-paragraph prose that may contain a raw `&`.
- No stated age/maturity bar for the curated feed beyond having a working template.

**Eligible now — nothing new to draft.** The template files already in the repo meet the documented schema; the remaining step is purely procedural (run `ca.unraid.net/submit` against the existing `unraid-templates/media-preview-generator.xml`), not a content draft.

---

## 7. Reddit

### r/selfhosted — eligible now

**Rules** (source: [r/selfhosted rules](https://www.reddit.com/r/selfhosted/wiki/rules) — wiki page currently disabled; text confirmed via mod removal comments and the pinned [Quarter 2 Update](https://www.reddit.com/r/selfhosted/comments/1sey9ch/)):

- Rule 2 defers to Reddit's general self-promotion guideline (the "9:1" norm) rather than stating its own ratio.
- A weekly "New Project Megathread" (Fridays) is **mandatory** for any project younger than 3 months from its first public commit/announcement. This project's public history goes back to 2022, so a standalone post is allowed.
- Flair is required sub-wide for project posts.
- No adopted account-age/karma minimum.
- Must disclose AI involvement if any was used in building the project (automod-enforced reply).

**Draft:**

> **Title:** Media Preview Generator — GPU-accelerated preview thumbnails for Plex, Emby and Jellyfin (self-hosted, MIT, Docker)
>
> I maintain this project and wanted to share it here. It's a self-hosted Docker container that generates video preview thumbnails (the scrubber images you see when dragging a video's timeline) for Plex, Emby and Jellyfin, using FFmpeg with GPU decoding where one's available (NVIDIA/Intel/AMD on Linux; NVIDIA on Windows via WSL2; CPU fallback everywhere else).
>
> What it does differently from the built-in generators: it runs as its own container, so the media server doesn't do the decode work itself, and it can trigger per-file from Sonarr/Radarr/media-server webhooks or a schedule instead of only a periodic scan. If you run more than one of Plex/Emby/Jellyfin against the same library, it decodes a shared file once and writes each server's own native format (Plex BIF, Emby sidecar BIF, Jellyfin trickplay tiles).
>
> Limits, stated plainly: Docker-only, no CLI. It needs write access to wherever each server stores its thumbnails — Plex's data folder, or the media folder for Emby and Jellyfin's default layout (Jellyfin also supports keeping trickplay off the media drive with a companion plugin). It doesn't do intro/credit detection or chapter thumbnails, and I haven't published a speed benchmark against the built-in generators — it depends heavily on your GPU, codec and storage.
>
> MIT licensed. Docs: https://stevezau.github.io/media_preview_generator/ · Repo: https://github.com/stevezau/media_preview_generator
>
> Happy to answer questions.

### r/PleX — blocked, do not post as a normal submission

**Rules** (source: [r/PleX sidebar](https://old.reddit.com/r/PleX/); detailed wiki rules page could not be fetched):

- Rule 6, verbatim: **"No self-promotion posts."** Rule 7 bans referral/affiliate links, campaigning, and selling posts. No stated ratio, megathread, or exception mechanism — this reads as an outright ban, not a throttle.

**No post draft provided** — posting a promotional thread would violate the stated rule as found. The only compliant path is asking the moderators first via modmail; a draft for that:

> Hi, I maintain an open-source (MIT) tool that generates GPU-accelerated preview thumbnails for Plex, Emby and Jellyfin (https://github.com/stevezau/media_preview_generator). I saw the sub's "no self-promotion posts" rule and wanted to check before posting — would a factual write-up (what it does, its limits, no marketing language) be OK here, or is this a hard no regardless of framing? Happy to follow whatever format you'd want.

### r/jellyfin — eligible now, mod discretion

**Rules:** no subreddit-specific self-promo rule was found. The sub was closed to general posting for two years and only reopened October 2025 ([announcement](https://www.reddit.com/r/jellyfin/comments/1ob7vsp/)); mods said sub-specific rules were "forthcoming" but none have been published. It defers to Jellyfin's own [Community Standards](https://jellyfin.org/docs/general/community-standards/), which cover conduct/piracy/ToS but have no self-promotion clause. Plugin/tool announcement posts currently appear on the front page unchallenged — treat as mod-discretion, not a documented green light.

**Draft:**

> **Title:** Media Preview Generator — GPU trickplay generation for Jellyfin (and Plex/Emby), self-hosted, MIT
>
> I maintain this project and figured it's relevant here. It's a Docker container that generates Jellyfin trickplay images (and Plex/Emby preview thumbnails) using FFmpeg with GPU decoding, triggered per-file rather than only on a scheduled scan.
>
> On Jellyfin specifically: it needs Jellyfin 10.10+, writes trickplay tiles next to the video by default, and can instead write them into Jellyfin's config folder with a small companion plugin if you'd rather not give it write access to your media. It's a separate thing from Jellyfin's own built-in trickplay (10.9+) — worth trying that first if you're Jellyfin-only, since it has its own hardware decode options; this project is more useful once you've got a backlog it can't keep up with, want per-file triggers, or run more than one media server off the same library.
>
> Limits: Docker-only, no CLI, no intro/credit detection, no published speed benchmark against Jellyfin's own trickplay generation — depends on your GPU/codec/storage.
>
> MIT licensed. Docs: https://stevezau.github.io/media_preview_generator/ · Repo: https://github.com/stevezau/media_preview_generator

### r/emby and r/unRAID — unverified, check before posting

Both subs' rules/about pages were unreachable during this research pass (persistent Cloudflare/crawler blocking across ~10 fetch attempts each), and no search result surfaced a stated self-promo rule for either. Note: emby.media's own *forum* (a different venue from r/emby) bans self-promotion without prior mod approval — don't assume that carries over to the subreddit without checking.

**Before posting to either, manually check** `reddit.com/r/emby/about` and `reddit.com/r/unRAID/about` (rules tab) for ratio/megathread/flair requirements.

**Draft for r/emby** (generic, pending rule verification):

> **Title:** Media Preview Generator — GPU-accelerated BIF thumbnails for Emby (and Plex/Jellyfin), self-hosted, MIT
>
> I maintain this project. It's a Docker container that generates Emby's sidecar BIF thumbnails using FFmpeg with GPU decoding, triggered per-file via webhook or schedule rather than only Emby's own scheduled task. It also covers Plex and Jellyfin if you run those too, decoding a shared file once and writing each server's native format.
>
> Limits: Docker-only, no CLI, needs write access to your media folder (that's where Emby expects BIFs), no intro/credit detection, no published speed benchmark against Emby's built-in extraction — if your storage is the bottleneck rather than the GPU, this won't help.
>
> MIT licensed. Docs: https://stevezau.github.io/media_preview_generator/ · Repo: https://github.com/stevezau/media_preview_generator

**Draft for r/unRAID** (generic, pending rule verification):

> **Title:** Media Preview Generator — GPU preview-thumbnail generator for Plex/Emby/Jellyfin, Unraid template included
>
> I maintain this project. It's a Docker container (Unraid Community Applications template included in the repo — installable now via Docker → Add Container → Template Repositories, or via CA search once the curated-feed submission goes through) that generates video preview thumbnails for Plex, Emby and Jellyfin using FFmpeg with GPU decoding (NVIDIA, Intel or AMD passthrough).
>
> Limits: no CLI, needs write access to wherever each server stores its thumbnails, no intro/credit detection, no published speed benchmark against the built-in generators.
>
> MIT licensed. Docs: https://stevezau.github.io/media_preview_generator/ · Repo: https://github.com/stevezau/media_preview_generator
