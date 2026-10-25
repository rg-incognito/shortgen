"""
Campaign profiles — one pipeline, many sources.

`friends` is the original sitcom channel behaviour. Every other profile is a
Whop Content Rewards campaign: footage comes from the brand's own Drive files,
and the brief's hard rules (clip length, required caption text, banned topics)
are enforced in code rather than trusted to the LLM.
"""
import datetime
import itertools
import random
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

# ---------------------------------------------------------------------------
# Render config (contract with the render side): niche A = sitcom / reality /
# drama, niche B = podcast / late-night / creator talk. Whop briefs decide what
# a campaign may add, so the defaults are conservative: no added overlay text,
# no handle, no music, no SFX. Only a profile that sets a key turns it on.
# ---------------------------------------------------------------------------
_RENDER_DEFAULTS = {
    'A': {'niche': 'A', 'template': 'A1', 'caption_preset': 'tv', 'header': True,
          'overlays_allowed': False, 'handle': None, 'tail_s': 1.0},
    'B': {'niche': 'B', 'template': 'B1', 'caption_preset': 'talk', 'header': False,
          'overlays_allowed': False, 'handle': None, 'tail_s': 0.5},
}


def _render(niche, **overrides):
    r = dict(_RENDER_DEFAULTS[niche])
    r.update({'allow_music': False, 'music_mode': 'off',
              'allow_sfx': False, 'sfx': {'payoff_boom': False, 'time_jump_flash': False},
              'max_duration': 60})
    r.update(overrides)
    return r


# Length by niche: 16-25 s had the best viral rate (87%), and our own retention
# curves (Oct 24) show stalled 26-50 s Shorts lose 25-40 points between 5 and 8 s
# while our 15-17 s hits loop, so the target is 15-22 s with a 28 s cap. B allows up
# to LONG_ARC_MAX only for one escalating story whose payoff sits in the last 30%.
_FRIENDS_CAST = ['Ross', 'Rachel', 'Monica', 'Chandler', 'Joey', 'Phoebe', 'Gunther', 'Janice',
                 'Emily', 'Carol', 'Susan', 'Mike', 'Richard', 'Ursula', 'Frank']

NICHE_LENGTH = {'A': {'target': (15, 22), 'max': 28}, 'B': {'target': (15, 22), 'max': 28}}
LONG_ARC_MAX = 35.0
LONG_ARC_PAYOFF_POS = 0.70

# --- milkXP (MilkPEP, a regulated milk brand) ---------------------------------
# Creators in Episode 3 (from a local Whisper transcript, wm_build/cache/milkxp_ep3.mp4.txt): creator
# Ben Azelart (Whisper: "Azalart") teams up with teacher Mr. Greg (the show's score card spells "Greg",
# Whisper also "Gregg") to build a secret student lounge on a $5,000 budget (the 50/30/20 rule).
# Single words for the caption highlight and the echo checks, full names for titles.
_MILKXP_CREATORS = ['Ben Azelart', 'Mr. Greg']
_MILKXP_CAST = ['Ben', 'Azelart', 'Greg', 'Gregg']

# Other drinks (the brief: no oat/almond/soy/coconut/rice/nut milk, raw milk, juice, sports
# drinks, AG1, Muscle Milk) and drink or milk brands (never named, never shown).
_OTHER_DRINKS = [
    r'\b(oat|almond|soy|coconut|rice|cashew|nut|hemp|pea|plant[- ]based|raw)[- ]?milks?\b',
    r'\bjuices?\b', r'\bsports? drinks?\b', r'\benergy drinks?\b', r'\bsodas?\b', r'\bprotein shakes?\b',
    r'\bsmoothies?\b', r'\bcoffee\b', r'\blattes?\b', r'\bbeers?\b', r'\bwine\b', r'\balcohol', r'\bcocktails?\b',
    r'\bvodka\b', r'\bwhiske?y\b', r'\btequila\b',
]
_DRINK_BRANDS = [
    r'\bag1\b', r'athletic greens', r'muscle ?milk', r'\bgatorade\b', r'\bpowerade\b', r'\bprime (hydration|energy|drink)',
    r'\bred ?bull\b', r'\bmonster energy\b', r'\bcelsius\b', r'\bcoca[- ]?cola\b', r'\bcoke\b', r'\bpepsi\b',
    r'\bsprite\b', r'\bfanta\b', r'\bdr\.? ?pepper\b', r'\bmountain dew\b', r'\bstarbucks\b', r'\bdunkin',
    r'\bliquid death\b', r'\bbody ?armou?r\b', r'\bfairlife\b', r'\bhorizon organic\b', r'\borganic valley\b',
    r'\blactaid\b', r'\bnesquik\b', r'\bcore power\b', r'\boatly\b', r'\bchobani\b', r'\bdarigold\b',
    r'\btillamook\b', r'\bkemps\b', r'\bborden\b', r'\ba2 milk\b', r'\bnestl[eé]\b', r'\bdanone\b', r'\byoo-?hoo\b',
]
_CLAIM_WORDS = r'(best|perfect|perfectly|ideal|greatest|healthy|healthier|healthiest)'
_MILK_NEGATIVE = r'(gross|disgusting|nasty|hate|hated|sour|spoiled|expired|chunky|curdled|throw(ing)? up|puke|vomit)'
_SWEARING = [r'\bf+u+c+k', r'motherf', r'\bsh[i*]t', r'\bbitch', r'\bcunt\b', r'\bdamn', r'\bhell\b',
             r'\bass(hole)?\b', r'\bcrap\b', r'\bdick\b', r'\bpiss', r'\bbastard', r'\bwtf\b', r'\bnigg', r'\bfag',
             r'\bretard',
             # Whisper writes a bleeped swear as "f***" / "s***": still a swear for this brand
             r'\w\*{2,}']
# Caption, title, tags, header: no exaggerated claim word anywhere (milk is the topic of every
# caption), nothing ad-like, no other drink or brand, no adult words, no negativity or sarcasm.
_MILK_CAPTION_BANS = [
    rf'\b{_CLAIM_WORDS}\b', r'\bgoat\b', r'\bsuperfood', r'\bnutritious\b', r'\bnutrients?\b', r'\bnourish',
    r'\bvitamins?\b', r'\bcalcium\b', r'\bprotein\b', r'\bmiracle',
    r'\bcures?\b', r'\bboosts?\b', r'\bstrong(er)? bones\b',
    r'\bbuy\b', r'\bcheck (it |this |them )?out\b', r'\bshop\b', r'\bsponsor', r'#ad\b', r'\bpaid\b', r'\bpromo',
    r'\bdiscount', r'\bcoupon', r'\buse code\b', r'link in', r'\bsign up\b', r'\bget yours\b',
    r'\bavailable (at|in|now)\b', r'\border (now|today)\b', r'\bgrab (a|one|yours)\b', r'\bstores?\b',
    *_OTHER_DRINKS, r'\btea\b', *_DRINK_BRANDS,
    *_SWEARING, r'\baf\b', r'\bsex', r'\bnsfw\b', r'\bnaked\b',
    r'\bgross\b', r'\bdisgusting\b', r'\bnasty\b', r'\bhate', r'\bworst\b', r'\bsucks?\b', r'\bcringe',
    r'\bstupid\b', r'\bidiot', r'\bdumb\b', r'\bloser', r'\btrash\b', r'\bclown', r'\bterrible\b', r'\bawful\b',
    r'\byuck\b', r'\bew+\b', r'\blactose\b', r'\bdairy[- ]free\b', r'\bspoiled\b', r'\bexpired\b', r'\bsour\b',
    r'\bfake\b', r'\bscam\b', r'\bsarcas', r'\byeah right\b', r'\bclickbait\b',
    r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bdemocrat', r'\brepublican',
]
# Clip audio: a creator naming another drink or brand, a claim or a complaint about milk, or
# swearing rules the moment out.
_MILK_AUDIO_BANS = [
    *_OTHER_DRINKS, *_DRINK_BRANDS, *_SWEARING,
    rf'\bmilk\w*\W+(?:\w+\W+){{0,4}}?{_CLAIM_WORDS}\b', rf'\b{_CLAIM_WORDS}\W+(?:\w+\W+){{0,4}}?milk',
    rf'\bmilk\w*\W+(?:\w+\W+){{0,4}}?{_MILK_NEGATIVE}\b', rf'\b{_MILK_NEGATIVE}\W+(?:\w+\W+){{0,4}}?milk',
    r'\blactose\b', r'\bdairy[- ]free\b', r'\bsex', r'\bnaked\b',
    # The episode's scripted sponsor read (213-245 s: "a quick break from our sponsor", "brought to
    # you by", "13 essential nutrients"): an ad with nutrition claims, not a creator's own words.
    r'\bsponsor', r'brought to you by', r'\bnutrients?\b', r'\bessential nutrient', r'\bvitamins?\b',
]

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
        # Characters: not counted as content words when checking headers and overlays for echoes.
        'cast_names':      _FRIENDS_CAST,
        'genre':           'sitcom',
        # Our own channel: the originality layer and the handle are ours to add.
        # names: caption words that get the highlight colour.
        'render':          _render('A', overlays_allowed=True, handle='@theonewhereclips', names=_FRIENDS_CAST),
        # Opening credits are burned over the first scene after the theme: a cheap prior
        # next to the burned-text detector (episode seconds 0-avoid_first_s are avoided).
        'avoid_first_s':   240,
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
        'genre':           'reality',
        # Brief: original sound (music stays off). Added overlay text and a handle are not
        # confirmed as allowed, so only today's header is shown.
        'render':          _render('A'),
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/382b0008-cecb-4b86-89a1-e5c8ef074ca7',
        'required_caption': ['Love Is Blind', 'Netflix', '#loveisblind'],
        'caption_footer':   'Watch Love Is Blind on Netflix.\n\n#loveisblind #LoveIsBlindNetflix #Netflix',
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
        'genre':           'podcast',
        # No on-screen text and ST openers (1 point): 8/12 reachable is the bar, not 11/16.
        'min_rubric':      8,
        # Brief: full subtitles, Mark on screen in 5 s. Full-bleed, no added text.
        'render':          _render('B', caption_preset='full_subs', names=['Mark', 'Manson']),
        # Big life questions are this show's topic, not a vague tease.
        'vague_exempt':    ['life'],
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
        'genre':           'reality',
        'render':          _render('A', names=['Zach']),
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
            # Newest episode first: its Whop campaign has the fresh budget.
            ('unpacked_e03', 'HS_UNPACKED_E03'),  # David Dobrik (brand Drive file, re-encoded to 720p)
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
        'title_names':     ['Unpacked', 'MatCrackz', 'Steve Aoki', 'YourRAGE', 'David Dobrik', 'HardScope'],
        'genre':           'talk',
        'min_rubric':      8,       # no on-screen text: 8 of the 12 reachable points
        'render':          _render('B', names=['MatCrackz', 'Aoki', 'YourRAGE', 'Dobrik']),
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/c85645f1-b75f-49e2-91d5-988cf9b3fadb',
        # Each episode is its own Whop campaign; submitting to the wrong one is a ban.
        'whop_campaigns': {
            'HS_UNPACKED_E01': 'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/c85645f1-b75f-49e2-91d5-988cf9b3fadb',
            'HS_UNPACKED_E02': 'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/58795e60-259f-4f9d-932c-87cf1aac7c8d',
            'HS_UNPACKED_E03': 'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/db7f38ed-20ee-40aa-aa7c-658592ddcbf1',
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
        'genre':           'talk',
        'min_rubric':      8,       # no on-screen text: 8 of the 12 reachable points
        # Brief: must look organic: no @, no brand, no added text beyond captions.
        'render':          _render('B', names=['IShowSpeed', 'Speed', 'Cutler', 'Heath']),
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

    # Whop: Clip Farm — Ramp "Tony Stock: Pro Trader" FINALE (Tony Hawk + Bam Margera
    # comedy, set in 2006). Brief: finale footage only (Drive folder
    # 1dcgzFmgxP1cqN-I9hFAk912rHxBBw9wP); the clip must tell a Tony and Bam story, start
    # at the start of a line and end after the laugh; hook and caption match the clip.
    # Caption: short, fun, must NOT read like an ad ("sign up", "check out Ramp"), MUST tag
    # @tryramp on YouTube; no clickbait, fake claims or stock tips; no NSFW. Keep 30 days.
    'tonyhawk': {
        'source':  'drive_files',
        'files': [
            # The 16:9-ish (1440x1080) finale episode first: it is the story source the brief wants.
            ('1J8UxY4Qsc_U6szlpQhcxln9ghiLkuFsN', 'TONYSTOCK_EP4_FINALE'),
            # The 4K vertical recap cuts (Drive 1mUf4x..., 1HoeF9..., 1TKvwx...) are left out: they
            # are a rap song over montage footage with almost no speech, so no clip can be cut on
            # whole sentences and every run on them would be a no-candidate skip.
        ],
        # The finale runs under 2 minutes (~2-4 clips); two early runs were rule-failure skips.
        'max_runs':        5,
        'archive':         False,    # files belong to the brand's Drive
        'silence_thresh':  0.45,     # scripted comedy: keep the timing, cut only dead air
        # The brief sets no minimum and the finale is 117 s of quick bits: a complete
        # 10-12 s bit is a clip (the first run rejected every 11.5-12.6 s candidate).
        'min_seconds':     9.5,
        'target_seconds':  (12, 22),
        'max_seconds':     35.0,
        'playlist':        'Tony Stock: Pro Trader',
        # Skate / comedy crowd: late-evening scroll.
        'publish_slots_et': ['22:00'],
        'title_names':     ['Tony Hawk', 'Bam Margera', 'Tony Stock'],
        'cast_names':      ['Tony', 'Bam', 'Hawk', 'Margera'],
        'genre':           'sitcom',
        # Brief allows our own framing as long as it tells the Tony and Bam story and nothing
        # is promotional: header (whole clip) + closing line only, no context card, no handle.
        'render':          _render('A', overlays_allowed=True, names=['Tony', 'Hawk', 'Bam', 'Margera']),
        'overlay_fields':  ['closing_line'],
        # The header carries the framing; a closing line is welcome but not required.
        'layers_optional': True,
        # Only the punchline's own words spoil it: the bit's topic (trading, the desk, the
        # chaos) is the situation the header has to name. Topic words are not echoes either.
        'spoiler_scope':   'punchline',
        'topic_words':     ['trading', 'trade', 'trades', 'trader', 'traders', 'stock', 'stocks',
                            'market', 'markets', 'desk', 'floor', 'chaos', 'money', 'finance', 'wall', 'street',
                            'skate', 'skating', 'skateboard', 'board', 'finale', 'livestream', 'pro'],
        # A situation header shares words with a scripted bit; only a near-transcription is an echo.
        'header_echo_share': 0.6,
        # A native 9:16 source (the recap cuts) goes full-bleed: the A1 4:5 window would
        # crop away a third of an already-vertical frame.
        'vertical_template': 'A2',
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/ce9f2373-a374-491e-8a51-3684d030152a',
        'required_caption': ['@tryramp'],
        'caption_footer':   'Tony Hawk and Bam Margera in Tony Stock: Pro Trader @tryramp\n\n#TonyHawk #BamMargera #TonyStock',
        # Must not read like an ad, no stock tips or fake claims, no NSFW (captions, header, overlays).
        'banned_patterns': [
            r'\bsign(ing|ed)? ?up\b', r'check (it )?out ramp', r'\bramp (is|was|makes|helps|lets|gives|saves|has)\b',
            r'\btry ramp\b', r'\bdownload\b', r'\blink in\b', r'\bpromo\b', r'\bdiscount\b', r'\buse code\b',
            r'\bfree trial\b', r'\bsponsor', r'#ad\b', r'\bpaid partnership\b', r'\bcorporate cards?\b',
            r'\b(expense|spend) management\b', r'\bsave (time|money)\b',
            r'\bstock tips?\b', r'\bfinancial advice\b', r'\binvest(ing|ment)? (in|advice|tip)', r'\bbuy (the|this|these) stocks?\b',
            r'\bto the moon\b', r'\bguarantee', r'\bget rich\b', r'\bprice target\b', r'\bclickbait\b',
            r'\bsex\b', r'\bsexy\b', r'\bnsfw\b', r'\bnaked\b',
            r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bdemocrat', r'\brepublican',
        ],
        # No NSFW: hard swearing or slurs in the audio rule the moment out.
        'transcript_banned': [
            r'\bfuck', r'motherf', r'\bcunt\b', r'\bnigg', r'\bfag', r'\bretard',
        ],
    },

    # Whop: Runback.TV — BILF Podcast (Boss I'd Like to Follow, host Mara Dorne).
    # Brief (footage/bilf_guidelines.md): approved episodes only; 15-60 s (aim 25-45),
    # 9:16, burned-in subtitles, "BILF Podcast" typed on screen for the WHOLE clip and a
    # text hook in the first 2 s; caption tags @bilfpod + #bilfpodpartner and names the
    # guest or episode; nothing ad-like; keep it positive (no mocking, no out-of-context
    # cuts, quotation marks only for verbatim words); never clip sponsor reads; paid-
    # promotion disclosure on. Audience: career millennials / founders, real talk about
    # money, work and building something. 40%+ T1, engagement >= 1.0%.
    'bilf': {
        'source':       'release',
        'release_tag':  'footage-bilf',
        'files': [
            # The guidelines' "under-clipped" episodes (approved, less competition).
            ('V6sjFiYkJvM', 'BILF_V6sjFiYkJvM'),   # Suneera Madhani (Stax founder)
            ('DIxdW4xQH_w', 'BILF_DIxdW4xQH_w'),   # Alli Webb (Drybar founder)
            ('WEyv58H3cxk', 'BILF_WEyv58H3cxk'),   # Andrea Carmona (Love Island USA)
        ],
        'max_runs':        8,
        'archive':         False,
        'silence_thresh':  0.6,
        'wide_fallback':   True,
        # Brief 15-60 s, aim 25-45: 20 s keeps a complete moment. Niche B caps a normal clip at
        # 35 s and lets one escalating single-speaker story run to 60 s.
        'min_seconds':     15.0,
        'max_seconds':     40.0,
        'sponsorblock':    True,
        'playlist':        'BILF Podcast Clips',
        # Career millennials with young kids: after bedtime, US evening.
        'publish_slots_et': ['20:30'],
        'title_names':     ['BILF Podcast', 'Mara Dorne', 'Suneera Madhani', 'Alli Webb', 'Andrea Carmona'],
        'cast_names':      ['Mara', 'Dorne', 'Suneera', 'Madhani', 'Alli', 'Webb', 'Andrea', 'Carmona',
                            'Stax', 'Drybar', 'BILF'],
        'genre':           'podcast',
        # On-screen text is required here (context card = the 2 s text hook, closing line),
        # so framing/originality are reachable; ST openers score 1: 10 of 16.
        'min_rubric':      10,
        # "BILF Podcast" for the whole clip: the persistent handle layer is the only text the
        # render keeps on screen start to end on B1 (the header band exists on A1 only).
        # handle_required keeps it through the QA safe re-encode; handle_size makes it legible.
        'render':          _render('B', overlays_allowed=True, handle='BILF Podcast', handle_required=True,
                                   context_required=True,
                                   handle_size=44, names=['Mara', 'Suneera', 'Alli', 'Andrea', 'BILF']),
        # The text hook in the first 2 s is the context card.
        'required_layers': ['context_line'],
        # Quotation marks only around words actually said in the clip.
        'verbatim_quotes': True,
        'vague_exempt':    ['life'],
        # YouTube "includes paid promotion" (the brief's paid-partnership toggle).
        'paid_promotion':  True,
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/088c2279-ab1d-4434-9ba0-6637ab9311fa',
        'required_caption': ['@bilfpod', '#bilfpodpartner'],
        # {guest} is filled per episode from episode_notes (the caption must name the guest).
        'caption_footer':   '{guest} on the BILF Podcast. Full episode on the BILF Podcast YouTube channel @bilfpod\n\n#bilfpodpartner #BILFPodcast',
        # Guest names (required in the caption) and the guidelines' under-clipped moments
        # (episode seconds), passed to the analyzer as priority hints.
        'episode_notes': {
            'BILF_V6sjFiYkJvM': {
                'guest': 'Suneera Madhani', 'about': 'founder of Stax (sold it, then started over in 6 months)',
                'moments': [
                    (0, 40, 'cold open: rejected by every bank and every processor, "pale, male and stale"'),
                    (258, 278, 'sold it, six months later building another one; "you don\'t work enough"'),
                    (787, 804, 'her secret superpower: her mother lives across the street'),
                    (1073, 1122, 'pitched Stax to her bosses, laughed out of the room, quit and built it'),
                    (1131, 1161, 'zero to $40B processed, 400 employees, Series A/B/C, PE exit'),
                    (1299, 1352, '"Can women have it all?" -> "100%, whatever all looks like to her" -> the three-bucket rule'),
                    (1741, 1754, 'less than 2% of female founders ever break a million in revenue'),
                    (1788, 1805, 'nothing bad happens when women make more money'),
                    (2019, 2042, "Mara's 24-hour rule for decisions"),
                    (2520, None, '"I\'m going to win the championship"'),
                ],
            },
            'BILF_DIxdW4xQH_w': {
                'guest': 'Alli Webb', 'about': 'founder of Drybar ($255M exit)',
                'moments': [
                    (0, None, 'door to door with a blow dryer, then a $255M exit'),
                    (210, None, 'rehab, divorce and a death while Drybar was thriving'),
                    (435, None, 'the branding trick: do the exact opposite'),
                    (660, None, '"we shouldn\'t have gotten married, truly"'),
                    (960, None, '"work-life balance is bullsh*t"'),
                    (1140, None, 'can you actually have it all?'),
                    (1440, None, 'bawling in a bathroom at Drybar'),
                    (1650, None, 'her kids: "we don\'t know what version of you we\'re gonna get"'),
                    (1815, None, '"petty is a choice"'),
                    (2020, None, "the facelift, and Mara's tummy tuck"),
                    (2445, None, 'two women vs TikTok'),
                    (2640, None, '"always be filming"'),
                ],
            },
            'BILF_WEyv58H3cxk': {
                'guest': 'Andrea Carmona', 'about': 'Love Island USA, painted as the villain, then loved',
                'moments': [
                    (20, 72, 'cold open: bullied by kids, "you can go into a hole or..."'),
                    (284, 300, 'how editing painted her as the villain'),
                    (566, 620, 'the hate was all kids; her first call off the show'),
                    (800, None, 'dumped six weeks after'),
                    (861, 992, 'back for Love Island Games, the breakup'),
                    (1140, 1222, 'no phone for six weeks; people cheated with the hotel room phone'),
                    (1466, 1474, '"bad publicity is still publicity" and crashing out online'),
                    (2044, 2083, 'landing in Miami: stopped everywhere, and it was all love'),
                ],
            },
        },
        # Sponsor reads and ad breaks are an instant rejection; catch what SponsorBlock misses.
        'transcript_banned': [
            r'sponsored by', r'today\'?s sponsor', r'our sponsors?\b', r'word from our', r'brought to you by',
            r'this episode is (brought|sponsored|presented)', r'promo code', r'use code', r'\bcode \w+ (at|for)\b',
            r'link in (the )?(bio|description|show notes)', r'percent off', r'\d+ ?% off', r'free trial',
            r'dot com slash', r'\.com ?/ ?\w+', r'\bsign up (at|today|now)\b',
            r'\bshopify\b', r'\bbetterhelp\b', r'\bsquarespace\b', r'\bag1\b', r'athletic greens',
            r'\bnord ?vpn\b', r'\bexpress ?vpn\b', r'\baudible\b', r'\bmasterclass\b', r'\bhello ?fresh\b',
        ],
        # Caption / header / overlays: keep it positive and organic, never "official", nothing ad-like.
        'banned_patterns': [
            r'\bofficial\b', r'\bsponsor', r'promo code', r'\buse code\b', r'#ad\b', r'\bdiscount\b',
            r'\bpsycho\b', r'\bcrazy\b', r'\binsane\b', r'\bidiot\b', r'\bstupid\b', r'\bdumb\b',
            r'\bugly\b', r'\bfat\b', r'\bloser\b', r'\btrash\b', r'\bclown\b', r'\bdelulu\b', r'\bcringe\b',
            r'\bf+u+c+k', r'sh[i*]t\b', r'\bsex\b', r'\bsexy\b', r'\bnsfw\b', r'\bnaked\b',
            r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bdemocrat', r'\brepublican', r'\bclickbait\b',
        ],
    },

    # Whop: ClipUp Official — LADbible Entertainment: "Clip LADbible's episode with John Cena &
    # Jessica Biel". The brief ("all in Notion") was never linked, so these are its stated
    # defaults: that episode only; the LADbible watermark visible and uncropped on every clip;
    # tag @ladbible in the caption; burned-in captions; 9:16; 15-60 s; no NSFW; the caption must
    # match the clip and must not mislead. $1/1K views, max $300 per clip.
    'ladbible': {
        'source':       'release',
        'release_tag':  'footage-ladbible',
        'files': [
            # YouTube -jNqG2Dx1t8 (LADbible Entertainment, 2026-10-16, 11 min): "John Cena & Jessica
            # Biel Go Head to Head in the Ultimate Snack Wars: Showdown". The release asset drops the
            # leading '-' of the id (a leading dash reads as a flag to gh).
            ('ladbible_jNqG2Dx1t8', 'LADBIBLE_CENA_BIEL_SNACKWARS'),
        ],
        'max_runs':        5,
        'archive':         False,
        'silence_thresh':  0.6,
        # Two people side by side at a table: show the whole shot when faces are small.
        'wide_fallback':   True,
        # Brief 15-60 s; our retention falls off past ~30 s (niche B caps a normal clip at 35 s).
        'min_seconds':     15.0,
        'target_seconds':  (15, 22),
        'max_seconds':     35.0,
        'playlist':        'John Cena & Jessica Biel',
        # Celebrity entertainment, US late evening (free slot: 22:00 is tonyhawk); 23:15 is a
        # second daily Short (the slot the stopped Friends posts used to take).
        'publish_slots_et': ['22:30', '23:15'],
        'title_names':     ['John Cena', 'Jessica Biel', 'LADbible'],
        'cast_names':      ['John', 'Cena', 'Jessica', 'Biel', 'LADbible'],
        # A celebrity sit-down / game with two talkers: the talk niche (B1 full-bleed, talk captions).
        'genre':           'talk',
        # No added on-screen text beyond captions and the required watermark: 8 of 12 reachable.
        'min_rubric':      8,
        'render':          _render('B', names=['John', 'Cena', 'Jessica', 'Biel'],
                                   # Required on every frame, clear and uncropped: top of the frame,
                                   # above the faces, away from captions and the Shorts UI.
                                   watermark={'file': 'assets/watermarks/ladbible.png', 'width_frac': 0.5,
                                              'pos': 'top'}),
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/187d5d23-22c6-4dfb-9e32-cf296a3fb0b7',
        # Paid brand content: tick YouTube's "includes paid promotion" (user decision 2026-10-24).
        'paid_promotion':  True,
        'required_caption': ['@ladbible'],
        'caption_footer':   'John Cena and Jessica Biel on @ladbible\n\n#JohnCena #JessicaBiel #LADbible',
        # Not misleading, not an ad, nothing NSFW, never mock the guests.
        'banned_patterns': [
            r'\bsponsor', r'promo code', r'\buse code\b', r'#ad\b', r'\bdiscount\b', r'link in (the )?bio',
            r'\bbuy (this|it|now|yours|tickets)\b', r'\bshop now\b', r'\bsign up\b', r'\bfree trial\b',
            r'\bgiveaway\b', r'\bin (cinemas|theaters|theatres) now\b', r'\bstream(ing)? (it )?now\b',
            r'\bleak(ed|s)?\b', r'\bexclusive\b', r'\bclickbait\b', r'\bofficial\b',
            r'\bpsycho\b', r'\bidiot\b', r'\bstupid\b', r'\bdumb\b', r'\bugly\b', r'\bfat\b', r'\bloser\b',
            r'\btrash\b', r'\bclown\b', r'\bcringe\b', r'\bdivorc', r'\bcheat(ing|ed)?\b',
            r'\bf+u+c+k', r'sh[i*]t\b', r'\bsex\b', r'\bsexy\b', r'\bnsfw\b', r'\bnaked\b',
            r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bdemocrat', r'\brepublican',
        ],
        # No NSFW: swearing, sex talk or slurs in the audio rule the moment out (the episode has a
        # "that is me having sex" sound guess and a couple of unbleeped "shit"s). LADbible bleeps
        # its f-words itself (Whisper writes "f***"); those bleeped lines are allowed.
        'transcript_banned': [
            r'\bfuck', r'motherf', r'\bsh[i*]t\b', r'\bbitch', r'\bcunt\b', r'\bsex\b', r'\bnigg', r'\bfag', r'\bretard',
        ],
    },

    # Whop: Clip Farm — milkXP Episode 3 (MilkPEP's creator challenge show: big creators take on
    # fun challenges). Brief (Google doc, read in full): Episode 3 footage only; the milkXP
    # watermark kept clear and never cropped; clip BOTH funny moments (laugh, fail, shocked face,
    # best play, win, close call) AND "milk moments" (a creator talks about milk in their own
    # words); caption in our own words, short, fun, matching the clip, tagging @milkxpofficial,
    # never an ad ("buy this", "check out"). Milk is a regulated brand: no other drinks or drink
    # brands, no exaggerated claims about milk (never best / perfect / ideal / greatest /
    # healthy; "Milk can help you focus / lock in" is fine), only positive about milk, milkXP,
    # the creators, teachers and students, no swearing or adult words, no milk brand name, label
    # or logo, no sarcasm or negativity. Never comment on milkXP's long-form video, so the
    # first-comment feature is hard-off here. $1.25/1K, 40%+ tier 1 with a primary US audience.
    'milkxp': {
        'source':       'release',
        'release_tag':  'footage-milkxp',
        'files': [
            # WeTransfer "MilkPEP_milkXP_Episode3_YT_16x9.mp4" (9:38, 1080p), re-encoded to 720p H.264.
            ('milkxp_ep3', 'MILKXP_EP3'),
        ],
        'max_runs':        6,
        'archive':         False,
        'silence_thresh':  0.8,
        'wide_fallback':   True,
        # Brief ~15-35 s. Milk moments are short asides (the first local run's were 7-13 s), so a
        # complete 14 s beat counts (the render check allows 1 s under).
        'min_seconds':     14.0,
        'target_seconds':  (15, 22),
        'max_seconds':     35.0,     # niche A caps a clip at 32 s
        # The header names the challenge (the lounge, the budget, getting caught): only the
        # punchline's own words spoil it, the show's topic words are not echoes or spoilers.
        'spoiler_scope':   'punchline',
        'topic_words':     ['secret', 'student', 'students', 'lounge', 'room', 'school', 'teacher', 'lesson',
                            'budget', 'budgeting', 'needs', 'need', 'wants', 'want', 'savings', 'build', 'building',
                            'caught', 'milk', 'challenge', 'grade', 'grades', 'class', 'cube', 'couch', 'hoop'],
        'header_echo_share': 0.6,
        'playlist':        'milkXP',
        # Students and young adults, US after-dinner scroll (free slot between 19:30 and 20:00);
        # 18:45 is a second daily Short (the earliest slot inside the window in both EST and EDT is 18:30).
        'publish_slots_et': ['18:45', '19:45'],
        'title_names':     ['milkXP'] + _MILKXP_CREATORS,
        'cast_names':      list(_MILKXP_CAST),
        # A creator challenge show: reality-like entertainment (niche A, A1 with a header band).
        'genre':           'reality',
        # Brief: on-screen text is optional (hook, point to the full show, build suspense): the A1
        # header only, no context card / closing line / annotations. The watermark sits under the
        # header band at the top of the video window, away from captions and the Shorts UI.
        # The episode is full of the show's own graphics (name labels, lesson cards, score cards):
        # they are the brand's content, so burned-in text only warns (burned_text_ok).
        'render':          _render('A', names=list(_MILKXP_CAST), burned_text_ok=True,
                                   watermark={'file': 'assets/watermarks/milkxp.png', 'width_frac': 0.62,
                                              'pos': 'top'}),
        # The brief asks for two kinds of clips: runs alternate which one is wanted (the analyzer
        # is asked for both; the run's type wins when one of those passes every rule).
        'clip_types': {
            'funny':       ('a FUNNY MOMENT: a laugh, a fail, a shocked face, a big play, a win or a '
                            'close call in a challenge'),
            'milk_moment': ('a MILK MOMENT: a creator talks about milk in their own words (why they drink '
                            'it, a milk memory, milk helping them focus or lock in), positive and genuine'),
        },
        # Never comment on milkXP's videos: no first comment is stored or posted for this campaign.
        'first_comment':   False,
        'whop_campaign':   'https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/7d376c42-0d49-4dab-80e9-5c3d76d67103',
        # Paid brand content: tick YouTube's "includes paid promotion" (user decision 2026-10-24).
        'paid_promotion':  True,
        'required_caption': ['@milkxpofficial'],
        'caption_footer':   'From milkXP Episode 3. Full show on YouTube @milkxpofficial\n\n#milkXP #milk #challenge',
        # Caption, title, tags, header and overlays.
        'banned_patterns': _MILK_CAPTION_BANS,
        # Clip audio: other drinks and drink brands, claims or negativity about milk, swearing.
        'transcript_banned': _MILK_AUDIO_BANS,
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


def publish_plan(profile, now=None, slot=None):
    """
    (publishAt, slot) as UTC ISO 'Z' strings, or (None, None) if the profile has
    no slots (publish right away). publishAt is the next slot, or "as soon as
    possible" for a run that finishes shortly after a slot, but only while that
    is still inside the US-evening window; `slot` is the nominal slot it serves.

    `slot` ('HH:MM' ET, e.g. from the PUBLISH_SLOT env var) pins a run to one of
    the profile's slots, so a campaign with two daily slots has one cron per
    slot instead of both crons landing on whichever slot comes next.
    """
    slots = profile.get('publish_slots_et')
    if not slots:
        return None, None
    if slot:
        slot = slot.strip()
        if slot not in slots:
            raise ValueError(f"PUBLISH_SLOT {slot} is not one of this campaign's slots {slots}")
        slots = [slot]
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
    late = [t for t in times
            if earliest - datetime.timedelta(minutes=_LATE_GRACE_MIN + _PROCESSING_MIN) < t < earliest]
    if in_publish_window(earliest) and late:
        pick, nominal = earliest, max(late)
    else:
        pick = nominal = min(t for t in times if t >= earliest)
    fmt = '%Y-%m-%dT%H:%M:%SZ'
    return pick.strftime(fmt), nominal.strftime(fmt)


def next_publish_time(profile, now=None, slot=None):
    """Next publishAt (UTC ISO 'Z') for the profile's slots, or None (see publish_plan)."""
    return publish_plan(profile, now, slot)[0]


def get_campaign(name):
    name = (name or 'friends').strip().lower()
    if name not in CAMPAIGNS:
        raise ValueError(f"Unknown CAMPAIGN '{name}' — choose from: {', '.join(CAMPAIGNS)}")
    return name, CAMPAIGNS[name]


def episode_campaign(episode_name):
    """Campaign an episode belongs to: listed in a profile's files, else the Friends folder."""
    for name, prof in CAMPAIGNS.items():
        if any(ep == episode_name for _, ep in prof.get('files', [])):
            return name
    return 'friends'


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


def episode_guest(profile, episode=None):
    """The guest named for this episode in the profile's episode_notes, or None."""
    return ((profile.get('episode_notes') or {}).get(episode or '') or {}).get('guest')


def required_caption(profile, episode=None):
    """Caption text the brief requires: the profile's list, plus the episode's guest when it has one."""
    required = list(profile.get('required_caption') or [])
    guest = episode_guest(profile, episode)
    if guest:
        required.append(guest)
    return required


def enforce_caption(yt_meta, profile, episode=None):
    """Append the brief's required caption text if the LLM left any of it out."""
    required = required_caption(profile, episode)
    if not required:
        return yt_meta
    desc = yt_meta.get('description', '')
    missing = [r for r in required if r.lower() not in desc.lower()]
    if missing:
        footer = profile['caption_footer'].replace('{guest}', episode_guest(profile, episode) or 'This guest')
        yt_meta['description'] = desc.rstrip() + '\n\n' + footer
    return yt_meta


def moment_hints(profile, episode=None):
    """
    Prompt text for the episode's notes (guest, and moments the brand flagged
    as worth clipping, in episode seconds), or '' if the profile has none.
    """
    notes = (profile.get('episode_notes') or {}).get(episode or '')
    if not notes:
        return ''
    lines = []
    if notes.get('guest'):
        about = f" ({notes['about']})" if notes.get('about') else ''
        lines.append(f"Guest in this episode: {notes['guest']}{about}. Name them correctly.")
    if notes.get('moments'):
        lines.append('PRIORITY MOMENTS the show wants clipped (episode seconds; timestamps are approximate, '
                     'find the matching sentences and still apply every rule). Prefer these over other moments:')
        for start, end, what in notes['moments']:
            span = f"{start:.0f}-{end:.0f}s" if end is not None else f"from ~{start:.0f}s"
            lines.append(f"  - [{span}] {what}")
    return '\n'.join(lines)


def clip_type_for_run(profile, run):
    """
    The clip type this run wants when the brief asks for several kinds
    (milkxp: funny / milk_moment): they alternate by run number (1-based),
    so consecutive runs of an episode cover both. None if the profile has none.
    """
    types = list((profile.get('clip_types') or {}).keys())
    if not types:
        return None
    try:
        k = (int(run) - 1) % len(types)
    except (TypeError, ValueError):
        k = 0
    return types[k]


def clip_type_hint(profile, run):
    """Prompt text asking for every clip type (each candidate labelled), this run's type first; '' if none."""
    types = profile.get('clip_types') or {}
    want = clip_type_for_run(profile, run)
    if not want:
        return ''
    lines = ['CLIP TYPES: the brand wants these kinds of clips. Label every candidate with "clip_type" '
             f'({" or ".join(repr(k) for k in types)}):']
    lines += [f'  - "{k}": {v}' for k, v in types.items()]
    others = [k for k in types if k != want]
    lines.append(f'THIS RUN WANTS "{want}": give at least two "{want}" candidates first (if the transcript has '
                 f'them), then at least one {" / ".join(repr(o) for o in others)} candidate as a fallback.')
    return '\n'.join(lines)


def comments_allowed(profile):
    """False when the brief forbids commenting (milkxp): no first comment is stored or posted."""
    return profile.get('first_comment', True) is not False


_QUOTED = re.compile(r'"([^"]{3,})"|“([^”]{3,})”')


def quote_problems(text, transcript):
    """Quoted spans in text that were not said word for word in the clip (brief: quotes are verbatim only)."""
    spoken = ' '.join(re.findall(r"[a-z0-9]+", (transcript or '').lower().replace("'", '')))
    bad = []
    for m in _QUOTED.finditer(text or ''):
        q = m.group(1) or m.group(2)
        words = ' '.join(re.findall(r"[a-z0-9]+", q.lower().replace("'", '').replace('’', '')))
        if words and f' {words} ' not in f' {spoken} ':
            bad.append(q)
    return bad


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
            if w not in _STOP and w not in skip and not (w.endswith('s') and w[:-1] in skip)}


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


def _title_head(title, profile, n=4):
    """First n words after the leading show/person name ("Mark Manson's", "Love Is Blind:")."""
    norm = norm_title(title)
    for name in sorted(profile.get('title_names') or [], key=len, reverse=True):
        nn = norm_title(name)
        if nn and norm.startswith(nn):
            norm = re.sub(r"^'s\b", '', norm[len(nn):]).strip()
            break
    words = norm.split()
    if words and words[0] in ('on', 'and', 'with', 'vs'):
        words = words[1:]
    return words[:n]


def title_problems(title, profile, prior_titles=(), payoff_line='', campaign_titles=(), vague_hard=False):
    """
    (hard, soft) problems with a title. Hard ones must be fixed before upload:
    "Wait…"-style openers, an exact reuse of any earlier title, spoiling the
    payoff and (vague_hard, as the upload path uses it) a vague tease word with
    no concrete object. Soft ones are preferences the LLM should meet,
    including not reusing the opening words (after the leading name) of this
    campaign's recent titles.
    """
    hard, soft = [], []
    norm = norm_title(title)
    if _BANNED_OPENERS.search(title or ''):
        hard.append('banned opener')
    if vague_hard:
        vague = vague_hits(title, profile.get('vague_exempt', ()))
        if vague:
            hard.append(f'vague words {vague}')
    if re.search(r'really just say that', norm):
        hard.append('templated "really just say that" title')
    if norm in {norm_title(t) for t in prior_titles if t}:
        hard.append('exact title already used')
    else:
        head = _title_head(title, profile)
        if len(head) == 4 and any(_title_head(t, profile) == head for t in list(campaign_titles)[-50:] if t):
            soft.append('same opening words as a recent title')
    spoils = spoiler_hits(title, payoff_line, profile)
    if len(spoils) >= 2:
        hard.append(f'gives away the payoff {spoils}')
    if len(title) > TITLE_MAX:
        soft.append(f'{len(title)} chars > {TITLE_MAX}')
    names = profile.get('title_names') or []
    if names and not any(n.lower() in title[:35].lower() for n in names):
        soft.append('does not lead with the show or a real name')
    return hard, soft


def clean_hashtags(yt_meta, profile, episode=None):
    """
    Keep 3-5 specific hashtags: hashtags that spell a required caption term
    (#loveisblind, #Netflix for "Netflix") are always kept, broad ones
    (#viral, #funnymoments...) are dropped. Hashtag-only lines are merged into
    one final line; a hashtag inside a sentence stays if kept, else becomes a
    plain word so the sentence still reads. Re-checks the required caption
    afterwards and drops generic tags.
    """
    desc = yt_meta.get('description', '') or ''
    required = {re.sub(r'[^a-z0-9]', '', r.lower()) for r in profile.get('required_caption', [])}
    found = re.findall(r'#\w+', desc)
    must = [t for t in found if t[1:].lower() in required]
    keep, seen = [], set()
    for tag in must + found:
        low = tag.lower()
        if low in seen or low[1:] in BROAD_HASHTAGS:
            continue
        if tag not in must and len(keep) >= MAX_HASHTAGS:   # required ones come first and always stay
            continue
        seen.add(low)
        keep.append(tag)
    kept = {k.lower() for k in keep}

    lines, inline = [], set()
    for line in desc.splitlines():
        if line.strip() and not re.sub(r'#\w+', '', line).strip():
            continue                                   # hashtag-only line: rebuilt below
        def plain(m):
            if m.group(0).lower() in kept:
                inline.add(m.group(0).lower())
                return m.group(0)
            return m.group(0)[1:]
        lines.append(re.sub(r'#\w+', plain, line).rstrip())
    body = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
    tail = ' '.join(k for k in keep if k.lower() not in inline)
    yt_meta['description'] = f"{body}\n\n{tail}" if tail else body
    tags = [t.strip() for t in str(yt_meta.get('tags', '')).split(',') if t.strip()]
    yt_meta['tags'] = ', '.join(t for t in tags if t.lower() not in _BROAD_TAGS)
    return enforce_caption(yt_meta, profile, episode)


# ---------------------------------------------------------------------------
# Header, overlay and length rules (packaging v2)
# ---------------------------------------------------------------------------

# Vague tease words: 18% of corpus virals vs 39% of flops (and 46% of our titles).
# These never name anything concrete:
VAGUE_ALWAYS = {'something', 'unexpected', 'exposed', 'moment', 'moments', 'changed', 'reaction',
                'reactions', 'life'}
# ...and these are fine only when the concrete thing follows ("this sandwich", "secret job").
VAGUE_NEEDS_OBJECT = {'this', 'that', 'secret', 'secrets', 'truth'}
HEADER_MAX_WORDS = 9
HEADER_OPENER_WORDS = 3
HEADER_OPENER_LOOKBACK = 30


def vague_hits(text, exempt=()):
    """Vague tease words in text that have no concrete object after them."""
    words = re.findall(r"[a-z0-9$']+", (text or '').lower().replace('’', "'"))
    skip = {w.lower() for w in exempt}
    hits = []
    for i, w in enumerate(words):
        if w in skip:
            continue
        if w in VAGUE_ALWAYS:
            hits.append(w)
        elif w in VAGUE_NEEDS_OBJECT:
            nxt = words[i + 1].replace("'s", '') if i + 1 < len(words) else ''
            if len(nxt) < 3 or nxt in _STOP or nxt in VAGUE_ALWAYS or nxt in VAGUE_NEEDS_OBJECT:
                hits.append(w)
    return hits


def _names(profile):
    return list(profile.get('title_names') or []) + list(profile.get('cast_names') or [])


def _proper_nouns(text, anywhere=False):
    """Capitalised words in text (only mid-sentence ones unless anywhere), e.g. names in a reveal."""
    out = []
    for m in re.finditer(r"[A-Za-z][a-z']+", text or ''):
        w = m.group(0)
        before = (text or '')[:m.start()].rstrip()
        start = not before or before[-1] in '.?!"'
        if w[0].isupper() and (anywhere or not start) and w.lower() not in _STOP:
            out.append(re.sub(r"'s$", '', w))
    return out


def header_opener(header, n=HEADER_OPENER_WORDS):
    return ' '.join(norm_title(header).split()[:n])


def header_problems(story, profile, prior_headers=()):
    """
    Hard problems with the on-screen header: too long, a vague tease, any
    content word shared with the payoff or the LLM's own reveal (it is read
    before the joke lands), a banned brief term, or the same opening words as
    one of the last 30 headers.
    """
    header = story.get('header') or story.get('hook_text') or ''
    if not header:
        return ['missing header']
    problems = []
    n = len(norm_title(header).split())
    if n > HEADER_MAX_WORDS:
        problems.append(f'header is {n} words > {HEADER_MAX_WORDS}')
    vague = vague_hits(header, profile.get('vague_exempt', ()))
    if vague:
        problems.append(f'header uses vague words {vague}')
    # The header must name a person, who is usually in the payoff and the reveal too: proper
    # names (profile names, capitalised words in the reveal, mid-sentence capitals in the
    # dialogue) are not spoilers.
    names = _names(profile) + _proper_nouns(story.get('reveal'), anywhere=True)
    for k in ('payoff_line', '_payoff_text'):
        names += _proper_nouns(story.get(k))
    if profile.get('spoiler_scope') == 'punchline':
        # Only the punchline sentence's own words: not the reveal summary, not the topic
        # words, not words the clip already said before the punchline (that is the setup).
        names += list(profile.get('topic_words') or [])
        punch = content_words(story.get('_payoff_text') or story.get('payoff_line'), names)
        spoils = sorted((punch - content_words(story.get('_setup_text'), names)) & content_words(header, names))
    else:
        spoil_src = ' '.join(str(story.get(k) or '') for k in ('payoff_line', '_payoff_text', 'reveal'))
        spoils = sorted(content_words(spoil_src, names) & content_words(header, names))
    if spoils:
        problems.append(f'header gives away the payoff {spoils}')
    banned = find_banned(header, profile)
    if banned:
        problems.append(f'header uses banned words {banned}')
    opener = header_opener(header)
    recent = [header_opener(h) for h in list(prior_headers)[-HEADER_OPENER_LOOKBACK:] if h]
    if len(opener.split()) == HEADER_OPENER_WORDS and opener in recent:
        problems.append(f'header opens like a recent one ("{opener}")')
    return problems


def _tokens(text, names=()):
    skip = {w.replace("'", '') for name in names for w in norm_title(name).split()}
    return [w for w in re.findall(r"[a-z0-9']+", (text or '').lower().replace("'", ''))
            if len(w) >= 3 and w not in _STOP and w not in _FUNCTION and w not in skip
            and not (w.endswith('s') and w[:-1] in skip)]


# A run of these short function words is not a quote ("out of the", "in the room").
_FUNCTION = {'out', 'off', 'the', 'and', 'but', 'for', 'not', 'are', 'was', 'her', 'his', 'him', 'she', 'you',
             'who', 'how', 'why', 'all', 'one', 'our', 'its', 'own', 'too', 'has', 'had', 'got', 'get', 'can'}


def echo_problem(text, transcript, names=(), share_max=0.4, gram=3, min_said=1):
    """
    Why an overlay line only repeats the dialogue (it must add a voice, not a
    transcription): any shared `gram`-word run that is not all stop-words, or
    share_max+ of its content words (and at least min_said of them) spoken in
    the clip. None if it is original.
    """
    words = re.findall(r"[a-z0-9']+", (text or '').lower().replace("'", ''))
    spoken = re.findall(r"[a-z0-9']+", (transcript or '').lower().replace("'", ''))
    grams = {tuple(spoken[i:i + gram]) for i in range(len(spoken) - gram + 1)}
    # a 3-word run counts with one content word; a longer run needs two ("out of the room" is not a quote)
    need = 1 if gram <= 3 else 2
    # names (and a profile's topic words) are not quoted content: "the trading desk" is the topic
    skip = {w.replace("'", '') for name in names for w in norm_title(name).split()}
    filler = lambda w: w in _STOP or len(w) < 3 or w in skip or w in _FUNCTION
    for i in range(len(words) - gram + 1):
        g = tuple(words[i:i + gram])
        if g in grams and sum(not filler(w) for w in g) >= need:
            return f'repeats the line "{" ".join(g)}"'
    # any 5 words in a row from the clip are a quote, however plain the words
    five = {tuple(spoken[i:i + 5]) for i in range(len(spoken) - 4)}
    for i in range(len(words) - 4):
        if tuple(words[i:i + 5]) in five:
            return f'repeats the line "{" ".join(words[i:i + 5])}"'
    toks = _tokens(text, names)
    said = set(_tokens(transcript, names))
    if toks:
        n_said = sum(t in said for t in toks)
        share = n_said / len(toks)
        if share >= share_max and n_said >= min_said:
            return f'{share:.0%} of its words are spoken in the clip'
    return None


# A context line describes the situation, so it naturally names what is talked about
# ("Stax founder on being laughed out of the room"): only a near-quote is an echo.
CONTEXT_ECHO = dict(share_max=0.75, gram=4, min_said=3)


def layer_line_problem(field, text, transcript, names, profile):
    """Why one originality line (context_line / closing_line / annotation) cannot be shown, or None."""
    exempt = profile.get('vague_exempt', ())
    why = echo_problem(text, transcript, names, **(CONTEXT_ECHO if field == 'context_line' else {}))
    banned = find_banned(text, profile)
    misquoted = quote_problems(text, transcript) if profile.get('verbatim_quotes') else []
    if banned:
        why = f'banned words {banned}'
    elif misquoted:
        why = f'quotes words not said in the clip {misquoted}'
    elif not why and field == 'context_line' and vague_hits(text, exempt):
        why = f'vague words {vague_hits(text, exempt)}'
    return why


def layer_names(story, all_words, profile):
    """(transcript, names) the overlay checks use: naming who is on screen or the topic is not an echo."""
    transcript = clip_text(story, all_words) if all_words else ' '.join(
        str(story.get(k) or '') for k in ('opening_line', 'payoff_line'))
    names = _names(profile) + _proper_nouns(transcript) + list(profile.get('topic_words') or [])
    return transcript, names


# Safe closing lines when the LLM's was missing or dropped: an open question adds the
# viewer's voice without quoting anyone. Context lines come from the LLM's own fields.
FALLBACK_CLOSERS = {
    'podcast': ['Would you have done the same?', 'Agree or disagree?', 'What would you have done?'],
    'sitcom':  ['Whose side are you on?', 'Who was right here?', 'Who handled that worse?'],
    'reality': ['Who was in the wrong here?', 'Whose side are you on?', 'Would you have stayed?'],
}
FALLBACK_CLOSERS['talk'] = FALLBACK_CLOSERS['podcast']
FALLBACK_CLOSERS['drama'] = FALLBACK_CLOSERS['reality']


def _sentence_case(text):
    text = re.sub(r'\s+', ' ', str(text or '')).strip().rstrip('.')
    if text and text.upper() == text:          # an ALL CAPS header
        text = text.lower()
        text = text[:1].upper() + text[1:]
    return text


def fallback_layer_lines(story, field, profile):
    """
    Candidate lines for a missing originality field, best first: for
    context_line the LLM's own stake_line, header and summary (4-10 words);
    for closing_line a genre question.
    """
    if field == 'closing_line':
        return list(FALLBACK_CLOSERS.get(profile.get('genre') or 'sitcom', FALLBACK_CLOSERS['sitcom']))
    out = []
    for key in ('stake_line', 'header', 'storyline_summary'):
        text = _sentence_case(story.get(key))
        # restore proper names the ALL CAPS header lowered (profile names and cast)
        for name in _names(profile):
            text = re.sub(rf"\b{re.escape(name.lower())}\b", name, text, flags=re.IGNORECASE)
        if 4 <= len(text.split()) <= 10 and text not in out and '"' not in text:
            out.append(text)
    return out


ROLE_WORDS = {'boss', 'bosses', 'manager', 'husband', 'wife', 'boyfriend', 'girlfriend', 'fiance', 'fiancee',
              'mom', 'mother', 'dad', 'father', 'son', 'daughter', 'sister', 'brother', 'ex', 'coworker',
              'coworkers', 'colleague', 'colleagues', 'employee', 'employees', 'investor', 'investors', 'ceo',
              'cop', 'police', 'teacher', 'doctor', 'lawyer', 'therapist', 'mentor', 'landlord', 'neighbor',
              'kids', 'kid', 'children', 'parents', 'grandma', 'grandpa', 'stranger', 'billionaire', 'millionaire'}
_ROLE_KIN = {'mom': {'mother', 'mum', 'mama'}, 'mother': {'mom', 'mum', 'mama'}, 'dad': {'father', 'papa'},
             'father': {'dad', 'papa'}, 'kids': {'kid', 'children', 'son', 'daughter'},
             'kid': {'kids', 'children'}, 'children': {'kids', 'kid'}, 'boss': {'bosses'}, 'bosses': {'boss'},
             'coworker': {'coworkers', 'colleague'}, 'coworkers': {'coworker', 'colleagues'}}


def faithful_problems(meta, transcript, profile=None, extra=''):
    """
    Hard problems when the metadata claims something the clip does not say:
    a person or role (boss, husband, investor...) that never appears in the
    clip transcript (or extra: the guest/episode notes), or the model's own
    title_check saying the title adds something unsupported.
    """
    if not transcript:
        return []
    said = set(re.findall(r"[a-z]+", (transcript + ' ' + (extra or '')).lower().replace("'s ", ' ')))
    title = str(meta.get('title') or '')
    problems = []
    added = []
    for w in re.findall(r"[a-z]+", title.lower().replace("'s ", ' ').replace("’s ", ' ')):
        if w in ROLE_WORDS and w not in said and not (_ROLE_KIN.get(w, set()) & said):
            added.append(w)
    if added:
        problems.append(f"title adds {sorted(set(added))}, not in the clip")
    check = meta.get('title_check')
    if isinstance(check, dict) and check.get('adds_unsupported') in (True, 'true', 'True'):
        problems.append('title_check says the title adds something the clip does not say')
    return problems


def overlay_checks(story, all_words, profile):
    """
    {field: reason} for every originality-layer line (context_line,
    closing_line, each annotation) that echoes the clip or uses a banned or
    vague term, plus 'header' if the header echoes the dialogue.
    """
    transcript, names = layer_names(story, all_words, profile)
    bad = {}
    header = story.get('header') or story.get('hook_text')
    if header:
        why = echo_problem(header, transcript, names, share_max=profile.get('header_echo_share', 0.4))
        if why:
            bad['header'] = why
    lines = [(k, story.get(k)) for k in ('context_line', 'closing_line')]
    lines += [(f"annotation@{a['t']:.1f}", a['text']) for a in story.get('annotations') or []]
    for field, text in lines:
        if not text:
            continue
        why = layer_line_problem(field, text, transcript, names, profile)
        if why:
            bad[field] = why
    return bad


def render_config(profile):
    """The profile's render dict (contract keys), defaulting by niche for profiles that predate it."""
    return profile.get('render') or _render('A')


def length_plan(profile):
    """(min_seconds, hard max, target range) for the profile's niche, inside the brief's own limits."""
    niche = NICHE_LENGTH[render_config(profile).get('niche', 'A')]
    lo = profile.get('min_seconds') or 12.0
    hi = min(profile.get('max_seconds') or niche['max'], niche['max'])
    t_lo, t_hi = profile.get('target_seconds') or niche['target']
    t_lo = max(t_lo, lo)
    return lo, hi, (t_lo, max(t_lo, min(t_hi, hi)))


def story_max_seconds(story, profile):
    """Hard max for this story: the niche max, or up to 60 s for a single long arc in niche B."""
    _, hi, _ = length_plan(profile)
    render = render_config(profile)
    if (render.get('niche') == 'B' and story.get('single_story_arc')
            and story.get('_payoff_pos', 0) >= LONG_ARC_PAYOFF_POS):
        return min(LONG_ARC_MAX, float(render.get('max_duration') or LONG_ARC_MAX))
    return hi


def layer_caps(profile):
    """
    (framing_cap, originality_cap): rubric points this campaign can earn with
    what it may show. Framing needs any on-screen text (header or overlays);
    originality needs the overlay lines, which only overlays_allowed renders.
    """
    render = render_config(profile)
    shows_text = bool(render.get('header') or render.get('overlays_allowed'))
    return (2 if shows_text else 0), (2 if render.get('overlays_allowed') else 0)


def rubric_gate(profile, base=11):
    """
    Minimum rubric total (of 16). A campaign that may not show framing or
    originality text cannot earn those 4 points, so its gate scales with
    what is reachable (11/16 of 14 -> 10). A profile can set min_rubric
    (the podcast/talk profiles use 8: their story openers score ST = 1).
    """
    if profile.get('min_rubric') is not None:
        return profile['min_rubric']
    reachable = 12 + sum(layer_caps(profile))
    return -(-base * reachable // 16)


# ---------------------------------------------------------------------------
# Experiments (BUILD_PLAN section 5). Unit = one upload; blocked randomisation
# within each campaign: every consecutive block of len(cells) uploads gets each
# cell once. Off until the hygiene baseline week is done: set 'active': True
# here or list ids in the EXPERIMENTS_ON env var (e.g. "X1,X2").
# ---------------------------------------------------------------------------
EXPERIMENTS = {
    'X1': {'factor': 'overlay',  'arms': ['on', 'off'],   'campaigns': ['friends'], 'active': False,
           'requires': 'overlays_allowed'},
    'X2': {'factor': 'template', 'arms': ['A1', 'A2'],    'campaigns': ['friends'], 'active': False},
    # Needs a licensed music library first (user decision); allow_music stays False until then.
    'X3': {'factor': 'music',    'arms': ['bed', 'off'],  'campaigns': ['markmanson', 'unpacked'],
           'active': False, 'requires': 'allow_music'},
    # Non-laugh-track sources only; allow_sfx stays False until a brief allows it.
    'X4': {'factor': 'sfx',      'arms': ['boom', 'off'], 'campaigns': ['loveisblind', 'loveandjustice'],
           'active': False, 'requires': 'allow_sfx'},
}


def base_variant(render):
    """Contract variant for a render config with no experiment running."""
    sfx = render.get('sfx') or {}
    return {
        'template': render.get('template', 'A1'),
        'overlay':  'on' if render.get('overlays_allowed') else 'off',
        'music':    'bed' if render.get('allow_music') and render.get('music_mode') == 'bed' else 'off',
        'sfx':      'boom' if render.get('allow_sfx') and sfx.get('payoff_boom') else 'off',
    }


def active_experiments(campaign, render, enabled=None):
    """[(id, experiment)] running for this campaign, skipping any whose factor the brief does not allow."""
    enabled = {e.strip() for e in (enabled or '').split(',') if e.strip()}
    out = []
    for eid, exp in EXPERIMENTS.items():
        if not (exp.get('active') or eid in enabled) or campaign not in exp['campaigns']:
            continue
        if exp.get('requires') and not render.get(exp['requires']):
            continue
        out.append((eid, exp))
    return out


def assign_variant(campaign, render, prior, seed, enabled=None):
    """
    (variant, experiment ids). prior: this campaign's earlier logged variants,
    oldest first (dicts with an 'exp' list). Within the current block the cell
    is drawn (seeded by episode and run) from the cells not used yet, so
    consecutive pairs (X1 alone) or quads (X1 x X2) each get every arm once.
    """
    variant = base_variant(render)
    exps = active_experiments(campaign, render, enabled)
    if not exps:
        return variant, []
    ids = [eid for eid, _ in exps]
    cells = list(itertools.product(*[exp['arms'] for _, exp in exps]))
    same = [p for p in prior if isinstance(p, dict) and sorted(p.get('exp') or []) == sorted(ids)]
    pos = len(same) % len(cells)
    used = [tuple(p.get(exp['factor']) for _, exp in exps) for p in same[len(same) - pos:]] if pos else []
    remaining = [c for c in cells if c not in used] or cells
    cell = random.Random(str(seed)).choice(remaining)
    for (_, exp), arm in zip(exps, cell):
        variant[exp['factor']] = arm
    return variant, ids


def apply_variant(story, variant):
    """
    Strip what the variant does not show: overlay 'off' keeps the header only
    (the A2 template keeps its context card, which stands in for the header).
    """
    if variant.get('overlay') == 'off':
        story['closing_line'] = None
        story['annotations'] = []
        if variant.get('template') != 'A2':
            story['context_line'] = None
    return story
