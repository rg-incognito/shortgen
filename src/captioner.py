"""
Word-by-word karaoke caption overlay.

Each spoken word pops on screen in yellow (uppercase) next to the upcoming word
in white, both on a semi-transparent dark background box.  The box refreshes with
every word, creating the viral Shorts pop effect.

Timings come from Whisper word-level timestamps, remapped from episode time
into the merged video's timeline using the story clip list.
"""
import os
import subprocess
import tempfile
from pathlib import Path

# ASS style constants for the 0.5s hook
_HOOK_FONT_SIZE = 72
_HOOK_Y_POS     = 960 # Vertical center for 1920px height
_HOOK_COLOR     = "&H00FFFFFF&" # White
_HOOK_OUTLINE   = "&H00000000&" # Black
_HOOK_SHADOW    = "&H80000000&" # Shadow


# ASS style constants
_FONT      = "Arial"
_FONT_SIZE = 72          # larger for word-by-word impact
_OUT_W     = 1080
_OUT_H     = 1920
_MARGIN_V  = 350         # px from bottom (alignment 2 = bottom-center)
_WHITE     = "&H00FFFFFF&"
_YELLOW    = "&H0000FFFF&"   # BGR in ASS = #FFFF00 = yellow
_SHADOW    = "&H80000000&"   # kept for Hook style
_OUTLINE   = "&H00000000&"
_BOX_BG    = "&H40000000&"   # 75% opaque black box behind text


def _remap_words(all_words, story_clips):
    """
    Filter words that fall inside any story clip and remap episode timestamps
    to merged-video timestamps.

    story_clips: list of {start_seconds, end_seconds} in episode time.
    Returns [{word, start, end}] in merged-video time.
    """
    # Build cumulative offsets for each clip
    offsets = []
    t = 0.0
    for clip in story_clips:
        offsets.append((clip['start_seconds'], clip['end_seconds'], t))
        t += clip['end_seconds'] - clip['start_seconds']

    result = []
    for w in all_words:
        ws, we = w['start'], w['end']
        for cs, ce, offset in offsets:
            # word must start inside the clip window
            if ws >= cs and ws < ce:
                new_start = offset + (ws - cs)
                new_end   = offset + (min(we, ce) - cs)
                result.append({'word': w['word'], 'start': new_start, 'end': new_end})
                break
    return result


def _format_ass_time(seconds):
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _build_ass(mapped_words, total_duration, yt_title=None):
    """
    Word-by-word karaoke captions.

    Each event shows 2 words: the active word (yellow) + the next word (white preview),
    both rendered on a semi-transparent dark background box.  Words are uppercased.
    The box appears/disappears with each spoken word, creating the viral Shorts pop effect.
    """
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {_OUT_W}
PlayResY: {_OUT_H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{_FONT},{_FONT_SIZE},{_WHITE},&H000000FF&,{_OUTLINE},{_BOX_BG},-1,0,0,0,100,100,0,0,3,0,0,2,20,20,{_MARGIN_V},1
Style: Hook,{_FONT},{_HOOK_FONT_SIZE},{_HOOK_COLOR},&H000000FF&,{_HOOK_OUTLINE},{_HOOK_SHADOW},-1,0,0,0,100,100,0,0,1,3,1,2,30,30,{_HOOK_Y_POS},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []

    if yt_title:
        hook_start_str = _format_ass_time(0.0)
        hook_end_str   = _format_ass_time(0.5)
        events.append(f"Dialogue: 0,{hook_start_str},{hook_end_str},Hook,,0,0,0,,{{\\an5}}{yt_title}")

    if not mapped_words:
        return header + "\n".join(events) + "\n"

    MAX_WORD_S   = 0.9   # cap on how long one word stays on screen
    CAPTION_LEAD = 0.10  # show slightly before the word is spoken

    for i, word in enumerate(mapped_words):
        seg_start = max(0.0, word['start'] - CAPTION_LEAD)
        if i + 1 < len(mapped_words):
            next_start = mapped_words[i + 1]['start']
            seg_end = min(next_start - CAPTION_LEAD, word['start'] + MAX_WORD_S)
        else:
            seg_end = min(word['end'] + 0.15, total_duration)
        if seg_end <= seg_start:
            seg_end = seg_start + 0.05

        active = word['word'].strip().upper()
        if not active:
            continue

        if i + 1 < len(mapped_words):
            upcoming = mapped_words[i + 1]['word'].strip().upper()
            line = r"{\c" + _YELLOW + r"}" + active + r"{\c" + _WHITE + r"} " + upcoming
        else:
            line = r"{\c" + _YELLOW + r"}" + active

        start_str = _format_ass_time(seg_start)
        end_str   = _format_ass_time(seg_end)
        events.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{line}")

    return header + "\n".join(events) + "\n"


def burn_captions(video_path, output_path, all_words, story_clips, yt_title=None):
    mapped = _remap_words(all_words, story_clips)
    if not mapped:
        print("  Captions: no words mapped — copying without captions")
        import shutil
        shutil.copy2(video_path, output_path)
        return

    print(f"  Captions: {len(mapped)} words across {len(story_clips)} clips")

    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
        capture_output=True, text=True
    )
    total_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 60.0

    ass_content = _build_ass(mapped, total_duration, yt_title)
    ass_file = tempfile.mktemp(suffix='.ass')
    try:
        with open(ass_file, 'w', encoding='utf-8') as f:
            f.write(ass_content)

        ass_safe = ass_file.replace('\\', '/')
        enhance   = "unsharp=5:5:0.8:3:3:0,eq=contrast=1.05:saturation=1.1"

        cmd = [
            'ffmpeg', '-y',
            '-i', video_path,
            '-vf', f"ass={ass_safe},{enhance}",
            '-map', '0:v', '-map', '0:a?',
            '-c:v', 'libx264', '-preset', 'slow', '-crf', '18',
            '-c:a', 'aac', '-b:a', '128k',
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
    finally:
        try:
            os.unlink(ass_file)
        except OSError:
            pass
