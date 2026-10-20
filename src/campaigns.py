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
# mostly US) so they follow daylight saving. Picked per niche for habit-scroll
# moments: in bed in the morning, lunch, after school/work, late night in bed.
_ET = ZoneInfo('America/New_York')
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
        'min_seconds':     None,
        'max_seconds':     None,
        'playlist_map':    None,     # main.py's _SERIES_PLAYLIST
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
        # Brief allows 15–60s; keep a margin on both ends.
        'min_seconds':     16.0,
        'max_seconds':     58.0,
        'playlist':        'Love Is Blind',
        # Women 18-34: lunch-break scroll (UK 5:30 PM commute) + US late night in bed.
        'publish_slots_et': ['12:30', '21:30'],
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/382b0008-cecb-4b86-89a1-e5c8ef074ca7',
        'required_caption': ['Love Is Blind', 'Netflix', '#loveisblind'],
        'caption_footer':   'Watch Love Is Blind on Netflix.\n\n#loveisblind #LoveIsBlindNetflix #Netflix #realitytv #dating',
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
        'min_seconds':     20.0,
        'max_seconds':     58.0,
        'sponsorblock':    True,
        'playlist':        'Solved with Mark Manson',
        # Self-improvement: morning-in-bed / commute scroll (UK lunch).
        'publish_slots_et': ['07:00'],
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/6bdc8e4b-3ebf-44f1-aedb-067589ca9c89',
        'required_caption': ['@solvedpodcast', '@iammarkmanson'],
        'caption_footer':   'From Solved with Mark Manson @solvedpodcast @iammarkmanson\n\n#markmanson #solvedpodcast #selfimprovement #psychology #mindset',
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
        'min_seconds':     16.0,
        'max_seconds':     58.0,
        'playlist':        'Love & Justice',
        # Relationship drama: after-work scroll (UK 11 PM in-bed scroll).
        'publish_slots_et': ['18:00'],
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/3b59058d-81d1-489b-bf54-97375caa10ad',
        'required_caption': ['#LOVEANDJUSTICE', '@HardScopeTV'],
        'caption_footer':   'Full episode of Love & Justice on @HardScopeTV\n\n#LOVEANDJUSTICE #relationships #couples #dating',
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
        'min_seconds':     16.0,
        'max_seconds':     58.0,
        'playlist':        'Unpacked',
        # Collectibles, younger male audience: after school/work (UK 9 PM).
        'publish_slots_et': ['16:00'],
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/c85645f1-b75f-49e2-91d5-988cf9b3fadb',
        # Each episode is its own Whop campaign; submitting to the wrong one is a ban.
        'whop_campaigns': {
            'HS_UNPACKED_E01': 'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/c85645f1-b75f-49e2-91d5-988cf9b3fadb',
            'HS_UNPACKED_E02': 'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/58795e60-259f-4f9d-932c-87cf1aac7c8d',
        },
        'required_caption': ['#unpacked', '@HardScopeTV'],
        'caption_footer':   'Watch the full episode of Unpacked on @HardScopeTV\n\n#unpacked #tradingcards #collectibles #packopening',
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
        'max_seconds':     58.0,
        'playlist':        'Gym Moments',
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/f40d3229-94a2-4886-b7cd-94fff1c5622f',
        # Gym crowd: after-work gym / pre-bed scroll in the US, UK evening.
        'publish_slots_et': ['14:00'],
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


def next_publish_time(profile, now=None):
    """
    Next publishAt (UTC ISO 'Z') for the profile's niche slots, or None if the
    profile has none (publish right away). A run that finishes shortly after a
    slot publishes as soon as possible rather than waiting a whole day.
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
            times.append(datetime.datetime(d.year, d.month, d.day, h, m, tzinfo=_ET)
                         .astimezone(datetime.timezone.utc))
    if any(earliest - datetime.timedelta(minutes=_LATE_GRACE_MIN + _PROCESSING_MIN) < t < earliest
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
