import os
import subprocess
import tempfile

# Gaps between words longer than this are treated as laugh-track / dead air and cut.
_SILENCE_THRESH = 0.45  # seconds
# Natural breath padding kept before and after each speech burst.
_SPEECH_PAD = 0.15      # seconds


def _split_by_speech(clip_start, clip_end, all_words):
    """
    Split [clip_start, clip_end] into speech-only sub-intervals by removing
    silent gaps longer than _SILENCE_THRESH.

    Returns a list of (start, end) tuples in episode time.
    Falls back to [(clip_start, clip_end)] if no words are found in range.
    """
    words = [w for w in all_words if w['start'] >= clip_start and w['start'] < clip_end]
    if not words:
        return [(clip_start, clip_end)]

    # Group words into continuous speech bursts
    bursts = []
    burst_start = words[0]['start']
    burst_end   = words[0]['end']
    for w in words[1:]:
        if w['start'] - burst_end > _SILENCE_THRESH:
            bursts.append((burst_start, burst_end))
            burst_start = w['start']
        burst_end = max(burst_end, w['end'])
    bursts.append((burst_start, burst_end))

    # Add padding, clamp to clip bounds
    padded = []
    for bs, be in bursts:
        padded.append((max(clip_start, bs - _SPEECH_PAD),
                       min(clip_end,   be + _SPEECH_PAD)))

    # Merge overlapping intervals that touch after padding
    merged = [padded[0]]
    for start, end in padded[1:]:
        ps, pe = merged[-1]
        if start <= pe:
            merged[-1] = (ps, max(pe, end))
        else:
            merged.append((start, end))

    return merged


def create_story_short(video_path, story, output_dir, episode_name,
                        pipeline=None, all_words=None, yt_title=None):
    """
    Cuts every clip in a story arc, reframes each to 9:16, concatenates into
    one compiled video, then burns viral-style captions if word timestamps
    are provided.

    When all_words is supplied, each story clip is first split into speech-only
    sub-clips (laugh-track / dead-air gaps removed) before reframing.
    """
    from reframer import reframe_clip
    from captioner import burn_captions

    clips = story['clips']
    title = story['storyline_title']
    score = story['score']

    parts_dir = os.path.join(output_dir, '_parts')
    os.makedirs(parts_dir, exist_ok=True)

    part_paths    = []
    expanded_clips = []   # sub-clips passed to captioner for timestamp remapping
    try:
        for i, clip in enumerate(clips, 1):
            cs, ce = clip['start_seconds'], clip['end_seconds']

            if all_words:
                sub_clips = _split_by_speech(cs, ce, all_words)
            else:
                sub_clips = [(cs, ce)]

            removed = (ce - cs) - sum(e - s for s, e in sub_clips)
            suffix  = f" (-{removed:.1f}s silence)" if removed > 0.05 else ""
            print(f"  Part {i}/{len(clips)}: [{clip['label']}] "
                  f"{cs:.1f}s - {ce:.1f}s  =>  {len(sub_clips)} sub-clip(s){suffix}")

            for j, (sc_start, sc_end) in enumerate(sub_clips):
                part_path = os.path.join(parts_dir, f"part_{i:02d}_{j:02d}.mp4")
                reframe_clip(video_path, sc_start, sc_end, part_path, pipeline=pipeline)
                part_paths.append(part_path)
                expanded_clips.append({'start_seconds': sc_start, 'end_seconds': sc_end})

        merged_path = os.path.join(output_dir, '_merged.mp4')
        print(f"  Merging {len(part_paths)} parts...")
        _merge_clips(part_paths, merged_path)

        out_path = os.path.join(output_dir, f"{episode_name}_{title}_s{score}.mp4")
        if all_words:
            print("  Burning captions...")
            burn_captions(merged_path, out_path, all_words, expanded_clips, yt_title=yt_title)
            os.unlink(merged_path)
        else:
            os.rename(merged_path, out_path)

        return out_path

    finally:
        for p in part_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(parts_dir)
        except OSError:
            pass


def _merge_clips(clip_paths, output_path):
    """Concatenate same-format clips using ffmpeg concat demuxer (no re-encode)."""
    list_file = tempfile.mktemp(suffix='.txt')
    try:
        with open(list_file, 'w') as f:
            for p in clip_paths:
                # escape single quotes in path for ffmpeg concat list
                safe = p.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
        subprocess.run([
            'ffmpeg', '-y',
            '-f', 'concat', '-safe', '0',
            '-i', list_file,
            '-c', 'copy',
            output_path,
        ], check=True, capture_output=True)
    finally:
        try:
            os.unlink(list_file)
        except OSError:
            pass
