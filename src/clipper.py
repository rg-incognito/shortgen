import math
import os
import re
import subprocess
import tempfile

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

_SENTENCE_END = re.compile(r'[.!?]["\')\]]*$')


def _split_by_speech(clip_start, clip_end, all_words, silence_thresh=_SILENCE_THRESH,
                     final=True):
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
    tail = _TAIL_FINAL if final else _TAIL_HOLD
    last_end = words[-1]['end']
    end = min(max(clip_end, last_end + tail), last_end + _MAX_HOLD)
    if following:
        end = min(end, max(words[-1]['end'] + 0.05, following[0] - 0.05))
    # Never before the requested start: upstream snaps it to a sentence start,
    # and earlier would reach into the previous word.
    start = max(clip_start, words[0]['start'] - _LEAD_PAD)

    # Cut points inside long pauses: (end of kept part, start of next part)
    parts = [[start, None]]
    for prev, w in zip(words, words[1:]):
        gap = w['start'] - prev['end']
        if gap <= silence_thresh:
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


def edited_duration(story, all_words, silence_thresh=_SILENCE_THRESH):
    """Length of the finished short after silence trimming, without rendering anything."""
    total = 0.0
    clips = story['clips']
    for i, clip in enumerate(clips, 1):
        cs, ce = clip['start_seconds'], clip['end_seconds']
        subs = (_split_by_speech(cs, ce, all_words, silence_thresh, final=(i == len(clips)))
                if all_words else [(cs, ce)])
        total += sum(e - s for s, e in subs)
    return total


def create_story_short(video_path, story, output_dir, episode_name,
                        pipeline=None, all_words=None, silence_thresh=_SILENCE_THRESH,
                        wide_fallback=False):
    """
    Cuts every clip in a story arc, reframes each to 9:16, concatenates into
    one compiled video, then burns viral-style captions plus the persistent
    hook banner if word timestamps are provided.

    When all_words is supplied, each story clip is first split into speech
    sub-clips (long laugh-track / dead-air gaps shortened) before reframing.
    Every sub-clip is snapped to the frame grid; audio is cut once from the
    source with short fades at each join and normalised in the final encode.
    """
    from reframer import reframe_clip, probe_fps, HOOK_BAR_HEIGHT
    from captioner import burn_captions

    clips = story['clips']
    title = story['storyline_title']
    score = story['score']
    _, fps = probe_fps(video_path)
    top_reserved = HOOK_BAR_HEIGHT if story.get('hook_text') else 0

    parts_dir = os.path.join(output_dir, '_parts')
    os.makedirs(parts_dir, exist_ok=True)

    part_paths    = []
    expanded_clips = []   # sub-clips passed to captioner for timestamp remapping
    audio_path  = os.path.join(parts_dir, '_audio.wav')
    merged_path = os.path.join(output_dir, '_merged.mkv')
    try:
        for i, clip in enumerate(clips, 1):
            cs, ce = clip['start_seconds'], clip['end_seconds']

            if all_words:
                sub_clips = _split_by_speech(cs, ce, all_words, silence_thresh,
                                             final=(i == len(clips)))
            else:
                sub_clips = [(cs, ce)]
            sub_clips = [_snap_to_frames(s, e, fps) for s, e in sub_clips]

            kept = sum(e - s for s, e in sub_clips)
            delta = (ce - cs) - kept
            suffix = f" ({-delta:+.1f}s after trimming)" if abs(delta) > 0.05 else ""
            print(f"  Part {i}/{len(clips)}: [{clip['label']}] "
                  f"{cs:.1f}s - {ce:.1f}s  =>  {len(sub_clips)} sub-clip(s){suffix}")

            for j, (sc_start, sc_end) in enumerate(sub_clips):
                part_path = os.path.join(parts_dir, f"part_{i:02d}_{j:02d}.mp4")
                reframe_clip(video_path, sc_start, sc_end, part_path, pipeline=pipeline,
                             wide_fallback=wide_fallback, top_reserved=top_reserved,
                             zoom=_PUNCH_IN if j % 2 else 1.0,
                             push_in=(sc_end - sc_start) > _PUSH_IN_MIN_S)
                part_paths.append(part_path)
                expanded_clips.append({'start_seconds': sc_start, 'end_seconds': sc_end})

        print(f"  Merging {len(part_paths)} parts...")
        _build_audio(video_path, expanded_clips, audio_path)
        _merge_clips(part_paths, merged_path, audio_path)

        out_path = os.path.join(output_dir, f"{episode_name}_{title}_s{score}.mp4")
        print("  Burning captions..." if all_words else "  Finishing (no captions)...")
        burn_captions(merged_path, out_path, all_words or [], expanded_clips,
                      hook_text=story.get('hook_text'), hook_emoji=story.get('hook_emoji'))
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


def _build_audio(video_path, segments, out_wav):
    """
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
        chains.append(
            f"[{k}:a:0]aresample=48000,{stereo},apad,atrim=duration={d:.6f},"
            f"asetpts=PTS-STARTPTS,afade=t=in:d={fade:.3f},"
            f"afade=t=out:st={d - fade:.6f}:d={fade:.3f}[a{k}]")
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
