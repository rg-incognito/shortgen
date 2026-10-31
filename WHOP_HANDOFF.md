# Whop Clipping — Handoff

How the Whop Content Rewards setup works, what runs on its own, and what needs a human
(or Claude) to keep earning. Written 2026-10-18.

> **2026-10-31: Whop clipping is retired.** The channel now posts two Shorts a day from this
> laptop only (the GitHub workflow is disabled: Actions minutes are used up). Campaign
> `podcasts` (src/campaigns.py): Gemini picks a popular podcast episode from YouTube's last two
> weeks (src/podcast_pick.py, picks kept in logs/podcast_picks.json), yt-dlp downloads it, and
> the normal pipeline + edit layer clips it, up to 4 Shorts per episode.
>
> | Task (Windows Task Scheduler) | Starts (IST) | Publishes (ET) |
> |---|---|---|
> | shortGen fallback PODCASTS 1900 | 14:00 | 19:00 |
> | shortGen fallback PODCASTS 2200 | 17:30 | 22:00 |
>
> Re-register with `scripts/register_fallback_tasks.ps1`. The laptop must be on and signed in
> at the start times (a missed start runs when the laptop comes back). The rest of this file
> describes the old Whop setup.

## What runs on its own

| Piece | Where | What it does |
|---|---|---|
| GitHub Actions schedule | `.github/workflows/generate_shorts.yml` | Each run starts ~6 h before its campaign's publish slot (~7 h in EST) and schedules the YouTube upload to go public at the slot (see table below). It picks the next unused moment, renders a captioned 9:16 Short, adds it to the playlist, logs it in the tracker sheet. |
| Local fallback | Windows Task Scheduler: `shortGen fallback *` (one per slot, ~2 h before it; registered by `scripts/register_fallback_tasks.ps1`) | `python run_local.py <campaign> [--slot HH:MM] --if-gh-missed <cron HH:MM UTC>` — waits for a GitHub run of the same cron that is still going, then runs the pipeline, which exits at once if the slot already holds a Short and uploads only if the slot is still empty after its render. One local run at a time; logs in `logs/`, every fallback decision in `logs/fallback.log`. Needs the PC on and you logged into Windows. |

### Publish slots (US Eastern — `publish_slots_et` in `src/campaigns.py`, follows daylight saving)

| Campaign | Publish (ET) | Publish (UTC, EDT / EST) | Why | GitHub cron (UTC, ~6 h early in EDT, ~7 h in EST) | Local fallback (IST, 2 h early in EDT, 3 h in EST) |
|---|---|---|---|---|---|
| MilkXP Ep. 3 | 18:45 + 19:45 | 22:45 / 23:45 + 23:45 / 00:45 | US evening; strongest early views (782 in 3 h on the first Short) | `42 16 * * *`, `42 17 * * *` | 02:15, 03:15 |
| Mark Manson | 20:00 | 00:00 / 01:00 | US evening (self-improvement wind-down; US-evening slots seed a T1 audience) | `57 17 * * *` | 03:30 |
| Unpacked (Ep. 3, David Dobrik first) | 21:00 | 01:00 / 02:00 | US evening; fresh Ep. 3 YouTube budget | `57 18 * * *` | 04:30 |
| LADbible (John Cena & Jessica Biel) | 22:30 + 23:15 | 02:30 / 03:30 + 03:15 / 04:15 | US late evening; $5K budget, A-list | `27 20 * * *`, `12 21 * * *` | 06:00, 06:45 |

Local fallbacks (Task Scheduler, `shortGen fallback <CAMPAIGN> <slot>`, wake the PC, 3 h limit) run
`run_local.py <campaign> [--slot HH:MM] --if-gh-missed <cron HH:MM UTC>`; a local run takes ~16-45 min (2026-10-25).
Re-register them with `powershell -ExecutionPolicy Bypass -File scripts\register_fallback_tasks.ps1` (`-WhatIf` previews).
A cron string lives in three places that must match: the `schedule:` list and the CAMPAIGN / PUBLISH_SLOT
expressions in the workflow, and `--if-gh-missed` in the task script (`test_scheduling.py` checks them).

**Spacing rule (since 2026-10-26):** no two of our Shorts, across all campaigns, go public less than 75 min apart
(Shorts posted within 0-60 min of another got under 50 views). With the slots above that means 18:45 and 19:45
(60 min), 19:45 and 20:00, 20:00 and 21:00, 22:30 and 23:15 cannot all be used on one evening: the first
run to book wins, a blocked run rolls its Short to the campaign's next free slot only if the campaign has
nothing queued yet (one rolled Short at a time), and otherwise stops before rendering. Expect ~3 Shorts a
night (MilkXP 18:45, Mark Manson or Unpacked, LADbible 22:30) until the slots are re-spaced; the window that
is open in both EST and EDT (18:30-23:30 ET) fits at most five slots 75 min apart (18:30, 19:45, 21:00,
22:15, 23:30).

Since 2026-10-25 the Rohit-Gande/shortgen copy (old Friends code, posting to the same channel) is disabled,
and its two daily posts went to a second MilkXP and LADbible slot (each cron sets PUBLISH_SLOT).

BILF Podcast (20:30 ET) is built but on standby: its first Short got 14 views in 2 h and its brief needs 1%
engagement plus a comment per 1K views. Tony Hawk FINALE (22:00 ET) runs by hand only (117 s of footage).

Only the 22:30-04:30 UTC (US evening) window is allowed; `src/campaigns.py` enforces it. Since 2026-10-24 there is no
"min payout reachable at 1K views" rule: campaigns are picked for viral potential. Ramp Tony Hawk FINALE (`tonyhawk`,
22:00 ET) is built but not scheduled until a test run uploads; MilkXP Ep. 3 is joined but blocked (needs the Milk
watermark overlay).

Only campaigns whose minimum payout is reached at ~1,000 views are scheduled (our Shorts mostly plateau
around 1K). Love is Blind (12:30 PM / 9:30 PM ET) was unscheduled on 2026-10-20: the campaign stopped
accepting YouTube (TikTok and Instagram only; Whop rejects YouTube links). YoungLA ($1 min) was unscheduled on 2026-10-20 because its
YouTube budget split ($2,000) is fully paid out. Mark Manson (1,600 views),
Unpacked and Love & Justice (3,000 views) were unscheduled on 2026-10-19; their profiles and footage remain,
and their local fallback tasks are disabled, not deleted.

Jacob & Co x Kylie Jenner Fantasy Bra (Clip Farm, `jacobco`): runway/craft footage with no speech, so
`jacobco_edits.py` cuts three fixed edits (each with one of the brief's exact on-screen headlines) from the
brand's Dropbox footage; they were uploaded directly for 2026-10-20 23:00, 10-21 20:00 and 10-22 20:00 UTC.
Captions start with @JacobCoofficial and write out Kylie Jenner / Victoria's Secret (never @ them).
Submissions close Fri 2026-10-23 11:59 PM ET; no demographics ticket (stats come from the linked account).

GitHub starts scheduled runs late (4.6 h for LADbible 23:15 on 2026-10-25), hence the early crons; the upload is still
scheduled for the slot. Each run checks YouTube (every upload on the channel) and the tracker sheet at the start and
again right before uploading: if the campaign already has a Short in that slot (late cron + local fallback both fired)
the second run exits without uploading. A run never publishes "now": if its slot has passed or is under 15 min away
it rolls to the campaign's next free slot (or the same slot tomorrow).
After a reboot: relaunch Claude Code (it runs the Whop auto-submit watcher) and stay signed in to Windows.

Submit to Whop within 30 min of each publish time.
| Tracker sheet | Google Sheet `1d-GID8JCIrw89ygIKXfsyT9Fe0qtxxmsUaKORuDliOM`, tab `PostingTracker` | One row per Short. Column K = YouTube link, column M = Whop status. |

Manual run: `gh workflow run "Generate Shorts" -f campaign=<name>` (or `python run_local.py <name>` on this PC).
Campaign names: `loveisblind`, `markmanson`, `loveandjustice`, `unpacked` (`friends` still exists but is unscheduled).

## What needs a human (Claude did these)

### 1. Submit every campaign Short to Whop within 30 minutes of it going public
Whop rejects anything not "posted within the last 30 minutes". If a Short misses the window it never earns.

```
python whop_queue.py due --fresh      # live campaign Shorts not yet submitted, with the right Whop campaign URL
```
Then on Whop: open the campaign URL → **Submit clip** → paste `https://www.youtube.com/shorts/<id>` → tick
"I've read the requirements" → **Submit clip**. Afterwards:
```
python whop_queue.py mark https://youtu.be/<id> submitted
```
Unpacked has one Whop campaign **per episode** — always use the URL `whop_queue` prints (wrong campaign = ban).

### 2. Check status
Whop → Content Rewards → **Submissions** (Approved / Pending / Rejected, views, rejection reasons),
**Analytics**, **Earnings**.

### 3. Demographics ticket (Clip Farm rule)
When a clip reaches its campaign's minimum payout, open a ticket in the **Clip Farm Whop community**
(not Discord) with a screenshot of YouTube Studio → that video → Analytics → Audience → Top geographies.
Needs 40%+ US/UK/CA/AU.

### 4. Find new campaigns
Lists: `https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/discover/clipping?section=for-you&platform=youtube`
and the Clip Farm profile `https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/clip-farm`.
Every campaign already evaluated (built / skipped / blocked, with reason) is in `campaign_scout.json` (local, gitignored).

A campaign is doable only if: YouTube Shorts is paid and its YouTube budget split isn't spent · ≥ $1K left and
≥ $1 per 1K views · footage from brand Drive links or an official YouTube video · talking content (podcast,
interview, reality/TV, stream) · no dedicated account, bio/link change, logo overlay or follower minimum ·
min views per clip under 5K · not gambling/crypto/trading/political.

To add one: profile in `src/campaigns.py` (length limits, `required_caption`, `caption_footer`,
`banned_patterns`, `transcript_banned`, `whop_campaign` / per-episode `whop_campaigns`), prompt pair in
`src/analyzer.py` → `_PROMPTS`, footage as Drive file IDs or a GitHub release `footage-<name>`
(download YouTube sources as **H.264** — AV1 breaks OpenCV on CI:
`python -m yt_dlp -f "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/b[height<=720][vcodec^=avc1]" --merge-output-format mp4 <url>`),
test with one manual run, then pick a `publish_slots_et` time for its audience (at least 75 min from every other slot) and add a cron ~6 h
earlier (plus a row in `scripts/register_fallback_tasks.ps1` ~2 h before the slot). Add `hotwords` (names and terms
Whisper mishears) to the profile; talk / podcast campaigns transcribe with `medium.en` (falls back to `medium`).

## Active campaigns (as of 2026-10-18)

| Profile | Whop campaign | Pay | Footage | Capacity |
|---|---|---|---|---|
| `loveisblind` | Clip Farm — Love is Blind | $1.25/1K, min $1.25 | brand Drive files (S11 E01–E05) | 5 eps × 8 = 40 Shorts (~20 days at 2/day) |
| `markmanson` | Clip Farm — Solved w/ Mark Manson | $1.25/1K, min $2 | release `footage-markmanson` (6 eps) | 36 Shorts |
| `loveandjustice` | Clip Farm — HardScope Love & Justice Ep 3 | $1/1K, min $3 | release `footage-hardscope` | 6 Shorts |
| `unpacked` | Clip Farm — HardScope Unpacked Ep 1 / Ep 2 (separate campaigns) | $1/1K, min $3 | release `footage-hardscope` | 10 Shorts |
| `youngla` | Clip Farm — YoungLA (caption must NOT mention the brand) | $1/1K, min $1 | release `footage-youngla` (YoungLA WORLD videos) | 12 Shorts |

## Decisions made
- Posting to the existing "The One Where" channel (@theonewhereclips); Friends Shorts stopped on 2026-10-17.
- Joined: Love is Blind, Mark Manson, HardScope Love & Justice Ep 3, Unpacked Ep 1 + Ep 2
  (also joined Jacob Nachinson, Jackelyne, It's Personal only to read their rules — not built).
- Skipped/blocked: SoFi (TikTok/IG carousels only), CLASH (all platforms required), 2 Bears (bio link),
  dedicated-account campaigns, TikTok-only, gambling/crypto/trading, non-English, low budget/CPM — reasons in `campaign_scout.json`.
- First Love is Blind + Mark Manson Shorts were re-uploaded after missing the 30-min window.
- Never: paid for anything, entered passwords, changed channel/account settings, deleted videos.

## Repo changes (`rg-incognito/shortgen`, main)
| Commit | Change |
|---|---|
| d320f42 | Campaign profiles (`src/campaigns.py`), Love is Blind mode + prompts |
| 6cf1f29 | Mark Manson profile, wide-shot fallback (`src/reframer.py`), release footage (`src/footage.py`), SponsorBlock ad filter |
| 6ce12d9 | Retry playlist add after YouTube 409 |
| 5bc8782 | 9 PM ET slot → Mark Manson |
| fb17637 | Fix: tracker row written before upload so the YouTube link is saved |
| 13abd18 | `whop_queue.py due --fresh` |
| cde3a83 | HardScope Love & Justice / Unpacked profiles, per-episode Whop URLs |
| 43ca1c0 | Scheduled Friends Shorts stopped |
| 0b8cb35 | `run_local.py`, `setup_local_env.py`, Windows caption fix |
| 15cdc1b | Love is Blind: reject clips with looks insults / real people named |
| 1c00104 | Niche publish slots in US Eastern; Unpacked + Love & Justice scheduled |

GitHub releases holding footage: `footage-markmanson`, `footage-hardscope`.
Local only (gitignored): `.env`, `yt_token.json`, `tokens.txt`, `campaign_scout.json`, `footage_hs/`, `logs/`.

## Will break without attention
- **Tokens expire every 7 days** while the Google OAuth app is in "Testing". Permanent fix: Google Cloud Console →
  APIs & Services → OAuth consent screen → **Publish app**. Otherwise weekly: `python setup_yt_oauth.py`
  (then `gh secret set YT_TOKEN_JSON < yt_token.json`) and `python setup_local_env.py`.
- **Footage runs out**: Love is Blind ~20 days, Mark Manson ~36 days. Runs for that campaign then fail until new footage/campaigns are added.
- **GitHub Actions minutes**: free tier (2,000/month on a private repo) runs out around the 22nd; the local fallback covers it while this PC is on.
- **Embedded GitHub token** in the git remote URL — rotate it at github.com/settings/tokens and switch the remote to `gh` auth.
