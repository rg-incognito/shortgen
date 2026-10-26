"""
Pick the publishAt time for an upload from what is already booked on the channel.

Rules (BACKLOG P0-6, P0-8, night of 2026-10-25):
  * a run whose slot already holds a Short of the same campaign does not upload (`slot_filled`);
  * never publish immediately: a pinned slot that has passed or is under LEAD_MIN away rolls to the
    campaign's next free slot (or the same slot tomorrow) (`choose_publish_time`);
  * no two of our own publishAt times, across every campaign, are less than MIN_GAP_MIN apart;
  * a campaign rolls at most one Short at a time: if it already has one queued, a blocked run
    stops (before rendering when possible, `start_blocked`) instead of growing a backlog.

A booking is {'when': aware UTC datetime, 'campaign': name or None, 'video_id': id or None}.
`merge_bookings` builds the list from the YouTube API (authoritative: it sees every upload on the
channel, from GitHub, the local fallback or by hand) and the tracker sheet (which knows the campaign).
"""
import datetime

from campaigns import _ET, _PROCESSING_MIN, SLOT_GUARD_MIN, episode_campaign, in_publish_window, window_publish_time

MIN_GAP_MIN = 75      # Shorts posted within 0-60 min of another of ours got under 50 views ("ghost")
LEAD_MIN = max(_PROCESSING_MIN, 15)   # a publishAt closer than this is "now": roll instead
ROLL_DAYS = 7
_FMT = '%Y-%m-%dT%H:%M:%SZ'
_UTC = datetime.timezone.utc


def parse_iso(iso):
    """'2026-10-25T22:45:00Z' / '...+00:00' / '...000Z' -> aware UTC datetime (None if unparseable)."""
    if not iso:
        return None
    if isinstance(iso, datetime.datetime):
        return iso if iso.tzinfo else iso.replace(tzinfo=_UTC)
    s = str(iso).strip().replace('Z', '+00:00')
    try:
        t = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=_UTC)).astimezone(_UTC)


def fmt(t):
    return t.astimezone(_UTC).strftime(_FMT)


def video_id_from_url(url):
    url = (url or '').strip()
    for prefix in ('https://youtu.be/', 'https://www.youtube.com/shorts/', 'https://www.youtube.com/watch?v='):
        if url.startswith(prefix):
            return url[len(prefix):].split('&')[0].split('?')[0] or None
    return None


def merge_bookings(youtube_items=None, sheet_rows=None, now=None, horizon_days=2):
    """
    youtube_items: [{'video_id', 'when'}] from youtube_client.own_schedule(), or None if the API
    call failed. sheet_rows: [{'episode', 'when', 'video_id'}] from SheetTracker.get_bookings().
    YouTube decides when a video goes out (and whether it still exists); the sheet adds the campaign.
    Rows older than horizon_days are dropped.
    """
    now = now or datetime.datetime.now(_UTC)
    floor = now - datetime.timedelta(days=horizon_days)
    sheet_rows = sheet_rows or []
    camp_of = {}
    for r in sheet_rows:
        if r.get('video_id'):
            camp_of[r['video_id']] = episode_campaign(r.get('episode') or '')
    out = []
    if youtube_items is not None:
        for it in youtube_items:
            when = parse_iso(it.get('when'))
            if not when or when < floor:
                continue
            vid = it.get('video_id')
            out.append({'when': when, 'campaign': camp_of.get(vid), 'video_id': vid})
    for r in sheet_rows:
        when = parse_iso(r.get('when'))
        if not when or when < floor:
            continue
        vid = r.get('video_id')
        if youtube_items is not None and vid:
            # Listed on YouTube: already added with YouTube's time. Not listed: deleted / never went up.
            continue
        out.append({'when': when, 'campaign': episode_campaign(r.get('episode') or ''), 'video_id': vid})
    out.sort(key=lambda b: b['when'])
    return out


def _occurrences(slots, start_date, days):
    """UTC datetimes of the given 'HH:MM' ET slots on each day from start_date, inside the window."""
    out = []
    for day in range(days):
        d = start_date + datetime.timedelta(days=day)
        for hhmm in slots:
            h, m = map(int, hhmm.split(':'))
            t = datetime.datetime(d.year, d.month, d.day, h, m, tzinfo=_ET).astimezone(_UTC)
            if in_publish_window(t):
                out.append(t)
    return sorted(out)


def _slot_span(profile, nominal):
    """How far after a pinned slot a same-campaign Short still counts as 'in' it: up to the campaign's
    next slot (LADbible 22:30 must not see the 23:15 Short), at most SLOT_GUARD_MIN."""
    slots = profile.get('publish_slots_et') or []
    later = [t for t in _occurrences(slots, nominal.astimezone(_ET).date() - datetime.timedelta(days=1), 3)
             if t > nominal]
    span = datetime.timedelta(minutes=SLOT_GUARD_MIN)
    if later:
        span = min(span, later[0] - nominal)
    return span


def slot_filled(campaign, profile, nominal_iso, bookings, pinned=False):
    """
    The booking (dict) of a Short of this campaign that already serves the slot at nominal_iso,
    else None. Pinned: only Shorts at or after the slot and before the campaign's next slot count.
    Unpinned: anything of the campaign within SLOT_GUARD_MIN of the slot.
    """
    nominal = parse_iso(nominal_iso)
    if not nominal:
        return None
    span = _slot_span(profile, nominal) if pinned else None
    guard = datetime.timedelta(minutes=SLOT_GUARD_MIN)
    for b in bookings:
        if b.get('campaign') != campaign:
            continue
        if pinned:
            if nominal <= b['when'] < nominal + span:
                return b
        elif abs(b['when'] - nominal) <= guard:
            return b
    return None


def _clash(t, bookings, gap):
    return [b for b in bookings if abs(b['when'] - t) < gap]


def _pending(campaign, bookings, now):
    """This campaign's Shorts that have not gone public yet."""
    return [b for b in bookings if campaign and b.get('campaign') == campaign and b['when'] > now]


def start_blocked(campaign, profile, nominal_iso, bookings, now=None, pinned=False, min_gap_min=MIN_GAP_MIN):
    """
    Why a run should not even render (None: go ahead): its slot already holds a Short of the
    campaign, or the slot sits within min_gap_min of another Short while the campaign already
    has one queued (the render could only roll, and rolling a second Short every day would
    pile up a backlog: MilkXP 19:45 is 60 min after MilkXP 18:45).
    """
    now = now or datetime.datetime.now(_UTC)
    filled = slot_filled(campaign, profile, nominal_iso, bookings, pinned=pinned)
    if filled:
        return f"{filled.get('video_id') or 'a Short'} is already scheduled for the slot ({fmt(filled['when'])})"
    nominal = parse_iso(nominal_iso)
    hit = nominal and _clash(nominal, bookings, datetime.timedelta(minutes=min_gap_min))
    pend = _pending(campaign, bookings, now)
    if hit and pend:
        return (f"the slot is within {min_gap_min} min of {', '.join(b.get('video_id') or '?' for b in hit)} and "
                f"{campaign} already has {pend[0].get('video_id') or 'a Short'} queued for {fmt(pend[0]['when'])}")
    return None


def choose_publish_time(profile, now=None, bookings=(), nominal_iso=None, min_gap_min=MIN_GAP_MIN, campaign=None):
    """
    (publishAt ISO 'Z', note) for an upload made now. Never "as soon as possible" for a slotted
    campaign: the slot this run serves (nominal_iso) if it is at least LEAD_MIN away and at least
    min_gap_min from every booking, else the first later slot time of the campaign that is.
    A roll is skipped, (None, why), when the campaign already has a Short queued: one rolled
    Short at a time, not a growing backlog.
    A campaign without slots goes out LEAD_MIN from now, inside the window, pushed past bookings.
    """
    now = now or datetime.datetime.now(_UTC)
    earliest = now + datetime.timedelta(minutes=LEAD_MIN)
    gap = datetime.timedelta(minutes=min_gap_min)
    bookings = list(bookings)
    slots = profile.get('publish_slots_et')
    if not slots:
        t = parse_iso(window_publish_time(fmt(earliest)))
        for _ in range(200):
            hit = _clash(t, bookings, gap)
            if not hit:
                return fmt(t), 'asap'
            t = parse_iso(window_publish_time(fmt(max(b['when'] for b in hit) + gap)))
        raise RuntimeError('no publish time free of other Shorts found')
    nominal = parse_iso(nominal_iso)
    if nominal and nominal >= earliest and not _clash(nominal, bookings, gap):
        return fmt(nominal), 'slot'
    pend = _pending(campaign, bookings, now)
    if nominal and pend:
        return None, (f"slot {fmt(nominal)} is {'passed or too close' if nominal < earliest else 'too close to another Short'} "
                      f"and {campaign} already has {pend[0].get('video_id') or 'a Short'} queued for "
                      f"{fmt(pend[0]['when'])} — not rolling a second one")
    start = max(t for t in (nominal, earliest) if t is not None)
    first_day = (nominal or earliest).astimezone(_ET).date() - datetime.timedelta(days=1)
    skipped = []
    for t in _occurrences(slots, first_day, ROLL_DAYS + 2):
        if t < start:
            continue
        hit = _clash(t, bookings, gap)
        if hit:
            skipped.append(f"{fmt(t)} (within {min_gap_min} min of {', '.join(b.get('video_id') or '?' for b in hit)})")
            continue
        if nominal and t != nominal:
            why = 'slot passed' if nominal < earliest else 'slot too close to another Short'
            return fmt(t), f'rolled from {fmt(nominal)} ({why}); skipped: {"; ".join(skipped) or "none"}'
        return fmt(t), 'slot'
    raise RuntimeError(f'no free slot in the next {ROLL_DAYS} days: {"; ".join(skipped)}')


def gaps_ok(times, min_gap_min=MIN_GAP_MIN):
    """True if every pair of the given datetimes / ISO strings is at least min_gap_min apart."""
    ts = sorted(parse_iso(t) for t in times)
    return all((b - a) >= datetime.timedelta(minutes=min_gap_min) for a, b in zip(ts, ts[1:]))
