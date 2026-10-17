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


_LIB_PROMPT = """You are a short-form video editor clipping Netflix's dating reality show "Love Is Blind" (Season 11) for an official Netflix-approved clipping campaign. The goal: get people talking about the show and send them to Netflix.

Find the SINGLE best standalone moment in this episode for a vertical YouTube Short.

WHAT WORKS (ranked):
1. Drama or a plot twist — a confession, a secret coming out, a fight in the pods, an unexpected answer, a cliffhanger line
2. Real, raw conversation between cast members — a vulnerable admission, a hard question, a moment of chemistry
3. Sweet or funny moments — a proposal, a first reaction, a laugh that lands, an awkward beat played for comedy

HARD RULES (a clip that breaks any of these is rejected by the brand):
- ONE continuous scene. 1-3 clips from that same scene, back-to-back, in chronological order.
- Total duration 25-45 seconds (absolute limits 18-55 seconds). It must feel complete on its own: a setup and a payoff, not a fragment.
- The moment must have a clear story — no random footage, no "nothing happens" clips.
- Never pick content that mocks or humiliates a cast member, anything sexual/explicit, violent, political, or racist.
- Start at the natural start of the beat (mid-sentence is fine if that's how it plays). End on a finished line or a reaction — NEVER mid-word or mid-sentence. Extend end_seconds by 1-2s to catch the full line.
- Clips must NOT overlap each other or the previously used segments.

Previously used moments from this episode (AVOID these themes): {used_storylines}

Previously used segments (AVOID these timestamps): {used_segments}

Transcript (format: [start_sec - end_sec] text):
{transcript}

Return ONLY a valid JSON object — no markdown, no explanation:
{{
  "format": "standalone_scene",
  "storyline_title": "pods_secret_confession",
  "storyline_summary": "She admits something in the pods that changes how he sees her",
  "series_tag": "love_is_blind",
  "hook_text": "SHE DIDN'T EXPECT THAT ANSWER",
  "hook_emoji": "😳",
  "clips": [
    {{
      "start_seconds": 812.0,
      "end_seconds": 846.0,
      "label": "scene",
      "description": "Her confession and his stunned reaction"
    }}
  ],
  "score": 8
}}

Field rules:
- storyline_title: snake_case, max 40 chars
- series_tag: always "love_is_blind"
- hook_text: 3-8 words, ALL CAPS, teases the moment WITHOUT spoiling the payoff. Neutral or warm — NEVER insulting a cast member (no "crazy", "psycho", "delusional", "toxic", etc.) and nothing sexual. Do not name any other show or streaming service. It is burned onto the video as a banner for the whole clip.
- hook_emoji: 1-2 emoji that fit the moment (😳 😭 👀 🥹 😬 💍 ❤️ …) or "" if none fit
- score: 1-10 viral potential"""


_LIB_YT_PROMPT = """You write YouTube Shorts metadata for an official clip of Netflix's "Love Is Blind" (Season 11).

Episode  : {episode_str}
Moment   : {storyline}
Summary  : {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "She did not see that answer coming 😳 #loveisblind",
  "description": "One question in the pods and everything changed.\\nWould you have stayed after hearing that?\\n\\nLove Is Blind Season 11 is streaming now on Netflix.\\n\\n#loveisblind #LoveIsBlindNetflix #Netflix #realitytv #dating",
  "tags": "Love Is Blind, Love Is Blind Netflix, Love Is Blind Season 11, Netflix, Netflix reality, reality TV, dating show, the pods, Love Is Blind clips, Love Is Blind moments, engaged, proposal, relationships, dating, reality tv clips",
  "pinned_comment": "Would you have said yes after that? 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens in this clip. Do not invent events, names or outcomes.
- title: max 90 chars, curiosity-driven, does not spoil the payoff, ends with #loveisblind. Warm or neutral tone.
- description: 2-line hook → blank line → a line that contains both "Love Is Blind" and "Netflix" → blank line → hashtags line that includes #loveisblind (max 5 hashtags)
- tags: exactly 15, comma-separated
- NEVER mock, insult or judge a cast member (no "crazy", "psycho", "delusional", "toxic", "red flag" about a person, etc.)
- NEVER mention any other TV show, streaming service or platform — only Netflix.
- Nothing sexual, political or racist.
- pinned_comment: a question that invites opinions on the situation (not on anyone's looks or character)."""


_SOLVED_PROMPT = """You are a short-form editor clipping "Solved with Mark Manson" (self-improvement podcast) for an official, brand-approved clipping campaign.

Find the SINGLE best standalone moment in this episode for a vertical YouTube Short.

WHAT WORKS — pick ONE of these shapes:
1. Blunt reframe — Mark states a counter-intuitive idea with no setup needed ("Most people think X. It's actually Y.")
2. Problem → answer — a common struggle named, then Mark's clear answer
3. Short story with a turn — a story that pivots and lands on a point
Best topics for YouTube: values, purpose, big life questions, personal growth, decision-making, failure, anxiety, meaning.

HARD RULES (the brand rejects clips that break these):
- ONE continuous passage. 1-2 clips, back-to-back, chronological. Total 30-50 seconds (absolute limits 22-55s).
- It must be a COMPLETE thought: starts where the idea starts, ends after the point lands. Never cut mid-thought or mid-sentence. Extend end_seconds by 1-2s to catch the full line.
- The clip must OPEN with Mark Manson himself speaking (he must be on screen in the first 5 seconds). If a co-host or guest speaks first in the passage, start later.
- NEVER include ad reads, sponsor mentions, promo codes, "this episode is brought to you by", or any other brand.
- Never change the meaning of what was said — don't start or stop the clip so a quote means something different.
- Nothing NSFW, violent, or political.
- Clips must NOT overlap each other or the previously used segments (these also include known ad-read segments).

Previously used moments from this episode (AVOID these ideas): {used_storylines}

Previously used / forbidden segments (AVOID these timestamps): {used_segments}

Transcript (format: [start_sec - end_sec] text):
{transcript}

Return ONLY a valid JSON object — no markdown, no explanation:
{{
  "format": "standalone_scene",
  "storyline_title": "failure_is_the_tuition",
  "storyline_summary": "Mark argues failure feels worse than it is because we treat it as identity instead of information",
  "series_tag": "solved",
  "hook_text": "WHY FAILURE HURTS MORE THAN IT SHOULD",
  "hook_emoji": "",
  "clips": [
    {{
      "start_seconds": 512.0,
      "end_seconds": 551.0,
      "label": "scene",
      "description": "Mark reframes failure as information, ends on the key line"
    }}
  ],
  "score": 8
}}

Field rules:
- storyline_title: snake_case, max 40 chars
- series_tag: always "solved"
- hook_text: 3-9 words, ALL CAPS, states the idea or the question the clip answers — accurate to what Mark actually says, not clickbait. Burned onto the video as a banner for the whole clip.
- hook_emoji: usually "" for this show; at most one subtle emoji if it really fits
- score: 1-10 viral potential"""


_SOLVED_YT_PROMPT = """You write YouTube Shorts metadata for an official clip of the podcast "Solved with Mark Manson".

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "Failure isn't the problem. This is.",
  "description": "Most people treat failure as a verdict on who they are. Mark Manson explains why it's just information.\\n\\nFrom Solved with Mark Manson @solvedpodcast @iammarkmanson\\n\\n#markmanson #solvedpodcast #selfimprovement #psychology #mindset",
  "tags": "Mark Manson, Solved with Mark Manson, Solved podcast, self improvement, personal growth, psychology, mindset, failure, motivation, life advice, podcast clips, Mark Manson podcast, meaning, purpose, self help",
  "pinned_comment": "Do you agree with Mark on this? 👇"
}}

Rules (brand requirements — breaking them gets the clip rejected):
- Lead with the IDEA in the clip, not a summary of the show. Describe only what Mark actually says — never misrepresent him, no clickbait.
- title: max 80 chars, punchy, no emoji spam.
- description: 1-2 lines on the idea → blank line → a line containing "@solvedpodcast" and "@iammarkmanson" → blank line → max 5 hashtags.
- tags: exactly 15, comma-separated.
- Never mention sponsors, promo codes or other brands. Nothing political or NSFW.
- pinned_comment: a genuine question about the idea that invites discussion."""


_HS_SCENE_RULES = """HARD RULES (the brand rejects clips that break these):
- ONE continuous scene. 1-3 clips from that same scene, back-to-back, in chronological order.
- Total duration 25-45 seconds (absolute limits 18-55 seconds). Hook in the first 2 seconds: start at (or right before) the wildest line or reaction, keep any setup short.
- End on a finished line or a reaction — NEVER mid-word or mid-sentence. Extend end_seconds by 1-2s to catch the full line. It is fine to cut before the full resolution so people go watch the episode.
- Real moments only: never pick a cut that changes what someone meant.
- NEVER pick anything that mentions FaZe, anything political, sexual/explicit, or that makes HardScope, the host or a guest look bad (no humiliation, no "scam"/"rigged" angles).
- Clips must NOT overlap each other or the previously used segments.

Previously used moments from this episode (AVOID these themes): {used_storylines}

Previously used segments (AVOID these timestamps): {used_segments}

Transcript (format: [start_sec - end_sec] text):
{transcript}
"""

_HS_FIELD_RULES = """
Field rules:
- storyline_title: snake_case, max 40 chars
- series_tag: always "{tag}"
- hook_text: 3-8 words, ALL CAPS, teases the moment without spoiling the payoff, accurate to what happens — no lying clickbait. Never mention FaZe or any other channel. Burned onto the video as a banner for the whole clip.
- hook_emoji: 1-2 emoji that fit the moment or "" if none fit
- score: 1-10 viral potential"""

_LJ_PROMPT = """You are a short-form video editor clipping "Love & Justice", Zach Justice's relationship show on the HardScope YouTube channel, for an official brand-approved clipping campaign. Real couples bring one relationship issue to Zach, he hears both sides and delivers a verdict on who is at fault.

Find the SINGLE best standalone moment in this episode for a vertical YouTube Short.

WHAT WORKS (ranked):
1. The verdict or a big reveal — Zach calling who is at fault, a partner admitting something, a twist in the story
2. A heated or hilarious exchange between the couple, or Zach's sharpest reaction to what he hears
3. A relatable relationship question with a clear answer

""" + _HS_SCENE_RULES + """
Return ONLY a valid JSON object — no markdown, no explanation:
{{
  "format": "standalone_scene",
  "storyline_title": "zach_rules_who_is_wrong",
  "storyline_summary": "After hearing both sides, Zach tells the couple exactly who is at fault",
  "series_tag": "love_and_justice",
  "hook_text": "ZACH DID NOT HOLD BACK",
  "hook_emoji": "⚖️",
  "clips": [
    {{
      "start_seconds": 812.0,
      "end_seconds": 846.0,
      "label": "scene",
      "description": "Zach's verdict and the couple's reaction"
    }}
  ],
  "score": 8
}}
""" + _HS_FIELD_RULES.replace('{tag}', 'love_and_justice')

_LJ_YT_PROMPT = """You write YouTube Shorts metadata for an official clip of "Love & Justice", Zach Justice's relationship show on HardScope.

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "He thought he was right... until Zach heard her side ⚖️",
  "description": "She told her side and the whole story flipped.\\nWho do you think was wrong?\\n\\nFull episode of Love & Justice on @HardScopeTV\\n\\n#LOVEANDJUSTICE #relationships #couples #dating",
  "tags": "Love and Justice, Zach Justice, HardScope, relationship advice, couples, dating, relationships, who is wrong, relationship drama, couple argument, relationship show, dating advice, boyfriend, girlfriend, verdict",
  "pinned_comment": "Who was actually wrong here? 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens in this clip. Do not invent events, names or outcomes. No misleading or damaging titles.
- title: max 90 chars, conversational, curiosity-driven, like a fan posting.
- description: 1-2 short lines on what happens → blank line → a line containing "@HardScopeTV" → blank line → hashtags line that includes #LOVEANDJUSTICE (max 5 hashtags)
- tags: exactly 15, comma-separated
- NEVER mention FaZe, other channels, or anything political or sexual. Never insult the couple.
- pinned_comment: a question that invites opinions on who was right."""

_UNPACKED_PROMPT = """You are a short-form video editor clipping "Unpacked", MatCrackz's collectibles show on the HardScope YouTube channel, for an official brand-approved clipping campaign. Celebrity guests hunt down their most-wanted collectibles and memorabilia — rare trading cards, big pulls, huge prices.

Find the SINGLE best standalone moment in this episode for a vertical YouTube Short.

WHAT WORKS (ranked):
1. A big pull or reveal — the moment a rare card or item appears and everyone reacts
2. A jaw-dropping price or a high-stakes decision ("that's $60,000")
3. A funny or hype exchange between MatCrackz and the celebrity guest

""" + _HS_SCENE_RULES + """
Return ONLY a valid JSON object — no markdown, no explanation:
{{
  "format": "standalone_scene",
  "storyline_title": "sixty_thousand_dollar_pull",
  "storyline_summary": "The guest opens the pack and pulls the card everyone was hoping for",
  "series_tag": "unpacked",
  "hook_text": "HE PULLED THE $60K CARD",
  "hook_emoji": "🤯",
  "clips": [
    {{
      "start_seconds": 812.0,
      "end_seconds": 846.0,
      "label": "scene",
      "description": "The pull and the reaction"
    }}
  ],
  "score": 8
}}
""" + _HS_FIELD_RULES.replace('{tag}', 'unpacked')

_UNPACKED_YT_PROMPT = """You write YouTube Shorts metadata for an official clip of "Unpacked", MatCrackz's collectibles show on HardScope.

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "He did NOT expect to pull this card 🤯",
  "description": "One pack and the whole room lost it.\\nWould you have kept it or sold it?\\n\\nWatch the full episode of Unpacked on @HardScopeTV\\n\\n#unpacked #tradingcards #collectibles #packopening",
  "tags": "Unpacked, HardScope, MatCrackz, trading cards, pack opening, rare cards, collectibles, card pulls, sports cards, pokemon cards, memorabilia, big pull, card collecting, hobby, rare pull",
  "pinned_comment": "Keep it or sell it? 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens in this clip. Do not invent names, prices or outcomes. No misleading or damaging titles.
- title: max 90 chars, conversational, like a fan posting.
- description: 1-2 short lines on what happens → blank line → a line containing "@HardScopeTV" → blank line → hashtags line that includes #unpacked (max 5 hashtags)
- tags: exactly 15, comma-separated
- NEVER mention FaZe, other channels, or anything political or sexual. Nothing that frames the show as fake or a scam.
- pinned_comment: a short question that invites opinions on the moment."""


_PROMPTS = {
    'loveisblind':    (_LIB_PROMPT, _LIB_YT_PROMPT),
    'markmanson':     (_SOLVED_PROMPT, _SOLVED_YT_PROMPT),
    'loveandjustice': (_LJ_PROMPT, _LJ_YT_PROMPT),
    'unpacked':       (_UNPACKED_PROMPT, _UNPACKED_YT_PROMPT),
}


def generate_yt_metadata(story, episode_name, api_key, retries=3, campaign='friends'):
    import re
    api_key = api_key.strip()
    m = re.search(r'[Ss](\d+)[Ee](\d+)', episode_name)
    ep_str = f"S{int(m.group(1))}E{int(m.group(2))}" if m else episode_name

    template = _PROMPTS.get(campaign, (_PROMPT, _YT_PROMPT))[1]
    prompt = template.format(
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


def analyze_transcript(segments, api_key, used_segments=None, used_storylines=None, retries=5,
                       campaign='friends'):
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
    template = _PROMPTS.get(campaign, (_PROMPT, _YT_PROMPT))[0]
    prompt = template.format(
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
