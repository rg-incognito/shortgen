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
final encode: peak limiter + two-pass linear loudnorm to -14 LUFS / -2 dBTP,
stereo AAC 192k.

With a render config (clipper resolves it from the profile) the template
look is used instead: Anton captions at a measured 64 px cap height with a
static keyword highlight, a Montserrat headline in the A1 blur band, and the
originality layer (context card, closing line, stage directions).
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
# Peak limiter ahead of loudnorm (dBFS); lowered per clip until linear mode holds.
_LIMIT_DB = -2.05      # = alimiter limit 0.79
# The AAC encode adds inter-sample overshoot on top of loudnorm's -2 dBTP
# (BILF measured -1.18 dBTP): the finished file is re-measured and, above this
# ceiling, its audio is re-encoded once with a lower true-peak target.
_TP_CEIL = -1.5
_TP_MARGIN = 0.3
_TP_TRIM_MAX = 0.6       # last resort linear trim (LUFS -14 -> at most -14.6)
_TP_POST = 0.3              # oversampled limiter this far under the target (AAC adds ~0.25 dB)
_TP_SAMPLE_MARGIN = 0.6    # sample-peak limiter -> true peak (measured +0.5 dB on BILF speech)
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


# --- Template captions and overlays (render config set; see clipper) ----------
#
# Captions stay Anton (bundled) but are sized from the font's OS/2 metrics so
# the measured cap height is 64 px (libass sizes the whole winAscent+winDescent
# box to Fontsize, so Anton "104" was only a 52 px cap). Static keyword
# highlight (at most one coloured word per chunk), centred (\an5) at a fixed
# per-template y. Headline / context card / closing line / stage directions
# use Montserrat (fonts/, SIL OFL 1.1, static instances of the google/fonts
# variable font), never a system font.
_MONT_XB   = "Montserrat-ExtraBold.ttf"
_MONT_XBI  = "Montserrat-ExtraBoldItalic.ttf"
_MONT_BLKI = "Montserrat-BlackItalic.ttf"
_MONT_SB   = "Montserrat-SemiBold.ttf"
_ASS_FONTS = (_FONT_FILE, _MONT_XBI, _MONT_BLKI)

_PRESETS = {
    # cap px, max words, max chars, lines, centre pop on entry
    'tv':        dict(cap=64, words=3, chars=14, lines=1, pop=False),
    'talk':      dict(cap=64, words=3, chars=14, lines=1, pop=True),
    'full_subs': dict(cap=62, words=7, chars=30, lines=2, pop=False, line_chars=17),
}
_TEMPLATE_CAPTION_Y = {'A1': 1160, 'A2': 1160, 'B1': 960}   # \an5 centre per template
_CAP_X2       = 530       # centre x: the chunk (max 800 wide) stays left of the right rail
_CAP_MAX_W    = 800
_CAP_MIN_FIT  = 85        # shrink-to-fit floor (\fscx %)
_CAP_CLAMP    = (880, 1320)
_SAFE_BOTTOM  = 1440      # nothing we draw goes below this (Shorts title / channel UI)
_OUTLINE_V2, _SHADOW_V2 = 4, 4
_SHADOW_V2_C  = "&H80000000&"        # 50 % black
_YELLOW_V2    = "&H0000E6FF&"        # #FFE600
_GREEN        = "&H006BE62E&"        # #2EE66B numbers / money
_RED          = "&H00303BFF&"        # #FF3B30 negatives
_CHUNK_GAP_V2, _CHUNK_HOLD_V2, _CHUNK_MIN_V2 = 0.25, 0.60, 0.25
_CAPTION_LEAD_V2 = 0.04
_KEYWORD_SHARE = 0.45     # at most this share of chunks carries a coloured word
_POP_V2 = r"\fscx{a}\fscy85\t(0,67,\fscx{b}\fscy112)\t(67,133,\fscx{c}\fscy100)"

_NUM_RE = re.compile(r"\d|[$%£€]|^(MILLION|BILLION|THOUSAND|HUNDRED|HOURS?|YEARS?|DOLLARS?|BUCKS)$")
_NEG_WORDS = {"RAW", "WRONG", "FIRED", "DEAD", "NEVER", "WORST", "HATE", "STOP", "NO", "DISGUSTING",
              "LIED", "BROKE", "DIVORCE", "DUMPED", "CHEATED", "KILL", "QUIT", "TERRIBLE", "HORRIBLE"}
_STOPWORDS = {"A", "AN", "THE", "TO", "OF", "AND", "I", "IT", "IS", "IN", "ON", "AT", "FOR", "YOU",
              "WE", "HE", "SHE", "THEY", "SO", "BUT", "OR", "MY", "YOUR", "HIS", "HER", "OUR",
              "THEIR", "WITH", "THAT", "THIS", "BE", "ARE", "WAS", "AM", "IF", "AS", "BY", "FROM",
              "I'M", "IT'S", "DON'T", "JUST", "LIKE", "UM", "UH", "OH"}
# Names that always get the name colour (the channel's main show); profiles add
# their own through render['names'] and stories through story['keywords'].
_DEFAULT_NAMES = {"ROSS", "RACHEL", "MONICA", "CHANDLER", "JOEY", "PHOEBE", "GUNTHER", "JANICE"}

# Headline band (A1), context card, closing line, stage directions, handle.
_HEAD_BAND     = (90, 280)
_HEAD_SIZE     = 64       # Pillow px (Montserrat cap height = 0.70 x size -> ~45 px)
_HEAD_MIN      = 52
_HEAD_MAX_W    = 1000
_HEAD_PITCH    = 76
_HEAD_STROKE   = 3
_CARD_SIZE     = 56
_CARD_MIN      = 44
_CARD_MAX_W    = 900
_CARD_GAP      = 110      # card top this far below the caption centre
                          # (the eye line sits at ~y 540, so a card there covers the face)
_CARD_END      = 2.5      # s
_CLOSE_EM      = 60       # Montserrat ExtraBold Italic, em px (header look)
_CLOSE_MIN_EM  = 44
_CLOSE_MAX_W   = 900
_CLOSE_Y       = 1065     # A templates; B1 uses the caption position
_CLOSE_MIN_S, _CLOSE_MAX_S = 1.0, 1.5
_TAG_CAP       = 40
_TAG_S         = 1.0
_TAG_ABOVE     = 95       # tag centre sits this far above the caption centre
_HANDLE_SIZE   = 30
_HANDLE_ALPHA  = 178      # 70 %
_HANDLE_A1_Y   = 1700


def _font_path(name):
    return os.path.join(_FONT_DIR, name)


def _font_metrics(path):
    """(unitsPerEm, winAscent, winDescent, capHeight) read straight from the TTF tables."""
    import struct
    with open(path, 'rb') as f:
        data = f.read()
    n = struct.unpack('>H', data[4:6])[0]
    tables = {}
    for i in range(n):
        tag, _, off, _ = struct.unpack('>4sIII', data[12 + 16 * i:28 + 16 * i])
        tables[tag] = off
    head, os2 = tables[b'head'], tables[b'OS/2']
    upm = struct.unpack('>H', data[head + 18:head + 20])[0]
    version = struct.unpack('>H', data[os2:os2 + 2])[0]
    win_a, win_d = struct.unpack('>HH', data[os2 + 74:os2 + 78])
    cap = struct.unpack('>h', data[os2 + 88:os2 + 90])[0] if version >= 2 else int(upm * 0.7)
    return upm, win_a, win_d, cap


def _ass_fontsize(path, cap_px):
    """ASS Fontsize that renders a cap height of cap_px under libass."""
    _, win_a, win_d, cap = _font_metrics(path)
    return int(round(cap_px * (win_a + win_d) / cap))


def _ass_em(path, fontsize):
    """Pillow em size (px) matching an ASS Fontsize, for width measurement."""
    upm, win_a, win_d, _ = _font_metrics(path)
    return fontsize * upm / (win_a + win_d)


def _bare(text):
    return re.sub(r"[^\w$%£€']", "", text)


_PUNCT_END = re.compile(r'[.!?,]["\')\]]*$')


def _chunk_words_v2(mapped_words, max_words=3, max_chars=14):
    """1-3 word chunks; breaks on .?!, , pauses > 0.25 s; never ends on a stopword;
    chunks shown < 0.25 s merge into the next one."""
    words = []
    for w in mapped_words:
        text = w['word'].strip().upper()
        if text:
            words.append(dict(w, text=text))
    chunks, cur = [], []
    for w in words:
        if cur:
            prev = cur[-1]
            line = " ".join(x['text'] for x in cur + [w])
            if (len(cur) >= max_words or len(line) > max_chars
                    or w['start'] - prev['end'] > _CHUNK_GAP_V2
                    or _PUNCT_END.search(prev['text'])):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    # a chunk never ends on a lone article / pronoun / preposition
    for i in range(len(chunks) - 1):
        c, nxt = chunks[i], chunks[i + 1]
        while (len(c) > 1 and _bare(c[-1]['text']) in _STOPWORDS
               and not _PUNCT_END.search(c[-1]['text'])
               and nxt[0]['start'] - c[-1]['end'] <= _CHUNK_GAP_V2
               and len(nxt) <= max_words
               and len(" ".join(x['text'] for x in [c[-1]] + nxt)) <= max_chars + 6):
            nxt.insert(0, c.pop())

    # minimum on-screen time: fold a too-short chunk into the next one
    out = []
    for i, c in enumerate(chunks):
        if out and out[-1] is not None:
            prev = out[-1]
            if (c[0]['start'] - prev[0]['start'] < _CHUNK_MIN_V2
                    and len(prev) + len(c) <= max_words + 1
                    and not re.search(r'[.!?]["\')\]]*$', prev[-1]['text'])):
                out[-1] = prev + c
                continue
        out.append(c)
    return out


def _wrap_two(texts, limit):
    """Split words into <= 2 balanced lines of <= limit chars (full_subs)."""
    line = " ".join(texts)
    if len(line) <= limit or len(texts) < 2:
        return [texts]
    best = None
    for k in range(1, len(texts)):
        a, b = " ".join(texts[:k]), " ".join(texts[k:])
        score = max(len(a), len(b))
        if best is None or score < best[0]:
            best = (score, k)
    return [texts[:best[1]], texts[best[1]:]]


def _keyword_pick(chunk, names, sentence_end):
    """(index, colour, priority) of the one word to colour in this chunk, or None."""
    words = [_bare(w['text']) for w in chunk]
    for i, w in enumerate(words):
        if _NUM_RE.search(w):
            return i, _GREEN, 3
    for i, w in enumerate(words):
        if w in _NEG_WORDS:
            return i, _RED, 2
    for i, w in enumerate(words):
        base = w[:-2] if w.endswith("'S") else w
        if w in names or base in names:
            return i, _YELLOW_V2, 2
    if sentence_end and len(words) >= 2:
        for i in range(len(words) - 1, -1, -1):
            if words[i] not in _STOPWORDS and len(words[i]) > 2:
                return i, _YELLOW_V2, 1
    return None


def _measure_chunk(text, fontsize):
    path = _font_path(_FONT_FILE)
    try:
        font = ImageFont.truetype(path, max(1, int(round(_ass_em(path, fontsize)))))
        return font.getlength(text) + len(text)          # + Spacing 1 per glyph
    except OSError:
        return 0


def _closing_lines(text, path, max_w=_CLOSE_MAX_W):
    """(em, [line1, line2?]) for the closing line: Montserrat, caps, <= 2 lines."""
    text = " ".join(text.upper().split())
    img = ImageDraw.Draw(Image.new('L', (8, 8)))
    em = _CLOSE_EM
    while True:
        font = ImageFont.truetype(path, em)
        lines = _wrap_lines(img, text, font, max_w)
        if len(lines) <= 2 or em <= _CLOSE_MIN_EM:
            return em, lines[:2]
        em -= 4


def _ass_escape(text):
    return text.replace('\\', '').replace('{', '(').replace('}', ')').replace('\n', ' ')


def _build_ass_v2(mapped, total_duration, cfg, overlays):
    """
    Template captions + originality overlays in one ASS script.

    cfg: caption_preset, caption_y, names (upper-case set), template.
    overlays: closing (text), closing_y, closing_start, tags [(t, text)],
    caption_end (s; captions clear here so the closing line has the screen).
    """
    P = _PRESETS.get(cfg.get('caption_preset'), _PRESETS['tv'])
    cy = int(cfg.get('caption_y', 1160))
    fs = _ass_fontsize(_font_path(_FONT_FILE), P['cap'])
    close_path = _font_path(_MONT_XBI)
    tag_fs = _ass_fontsize(_font_path(_MONT_BLKI), _TAG_CAP)
    close_em, close_lines = (_closing_lines(overlays['closing'], close_path)
                             if overlays.get('closing') else (_CLOSE_EM, []))
    close_fs = int(round(close_em * (lambda m: (m[1] + m[2]) / m[0])(_font_metrics(close_path))))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {_OUT_W}
PlayResY: {_OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{_FONT},{fs},{_WHITE},&H000000FF&,{_OUTLINE},{_SHADOW_V2_C},0,0,0,0,100,100,1,0,1,{_OUTLINE_V2},{_SHADOW_V2},5,0,0,0,1
Style: Close,Montserrat ExtraBold,{close_fs},{_WHITE},&H000000FF&,{_OUTLINE},&H66000000&,0,1,0,0,100,100,0,0,1,3,4,5,0,0,0,1
Style: Tag,Montserrat Black,{tag_fs},{_YELLOW_V2},&H000000FF&,{_OUTLINE},&H80000000&,0,1,0,0,100,100,0,0,1,3,3,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    chunks = _chunk_words_v2(mapped, P['words'], P['chars'])
    names = cfg.get('names') or _DEFAULT_NAMES
    picks = [_keyword_pick(c, names, bool(re.search(r'[.!?]["\')\]]*$', c[-1]['text'])))
             for c in chunks]
    # keep the highlight rare: drop the weakest picks above the share cap
    limit = int(_KEYWORD_SHARE * len(chunks))
    order = sorted((i for i, p in enumerate(picks) if p), key=lambda i: (picks[i][2], -i))
    while sum(1 for p in picks if p) > limit and order:
        picks[order.pop(0)] = None

    starts = [max(0.0, c[0]['start'] - _CAPTION_LEAD_V2) for c in chunks]
    if starts and starts[0] < 0.3:
        starts[0] = 0.0
    cap_end = min(total_duration, overlays.get('caption_end') or total_duration)
    for ci, chunk in enumerate(chunks):
        s = starts[ci]
        if ci + 1 < len(chunks):
            e = min(starts[ci + 1], chunk[-1]['end'] + _CHUNK_HOLD_V2)
        else:
            e = cap_end                     # the last chunk stays up (until the closing line)
        e = min(e, cap_end) if ci + 1 == len(chunks) else e
        if e - s < 0.04:
            continue
        kw = picks[ci]
        texts = [w['text'] for w in chunk]
        lines = _wrap_two(texts, P.get('line_chars', P['chars'])) if P['lines'] > 1 else [texts]
        rendered, j = [], 0
        for ln in lines:
            parts = []
            for t in ln:
                parts.append((r"{\c" + kw[1] + "}" + t + r"{\c" + _WHITE + "}") if kw and j == kw[0] else t)
                j += 1
            rendered.append(" ".join(parts))
        width = max(_measure_chunk(" ".join(ln), fs) for ln in lines)
        fx = 100 if not width or width <= _CAP_MAX_W else max(_CAP_MIN_FIT, int(100 * _CAP_MAX_W / width))
        fit = rf"\fscx{fx}" if fx < 100 else ""
        anim = (_POP_V2.format(a=round(85 * fx / 100), b=round(112 * fx / 100), c=fx)
                if P['pop'] else "")
        line = rf"{{\an5\pos({_CAP_X2},{cy}){fit}{anim}}}" + r"\N".join(rendered)
        events.append(f"Dialogue: 1,{_format_ass_time(s)},{_format_ass_time(e)},Cap,,0,0,0,,{line}")

    for t, text in overlays.get('tags') or ():
        t1 = min(total_duration, t + _TAG_S)
        if t1 - t < 0.3:
            continue
        pop = _POP_V2.format(a=85, b=112, c=100)
        events.append(f"Dialogue: 2,{_format_ass_time(t)},{_format_ass_time(t1)},Tag,,0,0,0,,"
                      rf"{{\an5\pos(540,{cy - _TAG_ABOVE}){pop}}}" + _ass_escape(text.upper()))

    if close_lines:
        body = _ass_escape(close_lines[0])
        if len(close_lines) > 1:
            body += r"\N{\c" + _YELLOW_V2 + "}" + _ass_escape(close_lines[1])
        events.append(f"Dialogue: 3,{_format_ass_time(overlays['closing_start'])},"
                      f"{_format_ass_time(total_duration + 0.1)},Close,,0,0,0,,"
                      rf"{{\an5\pos(540,{overlays['closing_y']})}}" + body)
    return header + "\n".join(events) + "\n"


def _text_layer(lines, font, colours, centre_y, pitch, stroke=_HEAD_STROKE):
    """Transparent 1080x1920 layer: centred lines with a black stroke and a soft drop shadow."""
    img = Image.new('RGBA', (_OUT_W, _OUT_H), (0, 0, 0, 0))
    shadow = Image.new('RGBA', (_OUT_W, _OUT_H), (0, 0, 0, 0))
    d, sd = ImageDraw.Draw(img), ImageDraw.Draw(shadow)
    top = centre_y - pitch * len(lines) / 2
    for k, (line, colour) in enumerate(zip(lines, colours)):
        w = d.textlength(line, font=font)
        x = (_OUT_W - w) / 2
        y = top + pitch * k + pitch / 2
        sd.text((x, y + 4), line, font=font, fill=(0, 0, 0, 153), anchor='lm',
                stroke_width=stroke, stroke_fill=(0, 0, 0, 153))
        d.text((x, y), line, font=font, fill=colour, anchor='lm',
               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    return Image.alpha_composite(shadow, img)


def _balanced_two(draw, text, font, max_w):
    """Two lines of similar width (the A1 headline look: white line, yellow line)."""
    words = text.split()
    if len(words) < 3:
        return [text]
    best = None
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        wa, wb = draw.textlength(a, font=font), draw.textlength(b, font=font)
        if max(wa, wb) <= max_w and (best is None or abs(wa - wb) < best[0]):
            best = (abs(wa - wb), [a, b])
    return best[1] if best else _wrap_lines(draw, text, font, max_w)


def render_overlay_png(out_path, template, header=None, handle=None, handle_y=None, handle_size=None):
    """
    Persistent overlay layer: the A1 headline in the top blur band (Montserrat
    ExtraBold Italic, ALL CAPS, line 1 white / line 2 #FFE600, no emoji) and the
    optional channel handle (or a brief-required show label, drawn at
    handle_size px, fully opaque). Returns the handle's box (x0, y0, x1, y1) or None.
    """
    layer = Image.new('RGBA', (_OUT_W, _OUT_H), (0, 0, 0, 0))
    if header and template == 'A1':
        text = " ".join(header.upper().split())
        d = ImageDraw.Draw(layer)
        size = _HEAD_SIZE
        while True:
            font = ImageFont.truetype(_font_path(_MONT_XBI), size)
            lines = _balanced_two(d, text, font, _HEAD_MAX_W)
            if len(lines) <= 2 or size <= _HEAD_MIN:
                break
            size -= 4
        lines = lines[:2]
        pitch = int(round(_HEAD_PITCH * size / _HEAD_SIZE))
        cols = [(255, 255, 255, 255), (255, 230, 0, 255)][:len(lines)]
        layer = Image.alpha_composite(layer, _text_layer(lines, font, cols, sum(_HEAD_BAND) / 2, pitch))
    box = None
    if handle:
        size = int(handle_size or _HANDLE_SIZE)
        label = size > _HANDLE_SIZE          # a required show label: opaque, heavier stroke
        font = ImageFont.truetype(_font_path(_MONT_XB if label else _MONT_SB), size)
        d = ImageDraw.Draw(layer)
        w = d.textlength(handle, font=font)
        y = handle_y or _HANDLE_A1_Y
        hl = Image.new('RGBA', (_OUT_W, _OUT_H), (0, 0, 0, 0))
        ImageDraw.Draw(hl).text(((_OUT_W - w) / 2, y), handle, font=font, anchor='lm',
                                fill=(255, 255, 255, 255 if label else _HANDLE_ALPHA),
                                stroke_width=3 if label else 1,
                                stroke_fill=(0, 0, 0, 200 if label else 90))
        layer = Image.alpha_composite(layer, hl)
        box = (int((_OUT_W - w) / 2) - 4, int(y - size), int((_OUT_W + w) / 2) + 4,
               int(y + size))
    layer.save(out_path)
    return box


# Brand watermark (a brief-required logo PNG on every frame, e.g. "@ladbible").
# render['watermark'] = {'file': 'assets/watermarks/x.png' (repo-relative), 'width_frac': share of
# the 1080 px width, 'pos': 'top' | 'center_low', optional 'y' (top edge px, overrides pos)}.
# Placement stays inside the Shorts safe zone: above y _SAFE_BOTTOM (title / channel UI), left
# of the right-hand button rail (x 990), clear of the A1 header band and the caption line.
_REPO_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
_WM_TOP        = {'A1': _HEAD_BAND[1] + 24, 'A2': 150, 'B1': 150}   # 'top': top edge per template
_WM_LOW_GAP    = 150      # 'center_low': top edge this far below the caption centre
_WM_RAIL_X     = 990      # right-rail buttons start here
_WM_MAX_W      = 2 * (_WM_RAIL_X - 10 - _OUT_W // 2)    # centred at x 540, ends left of the rail
_WM_MIN_W      = 240


def watermark_path(wm):
    """Absolute path of a watermark config's PNG (repo-relative 'file')."""
    f = str((wm or {}).get('file') or '')
    return f if os.path.isabs(f) else os.path.normpath(os.path.join(_REPO_DIR, f))


def watermark_box(wm, template='B1', caption_y=None, cap=64, size=None):
    """
    (x0, y0, x1, y1) of the watermark on the 1080x1920 canvas. size: the
    PNG's (w, h) (read from the file when None). Raises ValueError on a bad
    config or when the box would leave the safe zone (never drawn cropped).
    """
    if size is None:
        with Image.open(watermark_path(wm)) as im:
            size = im.size
    sw, sh = size
    w = int(round(float(wm.get('width_frac') or 0.5) * _OUT_W))
    w = max(_WM_MIN_W, min(_WM_MAX_W, w))
    h = int(round(w * sh / sw))
    if wm.get('y') is not None:
        y0 = int(wm['y'])
    else:
        pos = wm.get('pos') or 'top'
        if pos == 'top':
            y0 = _WM_TOP.get(template, _WM_TOP['B1'])
        elif pos == 'center_low':
            cy = int(caption_y or _TEMPLATE_CAPTION_Y.get(template, 1160))
            y0 = min(cy + _WM_LOW_GAP, _SAFE_BOTTOM - 10 - h)
        else:
            raise ValueError(f"watermark pos {pos!r} (use 'top' or 'center_low')")
    x0 = (_OUT_W - w) // 2
    box = (x0, y0, x0 + w, y0 + h)
    if box[0] < 0 or box[1] < 0 or box[2] > _WM_RAIL_X or box[3] > _SAFE_BOTTOM:
        raise ValueError(f"watermark box {box} leaves the safe zone (y <= {_SAFE_BOTTOM}, x <= {_WM_RAIL_X})")
    return box


def render_watermark_png(out_path, wm, template='B1', caption_y=None, cap=64):
    """
    Full-canvas transparent PNG with the brand watermark scaled (Lanczos,
    alpha kept) into watermark_box. Returns the box. A missing file raises:
    a brief-required watermark must never silently drop off a render.
    """
    path = watermark_path(wm)
    if not os.path.exists(path):
        raise FileNotFoundError(f"watermark file missing: {path}")
    logo = Image.open(path).convert('RGBA')
    box = watermark_box(wm, template, caption_y, cap, size=logo.size)
    logo = logo.resize((box[2] - box[0], box[3] - box[1]), Image.LANCZOS)
    layer = Image.new('RGBA', (_OUT_W, _OUT_H), (0, 0, 0, 0))
    layer.alpha_composite(logo, (box[0], box[1]))
    layer.save(out_path)
    return box


def render_card_png(text, out_path, centre_y=None, top_y=None, max_bottom=_SAFE_BOTTOM - 10):
    """Context card: black Montserrat ExtraBold on a white rounded box (radius 24)."""
    text = " ".join(text.split())
    layer = Image.new('RGBA', (_OUT_W, _OUT_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    size = _CARD_SIZE
    while True:
        font = ImageFont.truetype(_font_path(_MONT_XB), size)
        lines = _wrap_lines(d, text, font, _CARD_MAX_W - 80)
        if len(lines) <= 2 or size <= _CARD_MIN:
            break
        size -= 4
    lines = lines[:2]
    pitch = int(size * 1.25)
    w = max(d.textlength(ln, font=font) for ln in lines)
    bw, bh = w + 80, pitch * len(lines) + 56 - (pitch - size)
    y0 = top_y if top_y is not None else centre_y - bh / 2
    y0 = min(y0, max_bottom - bh)
    x0 = (_OUT_W - bw) / 2
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=24, fill=(255, 255, 255, 255))
    for k, ln in enumerate(lines):
        lw = d.textlength(ln, font=font)
        d.text(((_OUT_W - lw) / 2, y0 + 28 + pitch * k + size / 2), ln, font=font,
               fill=(17, 17, 17, 255), anchor='lm')
    layer.save(out_path)
    return (int(x0), int(y0), int(x0 + bw), int(y0 + bh))


def burn_captions(video_path, output_path, all_words, story_clips,
                   hook_text=None, hook_emoji=None, render=None, overlays=None):
    """
    Final encode of the Short: picture enhance, banner / template overlays,
    captions burned last, loudness-normalised audio.

    render None: today's look (white hook banner, moving-highlight captions).
    render (resolved by clipper): template, caption_preset, caption_y, names,
    handle; overlays: header, context, closing, tags (output seconds),
    last_word_end. Returns {'normalization_type', 'allowed_boxes', 'closing'}.
    """
    mapped = _remap_words(all_words, story_clips)
    if render:
        return _burn_v2(video_path, output_path, mapped, render, overlays or {})

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
        return _final_encode(video_path, output_path, hook_png, mapped)
    finally:
        if hook_png:
            try:
                os.unlink(hook_png)
            except OSError:
                pass


def _burn_v2(video_path, output_path, mapped, cfg, ov):
    dur = _probe_duration(video_path)
    template = cfg.get('template', 'A1')
    cy = int(cfg.get('caption_y', _TEMPLATE_CAPTION_Y.get(template, 1160)))
    work = tempfile.mkdtemp(prefix='ovl_')
    try:
        pngs, allowed = [], []
        handle_y = (_HANDLE_A1_Y if template == 'A1'
                    else min(_SAFE_BOTTOM - 20, cy + _PRESETS.get(cfg.get('caption_preset'), _PRESETS['tv'])['cap'] // 2 + 40))
        header = ov.get('header') if template == 'A1' else None
        if header or cfg.get('handle'):
            p = os.path.join(work, 'layer.png')
            box = render_overlay_png(p, template, header, cfg.get('handle'), handle_y, cfg.get('handle_size'))
            pngs.append((p, None))
            if box and box[3] > _SAFE_BOTTOM:
                allowed.append(box)
        if ov.get('context'):
            p = os.path.join(work, 'card.png')
            # under the caption on every template: higher up it covers the head
            render_card_png(ov['context'], p, top_y=cy + _CARD_GAP)
            pngs.append((p, min(_CARD_END, dur)))
        # brand watermark: on every frame, composited last so no layer of ours covers it
        wm_box = None
        if cfg.get('watermark'):
            p = os.path.join(work, 'watermark.png')
            cap = _PRESETS.get(cfg.get('caption_preset'), _PRESETS['tv'])['cap']
            wm_box = render_watermark_png(p, cfg['watermark'], template, cy, cap)
            pngs.append((p, None))

        # closing line rides the tail hold; captions clear when it starts
        script_ov = {'tags': ov.get('tags') or []}
        last_end = ov.get('last_word_end') or dur        # no transcript words: hold = whole end
        if ov.get('closing'):
            start = max(dur - _CLOSE_MAX_S, min(last_end + 0.05, dur - _CLOSE_MIN_S))
            if start >= last_end - 0.15:
                start = max(start, last_end)
                y = _CLOSE_Y if template in ('A1', 'A2') else cy
                script_ov['caption_end'] = start
            else:
                # B tails are short: the closing line shares the screen with the
                # last caption, so it sits under it (never below the safe line)
                y = min(cy + 190, _SAFE_BOTTOM - 90)
            script_ov.update(closing=ov['closing'], closing_start=start, closing_y=y)
        print(f"  Captions: {len(mapped)} words, template {template}, preset "
              f"{cfg.get('caption_preset', 'tv')}, y {cy}" +
              (" + header" if header else "") + (" + context card" if ov.get('context') else "") +
              (" + closing line" if ov.get('closing') else "") +
              (f" + {len(script_ov['tags'])} tag(s)" if script_ov['tags'] else "") +
              (f" + watermark {wm_box}" if wm_box else ""))
        with open(os.path.join(work, 'captions.ass'), 'w', encoding='utf-8') as f:
            f.write(_build_ass_v2(mapped, dur, dict(cfg, caption_y=cy), script_ov))
        for name in _ASS_FONTS:
            if os.path.exists(_font_path(name)):
                shutil.copy(_font_path(name), os.path.join(work, name))
            else:
                print(f"  Font missing ({name}) — libass will substitute")

        inputs, chains, last = ['-i', os.path.abspath(video_path)], [], 'base'
        chains.append("[0:v]unsharp=5:5:0.6:3:3:0,eq=contrast=1.05:saturation=1.1[base]")
        for k, (p, until) in enumerate(pngs, 1):
            inputs += ['-i', p]
            enable = f":enable='lt(t,{until:.2f})'" if until else ""
            chains.append(f"[{last}][{k}:v]overlay=0:0{enable}[o{k}]")
            last = f"o{k}"
        chains.append(f"[{last}]ass=captions.ass:fontsdir=.[vout]")
        af, info = _loudness_chain(video_path)
        cmd = ['ffmpeg', '-y', *inputs, '-filter_complex', ";".join(chains),
               '-map', '[vout]', '-map', '0:a?', *_VIDEO_ARGS, '-af', af, *_AUDIO_ARGS,
               os.path.abspath(output_path)]
        p = subprocess.run(cmd, capture_output=True, text=True, errors='replace', cwd=work)
        if p.returncode != 0:
            raise subprocess.CalledProcessError(p.returncode, cmd, p.stdout, p.stderr)
        info['normalization_type'] = _norm_type(p.stderr)
        _cap_true_peak(video_path, output_path, info)
        info['allowed_boxes'] = allowed
        info['caption_y'] = cy
        info['watermark_box'] = wm_box
        info['closing'] = script_ov.get('closing_start')
        return info
    finally:
        shutil.rmtree(work, ignore_errors=True)


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


def _measure_loudness(video_path, pre=""):
    """loudnorm pass-1 stats of the file's audio (after the optional `pre` filter)."""
    target = f"loudnorm=I={_LOUD_I}:TP={_LOUD_TP}:LRA={_LOUD_LRA}:print_format=json"
    p = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', video_path, '-vn',
                        '-af', (pre + "," if pre else "") + target, '-f', 'null', '-'],
                       capture_output=True, text=True, errors='replace')
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', p.stderr)
    st = json.loads(m.group(0))
    if float(st['input_i']) < -70:          # silence / -inf
        raise ValueError('silent')
    return st


def _norm_type(stderr):
    m = re.search(r'"normalization_type"\s*:\s*"(\w+)"', stderr or "")
    return m.group(1) if m else None


def _tp_guard(tp):
    """
    True-peak guard after loudnorm: limit at 4x oversampling (192 kHz sample
    peaks ~ true peaks) _TP_POST under the target, then back to 48 kHz. The
    linear loudnorm output still overshot by ~1 dB (inter-sample peaks of the
    sample-peak-limited speech, then the AAC encode): BILF -1.18 / -1.36 dBTP.
    Measured on the BILF clip: -2.06 dBTP after AAC, loudness -14.08 LUFS.
    """
    return (f",aresample=192000,alimiter=limit={10 ** ((tp - _TP_POST) / 20):.4f}"
            f":attack=1:release=50:level=disabled,aresample=48000")


def _loudness_chain(video_path, tp=_LOUD_TP):
    """
    Peak limiter + two-pass linear loudnorm, so speech and laugh-track dynamics
    survive. loudnorm only runs linear when (measured TP + gain) <= target TP;
    a raw episode is usually too peaky for that and silently fell back to the
    dynamic (compressing) mode — test1 measured an LRA of ~2 LU. The limiter
    shaves the few peaks that would block linear mode, lowered until the
    measurement predicts linear (at most 3 tries).
    Returns (filter, info) with info = limit_db and predicted mode.

    The loudness gain is applied BEFORE the limiter, so the limiter works near
    0 dBFS: on quiet sources (podcasts at ~-34 LUFS) a limit of -22..-24 dB hit
    alimiter's floor (0.0625 = -24 dB) and peaks slipped through. alimiter is a
    sample-peak limiter, so it sits _TP_SAMPLE_MARGIN under the true-peak target.
    After loudnorm, _tp_guard limits again at 4x oversampling (= true peak).
    """
    try:
        st = _measure_loudness(video_path)
    except (AttributeError, KeyError, ValueError):
        return f"loudnorm=I={_LOUD_I}:TP={tp}:LRA={_LOUD_LRA}" + _tp_guard(tp), {"limit_db": None}
    gain = _LOUD_I - float(st['input_i'])
    vol = f"volume={gain:.2f}dB," if abs(gain) >= 0.05 else ""
    limit_db = min(_LIMIT_DB + (tp - _LOUD_TP), tp - _TP_SAMPLE_MARGIN)
    for _ in range(3):
        limit_db = max(limit_db, -23.5)              # alimiter's floor is -24 dB
        pre = f"{vol}alimiter=limit={10 ** (limit_db / 20):.4f}:attack=5:release=50:level=disabled"
        try:
            st = _measure_loudness(video_path, pre)
        except (AttributeError, KeyError, ValueError):
            break
        over = float(st['input_tp']) + (_LOUD_I - float(st['input_i'])) - tp
        if over <= 0:
            break
        limit_db -= over + 0.2
    lra = min(20.0, max(_LOUD_LRA, float(st['input_lra']) + 1.0))
    ln = (f"loudnorm=I={_LOUD_I}:TP={tp:.2f}:LRA={lra:.1f}"
          f":measured_I={st['input_i']}:measured_TP={st['input_tp']}"
          f":measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}"
          f":offset={st['target_offset']}:linear=true:print_format=json" + _tp_guard(tp))
    return (pre + "," if pre else "") + ln, {'limit_db': round(limit_db, 2)}


def _cap_true_peak(source, output_path, info, ceil=_TP_CEIL):
    """
    Keep the finished file's true peak <= ceil: measure it and, if it is
    over, re-encode only the audio (video stream copied) from `source` with
    the true-peak target lowered by the overshoot. One retry; never raises.
    Records true_peak (and tp_fixed) in info.
    """
    try:
        tp = float(_measure_loudness(output_path)['input_tp'])
    except Exception:
        return info
    info['true_peak'] = tp
    if tp <= ceil:
        return info
    first, target, trim = tp, max(-9.0, _LOUD_TP - (tp - (ceil - _TP_MARGIN))), 0.0   # loudnorm: TP -9..0
    af, _ = _loudness_chain(source, tp=round(target, 2))
    stderr = ''
    for step in range(2):
        # 2nd pass (still over): a small linear trim after loudnorm, <= _TP_TRIM_MAX dB
        # so the loudness stays inside the -14 +- 0.7 LUFS QA band
        chain = af + (f",volume=-{trim:.2f}dB" if trim else "")
        tmp = output_path + '.tp.mp4'
        p = subprocess.run(['ffmpeg', '-y', '-i', os.path.abspath(output_path), '-i', os.path.abspath(source),
                            '-map', '0:v', '-map', '1:a?', '-c:v', 'copy', '-af', chain, *_AUDIO_ARGS,
                            '-movflags', '+faststart', tmp],
                           capture_output=True, text=True, errors='replace')
        if p.returncode != 0 or not os.path.exists(tmp):
            print(f"  True peak {tp:.2f} dBTP: audio re-encode failed, kept")
            break
        os.replace(tmp, output_path)
        stderr = p.stderr
        try:
            tp = float(_measure_loudness(output_path)['input_tp'])
        except Exception:
            break
        if tp <= ceil:
            break
        trim = min(_TP_TRIM_MAX, tp - (ceil - _TP_MARGIN) + trim)
    print(f"  True peak {first:.2f} dBTP > {ceil} -> audio re-encoded (TP target {target:.2f}"
          f"{f', trim -{trim:.2f} dB' if trim else ''}): {tp:.2f} dBTP")
    info.update(true_peak=tp, tp_fixed=round(first, 2), tp_trim_db=round(trim, 2),
                normalization_type=_norm_type(stderr) or info.get('normalization_type'))
    return info


def _loudnorm_filter(video_path):
    """Audio filter for the final encode (legacy name; see _loudness_chain)."""
    return _loudness_chain(video_path)[0]


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

        af, info = _loudness_chain(video_path)
        cmd = [
            'ffmpeg', '-y', *inputs,
            '-filter_complex', filter_complex,
            '-map', '[vout]', '-map', '0:a?',
            *_VIDEO_ARGS,
            '-af', af, *_AUDIO_ARGS,
            output_path,
        ]
        p = subprocess.run(cmd, capture_output=True, text=True, errors='replace', cwd=work)
        if p.returncode != 0:
            raise subprocess.CalledProcessError(p.returncode, cmd, p.stdout, p.stderr)
        info['normalization_type'] = _norm_type(p.stderr)
        _cap_true_peak(video_path, output_path, info)
        return info
    finally:
        shutil.rmtree(work, ignore_errors=True)
