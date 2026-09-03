import json
import re
import time
import requests

_GEMINI_BASE    = 'https://generativelanguage.googleapis.com/v1/models/{}:generateContent'
_FALLBACK_MODELS = ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-2.0-flash']


def _gemini_post(payload, api_key, timeout=120):
    """POST to Gemini with automatic model fallback on 503/429."""
    for i, model in enumerate(_FALLBACK_MODELS):
        try:
            resp = requests.post(
                _GEMINI_BASE.format(model),
                params={'key': api_key},
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            if i > 0:
                print(f"  Success with fallback model: {model}")
            return resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        except requests.HTTPError as e:
            if e.response.status_code in (503, 429) and i < len(_FALLBACK_MODELS) - 1:
                print(f"  {model} returned {e.response.status_code}, trying {_FALLBACK_MODELS[i+1]}")
                time.sleep(5)
            else:
                raise

_PROMPT = """You are a viral short-form video editor working with Friends (US sitcom, 1994-2004).

Your task: find the BEST short-form moment in this episode for a vertical reel under 35 seconds, and choose the FORMAT that fits it best.

FORMAT "standalone_scene" — use when ONE scene already works as a complete moment on its own:
- A single continuous scene: an iconic monologue, a quotable line, a tight back-and-forth with a clean punchline, or one big reaction beat
- 1-3 clips from that ONE scene, back-to-back (small dead-air/cutaway trims between them are fine)
- Total duration: 10-25 seconds
- Must be fully funny/emotional WITHOUT any other clips for context
- First clip starts at the natural beginning of the moment — mid-sentence is fine if that's how it plays, but don't force an artificial "hook" cut from elsewhere

FORMAT "story_recap" — use when the best moment NEEDS setup from earlier in the episode to land:
1. HOOK (first clip, 3-5 sec) — the single most shocking/funny/relatable moment from the ENTIRE storyline. Does NOT need to be the chronological start. CRITICAL: the very first frame must start MID-ACTION — the character must already be mid-sentence, mid-reaction, or mid-punchline. NEVER open on someone walking in, sitting down, looking around, or any establishing shot. Start on the embarrassed face already in frame, the punchline already being delivered, the shocked reaction already happening.
2. CONTEXT clips (2-4 clips, 3-6 sec each) — the minimum lines needed to understand why the hook moment is funny or emotionally powerful. Only the single KEY line or reaction per beat. Cut everything else.
3. PAYOFF (last clip, 3-5 sec) — the final punchline, resolution, emotional reveal, or biggest laugh.
TOTAL target: 25-30 seconds across all clips combined.

Default to "standalone_scene" when a single scene is strong enough on its own — it feels like a real clip rather than an edit and often performs better. Use "story_recap" only when no single scene can stand alone and you genuinely need to connect a setup to a later payoff.

Great storylines/moments to look for — RANKED BY VIRAL POTENTIAL:
TIER 1 (highest views — pick these first):
1. A romantic revelation or confession — Ross telling Rachel he loves her, Chandler proposing to Monica, any "I've always loved you" moment
2. A plot twist or secret exposed — someone finds out something that changes everything, a lie finally unravels in front of everyone
3. An emotional moment that also lands funny — a character being vulnerable then immediately undercut by a joke
4. A relationship turning point — first kiss, breakup, getting back together, a jealousy moment with a great reaction shot

TIER 2 (solid performers):
5. A character does something embarrassing/dumb that escalates (Ross, Joey, Chandler)
6. Phoebe's bizarre behaviour with an absurd payoff
7. A lie or scheme that unravels with great reaction shots
8. A misunderstanding that snowballs
9. Physical comedy with a clear punchline

Previously used storylines from this episode (AVOID repeating these themes): {used_storylines}

Transcript (format: [start_sec - end_sec] text):
{transcript}

Previously used segments (AVOID these timestamps): {used_segments}

Return ONLY a valid JSON object — no markdown, no explanation:
{{
  "format": "standalone_scene",
  "storyline_title": "phoebe_bites_massage_client",
  "storyline_summary": "Phoebe bites her client mid-massage and tries to play it off",
  "series_tag": "phoebe_being_phoebe",
  "hook_text": "PHOEBE BITES HER MASSAGE CLIENT",
  "hook_emoji": "😳",
  "clips": [
    {{
      "start_seconds": 87.0,
      "end_seconds": 92.0,
      "label": "scene",
      "description": "Phoebe bites her client and immediately tries to act normal"
    }}
  ],
  "score": 9
}}

Rules:
- If format = "standalone_scene": 1-3 clips from ONE scene, label each "scene", total 10-25 seconds, clips in chronological order, may skip small dead-air/cutaway gaps but must otherwise read as one continuous moment
- If format = "story_recap": 4-6 clips, FIRST clip labeled "hook" (most viral moment, any timestamp, first frame mid-action, never an establishing shot), LAST clip labeled "payoff" (punchline/resolution), middle clips labeled "context" (3-6 sec each, just the KEY line or reaction), total 25-30 seconds, clips other than the hook in rough CHRONOLOGICAL ORDER
- Clips must NOT overlap each other or the previously-used segments
- CRITICAL: Every clip must end at a natural sentence or speech pause — NEVER cut mid-word or mid-sentence. Extend end_seconds by 1-2s if needed to catch the full line.
- Pick moments with a clear spoken punchline or strong visual reaction
- Score 1-10 for viral potential (10 = highest)
- storyline_title: snake_case, max 40 chars
- series_tag: pick exactly one — ross_being_ross | joey_being_joey | chandler_being_chandler | phoebe_being_phoebe | monica_being_monica | rachel_being_rachel | relationship_drama | gang_together
- hook_text: 3-8 words, ALL CAPS, describing the clip's premise/moment WITHOUT giving away the punchline (e.g. "SHE BIT HER MASSAGE CLIENT", "ROSS SAYS THE WRONG NAME AT THE ALTAR", "THE ONE WITH THE FAKE BOYFRIEND"). This is burned onto the video as a persistent white banner across the top of the ENTIRE video (not just the opening) — it can be a touch more descriptive than a quick flash-hook since it stays visible throughout. Still separate from and shorter than the YouTube title.
- hook_emoji: 1-2 emoji matching the COMEDIC reaction to this specific moment (e.g. 😳 😂 💀 🤦 😬 😏 👀 🙈 — pick whatever actually fits the clip's humor, don't default to the same one or two every time), or "" if none fit naturally — never force one"""


_YT_PROMPT = """You are a YouTube content strategist for a Friends (US sitcom, 1994-2004) clips channel called "The One Where".

Generate YouTube metadata for a short-form vertical reel (under 60 seconds).

Episode  : {episode_str}
Storyline: {storyline}
Summary  : {summary}
Series   : {series_tag}
Score    : {score}/10

Return ONLY valid JSON, no markdown:
{{
  "title": "Nobody saw this coming from Phoebe",
  "description": "Phoebe actually bites someone during a massage — and the gang's reaction is everything.\\n\\nFriends | The One with the Ballroom Dancing\\n\\n📌 Subscribe for daily Friends moments — the best scenes cut down to under a minute.\\n\\n#Friends #FriendsTV #PhoebeBuffay #FriendsClips #Sitcom #FunnyMoments #90sTV #PhoebeBeingPhoebe #FriendsShorts #NBC",
  "tags": "Friends, Friends TV show, Phoebe Buffay, Friends clips, Friends funny moments, Friends compilation, sitcom, 90s TV, Lisa Kudrow, Friends shorts, comedy, NBC, Friends rewatch, Phoebe massage, Friends moments",
  "pinned_comment": "Phoebe really said bite first, apologise never 😭 Team Phoebe or Team Client? 👇"
}}

Rules:
- title: Write a concise, curiosity-driven tease (max 80 chars) that hints at the clip's most impactful moment WITHOUT revealing what happens. Open a curiosity gap — the viewer MUST watch to find out. NEVER write a narrative sentence that describes the outcome or spoils the punchline. NO episode code. NO character brand prefix. NO emoji in the middle. Frame it as tension or surprise: "The moment [character] crossed a line", "Nobody expected [character] to do this", "Wait… did [character] really just say that?" — tease the feeling, not the resolution. One optional emoji at the very end only if it fits naturally.
- The best titles make viewers NEED to click — they hint at a shocking or funny situation without giving away what happens
- description: 2-line punchy hook → blank line → show + episode name line (no season/episode code) → blank line → subscribe CTA → blank line → exactly 10 hashtags on one line
- tags: exactly 15 tags, comma-separated, mix of broad (Friends) and specific (character, theme, relationship)
- For relationship_drama series_tag, add ship hashtags to description: #RossAndRachel or #ChandlerAndMonica depending on who's involved
- pinned_comment: must be ONE of these formats — (a) polarizing take specific to this clip: "Team X or Team Y? 👇", (b) ranking prompt: "Rate this [character] moment 1-10 👇", (c) completion gap: "What did [character] say next? 👇". NEVER generic. Must reference the specific characters or situation from THIS clip."""


def generate_yt_metadata(story, episode_name, api_key, retries=3):
    import re
    api_key = api_key.strip()
    m = re.search(r'[Ss](\d+)[Ee](\d+)', episode_name)
    ep_str = f"S{int(m.group(1))}E{int(m.group(2))}" if m else episode_name

    prompt = _YT_PROMPT.format(
        episode_str=ep_str,
        storyline=story['storyline_title'].replace('_', ' '),
        summary=story['storyline_summary'],
        series_tag=story.get('series_tag', 'gang_together'),
        score=story['score'],
    )
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.7},
    }
    for attempt in range(1, retries + 1):
        try:
            raw = _gemini_post(payload, api_key, timeout=60)
            raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
            raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
            return json.loads(raw)
        except Exception as e:
            print(f"  YT metadata attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(10)
    return None


def analyze_transcript(segments, api_key, used_segments=None, used_storylines=None, retries=5):
    api_key = api_key.strip()

    transcript_text = '\n'.join(
        f"[{s['start']} - {s['end']}] {s['text']}"
        for s in segments
    )
    used_str = (
        ', '.join(f"{s:.0f}s-{e:.0f}s" for s, e in used_segments)
        if used_segments else 'none'
    )
    storylines_str = (
        '; '.join(used_storylines) if used_storylines else 'none'
    )
    prompt = _PROMPT.format(
        transcript=transcript_text,
        used_segments=used_str,
        used_storylines=storylines_str,
    )

    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.2},
    }

    for attempt in range(1, retries + 1):
        try:
            raw = _gemini_post(payload, api_key, timeout=120)
            raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
            raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
            story = json.loads(raw)
            total = sum(c['end_seconds'] - c['start_seconds'] for c in story['clips'])
            story['_total_seconds'] = round(total, 1)
            return story
        except Exception as e:
            print(f"  Gemini attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(15)

    raise RuntimeError("Gemini analysis failed after all retries")
