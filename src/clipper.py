import math
import os
import re
import subprocess
import tempfile
import time

# Gaps between words longer than this are treated as laugh-track / dead air and
# shortened (not deleted).
_SILENCE_THRESH = 0.45  # seconds
# Lead-in kept before the first word of each story clip (hard trim of the opening).
_LEAD_PAD = 0.10        # seconds
# Of each long inner pause, keep this much (split 60/40 after/before the words)...
_KEEP_GAP = 0.35        # seconds
# ...or this much when the pause follows a sentence end (reaction / laugh beat).
_KEEP_BEAT = 0.60       # seconds
# A cut that would remove less than this is not worth a jump cut.
_MIN_CUT   = 0.20       # seconds
# Minimum hold after the last word of a story clip (the clip normally runs to
# its requested end, which carries the reaction hold); never more than _MAX_HOLD.
_TAIL_HOLD  = 0.30      # seconds, mid-story clips
_TAIL_FINAL = 0.25      # seconds, after the payoff word
_MAX_HOLD   = 1.20      # seconds of silence kept after the last word
# Words ending this far past the requested clip end belong to the next line.
_END_SLACK = 0.25       # seconds
# Sub-clips shorter than this are merged into a neighbour if the gap between
# them is short enough to restore.
_MIN_PART   = 1.0       # seconds
_MAX_BRIDGE = 1.2       # seconds
# Alternate punch-in on same-clip jump cuts; slow push-in on long parts.
_PUNCH_IN     = 1.12
_PUSH_IN_MIN_S = 4.0
# Short audio fades at every join to remove clicks.
_JOIN_FADE = 0.025      # seconds

# Render profiles (render= set): niche tail after the last word, reaction-cut
# extension, native laughs kept in the last 2 s, 4 s shot cap with punch-ins,
# a slow push only on the payoff part.
_TAIL_A, _TAIL_B = 1.0, 0.5   # seconds after the last word (sitcom / podcast)
_MAX_HOLD_A, _MAX_HOLD_B = 1.6, 0.7
_REACTION_HOLD = 0.8          # hold after a source cut into a reaction in the tail
_KEEP_END_S = 2.0
_SHOT_CAP_S = 4.0
# Podcast / talk (niche B): one static camera often holds 10 s+, so the same cap
# applies there, with punches allowed on any word boundary (talk rarely pauses).
_SHOT_CAP_B = 4.0
# ...and the payoff push only covers the last few seconds before the payoff
# (from the part's last camera cut it can span a whole one-camera answer).
_PUSH_B_LEAD = 2.5
# Talk tail: the next speaker often starts at once. The picture holds tail_s
# anyway and the audio fades out just before their first word.
_HOLD_FADE = 0.08
_HOLD_FADE_LEAD = 0.10
_PUNCH_HI, _PUNCH_LO = 1.10, 1.06   # punch level; LO when the source crop is < 900 px wide
_PUNCH_GAP_S = 3.0
_CALM_HOOK_S = 1.2
_PUSH_PAYOFF = 0.06
_PUSH_MIN_S = 1.0
# Loop seam: the Shorts player restarts at the end, and since Mar 2025 every replay is a
# public view. Our hits looped (178-190 % at the start); the stalled ones end on a frame
# and sound unlike the opening, so the restart reads as "over" and the viewer swipes.
# The tail's last 0.3 s dissolves into the opening frame and its audio fades, never
# closer than the guard to the payoff word's end; no seam when the tail is shorter.
_LOOP_SEAM = 0.30
_SEAM_GUARD = 0.15
_SEAM_MIN = 0.12
# Captions engine: 'ass' (libass, the default) or 'remotion' (remotion/, animated word pops;
# falls back to ASS on any problem). A profile may set render['captions_engine'] to a
# {slot 'HH:MM' ET: engine, '*': engine} dict, so one daily slot is the A/B arm.
_CAPTIONS_ENGINES = ('ass', 'remotion')

# --- Edit layer (render['edit'], campaigns.EDIT_FULL / EDIT_LIGHT; EDIT_SPEC.md) ----------
# Pace (R1): the first visual cut lands by 2.0 s and no framing is held longer than 3.5 s.
# Where the source has no camera cut, the cut is a reframe: the same take at a tighter
# (1.3-1.4x) face-centred crop, toggled back to wide on the next one; a hard cut resets wide.
_PACE_CAP = 3.5
_PACE_FIRST_BY = 2.0
_PACE_FIRST_FROM = 1.0       # the opening line lands before the first cut
_PACE_MIN_SHOT = 1.0         # a reframe never leaves a shot shorter than this
_PACE_TARGET = 2.4           # preferred reframe spacing (winners' median shot is 2.2 s)
_PACE_PAYOFF_GUARD = 0.5     # no reframe in the last 0.5 s before the payoff word
_REFRAME_HI, _REFRAME_LO = 1.40, 1.30   # close crop (source crop >= / < 900 px wide)
_REFRAME_ZOOM_MAX = 1.75     # face zoom x reframe level cap (720p sources stay readable)
# Flash (R6): a warm wash peaking (0.85) on the first frame after the cut, rising over
# 0.08 s and falling over 0.22 s (<= 0.35 s in all), at the biggest beat change only.
_FLASH_PEAK, _FLASH_RISE, _FLASH_FALL = 0.85, 0.08, 0.22
_FLASH_SECOND_MIN_S = 10.0   # a second flash only on a clip this long, at a story-clip join
_FLASH_GAP_S = 3.0
# SFX (R5): files from assets/sfx (own-work / CC0, assets/sfx/LICENSES.md). Levels are the
# peak 50 ms RMS relative to the clip's median speech RMS; under any word the SFX is ducked
# to <= _SFX_UNDER_SPEECH so it never masks a word, and the original audio is never replaced.
_SFX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'sfx')
_SFX = {
    # kind: (file, level dB re voice, window before / after the file's peak (s), fade-out s)
    'whoosh':  ('whoosh_1_short.wav', -8.0, 0.24, 0.14, 0.06),
    'whoosh2': ('whoosh_2_medium.wav', -8.0, 0.24, 0.14, 0.06),
    'flash':   ('swoosh_transition_1_up.wav', -8.0, 0.30, 0.10, 0.06),
    'pop':     ('pop_1_bubble.wav', -9.0, 0.02, 0.10, 0.02),
    'boom':    ('boom_1_bass_hit.wav', -4.0, 0.0, 0.60, 0.25),
}
_SFX_UNDER_SPEECH = -14.0    # dB re voice: the most an SFX may play while a word is spoken
_SFX_SPEECH_PAD = 0.03
_SFX_RATE = {'full': 1.6, 'cuts': 1.2}   # SFX per 10 s (spec 1-2, max 3)
_SFX_GAP_S = 3.0             # between two SFX (the frame-0 pop excepted)
_SFX_PAIR_GAP_S = 0.8        # ...except the flash swoosh into the payoff boom
_SFX_MIN_SPEECH_GAP = 0.08   # cut SFX sit in a speech gap at least this long
# Music bed (R4): built but off (allow_music False until licensed tracks exist).
_BED_DB, _BED_DUCK_DB = -16.0, 4.0
_BED_ATTACK, _BED_RELEASE = 0.03, 0.25
_BED_FADE_IN, _BED_FADE_OUT = 0.3, 0.5
_MUSIC_LICENSES = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'music', 'LICENSES.json')

_SENTENCE_END = re.compile(r'[.!?]["\')\]]*$')
_PHRASE_END = re.compile(r'[,;:]["\')\]]*$')


def _split_by_speech(clip_start, clip_end, all_words, silence_thresh=_SILENCE_THRESH,
                     final=True, tail=None, max_hold=None, tail_cut=None, keep_end_s=0.0,
                     hold_over=False):
    """
    Split [clip_start, clip_end] into speech sub-intervals.

    - The opening is trimmed hard to just before the first word.
    - Inner pauses longer than silence_thresh are shortened, not deleted: a
      sentence-ending pause keeps up to _KEEP_BEAT (the reaction / laugh beat),
      others keep _KEEP_GAP.
    - Sub-clips under _MIN_PART are merged with a neighbour across a short gap.
    - The last burst runs to clip_end (which carries the reaction hold), but
      at least _TAIL_FINAL (final clip) / _TAIL_HOLD after the last word and
      at most _MAX_HOLD, never running into the next spoken word.

    With tail (render profiles, final clip) the last burst ends exactly
    `tail` after the last word instead (A 1.0 s, B 0.5 s); a source cut to a
    reaction inside that window extends it to cut + 0.8 s, never past
    max_hold; pauses within keep_end_s of the last word are never shortened
    (native laughs before the payoff). hold_over (talk): the tail is kept
    even when the next speaker starts inside it (the caller fades that
    audio out, see _held_tail).

    Returns a list of (start, end) tuples in episode time.
    Falls back to [(clip_start, clip_end)] if no words are found in range.
    """
    words = [w for w in all_words if clip_start <= w['start'] < clip_end
             and w['end'] <= clip_end + _END_SLACK]
    if not words:
        return [(clip_start, clip_end)]

    following = [w['start'] for w in all_words if w['start'] >= words[-1]['end']]
    # The clip end is chosen upstream (sentence end + a deliberate reaction
    # hold), so run the last burst to it — at least `tail` past the last word
    # so the payoff is never clipped, at most _MAX_HOLD of silence.
    last_end = words[-1]['end']
    if tail is not None:
        end = last_end + tail
        if tail_cut is not None and last_end < tail_cut < last_end + tail:
            end = max(end, tail_cut + _REACTION_HOLD)
        end = min(end, last_end + (max_hold or tail))
    else:
        tail = _TAIL_FINAL if final else _TAIL_HOLD
        end = min(max(clip_end, last_end + tail), last_end + _MAX_HOLD)
    if following and not (hold_over and tail is not None):
        end = min(end, max(words[-1]['end'] + 0.05, following[0] - 0.05))
    # Never before the requested start: upstream snaps it to a sentence start,
    # and earlier would reach into the previous word.
    start = max(clip_start, words[0]['start'] - _LEAD_PAD)

    # Cut points inside long pauses: (end of kept part, start of next part)
    parts = [[start, None]]
    for prev, w in zip(words, words[1:]):
        gap = w['start'] - prev['end']
        if gap <= silence_thresh or (keep_end_s and w['start'] >= last_end - keep_end_s):
            continue
        keep = _KEEP_BEAT if _SENTENCE_END.search(prev['word'].strip()) else _KEEP_GAP
        if gap - keep < _MIN_CUT:
            continue
        parts[-1][1] = prev['end'] + keep * 0.6
        parts.append([w['start'] - keep * 0.4, None])
    parts[-1][1] = end

    # Merge parts that are too short to read as a deliberate cut
    merged = True
    while merged and len(parts) > 1:
        merged = False
        for i, (s, e) in enumerate(parts):
            if e - s >= _MIN_PART:
                continue
            gaps = []
            if i > 0:
                gaps.append((s - parts[i - 1][1], i - 1))
            if i + 1 < len(parts):
                gaps.append((parts[i + 1][0] - e, i))
            gap, left = min(gaps)
            if gap <= _MAX_BRIDGE:
                parts[left] = [parts[left][0], parts[left + 1][1]]
                del parts[left + 1]
                merged = True
                break

    return [(s, e) for s, e in parts]


def _snap_to_frames(start, end, fps):
    """Frame-aligned (start, end) whose length is a whole number of frames."""
    s_f = math.floor(start * fps + 1e-6)
    e_f = max(s_f + 1, int(round(end * fps)))
    return s_f / fps, e_f / fps


def edited_duration(story, all_words, silence_thresh=_SILENCE_THRESH, render=None,
                    video_path=None, wide_fallback=False):
    """
    Length of the finished short after silence trimming, without rendering
    anything. Pass the same render (and video_path, for the reaction-cut tail
    extension) as create_story_short to get the same number.
    """
    cfg = _render_config(render, story, wide_fallback)
    return sum(e - s for subs in _story_parts(story, all_words, silence_thresh, cfg, video_path)
               for s, e in subs)


def _render_config(render, story, wide_fallback=False):
    """
    Resolve the profile's render dict (BUILD_PLAN §3) plus the story's variant
    into the settings this module uses. None -> today's look (legacy path):
    every key that is missing falls back to the niche default.
    """
    if not render:
        return None
    variant = story.get('variant') or {}
    niche = render.get('niche') or ('B' if wide_fallback else 'A')
    template = variant.get('template') or render.get('template') or ('A1' if niche == 'A' else 'B1')
    if template not in ('A1', 'A2', 'B1'):
        template = 'A1' if niche == 'A' else 'B1'
    sfx = render.get('sfx') or {}
    boom = bool(render.get('allow_sfx')) and (variant.get('sfx') == 'boom' if variant.get('sfx')
                                             else bool(sfx.get('payoff_boom')))
    music = render.get('music_mode', 'off') if render.get('allow_music') else 'off'
    if variant.get('music') == 'off':
        music = 'off'
    tail = float(render.get('tail_s') or (_TAIL_A if niche == 'A' else _TAIL_B))
    names = {str(n).upper() for n in (render.get('names') or [])}
    names |= {str(k).upper() for k in (story.get('keywords') or [])}
    edit = _edit_config(render)
    if edit.get('headline') and template == 'B1':
        template = 'B1H'            # full-bleed under a blurred headline band
    sfx_mode = edit.get('sfx') if edit.get('sfx') in ('full', 'cuts') else None
    if sfx_mode == 'full' and variant.get('sfx') == 'off' and render.get('allow_sfx'):
        sfx_mode = None             # an SFX experiment arm switched it off
    if sfx_mode == 'full':
        boom = False                # the payoff boom is part of the edit layer's SFX plan
    return {
        'niche': niche, 'template': template,
        'caption_preset': ('edit' if edit.get('captions') else
                           render.get('caption_preset') or ('tv' if niche == 'A' else 'talk')),
        'header': bool(render.get('header', template == 'A1')) or bool(edit.get('headline')),
        # edit layer: the hook headline look (keywords coloured, one emoji), pace, flash, SFX
        'headline': bool(edit.get('headline')),
        'headline_s': edit.get('headline_s'),
        'pace': bool(edit.get('pace')),
        'flash': bool(edit.get('flash')),
        'sfx_mode': sfx_mode,
        'sfx_gain_db': float(edit.get('sfx_gain_db') or 0.0),
        'hook_qa': bool(edit.get('hook_rejects')),
        'edit_captions': bool(edit.get('captions')),
        'overlays': bool(render.get('overlays_allowed', True)) and variant.get('overlay', 'on') != 'off',
        'handle': render.get('handle'),
        # A brief-required label ("BILF Podcast" for the whole clip) is kept through the
        # QA safe re-encode, and may be drawn larger than a channel handle.
        'handle_required': bool(render.get('handle_required')),
        'context_required': bool(render.get('context_required')),
        'handle_size': render.get('handle_size'),
        # A brief-required brand watermark (logo PNG on every frame); kept through the QA re-encode.
        'watermark': render.get('watermark') or None,
        # The source's own graphics are content (a brand show's labels / cards): QA only warns.
        'burned_text_ok': bool(render.get('burned_text_ok')),
        'tail_s': tail,
        'max_hold': _MAX_HOLD_A if niche == 'A' else max(tail, _MAX_HOLD_B),
        'reaction_cut': niche == 'A',
        'shot_cap': (_PACE_CAP if edit.get('pace') else
                     render.get('shot_cap_s', _SHOT_CAP_S if niche == 'A' else _SHOT_CAP_B)),
        # talk: hold the tail over the next speaker's first word (audio faded)
        'hold_over': niche == 'B',
        'boom': boom, 'music': music,
        'names': names or None,
        # end-to-start dissolve for the replay (on unless a profile sets loop_seam False)
        'loop_seam': bool(render.get('loop_seam', True)),
        'captions_engine': captions_engine(render.get('captions_engine')),
    }


def _edit_config(render):
    """campaigns.edit_config for a render dict (every edit item off when it has no 'edit')."""
    try:
        from campaigns import edit_config
    except Exception:
        return {}
    return edit_config(render or {})


def _run_slot():
    """This run's publish slot ('HH:MM' ET) or None: PUBLISH_SLOT (cron / run_local --slot),
    else the slot the CAMPAIGN's next publish serves (read only; main does the scheduling)."""
    slot = os.environ.get('PUBLISH_SLOT', '').strip()
    if slot:
        return slot
    try:
        import campaigns
        _, prof = campaigns.get_campaign(os.environ.get('CAMPAIGN'))
        nominal = campaigns.publish_plan(prof)[1]
        if not nominal:
            return None
        import datetime
        t = datetime.datetime.strptime(nominal, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
        return t.astimezone(campaigns._ET).strftime('%H:%M')
    except Exception:
        return None


def captions_engine(setting, slot=None):
    """'ass' | 'remotion' for this render. setting: an engine name, or a per-slot dict
    ({'23:15': 'remotion', '*': 'ass'}; a slot not listed gets '*', else 'ass')."""
    if isinstance(setting, dict):
        slot = slot or _run_slot()
        setting = setting.get(slot) if slot in setting else setting.get('*')
    setting = str(setting or 'ass').strip().lower()
    return setting if setting in _CAPTIONS_ENGINES else 'ass'


def _loop_seam(cfg, words_out, total):
    """Seconds of end-to-start seam for this render (0.0: none). Output-time words."""
    if not cfg or not cfg.get('loop_seam') or total <= 0:
        return 0.0
    last = words_out[-1]['end'] if words_out else 0.0
    seam = min(_LOOP_SEAM, total - last - _SEAM_GUARD)
    return round(seam, 3) if seam >= _SEAM_MIN else 0.0


def _tail_cut(video_path, t0, t1):
    """First source camera cut in [t0, t1] (episode seconds), or None. ~0.5 s."""
    if not video_path or t1 - t0 < 0.1:
        return None
    p = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-ss', f'{t0:.3f}', '-i', video_path,
                        '-t', f'{t1 - t0:.3f}', '-an', '-vf',
                        "scale=270:-2,select='gt(scene,0.3)',showinfo", '-f', 'null', '-'],
                       capture_output=True, text=True, errors='replace')
    hits = [float(t) for t in re.findall(r'pts_time:([\d.]+)', p.stderr)]
    hits = [t0 + t for t in hits if t > 0.04]
    return hits[0] if hits else None


def _story_parts(story, all_words, silence_thresh, cfg=None, video_path=None):
    """Per story clip: list of (start, end) sub-clips in episode time (not frame-snapped)."""
    clips = story['clips']
    out = []
    for i, clip in enumerate(clips, 1):
        cs, ce = clip['start_seconds'], clip['end_seconds']
        final = i == len(clips)
        if not all_words:
            out.append([(cs, ce)])
            continue
        kw = {}
        if cfg and final:
            kw = {'tail': cfg['tail_s'], 'max_hold': cfg['max_hold'], 'keep_end_s': _KEEP_END_S,
                  'hold_over': bool(cfg.get('hold_over'))}
            if cfg['reaction_cut'] and video_path:
                words = [w for w in all_words if cs <= w['start'] < ce
                         and w['end'] <= ce + _END_SLACK]
                if words:
                    last = words[-1]['end']
                    kw['tail_cut'] = _tail_cut(video_path, last, last + cfg['tail_s'])
        out.append(_split_by_speech(cs, ce, all_words, silence_thresh, final=final, **kw))
    return out


def _held_tail(story, all_words, final_end):
    """
    (last word end, next word start) in episode time when the final clip's
    tail hold runs into the next speaker's first word (hold_over), else None.
    """
    clip = story['clips'][-1]
    cs, ce = clip['start_seconds'], clip['end_seconds']
    words = [w for w in all_words or [] if cs <= w['start'] < ce and w['end'] <= ce + _END_SLACK]
    if not words:
        return None
    last_end = words[-1]['end']
    nxt = next((w['start'] for w in all_words if w['start'] >= last_end), None)
    if nxt is None or nxt >= final_end:
        return None
    return last_end, nxt


def _remap_t(t, segments):
    """Episode time -> output time through the rendered sub-clip list (None if cut out)."""
    off = 0.0
    best = None
    for seg in segments:
        s, e = seg['start_seconds'], seg['end_seconds']
        if s - 0.3 <= t <= e + 0.3:
            cand = off + min(max(t - s, 0.0), e - s)
            if s <= t <= e:
                return cand
            best = cand if best is None else best
        off += e - s
    return best


def _looks_like_cut(video_path, prev_end, next_start, fps):
    """True when the frames either side of a join differ like a camera cut."""
    import cv2
    import numpy as np
    from reframer import _CUT_MAD
    cap = cv2.VideoCapture(video_path)
    thumbs = []
    try:
        for t in (prev_end - 1.0 / fps, next_start):
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(round(t * fps))))
            ok, fr = cap.read()
            if not ok:
                return True
            thumbs.append(cv2.resize(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), (64, 36),
                                     interpolation=cv2.INTER_AREA).astype(np.int16))
    finally:
        cap.release()
    return float(np.mean(np.abs(thumbs[0] - thumbs[1]))) > _CUT_MAD


def _level_fn(stretches, toggles):
    def level(t):
        for s, e in stretches:
            if s <= t < e:
                lv = 1.0
                for ct, l in toggles:
                    if s <= ct <= t:
                        lv = l
                return lv
        return 1.0
    return level


def _punch_plan(parts, words_out, payoff_out, crop_w, push_from, cap=_SHOT_CAP_S, any_gap=False):
    """
    Shot cap (default 4 s): inside any stretch of the output with no hard cut for > cap,
    toggle a punch-in (1.00 <-> 1.10, or 1.06 on narrow source crops) at a word
    boundary — sentence ends first, then silence-trim joins, then any word gap.
    At most one punch per 3 s, none in 0-1.2 s (calm hook), none in the 0.6 s
    before the payoff word, none inside the payoff push. any_gap (talk, where
    speech rarely pauses 80 ms) also allows a plain word boundary, last choice.

    parts: [{'off', 'dur', 'clip', 'cuts' (output s)}]. Returns (stretches, toggles).
    """
    total = parts[-1]['off'] + parts[-1]['dur']
    hard = {0.0}
    for k, p in enumerate(parts):
        hard |= set(p['cuts'])
        if k and p.get('join_cut'):
            hard.add(p['off'])
    bounds = sorted(hard) + [total]
    stretches = list(zip(bounds, bounds[1:]))
    cands = [(p['off'], 2) for k, p in enumerate(parts) if k and not p.get('join_cut')]
    for a, b in zip(words_out, words_out[1:]):
        if b['start'] - a['end'] >= 0.08:
            cands.append(((a['end'] + b['start']) / 2, 3 if _SENTENCE_END.search(a['word'].strip()) else 1))
        elif any_gap and b['start'] >= a['end'] - 0.01:
            cands.append(((a['end'] + b['start']) / 2, 0))
    hi = _PUNCH_HI if crop_w >= 900 else _PUNCH_LO
    toggles, last = [], -99.0
    for s, e in stretches:
        t, level = s, 1.0
        while e - t > cap:
            valid = [(ct, pr) for ct, pr in cands
                     if t + 1.0 <= ct <= e - 0.8 and ct >= _CALM_HOOK_S
                     and ct - last >= _PUNCH_GAP_S
                     and not (payoff_out is not None and payoff_out - 0.6 <= ct <= payoff_out + 0.05)
                     and not (push_from is not None and ct > push_from + 0.01)]
            ok = [c for c in valid if c[0] <= t + cap]
            if ok:
                # prefer the back half of the window so one punch covers more of the stretch
                late = [c for c in ok if c[0] >= t + cap / 2] or ok
                ct = max(late, key=lambda c: (c[1], c[0]))[0]
            elif valid:
                ct = min(valid)[0]          # no boundary inside 4 s: the first one after it
            else:
                break
            level = hi if level == 1.0 else 1.0
            toggles.append((round(ct, 3), level))
            last = t = ct
    return stretches, toggles


def _payoff_sentence_start(words_out, payoff_out):
    """Output time the payoff sentence starts (the word after the last sentence end before the
    payoff word), or None."""
    if payoff_out is None or not words_out:
        return None
    k = max((i for i, w in enumerate(words_out) if w['start'] <= payoff_out - 0.01), default=None)
    if k is None:
        return None
    for i in range(k - 1, -1, -1):
        if _SENTENCE_END.search(words_out[i]['word'].strip()):
            return words_out[i + 1]['start']
    return words_out[0]['start']


def _pace_candidates(parts, words_out):
    """[(t, priority)] places a reframe cut may land: sentence ends 3, phrase ends and
    silence-trim joins 2, other pauses >= 80 ms 1, any word boundary 0."""
    cands = [(p['off'], 2) for k, p in enumerate(parts) if k and not p.get('join_cut')]
    for a, b in zip(words_out, words_out[1:]):
        mid = (a['end'] + b['start']) / 2
        gap = b['start'] - a['end']
        word = a['word'].strip()
        if _SENTENCE_END.search(word):
            cands.append((mid, 3))
        elif _PHRASE_END.search(word) or gap >= 0.25:
            cands.append((mid, 2))
        elif gap >= 0.08:
            cands.append((mid, 1))
        elif gap >= -0.01:
            cands.append((mid, 0))
    return sorted(cands)


def _pace_plan(parts, words_out, payoff_out, payoff_start, crop_w, cap=_PACE_CAP, first_by=_PACE_FIRST_BY):
    """
    Edit-layer pace (EDIT_SPEC R1). Hard cuts (source camera cuts and story-clip joins
    that are real cuts) stay; reframe cuts are added so that:
      - a cut lands at the payoff sentence's start (the beat the flash marks), unless a
        hard cut is already within 0.6 s of it;
      - the first cut lands by first_by (2.0 s), not before _PACE_FIRST_FROM;
      - no framing is held longer than cap (3.5 s); spacing aims at _PACE_TARGET and never
        leaves a shot under _PACE_MIN_SHOT; no cut in the last 0.5 s before the payoff word.
    Reframe cuts prefer sentence ends, then phrase ends / joins / pauses, then any word
    boundary, then (no boundary at all) a mid-word time: the audio runs on, only the crop
    changes. Level per stretch between hard cuts starts wide (1.0) and flips to close
    (_REFRAME_HI, or _REFRAME_LO on a source crop under 900 px) and back on each reframe.
    Returns (stretches, toggles [(t, level)], marks {'payoff_cut', 'first_cut', 'hard'}).
    """
    total = parts[-1]['off'] + parts[-1]['dur']
    hard = {c for p in parts for c in p['cuts']}
    hard |= {p['off'] for k, p in enumerate(parts) if k and p.get('join_cut')}
    hard = sorted(c for c in hard if 0.05 < c < total - 0.05)
    cands = _pace_candidates(parts, words_out)
    guard = ((payoff_out - _PACE_PAYOFF_GUARD, payoff_out + 0.05) if payoff_out is not None else None)
    adds, marks = [], {'payoff_cut': None, 'first_cut': None, 'hard': hard}

    def free(t, lo_gap=_PACE_MIN_SHOT):
        events = [0.0, total] + hard + adds
        return all(abs(t - e) >= lo_gap - 1e-6 for e in events) and not (guard and guard[0] < t < guard[1])

    def best(lo, hi, aim):
        pool = [(t, pr) for t, pr in cands if lo <= t <= hi and free(t)]
        if not pool:
            return None
        return max(pool, key=lambda c: (c[1], -abs(c[0] - aim)))[0]

    # 1. the cut into the payoff sentence
    if payoff_start is not None and payoff_start >= _PACE_FIRST_FROM:
        near_hard = [c for c in hard if abs(c - payoff_start) <= 0.6]
        if near_hard:
            marks['payoff_cut'] = min(near_hard, key=lambda c: abs(c - payoff_start))
        else:
            t = best(payoff_start - 0.35, payoff_start + 0.2, payoff_start)
            if t is None and free(payoff_start - 0.04, 0.8):
                t = payoff_start - 0.04
            if t is not None:
                adds.append(round(t, 3))
                marks['payoff_cut'] = round(t, 3)
    # 2. the first cut by first_by
    if not any(0.0 < e <= first_by for e in hard + adds):
        t = best(_PACE_FIRST_FROM, first_by, 1.6)
        if t is None:
            t = next((x for x in (first_by - 0.1, 1.5, 1.2) if free(x, 0.8)), None)
        if t is not None:
            adds.append(round(t, 3))
    # 3. no framing held longer than cap
    stuck = set()
    while True:
        events = sorted({0.0, total, *hard, *adds})
        long_gap = next(((a, b) for a, b in zip(events, events[1:]) if b - a > cap + 1e-6 and (a, b) not in stuck),
                        None)
        if not long_gap:
            break
        a, b = long_gap
        lo, hi = a + _PACE_MIN_SHOT, min(a + cap, b - _PACE_MIN_SHOT)
        t = best(lo, hi, a + _PACE_TARGET) if lo <= hi else None
        if t is None:
            # nothing on a boundary: the cap wins (a mid-word reframe; the audio runs on)
            t = a + min(cap, (b - a) / 2.0) if (b - a) < 2 * cap else a + _PACE_TARGET
            if guard and guard[0] < t < guard[1]:
                t = guard[0] - 0.05
            if t - a < 0.5 or b - t < 0.5:
                stuck.add((a, b))           # nothing fits in this one; the others still get filled
                continue
        adds.append(round(t, 3))
    adds = sorted(set(adds))
    events_after0 = sorted(hard + adds)
    marks['first_cut'] = events_after0[0] if events_after0 else None
    hi_level = _REFRAME_HI if crop_w >= 900 else _REFRAME_LO
    bounds = [0.0] + hard + [total]
    stretches = list(zip(bounds, bounds[1:]))
    toggles = []
    for s, e in stretches:
        level = 1.0
        for t in adds:
            if s < t < e:
                level = hi_level if level == 1.0 else 1.0
                toggles.append((t, level))
    return stretches, toggles, marks


def _flash_times(marks, parts, payoff_out, total):
    """Edit-layer flash cuts (EDIT_SPEC R6): the cut into the payoff (the biggest beat change),
    plus, on a clip >= 10 s, one story-clip join (a time jump) at least 3 s away. At most 2."""
    out = []
    t1 = marks.get('payoff_cut')
    if t1 is None and payoff_out is not None:
        before = [c for c in marks.get('hard') or [] if payoff_out - 3.0 <= c <= payoff_out]
        t1 = max(before) if before else None
    if t1 is not None and 0.5 <= t1 <= total - 0.3:
        out.append(t1)
    if total >= _FLASH_SECOND_MIN_S:
        joins = [p['off'] for k, p in enumerate(parts) if k and p.get('join_cut') and p['clip'] != parts[k - 1]['clip']]
        joins = [t for t in joins if 1.0 <= t <= total - 1.0 and all(abs(t - o) >= _FLASH_GAP_S for o in out)]
        if joins:
            out.append(joins[0])
    return [round(t, 3) for t in sorted(out)[:2]]


def _flash_frames(part, flashes):
    """{frame: opacity} of the flash wash on one part (frame centres, the cut frame at the peak)."""
    fps = part['fps']
    n = int(round(part['dur'] * fps))
    out = {}
    for T in flashes:
        if not (part['off'] - _FLASH_FALL - 0.1 <= T <= part['off'] + part['dur'] + _FLASH_RISE + 0.1):
            continue
        for f in range(n):
            k = math.floor((part['off'] + (f + 0.5) / fps - T) * fps)
            if k >= 0:
                a = _FLASH_PEAK * (1.0 - k / (fps * _FLASH_FALL))
            else:
                a = _FLASH_PEAK * (1.0 + k / (fps * _FLASH_RISE))
            if a > 0.01:
                out[f] = round(max(out.get(f, 0.0), a), 3)
    return out


def _speech_spans(words_out, pad=_SFX_SPEECH_PAD):
    return [(w['start'] - pad, w['end'] + pad) for w in words_out]


def _speech_gap_at(t, words_out):
    """Length of the speech pause containing t (0.0 when t is inside a word)."""
    prev_end, nxt = 0.0, None
    for w in words_out:
        if w['start'] <= t <= w['end']:
            return 0.0
        if w['end'] <= t:
            prev_end = max(prev_end, w['end'])
        elif nxt is None or w['start'] < nxt:
            nxt = w['start']
    return (nxt if nxt is not None else t + 1.0) - prev_end


def _sfx_plan(cfg, total, words_out, cuts, payoff_out, flashes, headline):
    """
    Edit-layer SFX events (EDIT_SPEC R5), output seconds: [{'t' (where the file's peak lands),
    'kind', 'why'}]. 'full': a pop on the headline at frame 0, a low boom 40 ms after the payoff
    word, whooshes on cuts; 'cuts' (LADbible light): whooshes on cuts only. About 1.6 (full) /
    1.2 (cuts) per 10 s, never two within 3 s (the frame-0 pop aside), cut SFX only in a speech
    pause of >= 80 ms; a flash cut gets the transition swoosh.
    """
    mode = cfg.get('sfx_mode')
    if not mode or total <= 0:
        return []
    budget = max(1, int(round(_SFX_RATE[mode] * total / 10.0)))
    budget = min(budget, int(3 * total / 10.0) or 1)
    out = []
    if mode == 'full' and headline:
        out.append({'t': 0.0, 'kind': 'pop', 'why': 'headline'})
    if mode == 'full' and payoff_out is not None and payoff_out + 0.3 < total:
        out.append({'t': round(payoff_out + 0.04, 3), 'kind': 'boom', 'why': 'payoff'})
    counted = lambda: sum(1 for e in out if e['why'] != 'headline')
    flash_set = {round(f, 3) for f in flashes}
    ranked = sorted(((c, round(c, 3) in flash_set) for c in cuts if 0.5 <= c <= total - 0.3),
                    key=lambda x: (not x[1], -_speech_gap_at(x[0], words_out)))
    k = 0
    for c, is_flash in ranked:
        if counted() >= budget:
            break
        if not is_flash and _speech_gap_at(c, words_out) < _SFX_MIN_SPEECH_GAP:
            continue
        # the flash swoosh leads into the payoff boom (a pair, >= 0.8 s apart); otherwise 3 s apart
        if any(abs(c - e['t']) < (_SFX_PAIR_GAP_S if is_flash and e['kind'] == 'boom' else _SFX_GAP_S)
               for e in out if e['why'] != 'headline'):
            continue
        kind = 'flash' if is_flash else ('whoosh', 'whoosh2')[k % 2]
        k += 0 if is_flash else 1
        out.append({'t': round(c, 3), 'kind': kind, 'why': 'flash cut' if is_flash else 'cut'})
    return sorted(out, key=lambda e: e['t'])


def _read_wav(path):
    """(float32 [n, 2] in -1..1, rate) of a 16-bit PCM WAV (clipper's own _build_audio output)."""
    import wave
    import numpy as np
    with wave.open(path, 'rb') as w:
        ch, rate, width, n = w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError(f'{path}: {8 * width}-bit WAV, expected 16-bit')
    a = np.frombuffer(raw, np.int16).astype(np.float32).reshape(-1, ch) / 32768.0
    if ch == 1:
        a = np.repeat(a, 2, axis=1)
    return a[:, :2], rate


def _write_wav(path, a, rate):
    import wave
    import numpy as np
    pcm = (np.clip(a, -1.0, 1.0) * 32767.0).astype(np.int16)
    tmp = path + '.tmp.wav'
    with wave.open(tmp, 'wb') as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)


def _load_sfx(name, rate):
    """A kit file as float32 [n, 2] at `rate` (ffmpeg decodes the 24-bit WAV)."""
    import numpy as np
    path = os.path.join(_SFX_DIR, name)
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ac', '2', '-ar', str(rate), '-f', 'f32le', '-'],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise FileNotFoundError(f'SFX file unreadable: {path}')
    return np.frombuffer(p.stdout, np.float32).reshape(-1, 2).copy()


def _rms_db(a, rate, win=0.05):
    """dB of the 50 ms RMS frames of a [n, 2] signal (mono sum)."""
    import numpy as np
    m = a.mean(axis=1)
    n = max(1, int(rate * win))
    k = len(m) // n
    if k == 0:
        return np.array([20 * math.log10(max(1e-9, float(np.sqrt(np.mean(m * m)))))])
    fr = m[:k * n].reshape(k, n)
    return 20 * np.log10(np.maximum(1e-9, np.sqrt((fr * fr).mean(axis=1))))


def voice_level_db(audio, rate, words_out):
    """Median 50 ms RMS (dB) over the spoken words: the reference every SFX / bed level is set against."""
    import numpy as np
    db = _rms_db(audio, rate)
    win = 0.05
    vals = [db[i] for w in words_out for i in range(int(w['start'] / win), int(w['end'] / win))
            if 0 <= i < len(db)]
    vals = [v for v in vals if v > -60]
    return float(np.median(vals)) if vals else float(np.percentile(db, 75))


def _speech_mask(n, rate, words_out, pad=_SFX_SPEECH_PAD, ramp=0.02):
    """[n] 0..1: 1 while a word is spoken (+-pad), with ramp-s edges."""
    import numpy as np
    m = np.zeros(n, np.float32)
    for s, e in _speech_spans(words_out, pad):
        m[max(0, int(s * rate)):max(0, min(n, int(e * rate)))] = 1.0
    r = max(1, int(ramp * rate))
    if r > 1 and n > r:
        m = np.convolve(m, np.ones(r, np.float32) / r, mode='same')
    return np.clip(m, 0.0, 1.0)


def _sfx_clip(kind, rate):
    """(trimmed float32 [n, 2], peak offset s, peak 50 ms RMS dB) of one SFX kind."""
    import numpy as np
    name, _, before, after, fade = _SFX[kind]
    a = _load_sfx(name, rate)
    env = np.abs(a).mean(axis=1)
    k = max(1, int(0.01 * rate))
    smooth = np.convolve(env, np.ones(k) / k, mode='same')
    peak = int(np.argmax(smooth)) if kind != 'boom' else int(np.argmax(env > env.max() * 0.2))
    s, e = max(0, peak - int(before * rate)), min(len(a), peak + int(after * rate))
    clip = a[s:e].copy()
    fi = min(len(clip), max(1, int(0.005 * rate)))
    clip[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)[:, None]
    fo = min(len(clip), max(1, int(fade * rate)))
    clip[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)[:, None]
    return clip, (peak - s) / rate, float(_rms_db(clip, rate, 0.05).max())


def mix_sfx(audio, rate, events, words_out, gain_db=0.0):
    """
    The SFX events mixed UNDER the original audio (which is kept as it is). Each SFX is set to
    its level relative to the voice (median speech RMS) plus gain_db, and while a word is
    spoken it is ducked to at most _SFX_UNDER_SPEECH dB re voice, so it never masks a word.
    Returns (mixed audio, [{'t', 'kind', 'start', 'level_db', 'ducked_db'}]).
    """
    import numpy as np
    if not events:
        return audio, []
    voice = voice_level_db(audio, rate, words_out)
    mask = _speech_mask(len(audio), rate, words_out)
    track = np.zeros_like(audio)
    log = []
    cache = {}
    for ev in events:
        kind = ev['kind']
        if kind not in cache:
            cache[kind] = _sfx_clip(kind, rate)
        clip, peak_off, peak_db = cache[kind]
        level = _SFX[kind][1] + gain_db
        g = 10 ** ((voice + level - peak_db) / 20.0)
        duck = min(0.0, _SFX_UNDER_SPEECH - level)          # extra attenuation under a word
        start = int(round((ev['t'] - peak_off) * rate))
        s0, s1 = max(0, start), min(len(audio), start + len(clip))
        if s1 <= s0:
            continue
        seg = clip[s0 - start:s1 - start] * g
        dk = 1.0 - mask[s0:s1] * (1.0 - 10 ** (duck / 20.0))
        track[s0:s1] += seg * dk[:, None]
        log.append({'t': ev['t'], 'kind': kind, 'why': ev.get('why'), 'start': round(s0 / rate, 3),
                    'level_db': round(level, 1), 'ducked_db': round(duck, 1)})
    out = audio + track
    peak = float(np.abs(out).max()) if len(out) else 0.0
    if peak > 0.99:
        out *= 0.99 / peak          # the final loudnorm sets the level; never clip here
    return out, log


def _add_sfx(audio_wav, events, words_out, gain_db=0.0):
    """mix_sfx on clipper's audio WAV in place. Returns the event log ([] when nothing was mixed)."""
    if not events:
        return []
    audio, rate = _read_wav(audio_wav)
    mixed, log = mix_sfx(audio, rate, events, words_out, gain_db)
    if log:
        _write_wav(audio_wav, mixed, rate)
    return log


def duck_bed(voice, bed, rate, words_out, bed_db=_BED_DB, duck_db=_BED_DUCK_DB, attack=_BED_ATTACK,
             release=_BED_RELEASE, fade_in=_BED_FADE_IN, fade_out=_BED_FADE_OUT):
    """
    The music-bed mixer (EDIT_SPEC R4): bed (looped / cut to the voice's length) at bed_db
    relative to the voice's median speech RMS, ducked a further duck_db while speech is
    active (sidechain on the word timings: attack / release time constants), faded in from
    frame 0 and out at the end. The voice is passed through untouched. Returns the mix.
    """
    import numpy as np
    n = len(voice)
    if not len(bed):
        return voice.copy()
    reps = -(-n // len(bed))
    bed = np.tile(bed, (reps, 1))[:n].astype(np.float32)
    v_db = voice_level_db(voice, rate, words_out)
    b_db = float(np.median(_rms_db(bed, rate)))
    g = 10 ** ((v_db + bed_db - b_db) / 20.0)
    # sidechain key = the word timings, followed at a 1 kHz control rate (attack / release)
    ctl = 1000
    m = max(1, int(math.ceil(n * ctl / rate)))
    key = np.zeros(m, np.float32)
    for w in words_out:
        key[max(0, int(w['start'] * ctl)):max(0, min(m, int(w['end'] * ctl)))] = 1.0
    a_c = math.exp(-1.0 / max(1.0, attack * ctl))
    r_c = math.exp(-1.0 / max(1.0, release * ctl))
    env_c = np.empty(m, np.float32)
    cur = 0.0
    for i in range(m):
        c = a_c if key[i] > cur else r_c
        cur = c * cur + (1.0 - c) * float(key[i])
        env_c[i] = cur
    env = np.interp(np.arange(n) * ctl / rate, np.arange(m), env_c).astype(np.float32)
    gain = g * (10 ** (-duck_db * env / 20.0))
    fi, fo = int(fade_in * rate), int(fade_out * rate)
    if fi:
        gain[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
    if fo:
        gain[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
    out = voice + bed * gain[:, None]
    peak = float(np.abs(out).max())
    return out * (0.99 / peak) if peak > 0.99 else out


def licensed_tracks(path=_MUSIC_LICENSES):
    """Music tracks cleared for use: music/LICENSES.json entries [{'file', 'license', ...}] only.
    The two files in music/ have no provenance and are never used (EDIT_SPEC: licensed only)."""
    import json
    try:
        with open(path, encoding='utf-8') as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    root = os.path.dirname(path)
    return [os.path.join(root, i['file']) for i in items if isinstance(i, dict) and i.get('file')
            and i.get('license') and os.path.exists(os.path.join(root, i['file']))]


def _add_payoff_boom(audio_wav, t_out):
    """
    Flagged experiment (render sfx.payoff_boom, default OFF): one synthesised
    sub boom (SFX spec §6 recipe) 40 ms after the payoff word, peak RMS at the
    dialogue reference (integrated LUFS + 6) + 3 dB, mixed before loudnorm.
    """
    p = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', audio_wav, '-af',
                        'loudnorm=print_format=json', '-f', 'null', '-'],
                       capture_output=True, text=True, errors='replace')
    m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', p.stderr)
    if not m:
        return False
    gain = (float(m.group(1)) + 6.0 + 3.0) - (-5.1)
    ms = int(max(0.0, t_out + 0.04) * 1000)
    boom = ("aevalsrc='min(1\\,t*400)*sin(2*PI*(44*t+46*(1-exp(-8*t))/8))*exp(-4.5*t)':s=48000:d=0.8,"
            "asoftclip=type=tanh:param=1,volume=4dB,asoftclip=type=tanh,lowpass=f=2500,"
            "afade=t=out:st=0.65:d=0.15,aformat=channel_layouts=stereo")
    tmp = audio_wav + '.boom.wav'
    subprocess.run(['ffmpeg', '-y', '-i', audio_wav, '-f', 'lavfi', '-i', boom, '-filter_complex',
                    f"[1:a]adelay={ms}|{ms},volume={gain:.1f}dB[b];"
                    f"[0:a][b]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89:level=disabled[a]",
                    '-map', '[a]', '-c:a', 'pcm_s16le', tmp], check=True, capture_output=True)
    os.replace(tmp, audio_wav)
    return True


def _music_bed(audio_wav, cfg, words_out=None):
    """
    Podcast / talk music bed (EDIT_SPEC R4) through duck_bed: ~16 dB under the voice, a
    further 4 dB sidechain duck while speech is active, 0.3 s fade in, 0.5 s out. Runs only
    when the profile allows music (allow_music, today False everywhere) AND a track is listed
    in music/LICENSES.json; otherwise it is logged and skipped and the audio is untouched.
    """
    if not cfg or cfg.get('music') in (None, 'off'):
        return False
    tracks = licensed_tracks()
    if not tracks:
        print(f"  Music bed '{cfg['music']}' requested but no licensed track is listed in music/LICENSES.json — skipped")
        return False
    import numpy as np
    voice, rate = _read_wav(audio_wav)
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', tracks[0], '-ac', '2', '-ar', str(rate), '-f', 'f32le', '-'],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        print(f"  Music bed skipped: {tracks[0]} unreadable")
        return False
    bed = np.frombuffer(p.stdout, np.float32).reshape(-1, 2)
    _write_wav(audio_wav, duck_bed(voice, bed, rate, words_out or []), rate)
    print(f"  Music bed: {os.path.basename(tracks[0])} at {_BED_DB:g} dB re voice, {_BED_DUCK_DB:g} dB duck")
    return True


def create_story_short(video_path, story, output_dir, episode_name,
                        pipeline=None, all_words=None, silence_thresh=_SILENCE_THRESH,
                        wide_fallback=False, render=None, qa_retry=True):
    """
    Cuts every clip in a story arc, reframes each to 9:16, concatenates into
    one compiled video, then burns viral-style captions plus the persistent
    hook banner if word timestamps are provided.

    When all_words is supplied, each story clip is first split into speech
    sub-clips (long laugh-track / dead-air gaps shortened) before reframing.
    Every sub-clip is snapped to the frame grid; audio is cut once from the
    source with short fades at each join and normalised in the final encode.

    render: the profile's render dict (BUILD_PLAN §3). None keeps today's
    look. With it: template A1 / A2 / B1, caption preset, the originality
    layer from story['header'|'context_line'|'closing_line'|'annotations'],
    niche tail hold with a reaction cut, the 4 s shot cap and a payoff push.
    Every render gets a QA lint written to <out>.qa.json.

    qa_retry: whether this render may spend the run's one QA retry: the safe
    re-encode (overlay / loudness fails) or, for a layout-breaking fail (black
    bars, static band), one re-render of every part as the whole bar-cropped
    picture over a blurred fill. main passes False once a run has used it.
    <out>.qa.json records which retry ran ('qa_retry').
    """
    from reframer import reframe_clip, analyse_clip, probe_fps, HOOK_BAR_HEIGHT, A1_WINDOW
    from captioner import burn_captions, _remap_words

    t_start = time.time()
    cfg = _render_config(render, story, wide_fallback)
    clips = story['clips']
    title = story['storyline_title']
    score = story['score']
    _, fps = probe_fps(video_path)
    top_reserved = HOOK_BAR_HEIGHT if (cfg is None and story.get('hook_text')) else 0

    parts_dir = os.path.join(output_dir, '_parts')
    os.makedirs(parts_dir, exist_ok=True)

    part_paths    = []
    expanded_clips = []   # sub-clips passed to captioner for timestamp remapping
    audio_path  = os.path.join(parts_dir, '_audio.wav')
    merged_path = os.path.join(output_dir, '_merged.mkv')
    try:
        all_subs = _story_parts(story, all_words, silence_thresh, cfg, video_path)
        held = None
        if cfg and cfg.get('hold_over') and all_words and all_subs and all_subs[-1]:
            held = _held_tail(story, all_words, all_subs[-1][-1][1])
            if held:
                # the next speaker's words in the held tail are neither captioned nor counted
                all_words = [w for w in all_words if not held[1] <= w['start'] < all_subs[-1][-1][1] + 0.01]
        jobs = []          # (clip index, sub-clip index, start, end)
        for i, (clip, subs) in enumerate(zip(clips, all_subs), 1):
            cs, ce = clip['start_seconds'], clip['end_seconds']
            sub_clips = [_snap_to_frames(s, e, fps) for s, e in subs]
            kept = sum(e - s for s, e in sub_clips)
            delta = (ce - cs) - kept
            suffix = f" ({-delta:+.1f}s after trimming)" if abs(delta) > 0.05 else ""
            print(f"  Part {i}/{len(clips)}: [{clip['label']}] "
                  f"{cs:.1f}s - {ce:.1f}s  =>  {len(sub_clips)} sub-clip(s){suffix}")
            for j, (sc_start, sc_end) in enumerate(sub_clips):
                jobs.append((i, j, sc_start, sc_end))
                expanded_clips.append({'start_seconds': sc_start, 'end_seconds': sc_end})

        stats, plan, render_cfg = [], {}, None
        if cfg is None:
            for i, j, sc_start, sc_end in jobs:
                part_path = os.path.join(parts_dir, f"part_{i:02d}_{j:02d}.mp4")
                stats.append(reframe_clip(video_path, sc_start, sc_end, part_path, pipeline=pipeline,
                                          wide_fallback=wide_fallback, top_reserved=top_reserved,
                                          zoom=_PUNCH_IN if j % 2 else 1.0,
                                          push_in=(sc_end - sc_start) > _PUSH_IN_MIN_S))
                part_paths.append(part_path)
        else:
            plan = _plan_render(video_path, jobs, expanded_clips, story, all_words, cfg,
                                pipeline, wide_fallback, analyse_clip)
            for k, (i, j, sc_start, sc_end) in enumerate(jobs):
                part_path = os.path.join(parts_dir, f"part_{i:02d}_{j:02d}.mp4")
                stats.append(reframe_clip(video_path, sc_start, sc_end, part_path,
                                          layout=cfg['template'], analysis=plan['analyses'][k],
                                          zoom_events=plan['zoom_events'][k],
                                          push=plan['push'] if k == plan['push_part'] else None,
                                          **_edit_part_kwargs(plan, k)))
                part_paths.append(part_path)

        print(f"  Merging {len(part_paths)} parts...")
        total = sum(c['end_seconds'] - c['start_seconds'] for c in expanded_clips)
        seam = _loop_seam(cfg, _remap_words(all_words or [], expanded_clips), total)
        plan['loop_seam'] = seam or None
        fade = None
        if held:
            last_end, nxt = held
            f0 = max(last_end + 0.02, nxt - _HOLD_FADE_LEAD) - expanded_clips[-1]['start_seconds']
            fade = (max(0.0, f0), _HOLD_FADE)
            print(f"  Tail held over the next speaker: audio fades {f0:.2f}s into the last part")
        elif seam and expanded_clips:
            last_d = expanded_clips[-1]['end_seconds'] - expanded_clips[-1]['start_seconds']
            fade = (max(0.0, last_d - seam), seam)
        if seam:
            print(f"  Loop seam: the last {seam:.2f}s dissolves into the opening frame")
        _build_audio(video_path, expanded_clips, audio_path, fade_out=fade)
        if cfg and cfg['boom'] and plan.get('payoff_out') is not None:
            if _add_payoff_boom(audio_path, plan['payoff_out']):
                print(f"  Payoff boom at {plan['payoff_out']:.2f}s (experiment flag)")
        words_mix = _remap_words(all_words or [], expanded_clips)
        if cfg and cfg.get('sfx_mode'):
            events = _sfx_plan(cfg, total, words_mix, plan.get('edit_cuts') or [], plan.get('payoff_out'),
                               plan.get('flashes') or [], bool(cfg.get('headline') and _headline_text(story, cfg)))
            try:
                plan['sfx'] = _add_sfx(audio_path, events, words_mix, cfg.get('sfx_gain_db', 0.0))
            except Exception as exc:          # an SFX problem never breaks a render
                print(f"  SFX skipped ({exc})")
                plan['sfx'] = []
            if plan['sfx']:
                print(f"  SFX: {[(e['kind'], e['t']) for e in plan['sfx']]}")
        _music_bed(audio_path, cfg, words_mix)
        _merge_clips(part_paths, merged_path, audio_path)

        out_path = os.path.join(output_dir, f"{episode_name}_{title}_s{score}.mp4")
        print("  Burning captions..." if all_words else "  Finishing (no captions)...")
        if cfg is None:
            enc = burn_captions(merged_path, out_path, all_words or [], expanded_clips,
                                hook_text=story.get('hook_text'), hook_emoji=story.get('hook_emoji'))
        else:
            words_out = _remap_words(all_words or [], expanded_clips)
            render_cfg = _caption_settings(cfg, stats)
            ov = _overlay_settings(story, cfg, expanded_clips, words_out)
            ov['loop_seam'] = plan.get('loop_seam')
            enc = burn_captions(merged_path, out_path, all_words or [], expanded_clips,
                                render=render_cfg, overlays=ov)
        def layout_fallback():
            """Re-render every part as the whole picture (bars cropped, looser rule) over a
            blurred fill, re-merge and burn with the same settings. No face pass is re-run."""
            for k, (i, j, sc_start, sc_end) in enumerate(jobs):
                reframe_clip(video_path, sc_start, sc_end, part_paths[k], wide_fallback=wide_fallback,
                             top_reserved=top_reserved, layout=cfg['template'] if cfg else 'legacy',
                             analysis=plan['analyses'][k] if plan.get('analyses') else None,
                             force_wide=True, **({'flash': plan['flash_frames'][k]}
                                                 if plan.get('flash_frames') else {}))
            _merge_clips(part_paths, merged_path, audio_path)
            if cfg is None:
                return burn_captions(merged_path, out_path, all_words or [], expanded_clips,
                                     hook_text=story.get('hook_text'), hook_emoji=story.get('hook_emoji'))
            return burn_captions(merged_path, out_path, all_words or [], expanded_clips,
                                 render=render_cfg, overlays=ov)

        letterbox = [lb for st in stats for lb in (st or {}).get('letterbox') or []]
        if letterbox:
            print(f"  Letterbox bars cropped in {len(letterbox)} shot(s)")
        _run_qa(out_path, merged_path, cfg, story, all_words, expanded_clips, jobs, enc or {},
                plan, time.time() - t_start, burn_captions, render_cfg if cfg else None,
                layout_fallback=layout_fallback, retry=qa_retry, letterbox=letterbox)
        return out_path

    finally:
        for p in part_paths + [audio_path, merged_path]:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(parts_dir)
        except OSError:
            pass


def _plan_render(video_path, jobs, segments, story, all_words, cfg, pipeline,
                 wide_fallback, analyse_clip):
    """Detection pass for every part, then the zoom plan across the whole Short."""
    from captioner import _remap_words
    analyses, parts, off = [], [], 0.0
    for k, (i, j, s, e) in enumerate(jobs):
        an = analyse_clip(video_path, s, e, pipeline, wide_fallback)
        analyses.append(an)
        # a join between story clips is a hard cut unless both sides are the
        # same camera setup (then it plays as a jump cut, like a silence trim)
        join_cut = bool(k) and i != jobs[k - 1][0] and _looks_like_cut(video_path, jobs[k - 1][3], s, an['fps'])
        parts.append({'off': off, 'dur': e - s, 'clip': i, 'fps': an['fps'], 'join_cut': join_cut,
                      'cuts': [off + c / an['fps'] for c in an['cuts']]})
        off += e - s
    words_out = _remap_words(all_words or [], segments)
    payoff_out = None
    if story.get('payoff_t') is not None:
        payoff_out = _remap_t(float(story['payoff_t']), segments)
    if payoff_out is None and words_out:
        last_clip = jobs[-1][0]
        first_k = next(k for k, job in enumerate(jobs) if job[0] == last_clip)
        tail_words = [w for w in words_out if w['start'] >= parts[first_k]['off']]
        payoff_out = (tail_words or words_out)[-1]['end']

    crop_w = 0.0
    if analyses:
        an = analyses[0]
        aspect = 1080 / {'A1': 1350, 'B1H': 1620}.get(cfg['template'], 1920)
        crop_w = min(an['src_w'], an['src_h'] * aspect)
    total = parts[-1]['off'] + parts[-1]['dur'] if parts else 0.0

    # edit layer: the pace plan (reframe cuts) comes first, the payoff push follows it
    marks, pace = {'hard': sorted({c for p in parts for c in p['cuts']} |
                                  {p['off'] for k, p in enumerate(parts) if k and p.get('join_cut')})}, None
    payoff_start = _payoff_sentence_start(words_out, payoff_out) if cfg.get('pace') else None
    if cfg.get('pace') and parts:
        pace = _pace_plan(parts, words_out, payoff_out, payoff_start, crop_w, cap=float(cfg['shot_cap']))
        marks = pace[2]

    # payoff push: the part holding the payoff word, from its last camera cut on
    push_part, push, push_from = None, None, None
    if payoff_out is not None:
        push_part = next((k for k, p in enumerate(parts)
                          if p['off'] <= payoff_out < p['off'] + p['dur']), len(parts) - 1)
        p = parts[push_part]
        start = max([p['off']] + [c for c in p['cuts'] if c < payoff_out])
        if cfg['niche'] == 'B' or cfg.get('pace'):
            # a one-camera answer has no cut to start from: push over the last beat only
            start = max(start, min(payoff_out - _PUSH_B_LEAD, p['off'] + p['dur'] - _PUSH_MIN_S))
        if p['off'] + p['dur'] - start >= _PUSH_MIN_S:
            push_from = start
            push = (int(round((start - p['off']) * p['fps'])), _PUSH_PAYOFF)

    toggles = []
    if pace:
        stretches, toggles = pace[0], pace[1]
        level = _level_fn(stretches, toggles)
    elif cfg['shot_cap']:
        stretches, toggles = _punch_plan(parts, words_out, payoff_out, crop_w, push_from,
                                         cap=float(cfg['shot_cap']), any_gap=cfg['niche'] == 'B')
        level = _level_fn(stretches, toggles)
    else:
        level = lambda t: 1.0
    zoom_events = []
    for p in parts:
        n = int(round(p['dur'] * p['fps']))
        ev, cur = [], None
        for f in range(n):
            lv = level(p['off'] + (f + 0.5) / p['fps'])
            if lv != cur:
                ev.append((f, lv))
                cur = lv
        zoom_events.append(ev)
    if toggles:
        print(f"  {'Pace: reframe cuts' if pace else 'Shot cap: punch-in toggles'} at {[t for t, _ in toggles]}")
    flashes = _flash_times(marks, parts, payoff_out, total) if cfg.get('flash') and parts else []
    if flashes:
        print(f"  Flash transition at {flashes}")
    return {'analyses': analyses, 'zoom_events': zoom_events, 'push': push,
            'push_part': push_part, 'payoff_out': payoff_out, 'toggles': toggles,
            'parts': parts, 'flashes': flashes,
            'flash_frames': [_flash_frames(p, flashes) for p in parts],
            'edit_cuts': sorted(set(marks.get('hard') or []) | {t for t, _ in toggles}),
            'first_cut': marks.get('first_cut') if pace else None,
            'payoff_cut': marks.get('payoff_cut'),
            'zoom_max': _REFRAME_ZOOM_MAX if pace else None}


def _edit_part_kwargs(plan, k):
    """reframe_clip kwargs of the edit layer for part k (flash wash, reframe zoom cap)."""
    kw = {}
    if plan.get('flash_frames') and plan['flash_frames'][k]:
        kw['flash'] = plan['flash_frames'][k]
    if plan.get('zoom_max'):
        kw['zoom_max'] = plan['zoom_max']
    return kw


def _headline_text(story, cfg):
    return (story.get('header') or story.get('hook_text')) if cfg.get('header') else None


def _caption_settings(cfg, stats):
    """Caption template settings; the face clamp keeps captions off faces."""
    from captioner import _TEMPLATE_CAPTION_Y, _CAP_CLAMP, _PRESETS
    y = _TEMPLATE_CAPTION_Y.get(cfg['template'], 1160)
    cap = _PRESETS.get(cfg['caption_preset'], _PRESETS['tv'])['cap']
    per_t = {}
    for k, st in enumerate(stats):
        for t, x0, y0, x1, y1 in (st or {}).get('faces', []):
            key = (k, round(t, 2))
            if key not in per_t or y1 - y0 > per_t[key][1] - per_t[key][0]:
                per_t[key] = (y0, y1)
    if per_t:
        bottoms = sorted(b for _, b in per_t.values())
        face_bottom = bottoms[len(bottoms) // 2]
        if face_bottom > y - cap / 2 - 40:
            y = int(min(_CAP_CLAMP[1], max(_CAP_CLAMP[0], face_bottom + 110)))
            print(f"  Captions moved to y {y} (face bottom at {face_bottom:.0f})")
    return {'template': cfg['template'], 'caption_preset': cfg['caption_preset'],
            'caption_y': y, 'names': cfg['names'], 'handle': cfg['handle'],
            'handle_size': cfg.get('handle_size'), 'watermark': cfg.get('watermark'),
            'captions_engine': cfg.get('captions_engine', 'ass'),
            'headline': bool(cfg.get('headline'))}


def _overlay_settings(story, cfg, segments, words_out):
    """Header / context card / closing line / stage directions for this render."""
    header = _headline_text(story, cfg)
    ov = {'header': header,
          'last_word_end': words_out[-1]['end'] if words_out else None}
    if header and cfg.get('headline'):
        # edit layer headline: 1-2 coloured keywords, an optional emoji, up for headline_s (None: whole clip)
        ov['header_keywords'] = list(story.get('header_keywords') or [])
        ov['header_emoji'] = str(story.get('hook_emoji') or '').strip()
        ov['header_until'] = cfg.get('headline_s')
    context = story.get('context_line')
    if cfg['template'] == 'A2' and not context:
        context = story.get('header') or story.get('hook_text')   # A2's intro card
    if context and (cfg['overlays'] or cfg['template'] == 'A2'):
        ov['context'] = context
    if cfg['overlays']:
        ov['closing'] = story.get('closing_line')
        tags = []
        for a in story.get('annotations') or []:
            try:
                t = _remap_t(float(a['t']), segments)
            except (KeyError, TypeError, ValueError):
                continue
            if t is not None and a.get('text'):
                tags.append((t, a['text']))
        ov['tags'] = sorted(tags)
    return ov


def _run_qa(out_path, merged_path, cfg, story, all_words, segments, jobs, enc, plan,
            seconds, burn_captions, render_cfg=None, layout_fallback=None, retry=True, letterbox=None):
    """
    Lint the finished Short (<out>.qa.json), then at most ONE retry (retry=False: none):
      - a layout-breaking fail (render_qa.LAYOUT_FAILS: black bars, static band) re-renders
        every part through layout_fallback() (whole picture over a blurred fill) and re-lints;
      - otherwise an overlay / loudness fail gets the safe re-encode without overlays.
    A layout fail that survives stays in hard_fail: main never uploads that render.
    """
    try:
        import render_qa
        from captioner import _remap_words
    except Exception as exc:
        print(f"  QA skipped ({exc})")
        return None
    words_out = _remap_words(all_words or [], segments)
    off, joins = 0.0, []
    for seg in segments:
        if off > 0:
            joins.append(off)
        off += seg['end_seconds'] - seg['start_seconds']
    ctx = {'template': cfg['template'] if cfg else 'legacy',
           'niche': cfg['niche'] if cfg else None,
           'words': words_out, 'joins': joins, 'clean_video': merged_path,
           'normalization_type': enc.get('normalization_type'),
           'render_seconds': round(seconds, 1), 'variant': story.get('variant'),
           'payoff_out': plan.get('payoff_out'), 'punches': plan.get('toggles'),
           'allowed_boxes': enc.get('allowed_boxes'),
           'watermark_box': enc.get('watermark_box'),
           'letterbox': letterbox or None,
           'loop_seam': plan.get('loop_seam'),
           'captions_engine': enc.get('captions_engine'), 'remotion_fallback': enc.get('remotion_fallback')}
    if cfg:
        ctx['caption_y'] = enc.get('caption_y')
        ctx['burned_text_ok'] = cfg.get('burned_text_ok', False)
        if cfg['template'] == 'A1':
            from reframer import A1_WINDOW
            ctx['live_rows'] = ctx['window'] = (A1_WINDOW[0], A1_WINDOW[0] + A1_WINDOW[1])
        elif cfg['template'] == 'B1H':
            from reframer import B1H_WINDOW
            ctx['live_rows'] = ctx['window'] = (B1H_WINDOW[0], B1H_WINDOW[0] + B1H_WINDOW[1])
        # edit layer: what QA checks it against (first cut, flash frames, the hook-frame fails)
        if cfg.get('pace') or cfg.get('flash') or cfg.get('sfx_mode') or cfg.get('hook_qa'):
            ctx['edit'] = {'pace': bool(cfg.get('pace')), 'first_cut': plan.get('first_cut'),
                           'edit_cuts': plan.get('edit_cuts'), 'flashes': plan.get('flashes') or [],
                           'sfx': plan.get('sfx') or [], 'hook_qa': bool(cfg.get('hook_qa')),
                           'headline': bool(cfg.get('headline'))}
    elif story.get('hook_text'):
        ctx['live_rows'] = ctx['window'] = (320, 1920)
    try:
        t = time.time()
        qa = render_qa.run(out_path, ctx)
        layout = [f for f in qa['hard_fail'] if f.split(':')[0] in render_qa.LAYOUT_FAILS]
        fixable = [f for f in qa['hard_fail'] if f.split(':')[0] in ('overlay_below_safe', 'loudness')]
        if (layout or (cfg and fixable)) and not retry:
            print(f"  QA failed ({'; '.join(layout + fixable)}) — this run's QA retry is used up, no re-render")
        elif layout and layout_fallback is not None:
            print(f"  QA failed ({'; '.join(layout)}) — re-rendering as the whole picture over a blurred fill")
            enc = layout_fallback() or {}
            ctx.update(normalization_type=enc.get('normalization_type'), allowed_boxes=enc.get('allowed_boxes'),
                       caption_y=enc.get('caption_y') if cfg else None, watermark_box=enc.get('watermark_box'),
                       captions_engine=enc.get('captions_engine'), qa_retry='layout_fallback')
            qa = render_qa.run(out_path, ctx)
        elif cfg and fixable:
            print(f"  QA failed ({'; '.join(fixable)}) — re-encoding without overlays")
            enc = burn_captions(merged_path, out_path, all_words or [], segments,
                                render=dict(render_cfg or {},
                                            handle=(render_cfg or {}).get('handle') if cfg.get('handle_required')
                                            else None),
                                overlays={**{k: v for k, v in _overlay_settings(story, cfg, segments, words_out).items()
                                             if k.startswith('header_')},
                                          'header': (story.get('header') or story.get('hook_text'))
                                          if cfg['header'] else None,
                                          # a brief-required text hook (BILF) stays: the card is
                                          # clamped above the safe line, so it cannot cause the fail
                                          'context': story.get('context_line') if cfg.get('context_required')
                                          else None,
                                          'last_word_end': words_out[-1]['end'] if words_out else None,
                                          'loop_seam': plan.get('loop_seam')})
            # render_cfg still carries the watermark: the safe re-encode keeps the brand logo
            ctx.update(normalization_type=enc.get('normalization_type'), allowed_boxes=None,
                       caption_y=enc.get('caption_y'), watermark_box=enc.get('watermark_box'),
                       captions_engine=enc.get('captions_engine'), safe_rerender=True,
                       qa_retry='safe_reencode')
            qa = render_qa.run(out_path, ctx)
        print(f"  QA ({time.time() - t:.0f}s): LUFS {qa.get('lufs')} TP {qa.get('true_peak')} "
              f"LRA {qa.get('lra')} | longest shot {qa.get('longest_shot')}s | tail {qa.get('tail_s')}s"
              + (f" | HARD FAIL: {qa['hard_fail']}" if qa['hard_fail'] else "")
              + (f" | warnings: {qa['warnings']}" if qa['warnings'] else ""))
        return qa
    except Exception as exc:                 # QA must never break a render
        print(f"  QA error: {exc}")
        return None


def _stereo_filter(video_path):
    """Downmix to stereo keeping dialogue (centre channel) forward."""
    p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                        '-show_entries', 'stream=channels,channel_layout',
                        '-of', 'default=noprint_wrappers=1', video_path],
                       capture_output=True, text=True)
    info = dict(l.split('=', 1) for l in p.stdout.splitlines() if '=' in l)
    try:
        channels = int(info.get('channels', 2))
    except ValueError:
        channels = 2
    if channels <= 2:
        return 'aformat=channel_layouts=stereo'
    layout = info.get('channel_layout', '')
    if layout.startswith('5.1') or layout.startswith('7.1'):
        side = ['SL', 'SR'] if 'side' in layout or layout.startswith('7.1') else []
        back = ['BL', 'BR'] if '(side)' not in layout else []
        left  = ['0.5*FL', '0.7*FC'] + [f'0.3*{c}' for c in (back + side)[0::2]]
        right = ['0.5*FR', '0.7*FC'] + [f'0.3*{c}' for c in (back + side)[1::2]]
        return f"pan=stereo|FL={'+'.join(left)}|FR={'+'.join(right)}"
    return 'aformat=channel_layouts=stereo'


def _build_audio(video_path, segments, out_wav, fade_out=None):
    """
    fade_out: (seconds into the last segment, duration): fade the audio to
    silence there (a held talk tail over the next speaker's first word).

    Cut every segment's audio straight from the source (exact frame-grid
    durations), fade each join in/out by _JOIN_FADE and concatenate into one
    48 kHz stereo PCM track — no per-part AAC encode, so no priming gaps/clicks.
    """
    stereo = _stereo_filter(video_path)
    inputs, chains, labels = [], [], []
    for k, seg in enumerate(segments):
        s, e = seg['start_seconds'], seg['end_seconds']
        d = e - s
        inputs += ['-ss', f'{s:.6f}', '-t', f'{d + 0.1:.6f}', '-i', video_path]
        fade = min(_JOIN_FADE, d / 4)
        out_st, out_d = d - fade, fade
        if fade_out and k == len(segments) - 1 and fade_out[0] < d - fade:
            out_st, out_d = fade_out[0], min(fade_out[1], d - fade_out[0])
        chains.append(
            f"[{k}:a:0]aresample=48000,{stereo},apad,atrim=duration={d:.6f},"
            f"asetpts=PTS-STARTPTS,afade=t=in:d={fade:.3f},"
            f"afade=t=out:st={out_st:.6f}:d={out_d:.3f}[a{k}]")
        labels.append(f"[a{k}]")
    filter_complex = ";".join(chains) + f";{''.join(labels)}concat=n={len(segments)}:v=0:a=1[aout]"
    # Passed inline: -filter_complex_script is deprecated in ffmpeg 8 and its
    # replacement (-/filter_complex) does not exist before ffmpeg 7. Inline
    # works on every version; ~200 chars per segment stays far below
    # command-line limits even for 40-part podcast clips.
    subprocess.run(['ffmpeg', '-y', *inputs, '-filter_complex', filter_complex,
                    '-map', '[aout]', '-c:a', 'pcm_s16le', out_wav],
                   check=True, capture_output=True)


def _merge_clips(clip_paths, output_path, audio_path=None):
    """Concatenate same-format video parts (no re-encode) and mux the joined audio."""
    list_file = tempfile.mktemp(suffix='.txt')
    try:
        with open(list_file, 'w') as f:
            for p in clip_paths:
                # forward slashes keep the concat demuxer happy on Windows;
                # escape single quotes in path for ffmpeg concat list
                safe = os.path.abspath(p).replace('\\', '/').replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
        cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', list_file]
        if audio_path:
            cmd += ['-i', audio_path, '-map', '0:v', '-map', '1:a', '-c:v', 'copy',
                    '-c:a', 'pcm_s16le']
        else:
            cmd += ['-c', 'copy']
        subprocess.run(cmd + [output_path], check=True, capture_output=True)
    finally:
        try:
            os.unlink(list_file)
        except OSError:
            pass
