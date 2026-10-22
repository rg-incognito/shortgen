"""
Campaign profiles — one pipeline, many sources.

`friends` is the original sitcom channel behaviour. Every other profile is a
Whop Content Rewards campaign: footage comes from the brand's own Drive files,
and the brief's hard rules (clip length, required caption text, banned topics)
are enforced in code rather than trusted to the LLM.
"""
import datetime
import re
from zoneinfo import ZoneInfo

# Publish times are set in US Eastern (the payout audience is 40%+ US/UK/CA/AU,
# mostly US) so they follow daylight saving. Every slot must fall in the US
# evening, when India is asleep: the Shorts seed batch is drawn from whoever is
# scrolling at publish time, and daytime-UTC publishes on this IN-country channel
# seeded 50-100% Indian audiences (failing Whop's demographics rule), while
# 22:30-04:30 UTC slots stayed ~100% tier-1.
_ET = ZoneInfo('America/New_York')
_WINDOW_UTC = (datetime.time(22, 30), datetime.time(4, 30))   # 6:30-11:30 PM ET in both EST and EDT
_PROCESSING_MIN = 15      # YouTube needs a few minutes before a scheduled publish
_LATE_GRACE_MIN = 90      # run finished a bit after its slot -> publish ASAP, not tomorrow
# A late run publishes up to this long after its slot; the duplicate-slot guard must look that far back.
SLOT_GUARD_MIN = _LATE_GRACE_MIN + _PROCESSING_MIN + 15

CAMPAIGNS = {
    'friends': {
        'source':          'drive_folder',
        'max_runs':        4,
        'archive':         True,
        'silence_thresh':  0.45,
        # Clips under ~27s held the most viewers; every breakout was 10-30s.
        'min_seconds':     12.0,
        'max_seconds':     35.0,
        'playlist_map':    None,     # main.py's _SERIES_PLAYLIST
        # Titles lead with what US viewers search: the show or the actor (not the character).
        'title_names':     ['Friends', 'Jennifer Aniston', 'Courteney Cox', 'Lisa Kudrow',
                            'Matt LeBlanc', 'Matthew Perry', 'David Schwimmer'],
    },

    # Whop: Clip Farm — "Love is Blind" (Netflix, season 11)
    # Brief: 15–60s, original sound, caption must say "Love Is Blind" + "Netflix"
    # and include #loveisblind, no mocking cast, no political/racist/NSFW,
    # no other shows or streaming apps, keep post up 30 days.
    'loveisblind': {
        'source':  'drive_files',
        'files': [
            ('1iyWC1osPc2s5caWqAIpuIbauZ9WSeK-w', 'LIB_S11E01'),
            ('1r9pvRnU8ELkX8ivXhePYZOKO7J_Ekxe5', 'LIB_S11E02'),
            ('1a1jcSfHtL3FoYq-9ScNuA5ghwreJOpav', 'LIB_S11E03'),
            ('1la2_uM50G4O_CXYzuhtCOSiuWEzu_BGc', 'LIB_S11E04'),
            ('1VmdlAtEu6Gq69EBdty8Cavh8v_Oi3XcU', 'LIB_S11E05'),
        ],
        # ~60 min episodes hold far more standalone moments than a 22 min sitcom.
        'max_runs':        8,
        'archive':         False,    # files belong to the brand's Drive
        # Reality-TV beats live in the pauses; only cut genuinely dead air.
        'silence_thresh':  1.0,
        # Brief allows 15–60s; retention falls off past ~30s, so cap well under the brief.
        'min_seconds':     16.0,
        'max_seconds':     40.0,
        'playlist':        'Love Is Blind',
        # Women 18-34: US evening + late night in bed (21:30 was the only 100% tier-1 slot).
        'publish_slots_et': ['19:00', '21:30'],
        'title_names':     ['Love Is Blind'],
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/382b0008-cecb-4b86-89a1-e5c8ef074ca7',
        'required_caption': ['Love Is Blind', 'Netflix', '#loveisblind'],
        'caption_footer':   'Watch Love Is Blind on Netflix.\n\n#LoveIsBlind #LoveIsBlindNetflix #Netflix',
        # Other streaming apps / shows must not be named; mocking the cast is grounds for rejection.
        'banned_patterns': [
            r'\bhulu\b', r'\bprime video\b', r'\bamazon\b', r'\bhbo\b',
            r'\bdisney\+?', r'\bpeacock\b', r'\bparamount\b', r'\bapple tv\b',
            r'\bfriends\b', r'\bbachelor(ette)?\b', r'\bvanderpump\b', r'\bsquid game\b',
            r'\bpsycho\b', r'\bcrazy\b', r'\binsane\b', r'\bidiot\b', r'\bstupid\b', r'\bdumb\b',
            r'\bugly\b', r'\bfat\b', r'\bloser\b', r'\btrash\b', r'\bclown\b', r'\bdelulu\b',
            r'\bsexy\b', r'\bsex\b', r'\bnsfw\b', r'\bnaked\b',
            r'\bdemocrat', r'\brepublican', r'\btrump\b', r'\bbiden\b', r'\bharris\b',
        ],
        # Cast rating people's looks reads as disrespectful in a clip, even when it's their own words.
        'transcript_banned': [
            r'\bugly\b', r'\bfat\b', r'\bretard', r'\bslut', r'\bwhore\b',
        ],
    },

    # Whop: Clip Farm — "Solved w/ Mark Manson" (podcast)
    # Brief: official Solved YouTube episodes only, newest first; 20–60s complete
    # thought; Mark on screen in the first 5s; no ad reads / sponsor segments /
    # other brands; no quotes edited out of context; full subtitles; tag
    # @solvedpodcast and @iammarkmanson on YouTube; keep post up 30 days.
    'markmanson': {
        # Downloaded locally (YouTube blocks CI runners) and attached to a
        # private GitHub release, which CI pulls with its own token.
        'source':       'release',
        'release_tag':  'footage-markmanson',
        'files': [
            # (youtube video id = release asset name, episode name) — newest first
            ('IcWpq_FPFHU', 'SOLVED_IcWpq_FPFHU'),   # Why Failure Hurts More Than It Should
            ('AawQ8YDRZv0', 'SOLVED_AawQ8YDRZv0'),   # The Failure Advice That's Ruining Your Life
            ('1A1c-QIh31Y', 'SOLVED_1A1c-QIh31Y'),   # Why Your Best Life Should Feel Irrational
            ('gZsE5nsbuao', 'SOLVED_gZsE5nsbuao'),   # How to Be Happy With Any Choice You Make
            ('e4EE71qghZ8', 'SOLVED_e4EE71qghZ8'),   # The Dopamine Trap That's Ruining Your Life
            ('15z1-j689jU', 'SOLVED_15z1-j689jU'),   # Why the Freest Generation Is So Anxious
        ],
        'max_runs':        6,
        'archive':         False,
        'silence_thresh':  0.6,
        # Wide table shots: faces too small to detect -> show the whole shot.
        'wide_fallback':   True,
        # Brief needs 20s+ (the render check allows 1s under min_seconds).
        'min_seconds':     21.0,
        'max_seconds':     40.0,
        'sponsorblock':    True,
        'playlist':        'Solved with Mark Manson',
        # Self-improvement: evening wind-down scroll (the 7 AM ET slot seeded India).
        'publish_slots_et': ['20:00'],
        'title_names':     ['Mark Manson'],
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/6bdc8e4b-3ebf-44f1-aedb-067589ca9c89',
        'required_caption': ['@solvedpodcast', '@iammarkmanson'],
        'caption_footer':   'From Solved with Mark Manson @solvedpodcast @iammarkmanson\n\n#MarkManson #SolvedPodcast #selfimprovement',
        # Ad reads inside a clip get it rejected; catch the ones SponsorBlock misses.
        'transcript_banned': [
            r'promo code', r'use code', r'brought to you by', r'sponsor', r'dot com slash',
            r'\.com\b', r'\bslash \w+', r'link in (the )?description', r'percent off',
            r'free trial', r'first month',
            # Brands that commonly buy podcast ad reads (Whisper writes URLs as "Shopify .com slash solved").
            r'\bshopify\b', r'\bbetterhelp\b', r'\bsquarespace\b', r'\bag1\b', r'athletic greens',
            r'\bnord ?vpn\b', r'\bexpress ?vpn\b', r'\baudible\b', r'\bmasterclass\b', r'\bhello ?fresh\b',
            r'\bmanscaped\b', r'\bfactor\b meals', r'\bcalm app\b', r'\bheadspace\b', r'\bzocdoc\b',
        ],
        'banned_patterns': [
            r'\bsponsor', r'promo code', r'\bclickbait\b',
            r'\bsex\b', r'\bsexy\b', r'\bnsfw\b',
            r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bdemocrat', r'\brepublican',
        ],
    },

    # Whop: Clip Farm — HardScope "Love & Justice" Ep 3 (Zach Justice relationship show)
    # Brief: 7s+, caption #LOVEANDJUSTICE, tag @HardScopeTV on YouTube, Ep 3 footage
    # only, never the full episode, no FaZe, no politics, nothing negative about
    # HardScope, no explicit content. ~70% of budget is on YouTube Shorts.
    'loveandjustice': {
        'source':       'release',
        'release_tag':  'footage-hardscope',
        'files': [
            ('lcc81gtNkfs', 'HS_LOVEJUSTICE_E03'),   # Zach Justice Exposes The Weirdest Relationship Of All Time
        ],
        'max_runs':        6,
        'archive':         False,
        'silence_thresh':  0.8,
        'wide_fallback':   True,
        # Brief needs 7s+; 15s keeps a complete beat.
        'min_seconds':     15.0,
        'max_seconds':     40.0,
        'playlist':        'Love & Justice',
        # Relationship drama: after-dinner scroll.
        'publish_slots_et': ['19:30'],
        'title_names':     ['Zach Justice', 'Love & Justice', 'Love and Justice', 'HardScope'],
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/3b59058d-81d1-489b-bf54-97375caa10ad',
        'required_caption': ['#LOVEANDJUSTICE', '@HardScopeTV'],
        'caption_footer':   'Full episode of Love & Justice on @HardScopeTV\n\n#LOVEANDJUSTICE #ZachJustice #HardScope',
        'banned_patterns': [
            r'\bfaze\b',
            r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bdemocrat', r'\brepublican', r'\belection\b',
            r'\bsex\b', r'\bsexy\b', r'\bnsfw\b', r'\bnaked\b',
            r'\bpsycho\b', r'\bcrazy\b', r'\binsane\b', r'\bidiot\b', r'\bstupid\b', r'\bdumb\b',
            r'\bugly\b', r'\bloser\b', r'\btrash\b', r'\bclown\b', r'\bscam\b', r'\bfake\b',
        ],
        'transcript_banned': [r'\bfaze\b'],
    },

    # Whop: Clip Farm — HardScope "Unpacked" (MatCrackz collectibles show), one
    # campaign per episode. Brief: 7s+, #unpacked in caption, tag @HardScopeTV,
    # that episode's footage only, never the full episode, no FaZe, no politics,
    # nothing negative about HardScope. ~70% of budget is on YouTube Shorts.
    'unpacked': {
        'source':       'release',
        'release_tag':  'footage-hardscope',
        'files': [
            ('DSmfWU_0Gf0', 'HS_UNPACKED_E01'),   # Steve Aoki Hunts For The World's Rarest Trading Cards
            ('h7XBn5JnZ3s', 'HS_UNPACKED_E02'),   # YourRAGE Spends $15,000 Opening Rare Trading Cards
        ],
        'max_runs':        5,
        'archive':         False,
        'silence_thresh':  0.8,
        'wide_fallback':   True,
        # Brief needs 7s+; 15s keeps a complete beat.
        'min_seconds':     15.0,
        'max_seconds':     40.0,
        'playlist':        'Unpacked',
        # Collectibles, younger male audience: late evening.
        'publish_slots_et': ['21:00'],
        'title_names':     ['Unpacked', 'MatCrackz', 'Steve Aoki', 'YourRAGE', 'HardScope'],
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/c85645f1-b75f-49e2-91d5-988cf9b3fadb',
        # Each episode is its own Whop campaign; submitting to the wrong one is a ban.
        'whop_campaigns': {
            'HS_UNPACKED_E01': 'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/c85645f1-b75f-49e2-91d5-988cf9b3fadb',
            'HS_UNPACKED_E02': 'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/58795e60-259f-4f9d-932c-87cf1aac7c8d',
        },
        'required_caption': ['#unpacked', '@HardScopeTV'],
        'caption_footer':   'Watch the full episode of Unpacked on @HardScopeTV\n\n#unpacked #MatCrackz #tradingcards',
        'banned_patterns': [
            r'\bfaze\b',
            r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bdemocrat', r'\brepublican', r'\belection\b',
            r'\bsex\b', r'\bsexy\b', r'\bnsfw\b',
            r'\bscam\b', r'\bfake\b', r'\brigged\b', r'\bidiot\b', r'\bstupid\b', r'\bloser\b',
        ],
        'transcript_banned': [r'\bfaze\b'],
    },

    # Whop: Clip Farm — YoungLA (gym apparel). Brief: only footage from the
    # YoungLA WORLD channel / listed videos, YoungLA clothing clearly visible,
    # must NOT feel like an ad — caption must not mention YoungLA, no tagging
    # accounts; funny organic caption; no NSFW/violence; YT/TT/IG.
    'youngla': {
        'source':       'release',
        'release_tag':  'footage-youngla',
        'files': [
            # Bright, face-on footage first; the zombie escape room is too dark to show the clothing.
            ('rDq33o8-Axk', 'YLA_rDq33o8-Axk'),   # iShowSpeed Vs. Bodybuilders (12 min)
            ('iQJfcQJ-ZtA', 'YLA_iQJfcQJ-ZtA'),   # Jay Cutler and Phil Heath Rank Our Athletes Physiques (13 min)
            ('RYC97psjwzA', 'YLA_RYC97psjwzA'),   # Bodybuilders Try Soccer (25 min)
            ('fupdRhE6RJQ', 'YLA_fupdRhE6RJQ'),   # Pretending To Be Millionaires In LA (14 min)
        ],
        'max_runs':        3,
        'archive':         False,
        'silence_thresh':  0.6,
        'wide_fallback':   True,
        'min_seconds':     16.0,
        'max_seconds':     40.0,
        'playlist':        'Gym Moments',
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/f40d3229-94a2-4886-b7cd-94fff1c5622f',
        # Gym crowd: post-gym evening scroll in the US.
        'publish_slots_et': ['18:30'],
        # Real names only, never the brand (the brief wants it to look organic).
        'title_names':     ['IShowSpeed', 'Jay Cutler', 'Phil Heath'],
        # No required caption text: the brief wants it to look organic, not branded.
        'banned_patterns': [
            r'young\s?la', r'@\w+', r'\bsponsor', r'\b#?ad\b', r'\bshop\b', r'\blink\b', r'\bdiscount\b',
            r'\bcode\b', r'\bmerch\b', r'\bsteroid', r'\bsex\b', r'\bnsfw\b',
        ],
        # Keep clips clean enough not to read as NSFW.
        'transcript_banned': [
            r'\bfuck', r'motherf', r'\bbitch', r'\bcunt\b', r'\bnigg', r'\bfag', r'\bretard',
        ],
    },
    # Runway/craft footage with no speech, so these are hand-cut by scripts/jacobco_edits.py and
    # uploaded directly; the profile only maps their episode names for whop_queue.
    # Submissions close Fri 2026-10-23 11:59 PM ET. No demographics ticket (stats come from Content Rewards).
    'jacobco': {
        'source':        'manual',
        'files': [
            ('', 'JACOBCO_runway'),
            ('', 'JACOBCO_reveal'),
            ('', 'JACOBCO_story'),
        ],
        'playlist':      'Fashion Moments',
        'whop_campaign': 'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/368755c4-c529-4d4a-97ae-2fcb242cdcfc',
    },
}


def in_publish_window(t):
    """True if the aware datetime t falls in the US-evening window (22:30-04:30 UTC)."""
    tt = t.astimezone(datetime.timezone.utc).time()
    return tt >= _WINDOW_UTC[0] or tt <= _WINDOW_UTC[1]


def _check_slots():
    """Every configured slot must land in the window in both EST and EDT."""
    for name, prof in CAMPAIGNS.items():
        for hhmm in prof.get('publish_slots_et', []):
            h, m = map(int, hhmm.split(':'))
            for month in (1, 7):
                t = datetime.datetime(2026, month, 15, h, m, tzinfo=_ET)
                assert in_publish_window(t), f"{name} slot {hhmm} ET is outside 22:30-04:30 UTC"


_check_slots()


def window_publish_time(iso):
    """Keep an ASAP publish time ('...Z') if it is in the window, else move it to the next window start."""
    t = datetime.datetime.strptime(iso, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    if in_publish_window(t):
        return iso
    start = t.replace(hour=_WINDOW_UTC[0].hour, minute=_WINDOW_UTC[0].minute, second=0, microsecond=0)
    return start.strftime('%Y-%m-%dT%H:%M:%SZ')


def next_publish_time(profile, now=None):
    """
    Next publishAt (UTC ISO 'Z') for the profile's niche slots, or None if the
    profile has none (publish right away). A run that finishes shortly after a
    slot publishes as soon as possible rather than waiting a whole day, but only
    while that is still inside the US-evening window; otherwise it takes the
    next slot.
    """
    slots = profile.get('publish_slots_et')
    if not slots:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    earliest = now + datetime.timedelta(minutes=_PROCESSING_MIN)
    today = now.astimezone(_ET).date()
    times = []
    for day in range(-1, 3):
        d = today + datetime.timedelta(days=day)
        for hhmm in slots:
            h, m = map(int, hhmm.split(':'))
            t = (datetime.datetime(d.year, d.month, d.day, h, m, tzinfo=_ET)
                 .astimezone(datetime.timezone.utc))
            if in_publish_window(t):
                times.append(t)
    if in_publish_window(earliest) and any(
            earliest - datetime.timedelta(minutes=_LATE_GRACE_MIN + _PROCESSING_MIN) < t < earliest
            for t in times):
        pick = earliest
    else:
        pick = min(t for t in times if t >= earliest)
    return pick.strftime('%Y-%m-%dT%H:%M:%SZ')


def get_campaign(name):
    name = (name or 'friends').strip().lower()
    if name not in CAMPAIGNS:
        raise ValueError(f"Unknown CAMPAIGN '{name}' — choose from: {', '.join(CAMPAIGNS)}")
    return name, CAMPAIGNS[name]


def find_banned(text, profile):
    """Return the banned terms found in text (case-insensitive)."""
    hits = []
    for pat in profile.get('banned_patterns', []):
        m = re.search(pat, text or '', flags=re.IGNORECASE)
        if m:
            hits.append(m.group(0))
    return hits


def clip_text(story, all_words):
    """Spoken text inside the story's clips."""
    return ' '.join(
        w['word'] for c in story['clips'] for w in all_words
        if c['start_seconds'] <= w['start'] < c['end_seconds']
    )


def find_transcript_banned(story, all_words, profile):
    text = clip_text(story, all_words)
    return [p for p in profile.get('transcript_banned', [])
            if re.search(p, text, flags=re.IGNORECASE)]


def enforce_caption(yt_meta, profile):
    """Append the brief's required caption text if the LLM left any of it out."""
    required = profile.get('required_caption')
    if not required:
        return yt_meta
    desc = yt_meta.get('description', '')
    missing = [r for r in required if r.lower() not in desc.lower()]
    if missing:
        yt_meta['description'] = desc.rstrip() + '\n\n' + profile['caption_footer']
    return yt_meta


# ---------------------------------------------------------------------------
# Packaging checks: titles, hashtags, spoilers
# ---------------------------------------------------------------------------

# Broad tags are dominated by South-Asian / SEA spam pools and pull the seed
# audience there; hashtag pages bring ~0.5% of views anyway.
BROAD_HASHTAGS = {
    'viral', 'viralshorts', 'viralvideo', 'trending', 'trend', 'foryou', 'foryoupage', 'fyp',
    'ytshorts', 'youtubeshorts', 'shorts', 'short', 'shortsfeed', 'shortsvideo', 'explore',
    'explorepage', 'reels', 'tiktok', 'funny', 'funnymoments', 'funnyvideos', 'comedy', 'lol',
    'meme', 'memes', 'entertainment', 'relationshipgoals', 'relationships', 'couples', 'dating',
    'love', 'realitytv', 'sitcom', '90stv', 'motivation', 'mindset', 'psychology', 'fitness',
}
_BROAD_TAGS = {'funny', 'funny moments', 'comedy', 'viral', 'trending', 'shorts', 'entertainment',
               'reality tv', 'dating', 'relationships', 'sitcom', '90s tv', 'tv shows', 'clips'}
MAX_HASHTAGS = 5
TITLE_MAX = 60
_BANNED_OPENERS = re.compile(r"^\W*(wait\b|nobody\b|no one\b|pov\b|when you\b|@)", re.IGNORECASE)
_EMOJI = re.compile('[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]')
_STOP = set("""about after again also always another around because been before being came come could
didn doesn don does done down even ever every from gets getting going gonna gotta have here into just
know like look made make many maybe more most much never only other over really right said same says
should some something still such sure take than that thats their them then there these they thing
think this those though through time very want wanna well were what when where which while will with
would yeah your youre okay""".split())


def norm_title(title):
    """Lower-case words only: emoji, punctuation and hashtags removed."""
    title = re.sub(r'#\w+', ' ', title or '')
    return ' '.join(re.findall(r"[a-z0-9']+", title.lower()))


def content_words(text, exclude=()):
    """Distinctive words (4+ letters, not stop-words or names in exclude) used to spot spoilers."""
    skip = {w for name in exclude for w in norm_title(name).split()}
    return {w for w in re.findall(r"[a-z']{4,}", (text or '').lower().replace("'", ''))
            if w not in _STOP and w not in skip}


def spoiler_hits(text, payoff_line, profile):
    """Payoff words that a banner or title repeats (2+ means it gives the punchline away)."""
    payoff = content_words(payoff_line, profile.get('title_names', []))
    return sorted(payoff & content_words(text))


def tidy_title(title):
    """Strip hashtags, keep at most 2 emoji (at the end), collapse spaces."""
    title = re.sub(r'\s*#\w+', '', title or '').strip()
    emoji = _EMOJI.findall(title)
    if len([e for e in emoji if e not in '️‍']) > 2:
        body = _EMOJI.sub('', title).strip()
        kept = [e for e in emoji if e not in '️‍'][:2]
        title = f"{body} {''.join(kept)}"
    return re.sub(r'\s{2,}', ' ', title).strip()


def title_problems(title, profile, prior_titles=(), payoff_line=''):
    """
    (hard, soft) problems with a title. Hard ones must be fixed before upload:
    "Wait…"-style openers, reuse of an earlier title (or its first 4 words) and
    spoiling the payoff. Soft ones are preferences the LLM should meet.
    """
    hard, soft = [], []
    norm = norm_title(title)
    if _BANNED_OPENERS.search(title or ''):
        hard.append('banned opener')
    if re.search(r'really just say that', norm):
        hard.append('templated "really just say that" title')
    priors = [norm_title(t) for t in prior_titles if t]
    if norm in priors:
        hard.append('exact title already used')
    else:
        head = norm.split()[:4]
        if len(head) == 4 and any(p.split()[:4] == head for p in priors[-50:]):
            hard.append('same opening words as a recent title')
    spoils = spoiler_hits(title, payoff_line, profile)
    if len(spoils) >= 2:
        hard.append(f'gives away the payoff {spoils}')
    if len(title) > TITLE_MAX:
        soft.append(f'{len(title)} chars > {TITLE_MAX}')
    names = profile.get('title_names') or []
    if names and not any(n.lower() in title[:35].lower() for n in names):
        soft.append('does not lead with the show or a real name')
    return hard, soft


def clean_hashtags(yt_meta, profile):
    """
    Keep 3-5 specific hashtags on one final description line: required ones
    first, broad ones (#viral, #funnymoments...) dropped. Also drops generic tags.
    """
    desc = yt_meta.get('description', '') or ''
    found = re.findall(r'#\w+', desc)
    required = [r for r in profile.get('required_caption', []) if r.startswith('#')]
    keep, seen = [], set()
    for tag in [t for t in found if t.lower() in {r.lower() for r in required}] + found:
        low = tag.lower()
        if low in seen or low[1:] in BROAD_HASHTAGS:
            continue
        seen.add(low)
        keep.append(tag)
    keep = keep[:MAX_HASHTAGS]
    body = re.sub(r'[ \t]*#\w+', '', desc)
    body = re.sub(r'[ \t]+\n', '\n', body)
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    yt_meta['description'] = f"{body}\n\n{' '.join(keep)}" if keep else body
    tags = [t.strip() for t in str(yt_meta.get('tags', '')).split(',') if t.strip()]
    yt_meta['tags'] = ', '.join(t for t in tags if t.lower() not in _BROAD_TAGS)
    return yt_meta
