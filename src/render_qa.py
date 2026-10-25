"""
Render QA lint for a finished Short.

Measures the final file (plus, when given, the clean pre-overlay picture and
the clip plan) and writes <out>.qa.json next to it:

  audio    integrated LUFS / true peak / LRA, loudnorm normalization_type
  edit     scene cuts on a 270 px proxy -> shot lengths, longest shot, cuts in
           the last 3 s; silence after the last word; joins that land mid-word
  text     caption cap height and centre at ~2 s; added overlay pixels below
           y 1440 / in the right rail; burned-in source text in 0-8 s (textguard)
  layout   black-bar rows and static uniform bands lasting > 0.5 s
  brand    a brief-required watermark: inside the safe zone and drawn on every
           sampled frame (final vs clean picture inside its box)
  faces    MediaPipe on 1 fps frames: face height, eye line, centre x, faces
           cut off by the video window

Only the listed items hard-fail (HARD_FAILS); everything else is a warning.
The caller decides what to do with a failure (clipper re-renders with safe
defaults where a re-render can fix it).
"""
import json
import re
import subprocess

import cv2
import numpy as np

W, H = 1080, 1920
SAFE_BOTTOM = 1440
RAIL_X, RAIL_Y = (990, (900, 1700))
LUFS_TARGET, LUFS_TOL = -14.0, 0.7
CAP_RANGE = (60, 68)
# Frame checks run on half-resolution frames (540x960): a quarter of the
# memory and decode time; results are reported in 1080x1920 canvas px.
QS = 2
HARD_FAILS = ('black_bars', 'static_band', 'overlay_below_safe', 'loudness', 'burned_text_0_8',
              'watermark_missing', 'watermark_unsafe')
# Hard fails that break the frame itself: the render gets one blurred-fill re-render,
# and if that still fails it is never uploaded (main picks another moment or skips).
LAYOUT_FAILS = ('black_bars', 'static_band')
# A watermark frame counts as drawn when this share of its box differs from the clean picture
# (the logo's glyphs, outline and shadow cover ~40-60% of the box; the enhance pass alone ~0%).
WM_DIFF, WM_MIN_SHARE, WM_MIN_FRAMES = 40, 0.08, 0.95


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors='replace')


def probe(path):
    p = _run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=width,height,codec_type',
              '-of', 'json', path])
    try:
        d = json.loads(p.stdout)
        v = next(s for s in d['streams'] if s.get('codec_type') == 'video')
        return float(d['format']['duration']), int(v['width']), int(v['height'])
    except (ValueError, KeyError, StopIteration):
        return 0.0, W, H


# --- audio -------------------------------------------------------------------

def loudness(path):
    p = _run(['ffmpeg', '-hide_banner', '-nostats', '-i', path, '-vn', '-af',
              'loudnorm=I=-14:TP=-2:LRA=11:print_format=json', '-f', 'null', '-'])
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', p.stderr)
    if not m:
        return {}
    st = json.loads(m.group(0))
    try:
        return {'lufs': float(st['input_i']), 'true_peak': float(st['input_tp']),
                'lra': float(st['input_lra'])}
    except (KeyError, ValueError):
        return {}


# --- edit --------------------------------------------------------------------

def scene_cuts(path, rows=None, thresh=0.3):
    """Output times of hard cuts (ffmpeg scene score on a 270 px proxy of `rows`)."""
    crop = f"crop=iw:{rows[1] - rows[0]}:0:{rows[0]}," if rows else ""
    p = _run(['ffmpeg', '-hide_banner', '-nostats', '-i', path, '-an', '-vf',
              f"{crop}scale=270:-2,select='gt(scene,{thresh})',showinfo", '-f', 'null', '-'])
    return [round(float(t), 3) for t in re.findall(r'pts_time:([\d.]+)', p.stderr)]


def shot_stats(cuts, dur):
    bounds = [0.0] + [c for c in cuts if 0.05 < c < dur - 0.05] + [dur]
    shots = [round(b - a, 2) for a, b in zip(bounds, bounds[1:]) if b - a > 0.01]
    return {'n_shots': len(shots), 'longest_shot': max(shots) if shots else dur,
            'mean_shot': round(float(np.mean(shots)), 2) if shots else dur,
            'last_shot': shots[-1] if shots else dur,
            'cuts_last_3s': sum(1 for c in cuts if c > dur - 3.0),
            'shots_over_4s': sum(1 for s in shots if s > 4.0)}


def mid_word_joins(joins, words, guard=0.04):
    return [round(j, 2) for j in joins
            if any(w['start'] + guard < j < w['end'] - guard for w in words)]


# --- frames ------------------------------------------------------------------

def _decode(path, fps, w, h, fmt='bgr24'):
    """All frames at `fps`, scaled to w x h, as a list of arrays."""
    ch = 3 if fmt == 'bgr24' else 1
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-an', '-vf', f'fps={fps},scale={w}:{h}',
                        '-f', 'rawvideo', '-pix_fmt', fmt, 'pipe:1'], capture_output=True)
    size = w * h * ch
    n = len(p.stdout) // size
    shape = (h, w, ch) if ch == 3 else (h, w)
    return [np.frombuffer(p.stdout[k * size:(k + 1) * size], np.uint8).reshape(shape) for k in range(n)]


def _runs(flags):
    """Longest run of True in a 1-D bool array."""
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def band_checks(path):
    """Black-bar rows and static uniform bands lasting > 0.5 s (2 fps, 270x480 grey)."""
    frames = _decode(path, 2, 270, 480, 'gray')
    if len(frames) < 2:
        return {}
    scale = H / 480.0
    stack = np.stack(frames).astype(np.float32)            # T x 480 x 270
    row_mean = stack.mean(axis=2)                           # T x 480
    row_std = stack.std(axis=2)
    lit = (row_mean > 30).mean(axis=1) > 0.3                # frame has picture (not a fade to black)
    # a bar is flat black across the whole row (a dark jacket is not)
    black = (row_mean < 3) & (row_std < 0.4) & lit[:, None]
    persist = black[:-1] & black[1:]                        # black in 2 consecutive samples (>= 0.5 s)
    bar_px = max((_runs(r) for r in persist), default=0) * scale
    # static band: rows that never change and are flat across the row
    t_std = stack.std(axis=0).mean(axis=1)                  # 480
    x_std = stack.std(axis=2).mean(axis=0)                  # 480
    static = (t_std < 1.5) & (x_std < 4.0)
    return {'black_bar_px': int(round(bar_px)), 'static_band_px': int(round(_runs(static) * scale)),
            'live_share': round(1.0 - float(static.mean()), 3)}


def _caption_box(frame, diff=None, rows=(700, 1500)):
    """(cap_px, centre_y) of the outlined white caption line in one frame, or None."""
    y0, y1 = rows[0] // QS, rows[1] // QS           # frame is half resolution
    roi = frame[y0:y1]
    white = roi.min(axis=2) > 215
    dark = roi.max(axis=2) < 60
    outlined = white & (cv2.dilate(dark.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0)
    mask = cv2.dilate(outlined.astype(np.uint8), np.ones((3, 3), np.uint8)) & white
    if diff is not None:
        mask &= diff[y0:y1]
    n, _, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    comps = [st[i] for i in range(1, n)
             if 12 <= st[i][3] <= 80 and st[i][4] > 15 and st[i][2] < 100]
    if len(comps) < 2:
        return None
    # the caption line = the tallest group of >= 2 glyphs sharing a baseline. A
    # smaller white label just under it (a brief's "BILF Podcast" handle) often
    # has more glyphs, so glyph count alone measured the label (30 px) instead.
    comps.sort(key=lambda s: s[1] + s[3])
    best, best_key = [], None
    for c in comps:
        grp = [d for d in comps if abs((d[1] + d[3]) - (c[1] + c[3])) <= 3]
        if len(grp) < 2:
            continue
        key = (float(np.median([g[3] for g in grp])) // 4, len(grp))
        if best_key is None or key > best_key:
            best, best_key = grp, key
    if len(best) < 2:
        return None
    cap = float(np.median([g[3] for g in best])) * QS
    top = min(g[1] for g in best)
    bot = max(g[1] + g[3] for g in best)
    return round(cap, 1), int((y0 + (top + bot) / 2) * QS)


def overlay_checks(final_frames, clean_frames, allowed=()):
    """Pixels we added (final vs clean picture): lowest y, right-rail hits, per-frame boxes."""
    lowest, rail, boxes = 0, 0, []
    for f, c in zip(final_frames, clean_frames):
        d = cv2.absdiff(f, c).max(axis=2) > 70
        d = cv2.morphologyEx(d.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)) > 0
        for x0, y0, x1, y1 in allowed:
            d[y0 // QS:-(-y1 // QS), x0 // QS:-(-x1 // QS)] = False
        ys, xs = np.nonzero(d)
        if len(ys) < 8:
            boxes.append(None)
            continue
        boxes.append((int(xs.min()) * QS, int(ys.min()) * QS, int(xs.max()) * QS, int(ys.max()) * QS))
        lowest = max(lowest, int(np.percentile(ys, 99.9)) * QS)
        band = d[RAIL_Y[0] // QS:RAIL_Y[1] // QS, RAIL_X // QS:]
        rail = max(rail, int(band.sum()) * QS * QS)
    return {'overlay_lowest_y': lowest, 'overlay_rail_px': rail}, boxes


def watermark_checks(final_frames, clean_frames, box):
    """Share of sampled frames whose watermark box holds the logo (final vs clean picture)."""
    x0, y0, x1, y1 = (int(v) // QS for v in box)
    shares = []
    for f, c in zip(final_frames, clean_frames):
        d = cv2.absdiff(f[y0:y1, x0:x1], c[y0:y1, x0:x1]).max(axis=2) > WM_DIFF
        shares.append(float(d.mean()) if d.size else 0.0)
    if not shares:
        return {}
    return {'watermark_frames': round(sum(s >= WM_MIN_SHARE for s in shares) / len(shares), 3),
            'watermark_cover': round(float(np.median(shares)), 3)}


def face_metrics(frames, window=None):
    """Median face size / eye line / centre x on output frames (fractions of the canvas)."""
    try:
        import mediapipe as mp
        from reframer import _create_face_landmarker
    except Exception as exc:                      # face metrics are a warning-only nicety
        return {'face_error': str(exc)}
    lm = _create_face_landmarker()
    hs, eyes, cxs, tops, clipped, two, both_in = [], [], [], [], 0, 0, 0
    wy0, wy1 = window or (0, H)
    try:
        for fr in frames:
            rgb = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
            res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
            faces = []
            for f in res.face_landmarks:
                ys = [p.y * H for p in f]
                xs = [p.x * W for p in f]
                eye = (f[159].y + f[386].y) / 2 * H
                faces.append((min(xs), min(ys), max(xs), max(ys), eye))
            if not faces:
                continue
            big = max(faces, key=lambda b: b[3] - b[1])
            hs.append((big[3] - big[1]) / H)
            eyes.append(big[4] / H)
            cxs.append((big[0] + big[2]) / 2 / W)
            tops.append(big[1])
            cut = [b for b in faces if b[0] <= 3 or b[2] >= W - 3 or b[1] <= wy0 + 3 or b[3] >= wy1 - 3]
            clipped += bool(cut)
            if len(faces) >= 2:
                two += 1
                both_in += not cut
    finally:
        lm.close()
    if not hs:
        return {'face_frames': 0}
    return {'face_frames': len(hs), 'face_h': round(float(np.median(hs)), 3),
            'eye_line': round(float(np.median(eyes)), 3), 'face_cx': round(float(np.median(cxs)), 3),
            'face_top_min_px': int(min(tops)), 'faces_clipped_share': round(clipped / len(hs), 3),
            'two_face_frames': two, 'two_face_both_inside': round(both_in / two, 3) if two else None}


# --- main entry ----------------------------------------------------------------

def run(path, ctx=None, write=True):
    """
    Measure `path` and return the QA dict; writes <path>.qa.json unless write=False.

    ctx (all optional): template, niche, words (output-time word dicts),
    joins (output-time sub-clip joins), clean_video (picture before overlays),
    caption_y (target centre), allowed_boxes [(x0, y0, x1, y1)] that may hold
    overlay pixels below the safe line (the handle), window (y0, y1) of the
    video window, live_rows (y0, y1) for cut detection, header_rows /
    caption_rows (fractions) to ignore when looking for burned-in text without
    a clean picture, normalization_type, render_seconds.
    """
    ctx = ctx or {}
    template = ctx.get('template', 'legacy')
    niche = ctx.get('niche')
    dur, _, _ = probe(path)
    qa = {'file': path.replace('\\', '/').rsplit('/', 1)[-1], 'duration': round(dur, 2),
          'template': template, 'niche': niche}
    for k in ('normalization_type', 'render_seconds', 'variant', 'punches', 'payoff_out', 'qa_retry', 'letterbox',
              'loop_seam', 'captions_engine', 'remotion_fallback'):
        if ctx.get(k) is not None:
            qa[k] = ctx[k]

    qa.update(loudness(path))
    cuts = scene_cuts(path, ctx.get('live_rows'))
    qa['cuts'] = cuts
    qa.update(shot_stats(cuts, dur))
    # punch-ins (clip plan) are edit events too: the shot cap counts them
    punches = [t for t, *_ in (ctx.get('punches') or [])]
    if punches:
        raw = qa['longest_shot']
        qa.update(shot_stats(sorted(set(cuts) | set(punches)), dur))
        qa['longest_scene_shot'] = raw
    words = ctx.get('words') or []
    if words:
        qa['tail_s'] = round(dur - words[-1]['end'], 2)
        qa['mid_word_joins'] = mid_word_joins(ctx.get('joins') or [], words)
    qa.update(band_checks(path))

    final1 = _decode(path, 1, W // QS, H // QS)
    clean = ctx.get('clean_video')
    clean1 = _decode(clean, 1, W // QS, H // QS) if clean else None
    # the loop seam dissolves the whole picture into the opening frame, so the final vs
    # clean diff there is not an overlay: 1 fps samples inside the seam are left out
    seam = float(ctx.get('loop_seam') or 0)
    n_ov = len(final1) if not seam else max(1, int(dur - seam - 0.5) + 1)
    if clean1:
        ov, boxes = overlay_checks(final1[:n_ov], clean1[:n_ov], ctx.get('allowed_boxes') or ())
        qa.update(ov)
    wm = ctx.get('watermark_box')
    if wm:
        qa['watermark_box'] = [int(v) for v in wm]
        if clean1:
            qa.update(watermark_checks(final1[:n_ov], clean1[:n_ov], wm))
    # caption size / position: median over the 1 fps frames of 1-8 s (one
    # frame can be thrown by burned-in text or a frame with no caption up)
    ty = ctx.get('caption_y')
    caps = []
    for t in range(1, min(9, len(final1))):
        diff = None
        if clean1 and t < len(clean1):
            diff = cv2.absdiff(final1[t], clean1[t]).max(axis=2) > 40
        c = _caption_box(final1[t], diff, (ty - 100, ty + 100) if ty else (700, 1500))
        if c:
            caps.append(c)
    if caps:
        qa['caption_cap_px'] = float(np.median([c[0] for c in caps]))
        qa['caption_cy'] = int(np.median([c[1] for c in caps]))
        qa['caption_frames'] = len(caps)
    qa.update(face_metrics(final1, ctx.get('window')))

    try:
        from textguard import burned_text
        if clean:
            hits = burned_text(clean, 0.0, min(8.0, dur))
        else:
            # our own watermark is not source text
            ignore = list(ctx.get('ignore_text_rows', ()))
            if wm:
                ignore.append((wm[1] / H, wm[3] / H))
            hits = burned_text(path, 0.0, min(8.0, dur), ignore_rows=ignore)
        qa['burned_text'] = [list(h) for h in hits]
    except Exception as exc:
        qa['burned_text_error'] = str(exc)

    qa['hard_fail'], qa['warnings'] = evaluate(qa, ctx)
    qa['ok'] = not qa['hard_fail']
    if write:
        with open(path + '.qa.json', 'w', encoding='utf-8') as f:
            json.dump(qa, f, indent=1)
    return qa


def evaluate(qa, ctx):
    """(hard_fail, warnings) lists of short reason strings."""
    template = ctx.get('template', 'legacy')
    niche = ctx.get('niche')
    hard, warn = [], []
    if qa.get('black_bar_px', 0) >= 24:
        hard.append(f"black_bars: {qa['black_bar_px']} px of black rows")
    if template != 'legacy' and qa.get('static_band_px', 0) >= 40:
        hard.append(f"static_band: {qa['static_band_px']} px static uniform band")
    if qa.get('overlay_lowest_y', 0) > SAFE_BOTTOM:
        hard.append(f"overlay_below_safe: overlay pixels down to y {qa['overlay_lowest_y']}")
    if 'lufs' in qa and abs(qa['lufs'] - LUFS_TARGET) > LUFS_TOL:
        hard.append(f"loudness: {qa['lufs']:.1f} LUFS")
    wm = qa.get('watermark_box')
    if wm:
        x0, y0, x1, y1 = wm
        if x0 < 0 or y0 < 0 or y1 > SAFE_BOTTOM or x1 > RAIL_X:
            hard.append(f"watermark_unsafe: box {wm} outside the safe zone (y <= {SAFE_BOTTOM}, x <= {RAIL_X})")
        if qa.get('watermark_frames') is not None and qa['watermark_frames'] < WM_MIN_FRAMES:
            hard.append(f"watermark_missing: drawn on {qa['watermark_frames']:.0%} of sampled frames")
    if qa.get('burned_text'):
        # podcasts carry their own lower thirds / slides: warn only on niche B
        # (and a brand show's own graphics when the profile says so: render['burned_text_ok'])
        target = warn if niche == 'B' or ctx.get('burned_text_ok') else hard
        target.append(f"burned_text_0_8: {qa['burned_text']}")

    if qa.get('true_peak', -99) > -1.8:
        warn.append(f"true peak {qa['true_peak']:.1f} dBTP")
    if qa.get('normalization_type') and qa['normalization_type'] != 'linear':
        warn.append(f"loudnorm ran {qa['normalization_type']}")
    if niche == 'A' and qa.get('lra', 99) < 4:
        warn.append(f"LRA {qa['lra']:.1f} LU (< 4)")
    if qa.get('longest_shot', 0) > 4.0:
        warn.append(f"longest shot {qa['longest_shot']:.2f} s")
    tail = qa.get('tail_s')
    if tail is not None:
        lo, hi = (0.8, 1.6) if niche == 'A' else ((0.4, 0.7) if niche == 'B' else (0.0, 2.0))
        if not lo <= tail <= hi:
            warn.append(f"tail {tail:.2f} s outside {lo}-{hi}")
        if qa.get('cuts_last_3s') == 0 and tail > 2.0:
            warn.append("no cut in the last 3 s and tail > 2 s")
    if qa.get('mid_word_joins'):
        warn.append(f"mid-word joins at {qa['mid_word_joins']}")
    if template != 'legacy':
        cap = qa.get('caption_cap_px')
        if cap is not None and not CAP_RANGE[0] <= cap <= CAP_RANGE[1]:
            warn.append(f"caption cap height {cap} px")
        if cap is not None and ctx.get('caption_y') and abs(qa['caption_cy'] - ctx['caption_y']) > 20:
            warn.append(f"caption centre {qa['caption_cy']} vs target {ctx['caption_y']}")
    if qa.get('overlay_rail_px', 0) > 200:
        warn.append(f"overlay in the right rail ({qa['overlay_rail_px']} px)")
    if qa.get('face_frames'):
        if template in ('B1', 'A2') and not 0.21 <= qa['face_h'] <= 0.29:
            warn.append(f"face height {qa['face_h']}")
        if template == 'A1' and qa['face_h'] < 0.17:
            warn.append(f"face height {qa['face_h']} (< 0.17)")
        if qa.get('face_top_min_px', 999) < 140:
            warn.append(f"head top at y {qa['face_top_min_px']}")
        if qa.get('two_face_both_inside') is not None and qa['two_face_both_inside'] < 0.95:
            warn.append(f"two-shots with a face cut off: {1 - qa['two_face_both_inside']:.0%}")
    return hard, warn


if __name__ == '__main__':
    import sys
    import time
    t0 = time.time()
    c = {}
    if len(sys.argv) > 2:
        c = json.loads(sys.argv[2])
    r = run(sys.argv[1], c, write='--nowrite' not in sys.argv)
    r.pop('cuts', None)
    print(json.dumps(r, indent=1))
    print(f'qa {time.time() - t0:.1f}s')
