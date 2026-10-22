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


# ---------------------------------------------------------------------------
# Sentence index: the LLM picks sentence numbers, code turns them into cut times
# ---------------------------------------------------------------------------
# Whisper segments are coarse (often several sentences and speakers), so asking
# the LLM for seconds made it guess sub-segment cuts and open or end mid-line.
_TERMINAL        = re.compile(r'[.?!]["\')\]]*$')
_TRAILING        = re.compile(r'(\.\.\.|…)["\')\]]*$')
_ABBREV          = {'mr.', 'mrs.', 'ms.', 'dr.', 'st.', 'jr.', 'sr.', 'vs.'}
_SENT_GAP        = 0.7    # a pause this long ends a line even without punctuation
_NEW_THOUGHT_GAP = 1.5    # ...and one this long starts a fresh thought
_SENT_MAX        = 15.0   # split run-ons so sentence numbers stay fine-grained
_LEAD_PAD        = 0.10   # breath kept before the first word
_TAIL_HOLD       = 0.5    # let the last word's tone finish, never into the next word
_SNAP_MAX        = 6.0    # how far a cut may move to reach a whole-sentence boundary

TARGET_SECONDS = (15, 30)   # retention sweet spot; profiles hold the hard limits
N_CANDIDATES   = (3, 5)

# First words that make a scrolling viewer leave: filler, connectives, setup.
FILLER_OPENERS = {'so', 'um', 'umm', 'uh', 'uhh', 'er', 'erm', 'hmm', 'mm', 'mhm', 'okay', 'ok',
                  'well', 'yeah', 'and', 'but', 'like', 'anyway', 'anyways', 'alright', 'right',
                  'because', 'cause', 'also'}
FILLER_PHRASES = {'you know', 'i mean', 'kind of', 'sort of'}
_SETUP_OPENER  = re.compile(
    r"^\W*(so\s+)?(today\b|welcome\b|hi\b|hello\b|hey (guys|everyone|everybody)\b|what'?s up\b|"
    r"let me (tell|explain)\b|let'?s (talk|get into|start)\b|in this (video|episode)\b|"
    r"we'?re (going|gonna) (to )?\b|i'?m (going|gonna) (to )?(explain|tell|show)\b|"
    r"the (reason|thing) (is|was)\b|basically\b|first of all\b)", re.IGNORECASE)
_SETUP_DESC    = re.compile(r'^\W*(explain|introduc|setting up|sets? up|intro\b|context\b|background\b|describ)',
                            re.IGNORECASE)


def _is_terminal(token):
    t = token.strip().lower()
    return bool(_TERMINAL.search(t)) and not _TRAILING.search(t) and t not in _ABBREV


def build_sentences(words, segments=None):
    """
    Numbered sentences from Whisper word timestamps. A line ends on . ? ! or a
    pause over _SENT_GAP. Each one carries its word range, whether it is a
    complete sentence and whether it starts a new thought (the previous line
    was complete or a long pause came first). Without word timestamps each
    Whisper segment is one line.
    """
    if not words:
        out = []
        for s in segments or []:
            text = s['text'].strip()
            out.append({'id': len(out), 'start': s['start'], 'end': s['end'], 'text': text,
                        'w0': None, 'w1': None, 'complete': _is_terminal(text),
                        'clean_start': not out or out[-1]['complete']})
        return out

    sents, first = [], 0
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        terminal = _is_terminal(w['word'])
        pause = nxt is None or nxt['start'] - w['end'] > _SENT_GAP
        too_long = w['end'] - words[first]['start'] > _SENT_MAX
        if terminal or pause or too_long or _TRAILING.search(w['word']):
            prev = sents[-1] if sents else None
            gap_before = words[first]['start'] - words[first - 1]['end'] if first else 99.0
            sents.append({
                'id':          len(sents),
                'start':       words[first]['start'],
                'end':         w['end'],
                'text':        ' '.join(x['word'] for x in words[first:i + 1]).strip(),
                'w0':          first,
                'w1':          i,
                'complete':    terminal,
                'clean_start': prev is None or prev['complete'] or gap_before >= _NEW_THOUGHT_GAP,
            })
            first = i + 1
    return sents


def format_sentences(sents):
    return '\n'.join(f"S{s['id']} [{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in sents)


def snap_range(sents, a, b):
    """
    Widen sentences [a, b] so the cut opens at the start of a thought and ends
    on a complete sentence, moving at most _SNAP_MAX seconds each way. None if
    no whole-sentence cut is that close.
    """
    a0, b0 = a, b
    while (not sents[a]['clean_start'] and a > 0
           and sents[a0]['start'] - sents[a - 1]['start'] <= _SNAP_MAX):
        a -= 1
    while (not sents[b]['complete'] and b + 1 < len(sents)
           and sents[b + 1]['end'] - sents[b0]['end'] <= _SNAP_MAX):
        b += 1
    if not sents[a]['clean_start'] or not sents[b]['complete']:
        return None
    return a, b


def clip_times(sents, words, a, b):
    """Cut times for sentences a..b: just before the first word, just after the last, never into a neighbour."""
    s, e = sents[a], sents[b]
    if s['w0'] is None:
        return s['start'], e['end']
    first, last = words[s['w0']], words[e['w1']]
    start = first['start'] - _LEAD_PAD
    if s['w0'] > 0:
        start = max(start, words[s['w0'] - 1]['end'] + 0.01)
    end = last['end'] + _TAIL_HOLD
    if e['w1'] + 1 < len(words):
        end = min(end, words[e['w1'] + 1]['start'] - 0.05)
    end = max(end, last['end'])
    return round(max(0.0, start), 3), round(end, 3)


def _sid(v):
    """Sentence number from 12, "12" or "S12"."""
    return int(re.search(r'\d+', str(v)).group())


def _range_from_seconds(sents, start, end):
    """Fallback for a clip given in seconds: the sentences it covers."""
    inside = [s['id'] for s in sents if s['end'] > start + 0.2 and s['start'] < end - 0.2]
    if not inside:
        raise ValueError('no sentences in range')
    return inside[0], inside[-1]


def build_candidate(raw, sents, words):
    """
    Turn one LLM candidate into a story with exact cut times. Returns
    (story, problems); story is None when its sentence numbers are unusable.
    """
    clips, ranges = [], []
    for c in raw.get('clips') or []:
        try:
            if 'start_sentence' in c:
                a = _sid(c['start_sentence'])
                b = _sid(c.get('end_sentence', c['start_sentence']))
            else:
                a, b = _range_from_seconds(sents, float(c['start_seconds']), float(c['end_seconds']))
        except (AttributeError, TypeError, ValueError, KeyError):
            return None, ['clip has no usable sentence numbers']
        if not 0 <= a <= b < len(sents):
            return None, [f'sentence range S{a}-S{b} is reversed or out of range']
        snapped = snap_range(sents, a, b)
        if not snapped:
            return None, [f'S{a}-S{b} cannot be cut on whole sentences']
        a, b = snapped
        start, end = clip_times(sents, words, a, b)
        ranges.append((a, b))
        clips.append({'start_seconds': start, 'end_seconds': end,
                      'label': c.get('label', 'scene'), 'description': c.get('description', '')})
    if not clips:
        return None, ['no clips']

    story = dict(raw)
    story['clips'] = clips
    story['_sentences'] = ranges
    story['_llm_opening'] = str(raw.get('opening_line') or '')
    # The real first/last lines (not the LLM's quote) drive the hook and spoiler checks.
    story['opening_line'] = sents[ranges[0][0]]['text']
    story['payoff_line'] = sents[ranges[-1][1]]['text']
    story['_total_seconds'] = round(sum(c['end_seconds'] - c['start_seconds'] for c in clips), 1)
    problems = [f'missing {k}' for k in ('storyline_title', 'storyline_summary', 'hook_text')
                if not story.get(k)]
    story.setdefault('storyline_title', 'untitled')
    story.setdefault('storyline_summary', '')
    return story, problems


def _words_of(text):
    return re.findall(r"[a-z0-9']+", (text or '').lower())


def _quotes(claimed, actual):
    """True if most of the LLM's quoted words appear in the actual line(s)."""
    cw, aw = _words_of(claimed), set(_words_of(actual))
    if len(cw) < 3:
        return True
    return sum(w in aw for w in cw) / len(cw) >= 0.6


def hook_problems(story, sents, words):
    """Reasons the opening will not hold a scrolling viewer (empty list = fine)."""
    problems = []
    a = story['_sentences'][0][0]
    opening = sents[a]['text']
    first = _words_of(opening)
    if first and first[0] in FILLER_OPENERS:
        problems.append(f'opens on filler "{first[0]}"')
    elif ' '.join(first[:2]) in FILLER_PHRASES:
        problems.append(f'opens on filler "{" ".join(first[:2])}"')
    if _SETUP_OPENER.search(opening):
        problems.append('opens on setup, not a hook')
    desc = story['clips'][0].get('description', '')
    if _SETUP_DESC.search(desc):
        problems.append(f'first clip is setup ("{desc[:50]}")')
    claimed = story.get('_llm_opening')
    nearby = ' '.join(s['text'] for s in sents[a:a + 2])
    if claimed and not _quotes(claimed, nearby):
        problems.append(f'opening_line does not match S{a} (sentence numbers are off)')
    if words and sents[a]['w0'] is not None:
        lead = words[sents[a]['w0']]['start'] - story['clips'][0]['start_seconds']
        if lead > 0.4:
            problems.append(f'speech starts {lead:.1f}s in')
    return problems


def overlap_problems(clips, used, max_frac=0.25, max_sec=3.0):
    """Clips that reuse more than max_sec or max_frac of an already-used segment."""
    problems = []
    for c in clips:
        cs, ce = c['start_seconds'], c['end_seconds']
        for s, e in used:
            ov = min(ce, e) - max(cs, s)
            if ov <= 0:
                continue
            shorter = min(ce - cs, e - s)
            if ov > max_sec or (shorter > 0 and ov / shorter > max_frac):
                problems.append(f'reuses {ov:.1f}s of used segment {s:.0f}-{e:.0f}s')
    return problems


def length_fit(dur, target=TARGET_SECONDS):
    """10 inside the target length, minus a point per second outside it."""
    lo, hi = target
    if lo <= dur <= hi:
        return 10.0
    return max(0.0, 10.0 - (lo - dur if dur < lo else dur - hi))


def _num(v, default):
    try:
        return min(10.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return default


def rank_score(story, dur):
    """Weighted 0-10 score: the opening matters most, then the payoff."""
    base    = _num(story.get('score'), 5.0)
    hook    = _num(story.get('hook_strength'), base)
    payoff  = _num(story.get('payoff_strength'), base)
    clarity = _num(story.get('standalone_clarity'), base)
    return round(0.4 * hook + 0.3 * payoff + 0.2 * clarity + 0.1 * length_fit(dur), 2)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_PROMPT = """You are a viral short-form video editor working with Friends (US sitcom, 1994-2004).

Your task: find the BEST short-form moments in this episode for a vertical reel, and choose the FORMAT that fits each one best.

FORMAT "standalone_scene" — use when ONE scene already works as a complete moment on its own:
- A single continuous scene: an iconic monologue, a quotable line, a tight back-and-forth with a clean punchline, or one big reaction beat
- 1-3 clips from that ONE scene, back-to-back (small dead-air/cutaway trims between them are fine)
- Must be fully funny/emotional WITHOUT any other clips for context
- The first line must already be the hook: drop in mid-scene, right where the tension starts

FORMAT "story_recap" — use when the best moment NEEDS setup from earlier in the episode to land:
1. HOOK (first clip, 3-5 sec) — a TEASE of the payoff situation (the question, the tension), not the answer itself. CRITICAL: it opens MID-ACTION on a line that grabs on its own. NEVER open on someone walking in, sitting down, looking around, or any establishing shot.
2. CONTEXT clips (1-2 clips, 3-6 sec each) — the minimum lines needed to understand why the payoff is funny or emotionally powerful. Only the single KEY line per beat.
3. PAYOFF (last clip, 3-5 sec) — the final punchline, resolution, emotional reveal, or biggest laugh. It must hit at least as hard as the hook.

Default to "standalone_scene" when a single scene is strong enough on its own — it feels like a real clip rather than an edit and performs better. Use "story_recap" only when no single scene can stand alone and you genuinely need to connect a setup to a later payoff.

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

Transcript, one numbered sentence per line (format: S<number> [start_sec-end_sec] text):
{transcript}

Previously used segments (AVOID these timestamps): {used_segments}

Rules:
- If format = "standalone_scene": 1-3 clips from ONE scene, label each "scene", clips in chronological order, may skip small dead-air/cutaway gaps but must otherwise read as one continuous moment
- If format = "story_recap": 3-4 clips, FIRST clip labeled "hook" (any timestamp, first frame mid-action), LAST clip labeled "payoff" (punchline/resolution), middle clips labeled "context" (just the KEY line), clips other than the hook in rough CHRONOLOGICAL ORDER, 30 seconds max in total
- Clips must NOT overlap each other or the previously-used segments
- Pick moments with a clear spoken punchline or strong visual reaction
- storyline_title: snake_case, max 40 chars
- series_tag: pick exactly one — ross_being_ross | joey_being_joey | chandler_being_chandler | phoebe_being_phoebe | monica_being_monica | rachel_being_rachel | relationship_drama | gang_together
- hook_text: 3-8 words, ALL CAPS, an open question or tension about the moment that does NOT give away the punchline (e.g. "SHE DID WHAT TO HER CLIENT?", "ROSS SAYS THE WRONG NAME", "HE WAS NOT SUPPOSED TO HEAR THIS"). This is burned onto the video as a persistent white banner across the top of the ENTIRE video, so a spoiler is visible from the first frame. Still separate from and shorter than the YouTube title.
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
  "title": "Lisa Kudrow's Phoebe crosses a line with a client 😳",
  "description": "Phoebe's massage session goes somewhere nobody signed up for.\\n\\nFriends | The One with the Ballroom Dancing\\n\\n📌 Subscribe for more Friends moments.\\n\\n#Friends #LisaKudrow #PhoebeBuffay",
  "tags": "Friends, Lisa Kudrow, Phoebe Buffay, Friends TV show, Friends Phoebe, The One with the Ballroom Dancing",
  "pinned_comment": "Phoebe really said bite first, apologize never 😭 Team Phoebe or Team Client? 👇"
}}

Rules:
- title: a curiosity-driven tease that hints at the clip's most impactful moment WITHOUT revealing what happens. Lead with the actor's real name (Matthew Perry, Jennifer Aniston, David Schwimmer, Courteney Cox, Matt LeBlanc, Lisa Kudrow), optionally with the character ("Matthew Perry's Chandler..."), or with "Friends:". NO episode code. Tease the feeling, not the resolution. One or two emoji at the very end only if they fit.
- description: 2-line punchy hook in American English → blank line → show + episode name line (no season/episode code) → blank line → subscribe CTA → blank line → 3-5 hashtags on one line, #Friends first
- tags: 5-10 specific tags: show, actors, characters, episode name
- For relationship_drama series_tag, use a ship hashtag: #RossAndRachel or #ChandlerAndMonica depending on who's involved
- pinned_comment: must be ONE of these formats — (a) polarizing take specific to this clip: "Team X or Team Y? 👇", (b) ranking prompt: "Rate this [character] moment 1-10 👇", (c) completion gap: "What did [character] say next? 👇". NEVER generic. Must reference the specific characters or situation from THIS clip."""


_LIB_PROMPT = """You are a short-form video editor clipping Netflix's dating reality show "Love Is Blind" (Season 11) for an official Netflix-approved clipping campaign. The goal: get people talking about the show and send them to Netflix.

Find the best standalone moments in this episode for a vertical YouTube Short.

WHAT WORKS (ranked):
1. Drama or a plot twist — a confession, a secret coming out, a fight in the pods, an unexpected answer, a cliffhanger line
2. Real, raw conversation between cast members — a vulnerable admission, a hard question, a moment of chemistry
3. Sweet or funny moments — a proposal, a first reaction, a laugh that lands, an awkward beat played for comedy

HARD RULES (a clip that breaks any of these is rejected by the brand):
- ONE continuous scene. 1-3 clips from that same scene, back-to-back, in chronological order.
- It must feel complete on its own: a hook and a payoff, not a fragment.
- The moment must have a clear story — no random footage, no "nothing happens" clips.
- Never pick content that mocks or humiliates a cast member, anything sexual/explicit, violent, political, or racist.
- Never pick a moment where someone rates or insults people's looks ("ugly", "fat", "hot girl with an ugly guy"…) or talks about real people outside the show (celebrities, comedians, exes by name).
- Clips must NOT overlap each other or the previously used segments.

Previously used moments from this episode (AVOID these themes): {used_storylines}

Previously used segments (AVOID these timestamps): {used_segments}

Transcript, one numbered sentence per line (format: S<number> [start_sec-end_sec] text):
{transcript}

Field rules:
- storyline_title: snake_case, max 40 chars
- series_tag: always "love_is_blind"
- hook_text: 3-8 words, ALL CAPS, teases the moment WITHOUT spoiling the payoff. Neutral or warm — NEVER insulting a cast member (no "crazy", "psycho", "delusional", "toxic", etc.) and nothing sexual. Do not name any other show or streaming service. It is burned onto the video as a banner for the whole clip.
- hook_emoji: 1-2 emoji that fit the moment (😳 😭 👀 🥹 😬 💍 ❤️ …) or "" if none fit"""


_LIB_YT_PROMPT = """You write YouTube Shorts metadata for an official clip of Netflix's "Love Is Blind" (Season 11).

Episode  : {episode_str}
Moment   : {storyline}
Summary  : {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "Love Is Blind: she did not see that question coming 😳",
  "description": "One question in the pods and the whole date changed.\\nWould you have stayed after hearing that?\\n\\nLove Is Blind Season 11 is streaming now on Netflix.\\n\\n#LoveIsBlind #LoveIsBlindNetflix #Netflix",
  "tags": "Love Is Blind, Love Is Blind Netflix, Love Is Blind Season 11, Netflix, the pods",
  "pinned_comment": "Would you have said yes after that? 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens in this clip. Do not invent events, names or outcomes.
- title: starts with "Love Is Blind" or a cast member's first name, curiosity-driven, does not spoil the payoff. Warm or neutral tone.
- description: 2-line hook → blank line → a line that contains both "Love Is Blind" and "Netflix" → blank line → hashtags line that includes #loveisblind (3-5 hashtags)
- tags: 5-10 specific tags (show, season, cast first names)
- NEVER mock, insult or judge a cast member (no "crazy", "psycho", "delusional", "toxic", "red flag" about a person, etc.)
- NEVER mention any other TV show, streaming service or platform — only Netflix.
- Nothing sexual, political or racist.
- pinned_comment: a question that invites opinions on the situation (not on anyone's looks or character)."""


_SOLVED_PROMPT = """You are a short-form editor clipping "Solved with Mark Manson" (self-improvement podcast) for an official, brand-approved clipping campaign.

Find the best standalone moments in this episode for a vertical YouTube Short.

WHAT WORKS — pick ONE of these shapes:
1. Blunt reframe — Mark states a counter-intuitive idea with no setup needed ("Most people think X. It's actually Y.")
2. Problem → answer — a common struggle named, then Mark's clear answer
3. Short story with a turn — a story that pivots and lands on a point
Best topics for YouTube: values, purpose, big life questions, personal growth, decision-making, failure, anxiety, meaning.

HARD RULES (the brand rejects clips that break these):
- ONE continuous passage. 1-2 clips, back-to-back, chronological.
- It must be a COMPLETE thought: opens on the claim or question, ends after the point lands. Never cut mid-thought.
- The clip must OPEN with Mark Manson himself speaking (he must be on screen in the first 5 seconds). If a co-host or guest speaks first in the passage, start later.
- NEVER include ad reads, sponsor mentions, promo codes, "this episode is brought to you by", or any other brand.
- Never change the meaning of what was said — don't start or stop the clip so a quote means something different.
- Nothing NSFW, violent, or political.
- Clips must NOT overlap each other or the previously used segments (these also include known ad-read segments).

Previously used moments from this episode (AVOID these ideas): {used_storylines}

Previously used / forbidden segments (AVOID these timestamps): {used_segments}

Transcript, one numbered sentence per line (format: S<number> [start_sec-end_sec] text):
{transcript}

Field rules:
- storyline_title: snake_case, max 40 chars
- series_tag: always "solved"
- hook_text: 3-9 words, ALL CAPS, states the question the clip answers (not the answer) — accurate to what Mark actually says, not clickbait. Burned onto the video as a banner for the whole clip.
- hook_emoji: usually "" for this show; at most one subtle emoji if it really fits"""


_SOLVED_YT_PROMPT = """You write YouTube Shorts metadata for an official clip of the podcast "Solved with Mark Manson".

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "Mark Manson on the real reason failure hurts",
  "description": "Most people treat failure as a verdict on who they are. Mark Manson sees it differently.\\n\\nFrom Solved with Mark Manson @solvedpodcast @iammarkmanson\\n\\n#MarkManson #SolvedPodcast #selfimprovement",
  "tags": "Mark Manson, Solved with Mark Manson, Solved podcast, failure, self improvement",
  "pinned_comment": "Do you agree with Mark on this? 👇"
}}

Rules (brand requirements — breaking them gets the clip rejected):
- Lead with "Mark Manson", then the IDEA in the clip as a question or tease — never state his conclusion. Describe only what Mark actually says — never misrepresent him, no clickbait.
- title: punchy, at most one emoji.
- description: 1-2 lines on the idea → blank line → a line containing "@solvedpodcast" and "@iammarkmanson" → blank line → 3-5 hashtags.
- tags: 5-10 specific tags.
- Never mention sponsors, promo codes or other brands. Nothing political or NSFW.
- pinned_comment: a genuine question about the idea that invites discussion."""


_HS_SCENE_RULES = """HARD RULES (the brand rejects clips that break these):
- ONE continuous scene. 1-3 clips from that same scene, back-to-back, in chronological order.
- Hook in the first 2 seconds: start at (or right before) the wildest line or reaction, cut the setup. It is fine to end before the full resolution so people go watch the episode.
- Real moments only: never pick a cut that changes what someone meant.
- NEVER pick anything that mentions FaZe, anything political, sexual/explicit, or that makes HardScope, the host or a guest look bad (no humiliation, no "scam"/"rigged" angles).
- Clips must NOT overlap each other or the previously used segments.

Previously used moments from this episode (AVOID these themes): {used_storylines}

Previously used segments (AVOID these timestamps): {used_segments}

Transcript, one numbered sentence per line (format: S<number> [start_sec-end_sec] text):
{transcript}
"""

_HS_FIELD_RULES = """
Field rules:
- storyline_title: snake_case, max 40 chars
- series_tag: always "{tag}"
- hook_text: 3-8 words, ALL CAPS, teases the moment without spoiling the payoff, accurate to what happens — no lying clickbait. Never mention FaZe or any other channel. Burned onto the video as a banner for the whole clip.
- hook_emoji: 1-2 emoji that fit the moment or "" if none fit"""

_LJ_PROMPT = """You are a short-form video editor clipping "Love & Justice", Zach Justice's relationship show on the HardScope YouTube channel, for an official brand-approved clipping campaign. Real couples bring one relationship issue to Zach, he hears both sides and delivers a verdict on who is at fault.

Find the best standalone moments in this episode for a vertical YouTube Short.

WHAT WORKS (ranked):
1. The verdict or a big reveal — Zach calling who is at fault, a partner admitting something, a twist in the story
2. A heated or hilarious exchange between the couple, or Zach's sharpest reaction to what he hears
3. A relatable relationship question with a clear answer

""" + _HS_SCENE_RULES + _HS_FIELD_RULES.replace('{tag}', 'love_and_justice')

_LJ_YT_PROMPT = """You write YouTube Shorts metadata for an official clip of "Love & Justice", Zach Justice's relationship show on HardScope.

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "Zach Justice wasn't ready for her side of the story ⚖️",
  "description": "She told her side and the room went quiet.\\nWho do you think was wrong?\\n\\nFull episode of Love & Justice on @HardScopeTV\\n\\n#LOVEANDJUSTICE #ZachJustice #HardScope",
  "tags": "Love and Justice, Zach Justice, HardScope, Love and Justice verdict, Zach Justice relationship show",
  "pinned_comment": "Who was actually wrong here? 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens in this clip. Do not invent events, names or outcomes. No misleading or damaging titles.
- title: starts with "Zach Justice" or "Love & Justice", conversational, curiosity-driven, like a fan posting.
- description: 1-2 short lines on what happens → blank line → a line containing "@HardScopeTV" → blank line → hashtags line that includes #LOVEANDJUSTICE (3-5 hashtags)
- tags: 5-10 specific tags
- NEVER mention FaZe, other channels, or anything political or sexual. Never insult the couple.
- pinned_comment: a question that invites opinions on who was right."""

_UNPACKED_PROMPT = """You are a short-form video editor clipping "Unpacked", MatCrackz's collectibles show on the HardScope YouTube channel, for an official brand-approved clipping campaign. Celebrity guests hunt down their most-wanted collectibles and memorabilia — rare trading cards, big pulls, huge prices.

Find the best standalone moments in this episode for a vertical YouTube Short.

WHAT WORKS (ranked):
1. A big pull or reveal — the moment a rare card or item appears and everyone reacts
2. A jaw-dropping price or a high-stakes decision ("that's $60,000")
3. A funny or hype exchange between MatCrackz and the celebrity guest

""" + _HS_SCENE_RULES + _HS_FIELD_RULES.replace('{tag}', 'unpacked')

_UNPACKED_YT_PROMPT = """You write YouTube Shorts metadata for an official clip of "Unpacked", MatCrackz's collectibles show on HardScope.

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "Steve Aoki did NOT expect this pack 🤯",
  "description": "One pack and the whole room lost it.\\nWould you have kept it or sold it?\\n\\nWatch the full episode of Unpacked on @HardScopeTV\\n\\n#unpacked #MatCrackz #tradingcards",
  "tags": "Unpacked, HardScope, MatCrackz, Steve Aoki, trading cards, pack opening",
  "pinned_comment": "Keep it or sell it? 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens in this clip. Do not invent names, prices or outcomes. No misleading or damaging titles.
- title: starts with the guest's real name, "MatCrackz" or "Unpacked", conversational, like a fan posting.
- description: 1-2 short lines on what happens → blank line → a line containing "@HardScopeTV" → blank line → hashtags line that includes #unpacked (3-5 hashtags)
- tags: 5-10 specific tags
- NEVER mention FaZe, other channels, or anything political or sexual. Nothing that frames the show as fake or a scam.
- pinned_comment: a short question that invites opinions on the moment."""


_YLA_PROMPT = """You are a short-form video editor making organic, funny clips from YoungLA WORLD videos (gym creators and bodybuilders doing challenges, trips and pranks) for an official clipping campaign. The clip must feel like a normal fan clip, NOT an ad.

Find the best standalone moments in this episode for a vertical YouTube Short.

WHAT WORKS (ranked):
1. A genuinely funny or chaotic moment — a reaction, a roast, a fail, a ridiculous challenge moment
2. A hype moment — a big lift, a win, a crazy stat, a celebrity guest moment
3. A great one-liner exchange between the creators

HARD RULES (the brand rejects clips that break these):
- ONE continuous scene. 1-3 clips from that same scene, back-to-back, in chronological order.
- Hook in the first 2 seconds: never open on someone explaining the challenge.
- Pick moments where the people are clearly on camera facing it, in good light (their clothing must be visible) — not dark rooms, night-vision, black screens, title cards or b-roll.
- NEVER pick product promos, merch/discount/"link in bio" talk, sponsor reads, anything NSFW, violent, drug/steroid talk, or heavy swearing.
- Clips must NOT overlap each other or the previously used segments.

Previously used moments from this episode (AVOID these themes): {used_storylines}

Previously used segments (AVOID these timestamps): {used_segments}

Transcript, one numbered sentence per line (format: S<number> [start_sec-end_sec] text):
{transcript}

Field rules:
- storyline_title: snake_case, max 40 chars
- series_tag: always "gym"
- hook_text: 3-8 words, ALL CAPS, funny/curious, accurate to the moment. NEVER mention any brand, store or clothing. Burned onto the video as a banner for the whole clip.
- hook_emoji: 1-2 emoji that fit (😭 💀 😂 🤯 💪) or \"\""""

_YLA_YT_PROMPT = """You write YouTube Shorts metadata for a funny gym/bodybuilder clip. It must read like a fan posting, never like an ad.

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "<real name of who is in it> <short funny tease about THIS moment> 😭",
  "description": "<one line on what actually happens>\\n\\n#IShowSpeed #gym #bodybuilding",
  "tags": "IShowSpeed, bodybuilders, gym humor, bodybuilding",
  "pinned_comment": "Who's the funniest one here? 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens. Funny, short, conversational.
- NEVER mention any brand, clothing, store, merch, discount, link or sponsor. NEVER tag or @ any account.
- title: starts with the real name of the creator or guest in the clip. description: 1 short line → blank line → 3-5 hashtags (names, gym, bodybuilding).
- tags: 5-10 specific tags, no brand names.
- Nothing NSFW, violent, drug/steroid related.
- pinned_comment: a short fun question about the moment."""


_PROMPTS = {
    'youngla':        (_YLA_PROMPT, _YLA_YT_PROMPT),
    'loveisblind':    (_LIB_PROMPT, _LIB_YT_PROMPT),
    'markmanson':     (_SOLVED_PROMPT, _SOLVED_YT_PROMPT),
    'loveandjustice': (_LJ_PROMPT, _LJ_YT_PROMPT),
    'unpacked':       (_UNPACKED_PROMPT, _UNPACKED_YT_PROMPT),
}

# Shape example per campaign. Sentence numbers are 0 placeholders: the model
# once copied an example's real-looking timestamps (commit 28b18e2).
_EXAMPLES = {
    'friends': ('standalone_scene', 'phoebe_bites_massage_client',
                'Phoebe bites her client mid-massage and tries to play it off', 'phoebe_being_phoebe',
                'SHE DID WHAT TO HER CLIENT?', '😳', 'scene',
                'Phoebe snaps at the client and the room freezes'),
    'loveisblind': ('standalone_scene', 'pods_secret_confession',
                    'She admits something in the pods that changes how he sees her', 'love_is_blind',
                    "SHE DIDN'T EXPECT THAT QUESTION", '😳', 'scene', 'His question and her stunned answer'),
    'markmanson': ('standalone_scene', 'failure_is_the_tuition',
                   'Mark argues failure feels worse than it is because we treat it as identity', 'solved',
                   'WHY FAILURE HURTS MORE THAN IT SHOULD', '', 'scene',
                   'Mark opens on the bold claim and lands the reframe'),
    'loveandjustice': ('standalone_scene', 'zach_rules_who_is_wrong',
                       'After hearing both sides, Zach tells the couple who is at fault', 'love_and_justice',
                       'ZACH DID NOT HOLD BACK', '⚖️', 'scene', "Zach's verdict and the couple's reaction"),
    'unpacked': ('standalone_scene', 'sixty_thousand_dollar_pull',
                 'The guest opens the pack and pulls the card everyone was hoping for', 'unpacked',
                 'WAIT FOR THE LAST CARD', '🤯', 'scene', 'The pull and the reaction'),
    'youngla': ('standalone_scene', 'bodybuilder_panics_in_escape_room',
                'A huge bodybuilder completely loses it when the lights go out', 'gym',
                'HE WAS NOT READY FOR THIS', '😭', 'scene', 'The lights go out and he panics'),
}


def _example_json(campaign):
    fmt, title, summary, tag, hook, emoji, label, desc = _EXAMPLES.get(campaign, _EXAMPLES['friends'])
    cand = {
        'format': fmt, 'storyline_title': title, 'storyline_summary': summary, 'series_tag': tag,
        'hook_text': hook, 'hook_emoji': emoji,
        'opening_line': '<first sentence of the first clip, verbatim>',
        'payoff_line': '<last sentence of the last clip, verbatim>',
        'clips': [{'start_sentence': 0, 'end_sentence': 0, 'label': label, 'description': desc}],
        'hook_strength': 8, 'payoff_strength': 8, 'standalone_clarity': 9,
    }
    return json.dumps({'candidates': [cand, '...2-4 more candidates in the same shape...']},
                      ensure_ascii=False, indent=2)


def _answer_rules(campaign, limits):
    lo, hi = limits
    t_lo, t_hi = TARGET_SECONDS
    return f"""

HOW TO PICK AND CUT (these rules override anything above):
- Give {N_CANDIDATES[0]}-{N_CANDIDATES[1]} DIFFERENT candidate moments (different scenes or ideas, not variations of one), best first.
- Cut by sentence number, never by seconds. Each clip runs from "start_sentence" to "end_sentence" (S-numbers from the transcript, inclusive); code turns them into exact cut times.
- start_sentence must begin a thought, never a line that continues the one before it. end_sentence must be a COMPLETE sentence that finishes the beat (the punchline, answer, verdict or reaction line), never a line that trails off or carries on into the next line.
- Length: aim for {t_lo}-{t_hi} seconds in total (use the timestamps). Never under {lo:.0f}s or over {hi:.0f}s. Shorter wins when the moment is complete: end right on the payoff.
- The OPENING LINE is the hook. The first sentence of the first clip must stop a scrolling stranger on its own, with no context: an odd or bold claim, a direct question, an accusation or confession, or a line dropped into the middle of a tense moment. Speech starts in the first second.
- Never open on filler ("so", "um", "uh", "okay", "well", "yeah", "and", "but", "like", "anyway", "alright"), a greeting, someone explaining or setting up what is about to happen, or a line that only makes sense after an earlier one. Cut the setup: the hook_text banner supplies the context.
- opening_line: the first sentence of the first clip, copied VERBATIM. payoff_line: the last sentence of the last clip, copied VERBATIM.
- hook_text must tease, not tell: no words from the payoff_line, never the punchline itself.
- Score each candidate 1-10, honestly and harshly (an ordinary moment is a 5): hook_strength = would a stranger stop scrolling on the opening line alone; payoff_strength = does the ending land (laugh, reveal, verdict, reaction); standalone_clarity = does it make sense with zero context.

Return ONLY a valid JSON object, no markdown, no explanation. The example shows the SHAPE only: its sentence numbers, titles and lines are placeholders; use real S-numbers and wording from THIS transcript:
{_example_json(campaign)}"""


def _title_rules(campaign, story, title_names, recent_titles):
    names = ', '.join(title_names or []) or 'the show or the person in the clip'
    actor = (' For a scripted show name the ACTOR (e.g. "Matthew Perry\'s Chandler..."), not only the character.'
             if campaign == 'friends' else '')
    recent = '\n'.join(f'  - {t}' for t in (recent_titles or [])[-30:]) or '  (none yet)'
    return f"""

TITLE AND HASHTAG RULES (these override the example and rules above):
- title: start with the real name US viewers search for, one of: {names}.{actor} Then the tease.
- title: under 60 characters, 1-2 emoji max and only at the end, no hashtags.
- title: never start with "Wait", "Nobody", "No one", "POV", "When you" or "@", and never "did X really just say that".
- title and description must NOT give away the payoff. The clip opens on "{story.get('opening_line', '')}" and ends on "{story.get('payoff_line', '')}": tease the situation, never state the outcome or reuse the payoff's key words.
- title: different wording AND structure from every one of these recent titles:
{recent}
- description: ends with ONE line of 3-5 specific hashtags: the show or person first, then names or the exact topic. Never #viral #trending #foryou #fyp #ytshorts #shorts #shortsfeed #funnymoments #comedy #relationshipgoals or any other broad tag.
- tags: 5-10 specific tags (show, real names, characters, episode or topic), no generic words like "funny moments" or "comedy"."""


def _parse_json(raw):
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
    return json.loads(raw)


def generate_yt_metadata(story, episode_name, api_key, retries=3, campaign='friends',
                         title_names=None, recent_titles=None):
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
    ) + _title_rules(campaign, story, title_names, recent_titles)
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.7},
    }
    for attempt in range(1, retries + 1):
        try:
            return _parse_json(_gemini_post(payload, api_key, timeout=60))
        except Exception as e:
            print(f"  YT metadata attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(10)
    return None


def analyze_candidates(sents, api_key, used_segments=None, used_storylines=None, retries=5,
                       campaign='friends', limits=(12, 40)):
    """Ask Gemini for 3-5 candidate moments over the numbered sentences; returns the raw candidate dicts."""
    api_key = api_key.strip()
    used_str = (
        ', '.join(f"{s:.0f}s-{e:.0f}s" for s, e in used_segments)
        if used_segments else 'none'
    )
    storylines_str = (
        '; '.join(used_storylines) if used_storylines else 'none'
    )
    template = _PROMPTS.get(campaign, (_PROMPT, _YT_PROMPT))[0]
    prompt = template.format(
        transcript=format_sentences(sents),
        used_segments=used_str,
        used_storylines=storylines_str,
    ) + _answer_rules(campaign, limits)

    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.3},
    }

    for attempt in range(1, retries + 1):
        try:
            data = _parse_json(_gemini_post(payload, api_key, timeout=180))
            if isinstance(data, dict):
                data = data.get('candidates', [data] if 'clips' in data else [])
            cands = [c for c in data if isinstance(c, dict) and c.get('clips')]
            if not cands:
                raise ValueError('no candidates in the response')
            return cands
        except Exception as e:
            print(f"  Gemini attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(15)

    raise RuntimeError("Gemini analysis failed after all retries")


def analyze_transcript(segments, api_key, used_segments=None, used_storylines=None, retries=5,
                       campaign='friends', all_words=None):
    """Best-ranked candidate as a story with exact cut times (kept for local_test/run.py)."""
    sents = build_sentences(all_words, segments)
    best = None
    for raw in analyze_candidates(sents, api_key, used_segments, used_storylines, retries, campaign):
        story, problems = build_candidate(raw, sents, all_words)
        if story and not problems:
            story['score'] = rank_score(story, story['_total_seconds'])
            if not best or story['score'] > best['score']:
                best = story
    if not best:
        raise RuntimeError("Gemini returned no usable candidate")
    return best
