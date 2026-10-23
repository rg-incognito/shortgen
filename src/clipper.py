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

_SENTENCE_END = re.compile(r'[.!?]["\')\]]*$')


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
    return {
        'niche': niche, 'template': template,
        'caption_preset': render.get('caption_preset') or ('tv' if niche == 'A' else 'talk'),
        'header': bool(render.get('header', template == 'A1')),
        'overlays': bool(render.get('overlays_allowed', True)) and variant.get('overlay', 'on') != 'off',
        'handle': render.get('handle'),
        # A brief-required label ("BILF Podcast" for the whole clip) is kept through the
        # QA safe re-encode, and may be drawn larger than a channel handle.
        'handle_required': bool(render.get('handle_required')),
        'context_required': bool(render.get('context_required')),
        'handle_size': render.get('handle_size'),
        'tail_s': tail,
        'max_hold': _MAX_HOLD_A if niche == 'A' else max(tail, _MAX_HOLD_B),
        'reaction_cut': niche == 'A',
        'shot_cap': render.get('shot_cap_s', _SHOT_CAP_S if niche == 'A' else _SHOT_CAP_B),
        # talk: hold the tail over the next speaker's first word (audio faded)
        'hold_over': niche == 'B',
        'boom': boom, 'music': music,
        'names': names or None,
    }


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


def _music_bed(audio_wav, cfg):
    """
    Hook point for the podcast music bed (BUILD_PLAN #14, soundtrack spec §5):
    static bed at -14 LU under dialogue, <= 3.5 dB duck, only tracks listed in
    music/LICENSES.json. Not implemented until licensed tracks exist, so any
    'bed' / 'ab' setting is logged and skipped; the audio is left untouched.
    """
    if cfg and cfg.get('music') not in (None, 'off'):
        print(f"  Music bed '{cfg['music']}' requested but no licensed library is set up — skipped")
    return False


def create_story_short(video_path, story, output_dir, episode_name,
                        pipeline=None, all_words=None, silence_thresh=_SILENCE_THRESH,
                        wide_fallback=False, render=None):
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
                                          push=plan['push'] if k == plan['push_part'] else None))
                part_paths.append(part_path)

        print(f"  Merging {len(part_paths)} parts...")
        fade = None
        if held:
            last_end, nxt = held
            f0 = max(last_end + 0.02, nxt - _HOLD_FADE_LEAD) - expanded_clips[-1]['start_seconds']
            fade = (max(0.0, f0), _HOLD_FADE)
            print(f"  Tail held over the next speaker: audio fades {f0:.2f}s into the last part")
        _build_audio(video_path, expanded_clips, audio_path, fade_out=fade)
        if cfg and cfg['boom'] and plan.get('payoff_out') is not None:
            if _add_payoff_boom(audio_path, plan['payoff_out']):
                print(f"  Payoff boom at {plan['payoff_out']:.2f}s (experiment flag)")
        _music_bed(audio_path, cfg)
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
            enc = burn_captions(merged_path, out_path, all_words or [], expanded_clips,
                                render=render_cfg, overlays=ov)
        _run_qa(out_path, merged_path, cfg, story, all_words, expanded_clips, jobs, enc or {},
                plan, time.time() - t_start, burn_captions, render_cfg if cfg else None)
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

    # payoff push: the part holding the payoff word, from its last camera cut on
    push_part, push, push_from = None, None, None
    if payoff_out is not None:
        push_part = next((k for k, p in enumerate(parts)
                          if p['off'] <= payoff_out < p['off'] + p['dur']), len(parts) - 1)
        p = parts[push_part]
        start = max([p['off']] + [c for c in p['cuts'] if c < payoff_out])
        if cfg['niche'] == 'B':
            # a one-camera answer has no cut to start from: push over the last beat only
            start = max(start, min(payoff_out - _PUSH_B_LEAD, p['off'] + p['dur'] - _PUSH_MIN_S))
        if p['off'] + p['dur'] - start >= _PUSH_MIN_S:
            push_from = start
            push = (int(round((start - p['off']) * p['fps'])), _PUSH_PAYOFF)

    crop_w = 0.0
    if analyses:
        an = analyses[0]
        aspect = 1080 / (1350 if cfg['template'] == 'A1' else 1920)
        crop_w = min(an['src_w'], an['src_h'] * aspect)
    toggles = []
    if cfg['shot_cap']:
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
        print(f"  Shot cap: punch-in toggles at {[t for t, _ in toggles]}")
    return {'analyses': analyses, 'zoom_events': zoom_events, 'push': push,
            'push_part': push_part, 'payoff_out': payoff_out, 'toggles': toggles,
            'parts': parts}


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
            'handle_size': cfg.get('handle_size')}


def _overlay_settings(story, cfg, segments, words_out):
    """Header / context card / closing line / stage directions for this render."""
    header = (story.get('header') or story.get('hook_text')) if cfg['header'] else None
    ov = {'header': header,
          'last_word_end': words_out[-1]['end'] if words_out else None}
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
            seconds, burn_captions, render_cfg=None):
    """Lint the finished Short (<out>.qa.json); one safe re-encode for fixable failures."""
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
           'allowed_boxes': enc.get('allowed_boxes')}
    if cfg:
        ctx['caption_y'] = enc.get('caption_y')
        if cfg['template'] == 'A1':
            from reframer import A1_WINDOW
            ctx['live_rows'] = ctx['window'] = (A1_WINDOW[0], A1_WINDOW[0] + A1_WINDOW[1])
    elif story.get('hook_text'):
        ctx['live_rows'] = ctx['window'] = (320, 1920)
    try:
        t = time.time()
        qa = render_qa.run(out_path, ctx)
        fixable = [f for f in qa['hard_fail'] if f.split(':')[0] in ('overlay_below_safe', 'loudness')]
        if cfg and fixable:
            print(f"  QA failed ({'; '.join(fixable)}) — re-encoding without overlays")
            enc = burn_captions(merged_path, out_path, all_words or [], segments,
                                render=dict(render_cfg or {},
                                            handle=(render_cfg or {}).get('handle') if cfg.get('handle_required')
                                            else None),
                                overlays={'header': (story.get('header') or story.get('hook_text'))
                                          if cfg['header'] else None,
                                          # a brief-required text hook (BILF) stays: the card is
                                          # clamped above the safe line, so it cannot cause the fail
                                          'context': story.get('context_line') if cfg.get('context_required')
                                          else None,
                                          'last_word_end': words_out[-1]['end'] if words_out else None})
            ctx.update(normalization_type=enc.get('normalization_type'), allowed_boxes=None,
                       caption_y=enc.get('caption_y'), safe_rerender=True)
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
