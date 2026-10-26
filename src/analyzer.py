import json
import re
import time
import requests

_GEMINI_BASE    = 'https://generativelanguage.googleapis.com/v1/models/{}:generateContent'
# gemini-2.0-flash was retired (404 on v1): the last step retries flash-lite, the model
# runs actually succeed on after a 2.5-flash 429, once more after the back-off.
_FALLBACK_MODELS = ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-2.5-flash-lite']


def _gemini_post(payload, api_key, timeout=120):
    """POST to Gemini with automatic model fallback on 503/429 (and 404: a retired model)."""
    for i, model in enumerate(_FALLBACK_MODELS):
        try:
            resp = requests.post(
                _GEMINI_BASE.format(model),
                # header, not ?key=: an HTTPError message quotes the URL, which leaked the key into logs
                headers={'x-goog-api-key': api_key},
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            if i > 0:
                print(f"  Success with fallback model: {model}")
            return resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        except requests.HTTPError as e:
            if e.response.status_code in (503, 429, 404) and i < len(_FALLBACK_MODELS) - 1:
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

TARGET_SECONDS = (15, 30)   # default sweet spot; profiles pick a per-niche target (campaigns.NICHE_LENGTH)
N_CANDIDATES   = (3, 5)

# 0-3 s hook classes (virality spec section 2). The LLM labels its own opening;
# SL (slow, low-stakes setup) opened 13 of the 16 mature corpus flops.
HOOK_CLASSES = {
    'AV': 'absurd or striking visual in frame 1',
    'BC': 'bold claim or stake stated in the first 3 seconds',
    'MA': 'drops in mid-action or mid-conflict',
    'Q':  'a question the viewer wants answered',
    'CN': 'two famous names colliding unexpectedly',
    'ST': 'a first-person story opener from a known person (podcast/talk)',
    'SL': 'slow, low-stakes setup: introductions, greetings, exposition, an opinion premise',
}
HOOK_POINTS     = {'AV': 2, 'BC': 2, 'MA': 2, 'Q': 2, 'CN': 2, 'ST': 1, 'SL': 0}
RUBRIC_LLM_KEYS = ('stake', 'payoff', 'emotion', 'framing', 'originality')
RUBRIC_GATE     = 11       # of 16: corpus virals scored 11-15, mature flops 4-9
PAYOFF_MAX_TAIL = 3.0      # end within 3 s of the payoff so the clip loops on it
LATER_START_MAX = 10.0     # how far a setup opener may be skipped to reach a question or conflict line
# Openers that drop the viewer into a conflict ("No!", "Don't wink at me", "Hey, Bozo").
_CONFLICT_LINE = re.compile(
    r"^\W*(no\b|stop\b|don'?t\b|hey\b|what the\b|get out\b|how could\b|"
    r"you (lied|did|said|never|ate|took|kissed|slept)\b)", re.IGNORECASE)
# Which originality layer each genre gets (virality spec section 8).
GENRES = {'friends': 'sitcom', 'loveisblind': 'reality', 'loveandjustice': 'reality',
          'markmanson': 'podcast', 'unpacked': 'talk', 'youngla': 'talk',
          'tonyhawk': 'sitcom', 'bilf': 'podcast', 'ladbible': 'talk', 'milkxp': 'reality'}

# First words that make a scrolling viewer leave: filler, connectives, setup.
FILLER_OPENERS = {'so', 'um', 'umm', 'uh', 'uhh', 'er', 'erm', 'hmm', 'mm', 'mhm', 'okay', 'ok',
                  'well', 'yeah', 'and', 'but', 'like', 'anyway', 'anyways', 'alright', 'right',
                  'because', 'cause', 'also'}
FILLER_PHRASES = {'you know', 'i mean', 'kind of', 'sort of'}
# Podcast / talk: leading filler is cut off the first sentence instead of
# rejecting the moment ("So they laughed me out of the room" -> "They laughed...").
TRIM_FILLERS = {'so', 'and', 'like', 'um', 'umm', 'uh', 'uhh', 'er', 'erm', 'hmm', 'mm', 'mhm',
                'okay', 'ok', 'well', 'yeah', 'anyway', 'anyways', 'alright'}
TRIM_FILLER_PHRASES = {('you', 'know'), ('i', 'mean')}
TRIM_MAX_WORDS = 4         # at most this many leading words are cut
TRIM_MIN_LEFT = 3          # ...and at least this many words of the sentence stay
# "so" as an intensifier is content ("So much money"), not filler.
_SO_CONTENT = {'much', 'many', 'far', 'long', 'bad', 'good', 'hard', 'big', 'true', 'proud', 'happy',
               'sad', 'weird', 'funny', 'crazy', 'scared', 'excited', 'grateful', 'lucky', 'close'}
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


def build_candidate(raw, sents, words, max_tail=PAYOFF_MAX_TAIL):
    """
    Turn one LLM candidate into a story with exact cut times. Returns
    (story, problems); story is None when its sentence numbers are unusable.
    max_tail: seconds the last clip may run past the payoff sentence before it is
    cut back to end on it (the edit layer uses 1.5 s: the payoff is the last sentence).
    """
    clips, ranges, asked = [], [], []
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
        asked.append(a)
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

    problems = []
    # End on the payoff: trailing lines after the punchline only dilute the loop.
    payoff_k = ranges[-1][1]
    try:
        k = _sid(raw['payoff_sentence']) if raw.get('payoff_sentence') not in (None, '') else None
    except (AttributeError, TypeError, ValueError):
        k = None
    if k is not None and any(a <= k <= b for a, b in ranges):
        payoff_k = k
        last_a, last_b = ranges[-1]
        if (last_a <= k < last_b and sents[k]['complete']
                and sents[last_b]['end'] - sents[k]['end'] > max_tail):
            ranges[-1] = (last_a, k)
            clips[-1]['end_seconds'] = clip_times(sents, words, last_a, k)[1]

    story = dict(raw)
    story['clips'] = clips
    story['_sentences'] = ranges
    story['_llm_opening'] = str(raw.get('opening_line') or '')
    story['_llm_start'] = asked[0]   # first sentence the LLM picked, before snapping
    # The real first/last lines (not the LLM's quote) drive the hook and spoiler checks.
    story['opening_line'] = sents[ranges[0][0]]['text']
    story['payoff_line'] = sents[ranges[-1][1]]['text']
    story['_payoff_text'] = sents[payoff_k]['text']
    story['_payoff_start'] = sents[payoff_k]['start']
    story['_total_seconds'] = round(sum(c['end_seconds'] - c['start_seconds'] for c in clips), 1)

    # Contract fields for the renderer (episode seconds).
    p_end = words[sents[payoff_k]['w1']]['end'] if sents[payoff_k]['w1'] is not None else sents[payoff_k]['end']
    story['payoff_t'] = round(float(p_end), 3)
    end_t = sents[ranges[-1][1]]['end']
    p_off, e_off = story_offset(clips, story['payoff_t']), story_offset(clips, end_t)
    story['_payoff_tail'] = round(e_off - p_off, 2) if p_off is not None and e_off is not None else 0.0
    story['_payoff_pos'] = round(p_off / story['_total_seconds'], 2) if p_off and story['_total_seconds'] else 1.0
    # A v2 answer carries hook_class; older-shape answers keep the Oct 22 checks only.
    story['_v2'] = 'hook_class' in raw
    story['hook_class'] = str(raw.get('hook_class') or '').strip().upper() or None
    story['header'] = re.sub(r'\s+', ' ', str(raw.get('header') or raw.get('hook_text') or '')).strip()
    if story['header']:
        story['hook_text'] = story['header']   # the current banner path reads hook_text
    story['reveal'] = str(raw.get('reveal') or '').strip()
    for key in ('context_line', 'closing_line'):
        story[key] = re.sub(r'\s+', ' ', str(raw.get(key) or '')).strip() or None
    story['annotations'] = _annotations(raw.get('annotations'), sents, clips)
    story['single_story_arc'] = raw.get('single_story_arc') in (True, 'true', 'True', 1)

    problems += [f'missing {k}' for k in ('storyline_title', 'storyline_summary', 'header')
                 if not story.get(k)]
    story.setdefault('storyline_title', 'untitled')
    story.setdefault('storyline_summary', '')
    return story, problems


def story_offset(clips, t):
    """Seconds into the (untrimmed) story at which episode time t plays, or None if t is cut out."""
    off = 0.0
    for c in clips:
        if c['start_seconds'] - 0.01 <= t <= c['end_seconds'] + 0.01:
            return off + max(0.0, t - c['start_seconds'])
        off += c['end_seconds'] - c['start_seconds']
    return None


def _annotations(raw, sents, clips, limit=3):
    """[{'t': episode seconds, 'text': '*HE KNOWS*'}] for the LLM's stage directions that land inside the clips."""
    out = []
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict) or not str(a.get('text') or '').strip():
            continue
        try:
            if a.get('sentence') not in (None, ''):
                k = _sid(a['sentence'])
                if not 0 <= k < len(sents):
                    continue
                t = sents[k]['start']
            else:
                t = float(a['t'])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if story_offset(clips, t) is None:
            continue
        text = str(a['text']).strip().strip('*').strip()
        out.append({'t': round(float(t), 3), 'text': f'*{text.upper()}*'})
    return sorted(out, key=lambda x: x['t'])[:limit]


def later_start(story, sents, words, max_shift=LATER_START_MAX):
    """
    The same story started on the first question or conflict line within
    max_shift seconds of a setup opener (test1: skip the bill set-up, open on
    "Joey, what are you doing?"). None if the first clip has no such line.
    """
    a, b = story['_sentences'][0]
    for k in range(a + 1, b + 1):
        s = sents[k]
        if s['start'] - sents[a]['start'] > max_shift:
            break
        if not s['clean_start']:
            continue
        text = s['text'].strip()
        bare = re.sub(r'["\')\]]+$', '', text)
        cls = 'Q' if bare.endswith('?') else 'MA' if (bare.endswith('!') or _CONFLICT_LINE.search(text)) else None
        if not cls:
            continue
        new = dict(story)
        new.pop('_open_w', None)
        new.pop('_filler_trim', None)
        new['clips'] = [dict(c) for c in story['clips']]
        new['clips'][0]['start_seconds'] = clip_times(sents, words, k, b)[0]
        new['_sentences'] = [(k, b)] + list(story['_sentences'][1:])
        new['opening_line'] = new['_llm_opening'] = text
        new['_llm_start'] = k
        new['hook_class'] = cls
        new['_later_start'] = (a, k)
        new['_total_seconds'] = round(sum(c['end_seconds'] - c['start_seconds'] for c in new['clips']), 1)
        if new.get('_payoff_tail') is not None:
            p_off = story_offset(new['clips'], new['payoff_t'])
            if p_off is not None and new['_total_seconds']:
                new['_payoff_pos'] = round(p_off / new['_total_seconds'], 2)
        new['annotations'] = [x for x in story.get('annotations') or []
                              if story_offset(new['clips'], x['t']) is not None]
        return new
    return None


def start_at(story, sents, words, k):
    """The same story with its first clip started on sentence k (inside that clip)."""
    a, b = story['_sentences'][0]
    new = dict(story)
    new.pop('_open_w', None)
    new.pop('_filler_trim', None)
    new['clips'] = [dict(c) for c in story['clips']]
    new['clips'][0]['start_seconds'] = clip_times(sents, words, k, b)[0]
    new['_sentences'] = [(k, b)] + list(story['_sentences'][1:])
    new['opening_line'] = new['_llm_opening'] = sents[k]['text'].strip()
    new['_llm_start'] = k
    new['_total_seconds'] = round(sum(c['end_seconds'] - c['start_seconds'] for c in new['clips']), 1)
    if new.get('payoff_t') is not None and new['_total_seconds']:
        p_off = story_offset(new['clips'], new['payoff_t'])
        if p_off is not None:
            new['_payoff_pos'] = round(p_off / new['_total_seconds'], 2)
    new['annotations'] = [x for x in story.get('annotations') or []
                          if story_offset(new['clips'], x['t']) is not None]
    return new


def length_trim(story, sents, words, fits):
    """
    Edit layer (P0-1): a moment that runs over the length cap starts later instead of being
    thrown away. Tries the first clip's later sentences that start a new thought, in order,
    and returns the first story for which fits(story) is True (fits checks the edited length
    and the opener rules), or None. The payoff end is never touched.
    """
    a, b = story['_sentences'][0]
    for k in range(a + 1, b + 1):
        if not sents[k].get('clean_start', True):
            continue
        new = start_at(story, sents, words, k)
        new['_length_trim'] = (a, k)
        if fits(new):
            return new
    return None


def _words_of(text):
    return re.findall(r"[a-z0-9']+", (text or '').lower())


def trim_filler_opener(story, sents, words):
    """
    The story with leading filler words ("so", "and", "like", "um", "you
    know"...) cut off its first sentence: the clip starts just before the
    next word (never inside the cut filler word). None when the opener has
    no leading filler, or too little of the sentence would be left.
    The caller re-checks the trimmed opener (hook_problems).
    """
    a = story['_sentences'][0][0]
    s = sents[a]
    if s['w0'] is None or story.get('_open_w') is not None:
        return None
    i, w1 = s['w0'], s['w1']
    tok = lambda k: (_words_of(words[k]['word']) or [''])[0]
    while i <= w1 and i - s['w0'] < TRIM_MAX_WORDS:
        t = tok(i)
        if i + 1 <= w1 and (t, tok(i + 1)) in TRIM_FILLER_PHRASES:
            i += 2
        elif t in TRIM_FILLERS and not (t == 'so' and i + 1 <= w1 and tok(i + 1) in _SO_CONTENT):
            i += 1
        elif t == 'then' and i > s['w0']:          # "and then she said" -> "she said"
            i += 1
        else:
            break
    if i == s['w0'] or w1 - i + 1 < TRIM_MIN_LEFT:
        return None
    new = dict(story)
    new['clips'] = [dict(c) for c in story['clips']]
    start = max(words[i]['start'] - _LEAD_PAD, words[i - 1]['end'] + 0.01)
    new['clips'][0]['start_seconds'] = round(max(0.0, start), 3)
    text = ' '.join(w['word'] for w in words[i:w1 + 1]).strip()
    new['opening_line'] = text[:1].upper() + text[1:]
    new['_open_w'] = i
    new['_filler_trim'] = ' '.join(words[k]['word'] for k in range(s['w0'], i)).strip()
    new['_total_seconds'] = round(sum(c['end_seconds'] - c['start_seconds'] for c in new['clips']), 1)
    if new.get('payoff_t') is not None and new['_total_seconds']:
        p_off = story_offset(new['clips'], new['payoff_t'])
        if p_off is not None:
            new['_payoff_pos'] = round(p_off / new['_total_seconds'], 2)
    new['annotations'] = [x for x in story.get('annotations') or []
                          if story_offset(new['clips'], x['t']) is not None]
    return new


def _quotes(claimed, actual):
    """True if most of the LLM's quoted words appear in the actual line(s)."""
    cw, aw = _words_of(claimed), set(_words_of(actual))
    if len(cw) < 3:
        return True
    return sum(w in aw for w in cw) / len(cw) >= 0.6


def hook_problems(story, sents, words, strict=False, genre=None):
    """Reasons the opening will not hold a scrolling viewer (empty list = fine).
    strict (edit layer hook_rejects, P0-3): also the deterministic opener rejects."""
    problems = []
    a = story['_sentences'][0][0]
    w0 = story.get('_open_w')          # first kept word after a filler trim
    opening = story['opening_line'] if w0 is not None else sents[a]['text']
    if w0 is None:
        w0 = sents[a]['w0']
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
    # Snapping can move the start back past the line the LLM quoted, so look through its own pick.
    nearby = ' '.join(s['text'] for s in sents[a:max(a, story.get('_llm_start', a)) + 2])
    if claimed and not _quotes(claimed, nearby):
        problems.append(f'opening_line does not match S{a} (sentence numbers are off)')
    if words and w0 is not None:
        lead = words[w0]['start'] - story['clips'][0]['start_seconds']
        if lead > 0.4:
            problems.append(f'speech starts {lead:.1f}s in')
    if strict:
        problems += opener_rejects(sents, a, opening, genre)
    return problems


# P0-3 deterministic opener rejects (edit layer 'hook_rejects'). Measured on our stalled Shorts:
# an interview outro ("thank you for that, LADbible"), a Whisper continuation that starts
# lower-case, and a jargon podcast opener ("Loss aversion never changes what you are loss
# averse to") all passed the LLM's own hook label.
_OUTRO_OPENER = re.compile(
    r"^\W*(thank(s| you)\b|thanks for (having|watching|coming)|that'?s (a wrap|all\b|it for)|"
    r"see you (next|soon|guys)|(good)?bye\b|subscribe\b|like and subscribe|"
    r"(i|we) (will|'ll) walk away with|it'?s been (a pleasure|amazing|great|fun) (having|being|to)|"
    r"what a (pleasure|treat) (to|it))", re.IGNORECASE)
_OUTRO_ANYWHERE = re.compile(r"\b(thank you (so much )?for (having|that|this|watching)|thanks for having)\b",
                             re.IGNORECASE)
_JARGON = re.compile(
    r"\b(aversion|averse|definition|defined|ego|paradigm|framework|cognitive|psycholog\w*|dopamine|"
    r"neuro\w*|concept\w*|fundamental(ly)?|essential(ly)|inherent(ly)?|mechanism|dichotomy|heuristic|"
    r"phenomen\w+|construct|existential|theoretical(ly)?|hypothes\w+|correlat\w+|metric|optimi[sz]\w*|"
    r"abstract(ly)?|ideolog\w*|philosoph\w*|asymmetr\w*|valence|cognition|bias(es)?)\b",
    re.IGNORECASE)
_PERSON = re.compile(
    r"\b(i|i'm|i've|i'd|me|my|you|your|you're|he|she|him|her|his|we|our|they|them|their|someone|"
    r"somebody|people|guy|guys|girl|man|woman|kid|kids|mom|dad|mother|father|wife|husband|boyfriend|"
    r"girlfriend|friend|boss|brother|sister|son|daughter)\b", re.IGNORECASE)
OPENER_MAX_WORDS = 14
# "We also do it in a lot of other areas", "That's why...", "Which is...": only make sense after
# the line before (seen on a Mark Manson pick, 2026-10-26).
_LEANS_BACK = re.compile(r"^\W*((?:\w+'?\w*\s+){0,2}(also|either|as well)\b|that'?s why\b|which\b|"
                         r"it'?s the same\b|same thing\b|(?:\w+'?\w*\s+){1,4}too\W*$)", re.IGNORECASE)


def opener_rejects(sents, a, opening=None, genre=None):
    """Reasons the first sentence (S{a}) cannot open a Short (P0-3), [] when it can."""
    s = sents[a]
    raw = (s.get('text') or '').strip()
    opening = (opening or raw).strip()
    out = []
    first = re.search(r'[A-Za-z]', raw)
    if first and first.group(0).islower():
        out.append(f'opens mid-sentence (lower-case "{raw[:30]}")')
    elif a > 0 and not s.get('clean_start', True) and s['start'] - sents[a - 1]['end'] < 0.3:
        out.append('opens mid-sentence (continues the line before)')
    if _OUTRO_OPENER.search(opening) or _OUTRO_ANYWHERE.search(opening):
        out.append('opens on an outro / thank-you line')
    if _LEANS_BACK.search(opening):
        out.append('opener leans on the line before ("also" / "too" / "that\'s why")')
    if genre in ('podcast', 'talk'):
        n = len(_words_of(opening))
        jargon = _JARGON.search(opening)
        if jargon:
            out.append(f'opens on an abstract / jargon line ("{jargon.group(0)}")')
        elif n > OPENER_MAX_WORDS and not _PERSON.search(opening):
            out.append(f'abstract opener: {n} words and no person in it')
    return out


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


def rank_score(story, dur, target=TARGET_SECONDS):
    """
    Weighted 0-10 score: the opening matters most, then the payoff. A story
    with a rubric total (story['_rubric'], of 16) blends it in with weight 0.5
    next to the 0.4/0.3/0.2/0.1 weights.
    """
    base    = _num(story.get('score'), 5.0)
    hook    = _num(story.get('hook_strength'), base)
    payoff  = _num(story.get('payoff_strength'), base)
    clarity = _num(story.get('standalone_clarity'), base)
    score = 0.4 * hook + 0.3 * payoff + 0.2 * clarity + 0.1 * length_fit(dur, target)
    if story.get('_rubric') is not None:
        score = (score + 0.5 * story['_rubric'] / 16 * 10) / 1.5
    return round(score, 2)


def _pt(v):
    try:
        return int(min(2, max(0, round(float(v)))))
    except (TypeError, ValueError):
        return 0


def rubric_points(story, dur, framing_cap=2, originality_cap=2, long_arc=False):
    """
    The 8-criterion rubric (virality spec section 4, 0-2 each, max 16). Code
    scores hook (from hook_class), length (edited seconds) and title (the
    title rules hard-ban vague templates, so a title that ships earns 2); the
    LLM scores stake, payoff, emotion, framing and originality, capped by what
    this campaign can actually put on screen (framing_cap / originality_cap).
    """
    llm = story.get('rubric') if isinstance(story.get('rubric'), dict) else {}
    if dur <= 25:
        length = 2 if dur >= 12 else 1
    elif dur <= 35:
        length = 1
    else:
        length = 1 if long_arc else 0
    pts = {
        'hook':        HOOK_POINTS.get(story.get('hook_class') or '', 0),
        'stake':       _pt(llm.get('stake')),
        'payoff':      _pt(llm.get('payoff')),
        'length':      length,
        'emotion':     _pt(llm.get('emotion')),
        'framing':     min(_pt(llm.get('framing')), framing_cap),
        'originality': min(_pt(llm.get('originality')), originality_cap),
        'title':       2,
    }
    return sum(pts.values()), pts


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
2. A plot twist — someone finds out what a friend has been hiding, a lie finally unravels in front of everyone
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
- header: 4-9 words, ALL CAPS, names a character and states the SITUATION or your STANCE on it, never the punchline (e.g. "JOEY CAN'T PAY HIS CENTRAL PERK TAB", "MONICA WAS 100% RIGHT HERE", "ROSS IS ABOUT TO SAY THE WRONG NAME"). It is on screen from the first frame for the ENTIRE video, so a spoiler is visible before the joke lands. Still separate from and shorter than the YouTube title.
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
1. Drama or a plot twist — a confession, a hidden past coming out, a fight in the pods, an answer nobody planned for, a cliffhanger line
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
- header: 4-9 words, ALL CAPS, names a cast member and states the situation, WITHOUT spoiling the payoff. Neutral or warm — NEVER insulting a cast member (no "crazy", "psycho", "delusional", "toxic", etc.) and nothing sexual. Do not name any other show or streaming service. It is burned onto the video for the whole clip.
- hook_emoji: 1-2 emoji that fit the moment (😳 😭 👀 🥹 😬 💍 ❤️ …) or "" if none fit"""


_LIB_YT_PROMPT = """You write YouTube Shorts metadata for an official clip of Netflix's "Love Is Blind" (Season 11).

Episode  : {episode_str}
Moment   : {storyline}
Summary  : {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "Love Is Blind: she did not see that question coming 😳",
  "description": "One question in the pods and the date went quiet.\\nWould you have stayed after hearing his answer?\\n\\nLove Is Blind Season 11 is streaming now on Netflix.\\n\\n#LoveIsBlind #LoveIsBlindNetflix #Netflix",
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
- header: 4-9 words, ALL CAPS, states the question the clip answers (not the answer) — accurate to what Mark actually says, not clickbait. Not shown on this campaign's video (kept for the logs), so keep it plain and accurate.
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
- header: 4-9 words, ALL CAPS, names who is in it and states the situation without spoiling the payoff, accurate to what happens — no lying clickbait. Never mention FaZe or any other channel. It may be shown on the video for the whole clip.
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
- header: 4-9 words, ALL CAPS, names who is in it, funny and accurate to the moment. NEVER mention any brand, store or clothing. Not shown on this campaign's video (kept for the logs).
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


_TONY_PROMPT = """You are a short-form video editor clipping the FINALE of "Tony Stock: Pro Trader", Ramp's scripted comedy series: skate legends Tony Hawk and Bam Margera play day traders in 2006. This is an official brand clipping campaign, but the clip must feel like a funny fan clip of the show, never like an ad.

Some footage files are the finale episode itself, others are vertical recap cuts of the finale and its livestream that may repeat the same moments.

Find the best standalone moments in this footage for a vertical YouTube Short. The brief: the clip must tell a Tony and Bam story, and its hook must match what the clip delivers.

WHAT WORKS (ranked):
1. Tony and Bam back-and-forth that builds to a real laugh: a terrible plan, a bit that escalates, one roasting the other, a reaction that sells it
2. Skaters out of place in the stock world: the 2006 trading-floor throwback, finance jargon used hilariously wrong, skate energy in a suit-and-tie room
3. A genuine surprise or reveal that gets a laugh (a twist, an unexpected guest, a deadpan answer)

HARD RULES (the brand rejects clips that break these):
- ONE continuous scene. 1-3 clips from that same scene, back-to-back, in chronological order.
- Start at the START of a line, never mid-sentence. End right after the funny part lands (the laugh or the reaction), not on the next setup.
- Tony and/or Bam must drive the moment. Never pick a stretch that is mostly a product pitch, a logo or end card, someone explaining what Ramp does, or b-roll with no dialogue.
- Nothing NSFW, no heavy swearing, nothing political. Never pick a line that sounds like real investment advice, a stock tip or a money claim taken out of the comedy.
- Clips must NOT overlap each other or the previously used segments.

Previously used moments (AVOID these themes; recap cuts repeat finale moments, so avoid the same joke too): {used_storylines}

Previously used segments (AVOID these timestamps): {used_segments}

Transcript, one numbered sentence per line (format: S<number> [start_sec-end_sec] text):
{transcript}

Field rules:
- storyline_title: snake_case, max 40 chars
- series_tag: always "tony_stock"
- header: 4-9 words, ALL CAPS, names Tony or Bam and states the situation or your stance on it (e.g. "BAM MARGERA RUNS THE TRADING DESK NOW"), never the punchline. It is on screen for the whole clip. NEVER promotional: no Ramp, no product, no "sign up", no stock tips, no money promises.
- hook_emoji: 1-2 emoji that fit the comedy (😭 💀 😂 🛹 📉) or "" if none fit"""

_TONY_YT_PROMPT = """You write YouTube Shorts metadata for a clip of "Tony Stock: Pro Trader", the comedy series where Tony Hawk and Bam Margera play 2006 day traders (finale). It must read like a fan posting a funny clip, NEVER like an ad.

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "Tony Hawk was not ready for Bam's trading plan 😭",
  "description": "Bam had one job on the trading desk.\\nTony's face says it all.\\n\\nTony Stock: Pro Trader finale @tryramp\\n\\n#TonyHawk #BamMargera #TonyStock",
  "tags": "Tony Hawk, Bam Margera, Tony Stock Pro Trader, Tony Stock finale, skateboarding legends",
  "pinned_comment": "Who's the better trader, Tony or Bam? 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens in this clip. Do not invent events, quotes or outcomes. No clickbait.
- title: starts with "Tony Hawk" or "Bam Margera", short, fun, conversational.
- description: 1-2 short fun lines about the moment → blank line → a line that contains "@tryramp" (e.g. "Tony Stock: Pro Trader finale @tryramp") → blank line → 3-5 hashtags (names, the show).
- It must NOT sound like an ad: never "sign up", "check out Ramp", "Ramp is/helps/lets/saves", "try it", "link in bio", discounts, product features or any call to action. @tryramp is a tag, not a pitch.
- NEVER stock tips, investment advice, price predictions, money promises or fake claims. Nothing NSFW or political.
- tags: 5-10 specific tags (names, show), no brand slogans.
- pinned_comment: a short fun question about the moment (who was funnier, what Bam does next)."""


_BILF_PROMPT = """You are a short-form editor clipping the BILF Podcast ("Boss I'd Like to Follow", hosted by Mara Dorne: founders, CEOs, artists and personalities on how they built what they built) for an official, brand-approved clipping campaign run by Runback.TV.

The audience: millennials deep into their careers or making the jump to their own business. They want real talk about money, work and building something, from people who have done it. The goal: someone sees the clip, has an opinion, and goes to find the full episode.

Find the best standalone moments in this episode for a vertical YouTube Short.

WHAT WORKS (ranked):
1. Money numbers said out loud: an exit, a deal, a first paycheck, what something cost, a stat that stings
2. Founder lessons: the mistake, the pivot, the rejection, the thing they would do differently
3. Hot takes on business, work, money, dating or success that people will argue with in the comments
4. Behind-the-scenes stories: what happened in the room before anyone knew their name
5. Mara's question plus the guest's answer: she asks what everyone is thinking, and the pair is often the whole clip
6. Funny and candid beats: the laugh, the pause, the answer nobody expected

HARD RULES (the brand rejects clips that break these):
- ONE clear moment. 1-2 clips from one continuous passage, back-to-back, chronological.
- Open on the strongest line, not the setup. It must be a complete thought: never cut mid-thought.
- KEEP IT POSITIVE: Mara and every guest must look like the bosses they are. Nothing that mocks or embarrasses them, nothing taken out of context, never start or stop the clip so a line means something different.
- NEVER include a sponsor read, ad break, promo code, "this episode is brought to you by" or any product plug.
- Nothing NSFW, violent or political.
- Clips must NOT overlap each other or the previously used segments (these also include known ad-read segments).

Previously used moments from this episode (AVOID these ideas): {used_storylines}

Previously used / forbidden segments (AVOID these timestamps): {used_segments}

Transcript, one numbered sentence per line (format: S<number> [start_sec-end_sec] text):
{transcript}

Field rules:
- storyline_title: snake_case, max 40 chars
- storyline_summary: one sentence that names the guest (full name) and the moment
- series_tag: always "bilf"
- header: 4-9 words, ALL CAPS, names the guest or Mara and states the question or stake the clip answers (not the answer). Accurate to what is said. Not shown on this campaign's video (the show label is), kept for the logs.
- context_line: REQUIRED here. It is the text hook on screen for the first 2.5 seconds: 4-10 words, names the guest and the stake (e.g. "Stax founder on getting turned down by every bank"). It must match what the clip delivers, never overpromise, never mock, and never put words in quotation marks.
- closing_line: a question that invites a take in the comments (e.g. "Would you have quit your job for this?").
- hook_emoji: usually "" for this show; at most one if it really fits"""

_BILF_YT_PROMPT = """You write YouTube Shorts metadata for a clip of the BILF Podcast ("Boss I'd Like to Follow", hosted by Mara Dorne). Audience: career millennials and founders who want real talk about money, work and building something.

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "<guest's full name> on <the concrete stake of THIS moment>",
  "description": "<a hook, a take or a question about THIS moment that invites a reply>\\n\\n<guest's full name> on the BILF Podcast @bilfpod\\n\\n#bilfpodpartner #BILFPodcast #<topic>",
  "tags": "BILF Podcast, Mara Dorne, <guest's full name>, <company or show>, <topic>",
  "pinned_comment": "<a genuine question about the idea that invites opinions> 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what is actually said in this clip. No misleading hooks, no claims the clip does not deliver.
- Put words in quotation marks ONLY if they were said word for word in the clip; otherwise paraphrase without quotes.
- title: starts with the guest's full name (or "Mara Dorne" if it is her moment), then the concrete stake. Positive or neutral: never mock or embarrass the guest or Mara.
- description: line 1 leads with a hook, a take or a question that invites a reply → blank line → a line that names the guest "on the BILF Podcast" and tags @bilfpod → blank line → hashtags line that includes #bilfpodpartner (3-5 hashtags).
- Call to action: pick ONE at random and vary it from post to post: none at all, "Full episode on the BILF Podcast YouTube channel", or "Watch the full conversation: @bilfpod on YouTube".
- Write it for this clip: nothing copy-pasted, nothing that reads like an ad (no "sponsored", promo codes, discounts or links), never call this page official.
- tags: 5-10 specific tags (show, host, guest, company, topic).
- Nothing NSFW, no swearing, nothing political.
- pinned_comment: a genuine question about the idea that invites discussion."""


_LADBIBLE_PROMPT = """You are a short-form video editor clipping LADbible Entertainment's episode with John Cena and Jessica Biel ("Snack Wars: Showdown") for an official clipping campaign run for LADbible. The episode cuts between two formats: Snack Wars (white set: the couple taste snacks from around the world and pick a winner and a loser) and Showdown (red set: head-to-head games: make the sound on the card for the other to guess, guess the famous movie quote, cup stacking).

The audience: fans of John Cena and Jessica Biel and of celebrity entertainment. The goal: a stranger laughs at the couple's banter, watches to the end and wants the full episode on LADbible.

Find the best standalone moments in this episode for a vertical YouTube Short.

WHAT WORKS (ranked):
1. A big reaction: one of them tastes something and their face and words sell it (disgust played for laughs, a surprise favourite, a dramatic verdict)
2. Couple banter that escalates: teasing, trash talk about the game, one calling the other out, a running joke that pays off
3. A great one-liner or a deadpan John Cena answer that lands a laugh
4. A competitive moment in the Showdown: a quote guessed (or butchered), a point won or lost, a bold guess, the final winner

HARD RULES (the brand rejects clips that break these):
- ONE continuous scene. 1-3 clips from that same passage, back-to-back, in chronological order, and from ONE format (never mix a Snack Wars bit with a Showdown bit).
- Open on the line or reaction that hooks, never on the host explaining the rules or reading out the next round. Never the intro or the "watch next / subscribe" end card.
- Keep it fun and kind: never pick a stretch that makes either of them look bad, nothing taken out of context, never cut so a line means something different from what they meant.
- Nothing NSFW, no heavy swearing, nothing political, no product plugs or "link in bio" talk.
- Clips must NOT overlap each other or the previously used segments.

Previously used moments from this episode (AVOID these themes): {used_storylines}

Previously used segments (AVOID these timestamps): {used_segments}

Transcript, one numbered sentence per line (format: S<number> [start_sec-end_sec] text):
{transcript}

Field rules:
- storyline_title: snake_case, max 40 chars
- storyline_summary: one sentence that names John Cena and/or Jessica Biel and what happens
- series_tag: always "ladbible"
- header: 4-9 words, ALL CAPS, names John Cena or Jessica Biel and states the situation (e.g. "JOHN CENA VS A SWEDISH SALT LIQUORICE"), never the reaction or the verdict. Not shown on this campaign's video (kept for the logs).
- hook_emoji: 1-2 emoji that fit (😭 😂 🤢 🍬 😳) or "" if none fit"""

_LADBIBLE_YT_PROMPT = """You write YouTube Shorts metadata for a clip of LADbible Entertainment's episode with John Cena and Jessica Biel (Snack Wars: Showdown). It must read like a fan posting a funny clip, and it must describe exactly what happens: the brand rejects misleading captions.

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "John Cena tries <the concrete snack or situation in THIS clip> 😭",
  "description": "<one fun line about what actually happens in this clip>\\n\\nJohn Cena and Jessica Biel on @ladbible\\n\\n#JohnCena #JessicaBiel #LADbible",
  "tags": "John Cena, Jessica Biel, LADbible, Snack Wars, celebrity snack challenge",
  "pinned_comment": "<a short fun question about the moment> 👇"
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens in this clip. No misleading hooks, no invented quotes, outcomes or drama between them. No clickbait.
- title: starts with "John Cena" or "Jessica Biel", short, fun, conversational.
- description: 1-2 short lines about the moment → blank line → a line that contains "@ladbible" (e.g. "John Cena and Jessica Biel on @ladbible") → blank line → 3-5 hashtags (names, LADbible).
- Never call this page official, never mention leaks or exclusives, no promo codes or links.
- Keep it kind: never mock either of them. Nothing NSFW, no swearing, nothing political.
- tags: 5-10 specific tags (names, LADbible, the snack or game).
- pinned_comment: a short fun question about the moment (would you eat it, who won)."""


_MILKXP_PROMPT = """You are a short-form video editor clipping Episode 3 of milkXP, a show where big viral creators take on fun challenges, for an official clipping campaign. milkXP is made by a milk brand, and milk is a regulated product: the brand's rules below are strict and a clip that breaks one is rejected.

This episode: creator Ben Azelart teams up with teacher Mr. Greg to secretly build a student lounge inside a real school on a $5,000 budget without getting caught, while Mr. Greg teaches the 50/30/20 budgeting rule (needs, wants, savings); the students move in and grade Mr. Greg at the end. Spell the names exactly: Ben Azelart, Mr. Greg.

The audience: students and young adults in the US who follow these creators. The goal: a stranger laughs or smiles, watches to the end and wants the full show on YouTube.

Find the best standalone moments in this episode for a vertical YouTube Short. The brand wants TWO kinds of clips (see CLIP TYPES below if given):
- FUNNY MOMENTS: a laugh, a fail, a shocked face, a big play, a win or a close call in a challenge
- MILK MOMENTS: a creator talks about milk in their own words (why they drink it, a milk memory, milk helping them focus or lock in), positive and genuine

HARD RULES (the brand rejects clips that break these):
- ONE continuous scene. 1-3 clips from that same scene, back-to-back, in chronological order.
- Hook in the first 2 seconds: never open on someone explaining the rules of the challenge.
- MILK RULES: only positive about milk, milkXP, the creators, teachers and students. NEVER pick a moment where anyone names or drinks another drink (oat, almond, soy, coconut, rice or nut milk, raw milk, juice, soda, coffee, sports or energy drinks, protein shakes, alcohol) or names any drink or milk brand. NEVER pick a moment where someone says milk is the best, perfect, ideal, greatest or healthy (or any health claim), or says anything negative or sarcastic about milk.
- No swearing or adult words at all (not even mild ones like damn or hell, and no bleeped words), nothing NSFW, violent or political.
- Never pick the sponsor read ("a quick break from our sponsor", "brought to you by", nutrients and vitamins): it is an ad, not a creator talking in their own words.
- Keep it kind: never pick a stretch that mocks or embarrasses a creator, a teacher or a student.
- Clips must NOT overlap each other or the previously used segments.

Previously used moments from this episode (AVOID these themes): {used_storylines}

Previously used segments (AVOID these timestamps): {used_segments}

Transcript, one numbered sentence per line (format: S<number> [start_sec-end_sec] text):
{transcript}

Field rules:
- storyline_title: snake_case, max 40 chars
- storyline_summary: one sentence that names the creator(s) and what happens
- series_tag: always "milkxp"
- clip_type: "funny" or "milk_moment"
- header: 4-9 words, ALL CAPS, names the creator and states the challenge or situation (e.g. "TWO CREATORS RACE TO STACK 50 CUPS"), never the outcome. It is on screen for the whole clip. It must follow the MILK RULES: never best, perfect, ideal, greatest, healthy or any claim about milk, never another drink or a brand, never sarcastic, nothing like an ad ("buy", "check out", "try it").
- hook_emoji: 1-2 emoji that fit (😂 😳 🔥 🥛 🏆) or "" if none fit"""

_MILKXP_YT_PROMPT = """You write YouTube Shorts metadata for a clip of milkXP Episode 3, a show where big viral creators take on fun challenges. milkXP is made by a milk brand: the caption must sound like a fan posting a fun clip in their own words, NEVER like an ad, and it must follow the brand's milk rules exactly.

Moment : {storyline}
Summary: {summary}

Return ONLY valid JSON, no markdown:
{{
  "title": "<creator's name> <short fun tease about THIS moment> 😂",
  "description": "<one short fun line about what actually happens>\\n\\nFrom milkXP Episode 3. Full show on YouTube @milkxpofficial\\n\\n#milkXP #<creator> #<challenge>",
  "tags": "milkXP, <creator's name>, <challenge>, creator challenge",
  "pinned_comment": ""
}}

Rules (brand requirements — breaking any of these gets the clip rejected):
- Describe ONLY what actually happens in this clip, in your own words. Short and fun. No misleading hooks, no invented quotes or outcomes.
- title: starts with the creator's name (or "milkXP"), under 60 characters.
- description: 1 short line → blank line → a line that tags @milkxpofficial (pointing to the full show on YouTube is good) → blank line → 3-5 hashtags.
- NOT AN AD: never "buy", "check out", "shop", "try it", "get yours", "link in bio", discounts, codes, sponsor talk or any call to buy something.
- MILK RULES: never call milk the best, perfect, ideal, greatest or healthy, and no health or nutrition claims at all. Allowed: "Milk can help you focus", "Milk can help you lock in". Only positive about milk, milkXP, the creators, teachers and students. No sarcasm, no negativity, nothing gross.
- Never name any other drink (oat, almond, soy, coconut, rice or nut milk, raw milk, juice, soda, coffee, tea, sports or energy drinks, protein shakes, alcohol) and never name any brand of milk or of any drink.
- No swearing or adult words at all, nothing NSFW or political.
- tags: 5-10 specific tags (milkXP, creator names, the challenge), no brands.
- pinned_comment: always "" (this campaign never comments)."""


_PROMPTS = {
    'youngla':        (_YLA_PROMPT, _YLA_YT_PROMPT),
    'loveisblind':    (_LIB_PROMPT, _LIB_YT_PROMPT),
    'markmanson':     (_SOLVED_PROMPT, _SOLVED_YT_PROMPT),
    'loveandjustice': (_LJ_PROMPT, _LJ_YT_PROMPT),
    'unpacked':       (_UNPACKED_PROMPT, _UNPACKED_YT_PROMPT),
    'tonyhawk':       (_TONY_PROMPT, _TONY_YT_PROMPT),
    'bilf':           (_BILF_PROMPT, _BILF_YT_PROMPT),
    'ladbible':       (_LADBIBLE_PROMPT, _LADBIBLE_YT_PROMPT),
    'milkxp':         (_MILKXP_PROMPT, _MILKXP_YT_PROMPT),
}

# Shape example per campaign. Sentence numbers are 0 placeholders: the model
# once copied an example's real-looking timestamps (commit 28b18e2). Headers
# state the situation or a stance; none uses a vague template word.
_EXAMPLES = {
    'friends': ('standalone_scene', 'joey_dodges_central_perk_bill',
                'Joey cannot pay his Central Perk tab and quietly starts working there', 'joey_being_joey',
                "JOEY CAN'T PAY HIS CENTRAL PERK TAB", '', 'scene',
                'Rachel catches Joey behind the counter', 'Q',
                'Joey owes Central Perk a lot of money', 'Gunther knew the whole time 😭', None),
    'loveisblind': ('standalone_scene', 'pods_hard_question_about_kids',
                    'He asks the question she was dreading in the pods and her answer surprises him',
                    'love_is_blind', "TAYLOR ASKS THE QUESTION SHE WAS DREADING", '😳', 'scene',
                    'His question and her stunned answer', 'Q',
                    'They have known each other for four days', 'Would you have answered first? 👀',
                    'she knows'),
    'markmanson': ('standalone_scene', 'failure_is_the_tuition',
                   'Mark argues failure feels worse than it is because we treat it as identity', 'solved',
                   'WHY FAILURE HURTS MORE THAN IT SHOULD', '', 'scene',
                   'Mark opens on the bold claim and lands the reframe', 'BC',
                   'Mark Manson on why one bad result feels personal', 'Is he right about failure?', None),
    'loveandjustice': ('standalone_scene', 'zach_rules_who_is_wrong',
                       'After hearing both sides, Zach tells the couple who is at fault', 'love_and_justice',
                       'ZACH HEARS BOTH SIDES OF THE PHONE FIGHT', '⚖️', 'scene',
                       "Zach's verdict and the couple's reaction", 'MA',
                       'She checked his phone while he slept', 'Who was wrong here? 👇', 'he knows'),
    'unpacked': ('standalone_scene', 'sixty_thousand_dollar_pull',
                 'The guest opens the pack and pulls the card everyone was hoping for', 'unpacked',
                 'STEVE AOKI OPENS A $60,000 PACK', '🤯', 'scene', 'The pull and the reaction', 'BC',
                 'Steve Aoki has chased this card for years', 'Keep it or sell it?', None),
    'youngla': ('standalone_scene', 'bodybuilder_panics_in_escape_room',
                'A huge bodybuilder completely loses it when the lights go out', 'gym',
                'A 250 POUND BODYBUILDER VS A DARK ESCAPE ROOM', '😭', 'scene',
                'The lights go out and he panics', 'AV',
                'Strongest guy in the room, scared of the dark', 'Would you last longer than him? 😭', None),
    'tonyhawk': ('standalone_scene', 'bam_pitches_his_trading_plan',
                 'Bam walks Tony through his trading plan and Tony slowly realises it is a skate trick',
                 'tony_stock', 'BAM MARGERA RUNS THE TRADING DESK NOW', '😭', 'scene',
                 "Bam's plan and Tony's reaction", 'MA',
                 None, 'Tony deserved a better desk partner 😭', None),
    'bilf': ('standalone_scene', 'founder_turned_down_by_every_bank',
             'The guest describes being turned down by every bank before building her company', 'bilf',
             'WHAT EVERY BANK TOLD THIS FOUNDER', '', 'scene',
             'She opens on the rejection and lands the lesson', 'ST',
             'Founder on hearing no from every bank in the room', 'Would you have kept going after that? 👇',
             None),
    'ladbible': ('standalone_scene', 'cena_tastes_salt_liquorice',
                 'John Cena tries a snack he has never seen before and Jessica Biel watches him suffer',
                 'ladbible', 'JOHN CENA VS A SNACK HE HAS NEVER SEEN', '😭', 'scene',
                 'The first bite and his reaction', 'AV',
                 None, None, None),
    'milkxp': ('standalone_scene', 'creator_cup_stack_close_call',
               'Two creators race to finish the challenge and it comes down to the last second',
               'milkxp', 'TWO CREATORS RACE THE CLOCK ON THE LAST ROUND', '😳', 'scene',
               'The final seconds and the reaction', 'AV',
               None, None, None),
}

# Campaign-specific originality layer, when the brief allows less (or asks for more) than the genre default.
_CAMPAIGN_LAYER_RULES = {
    'tonyhawk': ('Originality layer for this brand: the header (on screen the whole clip) states the Tony and Bam '
                 'situation or your stance, and closing_line is your verdict or a fun question for the viewer '
                 '(e.g. "Tony deserved a better desk partner"). context_line: null and annotations: [] (the brief '
                 'allows no other text). Nothing promotional: no Ramp, no product, no call to action, no stock tips.'),
    'bilf': ('Primary layer for this podcast: context_line is REQUIRED, it is the on-screen text hook for the first '
             '2.5 s (names the guest and the stake, matches what the clip delivers); closing_line is a question that '
             'invites a take. Never quotation marks around words that were not said verbatim. annotations: [].'),
    'ladbible': ('This campaign shows no added text besides the hook headline (the header), the captions and the '
                 'brand watermark: context_line: null, closing_line: null, annotations: []. The header must describe '
                 'what really happens in the clip (the brand rejects misleading captions).'),
    'milkxp': ('Originality layer for this brand: the header (on screen the whole clip) names the creator and the '
               'challenge or situation, and builds suspense without giving away the outcome. context_line: null, '
               'closing_line: null, annotations: [] (the brief allows only a short hook). The header follows the '
               'MILK RULES: no claim about milk, no other drink, no brand, nothing sarcastic or ad-like.'),
}

_LAYER_RULES = {
    'sitcom': ('Primary layer for a sitcom: the header is your STANCE or the situation, and closing_line is your '
               'verdict or a question for the viewer (e.g. "Gunther knew the whole time", "Team Ross or Team '
               'Rachel?"). Also give context_line: one line of pre-context a stranger needs (e.g. "Ross has been '
               'lying about this for 3 seasons"); some uploads show it instead of the header. annotations: [].'),
    'reality': ('Primary layer for reality/drama: annotations, 1-3 stage directions in asterisks that interpret '
                'the subtext of a silent or tense beat (e.g. "*she knows*", "*waiting for his answer*"), each tied '
                'to the S-number it appears on, plus closing_line (a verdict or a question). Also give context_line '
                '(one line of pre-context).'),
    'podcast': ('Primary layer for a podcast/talk clip: context_line, who is speaking and what is at stake in one '
                'line (shown 0-2.5 s), plus closing_line (a question that invites a take). annotations: [].'),
}
_LAYER_RULES['drama'] = _LAYER_RULES['reality']
_LAYER_RULES['talk'] = _LAYER_RULES['podcast']


def _example_json(campaign):
    (fmt, title, summary, tag, hook, emoji, label, desc, hook_class, context, closing,
     note) = _EXAMPLES.get(campaign, _EXAMPLES['friends'])
    cand = {
        'format': fmt, 'storyline_title': title, 'storyline_summary': summary, 'series_tag': tag,
        'hook_class': hook_class,
        'opening_line': '<first sentence of the first clip, verbatim>',
        'payoff_line': '<last sentence of the last clip, verbatim>',
        'payoff_sentence': 0,
        'header': hook, 'hook_emoji': emoji,
        'reveal': '<what the ending reveals, in plain words>',
        'context_line': context, 'closing_line': closing,
        'annotations': [{'sentence': 0, 'text': f'*{note}*'}] if note else [],
        'stake_line': '<the situation in 6-8 words: a person and a concrete noun or event>',
        'emotion': 'laughter',
        'single_story_arc': False,
        'clips': [{'start_sentence': 0, 'end_sentence': 0, 'label': label, 'description': desc}],
        'hook_strength': 8, 'payoff_strength': 8, 'standalone_clarity': 9,
        'rubric': {'stake': 2, 'payoff': 2, 'emotion': 2, 'framing': 2, 'originality': 2},
    }
    edit = _edit(campaign)
    if edit.get('headline') and edit.get('reaction_titles'):
        head, kws, emo = _EDIT_HEADLINES.get(campaign, _EDIT_HEADLINE_DEFAULT)
        cand['header'], cand['hook_emoji'] = head, emo
        items = list(cand.items())                    # header_keywords goes next to the header
        at = [k for k, _ in items].index('hook_emoji')
        items.insert(at, ('header_keywords', kws))
        cand = dict(items)
    return json.dumps({'candidates': [cand, '...2-4 more candidates in the same shape...']},
                      ensure_ascii=False, indent=2)


def _vague_rule():
    from campaigns import VAGUE_ALWAYS, VAGUE_NEEDS_OBJECT
    return (f'never use {", ".join(sorted(VAGUE_ALWAYS))}; never use {", ".join(sorted(VAGUE_NEEDS_OBJECT))} '
            f'unless the concrete thing follows ("this sandwich" is fine, "he did this" is not)')


def _edit(campaign):
    """The campaign's edit-layer settings (campaigns.edit_config), every item off when unknown."""
    try:
        from campaigns import CAMPAIGNS, edit_config
        return edit_config(CAMPAIGNS.get(campaign) or {})
    except Exception:
        return {}


# Edit-layer headline examples (P0-2 / P0-7): curiosity or emotion from the reacting person's
# point of view, never "<person> does X". Shape examples only (the model copies tone, not words).
_EDIT_HEADLINES = {
    'ladbible':   ('She was NOT ready for this', ['NOT', 'READY'], '😭'),
    'milkxp':     ('They have ONE minute left and NO plan', ['ONE', 'NO'], '😳'),
    'unpacked':   ("Every collector's WILDEST dream", ['WILDEST'], '😭'),
    'markmanson': ('Why you can never WIN their approval', ['WIN'], ''),
}
_EDIT_HEADLINE_DEFAULT = ('He did NOT see that coming', ['NOT'], '😳')


def _headline_rule(campaign, recent):
    ex, kws, emo = _EDIT_HEADLINES.get(campaign, _EDIT_HEADLINE_DEFAULT)
    return (f'- header (the on-screen HOOK HEADLINE, read in frame 0 over the clip): 3-8 words, a curiosity or '
            f'emotion line from the point of view of the person reacting in the clip, e.g. "{ex}", "They made '
            f'donuts... at a CAFE", "If it\'s not a yes, it\'s a no". Never a plain description ("John Cena tries a '
            f'snack", "Mark Manson on failure"), never the reveal or the punchline. Mixed case, with the 1-2 words '
            f'that carry the emotion in CAPS. No content word from payoff_line or reveal; {_vague_rule()} (a '
            f'closing "this" that points at the clip is fine). Do not start with the same three words as a recent '
            f'header: {recent}.\n'
            f'- header_keywords: the 1-2 words of the header to colour (e.g. {json.dumps(kws)}), copied from it.\n'
            f'- hook_emoji: at most one of 😭 😳 🤯 when it fits the emotion, else "" (about half the time "").')


def _answer_rules(campaign, limits, target=None, genre=None, recent_headers=None):
    lo, hi = limits
    t_lo, t_hi = target or TARGET_SECONDS
    genre = genre or GENRES.get(campaign, 'sitcom')
    edit = _edit(campaign)
    tail = 1.5 if edit.get('length') else PAYOFF_MAX_TAIL
    payoff_rule = (f'- payoff_sentence: the S-number of the punchline, reveal or verdict. It must be the LAST sentence '
                   f'of the last clip (at most one short reaction line, under {tail:g} seconds, after it): the clip '
                   f'loops on the payoff.' if edit.get('length') else
                   '- payoff_sentence: the S-number of the punchline, reveal or verdict. The clip must end at most 3 '
                   'seconds after it.')
    hooks = '; '.join(f'{k} = {v}' for k, v in HOOK_CLASSES.items())
    recent = '; '.join(f'"{h}"' for h in (recent_headers or [])[-30:]) or 'none yet'
    header_rule = (_headline_rule(campaign, recent) if edit.get('headline') and edit.get('reaction_titles') else
                   f'''- header (replaces hook_text): at most 9 words, ALL CAPS, no emoji, names a person and states the SITUATION or a STANCE (a side, a verdict, a superlative), never the reveal or the punchline. No content word from payoff_line or reveal; {_vague_rule()}. Do not start with the same three words as a recent header: {recent}.''')
    long_arc = (' Up to 60s only when single_story_arc is true (one escalating first-person story by one speaker) '
                'and the payoff is in the last 30%.' if genre in ('podcast', 'talk') else '')
    return f"""

HOW TO PICK AND CUT (these rules override anything above):
- Give {N_CANDIDATES[0]}-{N_CANDIDATES[1]} DIFFERENT candidate moments (different scenes or ideas, not variations of one), best first.
- Cut by sentence number, never by seconds. Each clip runs from "start_sentence" to "end_sentence" (S-numbers from the transcript, inclusive); code turns them into exact cut times.
- start_sentence must begin a thought, never a line that continues the one before it. end_sentence must be a COMPLETE sentence that finishes the beat (the punchline, answer, verdict or reaction line), never a line that trails off or carries on into the next line.
- Length: aim for {t_lo:.0f}-{t_hi:.0f} seconds in total (use the timestamps). Never under {lo:.0f}s or over {hi:.0f}s.{long_arc} Shorter wins when the moment is complete: end right on the payoff.
{payoff_rule}
- The OPENING LINE is the hook. The first sentence of the first clip must stop a scrolling stranger on its own, with no context: an odd or bold claim, a direct question, an accusation or confession, or a line dropped into the middle of a tense moment. Speech starts in the first second.
- hook_class: label what the first 3 seconds are, honestly: {hooks}. Never offer an SL candidate: start later, on the first question or conflict line.
- The SECOND beat must escalate by about 5 seconds in: a reply, a twist, a reaction or a new stake, never more setup. Our retention data shows viewers leave between seconds 5 and 8 when the line after the hook is just context, so cut straight from the hook to the escalation.
- Never open on filler ("so", "um", "uh", "okay", "well", "yeah", "and", "but", "like", "anyway", "alright"), a greeting, someone explaining or setting up what is about to happen, or a line that only makes sense after an earlier one. Cut the setup: the header supplies the context.
- Pick a concrete situation with a specific stake (stake_line: a person and a concrete noun or event in 6-8 words), not an abstract idea or opinion. Emotion must be high-arousal (laughter, shock, cringe, outrage, tension, triumph) or wholesome WITH stakes. For drama and reality, prefer threat, stakes or a call-out over wholesome or low-stakes beats.
- opening_line: the first sentence of the first clip, copied VERBATIM. payoff_line: the last sentence of the last clip, copied VERBATIM.
{header_rule}
- reveal: what the ending reveals, in plain words (only used to check the header does not give it away).
- ORIGINALITY LAYER (YouTube demotes clips that add nothing original). {_CAMPAIGN_LAYER_RULES.get(campaign) or _LAYER_RULES.get(genre, _LAYER_RULES['sitcom'])}
- header, context_line, closing_line and annotations are YOUR voice: an opinion, context or a question. Never quote or paraphrase the dialogue, never narrate what is on screen, never mock or insult anyone in the clip.
- Score each candidate 1-10, honestly and harshly (an ordinary moment is a 5): hook_strength = would a stranger stop scrolling on the opening line alone; payoff_strength = does the ending land (laugh, reveal, verdict, reaction); standalone_clarity = does it make sense with zero context.
- rubric, each 0-2, honestly: stake (2 = the situation fits in 6 words with a person and a concrete noun or event; 0 = abstract), payoff (2 = a clear reversal or punchline; 0 = none), emotion (2 = high-arousal; 1 = wholesome with stakes; 0 = mild or nostalgic), framing (2 = the header states a specific conflict or stance; 1 = generic; 0 = a vague tease), originality (2 = your lines add a stance, context or comparison that is not a description; 1 = annotations only; 0 = none).

Return ONLY a valid JSON object, no markdown, no explanation. The example shows the SHAPE only: its sentence numbers, titles and lines are placeholders; use real S-numbers and wording from THIS transcript:
{_example_json(campaign)}"""


_TRANSCRIPT_MAX = 1800     # chars of clip transcript sent with the metadata prompt


def _faith_rules(transcript):
    if not transcript:
        return ''
    text = ' '.join(str(transcript).split())
    if len(text) > _TRANSCRIPT_MAX:
        text = text[:_TRANSCRIPT_MAX].rsplit(' ', 1)[0] + ' ...'
    return f"""
- FAITHFUL: the title and description may claim ONLY what is said in this clip. Never add a person, role or relationship (boss, husband, mom, investor, coworker...) or an event the clip does not contain; use the speaker's own framing (if she says she was "laughed out of the room", do not turn it into "her boss laughed at her").
- title_check: also return "title_check": {{"supported_by": "<the clip words the title's claim rests on, copied verbatim>", "adds_unsupported": false}}. If your title adds anything the clip does not say, rewrite the title; never return adds_unsupported true.
- What is actually said in the clip (transcript): "{text}".
"""


def _title_rules(campaign, story, title_names, recent_titles, transcript=None):
    names = ', '.join(title_names or []) or 'the show or the person in the clip'
    actor = (' For a scripted show name the ACTOR (e.g. "Matthew Perry\'s Chandler..."), not only the character.'
             if campaign == 'friends' else '')
    recent = '\n'.join(f'  - {t}' for t in (recent_titles or [])[-30:]) or '  (none yet)'
    if _edit(campaign).get('reaction_titles'):
        first = (f'- title: a FEELING or a general statement from the point of view of the person reacting in the clip '
                 f'(e.g. "Jessica Biel was NOT ready for salty liquorice 😭", "If it\'s not a yes, it\'s a no", "Bro '
                 f'was NOT ready for his grades 😭"), never "<Celebrity> does X" / "<Name> on Y" ("John Cena tries a '
                 f'snack", "Mark Manson on failure"). Name one real person people search for ({names}) where it reads '
                 f'naturally.')
    else:
        first = (f'- title: start with the real name US viewers search for, one of: {names}.{actor} Then the concrete '
                 f'situation: a person and a specific object or event (e.g. "Matt LeBlanc\'s Joey dodges his Central '
                 f'Perk bill").')
    return f"""

TITLE AND HASHTAG RULES (these override the example and rules above):
{first}
- title: under 60 characters, 1-2 emoji max and only at the end, no hashtags.
- title: never start with "Wait", "Nobody", "No one", "POV", "When you" or "@", and never "did X really just say that".
- title: {_vague_rule()}.
- title and description must NOT give away the payoff. The clip opens on "{story.get('opening_line', '')}" and ends on "{story.get('payoff_line', '')}": tease the situation, never state the outcome or reuse the payoff's key words.
- title: different wording AND structure from every one of these recent titles:
{recent}
- description: ends with ONE line of 3-5 specific hashtags: the show or person first, then names or the exact topic. Never #viral #trending #foryou #fyp #ytshorts #shorts #shortsfeed #funnymoments #comedy #relationshipgoals or any other broad tag.
- tags: 5-10 specific tags (show, real names, characters, episode or topic), no generic words like "funny moments" or "comedy".""" + _faith_rules(transcript)


def _parse_json(raw):
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
    return json.loads(raw)


def yt_metadata_prompt(story, episode_name, campaign='friends', title_names=None, recent_titles=None,
                       notes=None, transcript=None):
    """The metadata prompt (notes: per-episode facts such as the guest's name; transcript: the clip's words)."""
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
    if notes:
        prompt += f"\n\nEpisode notes: {notes}"
    return prompt + _title_rules(campaign, story, title_names, recent_titles, transcript)


def generate_yt_metadata(story, episode_name, api_key, retries=3, campaign='friends',
                         title_names=None, recent_titles=None, notes=None, transcript=None):
    api_key = api_key.strip()
    prompt = yt_metadata_prompt(story, episode_name, campaign, title_names, recent_titles, notes, transcript)
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


_LAYER_FIELD_RULES = {
    'context_line': ('context_line: the on-screen text hook for the first 2.5 seconds, 4-10 words, names who is '
                     'speaking and the concrete stake of THIS clip (e.g. "Stax founder on getting turned down by '
                     'every bank"). It must match what the clip delivers and never overpromise.'),
    'closing_line': ('closing_line: shown over the last second, a verdict or a short question that invites a take '
                     'in the comments (e.g. "Would you have quit your job for this?"), at most 10 words.'),
}


def layer_line_prompt(story, field, transcript, campaign='friends', avoid=None):
    """Follow-up prompt for one missing originality line (context_line / closing_line)."""
    text = ' '.join(str(transcript or '').split())[:_TRANSCRIPT_MAX]
    tried = f'\nThese were rejected (echoed the dialogue or broke a rule), write something different: {avoid}' if avoid else ''
    return (f"You are packaging a vertical clip ({campaign}). Moment: {story.get('storyline_summary', '')}\n"
            f"What is said in the clip: \"{text}\"\n\n"
            f"Write the {_LAYER_FIELD_RULES[field]}\n"
            "It is YOUR voice: never quote or paraphrase the dialogue (no 3-word run from the transcript), never "
            "narrate what is on screen, no quotation marks, no emoji, nothing promotional, never mock anyone." + tried +
            f'\nReturn ONLY JSON: {{"{field}": "..."}}')


def layer_line(story, field, transcript, api_key, campaign='friends', avoid=None):
    """One cheap Gemini ask for a missing originality line; None on any failure (never retried)."""
    try:
        payload = {'contents': [{'parts': [{'text': layer_line_prompt(story, field, transcript, campaign, avoid)}]}],
                   'generationConfig': {'temperature': 0.5}}
        data = _parse_json(_gemini_post(payload, api_key.strip(), timeout=45))
        line = re.sub(r'\s+', ' ', str((data or {}).get(field) or '')).strip().strip('"')
        return line or None
    except Exception as e:
        print(f"      {field} follow-up failed: {e}")
        return None


def candidates_prompt(sents, used_segments=None, used_storylines=None, campaign='friends', limits=(12, 40),
                      target=None, genre=None, recent_headers=None, hints=None):
    """The moment-picking prompt (hints: per-episode notes such as the guest and flagged moments)."""
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
    )
    if hints:
        prompt += f"\n\nEPISODE NOTES:\n{hints}"
    return prompt + _answer_rules(campaign, limits, target=target, genre=genre, recent_headers=recent_headers)


def analyze_candidates(sents, api_key, used_segments=None, used_storylines=None, retries=5,
                       campaign='friends', limits=(12, 40), target=None, genre=None, recent_headers=None,
                       hints=None):
    """Ask Gemini for 3-5 candidate moments over the numbered sentences; returns the raw candidate dicts."""
    api_key = api_key.strip()
    prompt = candidates_prompt(sents, used_segments, used_storylines, campaign, limits, target, genre,
                               recent_headers, hints)

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
