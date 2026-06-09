"""
YouTube Channel Analytics — fetch all video stats for "The One Where" channel.

Usage:
    python fetch_analytics.py              # full report (table + CSV)
    python fetch_analytics.py --csv-only   # skip table, just write CSV

Outputs:
    analytics_report.csv   — per-video metrics
    analytics_summary.txt  — channel overview

Requires yt_token.json with full youtube + analytics scopes.
If the token is missing or lacks analytics scope, run this once:
    python fetch_analytics.py --setup
"""

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

_TOKEN_FILE = Path(__file__).parent / 'yt_token.json'
_CLIENT_SECRET_FILES = [
    Path(__file__).parent / 'client_secret_1061831573768-v5195flb6ql1a38fva5ic0a5b0gm80iq.apps.googleusercontent.com.json',
    Path(__file__).parent.parent / '4kflows' / 'normal-bike-ig' / 'yt_client_secret.json',
]
_SCOPES = [
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/yt-analytics.readonly',
]


# ── Auth ─────────────────────────────────────────────────────────────────────

def _setup_token():
    from google_auth_oauthlib.flow import InstalledAppFlow
    secret = next((p for p in _CLIENT_SECRET_FILES if p.exists()), None)
    if not secret:
        print("ERROR: No client_secret JSON found. Tried:")
        for p in _CLIENT_SECRET_FILES:
            print(f"  {p}")
        sys.exit(1)
    print(f"Using client secret: {secret.name}")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), _SCOPES)
    creds = flow.run_local_server(port=0)
    _TOKEN_FILE.write_text(creds.to_json())
    print(f"Saved token to {_TOKEN_FILE}")
    return creds


def _load_creds():
    if not _TOKEN_FILE.exists():
        print("yt_token.json not found — running OAuth setup...")
        return _setup_token()
    info = json.loads(_TOKEN_FILE.read_text(encoding='utf-8-sig'))
    creds = Credentials.from_authorized_user_info(info, _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _TOKEN_FILE.write_text(creds.to_json())
    return creds


# ── Data API helpers ──────────────────────────────────────────────────────────

def _get_channel(yt):
    resp = yt.channels().list(part='snippet,statistics,contentDetails', mine=True).execute()
    ch = resp['items'][0]
    return ch


def _get_all_videos(yt, uploads_playlist_id):
    """Page through uploads playlist and return list of video IDs in order."""
    video_ids = []
    token = None
    while True:
        resp = yt.playlistItems().list(
            part='contentDetails',
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=token,
        ).execute()
        for item in resp['items']:
            video_ids.append(item['contentDetails']['videoId'])
        token = resp.get('nextPageToken')
        if not token:
            break
    return video_ids


def _get_video_details(yt, video_ids):
    """Batch-fetch snippet + statistics + contentDetails for up to 50 video IDs."""
    details = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = yt.videos().list(
            part='snippet,statistics,contentDetails',
            id=','.join(batch),
        ).execute()
        for item in resp['items']:
            details[item['id']] = item
    return details


def _parse_duration(iso_duration):
    """Convert ISO 8601 duration (PT1M30S) to total seconds."""
    import re
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso_duration or '')
    if not m:
        return 0
    h, mn, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + s


# ── Analytics API helper ──────────────────────────────────────────────────────

def _get_analytics(analytics_svc, channel_id, start_date='2020-01-01'):
    """Fetch per-video analytics metrics. Returns dict keyed by videoId."""
    today = date.today().isoformat()
    metrics = (
        'views,estimatedMinutesWatched,averageViewDuration,'
        'averageViewPercentage,subscribersGained,likes,comments,shares'
    )
    result = {}
    try:
        resp = analytics_svc.reports().query(
            ids=f'channel=={channel_id}',
            startDate=start_date,
            endDate=today,
            metrics=metrics,
            dimensions='video',
            maxResults=200,
            sort='-views',
        ).execute()
        col_headers = [c['name'] for c in resp.get('columnHeaders', [])]
        for row in resp.get('rows', []):
            row_dict = dict(zip(col_headers, row))
            vid = row_dict.pop('video')
            result[vid] = row_dict
    except HttpError as e:
        print(f"  [Analytics API] {e.status_code}: {e.reason}")
        print("  (Analytics data unavailable — showing Data API stats only)")
    return result


# ── Formatting ────────────────────────────────────────────────────────────────

def _fmt_num(n):
    try:
        return f"{int(float(n)):,}"
    except (TypeError, ValueError):
        return '—'


def _fmt_pct(n):
    try:
        return f"{float(n):.1f}%"
    except (TypeError, ValueError):
        return '—'


def _fmt_sec(n):
    try:
        s = int(float(n))
        return f"{s // 60}m{s % 60:02d}s"
    except (TypeError, ValueError):
        return '—'


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup', action='store_true', help='Re-run OAuth setup')
    parser.add_argument('--csv-only', action='store_true', help='Skip terminal table')
    args = parser.parse_args()

    if args.setup:
        _setup_token()
        return

    print("Loading credentials...")
    creds = _load_creds()

    yt          = build('youtube',          'v3',  credentials=creds)
    analytics   = build('youtubeAnalytics', 'v2',  credentials=creds)

    # ── Channel info ────────────────────────────────────────────────────────
    print("Fetching channel info...")
    ch      = _get_channel(yt)
    ch_id   = ch['id']
    ch_name = ch['snippet']['title']
    ch_stat = ch['statistics']
    uploads_pl = ch['contentDetails']['relatedPlaylists']['uploads']

    subs   = _fmt_num(ch_stat.get('subscriberCount'))
    total_views = _fmt_num(ch_stat.get('viewCount'))
    vid_count   = _fmt_num(ch_stat.get('videoCount'))

    print(f"\n{'='*60}")
    print(f"  Channel : {ch_name}")
    print(f"  ID      : {ch_id}")
    print(f"  Subs    : {subs}")
    print(f"  Views   : {total_views}")
    print(f"  Videos  : {vid_count}")
    print(f"{'='*60}\n")

    # ── Videos ──────────────────────────────────────────────────────────────
    print("Fetching video list...")
    video_ids = _get_all_videos(yt, uploads_pl)
    print(f"  Found {len(video_ids)} videos")

    print("Fetching video details (Data API)...")
    details = _get_video_details(yt, video_ids)

    print("Fetching analytics (Analytics API)...")
    analytics_data = _get_analytics(analytics, ch_id)

    # ── Build rows ───────────────────────────────────────────────────────────
    rows = []
    for vid_id in video_ids:
        d = details.get(vid_id)
        if not d:
            continue
        snip  = d['snippet']
        stats = d.get('statistics', {})
        cd    = d.get('contentDetails', {})
        an    = analytics_data.get(vid_id, {})

        published = snip.get('publishedAt', '')[:10]
        title     = snip.get('title', '')
        duration_sec = _parse_duration(cd.get('duration'))

        row = {
            'video_id':       vid_id,
            'title':          title,
            'published':      published,
            'duration_sec':   duration_sec,
            'views':          int(stats.get('viewCount',    0) or 0),
            'likes':          int(stats.get('likeCount',    0) or 0),
            'comments':       int(stats.get('commentCount', 0) or 0),
            # Analytics API (may be empty)
            'watch_minutes':  an.get('estimatedMinutesWatched', ''),
            'avg_view_dur':   an.get('averageViewDuration', ''),
            'avg_view_pct':   an.get('averageViewPercentage', ''),
            'subs_gained':    an.get('subscribersGained', ''),
            'shares':         an.get('shares', ''),
        }
        rows.append(row)

    # Sort by views descending
    rows.sort(key=lambda r: r['views'], reverse=True)

    # ── CSV ──────────────────────────────────────────────────────────────────
    csv_path = Path(__file__).parent / 'analytics_report.csv'
    fieldnames = list(rows[0].keys()) if rows else []
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV saved: {csv_path}")

    if args.csv_only:
        return

    # ── Terminal table ───────────────────────────────────────────────────────
    print(f"\n{'─'*100}")
    print(f"{'#':<4} {'Published':<12} {'Dur':>6} {'Views':>10} {'Likes':>8} {'Cmt':>7} {'RetPct':>8} {'AvgView':>9}  Title")
    print(f"{'─'*100}")
    for i, r in enumerate(rows, 1):
        dur    = f"{r['duration_sec']//60}:{r['duration_sec']%60:02d}"
        ret    = _fmt_pct(r['avg_view_pct']) if r['avg_view_pct'] else '   —  '
        avgv   = _fmt_sec(r['avg_view_dur']) if r['avg_view_dur'] else '   —  '
        title  = r['title'][:52] + '…' if len(r['title']) > 52 else r['title']
        print(f"{i:<4} {r['published']:<12} {dur:>6} {r['views']:>10,} {r['likes']:>8,} {r['comments']:>7,} {ret:>8} {avgv:>9}  {title}")

    print(f"{'─'*100}")

    # ── Summary stats ────────────────────────────────────────────────────────
    total_v   = sum(r['views']    for r in rows)
    total_l   = sum(r['likes']    for r in rows)
    total_c   = sum(r['comments'] for r in rows)
    avg_views = total_v / len(rows) if rows else 0

    shorts  = [r for r in rows if r['duration_sec'] <= 60]
    longs   = [r for r in rows if r['duration_sec'] >  60]
    avg_s   = sum(r['views'] for r in shorts) / len(shorts) if shorts else 0
    avg_l   = sum(r['views'] for r in longs)  / len(longs)  if longs  else 0

    # CTR
    # AVD
    avds = [float(r['avg_view_dur']) for r in rows if r['avg_view_dur']]
    avg_avd = sum(avds) / len(avds) if avds else None

    print(f"\n  Total videos  : {len(rows)}")
    print(f"  Total views   : {total_v:,}")
    print(f"  Total likes   : {total_l:,}")
    print(f"  Total comments: {total_c:,}")
    print(f"  Avg views/vid : {avg_views:,.0f}")
    if avg_avd is not None:
        print(f"  Avg view dur  : {_fmt_sec(avg_avd)}")
    if shorts:
        print(f"\n  Shorts (≤60s) : {len(shorts)} videos | avg {avg_s:,.0f} views each")
    if longs:
        print(f"  Long-form     : {len(longs)} videos | avg {avg_l:,.0f} views each")

    # Top 5 by views
    print(f"\n  Top 5 by views:")
    for r in rows[:5]:
        print(f"    {r['views']:>10,}  {r['title'][:65]}")

    # Best engagement rate
    eng_rows = [(r['likes'] / r['views'] * 100, r) for r in rows if r['views'] > 100]
    if eng_rows:
        eng_rows.sort(reverse=True)
        print(f"\n  Top 5 by like rate (views > 100):")
        for rate, r in eng_rows[:5]:
            print(f"    {rate:>5.2f}%  {r['views']:>8,} views  {r['title'][:55]}")

    # Summary text file
    summary_path = Path(__file__).parent / 'analytics_summary.txt'
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write(f"Channel Analytics Report — {date.today()}\n")
        f.write(f"Channel : {ch_name}\n")
        f.write(f"Subs    : {subs}  |  Total Views: {total_views}  |  Videos: {vid_count}\n\n")
        f.write(f"Avg views/video : {avg_views:,.0f}\n")
        if avg_avd is not None:
            f.write(f"Avg view dur    : {_fmt_sec(avg_avd)}\n")
        f.write(f"\nTop 10 videos by views:\n")
        for i, r in enumerate(rows[:10], 1):
            f.write(f"  {i:>2}. {r['views']:>10,}  {r['published']}  {r['title']}\n")
    print(f"\n  Summary saved: {summary_path}")
    print()


if __name__ == '__main__':
    main()
