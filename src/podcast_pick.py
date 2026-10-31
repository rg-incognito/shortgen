"""
Source picker for the `podcasts` profile: Gemini chooses a popular recent podcast episode on
YouTube, this PC downloads it with yt-dlp, and the normal pipeline clips it.

    YouTube search (recent, long, most viewed) -> candidates -> Gemini picks one -> yt-dlp

Picks are kept in logs/podcast_picks.json (newest last); an episode is clipped up to the
profile's max_runs times before Gemini is asked for the next one. No video id is hard-coded:
the choice is Gemini's, from what is popular on YouTube that week.
"""
import datetime
import json
import os
import re
from pathlib import Path

from analyzer import _gemini_post, _parse_json

STATE = Path(__file__).resolve().parent.parent / 'logs' / 'podcast_picks.json'
EPISODE_PREFIX = 'POD_'
MIN_VIEWS = 50_000


def _load():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []


def _save(picks):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(picks, indent=1, ensure_ascii=False), encoding='utf-8')


def episode_name(video_id):
    return EPISODE_PREFIX + video_id


def apply_picks(profile, picks=None):
    """Fill the profile's files / episode_notes from the saved picks (newest first, like the others)."""
    picks = _load() if picks is None else picks
    profile['files'] = [(p['id'], episode_name(p['id'])) for p in reversed(picks)]
    profile['episode_notes'] = {
        episode_name(p['id']): {k: v for k, v in (('guest', p.get('guest')), ('show', p.get('channel')),
                                                  ('about', p.get('title'))) if v}
        for p in picks}
    return profile


def _iso_minutes(d):
    """ISO 8601 duration (PT1H2M3S) -> minutes."""
    m = re.fullmatch(r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', d or '')
    if not m:
        return 0.0
    days, h, mi, s = (int(x or 0) for x in m.groups())
    return days * 1440 + h * 60 + mi + s / 60


def candidates(profile, svc=None, now=None):
    """Popular long English videos from the last `pick_days` days for the profile's queries."""
    if svc is None:
        from youtube_client import _build_svc
        svc = _build_svc()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    after = (now - datetime.timedelta(days=profile.get('pick_days', 14))).strftime('%Y-%m-%dT%H:%M:%SZ')
    ids = []
    for q in profile['pick_queries']:
        resp = svc.search().list(part='id', q=q, type='video', order='viewCount', publishedAfter=after,
                                 videoDuration='long', relevanceLanguage='en', regionCode='US',
                                 maxResults=25).execute()
        ids += [it['id']['videoId'] for it in resp.get('items', []) if it['id'].get('videoId')]
    ids = list(dict.fromkeys(ids))
    out = []
    for i in range(0, len(ids), 50):
        resp = svc.videos().list(part='snippet,contentDetails,statistics', id=','.join(ids[i:i + 50])).execute()
        for v in resp.get('items', []):
            sn = v['snippet']
            out.append({'id': v['id'], 'title': sn['title'], 'channel': sn['channelTitle'],
                        'published': sn['publishedAt'][:10],
                        'minutes': round(_iso_minutes(v['contentDetails']['duration'])),
                        'views': int(v.get('statistics', {}).get('viewCount', 0)),
                        'lang': (sn.get('defaultAudioLanguage') or sn.get('defaultLanguage') or '')[:2]})
    return out


def eligible(cands, profile, picked_ids=()):
    lo, hi = profile.get('pick_minutes', (20, 100))
    return sorted((c for c in cands
                   if c['id'] not in picked_ids and lo <= c['minutes'] <= hi
                   and c['views'] >= MIN_VIEWS and c['lang'] in ('', 'en')),
                  key=lambda c: -c['views'])


def _prompt(cands, profile):
    lines = '\n'.join(f"{c['id']} | {c['channel']} | {c['title']} | {c['views']:,} views | "
                      f"{c['published']} | {c['minutes']} min" for c in cands)
    return f"""You run a YouTube Shorts clip channel. Pick the ONE podcast episode below that will give the most
viral 15-20 second Shorts this week.

Channel niche: {profile['pick_niche']}

Prefer: a famous host or guest people search for, lots of views for its age, two or three people talking
on camera, funny / shocking / emotional stories and strong opinions.
Never pick: politics or elections, news, music videos, sermons, kids content, non-English, compilations,
livestreams, audio-only or slideshow videos, or anything that is itself a clip compilation.

Candidates (id | channel | title | views | published | length):
{lines}

Reply with JSON only: {{"id": "<video id from the list>", "guest": "<main guest's name, or the host's if
there is no guest>", "why": "<one sentence>"}}"""


def ask_gemini(cands, profile, api_key):
    text = _gemini_post({'contents': [{'parts': [{'text': _prompt(cands, profile)}]}],
                         'generationConfig': {'temperature': 0.4}}, api_key.strip())
    choice = _parse_json(text) or {}
    by_id = {c['id']: c for c in cands}
    if choice.get('id') not in by_id:
        raise RuntimeError(f"Gemini picked a video that is not a candidate: {text[:200]}")
    return dict(by_id[choice['id']], guest=(choice.get('guest') or '').strip() or None,
                why=choice.get('why', ''))


def pick(profile, tracker, max_runs, api_key, svc=None):
    """(video id, episode name): the newest pick with runs left, else a new Gemini pick."""
    picks = _load()
    apply_picks(profile, picks)
    for p in reversed(picks):
        if tracker.get_run_count(episode_name(p['id'])) < max_runs:
            return p['id'], episode_name(p['id'])
    cands = eligible(candidates(profile, svc), profile, {p['id'] for p in picks})
    if not cands:
        raise RuntimeError('No podcast candidates on YouTube matched the filters')
    choice = ask_gemini(cands[:40], profile, api_key)
    choice['picked'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')
    print(f"      Gemini picked {choice['id']}: {choice['channel']} - {choice['title']} "
          f"({choice['views']:,} views, {choice['minutes']} min) — {choice['why']}")
    picks.append(choice)
    _save(picks)
    apply_picks(profile, picks)
    return choice['id'], episode_name(choice['id'])


def download(video_id, output_path):
    """720p mp4 of a YouTube video (works from this PC; YouTube blocks CI runners)."""
    import yt_dlp
    out = str(Path(output_path).with_suffix(''))
    opts = {'format': 'bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]',
            'merge_output_format': 'mp4', 'outtmpl': out + '.%(ext)s', 'quiet': True,
            'no_warnings': True, 'noplaylist': True, 'retries': 5}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
    if not os.path.exists(output_path):
        raise RuntimeError(f'yt-dlp did not produce {output_path}')
