import json
import re
import time
import requests

_GEMINI_URL = 'https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent'

_PROMPT = """You are a viral short-form video editor working with Friends (US sitcom, 1994-2004).

Your task: Create a ~45-second reel that tells ONE complete storyline in tight, punchy cuts — like a TikTok story recap.

OUTPUT STRUCTURE (strictly in this order):
1. HOOK (first clip, 4-8 sec) — the single most shocking/funny/relatable moment from the ENTIRE storyline. Does NOT need to be the chronological start. This must grab attention in the first second. E.g. the punchline reaction, the embarrassing confession, the loudest laugh moment.
2. CONTEXT clips (3-5 clips, 3-8 sec each) — the minimum lines needed to understand the story. Only the KEY line or reaction from each scene beat. Cut everything else.
3. PAYOFF (last clip, 4-8 sec) — the final punchline, resolution, or biggest laugh.

TOTAL target: 35-55 seconds across all clips combined.

Great storylines to look for:
1. A character does something embarrassing/dumb that escalates (Ross, Joey, Chandler)
2. Phoebe's bizarre behaviour with an absurd payoff
3. A lie or scheme that unravels with great reaction shots
4. A misunderstanding that snowballs
5. Physical comedy with a clear punchline

Transcript (format: [start_sec - end_sec] text):
{transcript}

Previously used segments (AVOID these): {used_segments}

Return ONLY a valid JSON object — no markdown, no explanation:
{{
  "storyline_title": "phoebe_bites_massage_client",
  "storyline_summary": "Phoebe bites her client, panics, the gang reacts, she confesses",
  "clips": [
    {{
      "start_seconds": 87.0,
      "end_seconds": 92.0,
      "label": "hook",
      "description": "Joey's face when he realises Phoebe actually bit someone"
    }}
  ],
  "score": 9
}}

Rules:
- 4 to 8 clips total
- FIRST clip = hook (most viral moment, any timestamp)
- LAST clip = payoff/punchline/resolution
- Each clip: 3-15 seconds — just the KEY line or reaction, nothing more
- Total combined duration: 45-60 seconds
- Clips (except the hook) should be in rough CHRONOLOGICAL ORDER
- Clips must NOT overlap each other or the previously-used segments
- CRITICAL: Every clip must end at a natural sentence or speech pause — NEVER cut mid-word or mid-sentence. Extend end_seconds by 1-2s if needed to catch the full line.
- Pick moments with a clear spoken punchline or strong visual reaction
- Score 1-10 for viral potential as a compiled reel (10 = highest)
- storyline_title: snake_case, max 40 chars"""


_YT_PROMPT = """You are a YouTube content strategist for a Friends (US sitcom, 1994-2004) clips channel called "The One Where".

Generate YouTube metadata for a short-form vertical reel (under 60 seconds).

Episode : {episode_str}
Storyline: {storyline}
Summary  : {summary}
Score    : {score}/10

Return ONLY valid JSON, no markdown:
{{
  "title": "Phoebe bites her massage client 😭 | Friends S4E4",
  "description": "Phoebe actually bites someone during a massage — and the gang's reaction is everything.\\n\\nFriends Season 4, Episode 4 | \\"The One with the Ballroom Dancing\\"\\n\\n📌 Subscribe for daily Friends story reels — the funniest moments cut down to under a minute.\\n\\n#Friends #Phoebe #FriendsTV #PhoebeBuffay #FriendsClips #TVShows #Sitcom #FunnyMoments #90sTV #FriendsReels",
  "tags": "Friends, Friends TV show, Phoebe Buffay, Friends clips, Friends funny moments, Friends Season 4, sitcom, 90s TV, Lisa Kudrow, Friends compilation, massage, Friends shorts, comedy, NBC, Friends rewatch",
  "pinned_comment": "Phoebe really said bite first, apologise never 😭 Which Friends character causes the most chaos? 👇"
}}

Rules:
- title: max 70 chars, punchy hook, episode code (e.g. S4E4), exactly 1 emoji
- description: 2-line punchy hook → blank line → episode info line → blank line → subscribe CTA line → blank line → exactly 10 hashtags on one line
- tags: exactly 15 tags, comma-separated, mix of broad (Friends) and specific (character, theme)
- pinned_comment: funny/relatable observation + engagement question + 1 emoji + 👇"""


def generate_yt_metadata(story, episode_name, api_key, retries=3):
    import re
    api_key = api_key.strip()
    m = re.search(r'[Ss](\d+)[Ee](\d+)', episode_name)
    ep_str = f"S{int(m.group(1))}E{int(m.group(2))}" if m else episode_name

    prompt = _YT_PROMPT.format(
        episode_str=ep_str,
        storyline=story['storyline_title'].replace('_', ' '),
        summary=story['storyline_summary'],
        score=story['score'],
    )
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.7},
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                _GEMINI_URL,
                params={'key': api_key},
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            raw = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
            raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
            return json.loads(raw)
        except Exception as e:
            print(f"  YT metadata attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(10)
    return None


def analyze_transcript(segments, api_key, used_segments=None, retries=5):
    api_key = api_key.strip()

    transcript_text = '\n'.join(
        f"[{s['start']} - {s['end']}] {s['text']}"
        for s in segments
    )
    used_str = (
        ', '.join(f"{s:.0f}s-{e:.0f}s" for s, e in used_segments)
        if used_segments else 'none'
    )
    prompt = _PROMPT.format(transcript=transcript_text, used_segments=used_str)

    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.2},
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                _GEMINI_URL,
                params={'key': api_key},
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            raw = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
            raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
            story = json.loads(raw)
            # Log total duration so we can sanity-check
            total = sum(c['end_seconds'] - c['start_seconds'] for c in story['clips'])
            story['_total_seconds'] = round(total, 1)
            return story
        except requests.HTTPError as e:
            print(f"  Gemini attempt {attempt} failed: {e} — {e.response.text[:300]}")
            if attempt < retries:
                wait = 60 if e.response.status_code in (503, 429) else 15
                time.sleep(wait)
        except Exception as e:
            print(f"  Gemini attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(15)

    raise RuntimeError("Gemini analysis failed after all retries")
