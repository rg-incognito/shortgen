"""
Word-by-word karaoke caption overlay.

Each spoken word pops on screen in yellow (uppercase) next to the upcoming word
in white, both on a semi-transparent dark background box.  The box refreshes with
every word, creating the viral Shorts pop effect.

Timings come from Whisper word-level timestamps, remapped from episode time
into the merged video's timeline using the story clip list.

Also renders the persistent top hook banner (render_hook_png).
"""
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ASS style constants
_FONT      = "Arial"
_FONT_SIZE = 72          # larger for word-by-word impact
_OUT_W     = 1080
_OUT_H     = 1920
_MARGIN_V  = 350         # px from bottom (alignment 2 = bottom-center)
_WHITE     = "&H00FFFFFF&"
_YELLOW    = "&H0000FFFF&"   # BGR in ASS = #FFFF00 = yellow
_SHADOW    = "&H80000000&"
_OUTLINE   = "&H00000000&"
_BOX_BG    = "&H40000000&"   # 75% opaque black box behind text

# --- Hook banner (persistent top bar, shown for the whole video — not a flash) ---
_HOOK_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # ubuntu-latest (CI runner)
    "C:/Windows/Fonts/arialbd.ttf",                            # Windows local dev
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",       # macOS
]
_HOOK_EMOJI_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",                            # Windows local dev
]
_HOOK_BAR_HEIGHT = 320               # px — top ~17% of the 1920-tall frame
_HOOK_TOP_SAFE_PAD = 90              # px reserved at the top of the bar so text clears phone camera cutouts/notches
_HOOK_BAR_BG     = (255, 255, 255, 255)
_HOOK_TEXT_COLOR = (0, 0, 0, 255)
_HOOK_FONT_SIZE  = 64
_HOOK_MIN_SIZE   = 40     # shrink text down to this size if it doesn't fit in 2 lines
_HOOK_MAX_WIDTH  = 1000   # px — leaves ~40px margin each side of the 1080-wide frame
_HOOK_MAX_LINES  = 2


def _find_font(candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


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


def _build_ass(mapped_words, total_duration):
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

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []

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


def _wrap_lines(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_hook_png(hook_text, hook_emoji, out_path):
    """
    Render a persistent top hook banner (opaque white bar across the top
    ~17% of the frame, bold black text + optional inline emoji) — composited
    for the ENTIRE video, not just the opening seconds, so it's baked into
    every frame including whatever one YouTube auto-picks as the grid
    thumbnail when no explicit thumbnail is set. Text is pushed down within
    the bar by _HOOK_TOP_SAFE_PAD so it clears phone camera cutouts/notches
    that otherwise overlap the very top of a full-bleed vertical video.
    Shrinks font size until the text fits within _HOOK_MAX_LINES; falls back
    to Pillow's built-in font if no system font is found so a missing font
    can never crash the pipeline. Emoji (one or more characters, e.g. "😳😬")
    is appended after the last line — best-effort, silently skipped if no
    color-emoji font is available.
    """
    img = Image.new('RGBA', (_OUT_W, _OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, _OUT_W, _HOOK_BAR_HEIGHT], fill=_HOOK_BAR_BG)

    font_path = _find_font(_HOOK_FONT_CANDIDATES)
    size = _HOOK_FONT_SIZE
    lines = [hook_text]
    font = None
    while size >= _HOOK_MIN_SIZE:
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default(size=size)
        lines = _wrap_lines(draw, hook_text, font, _HOOK_MAX_WIDTH)
        available = _HOOK_BAR_HEIGHT - _HOOK_TOP_SAFE_PAD - 24
        if len(lines) <= _HOOK_MAX_LINES and int(size * 1.2) * len(lines) <= available:
            break
        size -= 6

    line_height = int(size * 1.2)
    total_height = line_height * len(lines)
    y = _HOOK_TOP_SAFE_PAD + (_HOOK_BAR_HEIGHT - _HOOK_TOP_SAFE_PAD - total_height) // 2

    emoji_font = None
    if hook_emoji:
        emoji_font_path = _find_font(_HOOK_EMOJI_FONT_CANDIDATES)
        if emoji_font_path:
            try:
                emoji_font = ImageFont.truetype(emoji_font_path, int(size * 1.05))
            except Exception as exc:
                print(f"  Hook emoji skipped ({exc})")
                emoji_font = None

    for idx, line in enumerate(lines):
        w = draw.textlength(line, font=font)
        is_last = idx == len(lines) - 1
        gap = 14
        emoji_w = draw.textlength(hook_emoji, font=emoji_font) if (emoji_font and is_last) else 0
        total_w = w + (gap + emoji_w if emoji_w else 0)
        x = (_OUT_W - total_w) / 2
        draw.text((x, y), line, font=font, fill=_HOOK_TEXT_COLOR)
        if emoji_w:
            ey = y + (line_height - int(size * 1.05)) / 2
            draw.text((x + w + gap, ey), hook_emoji, font=emoji_font, embedded_color=True)
        y += line_height

    img.save(out_path)


def burn_captions(video_path, output_path, all_words, story_clips,
                   hook_text=None, hook_emoji=None):
    mapped = _remap_words(all_words, story_clips)

    hook_png = None
    if hook_text:
        hook_png = tempfile.mktemp(suffix='.png')
        try:
            render_hook_png(hook_text, hook_emoji or '', hook_png)
        except Exception as exc:
            print(f"  Hook card skipped ({exc})")
            hook_png = None

    try:
        if not mapped:
            print("  Captions: no words mapped" +
                  (" — hook card only" if hook_png else " — copying without captions"))
            if hook_png:
                _overlay_only(video_path, hook_png, output_path)
            else:
                import shutil
                shutil.copy2(video_path, output_path)
            return

        print(f"  Captions: {len(mapped)} words across {len(story_clips)} clips" +
              (" + hook card" if hook_png else ""))

        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True, text=True
        )
        total_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 60.0

        ass_content = _build_ass(mapped, total_duration)
        ass_file = tempfile.mktemp(suffix='.ass')
        try:
            with open(ass_file, 'w', encoding='utf-8') as f:
                f.write(ass_content)

            # Escape ':' (e.g. a Windows drive letter) so ffmpeg's filter-option
            # parser doesn't mistake it for a key=value separator.
            ass_safe = ass_file.replace('\\', '/').replace(':', '\\:')
            enhance   = "unsharp=5:5:0.8:3:3:0,eq=contrast=1.05:saturation=1.1"

            if hook_png:
                filter_complex = (
                    f"[0:v]ass={ass_safe},{enhance}[capped];"
                    f"[capped][1:v]overlay=0:0[vout]"
                )
                cmd = [
                    'ffmpeg', '-y',
                    '-i', video_path,
                    '-i', hook_png,
                    '-filter_complex', filter_complex,
                    '-map', '[vout]', '-map', '0:a?',
                    '-c:v', 'libx264', '-preset', 'slow', '-crf', '18',
                    '-c:a', 'aac', '-b:a', '128k',
                    output_path,
                ]
            else:
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
    finally:
        if hook_png:
            try:
                os.unlink(hook_png)
            except OSError:
                pass


def _overlay_only(video_path, hook_png, output_path):
    """Composite just the hook banner (no dialogue captions available)."""
    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-i', hook_png,
        '-filter_complex',
        "[0:v][1:v]overlay=0:0[vout]",
        '-map', '[vout]', '-map', '0:a?',
        '-c:v', 'libx264', '-preset', 'slow', '-crf', '18',
        '-c:a', 'aac', '-b:a', '128k',
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
