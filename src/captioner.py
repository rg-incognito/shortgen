"""
Word-highlight caption overlay.

Words are grouped into fixed 2-4 word chunks (broken on punctuation, pauses
and line length). A chunk stays on screen unchanged and only the highlight
(one colour) moves from word to word, so the line never re-centres or blinks.
Captions are burned last, in final-frame pixels, in the lower-middle of the
picture: clear of the Shorts title/channel UI at the bottom and the action
buttons on the right. Font: bundled Anton (fonts/, SIL OFL 1.1) with a thick
black outline, so CI renders exactly what local tests show.

Timings come from Whisper word-level timestamps, remapped from episode time
into the merged video's timeline using the (frame-snapped) sub-clip list.

Also renders the persistent top hook banner (render_hook_png) and does the
final encode: two-pass loudnorm to -14 LUFS / -1.5 dBTP, stereo AAC 192k.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ASS style constants
_FONT_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'fonts')
_FONT_FILE = "Anton-Regular.ttf"
_FONT      = "Anton"
_FONT_SIZE = 104
_OUT_W     = 1080
_OUT_H     = 1920
_CAPTION_X = 520         # px, a little left of centre to clear the right-side buttons
_CAPTION_Y = 1340        # px, bottom of the caption line (alignment 2)
_MARGIN_LR = 90
_OUTLINE_W = 7
_SHADOW_W  = 3
_WHITE     = "&H00FFFFFF&"
_YELLOW    = "&H0000FFFF&"   # BGR in ASS = #FFFF00 = yellow (the one highlight colour)
_SHADOW    = "&H80000000&"
_OUTLINE   = "&H00000000&"

# Caption chunking
_CHUNK_MAX_WORDS = 4
_CHUNK_MAX_CHARS = 16
_CHUNK_GAP_S     = 0.30   # a pause longer than this starts a new chunk
_CHUNK_HOLD_S    = 1.20   # max time a finished chunk lingers before the next one
_CAPTION_LEAD    = 0.08   # show slightly before the word is spoken

# Final audio: YouTube turns loud uploads down but never turns quiet ones up.
# TP target sits 0.5 dB under the -1.5 dBTP spec: the AAC encode overshoots
# loudnorm's limiter slightly (measured -1.47 dBTP with TP=-1.5).
_LOUD_I, _LOUD_TP, _LOUD_LRA = -14.0, -2.0, 11.0
_AUDIO_ARGS = ['-ac', '2', '-ar', '48000', '-c:a', 'aac', '-b:a', '192k']
_VIDEO_ARGS = ['-c:v', 'libx264', '-preset', 'slow', '-crf', '18',
               '-pix_fmt', 'yuv420p', '-movflags', '+faststart']

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
_HOOK_BAR_HEIGHT = 320               # px — top ~17% of the 1920-tall frame (= reframer.HOOK_BAR_HEIGHT)
_HOOK_TOP_SAFE_PAD = 90              # px reserved at the top of the bar so text clears phone camera cutouts/notches
_HOOK_BAR_BG     = (255, 255, 255, 255)
_HOOK_TEXT_COLOR = (0, 0, 0, 255)
_HOOK_FONT_SIZE  = 64
_HOOK_MIN_SIZE   = 40     # shrink text down to this size if it doesn't fit in 2 lines
_HOOK_MAX_WIDTH  = 900    # px — leaves ~90px margin each side of the 1080-wide frame
_HOOK_MAX_LINES  = 2

# When a hook banner is present the reframer already renders the picture into
# the area below the bar (top_reserved), so the banner is simply overlaid —
# no shrink and no letterbox here.


def _find_font(candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _remap_words(all_words, story_clips):
    """
    Filter words that fall inside any story clip and remap episode timestamps
    to merged-video timestamps.

    story_clips: list of {start_seconds, end_seconds} in episode time; their
    lengths must be the rendered lengths (clipper snaps them to the frame grid).
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
    # Story clips are not in episode order (the hook usually comes from later
    # in the episode), so sort into merged-video order.
    result.sort(key=lambda w: w['start'])
    return result


def _format_ass_time(seconds):
    total_cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(total_cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs  = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


_PHRASE_END = re.compile(r'[.!?]["\')\]]*$')


def _chunk_words(mapped_words):
    """Group words into 2-4 word caption chunks (lists of word dicts + 'text')."""
    chunks, cur = [], []
    for w in mapped_words:
        text = w['word'].strip().upper()
        if not text:
            continue
        w = dict(w, text=text)
        if cur:
            prev = cur[-1]
            line = " ".join(x['text'] for x in cur) + " " + text
            if (len(cur) >= _CHUNK_MAX_WORDS
                    or len(line) > _CHUNK_MAX_CHARS
                    or w['start'] - prev['end'] > _CHUNK_GAP_S
                    or _PHRASE_END.search(prev['text'])
                    or (prev['text'].endswith(',') and len(cur) >= 2)):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    # Fold a lone word into the chunk before it when it continues the phrase
    out = []
    for c in chunks:
        if (out and len(c) == 1 and len(out[-1]) < _CHUNK_MAX_WORDS
                and c[0]['start'] - out[-1][-1]['end'] <= _CHUNK_GAP_S
                and not re.search(r'[.!?,]["\')\]]*$', out[-1][-1]['text'])
                and len(" ".join(x['text'] for x in out[-1] + c)) <= _CHUNK_MAX_CHARS + 4):
            out[-1] = out[-1] + c
        else:
            out.append(c)
    return out


def _build_ass(mapped_words, total_duration):
    """
    Chunked captions with a moving word highlight.

    Every event of a chunk shows the same full text at the same fixed position;
    only the highlighted (yellow) word changes. A chunk pops in once, holds
    through short pauses, and the last chunk stays up to the final frame.
    """
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {_OUT_W}
PlayResY: {_OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{_FONT},{_FONT_SIZE},{_WHITE},&H000000FF&,{_OUTLINE},{_SHADOW},0,0,0,0,100,100,1,0,1,{_OUTLINE_W},{_SHADOW_W},2,{_MARGIN_LR},{_MARGIN_LR},{_OUT_H - _CAPTION_Y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    chunks = _chunk_words(mapped_words)
    starts = [max(0.0, c[0]['start'] - _CAPTION_LEAD) for c in chunks]
    if starts and starts[0] < 0.3:
        starts[0] = 0.0                    # frame 0 already carries the first caption

    for ci, chunk in enumerate(chunks):
        c_start = starts[ci]
        if ci + 1 < len(chunks):
            c_end = min(starts[ci + 1], chunk[-1]['end'] + _CHUNK_HOLD_S)
        else:
            c_end = total_duration          # last caption stays up to the final frame
        c_end = max(c_end, c_start + 0.05)

        for k, word in enumerate(chunk):
            e_start = c_start if k == 0 else max(c_start, word['start'] - _CAPTION_LEAD)
            if k + 1 < len(chunk):
                e_end = min(c_end, max(e_start, chunk[k + 1]['start'] - _CAPTION_LEAD))
                if _format_ass_time(e_end) == _format_ass_time(e_start):
                    continue
            else:
                e_end = c_end
            text = " ".join(r"{\c" + (_YELLOW if j == k else _WHITE) + "}" + w['text']
                            for j, w in enumerate(chunk))
            pop = r"\fscx88\fscy88\t(0,90,\fscx100\fscy100)" if k == 0 else ""
            line = r"{\an2\pos(" + f"{_CAPTION_X},{_CAPTION_Y}" + ")" + pop + "}" + text
            events.append(f"Dialogue: 0,{_format_ass_time(e_start)},{_format_ass_time(e_end)},"
                          f"Default,,0,0,0,,{line}")

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
        # Reserve room for the trailing emoji (roughly one font-size-wide glyph
        # per character, plus the gap) so wrapping never produces a line that
        # only overflows the 1080px frame once the emoji is appended at render time.
        emoji_reserve = int(size * 1.1) * len(hook_emoji) + 14 if hook_emoji else 0
        wrap_width = max(200, _HOOK_MAX_WIDTH - emoji_reserve)
        lines = _wrap_lines(draw, hook_text, font, wrap_width)
        available = _HOOK_BAR_HEIGHT - _HOOK_TOP_SAFE_PAD - 24
        if len(lines) <= _HOOK_MAX_LINES and int(size * 1.2) * len(lines) <= available:
            break
        size -= 6

    line_height = int(size * 1.2)
    total_height = line_height * len(lines)
    y = _HOOK_TOP_SAFE_PAD + (_HOOK_BAR_HEIGHT - _HOOK_TOP_SAFE_PAD - total_height) // 2

    emoji_img = None
    if hook_emoji:
        emoji_font_path = _find_font(_HOOK_EMOJI_FONT_CANDIDATES)
        if emoji_font_path:
            emoji_img = _render_emoji_glyph(hook_emoji, emoji_font_path, int(size * 1.05))
            if emoji_img is None:
                print("  Hook emoji skipped (no usable strike size in emoji font)")

    for idx, line in enumerate(lines):
        w = draw.textlength(line, font=font)
        is_last = idx == len(lines) - 1
        gap = 14
        emoji_w = emoji_img.width if (emoji_img and is_last) else 0
        total_w = w + (gap + emoji_w if emoji_w else 0)
        x = (_OUT_W - total_w) / 2
        draw.text((x, y), line, font=font, fill=_HOOK_TEXT_COLOR)
        if emoji_w:
            ey = y + (line_height - emoji_img.height) / 2
            img.paste(emoji_img, (int(x + w + gap), int(ey)), emoji_img)
        y += line_height

    img.save(out_path)


# NotoColorEmoji (the emoji font on the Ubuntu CI runner) is a CBDT/CBLC
# bitmap-strike font: it only supports a handful of fixed embedded pixel
# sizes, and ImageFont.truetype raises "invalid pixel size" for anything
# else — unlike Windows' Segoe UI Emoji, which accepts any size. Render at
# whichever strike size actually loads, then resize the glyph image itself
# to match the surrounding text height.
_EMOJI_STRIKE_SIZES = (109, 136, 96, 128, 64, 72, 160)


def _render_emoji_glyph(emoji_str, font_path, target_px):
    for candidate in (target_px,) + _EMOJI_STRIKE_SIZES:
        try:
            font = ImageFont.truetype(font_path, candidate)
        except OSError:
            continue
        canvas = Image.new('RGBA', (candidate * len(emoji_str) + 10, candidate + 10), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((0, 0), emoji_str, font=font, embedded_color=True)
        bbox = canvas.getbbox()
        if not bbox:
            return None
        glyph = canvas.crop(bbox)
        if target_px and glyph.height != target_px:
            scale = target_px / glyph.height
            glyph = glyph.resize((max(1, int(glyph.width * scale)), target_px), Image.LANCZOS)
        return glyph
    return None


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
        if mapped:
            print(f"  Captions: {len(mapped)} words across {len(story_clips)} clips" +
                  (" + hook card" if hook_png else ""))
        else:
            print("  Captions: no words mapped" +
                  (" — hook card only" if hook_png else " — no captions"))
        _final_encode(video_path, output_path, hook_png, mapped)
    finally:
        if hook_png:
            try:
                os.unlink(hook_png)
            except OSError:
                pass


def _probe_duration(video_path):
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
        capture_output=True, text=True
    )
    try:
        return float(probe.stdout.strip())
    except ValueError:
        return 60.0


def _loudnorm_filter(video_path):
    """
    Two-pass EBU R128 normalisation: measure first, then normalise linearly to
    _LOUD_I LUFS / _LOUD_TP dBTP so speech dynamics are kept. Falls back to the
    single-pass (dynamic) filter if the measurement fails or the clip is silent.
    """
    target = f"loudnorm=I={_LOUD_I}:TP={_LOUD_TP}:LRA={_LOUD_LRA}"
    p = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', video_path, '-vn',
                        '-af', f"{target}:print_format=json", '-f', 'null', '-'],
                       capture_output=True, text=True, errors='replace')
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', p.stderr)
    try:
        st = json.loads(m.group(0))
        if float(st['input_i']) < -70:          # silence / -inf
            raise ValueError('silent')
        return (f"{target}:measured_I={st['input_i']}:measured_TP={st['input_tp']}"
                f":measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}"
                f":offset={st['target_offset']}:linear=true,aresample=48000")
    except (AttributeError, KeyError, ValueError):
        return f"{target},aresample=48000"


def _final_encode(video_path, output_path, hook_png, mapped):
    """
    One encode for the finished Short: light enhance, hook banner overlay, then
    captions burned last (final-frame pixels, bundled font), with loudness-
    normalised stereo AAC 192k.

    ffmpeg runs from a private temp folder holding the .ass file and a copy of
    the font, referred to by bare name: a Windows drive letter ("C:") inside a
    filter graph breaks ffmpeg's option parser no matter how it is escaped.
    """
    video_path, output_path = os.path.abspath(video_path), os.path.abspath(output_path)
    enhance = "unsharp=5:5:0.6:3:3:0,eq=contrast=1.05:saturation=1.1"
    work = tempfile.mkdtemp(prefix='cap_')
    try:
        cap_filter = ""
        if mapped:
            with open(os.path.join(work, 'captions.ass'), 'w', encoding='utf-8') as f:
                f.write(_build_ass(mapped, _probe_duration(video_path)))
            font = os.path.join(_FONT_DIR, _FONT_FILE)
            if os.path.exists(font):
                shutil.copy(font, os.path.join(work, _FONT_FILE))
            else:
                print(f"  Caption font missing ({font}) — libass will substitute")
            cap_filter = ",ass=captions.ass:fontsdir=."

        inputs = ['-i', video_path]
        if hook_png:
            inputs += ['-i', os.path.abspath(hook_png)]
            filter_complex = f"[0:v]{enhance}[base];[base][1:v]overlay=0:0{cap_filter}[vout]"
        else:
            filter_complex = f"[0:v]{enhance}{cap_filter}[vout]"

        cmd = [
            'ffmpeg', '-y', *inputs,
            '-filter_complex', filter_complex,
            '-map', '[vout]', '-map', '0:a?',
            *_VIDEO_ARGS,
            '-af', _loudnorm_filter(video_path), *_AUDIO_ARGS,
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, cwd=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
