"""
Burned-in source text detector (opening credits, show titles, "previously on").

Sitcom episodes burn their credits ("Teleplay by ...", "Directed by ...")
into the picture over the first scenes. A Short cut from that window looks
like a raw rip, so clip selection rejects such windows and render QA checks
the first 8 s of every finished Short.

Heuristic, no OCR (cheap enough for CI):
  1. sample the window at 2 fps, 360 px wide, greyscale;
  2. bright, thin strokes = white top-hat response on bright pixels;
  3. glyph-like connected components (text-sized, not too solid / not lines);
  4. a text line = >= 4 glyphs of similar height on one baseline, close
     together, wide enough, with dense vertical edges (letter stems);
  5. a frame is flagged when it has a text line; text must persist on the
     same spot for >= 0.5 s (2 consecutive samples) to count, which drops
     passing signs, shirts and other moving clutter.

burned_text(video, start, end) -> [(t0, t1, band)], times in the video's own
timeline, band 'top' | 'middle' | 'bottom' (thirds of the frame height).
"""
import json
import subprocess

import cv2
import numpy as np

_SAMPLE_FPS = 2.0
_WIDTH = 360
_TOPHAT_K = 15           # px at 360 wide: strokes thinner than this
_TOPHAT_MIN = 45         # stroke must be this much brighter than its surroundings
_BRIGHT_MIN = 165        # and bright in absolute terms (white / cream text)
_GLYPH_H = (6, 44)       # glyph height range, px at 360 wide
_LINE_MIN_GLYPHS = 4
_LINE_MIN_W = 0.12       # a text line spans at least this share of the width
_EDGE_MIN = 0.10         # share of strong vertical-gradient pixels in the line box
_FG_MIN = 195            # mean stroke brightness of a text line
_CONTRAST_MIN = 105      # stroke minus background brightness
_INK_MAX = 0.30          # stroke share of the line box (solid shapes are not text)
_PERSIST_IOU = 0.3       # same text in consecutive samples


def _probe_size(video):
    p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=width,height', '-of', 'json', video],
                       capture_output=True, text=True)
    try:
        s = json.loads(p.stdout)['streams'][0]
        return int(s['width']), int(s['height'])
    except (ValueError, KeyError, IndexError):
        return 1920, 1080


def _frames(video, start, end, fps=_SAMPLE_FPS, width=_WIDTH):
    """Yield (t, grey uint8 frame) sampled at fps over [start, end)."""
    w, h = _probe_size(video)
    out_h = max(2, int(round(width * h / w / 2)) * 2)
    cmd = ['ffmpeg', '-v', 'error', '-ss', f'{max(0.0, start):.3f}', '-i', video,
           '-t', f'{max(0.1, end - start):.3f}', '-an', '-sn',
           '-vf', f'fps={fps},scale={width}:{out_h},format=gray',
           '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = width * out_h
    k = 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield start + k / fps, np.frombuffer(buf, np.uint8).reshape(out_h, width)
            k += 1
    finally:
        proc.stdout.close()
        proc.wait()


def text_lines(grey, ignore_rows=()):
    """
    Text-line boxes (x0, y0, x1, y1) in one greyscale frame.
    ignore_rows: [(y0, y1)] as fractions of the height, e.g. our own caption band.
    """
    H, W = grey.shape
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (_TOPHAT_K, _TOPHAT_K))
    tophat = cv2.morphologyEx(grey, cv2.MORPH_TOPHAT, kernel)
    mask = ((tophat > _TOPHAT_MIN) & (grey > _BRIGHT_MIN)).astype(np.uint8)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    glyphs = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (_GLYPH_H[0] <= h <= _GLYPH_H[1]) or w > 2.2 * h + 6 or w < 1:
            continue
        fill = area / float(w * h)
        if not 0.10 <= fill <= 0.85 or area < 8:
            continue
        cy = y + h / 2.0
        if any(a * H <= cy <= b * H for a, b in ignore_rows):
            continue
        glyphs.append((x, y, w, h, cy))
    if len(glyphs) < _LINE_MIN_GLYPHS:
        return []

    # Group glyphs on a shared baseline, then chain neighbours left to right.
    glyphs.sort(key=lambda g: g[4])
    rows, cur = [], [glyphs[0]]
    for g in glyphs[1:]:
        ref = float(np.median([c[3] for c in cur]))
        if abs(g[4] - float(np.median([c[4] for c in cur]))) <= 0.4 * ref and 0.5 <= g[3] / ref <= 2.0:
            cur.append(g)
        else:
            rows.append(cur)
            cur = [g]
    rows.append(cur)

    sobel = np.abs(cv2.Sobel(grey, cv2.CV_16S, 1, 0, ksize=3))
    lines = []
    for row in rows:
        if len(row) < _LINE_MIN_GLYPHS:
            continue
        row.sort(key=lambda g: g[0])
        med_h = float(np.median([g[3] for g in row]))
        chains, chain = [], [row[0]]
        for g in row[1:]:
            prev = chain[-1]
            if g[0] - (prev[0] + prev[2]) <= 1.6 * med_h:
                chain.append(g)
            else:
                chains.append(chain)
                chain = [g]
        chains.append(chain)
        for ch in chains:
            if len(ch) < _LINE_MIN_GLYPHS:
                continue
            x0 = min(g[0] for g in ch)
            x1 = max(g[0] + g[2] for g in ch)
            y0 = min(g[1] for g in ch)
            y1 = max(g[1] + g[3] for g in ch)
            if x1 - x0 < _LINE_MIN_W * W:
                continue
            edge = float(np.mean(sobel[y0:y1, x0:x1] > 120))
            if edge < _EDGE_MIN:
                continue
            # Titles are near-white strokes on a clearly darker scene, with
            # little ink per box; lit windows, table rows and props are dimmer,
            # lower contrast or more solid (measured on Friends S07E02).
            ink = mask[y0:y1, x0:x1].astype(bool)
            box = grey[y0:y1, x0:x1]
            fg = float(box[ink].mean()) if ink.any() else 0.0
            bg = float(box[~ink].mean()) if (~ink).any() else 255.0
            if fg < _FG_MIN or fg - bg < _CONTRAST_MIN or ink.mean() > _INK_MAX:
                continue
            lines.append((x0, y0, x1, y1))
    return lines


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def _band(y_centre, H):
    return 'top' if y_centre < H / 3 else ('bottom' if y_centre > 2 * H / 3 else 'middle')


def burned_text(video, start, end, ignore_rows=(), fps=_SAMPLE_FPS, min_persist=0.5):
    """
    Intervals of burned-in text in [start, end): [(t0, t1, band)].

    A sample only counts when a text line sits on (roughly) the same spot in
    the previous or next sample, so the text must persist >= 1/fps (0.5 s).
    """
    samples = []
    H = None
    for t, grey in _frames(video, start, end, fps):
        H = grey.shape[0]
        samples.append((t, text_lines(grey, ignore_rows)))
    flagged = []
    for k, (t, lines) in enumerate(samples):
        hit = None
        for ln in lines:
            for j in (k - 1, k + 1):
                if 0 <= j < len(samples) and any(_iou(ln, o) >= _PERSIST_IOU for o in samples[j][1]):
                    hit = ln
                    break
            if hit:
                break
        flagged.append((t, hit))

    out = []
    step = 1.0 / fps
    for t, hit in flagged:
        if hit is None:
            continue
        band = _band((hit[1] + hit[3]) / 2.0, H)
        if out and t - out[-1][1] <= step + 1e-6:
            out[-1] = (out[-1][0], t + step, out[-1][2])
        else:
            out.append((t, t + step, band))
    return [(round(a, 2), round(min(b, end), 2), band) for a, b, band in out
            if b - a >= min_persist - 1e-6]


if __name__ == '__main__':
    import sys
    v = sys.argv[1]
    s = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    e = float(sys.argv[3]) if len(sys.argv) > 3 else s + 8.0
    print(burned_text(v, s, e))
