# Whop Clipping — Handoff

How the Whop Content Rewards setup works, what runs on its own, and what needs a human
(or Claude) to keep earning. Written 2026-10-18.

## What runs on its own

| Piece | Where | What it does |
|---|---|---|
| GitHub Actions schedule | `.github/workflows/generate_shorts.yml` | 00:00 UTC Love is Blind · 01:00 UTC Mark Manson · 02:00 UTC Love is Blind. Each run picks the next unused moment, renders a captioned 9:16 Short, uploads it to YouTube (goes public 10–15 min later), adds it to the playlist, logs it in the tracker sheet. |
| Local fallback | Windows Task Scheduler: `shortGen fallback *` at 07:00 / 08:00 / 09:00 IST | `python run_local.py <campaign> --if-gh-missed HH:MM` — runs the same pipeline on this PC only if GitHub had no successful/running scheduled run for that slot (e.g. Actions minutes ran out). One local run at a time; logs in `logs/`. Needs the PC on and you logged into Windows. |
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
test with one manual run, then give it a cron slot in the workflow (keep ~3 runs/day for Actions minutes).

## Active campaigns (as of 2026-10-18)

| Profile | Whop campaign | Pay | Footage | Capacity |
|---|---|---|---|---|
| `loveisblind` | Clip Farm — Love is Blind | $1.25/1K, min $1.25 | brand Drive files (S11 E01–E05) | 5 eps × 8 = 40 Shorts (~20 days at 2/day) |
| `markmanson` | Clip Farm — Solved w/ Mark Manson | $1.25/1K, min $2 | release `footage-markmanson` (6 eps) | 36 Shorts |
| `loveandjustice` | Clip Farm — HardScope Love & Justice Ep 3 | $1/1K, min $3 | release `footage-hardscope` | 6 Shorts (not scheduled yet) |
| `unpacked` | Clip Farm — HardScope Unpacked Ep 1 / Ep 2 (separate campaigns) | $1/1K, min $3 | release `footage-hardscope` | 10 Shorts (not scheduled yet) |

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

GitHub releases holding footage: `footage-markmanson`, `footage-hardscope`.
Local only (gitignored): `.env`, `yt_token.json`, `tokens.txt`, `campaign_scout.json`, `footage_hs/`, `logs/`.

## Will break without attention
- **Tokens expire every 7 days** while the Google OAuth app is in "Testing". Permanent fix: Google Cloud Console →
  APIs & Services → OAuth consent screen → **Publish app**. Otherwise weekly: `python setup_yt_oauth.py`
  (then `gh secret set YT_TOKEN_JSON < yt_token.json`) and `python setup_local_env.py`.
- **Footage runs out**: Love is Blind ~20 days, Mark Manson ~36 days. Runs for that campaign then fail until new footage/campaigns are added.
- **GitHub Actions minutes**: free tier (2,000/month on a private repo) runs out around the 22nd; the local fallback covers it while this PC is on.
- **Embedded GitHub token** in the git remote URL — rotate it at github.com/settings/tokens and switch the remote to `gh` auth.
